using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Threading;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using UmaCutPanel;

namespace MFAAvalonia.Helper;

/// <summary>The existing portable center, hosted inside the multi-account MFA shell.</summary>
public sealed class MaaUmaPacerPage : UserControl
{
    public const string UpdateNotice = MaaUmaActivationView.UpdateNotice;
    public string PacerRoot { get; }
    public string CatalogRoot { get; }
    public UmaPacerView RuntimeView { get; }
    private readonly MaaUmaSharedConnectionBridge bridge;
    private readonly TextBlock status = new() { Text = "连接检查不会登录或启动育成。", TextWrapping = Avalonia.Media.TextWrapping.Wrap };

    public MaaUmaPacerPage() : this(Path.Combine(AppContext.BaseDirectory, "pacer")) { }

    // Optional delegates are for the offline page check; production uses the package's portable host.
    public MaaUmaPacerPage(string pacerRoot, IUmaPacerBridge? bridge = null,
        Func<JObject, Task<JObject>>? serviceRequest = null, Func<bool>? dailyRunning = null,
        Func<MaaUmaSharedConnection>? readConnection = null)
    {
        PacerRoot = Path.GetFullPath(pacerRoot);
        // The embedded component shares the ordinary shell's interface/portraits/catalogs.
        CatalogRoot = Path.GetDirectoryName(PacerRoot) ?? throw new InvalidOperationException("基础面板目录未找到。");
        var profile = CutPackageProfile.Read(PacerRoot);
        this.bridge = new MaaUmaSharedConnectionBridge(PacerRoot,
            bridge ?? new UmaPacerBridge(PacerRoot, allowCareer: true), readConnection);
        var connection = new TextBlock { Name = "MaaUmaPacerSharedConnection", TextWrapping = Avalonia.Media.TextWrapping.Wrap };
        var adb = new TextBlock { Name = "MaaUmaPacerSharedAdbPath", TextWrapping = Avalonia.Media.TextWrapping.Wrap };
        var probe = new Button { Name = "MaaUmaPacerConnectionSettings", Content = "前往系统设置 · 模拟器连接" };
        var fields = new StackPanel { Spacing = 6, Children = {
            new TextBlock { Text = "任务共用系统设置中的当前实例连接。", TextWrapping = Avalonia.Media.TextWrapping.Wrap },
            connection, adb, probe } };
        UmaPacerContext Context()
        {
            var current = this.bridge.ReadContext(endpoint => dailyRunning?.Invoke() ?? DailyRunningOn(endpoint));
            var selected = this.bridge.SelectedConnection;
            connection.Text = selected == null ? "主面板当前连接：尚未选择可用模拟器" : "主面板当前连接：" + selected.Endpoint;
            adb.Text = selected == null ? "" : "ADB：" + selected.AdbPath;
            if (!this.bridge.IsIdle && (this.bridge.BoundConnection is not { } bound || !bound.SameTarget(selected)))
                connection.Text += "；当前任务仍使用：" + (string.IsNullOrEmpty(current.Endpoint) ? "原后台连接" : current.Endpoint);
            return current;
        }
        var catalogBackend = Path.Combine(CatalogRoot, "backend");
        var trainees = UmaTraineePicker.Load(CatalogRoot, catalogBackend);
        RuntimeView = new UmaPacerView(this.bridge, Context, trainees,
            pageTitle: "运行总览", supportChoices: UmaSupportPicker.Load(CatalogRoot, catalogBackend),
            endpointAllowed: PortableConnection.AllowedEndpoint, standaloneCutProfiles: true, pacerCenter: true);
        probe.Click += (_, _) => MaaUmaSettingsNavigation.Open(this);
        var preferences = new EventPreferenceEditor(PacerRoot, this.bridge, value => status.Text = value, collapseAfterSave: false);
        ICutServiceClient service = serviceRequest is null ? new CutServiceClient(PacerRoot) : new LocalService(serviceRequest);
        var updates = new CutUpdateView(service, _ => Task.FromException(new InvalidOperationException(UpdateNotice)), profile.Channel);
        this.bridge.SnapshotReceived += snapshot => Dispatcher.UIThread.Post(() => {
            var state = (string?)snapshot["control"]?["state"] ?? "unknown";
            updates.SetRuntimeState(state);
        });
        var reviews = new ReviewExportView(this.bridge, trainees, service, PacerRoot, profile.ContinuationAssessment,
            profile.DecisionPreferences ? () => RuntimeView.CurrentDecisionPreferences : null);
        var exchange = new ScoringExchangeView(service, () => RuntimeView.CurrentScoringProfile,
            (value, label) => TopLevel.GetTopLevel(this) is Window owner
                ? RuntimeView.PreviewSharedWeights(owner, value, label) : Task.FromResult(false),
            () => RuntimeView.CanApplySharedWeights, () => RuntimeView.CurrentPresetLabel, this.bridge);
        var messages = new StackPanel { Spacing = 6, Children = {
            status, new TextBlock { Name = "MaaUmaPacerUpdateNotice", Text = UpdateNotice, TextWrapping = Avalonia.Media.TextWrapping.Wrap } } };
        // The shell's existing fix4 library remains the one library navigation entry.
        MaaUmaActivationView.AdditionalIdleCheck = () => this.bridge.CheckRuntimeIdle();
        Context();
        Content = new PacerCenterLayout(fields, messages, RuntimeView, preferences, reviews, updates, exchange, parentLibrary: null, showConnectionBar: false);
    }

    private static bool DailyRunningOn(string endpoint)
    {
        foreach (var tab in Instances.InstanceTabBarViewModel.Tabs)
        {
            var queue = tab.TaskQueueViewModel;
            if (queue == null || queue.CurrentController != MaaControllerTypes.Adb
                || (!queue.IsRunning && !queue.Processor.IsTaskRunActive)) continue;
            try {
                if (UmaPublicRuntime.NormalizeAddress(queue.Processor.Config.AdbDevice.AdbSerial) == endpoint) return true;
            } catch (InvalidOperationException) { }
        }
        return false;
    }

    public static void UpdateDistributionStatus(TextBlock display, JObject info)
    {
        var trace = (string?)info["trace_id"];
        var source = (string?)info["distribution_source"];
        display.Text = trace is not null && System.Text.RegularExpressions.Regex.IsMatch(trace, @"\AMU-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}\z")
            && source is "activation" or "package" ? (source == "activation" ? "已保存授权编号：" : "分发编号：") + trace : "来源编号：未记录";
    }

    private sealed class LocalService(Func<JObject, Task<JObject>> request) : ICutServiceClient
    {
        public Task<JObject> Request(JObject value) => request(value);
        public Task<JObject> Update(string action, Action<JObject>? progress = null, CancellationToken cancellationToken = default)
            => Task.FromException<JObject>(new InvalidOperationException(UpdateNotice));
    }
}
