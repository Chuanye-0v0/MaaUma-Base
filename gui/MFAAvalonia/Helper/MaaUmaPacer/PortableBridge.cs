using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public interface IUmaPacerBridge
{
    Task<JObject> Request(JObject request);
    string? CloseError => null;
}

public sealed record UmaPacerContext(string Endpoint, bool DailyRunning, bool Connected);

/// <summary>One private stdio host. Never retries an uncertain control request.</summary>
public sealed class UmaPacerBridge : IUmaPacerBridge
{
    private static readonly List<UmaPacerBridge> Bridges = new();
    public static readonly UmaPacerBridge Instance = new();
    private static bool closingAll;
    private readonly SemaphoreSlim gate = new(1, 1);
    private Process? process;
    private bool broken;
    private bool closing;
    private bool closed;
    private bool detachedForExit;
    private readonly object protocolSync = new();
    private Task<string?>? pendingReply;
    private Task<string?>? staleSnapshotRead;
    private string? staleSnapshotRequestId;
    internal TimeSpan SnapshotTimeout { get; set; } = TimeSpan.FromSeconds(45);
    internal int MaxConsecutiveSnapshotTimeouts { get; set; } = 3;
    internal TimeSpan BrokenProcessGracePeriod { get; set; } = TimeSpan.FromSeconds(10);
    private int consecutiveSnapshotTimeouts;
    private DateTime brokenAtUtc;
    private Task? closeTask;
    private Task? stdoutDrain;
    private string? protocolFailure;
    private string? diagnosticRoot;
    public string? CloseError { get; private set; }

    private readonly string rootDirectory;
    private readonly Func<ProcessStartInfo>? launchOverride;
    private readonly bool allowCareer;
    public string DefaultMode {get;private set;}="cut";
    public event Action<JObject>? SnapshotReceived;
    public UmaPacerBridge(string? rootDirectory=null, Func<ProcessStartInfo>? launchOverride=null,bool allowCareer=false)
    {
        this.rootDirectory=Path.GetFullPath(rootDirectory ?? AppContext.BaseDirectory);
        this.launchOverride=launchOverride;
        this.allowCareer=allowCareer;
        lock (Bridges) Bridges.Add(this);
    }

    public static UmaPacerContext ReadContext() => new(PortableConnection.Load(AppContext.BaseDirectory).Endpoint,false,false);

    public async Task PrepareConnectionChange()
    {
        if(process is not {HasExited:false})return;
        var result=await Request(new JObject {["action"]="snapshot"});
        if((string?)result["control"]?["state"] is not ("idle" or "stopped"))
            throw new InvalidOperationException("请先停止当前批次，再修改模拟器连接。");
        await gate.WaitAsync();
        try { process!.StandardInput.Close();await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));process.Dispose();process=null; }
        finally { gate.Release(); }
    }

    private static void KillProcessTreeSafe(Process? proc)
    {
        if (proc == null) return;
        try
        {
            if (!proc.HasExited)
                proc.Kill(entireProcessTree: true);
        }
        catch { }
        try { proc.WaitForExit(3000); } catch { }
    }

    private void EnsureProcess()
    {
        if (process is { HasExited: false })
        {
            bool shouldKillBroken;
            DateTime brokenSince;
            lock (protocolSync)
            {
                if (!broken) return;
                brokenSince = brokenAtUtc;
                shouldKillBroken = DateTime.UtcNow - brokenSince >= BrokenProcessGracePeriod;
            }
            if (!shouldKillBroken)
                throw new InvalidOperationException("控制通信中断，原后台正在安全停止；不会自动重放指令。 " + protocolFailure);
            try { LoggerHelper.Warning($"[PacerBridge] 原后台超过停机宽限期，清理并重建进程（pid={process.Id}）。"); } catch { }
            KillProcessTreeSafe(process);
            process = null;
            lock (protocolSync)
            {
                broken = false;
                staleSnapshotRead = null;
                staleSnapshotRequestId = null;
                consecutiveSnapshotTimeouts = 0;
                protocolFailure = null;
                stdoutDrain = null;
            }
        }
        else
        {
            process?.Dispose();
            process = null;
            lock (protocolSync)
            {
                broken = false;
                staleSnapshotRead = null;
                staleSnapshotRequestId = null;
                consecutiveSnapshotTimeouts = 0;
                protocolFailure = null;
                stdoutDrain = null;
            }
        }
        if (closing || closed) throw new InvalidOperationException("面板正在关闭。");
        var root = rootDirectory;
        var python = Path.Combine(root,"runtime","python.exe");
        var host = Path.Combine(root,"backend","scripts","larc_cut_standalone_host.py");
        if(launchOverride==null && (!File.Exists(python) || !File.Exists(host)))
            throw new InvalidOperationException("便携包不完整：缺少runtime/python.exe或刷切后台，请完整解压。");
        var info = launchOverride?.Invoke() ?? new ProcessStartInfo(python);
        info.WorkingDirectory = root; info.UseShellExecute=false;info.CreateNoWindow=true;
        info.RedirectStandardInput=true;info.RedirectStandardOutput=true;info.RedirectStandardError=true;
        info.StandardInputEncoding=new UTF8Encoding(false);info.StandardOutputEncoding=Encoding.UTF8;info.StandardErrorEncoding=Encoding.UTF8;
        if(launchOverride==null)foreach(var argument in new[] {"-B","-X","utf8","-u",host,"--config",Path.Combine(root,"portable_config.json")})info.ArgumentList.Add(argument);
        info.Environment["PYTHONUTF8"]="1";info.Environment["PYTHONIOENCODING"]="utf-8";info.Environment["PYTHONDONTWRITEBYTECODE"]="1";
        var temporary=Path.Combine(root,"runs","pacer-tmp");Directory.CreateDirectory(temporary);
        info.Environment["TEMP"]=temporary;info.Environment["TMP"]=temporary;
        AppRuntime.EnsureWritableProcessEnvironment();
        if (!info.Environment.TryGetValue("UMA_PUBLIC_LOCK_ROOT", out var configuredLockRoot) || string.IsNullOrWhiteSpace(configuredLockRoot))
        {
            var envLockRoot = Environment.GetEnvironmentVariable("UMA_PUBLIC_LOCK_ROOT");
            if (!string.IsNullOrWhiteSpace(envLockRoot))
            {
                info.Environment["UMA_PUBLIC_LOCK_ROOT"] = envLockRoot;
            }
            else
            {
                var localAppData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
                var defaultLockDir = string.IsNullOrWhiteSpace(localAppData) ? null : Path.Combine(localAppData, "UmaSuite", "device-locks");
                if (string.IsNullOrWhiteSpace(defaultLockDir) || !IsDirectoryWritable(defaultLockDir))
                {
                    var trimmedRoot = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                    var appRoot = Path.GetFileName(trimmedRoot).Equals("pacer", StringComparison.OrdinalIgnoreCase)
                        ? (Path.GetDirectoryName(trimmedRoot) ?? trimmedRoot)
                        : trimmedRoot;
                    var appLockRoot = Path.Combine(appRoot, "temp");
                    try
                    {
                        Directory.CreateDirectory(appLockRoot);
                        info.Environment["UMA_PUBLIC_LOCK_ROOT"] = appLockRoot;
                    }
                    catch { }
                }
            }
        }
        process = Process.Start(info) ?? throw new InvalidOperationException("育成后台未能启动。");
        diagnosticRoot = root;
        // Drain diagnostics without storing credentials/screens or unbounded text.
        process.ErrorDataReceived += (_, _) => { };
        process.BeginErrorReadLine();
        broken = false;
        stdoutDrain = null;
        protocolFailure = null;
        staleSnapshotRead = null;
        staleSnapshotRequestId = null;
    }

    private static bool IsDirectoryWritable(string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
            return false;
        try
        {
            Directory.CreateDirectory(path);
            var probe = Path.Combine(path, $".lock_probe_{Guid.NewGuid():N}");
            File.WriteAllText(probe, "1");
            File.Delete(probe);
            return true;
        }
        catch
        {
            return false;
        }
    }

    private void BreakProtocol(Task<string?>? pendingRead = null,string kind = "unspecified")
    {
        lock(protocolSync)
        {
            broken = true;
            brokenAtUtc = DateTime.UtcNow;
            var failed = process;
            var read = pendingRead ?? staleSnapshotRead ?? pendingReply;
            staleSnapshotRead = null;
            staleSnapshotRequestId = null;
            if(failed == null)return;
            // Closing a pipe may itself wait on a blocked writer. Keep it off
            // the UI thread and preserve the one outstanding stdout reader.
            var pid=failed.Id;
            protocolFailure=$"[{kind}; pid={pid}]";
            _ = Task.Run(() => {
                var stdinClosed=false;
                // StreamWriter.Close can reject disposal during WriteAsync.
                // Close the owned pipe itself to cancel that pending write.
                try { failed.StandardInput.BaseStream.Close();stdinClosed=true; }
                catch(IOException) { }
                catch(ObjectDisposedException) { }
                catch(InvalidOperationException) { }
                RecordProtocolFailure(pid,kind,stdinClosed);
            });
            stdoutDrain ??= Task.Run(()=>DrainFailedOutput(failed,read));
        }
    }

    private void RecordProtocolFailure(int pid,string kind,bool stdinClosed)
    {
        protocolFailure=$"[{kind}; pid={pid}]";
        try { LoggerHelper.Warning($"[PacerBridge] 协议中断：kind={kind}, child_pid={pid}, stdin_closed={stdinClosed}"); } catch { }
        // Record categories only: never a raw response, command or credential.
        if(diagnosticRoot == null)return;
        try
        {
            var directory=Path.Combine(diagnosticRoot,"runs","pacer-protocol");Directory.CreateDirectory(directory);
            var stamp=DateTime.UtcNow;
            var data=new JObject { ["type"]=kind,["child_pid"]=pid,["observed_at_utc"]=stamp.ToString("O"),
                ["stdin_close_requested"]=true,["stdin_closed"]=stdinClosed,["stdout_drain_requested"]=true };
            File.WriteAllText(Path.Combine(directory,$"{pid}_{stamp:yyyyMMddTHHmmssfff}.json"),data.ToString(Formatting.None),new UTF8Encoding(false));
        }
        catch(IOException) { }
        catch(UnauthorizedAccessException) { }
    }

    private static async Task DrainFailedOutput(Process failed,Task<string?>? pendingRead)
    {
        try
        {
            // WaitAsync timeout leaves ReadLineAsync alive. Hand that read to
            // the drain instead of starting concurrent reads on StreamReader.
            if(pendingRead != null)await pendingRead.ConfigureAwait(false);
            var buffer = new char[4096];
            while(await failed.StandardOutput.ReadAsync(buffer,0,buffer.Length).ConfigureAwait(false)>0) { }
        }
        catch(IOException) { }
        catch(ObjectDisposedException) { }
    }

    private void ValidateSynchronizedReply(string? line, string expectedRequestId, string? actionName, out JObject response)
    {
        if (line == null)
        {
            if (actionName != "export_stop_diagnostics") BreakProtocol(kind: "eof");
            throw new InvalidOperationException("育成后台已退出，请先查看原记录中的停点。 " + protocolFailure);
        }
        try { response = JObject.Parse(line); }
        catch (JsonReaderException)
        {
            if (actionName != "export_stop_diagnostics") BreakProtocol(kind: "non_json");
            throw new InvalidOperationException("后台输出不是控制回执，已请求安全停止；不会重放指令。 " + protocolFailure);
        }
        if ((string?)response["request_id"] != expectedRequestId)
        {
            if (actionName != "export_stop_diagnostics") BreakProtocol(kind: "id_mismatch");
            throw new InvalidOperationException("控制回执不匹配，已请求安全停止。 " + protocolFailure);
        }
        lock (protocolSync) consecutiveSnapshotTimeouts = 0;
    }

    private void ResetStalledHost(Task<string?> failedRead)
    {
        BreakProtocol(failedRead, "snapshot_stalled_limit");
        var stalled = process;
        try { LoggerHelper.Error($"[PacerBridge] 连续只读 snapshot 超时已达上限，重置卡死后台（pid={stalled?.Id}）。"); } catch { }
        lock (protocolSync)
        {
            process = null;
            broken = false;
            staleSnapshotRead = null;
            staleSnapshotRequestId = null;
            consecutiveSnapshotTimeouts = 0;
            protocolFailure = null;
            stdoutDrain = null;
        }
        KillProcessTreeSafe(stalled);
    }

    private JObject ParseAndValidateReply(string? line, string expectedRequestId, string? actionName, string? requestMode)
    {
        ValidateSynchronizedReply(line, expectedRequestId, actionName, out var response);
        if ((bool?)response["ok"] != true)
            throw new InvalidOperationException((string?)response["error"] ?? "控制请求被拒绝。");
        var result = (JObject)response["result"]!;
        if (allowCareer && actionName != "export_stop_diagnostics")
        {
            var actual = (string?)result["mode"] ?? (string?)result["snapshot"]?["mode"];
            DefaultMode = actual is "cut" or "career" ? actual : requestMode ?? DefaultMode;
        }
        if (!detachedForExit && actionName != "export_stop_diagnostics") SnapshotReceived?.Invoke(result);
        return result;
    }

    public async Task<JObject> Request(JObject request)
    {
        await gate.WaitAsync();
        try
        {
            EnsureProcess();
            request = (JObject)request.DeepClone();
            request["mode"] = allowCareer ? request["mode"] ?? DefaultMode : "cut";
            if ((string?)request["mode"] is not ("cut" or "career")) throw new InvalidOperationException("未知育成模式，未发送请求。");
            request["request_id"] ??= Guid.NewGuid().ToString("N");
            var actionName = (string?)request["action"];
            var requestMode = (string?)request["mode"];
            Task<string?>? inFlight;
            string? inFlightId;
            lock (protocolSync)
            {
                if (broken) throw new InvalidOperationException("原后台正在停止，未发送新的控制请求。");
                inFlight = staleSnapshotRead;
                inFlightId = staleSnapshotRequestId;
            }
            if (inFlight != null)
            {
                if (actionName == "snapshot")
                {
                    string? staleLine;
                    try { staleLine = await inFlight.WaitAsync(SnapshotTimeout); }
                    catch (TimeoutException)
                    {
                        int count;
                        lock (protocolSync) count = ++consecutiveSnapshotTimeouts;
                        if (count >= MaxConsecutiveSnapshotTimeouts)
                        {
                            ResetStalledHost(inFlight);
                            throw new InvalidOperationException("后台持续无响应，已自动重启控制通道；请稍候刷新状态。");
                        }
                        try { LoggerHelper.Warning($"[PacerBridge] 只读 snapshot 仍在等待原回执返回（pid={process?.Id}，连续超时={count}/{MaxConsecutiveSnapshotTimeouts}），继续保活不中断后台。"); } catch { }
                        throw new InvalidOperationException("后台高负载响应较慢，等待当前快照返回（不会中断运行中的育成）。");
                    }
                    lock (protocolSync)
                    {
                        staleSnapshotRead = null;
                        staleSnapshotRequestId = null;
                    }
                    return ParseAndValidateReply(staleLine, inFlightId!, actionName, requestMode);
                }
                string? drainedLine;
                try { drainedLine = await inFlight.WaitAsync(TimeSpan.FromSeconds(30)); }
                catch (TimeoutException)
                {
                    if (actionName != "export_stop_diagnostics") BreakProtocol(inFlight, "timeout");
                    throw new InvalidOperationException("后台响应超时，已请求安全停止；不会重复开始或购买。 " + protocolFailure);
                }
                lock (protocolSync)
                {
                    staleSnapshotRead = null;
                    staleSnapshotRequestId = null;
                }
                ValidateSynchronizedReply(drainedLine, inFlightId!, actionName, out _);
            }
            await process!.StandardInput.WriteLineAsync(request.ToString(Formatting.None));
            await process.StandardInput.FlushAsync();
            string? line;
            Task<string?> pendingRead;
            lock (protocolSync)
            {
                if (broken) throw new InvalidOperationException("原后台正在停止，未发送新的控制请求。");
                pendingRead = pendingReply = process.StandardOutput.ReadLineAsync();
            }
            // A read-only ZIP export must not trigger a control timeout stop; read-only snapshot polls wait without breaking the backend.
            try
            {
                line = actionName == "export_stop_diagnostics"
                    ? await pendingRead
                    : actionName == "snapshot"
                        ? await pendingRead.WaitAsync(SnapshotTimeout)
                        : await pendingRead.WaitAsync(TimeSpan.FromSeconds(30));
            }
            catch (TimeoutException)
            {
                if (actionName == "snapshot")
                {
                    int count;
                    lock (protocolSync)
                    {
                        staleSnapshotRead = pendingRead;
                        staleSnapshotRequestId = (string)request["request_id"]!;
                        count = ++consecutiveSnapshotTimeouts;
                    }
                    if (count >= MaxConsecutiveSnapshotTimeouts)
                    {
                        ResetStalledHost(pendingRead);
                        throw new InvalidOperationException("后台持续无响应，已自动重启控制通道；请稍候刷新状态。");
                    }
                    try { LoggerHelper.Warning($"[PacerBridge] 只读 snapshot 超过 {SnapshotTimeout.TotalSeconds:0.#}s 未返回（pid={process?.Id}，连续超时={count}/{MaxConsecutiveSnapshotTimeouts}），保留后台连接等待回执，不中断任务。"); } catch { }
                    throw new InvalidOperationException("后台高负载响应较慢，等待当前快照返回（不会中断运行中的育成）。");
                }
                if (actionName != "export_stop_diagnostics") BreakProtocol(pendingRead, "timeout");
                throw new InvalidOperationException("后台响应超时，已请求安全停止；不会重复开始或购买。 " + protocolFailure);
            }
            return ParseAndValidateReply(line, (string)request["request_id"]!, actionName, requestMode);
        }
        finally { lock (protocolSync) pendingReply = null; gate.Release(); }
    }

    public static bool DelayClose(Action retryClose, Func<string,Task<bool>>? confirmFrontendExit=null)
    {
        UmaPacerBridge[] active;
        lock (Bridges) active = Bridges.Where(bridge => !bridge.closed && !bridge.detachedForExit && bridge.process is { HasExited: false }).ToArray();
        if (active.Length == 0) return false;
        if (!closingAll) _ = CloseAllThenRetry(active, retryClose,confirmFrontendExit);
        return true;
    }

    public static async Task<bool> AreAllActiveIdleAsync()
    {
        UmaPacerBridge[] active;
        lock (Bridges) active = Bridges.Where(bridge => !bridge.closed && !bridge.detachedForExit && bridge.process is { HasExited: false }).ToArray();
        if (active.Length == 0) return true;
        foreach (var bridge in active)
        {
            bool isBroken;
            lock (bridge.protocolSync) isBroken = bridge.broken;
            if (isBroken) return false;
            try
            {
                var snap = await bridge.Request(new JObject { ["action"] = "snapshot" }).WaitAsync(TimeSpan.FromSeconds(3));
                if ((string?)snap["control"]?["state"] is not ("idle" or "stopped"))
                    return false;
            }
            catch
            {
                return false;
            }
        }
        return true;
    }

    public static async Task<bool> StopAllForUpdateAsync(TimeSpan timeout)
    {
        UmaPacerBridge[] active;
        lock (Bridges) active = Bridges.Where(bridge => !bridge.closed && !bridge.detachedForExit && bridge.process is { HasExited: false }).ToArray();
        if (active.Length == 0) return true;
        var closeAllTask = Task.WhenAll(active.Select(async bridge =>
        {
            try { await bridge.CloseAsync(); } catch { }
        }));
        try { await closeAllTask.WaitAsync(timeout); } catch { }
        var allStopped = true;
        foreach (var bridge in active)
        {
            var proc = bridge.process;
            if (proc is { HasExited: false })
            {
                try { proc.StandardInput.BaseStream.Close(); } catch { }
                KillProcessTreeSafe(proc);
            }
            if (proc is { HasExited: false })
                allStopped = false;
            else
            {
                bridge.process = null;
                bridge.closed = true;
            }
        }
        return allStopped;
    }

    private static async Task CloseAllThenRetry(UmaPacerBridge[] active, Action retryClose,Func<string,Task<bool>>? confirmFrontendExit)
    {
        closingAll = true;
        try
        {
            await Task.WhenAll(active.Select(bridge => bridge.CloseAsync()));
            Dispatcher.UIThread.Post(retryClose);
        }
        catch(Exception error)
        {
            if(confirmFrontendExit!=null && await confirmFrontendExit(error.Message))
            {
                foreach(var bridge in active)
                {
                    bridge.detachedForExit=true;
                    bridge.closing=true;
                    bridge.BreakProtocol(kind:"frontend_exit_pending_stop");
                }
                Dispatcher.UIThread.Post(retryClose);
            }
        }
        finally { closingAll = false; }
    }

    public async Task CloseAsync()
    {
        if (closed || process == null || process.HasExited) return;
        closing = true;
        CloseError = null;
        try
        {
            closeTask ??= CloseCoreAsync();
            // Includes waiting for the request gate, the close RPC and every
            // snapshot. A blocked backend cannot hold the window indefinitely.
            await closeTask.WaitAsync(TimeSpan.FromSeconds(8));
        }
        catch(Exception error)
        {
            CloseError="后台尚未确认停止，当前局仍保留。";
            BreakProtocol(kind:error is TimeoutException ? "close_timeout" : "close_failed");
            if(closeTask!=null)_ = ObserveCloseCompletion(closeTask);
            throw new InvalidOperationException(CloseError,error);
        }
    }

    private static async Task ObserveCloseCompletion(Task task)
    {
        try { await task.ConfigureAwait(false); } catch { /* Reported by CloseError. */ }
    }

    private async Task CloseCoreAsync()
    {
            // EnsureProcess permits requests to the existing process during close.
            await Request(new JObject { ["action"] = "close" });
            var deadline = DateTime.UtcNow.AddSeconds(30);
            while (true)
            {
                var result = await Request(new JObject { ["action"] = "snapshot" });
                if ((string?)result["control"]?["state"] is "idle" or "stopped") break;
                if (DateTime.UtcNow >= deadline)
                    throw new InvalidOperationException("正在等待育成后台收尾，未强制退出；稍后可再次关闭。");
                await Task.Delay(200);
            }
            process!.StandardInput.Close();
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
            closed = true;
    }
}
