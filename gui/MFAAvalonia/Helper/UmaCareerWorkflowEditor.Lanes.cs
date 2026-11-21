using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.VisualTree;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaCareerWorkflowEditor
{
    private readonly StackPanel lanes = new() { Name = "CareerWorkflowLanes", Spacing = 12 };
    private readonly List<LaneVisual> laneVisuals = new();
    private readonly HashSet<string> laneRendered = new();
    private readonly List<(WrapPanel Row, int Depth, bool Vertical)> laneRows = new();
    private Grid? planningLanes;
    private bool? planningColumnsWide;
    private sealed record LaneVisual(string Id, Border Border, IBrush Normal, IBrush Selected, IBrush Active);

    private void RefreshDiagram()
    {
        surface.Advanced = view.SelectedIndex == 1;
        diagramView.Content = null;
        if(surface.Advanced)diagramView.Content = advancedHost;
        else { RefreshLanes(); diagramView.Content = lanes; }
        surface.InvalidateMeasure(); surface.InvalidateVisual();
    }

    private void SelectNode(string id)
    {
        if(busy || Node(id) == null)return;
        if(selected != id)selectedClause = 0;
        selected = id; nodeDetails.IsVisible = true; nodeDetails.IsExpanded = true;
        BuildInspector(); RefreshRuntimeHighlight();
        nodeDetails.BringIntoView();
    }

    private void OpenAdvancedNode(string id)
    {
        view.SelectedIndex = 1; SelectNode(id);
    }

    private Button LaneButton(string name, string label, Action action)
    {
        var button = new Button { Name = name, Content = label, Margin = new Thickness(0, 0, 7, 4) };
        button.Click += (_, _) => { if(busy)return; try { action(); } catch(Exception error) { status.Text = error.Message; } };
        return button;
    }

    private string StepLabel(JObject node)
    {
        var label = (string?)node["label"];
        if(!string.IsNullOrWhiteSpace(label))return label;
        return (string?)node["kind"] switch {
            "cut" => "培养至筛选点", "continue_current" => "同一局继续", "condition" => "筛选检查",
            _ => KindLabel((string?)node["kind"])
        };
    }

    private Border LaneCard(string id, Control body, string? role = null)
    {
        var card = new Border { Padding = new Thickness(12), CornerRadius = new CornerRadius(9),
            BorderThickness = new Thickness(1), Child = body };
        card.Background = UmaWeightTheme.Brush(card, role ?? "card");
        var visual = new LaneVisual(id, card, UmaWeightTheme.Brush(card, "border"),
            UmaWeightTheme.Brush(card, "selected"), UmaWeightTheme.Brush(card, "positive"));
        laneVisuals.Add(visual); card.BorderBrush = visual.Normal;
        return card;
    }

    private void RefreshRuntimeHighlight()
    {
        foreach(var item in laneVisuals) {
            item.Border.BorderBrush = item.Id == selected ? item.Selected : item.Id == surface.ActiveNode ? item.Active : item.Normal;
            item.Border.BorderThickness = new Thickness(item.Id == selected || item.Id == surface.ActiveNode ? 2 : 1);
        }
        RefreshDemonstrationHighlights();
    }

    private void RefreshLanes()
    {
        if(surface.Advanced)return;
        lanes.Children.Clear(); laneVisuals.Clear(); laneRendered.Clear(); laneRows.Clear(); planningLanes = null; planningColumnsWide = null;
        BeginReadableRefresh();
        var entry = (string?)(document["run_graph"] as JObject)?["entry"];
        if(entry == null || Node(entry) == null) {
            lanes.Children.Add(Note("尚未载入流程。请在外层选择模式与配置，或载入工作流模板。"));
        } else if(TryBuildReadablePath(entry, out var readable))lanes.Children.Add(readable);
        else { lanes.Children.Add(Note("此流程含高级分支，以下保留原始步骤，未自动简化。")); lanes.Children.Add(BuildPlanningLanes(entry)); }
        var remaining = Nodes.Where(n => !laneRendered.Contains((string?)n["id"] ?? "")).ToArray();
        if(remaining.Length > 0) {
            var shelf = new StackPanel { Spacing = 8 };
            shelf.Children.Add(Note("这些节点仍在原文档中；可以接入流程或在高级画布继续编辑。"));
            foreach(var node in remaining) {
                var id = (string?)node["id"]; if(id == null)continue;
                laneRendered.Add(id); shelf.Children.Add(BuildStepCard(node));
                var outputs = Edges.Where(e => (string?)e["from"] == id).ToArray();
                if(outputs.Length > 0) {
                    var links = outputs.Select(e => OutcomeLabel((string?)e["outcome"] ?? "自定义") + " → " + TargetLabel((string?)e["to"]));
                    shelf.Children.Add(Note("保留的衔接：" + string.Join("；", links)));
                }
            }
            lanes.Children.Add(new Expander { Name = "CareerWorkflowOtherNodes", Header = $"其他节点与高级片段 · {remaining.Length}", Content = shelf,
                IsExpanded = remaining.Length <= 4, HorizontalContentAlignment = HorizontalAlignment.Stretch });
        }
        var rawGraph = document["run_graph"] as JObject;
        var rawNodes = rawGraph?["nodes"] as JArray;
        if(rawNodes != null && rawNodes.Count != Nodes.Count())lanes.Children.Add(Note("另有原始节点数据无法在普通视图展开，已完整保留。"));
        var ids = Nodes.Select(n => (string?)n["id"]).Where(id => id != null).ToHashSet();
        var looseEdges = Edges.Count(e => !ids.Contains((string?)e["from"]) || !ids.Contains((string?)e["to"]));
        if(looseEdges > 0)lanes.Children.Add(Note($"保留 {looseEdges} 条待修复的原始连线；请在高级画布核对。"));
        RefreshRuntimeHighlight();
    }

    private string TargetLabel(string? id) => Node(id) is JObject node ? StepLabel(node) : id ?? "未连接";

    private Control BuildPlanningLanes(string entry)
    {
        var cultivation = new StackPanel { Spacing = 9 };
        var screening = new StackPanel { Spacing = 9 };
        var followup = new StackPanel { Spacing = 9 };
        planningLanes = new Grid { Name = "CareerWorkflowPlanningLanes", ColumnSpacing = 12, RowSpacing = 12 };
        Control Column(string name, string heading, string note, StackPanel body)
        {
            var content = new StackPanel { Spacing = 10 };
            content.Children.Add(new TextBlock { Text = heading, FontSize = 18, FontWeight = FontWeight.SemiBold });
            content.Children.Add(Note(note)); content.Children.Add(body);
            var border = new Border { Name = name, Padding = new Thickness(12), CornerRadius = new CornerRadius(9), Child = content,
                VerticalAlignment = VerticalAlignment.Top };
            border.Background = UmaWeightTheme.Brush(border, "canvas"); return border;
        }
        planningLanes.Children.Add(Column("CareerWorkflowCultivationLane", "培养", "执行培养步骤，策略随当前配置。", cultivation));
        planningLanes.Children.Add(Column("CareerWorkflowScreeningLane", "筛选", "在检查点判断条件。", screening));
        planningLanes.Children.Add(Column("CareerWorkflowFollowupLane", "后续", "各结果对应的处理分别列出。", followup));
        var ancestors = new HashSet<string>(); string? id = entry; var branched = false;
        for(var count = 0; id != null && count < 64; count++) {
            if(Node(id) is not JObject node) { cultivation.Children.Add(Note("未连接到有效步骤：" + id)); break; }
            if(ancestors.Contains(id)) {
                var target = id; cultivation.Children.Add(LaneButton("CareerWorkflowLaneReference_" + id, "↩ 返回「" + StepLabel(node) + "」", () => FocusLaneNode(target))); break;
            }
            var kind = (string?)node["kind"];
            if(kind is "hold" or "finish" or "restart") { followup.Children.Add(BuildLanePath(id, ancestors, 0)); id = null; break; }
            ancestors.Add(id); laneRendered.Add(id);
            if(kind is "cut" or "condition") {
                if(kind == "cut")cultivation.Children.Add(BuildStepCard(node));
                screening.Children.Add(BuildDecisionCard(node));
                followup.Children.Add(BuildBranchLanes(node, ancestors, 0, true)); branched = true; break;
            }
            cultivation.Children.Add(BuildStepCard(node));
            if(node["ss_sieve"] is JObject)screening.Children.Add(PlanSsSieve(node));
            if(node["ace_cycle"] is JObject)screening.Children.Add(PlanAceCycle(node));
            var outputs = Edges.Where(e => (string?)e["from"] == id).ToArray();
            if(outputs.Length == 0)break;
            if(outputs.Length > 1 || !new[] { "entry", "career", "continue_current" }.Contains(kind)) {
                screening.Children.Add(Note("此步骤包含高级分支，原始节点与连线完整保留。"));
                followup.Children.Add(BuildBranchLanes(node, ancestors, 0, true)); branched = true; break;
            }
            id = (string?)outputs[0]["to"];
        }
        if(screening.Children.Count == 0)screening.Children.Add(Note("此模式未设置筛选，按培养流程继续。"));
        if(!branched && followup.Children.Count == 0)followup.Children.Add(Note("完成培养后按当前模式结束；未连接的后续可在步骤属性中补充。"));
        planningLanes.AttachedToVisualTree += (_, _) => ResizeLaneRows();
        ResizeLaneRows(); return planningLanes;
    }

    private Control BuildLanePath(string? id, HashSet<string> ancestors, int depth)
    {
        var path = new StackPanel { Spacing = 9 }; var trail = new HashSet<string>(ancestors);
        for(var step = 0; id != null && step < 64; step++) {
            if(Node(id) is not JObject node) { path.Children.Add(Note("未连接到有效步骤：" + id)); break; }
            if(trail.Contains(id) || laneRendered.Contains(id)) {
                var target = id; var text = (trail.Contains(id) ? "↩ 返回「" : "汇入「") + StepLabel(node) + "」";
                path.Children.Add(LaneButton("CareerWorkflowLaneReference_" + id, text, () => FocusLaneNode(target))); break;
            }
            trail.Add(id); laneRendered.Add(id); var kind = (string?)node["kind"];
            if(kind is "condition" or "cut") {
                if(kind == "cut")path.Children.Add(BuildStepCard(node));
                path.Children.Add(BuildDecisionCard(node));
                var branches = BuildBranchLanes(node, trail, depth);
                if(depth > 0)path.Children.Add(new Expander { Header = "后续分支", Content = branches, IsExpanded = false, HorizontalContentAlignment = HorizontalAlignment.Stretch });
                else path.Children.Add(branches);
                break;
            }
            path.Children.Add(BuildStepCard(node));
            if(node["ss_sieve"] is JObject)path.Children.Add(PlanSsSieve(node));
            if(node["ace_cycle"] is JObject)path.Children.Add(PlanAceCycle(node));
            var outputs = Edges.Where(e => (string?)e["from"] == id).ToArray();
            if(outputs.Length == 0) {
                if(kind is not ("hold" or "finish"))path.Children.Add(Note("后续尚未连接，可在步骤属性中指定。"));
                break;
            }
            if(outputs.Length > 1 || !new[] { "entry", "career", "continue_current", "restart" }.Contains(kind)) {
                path.Children.Add(BuildBranchLanes(node, trail, depth)); break;
            }
            var outcome = (string?)outputs[0]["outcome"] ?? "next";
            if(outcome is not ("next" or "completed"))path.Children.Add(Note("自定义衔接 · " + OutcomeLabel(outcome)));
            id = (string?)outputs[0]["to"];
        }
        return path;
    }

    private Control BuildBranchLanes(JObject node, HashSet<string> ancestors, int depth, bool vertical = false)
    {
        var id = (string)node["id"]!;
        var outputs = Edges.Where(e => (string?)e["from"] == id).ToArray();
        var decision = (string?)node["kind"] is "cut" or "condition";
        var outcomes = (decision ? new[] { "matched", "not_matched", "unknown" } : Array.Empty<string>())
            .Concat(outputs.Select(e => (string?)e["outcome"] ?? "自定义")).Distinct().ToArray();
        var row = new WrapPanel { Name = "CareerWorkflowBranchLanes_" + id, Orientation = Orientation.Horizontal };
        laneRows.Add((row, depth, vertical));
        foreach(var outcome in outcomes) {
            var title = outcome switch { "matched" => "满足", "not_matched" => "不满足", "unknown" => "暂未识别", _ => OutcomeLabel(outcome) };
            var body = new StackPanel { Spacing = 10 };
            var heading = new TextBlock { Text = title, FontSize = 15, FontWeight = FontWeight.SemiBold };
            heading.Foreground = UmaWeightTheme.Brush(heading, outcome == "matched" ? "positive" : outcome == "not_matched" ? "negative" : "muted");
            body.Children.Add(heading);
            if(outcome == "unknown")body.Children.Add(Note("数据不足保持独立，等待有效结果。"));
            var edges = outputs.Where(e => (string?)e["outcome"] == outcome).ToArray();
            if(edges.Length == 0)body.Children.Add(LaneButton("CareerWorkflowConnect_" + id + "_" + outcome, "指定后续步骤", () => SelectNode(id)));
            else if(edges.Length > 1 || depth >= 6) {
                body.Children.Add(Note("此处保留高级分支，未转换或删减连线。"));
                foreach(var edge in edges)body.Children.Add(Note("→ " + TargetLabel((string?)edge["to"])));
                body.Children.Add(LaneButton("CareerWorkflowAdvancedBranch_" + id + "_" + outcome, "打开高级片段", () => OpenAdvancedNode(id)));
            } else body.Children.Add(BuildLanePath((string?)edges[0]["to"], ancestors, depth + 1));
            var lane = new Border { Child = body, Padding = new Thickness(12), CornerRadius = new CornerRadius(9),
                Margin = new Thickness(0, 0, 10, 10),
                Width = 290, MinWidth = 210, VerticalAlignment = VerticalAlignment.Top };
            lane.Background = UmaWeightTheme.Brush(lane, "reference");
            row.Children.Add(lane);
        }
        row.AttachedToVisualTree += (_, _) => ResizeLaneRows();
        row.SizeChanged += (_, _) => ResizeLaneRows();
        return row;
    }

    private void ResizeLaneRows()
    {
        ResizeReadablePath();
        var viewWidth = diagramView.Bounds.Width > 0 ? diagramView.Bounds.Width : 900;
        if(planningLanes != null) {
            var wide = diagramView.Bounds.Width >= 900;
            if(planningColumnsWide != wide) {
                planningColumnsWide = wide;
                planningLanes.ColumnDefinitions = new(wide ? "*,*,1.2*" : "*");
                planningLanes.RowDefinitions = new(wide ? "Auto" : "Auto,Auto,Auto");
                for(var i = 0; i < planningLanes.Children.Count; i++) {
                    Grid.SetColumn(planningLanes.Children[i], wide ? i : 0);
                    Grid.SetRow(planningLanes.Children[i], wide ? 0 : i);
                }
            }
        }
        var followupWidth = viewWidth >= 900 ? (viewWidth - 24) * 1.2 / 3.2 - 24 : viewWidth - 24;
        foreach(var item in laneRows) {
            var available = item.Vertical ? followupWidth : item.Depth == 0 ? viewWidth : followupWidth - item.Depth * 32;
            var width = Math.Max(220, available);
            var columns = !item.Vertical && width >= 760 && item.Depth == 0 ? 3 : !item.Vertical && width >= 540 && item.Depth == 0 ? 2 : 1;
            foreach(var child in item.Row.Children.OfType<Border>())child.Width = Math.Max(210, (width - 14 * columns) / columns);
        }
    }

    private void FocusLaneNode(string id)
    {
        selected = id; RefreshRuntimeHighlight();
        laneVisuals.FirstOrDefault(item => item.Id == id)?.Border.BringIntoView();
    }

    private Control BuildDecisionCard(JObject node)
    {
        var id = (string)node["id"]!; var body = new StackPanel { Spacing = 7 };
        body.Children.Add(new TextBlock { Text = (string?)node["kind"] == "cut" ? "◇ 切状态检查" : "◇ " + StepLabel(node), FontWeight = FontWeight.SemiBold, FontSize = 16 });
        body.Children.Add(Note((string?)node["kind"] == "cut" ? CustomScreening(node) ? "按本配置自定义检查点依次筛选；数据不足暂停保留。" : "确认有切继续满足分支；到截止点确认无切才进入不满足分支。" : ConditionSummary(node)));
        body.Children.Add(LaneButton("CareerWorkflowEditFilter_" + id, "编辑筛选条件", () => SelectFilter(id)));
        return LaneCard(id, body, "reference");
    }

    private Control BuildStepCard(JObject node)
    {
        var id = (string)node["id"]!; var kind = (string?)node["kind"];
        if(kind == "entry") {
            var start = LaneButton("CareerWorkflowLaneInspect_" + id, "开始", () => SelectNode(id));
            start.HorizontalAlignment = HorizontalAlignment.Left; return start;
        }
        var known = new[] { "entry", "career", "cut", "continue_current", "restart", "hold", "finish" }.Contains(kind);
        var body = new StackPanel { Spacing = 7 };
        var actions = new WrapPanel();
        if(kind is "entry" or "continue_current") {
            body.Children.Add(LaneButton("CareerWorkflowLaneInspect_" + id, kind == "entry" ? "开始" : "↳ 同一局继续育成", () => SelectNode(id)));
            if(kind == "continue_current")body.Children.Add(Note("沿用已找到切的原局，不重新开局。"));
        } else {
            body.Children.Add(new TextBlock { Text = known ? StepLabel(node) : "高级子流程 · " + StepLabel(node), FontSize = 16, FontWeight = FontWeight.SemiBold });
            var summary = kind switch { "cut" => "按当前配置培养至有效筛选点，策略与判定分开设置。", "career" => "按当前配置继续培养，使用本步骤的分段策略。", "restart" => "按本配置的局数与补给上限准备下一局。", "hold" => "暂停自动推进，保留当前局。", "finish" => "本工作流结束并保留结果。", _ => "完整节点、参数及连线保留在原文档中。" };
            body.Children.Add(Note(summary));
            actions.Children.Add(LaneButton("CareerWorkflowLaneInspect_" + id, "步骤属性", () => SelectNode(id)));
        }
        if(kind is "career" or "cut") {
            body.Children.Add(Note(PolicySummary(node)));
            actions.Children.Add(LaneButton("CareerWorkflowLanesPolicy_" + id, "编辑本配置策略 ›", () => SelectPolicy(id)));
        }
        if(!known)body.Children.Add(LaneButton("CareerWorkflowLaneAdvanced_" + id, "打开高级画布", () => OpenAdvancedNode(id)));
        if(actions.Children.Count > 0)body.Children.Add(actions);
        return LaneCard(id, body);
    }

    private string PolicySummary(JObject node)
    {
        var policyId = (string?)node["policy"] ?? "default";
        var policy = (document["policies"] as JObject)?[policyId] as JObject;
        var cut = (string?)node["kind"] == "cut";
        var options = policy?[cut ? "cut_options" : "career_options"] as JObject;
        var settings = cut ? options?["cut"] as JObject : options;
        var preferences = settings?["decision_preferences"] as JObject;
        var workflow = preferences?["workflow"] as JObject;
        var tuning = preferences?["tuning"] as JObject;
        var enabled = workflow?["enabled"]?.Type == JTokenType.Boolean && (bool)workflow["enabled"]!;
        var name = policy?["label"]?.Type == JTokenType.String ? (string?)policy["label"] : policy?["name"]?.Type == JTokenType.String ? (string?)policy["name"] : null;
        var tuningMode = PolicyText(tuning?["mode"]);
        if(string.IsNullOrWhiteSpace(name))name = enabled || tuningMode is "detailed" or "formula" ? "未命名自定义权重" : "默认策略";
        var revision = (options?["larc_policy"] as JObject)?["revision"];
        var version = revision?.Type == JTokenType.String ? "策略 " + revision : "版本未记录";
        var stages = (workflow?["stages"] as JArray ?? new()).OfType<JObject>().ToArray();
        var active = stages.Where(stage => stage["enabled"]?.Type != JTokenType.Boolean || (bool)stage["enabled"]!).ToArray();
        var rules = active.SelectMany(stage => (stage["rules"] as JArray ?? new()).OfType<JObject>())
            .Where(rule => rule["enabled"]?.Type != JTokenType.Boolean || (bool)rule["enabled"]!).ToArray();
        var overrides = rules.Sum(rule => PolicyOverrideCount(rule["effects"] as JObject));
        var phase = enabled ? "分段已启用：" + (active.Length == 0 ? "尚无启用阶段" : string.Join("、", active.Take(2).Select(stage => PolicyText(stage["label"]) ?? PolicyText(stage["id"]) ?? "未命名阶段")))
            + (active.Length > 2 ? "等 " + active.Length + " 段" : "") + " · 覆盖 " + overrides + " 项"
            : "分段关闭" + (stages.Length > 0 ? "（保留 " + stages.Length + " 段草稿）" : "");
        if(!enabled && tuningMode == "detailed")phase += " · 细项覆盖 " + ((tuning?["factors"] as JObject)?.Count ?? 0) + " 项";
        else if(!enabled && tuningMode == "formula")phase += " · 使用已保存评分公式";
        var basis = new[] { ("numeric", "数值"), ("bond", "羁绊"), ("charge", "充电") }
            .Where(pair => preferences?[pair.Item1]?.Type is JTokenType.Integer or JTokenType.Float)
            .Select(pair => pair.Item2 + "×" + preferences![pair.Item1])
            .Concat(new[] { ("gold_ship_bonus", "黄金船额外分"), ("mei_bonus", "黄帽额外分") }
                .Where(pair => preferences?[pair.Item1]?.Type is JTokenType.Integer or JTokenType.Float).Select(pair => pair.Item2 + " " + preferences![pair.Item1]));
        var values = string.Join("、", basis);
        return (cut ? "前期" : "养完") + " · " + name + " · " + version + " · 引用 " + policyId + "\n" + phase
            + (values.Length > 0 ? "；" + values : "；基础数值未单独保存（执行器默认）") + "\n来源：本配置「" + StepLabel(node) + "」的策略快照";
    }

    private static int PolicyOverrideCount(JObject? effects)
    {
        if(effects == null)return 0;
        var count = new[] { "training_multipliers", "component_factors", "attribute_weights" }.Sum(key => (effects[key] as JObject)?.Count ?? 0);
        if((effects["preferred_actions"] as JArray)?.Count > 0)count++;
        if(effects["after_target_credit"] != null)count++;
        if(effects["adaptation"] is JObject adaptation && (PolicyText(adaptation["mode"]) is "prefer" or "defer" || adaptation["reserve_points"]?.Type == JTokenType.Integer && (long)adaptation["reserve_points"]! > 0))count++;
        return count;
    }
    private static string? PolicyText(JToken? value) => value?.Type == JTokenType.String ? (string?)value : null;

    private string ConditionSummary(JObject node)
    {
        if(node["when"] is not JObject when || when.Properties().FirstOrDefault()?.Value is not JArray clauses)return "高级判定规则已完整保留。";
        var labels = (schema?["condition_kinds"] as JArray ?? new()).OfType<JObject>()
            .Where(k => k["kind"]?.Type == JTokenType.String).GroupBy(k => (string)k["kind"]!).ToDictionary(g => g.Key, g => ConditionLabel(g.Key, (string?)g.First()["label"]));
        var descriptions = clauses.OfType<JObject>().Select(clause => {
            var kind = (string?)clause["kind"] ?? "高级条件"; var label = labels.GetValueOrDefault(kind, ConditionLabel(kind, kind));
            var op = (string?)clause["op"] switch { "lt" => " < ", "lte" => " ≤ ", "eq" => " = ", "gt" => " > ", "gte" => " ≥ ", _ => " " };
            return label + (clause["value"] != null ? op + clause["value"] : clause["minimum"] != null ? " ≥ " + clause["minimum"] : "");
        });
        return (when.Property("any") != null ? "任一满足：" : "全部满足：") + string.Join("；", descriptions) + " · " + TimeLabel((string?)node["check_at"]);
    }
}
