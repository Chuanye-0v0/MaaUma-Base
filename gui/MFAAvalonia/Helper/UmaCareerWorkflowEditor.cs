using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Batch flow and strategy drill-down share a single, lossless document.</summary>
public sealed partial class UmaCareerWorkflowEditor : StackPanel
{
    private readonly Func<JObject, Task<JObject>> request;
    private readonly Func<Task<JObject>> readPolicies;
    private readonly Action<JObject> started;
    private readonly Control policyEditor;
    private readonly Func<JObject> readPreferences;
    private readonly Action<JObject> loadPreferences;
    private readonly ContentControl content = new() { HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private readonly ComboBox templates = new() { Name = "CareerWorkflowTemplate", MinWidth = 190 };
    private readonly ComboBox view = new() { Name = "CareerWorkflowView", ItemsSource = new[] { "普通 · 分支泳道", "高级 · 自由连线" }, SelectedIndex = 0 };
    private readonly TextBox title = new() { Name = "CareerWorkflowTitle", Watermark = "工作流名称", MinWidth = 220 };
    private readonly StackPanel inspector = new() { Name = "CareerWorkflowInspector", Spacing = 9, Margin = new Thickness(12) };
    private readonly TextBlock status = Note("正在读取工作流能力；配置保存在本机，下次开始生效。");
    private readonly Button back;
    private readonly FlowSurface surface;
    private readonly Grid flowPage;
    private readonly ContentControl diagramView = new() { Name = "CareerWorkflowDiagram", HorizontalContentAlignment = HorizontalAlignment.Stretch };
    private readonly Expander nodeDetails;
    private readonly Control advancedHost;
    private readonly Control flowTools, flowActions;
    private readonly List<Control> libraryControls = new();
    private readonly List<Control> mutating = new();
    private readonly Button resumeWorkflow;
    private string? resumeRoot;
    private bool resumeReady;
    private JObject? schema;
    private JObject document = new();
    private string? selected, editingPolicy, editingPolicyMode;
    private JObject? editingPreferencesBaseline;
    private int selectedClause;
    private bool busy, loading, loaded;
    private bool embeddedLibraryMode, savingPolicy, loadingPolicy;
    public event Action? Changed;
    public JObject ReadDocument() { SavePolicy(); document["label"] = title.Text ?? "育成工作流"; return (JObject)document.DeepClone(); }
    public UmaCareerWorkflowEditor(Func<JObject, Task<JObject>> bridge, Func<Task<JObject>> policies,
        Action<JObject> onStarted, Control strategyEditor, Func<JObject> preferences, Action<JObject> load)
    {
        Name = "UmaCareerWorkflowEditor"; Spacing = 12; request = bridge; readPolicies = policies; started = onStarted;
        policyEditor = strategyEditor; readPreferences = preferences; loadPreferences = load;
        var heading = new TextBlock { Text = "育成工作流", FontSize = 22, FontWeight = FontWeight.SemiBold };
        Children.Add(heading); libraryControls.Add(heading);
        var introduction = Note("外层安排执行顺序与判定；育成策略附在执行块下，打开后编辑三年时间轴和权重。");
        Children.Add(introduction); libraryControls.Add(introduction);
        back = Button("CareerWorkflowBack", "总流程  ›  返回流程规划", ClosePolicy); back.IsVisible = false; Children.Add(back);
        var tools = new WrapPanel { Orientation = Orientation.Horizontal };
        flowTools = tools;
        tools.Children.Add(templates); var useTemplate = Button("CareerWorkflowUseTemplate", "使用模板", async () => await UseTemplate()); tools.Children.Add(useTemplate);
        libraryControls.Add(templates); libraryControls.Add(useTemplate); libraryControls.Add(title);
        tools.Children.Add(title); Children.Add(tools);
        var actions = new StackPanel { Spacing = 6 };
        flowActions = actions;
        var structureButtons = new WrapPanel();
        structureButtons.Children.Add(view);
        structureButtons.Children.Add(Button("CareerWorkflowAddCondition", "＋ 通用判定", AddCondition));
        var nodeKinds = new[] { "career", "cut", "continue_current", "restart", "hold", "finish" };
        var nodePicker = new ComboBox { Name = "CareerWorkflowNewNodeKind", ItemsSource = nodeKinds.Select(KindLabel), SelectedIndex = 0 };
        structureButtons.Children.Add(nodePicker); structureButtons.Children.Add(Button("CareerWorkflowAddExecution", "＋ 执行块", () => AddExecution(nodeKinds[Math.Clamp(nodePicker.SelectedIndex, 0, nodeKinds.Length - 1)])));
        actions.Children.Add(new Expander { Name = "CareerWorkflowStructureTools", Header = "编辑流程结构", Content = structureButtons, IsExpanded = false,
            HorizontalContentAlignment = HorizontalAlignment.Stretch });
        var libraryActions = new WrapPanel();
        var updatePolicies = Button("CareerWorkflowUpdatePolicies", "使用当前运行配置", async () => { document["policies"] = await readPolicies(); Notify("已将当前运行配置填入工作流。保存后用于下次开始。"); });
        libraryActions.Children.Add(updatePolicies); libraryControls.Add(updatePolicies);
        foreach (var item in new[] { ("Validate", "检查工作流", "validate_workflow"), ("Save", "保存工作流", "save_workflow"), ("Load", "读取已保存工作流", "load_workflow"), ("Start", "开始此工作流", "start_workflow") })
        { var action = Button("CareerWorkflow" + item.Item1, item.Item2, async () => await Command(item.Item3)); libraryActions.Children.Add(action); libraryControls.Add(action); }
        resumeWorkflow = Button("CareerWorkflowResume", "继续原工作流（沿用快照）", async () => await Command("resume_workflow")); resumeWorkflow.IsVisible = false; libraryActions.Children.Add(resumeWorkflow);
        actions.Children.Add(libraryActions);
        Children.Add(actions);
        surface = new FlowSurface(() => document, SelectNode, id => OpenPolicy(id));
        surface.Changed += () => { RefreshLanes(); Changed?.Invoke(); };
        flowPage = new Grid { RowDefinitions = new("Auto,Auto"), RowSpacing = 12 };
        advancedHost = new ScrollViewer { Name = "CareerWorkflowAdvancedScroll", Content = surface, MaxHeight = 650,
            HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,
            VerticalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Auto };
        flowPage.Children.Add(diagramView);
        nodeDetails = new Expander { Name = "CareerWorkflowNodeDetails", Header = "步骤属性", Content = inspector, IsExpanded = false, IsVisible = false,
            HorizontalContentAlignment = HorizontalAlignment.Stretch };
        Grid.SetRow(nodeDetails, 1); flowPage.Children.Add(nodeDetails); content.Content = flowPage; Children.Add(content);
        diagramView.SizeChanged += (_, _) => ResizeLaneRows();
        status.Name = "CareerWorkflowStatus"; Children.Add(status);
        view.SelectionChanged += (_, _) => { if(loading)return; Editor()["view"] = view.SelectedIndex == 1 ? "advanced" : "standard"; RefreshDiagram(); Changed?.Invoke(); };
        Loaded += async (_, _) => { AttachPolicyChanged(); if(!loaded && !EmbeddedLibraryMode)await InitializeAsync(); };
        AttachPolicyChanged(); RefreshDiagram();
    }
    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = 12 };
    private Button Button(string name, string label, Action action)
    { var b = new Button { Name = name, Content = label, Margin = new Thickness(0, 0, 8, 6) }; b.Click += (_, _) => { try { if(!busy)action(); } catch(Exception e) { status.Text = e.Message; } }; mutating.Add(b); return b; }
    private Button Button(string name, string label, Func<Task> action)
    { var b = new Button { Name = name, Content = label, Margin = new Thickness(0, 0, 8, 6) }; b.Click += async (_, _) => { if(busy)return; try { await action(); } catch(Exception e) { status.Text = e.Message; } }; mutating.Add(b); return b; }
    public async Task InitializeAsync()
    {
        if(busy)return; SetBusy(true);
        try {
            schema = await request(new JObject { ["action"] = "workflow_schema" });
            var choices = (schema["templates"] as JArray ?? new()).OfType<JObject>().ToArray();
            templates.ItemsSource = choices.Select(t => (string?)t["label"] ?? (string?)t["template_id"] ?? "模板").ToArray();
            templates.SelectedIndex = choices.Length > 0 ? 0 : -1;
            if(document.Count == 0 && choices.Length > 0) { var first = (JObject)choices[0].DeepClone(); first["policies"] = await readPolicies(); LoadDocument(first); }
            loaded = true; status.Text = "选择模板后保存或开始；判定的数据不足出口会保留当前局。";
        } catch(Exception e) { status.Text = "工作流服务尚未就绪：" + e.Message; }
        finally { SetBusy(false); }
    }
    public void LoadDocument(JObject value)
    {
        SavePolicy(); editingPolicy = editingPolicyMode = null; editingPreferencesBaseline = null; document = (JObject)value.DeepClone(); selected = (string?)document["run_graph"]?["entry"];
        loading = true; title.Text = (string?)document["label"] ?? "育成工作流"; view.SelectedIndex = (string?)document["editor"]?["view"] == "advanced" ? 1 : 0; loading = false;
        surface.Advanced = view.SelectedIndex == 1; ClosePolicy(); nodeDetails.IsExpanded = false; nodeDetails.IsVisible = false; BuildInspector(); RefreshDiagram();
    }
    public void ConfigureSchema(JObject value) { schema = (JObject)value.DeepClone(); loaded = true; BuildInspector(); RefreshLanes(); }
    public void UpdateRuntime(JObject snapshot)
    {
        var workflow = snapshot["workflow"] as JObject;
        var state = workflow?["state"] as JObject;
        var control = snapshot["control"] as JObject;
        if(state == null || control == null) {
            resumeRoot = null; resumeReady = false;
            resumeWorkflow.IsVisible = resumeWorkflow.IsEnabled = false;
            surface.ActiveNode = null; surface.InvalidateVisual();
            RefreshRuntimeHighlight();
            return;
        }
        resumeRoot = (string?)workflow?["root"];
        var status = (string?)state?["status"];
        resumeReady = resumeRoot != null && status is not ("completed" or "preserved") && (string?)control["state"] is "idle" or "stopped";
        resumeWorkflow.IsVisible = !EmbeddedLibraryMode && resumeRoot != null && status is not ("completed" or "preserved"); resumeWorkflow.IsEnabled = !busy && resumeReady;
        surface.ActiveNode = (string?)state?["active_node"];
        surface.InvalidateVisual();
        RefreshRuntimeHighlight();
    }
    private JObject Editor() { if(document["editor"] is not JObject)document["editor"] = new JObject(); return (JObject)document["editor"]!; }
    private IEnumerable<JObject> Nodes => ((JArray?)document["run_graph"]?["nodes"] ?? new()).OfType<JObject>();
    private IEnumerable<JObject> Edges => ((JArray?)document["run_graph"]?["edges"] ?? new()).OfType<JObject>();
    private JObject? Node(string? id) => Nodes.FirstOrDefault(n => (string?)n["id"] == id);
    private async Task UseTemplate()
    {
        if(schema?["templates"] is not JArray choices || templates.SelectedIndex < 0)return;
        var value = (JObject)choices[templates.SelectedIndex].DeepClone(); value["policies"] = await readPolicies(); LoadDocument(value); Notify("模板已填入当前运行配置；当前局不会因载入模板而改变。");
    }
    private async Task Command(string action)
    {
        SetBusy(true);
        try {
            if(action == "resume_workflow" && !resumeReady)throw new InvalidOperationException("当前原工作流暂不能恢复。请先等待后台停止或使用运行总览的继续按钮。");
            var payload = new JObject { ["action"] = action == "resume_workflow" ? "start_workflow" : action };
            if(action == "resume_workflow")payload["workflow_root"] = resumeRoot;
            else if(action != "load_workflow")payload["document"] = ReadDocument();
            var result = await request(payload);
            var returned = result["document"] as JObject ?? (result["workflow"] as JObject)?["document"] as JObject;
            if(returned == null && action is "load_workflow" or "save_workflow" or "validate_workflow") {
                status.Text = "没有返回可用文档，保留当前编辑。";
                return;
            }
            if(returned != null)LoadDocument(returned);
            if(action is "start_workflow" or "resume_workflow") {
                started(result);
                var runtimeState = (result["workflow"] as JObject)?["state"] as JObject;
                status.Text = (string?)runtimeState?["status"] switch {
                    "running" => action == "resume_workflow" ? "原工作流运行中，沿用原文档和原局快照。" : "工作流运行中；可在运行总览查看进度。",
                    "completed" => "工作流已完成。", "preserved" => "工作流已停下并保留当前局。",
                    "stopped" => "工作流已停止，当前局保留。", "manual_career" => "原局正在自动育成。",
                    _ => "后台已响应；请在运行总览查看工作流状态。"
                };
            } else status.Text = action switch { "save_workflow" => "工作流已保存。续跑仍沿用原局快照。", "load_workflow" => "已读取保存的完整工作流。", _ => "工作流检查通过。" };
        } finally { SetBusy(false); }
    }
    private void SetBusy(bool value) { busy = value; foreach(var control in mutating)control.IsEnabled = !value; if(resumeWorkflow != null)resumeWorkflow.IsEnabled = !value && resumeReady; templates.IsEnabled = view.IsEnabled = title.IsEnabled = !value; surface.IsEnabled = inspector.IsEnabled = diagramView.IsEnabled = !value; }
    private void Notify(string message) { status.Text = message; surface.InvalidateMeasure(); surface.InvalidateVisual(); BuildInspector(); RefreshLanes(); Changed?.Invoke(); }
    private void OpenPolicy(string id)
    {
        if(Node(id) is not JObject node || (string?)node["kind"] is not ("career" or "cut"))return;
        SavePolicy(); editingFilter = null; editingPolicy = (string?)node["policy"] ?? "default"; editingPolicyMode = (string?)node["kind"] == "cut" ? "cut_options" : "career_options";
        var options = document["policies"]?[editingPolicy]?[editingPolicyMode] as JObject;
        var settings = editingPolicyMode == "cut_options" ? options?["cut"] as JObject : options;
        var strategyPolicy = (options?["larc_policy"] ?? settings?["larc_policy"]) as JObject;
        var preferences = settings?["decision_preferences"] as JObject ?? strategyPolicy?["decision_preferences"] as JObject;
        loadingPolicy = true;
        try {
            if(policyEditor is UmaDecisionPreferences controls)
                controls.LoadForStrategy(preferences,(string?)strategyPolicy?["revision"],
                    preserveMissingBaseFields:strategyPolicy == null && options?.Property("larc_policy") == null);
            else loadPreferences(preferences == null ? new JObject() : (JObject)preferences.DeepClone());
            editingPreferencesBaseline = readPreferences();
        }
        finally { loadingPolicy = false; }
        content.Content = null; content.Content = policyEditor; back.Content = "总流程  ›  " + KindLabel((string?)node["kind"]) + "  ›  育成策略（点击返回）"; back.IsVisible = true;
        RefreshChrome();
    }
    private void SavePolicy()
    {
        if(editingPolicy == null || editingPolicyMode == null || savingPolicy)return;
        var changed = false; savingPolicy = true;
        try {
            var current = readPreferences();
            if(editingPreferencesBaseline != null && JToken.DeepEquals(current, editingPreferencesBaseline))return;
            if(document["policies"] is not JObject policies || policies[editingPolicy] is not JObject policy)return;
            if(policy[editingPolicyMode] is not JObject)policy[editingPolicyMode] = new JObject();
            var target = (JObject)policy[editingPolicyMode]!;
            if(editingPolicyMode == "cut_options") { if(target["cut"] is not JObject)target["cut"] = new JObject(); target = (JObject)target["cut"]!; }
            target["decision_preferences"] = current;
            editingPreferencesBaseline = (JObject)current.DeepClone(); changed = true;
        } finally { savingPolicy = false; }
        if(changed) { RefreshLanes(); Changed?.Invoke(); }
    }
    private void ClosePolicy() { SavePolicy(); editingFilter = null; editingPolicy = editingPolicyMode = null; editingPreferencesBaseline = null; content.Content = null; content.Content = flowPage; back.IsVisible = false; RefreshChrome(); RefreshDiagram(); }
    public static string KindLabel(string? kind) => kind switch { "entry" => "开始", "career" => "自动育成", "cut" => "刷切阶段", "condition" => "通用判定", "continue_current" => "原局继续育成", "restart" => "重开下一局", "hold" => "暂停并保留", "finish" => "结束并保留", _ => "高级节点：" + kind };
    public static string OutcomeLabel(string value) => value switch { "matched" => "满足", "not_matched" => "不满足", "unknown" => "数据不足", "completed" => "完成", "next" => "下一步", _ => value };
    private static string[] Outcomes(string? kind) => kind switch { "cut" or "condition" => new[] { "matched", "not_matched", "unknown" }, "career" => new[] { "completed" }, "entry" or "continue_current" or "restart" => new[] { "next" }, _ => Array.Empty<string>() };
    private void BuildInspector()
    {
        inspector.Children.Clear(); if(Node(selected) is not JObject node) { inspector.Children.Add(Note("选择执行块或判定；双击执行块可打开策略。")); return; }
        var kind = (string?)node["kind"]; var id = (string)node["id"]!;
        nodeDetails.Header = StepLabel(node) + " · 属性";
        inspector.Children.Add(new TextBlock { Text = KindLabel(kind), FontSize = 17, FontWeight = FontWeight.SemiBold });
        var label = new TextBox { Name = "CareerWorkflowNodeLabel", Text = (string?)node["label"], Watermark = StepLabel(node) };
        inspector.Children.Add(Note("步骤名称")); inspector.Children.Add(label);
        var displayedLabel = label.Text ?? "";
        label.TextChanged += (_, _) => { var value = label.Text ?? ""; if(value == displayedLabel)return; displayedLabel = value; if(string.IsNullOrWhiteSpace(value))node.Remove("label"); else node["label"] = value.Trim(); RefreshLanes(); Changed?.Invoke(); };
        inspector.Children.Add(Note("执行流程使用实线；策略附件在子画布编辑，不加入主执行连线。"));
        if(kind is "career" or "cut") {
            var policyId = (string?)node["policy"] ?? "default";
            var references = Nodes.Count(n => ((string?)n["kind"] is "career" or "cut") && ((string?)n["policy"] ?? "default") == policyId);
            inspector.Children.Add(Note($"策略 {policyId} · 被 {references} 个执行块共用；修改会影响这些块。"));
            inspector.Children.Add(Button("CareerWorkflowOpenPolicy", "打开育成策略 ›", () => OpenPolicy(id)));
            inspector.Children.Add(Button("CareerWorkflowCopyPolicy", "复制为本块独立策略", () => { var policy = document["policies"]?[policyId] as JObject ?? throw new InvalidOperationException("策略不存在。"); var copyId = "policy_" + Guid.NewGuid().ToString("N")[..8]; document["policies"]![copyId] = policy.DeepClone(); node["policy"] = copyId; Notify("本块已引用独立策略，其余执行块继续使用原策略。"); }));
        }
        if(kind == "cut") {
            inspector.Children.Add(Note("满足＝确认有切；不满足＝到截止点确认无切；未取得有效结果走数据不足。"));
            inspector.Children.Add(Button("CareerWorkflowEditFilter", "编辑筛选条件 ›", () => SelectFilter(id)));
        }
        if(kind == "condition")BuildCondition(node);
        var targetNodes = Nodes.ToArray();
        foreach(var outcome in Outcomes(kind))
        {
            inspector.Children.Add(Note(OutcomeLabel(outcome) + " →"));
            var edge = Edges.FirstOrDefault(e => (string?)e["from"] == id && (string?)e["outcome"] == outcome);
            var options = targetNodes.Where(n => (string?)n["id"] != id && (outcome != "unknown" || (string?)n["kind"] == "hold")).ToArray();
            var list = new ComboBox { Name = "CareerWorkflowTarget_" + outcome, ItemsSource = new[] { "未连接（保存时检查）" }.Concat(options.Select(n => KindLabel((string?)n["kind"]) + " · " + n["id"])), SelectedIndex = Array.FindIndex(options, n => (string?)n["id"] == (string?)edge?["to"]) + 1 };
            list.SelectionChanged += (_, _) => { if(list.SelectedIndex <= 0) { if(edge != null)edge.Remove(); } else { if(edge == null) { edge = new JObject { ["from"] = id, ["outcome"] = outcome }; ((JArray)document["run_graph"]!["edges"]!).Add(edge); } edge["to"] = options[list.SelectedIndex - 1]["id"]!.DeepClone(); } surface.InvalidateVisual(); RefreshLanes(); Changed?.Invoke(); };
            inspector.Children.Add(list);
        }
        inspector.Children.Add(Note("数据不足不会自动重开。特殊高级图按原文档保留；执行前交给后端完整检查。"));
        if(!new[] { "entry", "career", "cut", "condition", "continue_current", "restart", "hold", "finish" }.Contains(kind))
            inspector.Children.Add(new TextBox { Name = "CareerWorkflowOriginalAdvancedNode", Text = node.ToString(), IsReadOnly = true, AcceptsReturn = true, MaxHeight = 240, TextWrapping = TextWrapping.Wrap });
        if(kind != "entry")inspector.Children.Add(Button("CareerWorkflowDeleteNode", "删除此块并断开连线", () => { foreach(var edge in Edges.Where(e => (string?)e["from"] == id || (string?)e["to"] == id).ToArray())edge.Remove(); node.Remove(); selected = (string?)document["run_graph"]?["entry"]; Notify("已删除节点及关联连线；策略文档继续保留，可重新引用。"); }));
    }
    private void AddCondition()
    {
        if(document["run_graph"]?["nodes"] is not JArray nodes) { status.Text = "请先载入模板。"; return; }
        var node = new JObject { ["id"] = "condition_" + Guid.NewGuid().ToString("N")[..8], ["kind"] = "condition", ["check_at"] = "home", ["when"] = new JObject { ["all"] = new JArray(new JObject { ["kind"] = "vitality", ["op"] = "lt", ["value"] = 40 }) } };
        nodes.Add(node); var hold = Nodes.FirstOrDefault(n => (string?)n["kind"] == "hold");
        if(hold == null) { hold = new JObject { ["id"] = "hold_" + Guid.NewGuid().ToString("N")[..8], ["kind"] = "hold" }; nodes.Add(hold); }
        ((JArray)document["run_graph"]!["edges"]!).Add(new JObject { ["from"] = node["id"]!.DeepClone(), ["outcome"] = "unknown", ["to"] = hold["id"]!.DeepClone() });
        selected = (string)node["id"]!; Notify("已添加判定；请选择条件、检查时机，并连接前后流程。未连接节点会在检查时提示。");
    }
    private void AddExecution(string kind)
    {
        if(document["run_graph"]?["nodes"] is not JArray nodes) { status.Text = "请先载入模板。"; return; }
        var node = new JObject { ["id"] = kind + "_" + Guid.NewGuid().ToString("N")[..8], ["kind"] = kind };
        if(kind is "career" or "cut")node["policy"] = "default";
        if(kind == "cut")node["check_at"] = "cut_checkpoint";
        nodes.Add(node); selected = (string)node["id"]!; Notify("已添加执行块，请连接相应出口；启动前会检查完整流程。");
    }
    private void BuildCondition(JObject node) => BuildConditionControls(node, inspector, BuildInspector);
    private void BuildConditionControls(JObject node, StackPanel host, Action refresh, bool showTiming = true, JObject[]? suppliedKinds = null)
    {
        var when = node["when"] as JObject; var clauses = when?.Properties().FirstOrDefault()?.Value as JArray;
        selectedClause = Math.Clamp(selectedClause, 0, Math.Max(0, (clauses?.Count ?? 1) - 1));
        var clause = clauses?.ElementAtOrDefault(selectedClause) as JObject;
        if(when?.Count != 1 || when.Properties().First().Name is not ("all" or "any") || clauses == null || clauses.Any(row => row is not JObject) || clause == null) { host.Children.Add(Note("此判定包含高级表达式，原样保留。")); return; }
        var group = new ComboBox { Name = "CareerWorkflowConditionCombine", ItemsSource = new[] { "全部条件满足", "任一条件满足" }, SelectedIndex = when?.Property("any") != null ? 1 : 0 };
        host.Children.Add(group);
        group.SelectionChanged += (_, _) => { node["when"] = new JObject { [group.SelectedIndex == 1 ? "any" : "all"] = clauses!.DeepClone() }; refresh(); RefreshLanes(); Changed?.Invoke(); };
        var kinds = suppliedKinds ?? (schema?["condition_kinds"] as JArray ?? new()).OfType<JObject>().ToArray();
        var clausePicker = new ComboBox { Name = "CareerWorkflowConditionClause", ItemsSource = clauses!.OfType<JObject>().Select((row, i) => "条件 " + (i + 1) + " · " + ClauseLabel(row, kinds)), SelectedIndex = selectedClause };
        host.Children.Add(clausePicker);
        clausePicker.SelectionChanged += (_, _) => { if(clausePicker.SelectedIndex >= 0) { selectedClause = clausePicker.SelectedIndex; refresh(); } };
        var addClause = Button("CareerWorkflowAddClause", "＋ 本判定的条件", () => { clauses.Add(new JObject { ["kind"] = "vitality", ["op"] = "gte", ["value"] = 40 }); selectedClause = clauses.Count - 1; refresh(); RefreshLanes(); Changed?.Invoke(); });
        addClause.IsEnabled = clauses.Count < (suppliedKinds == null ? 32 : (int?)ScreeningCapabilities?["max_conditions"] ?? 32);host.Children.Add(addClause);
        if(clauses.Count > 1)host.Children.Add(Button("CareerWorkflowRemoveClause", "删除选中条件", () => { clause.Remove(); selectedClause = 0; refresh(); RefreshLanes(); Changed?.Invoke(); }));
        var keys = kinds.Select(k => (string?)k["kind"] ?? (string?)k["key"] ?? "").ToArray();
        if(!keys.Contains((string?)clause["kind"])) { host.Children.Add(Note("此项条件不在当前时机的可用条件库中，原文完整保留。"));host.Children.Add(new TextBox { Name = "CareerWorkflowAdvancedCondition", Text = clause.ToString(), IsReadOnly = true, AcceptsReturn = true, MaxHeight = 220 });return; }
        var chooser = new ComboBox { Name = "CareerWorkflowConditionKind", ItemsSource = kinds.Select(k => ConditionLabel((string?)k["kind"] ?? (string?)k["key"] ?? "", (string?)k["label"])).ToArray(), SelectedIndex = Array.IndexOf(keys, (string?)clause["kind"]) };
        host.Children.Add(Note("判定条件")); host.Children.Add(chooser);
        chooser.SelectionChanged += (_, _) => { if(chooser.SelectedIndex < 0)return; var key = keys[chooser.SelectedIndex]; clause.RemoveAll(); clause["kind"] = key; ApplyConditionDefaults(clause); var allowed = kinds[chooser.SelectedIndex]["supported_check_at"] as JArray; if(showTiming && allowed?.Count > 0 && !allowed.Values<string>().Contains((string?)node["check_at"]))node["check_at"] = allowed[0].DeepClone(); refresh(); surface.InvalidateVisual(); RefreshLanes(); Changed?.Invoke(); };
        var selectedKind = kinds.FirstOrDefault(k => ((string?)k["kind"] ?? (string?)k["key"]) == (string?)clause["kind"]);
        if(showTiming) {
        var times = (selectedKind?["supported_check_at"] as JArray ?? schema?["supported_check_at"] as JArray ?? new JArray("home", "junior", "inheritance", "cut_checkpoint")).Values<string>().Where(x => x != null).ToArray();
        var timing = new ComboBox { Name = "CareerWorkflowCheckAt", ItemsSource = times.Select(TimeLabel), SelectedIndex = Array.IndexOf(times, (string?)node["check_at"]) };
        host.Children.Add(Note("检查时机")); host.Children.Add(timing);
        timing.SelectionChanged += (_, _) => { if(timing.SelectedIndex >= 0) { node["check_at"] = times[timing.SelectedIndex]; RefreshLanes(); Changed?.Invoke(); } };
        }
        host.Children.Add(Note((string?)selectedKind?["source"] ?? "仅使用本局已确认数据；需要页面采集的条件不会每回合自动开页。"));
        if(clauses!.Count > 1)host.Children.Add(Note($"本判定共 {clauses.Count} 项，使用上方选择器逐项编辑。数据不足保持独立结果。"));
        foreach(var property in clause.Properties().Where(p => p.Name != "kind").ToArray())
        {
            var key = property.Name;
            var optionKeys = key switch { "op" => new[] { "lt", "lte", "eq", "gte", "gt" }, "stat" => new[] { "speed", "stamina", "power", "guts", "wisdom" }, "distance" => new[] { "short", "mile", "medium", "long" }, "rank" => new[] { "G", "F", "E", "D", "C", "B", "A", "S" }, "event_kind" => new[] { "small", "big" }, _ => Array.Empty<string>() };
            var optionLabels = key switch { "op" => new[] { "小于", "不高于", "等于", "不低于", "大于" }, "stat" => new[] { "速度", "耐力", "力量", "毅力", "智力" }, "distance" => new[] { "短距离", "英里", "中距离", "长距离" }, "event_kind" => new[] { "小切", "大切" }, _ => optionKeys };
            if(optionKeys.Length > 0) {
                var option = new ComboBox { Name = "CareerWorkflowCondition_" + key, ItemsSource = optionLabels, SelectedIndex = Array.IndexOf(optionKeys, (string?)property.Value) };
                host.Children.Add(Note(ParameterLabel(key))); host.Children.Add(option);
                option.SelectionChanged += (_, _) => { if(option.SelectedIndex >= 0) { clause[key] = optionKeys[option.SelectedIndex]; surface.InvalidateVisual(); RefreshLanes(); Changed?.Invoke(); } }; continue;
            }
            var field = new TextBox { Name = "CareerWorkflowCondition_" + key, Text = key == "skills" && property.Value is JArray skills ? string.Join("、", skills.Values<string>()) : property.Value.Type == JTokenType.String ? (string?)property.Value : property.Value.ToString(Newtonsoft.Json.Formatting.None), Watermark = key == "skills" ? "技能名称或编号，用顿号或逗号分隔" : null };
            host.Children.Add(Note(ParameterLabel(key))); host.Children.Add(field);
            host.Children.Add(Button("CareerWorkflowApply_" + key, "应用", () => { if(key == "skills")clause[key] = new JArray((field.Text ?? "").Split(new[] { ',', '，', '、', '\n' }, StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries)); else if(property.Value.Type == JTokenType.Integer) { if(!long.TryParse(field.Text, out var n))throw new ArgumentException("需要整数。"); clause[key] = n; } else if(property.Value.Type == JTokenType.Float) { if(!double.TryParse(field.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out var n) || !double.IsFinite(n))throw new ArgumentException("需要有效数字。"); clause[key] = n; } else if(property.Value.Type is JTokenType.Object or JTokenType.Array)clause[key] = JToken.Parse(field.Text ?? "null"); else clause[key] = field.Text; surface.InvalidateVisual(); RefreshLanes(); Changed?.Invoke(); refresh(); }));
        }
    }
    private static string TimeLabel(string? time) => time switch { "home" => "每回合正常主页 · 已有状态", "junior" => "初级年末检查点", "inheritance" => "继承后检查点", "cut_checkpoint" => "刷切有效检查点", _ => time ?? "未知时机" };
    private static string ConditionLabel(string kind, string? fallback) => kind switch { "stat_min" => "单项属性达到", "stat_sum_min" => "五维总和达到", "adaptation_points_min" => "适应性 Pt 达到", "supporter_points_min" => "协助者积分达到", "bond_ready_min" => "羁绊达线支援张数", "bond_card_min" => "指定卡羁绊", "friend_outing_unlocked" => "友人外出已解锁", "chain_stage_min" => "指定卡事件链段数", "chain_complete" => "指定卡事件链完走", "cut_event_completed" => "切事件已完走", "cut_present" => "才能出众状态", _ => fallback ?? kind };
    private static string ParameterLabel(string key) => key switch { "minimum" => "最低值", "value" => "比较值", "op" => "比较方式（lt/lte/eq/gte/gt）", "stat" => "属性", "card_id" => "指定卡编号", "bond_threshold" => "羁绊线", "event_kind" => "事件类型（big/small）", "chain_id" => "事件链编号", "skills" => "目标技能名单", "distances" => "目标距离适性", _ => key };
    private static void ApplyConditionDefaults(JObject clause)
    {
        var kind = (string?)clause["kind"];
        if(kind is "cut_present" or "friend_outing_unlocked")return;
        if(kind == "chain_complete") { clause["card_id"] = ""; return; }
        if(kind == "skill_count_min") { clause["skills"] = new JArray(); clause["minimum"] = 1; return; }
        if(kind == "distance_min") { clause["distance"] = "mile"; clause["rank"] = "A"; return; }
        if(kind == "cut_event_completed") { clause["event_kind"] = "small"; return; }
        if(kind is "stat_min" or "stat_sum_min" or "adaptation_points_min" or "supporter_points_min" or "bond_ready_min" or "bond_card_min" or "chain_stage_min") {
            clause["minimum"] = 1; if(kind == "stat_min")clause["stat"] = "speed"; if(kind is "bond_card_min" or "chain_stage_min")clause["card_id"] = ""; if(kind == "bond_ready_min")clause["bond_threshold"] = 80; return;
        }
        clause["op"] = "gte"; clause["value"] = 1;
    }

    private sealed class FlowSurface : Control
    {
        private readonly Func<JObject> read; private readonly Action<string> select, open;
        private string? picked; private Point dragStart; private Point oldPosition; private bool dragging;
        private readonly Dictionary<string, Point> positions = new();
        public bool Advanced { get; set; }
        public string? ActiveNode { get; set; }
        public event Action? Changed;
        public FlowSurface(Func<JObject> doc, Action<string> selected, Action<string> policy)
        {
            Name = "CareerWorkflowSurface"; MinHeight = 550; ClipToBounds = true; read = doc; select = selected; open = policy;
            ActualThemeVariantChanged += (_, _) => InvalidateVisual();
            PointerPressed += Press; PointerMoved += Move; PointerReleased += (_, e) => { dragging = false; e.Pointer.Capture(null); };
        }
        private IEnumerable<JObject> Nodes => ((JArray?)read()["run_graph"]?["nodes"] ?? new()).OfType<JObject>();
        private Point Position(JObject node, int index)
        {
            var id = (string)node["id"]!; if(Advanced && read()["editor"]?["positions"]?[id] is JObject p)return new Point((double?)p["x"] ?? 30, (double?)p["y"] ?? 30);
            var columns = Math.Max(1, (int)Math.Max(300, Bounds.Width) / 300); return new Point(25 + index % columns * 300, 40 + index / columns * 210);
        }
        private Rect BoundsOf(string id) => positions.TryGetValue(id, out var p) ? new Rect(p, new Size(195, 100)) : default;
        private bool IsDecision(JObject node) => (string?)node["kind"] is "condition" or "cut";
        protected override Size MeasureOverride(Size availableSize)
        {
            var width = double.IsFinite(availableSize.Width) ? Math.Max(300, availableSize.Width) : 900;
            var columns = Math.Max(1, (int)width / 300);
            var height = Math.Max(550, (Math.Ceiling((double)Nodes.Count() / columns) + 1) * 210 + 40);
            if(Advanced && read()["editor"]?["positions"] is JObject layout) {
                height = Math.Max(height, layout.Properties().Select(p => (double?)p.Value["y"] ?? 0).DefaultIfEmpty(0).Max() + 165);
                width = Math.Max(width, layout.Properties().Select(p => (double?)p.Value["x"] ?? 0).DefaultIfEmpty(0).Max() + 230);
            }
            return new Size(width, height);
        }
        public override void Render(DrawingContext context)
        {
            base.Render(context); var doc = read(); var nodes = Nodes.ToArray(); positions.Clear();
            var ordered = new List<JObject>(); var visited = new HashSet<string>(); var cursor = (string?)doc["run_graph"]?["entry"];
            while(cursor != null && visited.Add(cursor)) {
                var current = nodes.FirstOrDefault(n => (string?)n["id"] == cursor); if(current == null)break; ordered.Add(current);
                var outlet = (string?)current["kind"] switch { "cut" or "condition" => "matched", "career" => "completed", _ => "next" };
                cursor = (string?)(doc["run_graph"]?["edges"] as JArray)?.OfType<JObject>().FirstOrDefault(e => (string?)e["from"] == cursor && (string?)e["outcome"] == outlet)?["to"];
            }
            var columns = Math.Max(1, (int)Math.Max(300, Bounds.Width) / 300); var branchStart = ((ordered.Count + columns - 1) / columns) * columns;
            for(var i = 0; i < ordered.Count; i++)positions[(string)ordered[i]["id"]!] = Position(ordered[i], i);
            var branchIndex = 0; foreach(var node in nodes.Where(n => !visited.Contains((string)n["id"]!)))positions[(string)node["id"]!] = Position(node, branchStart + branchIndex++);
            var ink = UmaWeightTheme.Color(this, "text"); var line = new Pen(new SolidColorBrush(UmaWeightTheme.Color(this, "muted")), 1.6);
            foreach(var edge in (doc["run_graph"]?["edges"] as JArray ?? new()).OfType<JObject>()) {
                var from = BoundsOf((string?)edge["from"] ?? ""); var to = BoundsOf((string?)edge["to"] ?? ""); if(from.Width == 0 || to.Width == 0)continue;
                var outcome = (string?)edge["outcome"] ?? ""; var offset = outcome == "matched" ? -24 : outcome == "unknown" ? 24 : 0;
                var a = new Point(from.Right - Math.Abs(offset) * 1.9, from.Center.Y + offset); var b = new Point(to.Left, to.Center.Y); var bend = Math.Max(a.X + 18, (a.X + b.X) / 2);
                var geometry = new StreamGeometry(); using(var g = geometry.Open()) { g.BeginFigure(a, false); if(b.X <= a.X) { var lane = Math.Max(from.Bottom,to.Bottom) + 48 + (outcome == "unknown" ? 20 : 0); g.LineTo(new Point(from.Right + 16,a.Y)); g.LineTo(new Point(from.Right + 16,lane)); g.LineTo(new Point(to.Left - 14,lane)); g.LineTo(new Point(to.Left - 14,b.Y)); } else { g.LineTo(new Point(bend,a.Y)); g.LineTo(new Point(bend,b.Y)); } g.LineTo(b); }
                context.DrawGeometry(null, line, geometry); context.DrawLine(line, b, b + new Vector(-7, -4)); context.DrawLine(line, b, b + new Vector(-7, 4));
                Text(context, OutcomeLabel(outcome), new Point(from.Right + 4, a.Y - 16), ink, 10);
            }
            foreach(var node in nodes) {
                var id = (string)node["id"]!; var rect = BoundsOf(id); var decision = IsDecision(node); var color = new SolidColorBrush(UmaWeightTheme.Color(this, id == ActiveNode ? "reference" : "card")); var border = new Pen(new SolidColorBrush(UmaWeightTheme.Color(this, id == picked || id == ActiveNode ? "selected" : "border")), id == picked || id == ActiveNode ? 2.4 : 1.4);
                if(decision) { var shape = new StreamGeometry(); using(var g = shape.Open()) { g.BeginFigure(new Point(rect.Center.X, rect.Top), true); g.LineTo(new Point(rect.Right, rect.Center.Y)); g.LineTo(new Point(rect.Center.X, rect.Bottom)); g.LineTo(new Point(rect.Left, rect.Center.Y)); g.EndFigure(true); } context.DrawGeometry(color, border, shape); }
                else context.DrawRectangle(color, border, rect, 7, 7);
                Text(context, KindLabel((string?)node["kind"]), new Point(rect.Left + (decision ? 40 : 14), rect.Top + 28), ink, 14);
                Text(context, id.Length > 23 ? id[..23] : id, new Point(rect.Left + 22, rect.Top + 56), UmaWeightTheme.Color(this, "muted"), 10);
                if((string?)node["kind"] is "career" or "cut") {
                    var attachment = new Rect(rect.Left + 10, rect.Bottom + 5, rect.Width - 20, 25); context.DrawRectangle(new SolidColorBrush(UmaWeightTheme.Color(this, "reference")), null, attachment, 5, 5);
                    Text(context, "育成策略附件  ›", attachment.TopLeft + new Vector(9, 4), ink, 11);
                }
            }
            if(nodes.Length == 0)Text(context, "请先载入工作流模板", new Point(35, 50), ink, 18);
        }
        private static void Text(DrawingContext context, string value, Point point, Color color, int size)
        { context.DrawText(new FormattedText(value, CultureInfo.CurrentCulture, FlowDirection.LeftToRight, new Typeface("Microsoft YaHei UI"), size, new SolidColorBrush(color)), point); }
        private void Press(object? sender, PointerPressedEventArgs e)
        {
            var point = e.GetPosition(this); var node = Nodes.Reverse().FirstOrDefault(n => { var r = BoundsOf((string)n["id"]!); return new Rect(r.Position, new Size(r.Width, r.Height + 31)).Contains(point); });
            if(node == null)return; picked = (string)node["id"]!; select(picked); var rect = BoundsOf(picked);
            if(point.Y > rect.Bottom || e.ClickCount == 2) { open(picked); return; }
            if(Advanced) { dragging = true; dragStart = point; oldPosition = rect.Position; e.Pointer.Capture(this); } InvalidateVisual();
        }
        private void Move(object? sender, PointerEventArgs e)
        {
            if(!dragging || picked == null)return; var doc = read(); if(doc["editor"] is not JObject)doc["editor"] = new JObject(); var editor = (JObject)doc["editor"]!;
            if(editor["positions"] is not JObject)editor["positions"] = new JObject(); var point = oldPosition + (e.GetPosition(this) - dragStart);
            editor["positions"]![picked] = new JObject { ["x"] = Math.Max(5, point.X), ["y"] = Math.Max(5, point.Y) }; InvalidateMeasure(); InvalidateVisual(); Changed?.Invoke();
        }
    }
}
