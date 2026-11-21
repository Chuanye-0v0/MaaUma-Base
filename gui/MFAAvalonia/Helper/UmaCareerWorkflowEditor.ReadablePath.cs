using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaCareerWorkflowEditor
{
    private Grid? readableGrid;
    private JObject? readableDocument;
    private string? demonstratedOutcome;
    private TextBlock? demoCaption, readableDown1, readableDown2;
    private WorkflowPathArrow? readableArrow1;
    private Control? readableLoopSpace;
    private string? readableCheckId, readablePrimaryId;
    private readonly HashSet<string> readableCultivationIds = new();
    private readonly Dictionary<string,Control> readableOutputPorts = new();
    private readonly Dictionary<string,(string Source,string Target,string Outcome,bool Restarts)> readableReturnEdges = new();
    private readonly Dictionary<string,WorkflowPathArrow> readableReturnArrows = new();
    private readonly List<Control> readableReturnLabels = new();
    private readonly List<ReadableResult> readableResults = new();
    private readonly List<(WorkflowPathArrow Arrow, IBrush Normal, IBrush Highlight)> readableArrows = new();
    private readonly Dictionary<string, (WorkflowPathArrow Arrow, IBrush Normal, IBrush Highlight)> resultArrows = new();
    private sealed record ReadableResult(string Outcome, Border Card, IBrush Normal, IBrush Accent, IBrush PlainBackground, IBrush HighlightBackground);
    public string? DemonstratedOutcome => demonstratedOutcome;

    private void BeginReadableRefresh()
    {
        if(!ReferenceEquals(readableDocument, document)) { readableDocument = document; demonstratedOutcome = null; }
        readableGrid = null; demoCaption = null; readableArrow1 = null; readableDown1 = readableDown2 = null; readableLoopSpace = null;
        readableCheckId = readablePrimaryId = null;readableCultivationIds.Clear();readableOutputPorts.Clear();
        readableReturnEdges.Clear();readableReturnArrows.Clear();readableReturnLabels.Clear();
        readableResults.Clear(); readableArrows.Clear(); resultArrows.Clear();
    }

    private bool TryBuildReadablePath(string entry, out Control control)
    {
        control = new Border(); var prefix = new List<JObject>(); var ancestors = new HashSet<string>();
        JObject? check = null; string? tail = null; var cursor = entry;
        for(var index = 0; index < 64; index++) {
            if(!ancestors.Add(cursor) || Node(cursor) is not JObject node)return false;
            var kind = (string?)node["kind"];
            if(kind is "cut" or "condition") {
                var branches = Edges.Where(e => (string?)e["from"] == cursor).ToArray();
                if(branches.Any(e => (string?)e["outcome"] is not ("matched" or "not_matched" or "unknown"))
                    || branches.GroupBy(e => (string?)e["outcome"]).Any(group => group.Count() > 1))return false;
                check = node; break;
            }
            if(kind is "finish" or "hold") { tail = cursor; ancestors.Remove(cursor); break; }
            if(kind is not ("entry" or "career" or "continue_current"))return false;
            prefix.Add(node); var outputs = Edges.Where(e => (string?)e["from"] == cursor).ToArray();
            var expected = kind == "career" ? "completed" : "next";
            if(outputs.Length != 1 || (string?)outputs[0]["outcome"] != expected || outputs[0]["to"]?.Type != JTokenType.String)return false;
            cursor = (string)outputs[0]["to"]!;
        }
        if(check == null && tail == null)return false;
        var isCut = (string?)check?["kind"] == "cut";
        var primary = isCut ? check : prefix.FirstOrDefault(n => (string?)n["kind"] == "career");
        readableCheckId=(string?)check?["id"];readablePrimaryId=(string?)primary?["id"];
        foreach(var node in prefix)readableCultivationIds.Add((string)node["id"]!);
        if(isCut&&readableCheckId!=null)readableCultivationIds.Add(readableCheckId);
        foreach(var node in prefix)laneRendered.Add((string)node["id"]!);
        if(check != null)laneRendered.Add((string)check["id"]!);
        var panel = new StackPanel { Name = "CareerWorkflowReadablePath", Spacing = 10 };
        var sieveSteps=prefix.Where(node=>node["ss_sieve"] is JObject).ToArray();
        var aceSteps=prefix.Where(node=>node["ace_cycle"] is JObject).ToArray();
        panel.Children.Add(Note(isCut ? CustomScreening(check!) ? "按本配置培养 → 依次检查自定义条件 → 按每点的通过／未通过处理执行；结果未明暂停保留。" : "按本配置前期培养 → 检查切状态 → 有切、截止无切、结果未明分别走下方对应路径。"
            : check != null ? "先执行培养步骤 → 到检查点判断条件 → 只进入对应结果的后续路径。" : sieveSteps.Length>0
            ? "按本配置培养 → SS筛局 → 在限额内重开，直到首个正常结算或停止。" : aceSteps.Length>0
            ? "按本配置连养 → 中途筛马 / 终局毕业验货 → 达目标毕业数或限额时停机。" : "按本配置培养 → 实际育成结算完成 → 保存结果。"));
        if(check != null)panel.Children.Add(BuildDemonstration(isCut && !CustomScreening(check)));
        readableGrid = new Grid { Name = "CareerWorkflowPlanningLanes", ColumnSpacing = 80, RowSpacing = 10 };
        var cultivation = new StackPanel { Spacing = 8 };
        cultivation.Children.Add(Note(isCut ? CutTimingDescription(check!) : primary != null ? "使用所选配置的战马、配卡、培养目标与分段策略。" : "此模式没有自动培养步骤。"));
        if(isCut) {
            var settings = CutSettings(check!);
            cultivation.Children.Add(Note(BudgetText(settings, "max_attempts", "本批最多", "局（含当前局）")));
            cultivation.Children.Add(Note(BudgetText(settings, "max_tp_items", "TP 药上限", "瓶") + "；" + BudgetText(settings, "max_carrots", "萝卜上限", "")));
            if((bool?)settings?["restart_after_character_cut_miss"] == true)cultivation.Children.Add(Note("专属小切事件结束且确认无切时，可按配置提前重开。"));
            if((bool?)settings?["restart_after_training_failure"] == true)cultivation.Children.Add(Note("训练失败后仍须确认无切，才按本批配置重开。"));
        }
        if(primary != null) {
            var id = (string)primary["id"]!;
            var summary = Note(PolicySummary(primary));summary.Name = "CareerWorkflowPolicySummary_" + id;cultivation.Children.Add(summary);
            var buttons = new WrapPanel();buttons.Children.Add(LaneButton("CareerWorkflowLaneInspect_" + id, "培养设置", () => SelectNode(id)));
            buttons.Children.Add(LaneButton("CareerWorkflowLanesPolicy_" + id, "编辑培养策略", () => SelectPolicy(id)));cultivation.Children.Add(buttons);
        }
        foreach(var execution in prefix.Where(node => (string?)node["kind"] == "career" && (string?)node["id"] != (string?)primary?["id"])) {
            var id = (string)execution["id"]!;var summary = Note(PolicySummary(execution));summary.Name = "CareerWorkflowPolicySummary_" + id;cultivation.Children.Add(summary);
            cultivation.Children.Add(LaneButton("CareerWorkflowLanesPolicy_" + id, "编辑「" + StepLabel(execution) + "」策略", () => SelectPolicy(id)));
        }
        if(prefix.Count > 0)cultivation.Children.Add(TechnicalSteps(prefix, "前期步骤详情", primary == null ? null : (string?)primary["id"]));
        var screening = new StackPanel { Spacing = 8 };
        foreach(var step in sieveSteps)screening.Children.Add(PlanSsSieve(step));
        foreach(var step in aceSteps)screening.Children.Add(PlanAceCycle(step));
        if(check == null) {
            if(sieveSteps.Length==0&&aceSteps.Length==0)screening.Children.Add(Note("以执行器报告的真实育成结算为准。"));
        }
        else {
            if(!isCut)screening.Children.Add(Note(ConditionSummary(check)));
            else if(CustomScreening(check))screening.Children.Add(Note("自定义筛选 · " + ((check["screening"] as JObject)?["checkpoints"] as JArray)?.Count + " 个检查点"));
            foreach(var pair in new[] {("matched",ScreeningOutcomeLabel(check,"matched")),("not_matched",ScreeningOutcomeLabel(check,"not_matched")),("unknown","结果未明")}) {
                var port=Note(pair.Item2);port.Name="CareerWorkflowOutputPort_"+pair.Item1;
                port.HorizontalAlignment=HorizontalAlignment.Right;port.Margin=new Thickness(0,3,0,3);
                readableOutputPorts[pair.Item1]=port;screening.Children.Add(port);
            }
        }
        if(check != null) { var id = (string)check["id"]!; screening.Children.Add(LaneButton("CareerWorkflowEditFilter_" + id, "编辑筛选条件", () => SelectFilter(id))); }
        var followup = new StackPanel { Name = "CareerWorkflowBranchLanes_" + ((string?)check?["id"] ?? "completed"), Spacing = 8 };
        if(check != null)foreach(var outcome in new[] { "matched", "not_matched", "unknown" }) {
            var arrow = new WorkflowPathArrow { Name = "CareerWorkflowResultArrow_" + outcome, IsHitTestVisible = false };
            arrow.Name="CareerWorkflowOutcomeArrow_"+outcome;arrow.SourceNodeId=(string)check["id"]!;
            var edge=Edges.FirstOrDefault(e=>(string?)e["from"]==arrow.SourceNodeId&&(string?)e["outcome"]==outcome);
            arrow.TargetNodeId=(string?)edge?["to"];arrow.Outcome=outcome;arrow.ZIndex=2;
            resultArrows[outcome] = (arrow, UmaWeightTheme.Brush(arrow, "muted"), UmaWeightTheme.Brush(arrow, outcome == "matched" ? "positive" : outcome == "not_matched" ? "negative" : "selected"));
            followup.Children.Add(BuildReadableResult(check,outcome,ancestors,primary));
        }
        else if(tail != null)followup.Children.Add(BuildDirectResult(tail, ancestors));
        readableGrid.Children.Add(ReadableColumn("CareerWorkflowCultivationLane", "① 培养", cultivation));
        readableGrid.Children.Add(ReadableColumn("CareerWorkflowScreeningLane", check == null ? (sieveSteps.Length>0 ? "② SS筛局" : aceSteps.Length>0 ? "② 成品筛验" : "② 完成确认") : "② 检查", screening));
        readableGrid.Children.Add(ReadableColumn("CareerWorkflowFollowupLane", "③ 后续（只走一条）", followup));
        readableArrow1 = PathArrow("CareerWorkflowPathArrow_1");readableGrid.Children.Add(readableArrow1);
        foreach(var arrow in resultArrows.Values)readableGrid.Children.Add(arrow.Arrow);
        foreach(var pair in readableReturnEdges) {
            var arrow=new WorkflowPathArrow {Name="CareerWorkflowReturnArrow_"+pair.Key,SourceNodeId=pair.Value.Source,TargetNodeId=pair.Value.Target,Outcome=pair.Value.Outcome,IsHitTestVisible=false,ZIndex=2};
            arrow.Foreground=UmaWeightTheme.Brush(arrow,"muted");readableReturnArrows[pair.Key]=arrow;readableGrid.Children.Add(arrow);
        }
        if(readableReturnArrows.Count>0) {
            var labels=new StackPanel {Height=20+readableReturnArrows.Count*18,VerticalAlignment=VerticalAlignment.Top};
            foreach(var pair in readableReturnEdges) {
                var label=Note(pair.Value.Restarts?"重开后返回培养，本批次数与额度继续累计":"返回前面步骤，沿用原流程设置");
                label.Name="CareerWorkflowReturnLabel_"+pair.Key;label.HorizontalAlignment=HorizontalAlignment.Center;label.Margin=new Thickness(0,labels.Children.Count==0?18:0,0,0);
                labels.Children.Add(label);readableReturnLabels.Add(label);
            }
            readableLoopSpace=labels;Grid.SetRow(labels,1);Grid.SetColumnSpan(labels,3);readableGrid.Children.Add(labels);
        }
        readableDown1 = new TextBlock { Name = "CareerWorkflowPathDown_1", Text = "↓", FontSize = 24, HorizontalAlignment = HorizontalAlignment.Center };
        readableDown2 = new TextBlock { Name = "CareerWorkflowPathDown_2", Text = "↓", FontSize = 24, HorizontalAlignment = HorizontalAlignment.Center };
        cultivation.Children.Add(readableDown1);screening.Children.Add(readableDown2);
        panel.Children.Add(readableGrid);readableGrid.AttachedToVisualTree += (_, _) => ResizeReadablePath();
        var currentGrid = readableGrid;
        currentGrid.LayoutUpdated += (_, _) => { if(ReferenceEquals(currentGrid, readableGrid))UpdatePathArrowEndpoints(); };
        control = panel;ResizeReadablePath();RefreshDemonstrationHighlights();return true;
    }

    private Border ReadableColumn(string name, string heading, Control body)
    {
        var content = new StackPanel { Spacing = 10 };content.Children.Add(new TextBlock { Text = heading, FontSize = 17, FontWeight = FontWeight.SemiBold });content.Children.Add(body);
        var framed = name != "CareerWorkflowFollowupLane";
        var card = new Border { Name = name, Padding = new Thickness(10), CornerRadius = new CornerRadius(8), Child = content,
            VerticalAlignment = framed ? VerticalAlignment.Center : VerticalAlignment.Top,
            BorderThickness = new Thickness(framed ? 1.5 : 0) };
        card.BorderBrush = UmaWeightTheme.Brush(card, "border");
        card.Background = UmaWeightTheme.Brush(card, framed ? "card" : "canvas");return card;
    }

    private WorkflowPathArrow PathArrow(string name)
    {
        var arrow = new WorkflowPathArrow { Name = name, IsHitTestVisible = false, ZIndex = 2 };
        readableArrows.Add((arrow, UmaWeightTheme.Brush(arrow, "muted"), UmaWeightTheme.Brush(arrow, "positive")));return arrow;
    }

    private void ResizeReadablePath()
    {
        if(readableGrid == null)return;var wide = diagramView.Bounds.Width >= 900;
        if(readableGrid.ColumnDefinitions.Count != (wide ? 3 : 1)) {
            readableGrid.ColumnDefinitions = new(wide ? "*,*,1.15*" : "*");readableGrid.RowDefinitions = new(wide ? "Auto,Auto" : "Auto,Auto,Auto,Auto");
            readableGrid.Margin = new Thickness(wide?0:18,0,18,0);
            for(var i = 0;i < 3;i++) { Grid.SetColumn(readableGrid.Children[i], wide ? i : 0);Grid.SetRow(readableGrid.Children[i], wide ? 0 : i); }
            var overlays=new[] {readableArrow1!}.Concat(resultArrows.Values.Select(a=>a.Arrow)).Concat(readableReturnArrows.Values);
            foreach(var arrow in overlays) {
                Grid.SetColumn(arrow,0);Grid.SetRow(arrow,0);Grid.SetColumnSpan(arrow,wide?3:1);Grid.SetRowSpan(arrow,wide?2:4);
            }
            if(readableLoopSpace!=null){Grid.SetRow(readableLoopSpace,wide?1:3);Grid.SetColumn(readableLoopSpace,0);Grid.SetColumnSpan(readableLoopSpace,wide?3:1);}
        }
        if(readableArrow1 != null)readableArrow1.IsVisible = wide;
        foreach(var arrow in resultArrows.Values)arrow.Arrow.IsVisible=wide&&arrow.Arrow.TargetNodeId!=null;
        if(readableDown1 != null)readableDown1.IsVisible = !wide;if(readableDown2 != null)readableDown2.IsVisible = !wide;
    }

    private void UpdatePathArrowEndpoints()
    {
        if(readableGrid == null || readableArrow1 == null || readableGrid.Children.Count < 3)return;
        var cultivation = readableGrid.Children[0];var screening = readableGrid.Children[1];var followup = readableGrid.Children[2];
        var from = cultivation.TranslatePoint(new Point(cultivation.Bounds.Width,cultivation.Bounds.Height/2),readableGrid);
        var to = screening.TranslatePoint(new Point(0,screening.Bounds.Height/2),readableGrid);
        if(from.HasValue && to.HasValue)readableArrow1.SetEndpoints(from.Value,to.Value);
        var screenRight=screening.TranslatePoint(new Point(screening.Bounds.Width,0),readableGrid);
        var index=0;
        foreach(var outcome in new[]{"matched","not_matched","unknown"}) {
            if(!resultArrows.TryGetValue(outcome,out var arrow)||!readableOutputPorts.TryGetValue(outcome,out var port))continue;
            var result=readableResults.FirstOrDefault(r=>r.Outcome==outcome);if(result==null)continue;
            var portPoint=port.TranslatePoint(new Point(0,port.Bounds.Height/2),readableGrid);
            var end=result.Card.TranslatePoint(new Point(0,result.Card.Bounds.Height/2),readableGrid);
            if(screenRight.HasValue&&portPoint.HasValue&&end.HasValue) {
                var start=new Point(screenRight.Value.X,portPoint.Value.Y);
                var channel=start.X+(end.Value.X-start.X)*(4-(++index))/4;
                arrow.Arrow.SetRoute(new[]{start,new Point(channel,start.Y),new Point(channel,end.Value.Y),end.Value});
            }
        }
        var bottoms=new[]{cultivation,screening,followup}.Select(c=>c.TranslatePoint(new Point(0,c.Bounds.Height),readableGrid)?.Y??0).ToArray();
        var bottom=bottoms.Max();index=0;var wide=readableGrid.ColumnDefinitions.Count==3;
        var outerRight=readableResults.Select(r=>r.Card.TranslatePoint(new Point(r.Card.Bounds.Width,0),readableGrid)?.X??0).DefaultIfEmpty(0).Max()+12;
        foreach(var pair in readableReturnArrows) {
            var card=readableResults.FirstOrDefault(r=>r.Outcome==pair.Key)?.Card;if(card==null)continue;
            var target=readableCultivationIds.Contains(pair.Value.TargetNodeId??"")?cultivation:pair.Value.TargetNodeId==readableCheckId?screening:null;
            if(target==null){pair.Value.IsVisible=false;continue;}pair.Value.IsVisible=true;
            var start=card.TranslatePoint(wide?new Point(card.Bounds.Width,card.Bounds.Height/2):new Point(card.Bounds.Width/2,card.Bounds.Height),readableGrid);
            var end=target.TranslatePoint(wide?new Point(target.Bounds.Width/2,target.Bounds.Height):new Point(0,target.Bounds.Height/2),readableGrid);
            if(!start.HasValue||!end.HasValue)continue;var lane=bottom+10+(index++*6);
            pair.Value.SetRoute(wide
                ?new[]{start.Value,new Point(outerRight,start.Value.Y),new Point(outerRight,lane),new Point(end.Value.X,lane),end.Value}
                :new[]{start.Value,new Point(start.Value.X,lane),new Point(-12,lane),new Point(-12,end.Value.Y),end.Value});
        }
    }

    private sealed class WorkflowPathArrow : Control
    {
        private Point[] route=Array.Empty<Point>();
        public Point Start => route.Length>0?route[0]:default;
        public Point End => route.Length>0?route[^1]:default;
        public IReadOnlyList<Point> Route => route;
        public string? SourceNodeId { get; set; }
        public string? TargetNodeId { get; set; }
        public string? Outcome { get; set; }
        public Vector ArrowHeadDirection {
            get {for(var i=route.Length-2;i>=0;i--){var x=End.X-route[i].X;var y=End.Y-route[i].Y;var length=Math.Sqrt(x*x+y*y);if(length>0)return new Vector(x/length,y/length);}return default;}
        }
        private IBrush? foreground;
        public IBrush? Foreground { get=>foreground;set {if(ReferenceEquals(foreground,value))return;foreground=value;InvalidateVisual();} }
        public void SetEndpoints(Point start,Point end)
        {var middle=(start.X+end.X)/2;SetRoute(new[]{start,new Point(middle,start.Y),new Point(middle,end.Y),end});}
        public void SetRoute(IEnumerable<Point> points)
        {var next=points.ToArray();if(route.SequenceEqual(next))return;route=next;InvalidateVisual();}
        public override void Render(DrawingContext context)
        {
            base.Render(context);if(foreground==null || Start==End)return;
            var pen=new Pen(foreground,1.5);
            for(var i=1;i<route.Length;i++)context.DrawLine(pen,route[i-1],route[i]);
            var direction=ArrowHeadDirection;var baseX=End.X-direction.X*6;var baseY=End.Y-direction.Y*6;
            context.DrawLine(pen,new Point(baseX-direction.Y*4,baseY+direction.X*4),End);
            context.DrawLine(pen,new Point(baseX+direction.Y*4,baseY-direction.X*4),End);
        }
    }

    private Control BuildDemonstration(bool cut)
    {
        var buttons = new WrapPanel { VerticalAlignment = VerticalAlignment.Center };
        demoCaption = Note("");demoCaption.Name = "CareerWorkflowDemoCaption";demoCaption.Margin = new Thickness(0, 3, 12, 3);buttons.Children.Add(demoCaption);
        foreach(var pair in new[] { ("matched", cut ? "有切" : "条件满足"), ("not_matched", cut ? "截止无切" : "条件不满足"), ("unknown", "结果未明") }) {
            var outcome = pair.Item1;buttons.Children.Add(CompactPathButton("CareerWorkflowDemo_" + outcome, pair.Item2, () => { demonstratedOutcome = outcome;RefreshDemonstrationHighlights(); }));
        }
        buttons.Children.Add(CompactPathButton("CareerWorkflowDemo_clear", "清除演示", () => { demonstratedOutcome = null;RefreshDemonstrationHighlights(); }));return buttons;
    }

    private void RefreshDemonstrationHighlights()
    {
        if(demoCaption != null)demoCaption.Text = demonstratedOutcome == null ? "演示路径：只做本地高亮，不执行任何动作。"
            : "当前演示：" + (demonstratedOutcome == "matched" ? "满足" : demonstratedOutcome == "not_matched" ? "不满足" : "结果未明") + "。仅高亮，不执行任何动作。";
        foreach(var arrow in readableArrows)arrow.Arrow.Foreground = demonstratedOutcome == null ? arrow.Normal : arrow.Highlight;
        foreach(var arrow in resultArrows)arrow.Value.Arrow.Foreground = demonstratedOutcome == arrow.Key ? arrow.Value.Highlight : arrow.Value.Normal;
        foreach(var pair in readableReturnArrows)pair.Value.Foreground=UmaWeightTheme.Brush(pair.Value,demonstratedOutcome==pair.Key?(pair.Key=="matched"?"positive":pair.Key=="not_matched"?"negative":"selected"):"muted");
        foreach(var result in readableResults) {
            var picked = result.Outcome == demonstratedOutcome;result.Card.BorderBrush = picked ? result.Accent : result.Normal;
            result.Card.BorderThickness = new Thickness(picked ? 2 : 1);result.Card.Background = picked ? result.HighlightBackground : result.PlainBackground;
            result.Card.Opacity = demonstratedOutcome == null || picked ? 1 : .72;
        }
    }

    private JObject? CutSettings(JObject node)
    {
        var policy = (document["policies"] as JObject)?[(string?)node["policy"] ?? "default"] as JObject;
        return (policy?["cut_options"] as JObject)?["cut"] as JObject;
    }

    private string CutTimingDescription(JObject node)
    {
        if(CustomScreening(node))return ScreeningPlanDescription(node);
        var settings = CutSettings(node);var knownDeadline = (string?)schema?["cut_deadline"] == "existing_junior_december_second_half"
            && (string?)node["check_at"] == "cut_checkpoint";
        var timing = knownDeadline ? "初级年12月下为本阶段截止点；提前确认有切即走满足分支。" : "截止点按实际刷切执行器条件；提前确认有切即走满足分支。";
        var mode = (string?)settings?["check_mode"];
        if(mode == "every_turn")timing += " 本配置为每回合检查。";
        else if(mode == "event_triggered")timing += " 本配置按事件触发及已设补查节奏检查。";
        return timing;
    }

    private string ScreeningPlanDescription(JObject node)
    {
        var points = ((node["screening"] as JObject)?["checkpoints"] as JArray ?? new()).OfType<JObject>().ToArray();
        var kinds = (ScreeningCapabilities?["condition_kinds"] as JArray ?? schema?["condition_kinds"] as JArray ?? new()).OfType<JObject>().ToArray();
        var descriptions = points.Take(2).Select((point, index) => {
            var when = point["when"] as JObject;var rows = when?.Properties().FirstOrDefault()?.Value as JArray;
            var group = when?.Property("any") != null ? "任一" : "全部";
            var rules = rows == null ? "高级条件原样保留" : group + "：" + string.Join("、", rows.OfType<JObject>().Take(2).Select(row => ClauseLabel(row, kinds))) + (rows.Count > 2 ? "等 " + rows.Count + " 项" : "");
            var actions = new[] { "on_pass", "on_fail" }.Where(key => point[key]?.Type == JTokenType.String)
                .Select(key => (key == "on_pass" ? "通过：" : "未通过：") + CheckpointActionLabel(key, (string?)point[key]));
            return (index + 1) + ". " + CheckpointLabel(point) + "（" + rules + "）" + (actions.Any() ? "［" + string.Join("；", actions) + "］" : "");
        });
        return "顺序检查：" + (points.Length == 0 ? "尚未设置检查点" : string.Join(" → ", descriptions))
            + (points.Length > 2 ? " → 其余 " + (points.Length - 2) + " 个（编辑筛选条件查看）" : "")
            + "。指定日期起首次正常主页检查；按每点处理执行。未设动作沿用原处理与有切保护，明确失败走重开分支才允许重开有切局。";
    }

    private static string BudgetText(JObject? settings, string key, string label, string unit)
    {
        var token = settings?[key];var raw = token?.Type is JTokenType.Integer or JTokenType.String ? token.ToString().Trim() : "";
        if(raw.Length == 0)return label + "：沿用实际执行器默认";
        if(!long.TryParse(raw, out var amount) || amount < 0)return label + "：待校验（" + raw + "）";
        return label + " " + amount + (unit.Length > 0 ? " " + unit : "");
    }

    private bool StraightChain(string? id, HashSet<string> ancestors, out List<JObject> chain, out string? loop)
    {
        chain = new();loop = null;var seen = new HashSet<string>(ancestors);
        for(var count = 0;id != null && count < 64;count++) {
            if(!seen.Add(id)) { loop = id;return true; }
            if(Node(id) is not JObject node)return false;var kind = (string?)node["kind"];
            if(kind is not ("continue_current" or "career" or "finish" or "hold" or "restart"))return false;
            chain.Add(node);var outputs = Edges.Where(e => (string?)e["from"] == id).ToArray();
            if(kind is "finish" or "hold")return outputs.Length == 0;
            var expected = kind == "career" ? "completed" : "next";
            if(outputs.Length != 1 || (string?)outputs[0]["outcome"] != expected)return false;
            id = (string?)outputs[0]["to"];
        }
        return false;
    }

    private Control BuildReadableResult(JObject check, string outcome, HashSet<string> ancestors, JObject? primary)
    {
        var id = (string)check["id"]!;var edges = Edges.Where(e => (string?)e["from"] == id && (string?)e["outcome"] == outcome).ToArray();
        var isCut = (string?)check["kind"] == "cut";
        var condition = ScreeningOutcomeLabel(check, outcome);
        var body = new StackPanel { Spacing = 4 };var caption = new TextBlock { Text = condition, FontWeight = FontWeight.SemiBold, FontSize = 14 };
        caption.Foreground = UmaWeightTheme.Brush(caption, outcome == "matched" ? "positive" : outcome == "not_matched" ? "negative" : "muted");body.Children.Add(caption);
        var target = edges.Length == 1 ? (string?)edges[0]["to"] : null;
        var nodes = new List<JObject>();string? backTo = null;
        var safe = edges.Length == 1 && StraightChain(target, ancestors, out nodes, out backTo);
        if(!safe) { nodes = new();backTo = null; }
        if(outcome == "unknown" && (nodes.Count != 1 || (string?)nodes[0]["kind"] != "hold"))safe = false;
        if(safe) {
            foreach(var node in nodes)laneRendered.Add((string)node["id"]!);
            var kinds = nodes.Select(n => (string?)n["kind"]).ToArray();
            var path = kinds.SequenceEqual(new[] { "continue_current", "career", "finish" }) && backTo == null ? "原局养完 → 保留结果"
                : kinds.SequenceEqual(new[] { "career", "finish" }) && backTo == null ? "自动培养 → 保存结果"
                : nodes.Count == 1 && kinds[0] == "hold" ? "暂停并保留当前局"
                : nodes.Count == 1 && kinds[0] == "finish" ? isCut && outcome == "matched" ? "保留当前局，停止本批" : "保存结果并结束"
                : nodes.Count == 1 && kinds[0] == "restart" && backTo != null ? "重开下一局" : string.Join(" → ", nodes.Select(StepLabel));
            if(isCut && CustomScreening(check) && outcome == "not_matched" && nodes.Any(n => (string?)n["kind"] == "restart")) {
                var points = ((check["screening"] as JObject)?["checkpoints"] as JArray ?? new()).OfType<JObject>();
                path = points.Any(point => (string?)point["on_fail"] == "not_matched_branch") ? "按该点明确处理重开；未设动作时已有切仍保留" : "确认无切才重开；已有切则保留当前局";
            }
            body.Children.Add(new TextBlock { Name = "CareerWorkflowReadableResult_" + outcome, Text = "→ " + path, FontSize = 15, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap });
            if(backTo != null) {
                var last=nodes.LastOrDefault();var source=(string?)last?["id"]??id;
                var edgeOutcome=last==null?outcome:(string?)last["kind"]=="career"?"completed":"next";
                readableReturnEdges[outcome]=(source,backTo,edgeOutcome,nodes.Any(n=>(string?)n["kind"]=="restart"));
                var destination = backTo;var label = (string?)primary?["id"] == destination ? "↩ 回到①培养" : "↩ 回到「" + TargetLabel(destination) + "」";
                var reference = Note(label);reference.Name = "CareerWorkflowLaneReference_" + destination;body.Children.Add(reference);
            }
            if(outcome == "unknown")body.Children.Add(Note("不重开、不自动重试；当前局保留。"));
            foreach(var career in nodes.Where(n => (string?)n["kind"] == "career")) {
                var careerId = (string)career["id"]!;var summary = Note(PolicySummary(career));summary.Name = "CareerWorkflowPolicySummary_" + careerId;body.Children.Add(summary);
                body.Children.Add(CompactPathButton("CareerWorkflowLanesPolicy_" + careerId, "编辑养完策略", () => SelectPolicy(careerId)));
            }
            body.Children.Add(TechnicalSteps(nodes, "步骤详情", null));
        } else {
            body.Children.Add(new TextBlock { Name = "CareerWorkflowReadableResult_" + outcome, Text = "→ 高级分支（展开查看）", FontWeight = FontWeight.SemiBold });
            var details = new StackPanel { Spacing = 6 };details.Children.Add(Note("此处不是确定单链，原节点和出口完整保留。"));
            foreach(var edge in edges)details.Children.Add(BuildLanePath((string?)edge["to"], ancestors, 1));
            if(edges.Length == 0)details.Children.Add(LaneButton("CareerWorkflowConnect_" + id + "_" + outcome, "指定后续步骤", () => SelectNode(id)));
            body.Children.Add(new Expander { Name = "CareerWorkflowAdvancedResult_" + outcome, Header = "高级分支详情", Content = details, IsExpanded = false, HorizontalContentAlignment = HorizontalAlignment.Stretch });
        }
        var card = new Border { Name = "CareerWorkflowReadableBranch_" + outcome, Child = body, Padding = new Thickness(8), CornerRadius = new CornerRadius(8), BorderThickness = new Thickness(1) };
        var accent = UmaWeightTheme.Brush(card, outcome == "matched" ? "positive" : outcome == "not_matched" ? "negative" : "selected");
        var normal = UmaWeightTheme.Brush(card, "border");var plain = UmaWeightTheme.Brush(card, "card");var highlight = UmaWeightTheme.Brush(card, "reference");
        readableResults.Add(new ReadableResult(outcome, card, normal, accent, plain, highlight));card.BorderBrush = normal;card.Background = plain;
        return card;
    }

    private Control BuildDirectResult(string tail, HashSet<string> ancestors)
    {
        var body = new StackPanel { Spacing = 6 };
        if(StraightChain(tail, ancestors, out var nodes, out var loop) && nodes.Count == 1 && loop == null) {
            laneRendered.Add(tail);body.Children.Add(new TextBlock { Text = (string?)nodes[0]["kind"] == "hold" ? "→ 暂停并保留" : "→ 保存结果", FontSize = 16, FontWeight = FontWeight.SemiBold });
            body.Children.Add(TechnicalSteps(nodes, "步骤详情", null));
        } else body.Children.Add(new Expander { Header = "高级后续（原图保留）", Content = BuildLanePath(tail, ancestors, 1), HorizontalContentAlignment = HorizontalAlignment.Stretch });
        return body;
    }

    private Button CompactPathButton(string name, string label, Action action)
    {
        var button = LaneButton(name, label, action);button.MinHeight = 0;button.Height = 24;button.FontSize = 12;
        button.Padding = new Thickness(6, 1);button.HorizontalAlignment = HorizontalAlignment.Left;button.Margin = new Thickness(0, 0, 7, 1);return button;
    }

    private Button TechnicalSteps(IEnumerable<JObject> nodes, string caption, string? skip)
    {
        var steps = nodes.Where(node => (string?)node["id"] != skip).ToArray();
        return CompactPathButton("CareerWorkflowTechnicalSteps_" + (nodes.FirstOrDefault()?["id"]?.ToString() ?? "none"), "详情", () => {
            inspector.Children.Clear();nodeDetails.Header = caption;nodeDetails.IsVisible = nodeDetails.IsExpanded = true;
            inspector.Children.Add(Note("选择原流程步骤查看属性；节点和执行顺序保持原样。"));
            var navigation = new WrapPanel();
            foreach(var node in steps) {
                var id = (string)node["id"]!;navigation.Children.Add(LaneButton("CareerWorkflowLaneInspect_" + id, StepLabel(node), () => SelectNode(id)));
            }
            inspector.Children.Add(navigation);nodeDetails.BringIntoView();
        });
    }
}
