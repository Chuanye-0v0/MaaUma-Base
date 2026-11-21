using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>The active ordinary-panel ADB selection, validated without contacting a device.</summary>
public sealed record MaaUmaSharedConnection(string AdbPath, string Endpoint)
{
    public static MaaUmaSharedConnection FromValues(string adbPath, string address)
    {
        var endpoint = UmaPublicRuntime.NormalizeAddress(address);
        if (string.IsNullOrWhiteSpace(adbPath))
            throw new InvalidOperationException("请先在日活页选择本机模拟器的 ADB 设备。");
        string fullPath;
        try { fullPath = Path.GetFullPath(adbPath); }
        catch (Exception error) when (error is ArgumentException or NotSupportedException or IOException)
        { throw new InvalidOperationException("主面板的 ADB 路径无效，请重新选择设备。", error); }
        if (!File.Exists(fullPath))
            throw new InvalidOperationException("主面板的 ADB 文件不存在，请重新选择设备。");
        return new(fullPath, endpoint);
    }

    public static MaaUmaSharedConnection ReadActive()
    {
        var queue = Instances.InstanceTabBarViewModel.ActiveTab?.TaskQueueViewModel;
        if (queue == null || queue.CurrentController != MaaControllerTypes.Adb)
            throw new InvalidOperationException("请先在日活页选择本机模拟器的 ADB 设备。");
        var device = queue.Processor.Config.AdbDevice;
        return FromValues(device.AdbPath, device.AdbSerial);
    }

    internal bool SameTarget(MaaUmaSharedConnection? other) => other != null
        && string.Equals(AdbPath, other.AdbPath, StringComparison.OrdinalIgnoreCase)
        && string.Equals(Endpoint, other.Endpoint, StringComparison.Ordinal);
}

/// <summary>
/// Serializes the portable host with the shell selection. A running host keeps its
/// original target until it confirms idle/stopped; stop never depends on selection.
/// </summary>
public sealed class MaaUmaSharedConnectionBridge : IUmaPacerBridge
{
    private readonly string root;
    private readonly IUmaPacerBridge inner;
    private readonly Func<MaaUmaSharedConnection> readConnection;
    private readonly SemaphoreSlim gate = new(1, 1);
    private volatile MaaUmaSharedConnection? bound;
    private volatile string? observedEndpoint;
    private volatile string? observedRunRoot;
    private volatile string state = "idle";
    private volatile bool verified;

    public MaaUmaSharedConnectionBridge(string pacerRoot, IUmaPacerBridge inner,
        Func<MaaUmaSharedConnection>? readConnection = null)
    {
        root = Path.GetFullPath(pacerRoot);
        this.inner = inner;
        this.readConnection = readConnection ?? MaaUmaSharedConnection.ReadActive;
    }

    public MaaUmaSharedConnection? BoundConnection => bound;
    public bool ConnectionVerified
    {
        get { var selected = Selection(); return verified && bound?.SameTarget(selected) == true; }
    }
    public string? ConnectionMessage { get; private set; }
    public string RuntimeState => state;
    public bool IsIdle => state is "idle" or "stopped";
    public string? CloseError => inner.CloseError;
    public event Action<JObject>? SnapshotReceived;

    public MaaUmaSharedConnection? SelectedConnection => Selection();

    private MaaUmaSharedConnection? Selection()
    {
        try
        {
            var value = readConnection();
            // The offline reader substitutes selection only, never validation.
            var selected = MaaUmaSharedConnection.FromValues(value.AdbPath, value.Endpoint);
            if (bound?.SameTarget(selected) != true)
            {
                verified = false;
                ConnectionMessage = !IsIdle
                    ? $"后台仍有运行或暂停任务，当前任务继续使用 {bound?.Endpoint ?? observedEndpoint ?? "原连接"}；请先停止当前任务。"
                    : "与日活共用当前连接；检查或开始前会同步后台。";
            }
            else ConnectionMessage = null;
            return selected;
        }
        catch (Exception error)
        {
            verified = false;
            ConnectionMessage = error.Message;
            return null;
        }
    }

    public UmaPacerContext ReadContext(Func<string, bool>? dailyRunning = null)
    {
        var selected = Selection();
        var endpoint = !IsIdle ? bound?.Endpoint ?? observedEndpoint ?? "" : selected?.Endpoint ?? "";
        return new(endpoint, endpoint.Length != 0 && dailyRunning?.Invoke(endpoint) == true,
            verified && bound?.SameTarget(selected) == true);
    }

    // These actions edit/read application-local settings only. The host still
    // validates values and rejects preset writes while any worker is active.
    private static bool LocalConfiguration(string action) => action is "configuration_snapshot"
        or "career_presets" or "cut_presets" or "save_career_preset" or "save_cut_preset"
        or "select_career_preset" or "select_cut_preset" or "decision_preferences_schema"
        or "validate_decision_preferences" or "ai_schema" or "continuation_schema"
        or "ai_status" or "ai_test" or "ai_configure" or "ai_clear"
        or "workflow_schema" or "validate_workflow" or "validate_workflow_start" or "save_workflow" or "load_workflow"
        or "workflow_library" or "workflow_library/list" or "list_workflow_library"
        or "load_workflow_library" or "save_workflow_library" or "resolve_workflow_configuration"
        or "workflow_library_v2" or "load_workflow_library_v2" or "save_workflow_library_v2" or "resolve_workflow_configuration_v2"
        or "preview_decision_preferences"
        or "search_events" or "validate_event_preferences"
        or "runtime_stop_evidence" or "prepare_runtime_stop_report" or "submit_runtime_stop_report";

    private static bool Polling(string action) => action is "snapshot" or "list"
        or "decision_preferences_schema" or "ai_schema" or "continuation_schema"
        or "career_presets" or "cut_presets" or "validate_decision_preferences";

    private void Observe(JObject response)
    {
        var snapshot = response["snapshot"] as JObject
            ?? (response["control"] is JObject ? response : null);
        if (snapshot == null) return;
        if (snapshot["endpoint"]?.Type == JTokenType.String
            && !string.IsNullOrWhiteSpace((string?)snapshot["endpoint"]))
        {
            try { observedEndpoint = UmaPublicRuntime.NormalizeAddress((string)snapshot["endpoint"]!); }
            catch (InvalidOperationException) { observedEndpoint = null; }
        }
        state = (string?)snapshot["control"]?["state"] ?? "unknown";
        observedRunRoot=(string?)snapshot["run_root"];
        SnapshotReceived?.Invoke(snapshot);
    }

    private bool ProjectionMatches(MaaUmaSharedConnection selected)
    {
        var path = Path.Combine(root, "portable_config.json");
        if (!File.Exists(path)) return false;
        var value = JObject.Parse(File.ReadAllText(path));
        var adb = (string?)value["adb_path"] ?? "";
        if (!Path.IsPathRooted(adb)) adb = Path.GetFullPath(Path.Combine(root, adb));
        return string.Equals(adb, selected.AdbPath, StringComparison.OrdinalIgnoreCase)
            && (int?)value["adb_port"] == int.Parse(selected.Endpoint[10..], System.Globalization.CultureInfo.InvariantCulture);
    }

    private void SaveProjection(MaaUmaSharedConnection selected)
    {
        var path = Path.Combine(root, "portable_config.json");
        var value = File.Exists(path) ? JObject.Parse(File.ReadAllText(path)) : new JObject();
        value["adb_path"] = selected.AdbPath;
        value["adb_port"] = int.Parse(selected.Endpoint[10..], System.Globalization.CultureInfo.InvariantCulture);
        Directory.CreateDirectory(root);
        var temporary = path + ".shared-" + Guid.NewGuid().ToString("N") + ".tmp";
        File.WriteAllText(temporary, value.ToString());
        File.Move(temporary, path, true);
    }

    private async Task Bind(MaaUmaSharedConnection selected)
    {
        if (bound?.SameTarget(selected) == true && ProjectionMatches(selected)) return;
        verified = false;
        if (!IsIdle)
            throw new InvalidOperationException("当前育成或刷切任务尚未停止，不能切换模拟器连接。");
        if (inner is UmaPacerBridge portable)
            await portable.PrepareConnectionChange();
        else if (bound != null)
        {
            // Fake/offline bridges have no child process to close, but must obey
            // exactly the same freshly-confirmed idle/stopped precondition.
            var snapshot = await inner.Request(new JObject { ["action"] = "snapshot", ["endpoint"] = bound.Endpoint });
            Observe(snapshot);
            if (!IsIdle) throw new InvalidOperationException("当前任务尚未停止，连接未更改。");
        }
        var latest = Selection();
        if (!selected.SameTarget(latest))
            throw new InvalidOperationException("主面板连接刚刚变化，请重新操作；尚未启动任务。");
        SaveProjection(selected);
        bound = selected;
        observedEndpoint = selected.Endpoint;
        state = "idle";
        ConnectionMessage = null;
    }

    /// <summary>Read the existing host state for updates without selecting or probing a device.</summary>
    public async Task<bool> CheckRuntimeIdle()
    {
        await gate.WaitAsync();
        try
        {
            var request = new JObject { ["action"] = "snapshot" };
            if (bound != null) request["endpoint"] = bound.Endpoint;
            var response = await inner.Request(request);
            var snapshot = response["snapshot"] as JObject ?? response;
            Observe(response);
            var actualState = (string?)snapshot["control"]?["state"];
            if (actualState == null) state = "unknown";
            return actualState is "idle" or "stopped";
        }
        catch { state = "unknown"; throw; }
        finally { gate.Release(); }
    }

    public async Task<JObject> Request(JObject request)
    {
        await gate.WaitAsync();
        var action = (string?)request["action"] ?? "";
        var sent = false;
        try
        {
            if(LocalConfiguration(action))
            {
                // No selection, connection projection, probe, or device binding
                // is needed to prepare the next run's local configuration.
                var local=(JObject)request.DeepClone();local.Remove("endpoint");
                var localResponse=await inner.Request(local);
                Observe(localResponse);return localResponse;
            }
            if(action=="update_manual_handoff_plan")
            {
                // A plan edit is local configuration for the original run. It
                // must not bind/reconfigure ADB, probe, pause or start a worker.
                var endpoint=(string?)request["endpoint"];
                var run=(string?)request["run_root"];
                if((string?)request["mode"]!="career"||string.IsNullOrWhiteSpace(endpoint)||string.IsNullOrWhiteSpace(run))
                    throw new InvalidOperationException("请先读取本局暂停计划。");
                if(!IsIdle&&(!string.Equals(endpoint,bound?.Endpoint??observedEndpoint,StringComparison.OrdinalIgnoreCase)
                    ||observedRunRoot!=null&&!string.Equals(run,observedRunRoot,StringComparison.OrdinalIgnoreCase)))
                    throw new InvalidOperationException("暂停计划必须属于当前运行的同一局与设备。");
                var planResponse=await inner.Request((JObject)request.DeepClone());
                Observe(planResponse);return planResponse;
            }
            // Native diagnostics bypasses device selection/binding; it reads registered logs only.
            if(action=="export_stop_diagnostics")return await inner.Request((JObject)request.DeepClone());
            var control = action is "pause" or "stop";
            var selected = Selection();
            if (bound == null && !IsIdle)
            {
                // An update-only snapshot can discover an existing host before
                // the shell has selected any device. Keep controlling that same
                // stdio host; neither guess its ADB path nor bind a new target.
                if (!control && !Polling(action))
                    throw new InvalidOperationException("原后台仍有活动任务，请先停止原任务，再使用主面板当前连接。");
                if (observedEndpoint == null && action is not ("snapshot" or "pause" or "stop"))
                    throw new InvalidOperationException("原后台尚未返回设备地址；可以继续查询或停止原任务。");
                var original = (JObject)request.DeepClone();
                if (observedEndpoint != null) original["endpoint"] = observedEndpoint;
                else original.Remove("endpoint");
                if (action == "stop") state = "stop_requested";
                else if (action == "pause") state = "pause_requested";
                sent = true;
                var originalResponse = await inner.Request(original);
                Observe(originalResponse);
                return originalResponse;
            }
            if (!control)
            {
                if (bound != null && !IsIdle)
                {
                    if (!bound.SameTarget(selected) && !Polling(action))
                        throw new InvalidOperationException("主面板连接已变化；当前任务仍绑定原设备，请先停止当前任务。");
                }
                else
                {
                    if (selected == null)
                        throw new InvalidOperationException(ConnectionMessage ?? "请先在系统设置中选择模拟器。");
                    if (!Polling(action) && request["endpoint"] is JValue endpoint
                        && endpoint.Type == JTokenType.String && !string.IsNullOrWhiteSpace((string?)endpoint)
                        && UmaPublicRuntime.NormalizeAddress((string)endpoint!) != selected.Endpoint)
                        throw new InvalidOperationException("主面板连接已变化，请重新操作；尚未启动任务。");
                    await Bind(selected);
                }
            }
            if (bound == null)
            {
                if (selected == null)
                    throw new InvalidOperationException(ConnectionMessage ?? "尚未选择模拟器连接。");
                await Bind(selected);
            }
            var payload = (JObject)request.DeepClone();
            payload["endpoint"] = bound!.Endpoint;
            if (action is "start" or "start_workflow" or "resume" or "resume_manual_handoff" or "continue_cut_as_career") state = "starting";
            else if (action == "stop") state = "stop_requested";
            else if (action == "pause") state = "pause_requested";
            sent = true;
            var response = await inner.Request(payload);
            Observe(response);
            if (action == "probe_device")
                verified = (bool?)response["ok"] == true && bound.SameTarget(Selection());
            return response;
        }
        catch
        {
            if (action == "probe_device") verified = false;
            if (sent && (action is "start" or "start_workflow" or "resume" or "resume_manual_handoff" or "continue_cut_as_career" or "pause" or "stop"))
                state = "unknown";
            throw;
        }
        finally { gate.Release(); }
    }
}
