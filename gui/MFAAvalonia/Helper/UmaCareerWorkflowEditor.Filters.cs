using System;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaCareerWorkflowEditor
{
    private string? editingFilter;
    private int selectedCheckpoint;
    private static bool CustomScreening(JObject node) => (node["screening"] as JObject)?["enabled"]?.Type == JTokenType.Boolean
        && (bool)node["screening"]!["enabled"]!;

    /// <summary>Edit configuration values without changing the mode's execution graph.</summary>
    public bool SelectFilter(string nodeId)
    {
        if(busy || Node(nodeId) is not JObject node || (string?)node["kind"] is not ("cut" or "condition"))return false;
        SavePolicy(); editingPolicy = editingPolicyMode = null; editingPreferencesBaseline = null;
        if(editingFilter != nodeId) { selectedCheckpoint = 0; selectedClause = 0; }
        selected = editingFilter = nodeId;
        RenderFilterPage(); return true;
    }

    private void RenderFilterPage()
    {
        if(Node(editingFilter) is not JObject node)return;
        var body = new StackPanel { Name = "CareerWorkflowFilterEditor", Spacing = 10, Margin = new Thickness(4) };
        body.Children.Add(new TextBlock { Text = "编辑筛选条件", FontSize = 21, FontWeight = FontWeight.SemiBold });
        body.Children.Add(Note("修改作用于当前命名配置；保存后用于下一次新启动。正在进行或继续原局时仍沿用原局快照。"));
        if((string?)node["kind"] == "cut")BuildCutScreening(node, body);
        else BuildConditionControls(node, body, RenderFilterPage);
        body.Children.Add(Note("满足、不满足、数据不足分别沿用此模式已有的后续分支；这里不改连线，不启动育成。"));
        body.Children.Add(Button("CareerWorkflowFilterNodeDetails", "查看此步骤与分支连接", () => { ClosePolicy(); SelectNode((string)node["id"]!); }));
        content.Content = null; content.Content = body;
        back.Content = "总流程  ›  筛选条件（点击返回）"; back.IsVisible = true; RefreshChrome();
    }

    private JObject? ScreeningCapabilities => schema?["screening_capabilities"] as JObject;
    private bool CanEditScreening => (bool?)ScreeningCapabilities?["available"] == true;

    private void BuildCutScreening(JObject node, StackPanel body)
    {
        var value = node["screening"];
        if(value != null && value.Type != JTokenType.Null && value is not JObject) {
            body.Children.Add(Note("此筛选使用高级参数，原文完整保留；请在高级编辑器查看。"));
            body.Children.Add(new TextBox { Name = "CareerWorkflowScreeningRaw", Text = value.ToString(), IsReadOnly = true, AcceptsReturn = true, MaxHeight = 220 });return;
        }
        var screening = value as JObject;
        var enabled = new CheckBox { Name = "CareerWorkflowScreeningEnabled", Content = "启用自定义组合筛选", IsChecked = CustomScreening(node), IsEnabled = CanEditScreening };
        body.Children.Add(enabled);
        body.Children.Add(Note(CustomScreening(node)
            ? "检查点按下列顺序执行，每点可分别设置通过与未通过的处理。未设置动作时沿用原处理；只有明确选择失败走重开分支，才允许重开已有切的局。"
            : "当前沿用默认筛选：提前确认有切即通过；到原截止点确认无切才进入不满足分支；数据不足暂停并保留。下面的草稿不会自动启用。"));
        if(!CanEditScreening) {
            body.Children.Add(Note("当前服务尚未提供组合筛选能力，现有参数保持原样。"));
            if(screening != null)body.Children.Add(new TextBox { Name = "CareerWorkflowScreeningRaw", Text = screening.ToString(), IsReadOnly = true, AcceptsReturn = true, MaxHeight = 240 });return;
        }
        if(screening?["checkpoints"] is JToken raw && raw is not JArray) {
            body.Children.Add(Note("检查点包含高级格式，未转换或丢弃。"));return;
        }
        if(screening?["checkpoints"] is JArray existing && existing.Any(p => p is not JObject)) {
            body.Children.Add(Note("检查点包含高级片段，原文完整保留。"));return;
        }
        enabled.IsCheckedChanged += (_, _) => {
            var target = EnsureScreening(node);target["enabled"] = enabled.IsChecked == true;
            if(enabled.IsChecked == true && ((JArray)target["checkpoints"]!).Count == 0)((JArray)target["checkpoints"]!).Add(NewCheckpoint());
            FilterEdited();
        };
        var checkpoints = screening?["checkpoints"] as JArray;
        var buttons = new WrapPanel();
        var add = Button("CareerWorkflowAddCheckpoint", "＋ 添加检查点", () => {
            var rows = (JArray)EnsureScreening(node)["checkpoints"]!;
            rows.Add(NewCheckpoint());selectedCheckpoint = rows.Count - 1;selectedClause = 0;FilterEdited();
        });
        add.IsEnabled = ScreeningCapabilities?["max_checkpoints"] is not JValue maximum || maximum.Type != JTokenType.Integer || (checkpoints?.Count ?? 0) < (long)maximum;
        buttons.Children.Add(add);body.Children.Add(buttons);
        if(checkpoints == null || checkpoints.Count == 0) { body.Children.Add(Note("尚无自定义检查点。添加后可设置检查时间和一组条件；默认筛选仍保持关闭。"));return; }
        selectedCheckpoint = Math.Clamp(selectedCheckpoint, 0, checkpoints.Count - 1);
        var picker = new ComboBox { Name = "CareerWorkflowCheckpoint", ItemsSource = checkpoints.OfType<JObject>().Select((p, i) => (i + 1) + ". " + CheckpointLabel(p)).ToArray(), SelectedIndex = selectedCheckpoint };
        body.Children.Add(Note("按顺序检查；每个检查点可以有多项条件"));body.Children.Add(picker);
        picker.SelectionChanged += (_, _) => { if(picker.SelectedIndex >= 0) { selectedCheckpoint = picker.SelectedIndex;selectedClause = 0;RenderFilterPage(); } };
        var point = (JObject)checkpoints[selectedCheckpoint];
        buttons.Children.Add(Button("CareerWorkflowRemoveCheckpoint", "删除此检查点", () => { point.Remove();selectedCheckpoint = Math.Max(0, selectedCheckpoint - 1);selectedClause = 0;FilterEdited(); }));
        if(selectedCheckpoint > 0)buttons.Children.Add(Button("CareerWorkflowCheckpointUp", "上移", () => { var index = selectedCheckpoint;point.Remove();checkpoints.Insert(index - 1, point);selectedCheckpoint--;FilterEdited(); }));
        if(selectedCheckpoint < checkpoints.Count - 1)buttons.Children.Add(Button("CareerWorkflowCheckpointDown", "下移", () => { var index = selectedCheckpoint;point.Remove();checkpoints.Insert(index + 1, point);selectedCheckpoint++;FilterEdited(); }));
        BuildCheckpointTiming(point, body);
        BuildCheckpointActions(node, point, body);
        var catalog = (ScreeningCapabilities?["condition_kinds"] as JArray ?? schema?["condition_kinds"] as JArray ?? new()).OfType<JObject>().ToArray();
        var timing = (string?)point["check_at"];
        var available = catalog.Where(row => row["supported_check_at"] is not JArray times || times.Values<string>().Contains(timing)).ToArray();
        body.Children.Add(new TextBlock { Text = "这个检查点的组合条件", FontSize = 16, FontWeight = FontWeight.SemiBold });
        BuildConditionControls(point, body, RenderFilterPage, false, available);
        body.Children.Add(Note("指定日期起的首次正常主页检查；晚接管时使用当前鲜数据，不补写过去的结果。未到时间继续培养，日期未确认或条件数据不足时暂停保留。技能、适性、羁绊等仅在检查点按需采集。"));
    }

    private static JObject EnsureScreening(JObject node)
    {
        if(node["screening"] is not JObject value)node["screening"] = value = new JObject { ["enabled"] = false, ["checkpoints"] = new JArray() };
        if(value["checkpoints"] == null)value["checkpoints"] = new JArray();return value;
    }

    private static JObject NewCheckpoint() => new() { ["id"] = "cp_" + Guid.NewGuid().ToString("N")[..8], ["check_at"] = "home",
        ["when"] = new JObject { ["all"] = new JArray(new JObject { ["kind"] = "cut_present" }) } };

    private void FilterEdited() { surface.InvalidateVisual();RefreshLanes();Changed?.Invoke();RenderFilterPage(); }

    private void BuildCheckpointActions(JObject node, JObject point, StackPanel body)
    {
        var capabilities = ScreeningCapabilities?["checkpoint_actions"] as JObject;
        if(capabilities == null) { body.Children.Add(Note("此服务使用原检查点处理；当前保存的动作字段原样保留。"));return; }
        body.Children.Add(new TextBlock { Text = "这个检查点的后续处理", FontSize = 16, FontWeight = FontWeight.SemiBold });
        foreach(var field in new[] { "on_pass", "on_fail" }) {
            var passing = field == "on_pass";
            var keys = new[] { "" }.Concat((capabilities[field] as JArray ?? new()).Values<string>().Where(s => s != null)).ToArray();
            var selectedAction = point[field]?.Type == JTokenType.String ? (string?)point[field] : null;
            var picker = new ComboBox { Name = "CareerWorkflowCheckpoint_" + field,
                ItemsSource = keys.Select(key => CheckpointActionLabel(field, key)), SelectedIndex = point[field] == null ? 0 : Array.IndexOf(keys, selectedAction) };
            body.Children.Add(Note(passing ? "条件通过时" : "条件未通过时"));body.Children.Add(picker);
            if(point[field] != null && picker.SelectedIndex < 0)body.Children.Add(Note("此动作属于高级格式，原文保留；选择新动作才会替换。"));
            picker.SelectionChanged += (_, _) => {
                if(picker.SelectedIndex < 0)return;
                if(picker.SelectedIndex == 0)point.Remove(field);else point[field] = keys[picker.SelectedIndex];
                FilterEdited();
            };
        }
        var failureTarget = Edges.FirstOrDefault(e => (string?)e["from"] == (string?)node["id"] && (string?)e["outcome"] == "not_matched")?["to"];
        if((string?)point["on_fail"] == "not_matched_branch" && (string?)Node((string?)failureTarget)?["kind"] == "restart")
            body.Children.Add(Note("此点已明确允许未通过时重开本局，包含已确认有切的局；本批次数与资源额度继续累计。"));
        body.Children.Add(Note("数据不足：暂停并保留，不自动套用未通过处理。继续下一检查点若已是末点，则保存暂停。"));
    }

    private static string CheckpointActionLabel(string field, string? action) => action switch {
        "next_checkpoint" => "继续培养到下一检查点（末点保存暂停）",
        "matched_branch" => "进入本模式的满足分支（跳过后续检查点）",
        "not_matched_branch" => "进入本模式的不满足分支（若连接重开，允许本局重开）",
        "hold" => "暂停并保留当前局",
        "" or null => field == "on_pass" ? "沿用原处理：下一检查点，末点进入满足分支" : "沿用原处理：不满足分支，已有切仍保留",
        _ => action
    };

    private void BuildCheckpointTiming(JObject point, StackPanel body)
    {
        var times = (ScreeningCapabilities?["supported_check_at"] as JArray ?? new JArray("home", "junior", "inheritance", "cut_checkpoint", "game_date")).Values<string>().Where(s => s != null).ToArray();
        var picker = new ComboBox { Name = "CareerWorkflowCheckpointTiming", ItemsSource = times.Select(ScreeningTimeLabel), SelectedIndex = Array.IndexOf(times, (string?)point["check_at"]) };
        body.Children.Add(Note("检查时间（游戏内日期）"));body.Children.Add(picker);
        picker.SelectionChanged += (_, _) => {
            if(picker.SelectedIndex < 0)return;point["check_at"] = times[picker.SelectedIndex];
            if(times[picker.SelectedIndex] == "game_date" && point["game_date"] == null)point["game_date"] = new JObject { ["year"] = 1, ["month"] = 12, ["half"] = "second" };
            FilterEdited();
        };
        if((string?)point["check_at"] != "game_date")return;
        if(point["game_date"] is not JObject date || date["year"]?.Type != JTokenType.Integer || date["month"]?.Type != JTokenType.Integer) { body.Children.Add(Note("此检查点的游戏日期未设置或属于高级格式；原文保持不变。"));return; }
        var row = new WrapPanel();
        var year = new ComboBox { Name = "CareerWorkflowGameYear", ItemsSource = new[] { "初级年", "经典年", "高级年" }, SelectedIndex = ((int?)date["year"] ?? 0) - 1 };
        var month = new ComboBox { Name = "CareerWorkflowGameMonth", ItemsSource = Enumerable.Range(1, 12).Select(m => m + "月").ToArray(), SelectedIndex = ((int?)date["month"] ?? 0) - 1 };
        var half = new ComboBox { Name = "CareerWorkflowGameHalf", ItemsSource = new[] { "上旬", "下旬" }, SelectedIndex = (string?)date["half"] == "first" ? 0 : (string?)date["half"] == "second" ? 1 : -1 };
        foreach(var control in new[] { year, month, half }) { control.Margin = new Thickness(0, 0, 8, 0);row.Children.Add(control); }
        body.Children.Add(row);
        year.SelectionChanged += (_, _) => { if(year.SelectedIndex >= 0) { date["year"] = year.SelectedIndex + 1;FilterEdited(); } };
        month.SelectionChanged += (_, _) => { if(month.SelectedIndex >= 0) { date["month"] = month.SelectedIndex + 1;FilterEdited(); } };
        half.SelectionChanged += (_, _) => { if(half.SelectedIndex >= 0) { date["half"] = half.SelectedIndex == 0 ? "first" : "second";FilterEdited(); } };
    }

    private static string ScreeningTimeLabel(string? value) => value switch { "home" => "下一次安全主页 · 检查一次", "game_date" => "指定游戏日期起 · 首次正常主页", _ => TimeLabel(value) };
    private static string CheckpointLabel(JObject point)
    {
        if((string?)point["check_at"] != "game_date")return ScreeningTimeLabel((string?)point["check_at"]);
        var date = point["game_date"] as JObject;var year = date?["year"]?.Type == JTokenType.Integer ? (int?)date["year"] : null;
        return (year == 1 ? "初级年" : year == 2 ? "经典年" : year == 3 ? "高级年" : "年份待设") + date?["month"] + "月" + ((string?)date?["half"] == "first" ? "上旬" : (string?)date?["half"] == "second" ? "下旬" : "旬待设") + "起";
    }

    private static string ScreeningOutcomeLabel(JObject node, string outcome)
    {
        if((string?)node["kind"] != "cut")return outcome == "matched" ? "条件满足" : outcome == "not_matched" ? "条件不满足" : "结果未明";
        if(!CustomScreening(node))return outcome == "matched" ? "有切" : outcome == "not_matched" ? "截止／提前条件无切" : "结果未明";
        return outcome == "matched" ? "条件满足" : outcome == "not_matched" ? "条件不满足" : "结果未明";
    }

    private static string ClauseLabel(JObject row, JObject[] kinds)
    {
        var kind = (string?)row["kind"] ?? "高级条件";
        var label = ConditionLabel(kind, (string?)kinds.FirstOrDefault(k => (string?)k["kind"] == kind)?["label"]);
        var op = (string?)row["op"] switch { "lt" => " < ", "lte" => " ≤ ", "eq" => " = ", "gt" => " > ", _ => " ≥ " };
        return label + (row["value"] != null ? op + row["value"] : row["minimum"] != null ? " ≥ " + row["minimum"] : "");
    }
}
