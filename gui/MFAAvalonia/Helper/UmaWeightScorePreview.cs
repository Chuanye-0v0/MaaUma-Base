using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Explicit, local sample evaluation; never represents an observed game turn.</summary>
public sealed class UmaWeightScorePreview : Border
{
    private readonly ComboBox scenario = new() { Name = "WeightPreviewScenario", ItemsSource = new[] { "第一年 · 初级年", "第二年 · 国内", "第二年 · 远征", "第三年 · 国内", "第三年 · 远征", "第二年 · 低体力" }, SelectedIndex = 1, MinWidth = 180 };
    private static readonly string[] ScenarioKeys = { "junior", "classic_domestic", "classic_expedition", "senior_domestic", "senior_expedition", "low_vitality" };
    private readonly Button calculate = new() { Name = "WeightPreviewCalculate", Content = "计算示例得分" };
    private readonly TextBlock status = Note("选择示例回合，再比较修改前后的动作得分。只计算本地方案。 ");
    private readonly TextBlock trace = Note("");
    private readonly StackPanel candidates = new() { Spacing = 7 };
    private readonly StackPanel detail = new() { Spacing = 7 };
    private readonly Func<JObject> readProfile;
    private Func<JObject, Task<JObject>>? evaluate;
    private JObject? result;
    private int revision;
    private bool busy;
    public JObject? LastResult => result == null ? null : (JObject)result.DeepClone();
    public event Action? Updated;

    public UmaWeightScorePreview(Func<JObject> profile)
    {
        readProfile = profile; Name = "UmaWeightScorePreview";
        Background = UmaWeightTheme.Brush(this, "panel"); Padding = new Thickness(12); CornerRadius = new CornerRadius(8); HorizontalAlignment = HorizontalAlignment.Stretch;
        var body = new StackPanel { Spacing = 9 };
        var controls = new WrapPanel { Orientation = Orientation.Horizontal }; controls.Children.Add(scenario); controls.Children.Add(calculate);
        body.Children.Add(controls); body.Children.Add(status); body.Children.Add(trace);
        var work = new Grid { ColumnDefinitions = new("230,*"), ColumnSpacing = 14, Height = 360 };
        work.Children.Add(new ScrollViewer { Content = candidates });
        var right = new ScrollViewer { Content = detail, HorizontalScrollBarVisibility = Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled }; Grid.SetColumn(right, 1); work.Children.Add(right);
        body.Children.Add(work); Child = body;
        calculate.Click += async (_, _) => await Calculate();
        scenario.SelectionChanged += (_, _) => InvalidatePreview();
        Configure(null);
    }
    public void Configure(Func<JObject, Task<JObject>>? callback)
    {
        evaluate = callback; calculate.IsEnabled = callback != null;
        if (callback == null) status.Text = "当前后端未提供示例评分入口；画布仍可编辑和导出。";
        else if (result == null) status.Text = "选择示例回合，比较关闭节点方案的基线与当前方案；基础取舍保留。";
    }
    public void InvalidatePreview()
    {
        revision++;
        if (result != null) { status.Text = "方案或场景已修改，下面保留上次结果；请重新计算。"; trace.Opacity = .5; candidates.Opacity = detail.Opacity = .5; }
    }
    public async Task Calculate()
    {
        if (busy || evaluate == null) return;
        busy = true; calculate.IsEnabled = false;
        try
        {
            var request = new JObject { ["profile"] = readProfile(), ["scenario"] = ScenarioKeys[Math.Clamp(scenario.SelectedIndex, 0, ScenarioKeys.Length - 1)] };
            var requestedRevision = revision;
            status.Text = "正在本地计算示例……";
            var value = await evaluate(request);
            if ((bool?)value["ok"] != true) throw new InvalidOperationException((string?)value["error"] ?? "示例评分未返回有效结果。");
            if (requestedRevision != revision) { status.Text = "计算期间方案已修改，请重新计算。"; return; }
            Render(value); Updated?.Invoke();
        }
        catch (Exception error) { status.Text = "未完成计算：" + error.Message; }
        finally { busy = false; calculate.IsEnabled = evaluate != null; }
    }
    public void Render(JObject value)
    {
        result = (JObject)value.DeepClone(); candidates.Children.Clear(); detail.Children.Clear();
        trace.Opacity = candidates.Opacity = detail.Opacity = 1;
        var workflow = value["workflow"] as JObject ?? new JObject();
        var enabled = (bool?)workflow["enabled"] == true; var matched = (bool?)workflow["matched"] == true;
        status.Text = (string?)value["sample_note"] ?? "合成示例，使用真实评分器。";
        trace.Text = $"{value["scenario"]?["label"]} · 体力 {value["scenario"]?["vitality"]} · " + (!enabled ? "节点方案关闭，当前未应用积木" : matched ? $"命中：{workflow["stage_label"]} / {workflow["rule_label"]}" : "节点方案开启，当前示例未命中安排") + ((bool?)workflow["advanced_suspended"] == true ? "；旧高级权重暂存" : "");
        var selected = (string?)value["selection"]?["modified_id"];
        foreach (var row in (value["candidates"] as JArray ?? new JArray()).OfType<JObject>())
        {
            var candidate = row;
            var text = $"{candidate["label"]}" + ((string?)candidate["id"] == selected ? " · 当前选择" : "") + ((bool?)candidate["rejected"] == true ? " · 不可选" : "") + $"\n节点关闭 {Score(candidate["baseline_total"])}\n当前方案 {Score(candidate["modified_total"])}";
            var button = new Button { Content = text, HorizontalAlignment = HorizontalAlignment.Stretch, HorizontalContentAlignment = HorizontalAlignment.Left, Padding = new Thickness(8) };
            button.BorderBrush = UmaWeightTheme.Brush(button, (string?)candidate["id"] == selected ? "selected" : "border");
            button.Click += (_, _) => ShowCandidate(candidate); candidates.Children.Add(button);
        }
        var first = (value["candidates"] as JArray)?.OfType<JObject>().FirstOrDefault(c => (string?)c["id"] == selected) ?? (value["candidates"] as JArray)?.OfType<JObject>().FirstOrDefault();
        if (first != null) ShowCandidate(first);
    }
    private void ShowCandidate(JObject candidate)
    {
        detail.Children.Clear();
        detail.Children.Add(new TextBlock { Text = (string?)candidate["label"], FontSize = 17, FontWeight = FontWeight.SemiBold });
        detail.Children.Add(Note($"关闭节点方案的基线 {Score(candidate["baseline_total"])} → 当前方案 {Score(candidate["modified_total"])}；差值 {Score(candidate["delta"])}"));
        detail.Children.Add(Note("基线保留基础取舍，并恢复原有高级权重；用于比较节点方案整体效果。"));
        var priority = (bool?)candidate["priority_active"] == true ? $"类别优先顺序第{((int?)candidate["priority_rank"] ?? 0) + 1}位" : "未指定类别优先，沿实际策略比较";
        detail.Children.Add(Note($"{priority}；训练类别最终倍率：×{candidate["training_multiplier"] ?? 1}。优先行动和可执行状态也参与选择。"));
        if ((bool?)candidate["rejected"] == true || (candidate["risk_flags"] as JArray)?.Count > 0) detail.Children.Add(Note("执行状态／风险：" + string.Join("、", candidate["risk_flags"]?.Values<string>().Select(v => v == "failure_rate_unreliable_or_too_high" ? "失败率不可靠或超过当前上限" : v) ?? Array.Empty<string>())));
        var parts = (candidate["breakdown"] as JArray ?? new JArray()).OfType<JObject>().ToArray();
        var grouped = UmaWeightGraphModel.WeightGroups.Select(group => (Group: group, Parts: parts.Where(p => group.Components.Contains((string?)p["key"])).ToArray())).ToArray();
        var scale = Math.Max(1, grouped.Select(g => Math.Abs(g.Parts.Sum(p => (double?)p["effective"] ?? 0))).DefaultIfEmpty(1).Max());
        foreach (var group in grouped)
        {
            var before = group.Parts.Sum(p => (double?)p["base"] ?? 0); var after = group.Parts.Sum(p => (double?)p["effective"] ?? 0);
            detail.Children.Add(Note($"{group.Group.Label}　{before:0.##} → {after:0.##}"));
            detail.Children.Add(new SignedBar { Value = after, Scale = scale, Height = 13 });
        }
        detail.Children.Add(Note($"分项加总与实际总分的差额：{Score(candidate["residual"])}。属性收益先进入效用计算，再应用分项和训练类别倍率。"));
        var rows = new StackPanel { Spacing = 7 };
        foreach (var part in parts.Where(p => Math.Abs((double?)p["base"] ?? 0) > .0001 || Math.Abs((double?)p["effective"] ?? 0) > .0001 || (double?)p["factor"] is double factor && factor != 1))
            rows.Children.Add(Note($"{part["label"]}：原 {Score(part["base"])} → 属性层后 {Score(part["raw"])} ×{part["factor"]} → 生效 {Score(part["effective"])}"));
        detail.Children.Add(new Expander { Name = "WeightPreviewBreakdown", Header = "逐项展开计算过程", Content = rows, IsExpanded = true, HorizontalContentAlignment = HorizontalAlignment.Stretch });
        if (result?["phase"]?["coefficients"] is JObject coefficients)
            detail.Children.Add(Note("阶段原系数：" + string.Join("；", coefficients.Properties().Select(p => $"{CoefficientLabel(p.Name)} {p.Value}")) + "。这是基础系数，实际贡献还取决于候选收益与当前状态。"));
    }
    private static string CoefficientLabel(string key) => key switch { "stats" => "属性", "skill_pt" => "技能点", "effective_bond" => "羁绊", "net_charge" => "充电", "newly_full" => "新满电", "ss_preparation" => "SS准备", _ => key };
    private static string Score(JToken? value) => value?.Type is JTokenType.Integer or JTokenType.Float ? ((double)value).ToString("0.###", CultureInfo.InvariantCulture) : "—";
    private static TextBlock Note(string text) => new() { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = 12 };
    private sealed class SignedBar : Control
    {
        private readonly IBrush track;
        private readonly IBrush axis;
        private readonly IBrush positive;
        private readonly IBrush negative;
        public double Value { get; init; }
        public double Scale { get; init; }
        public SignedBar()
        {
            track = UmaWeightTheme.Brush(this, "barTrack");
            axis = UmaWeightTheme.Brush(this, "barAxis");
            positive = UmaWeightTheme.Brush(this, "positive");
            negative = UmaWeightTheme.Brush(this, "negative");
            ActualThemeVariantChanged += (_, _) => InvalidateVisual();
        }
        public override void Render(DrawingContext context)
        {
            var center = Bounds.Width / 2; var width = Math.Min(center, Math.Abs(Value) / Scale * center);
            context.FillRectangle(track, new Rect(Bounds.Size));
            context.DrawLine(new Pen(axis, 1), new Point(center, 0), new Point(center, Bounds.Height));
            context.FillRectangle(Value >= 0 ? positive : negative, new Rect(Value < 0 ? center - width : center, 1, width, Math.Max(0, Bounds.Height - 2)));
        }
    }
}
