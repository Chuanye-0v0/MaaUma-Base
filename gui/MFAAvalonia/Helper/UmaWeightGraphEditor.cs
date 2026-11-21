using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Avalonia.Styling;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Free node canvas over the shared typed graph compiler.</summary>
public sealed class UmaWeightGraphEditor : Border
{
    private readonly IBrush ForegroundBrush;
    private readonly IBrush MutedBrush;
    private readonly IBrush PanelBrush;
    private readonly GraphSurface surface;
    private readonly StackPanel properties = new() { Spacing = 10 };
    private readonly ComboBox outputs = new() { Name = "WeightGraphOutputPort", HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly ComboBox inputs = new() { Name = "WeightGraphInputPort", HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly CheckBox enabled = new() { Name = "WeightGraphEnabled", Content = "启用节点方案" };
    private readonly TextBlock status = Note("拖动节点标题移动；从输出端口拖到输入端口，或依次点击两个端口连线。");
    private readonly TextBlock zoom = Note("100%");
    private readonly ComboBox viewMode = Choice("WeightGraphViewMode", new[] { "当前安排", "全部画布" }, 0);
    private readonly CheckBox expandedGroups = new() { Name = "WeightGraphExpandGroups", Content = "展开分项积木", IsChecked = false };
    private readonly ComboBox referencePhase = Choice("WeightGraphReferencePhase", new[] { "初级年前期", "初级年后期", "初级年未分月份", "经典年国内", "经典年远征", "首次凯旋门后", "高级年国内", "高级年远征", "通用阶段" }, 8);
    private readonly ComboBox yearPicker = new() {Name="WeightGraphSectionYear",MinWidth=140,Height=34,MinHeight=0,FontSize=14};
    private readonly ComboBox stagePicker = new() {Name="WeightGraphSectionStage",MinWidth=240,MaxWidth=390,Height=34,MinHeight=0,FontSize=14};
    private readonly ComboBox arrangementPicker = new() {Name="WeightGraphSectionArrangement",MinWidth=240,MaxWidth=390,Height=34,MinHeight=0,FontSize=14};
    private readonly TextBlock sectionSummary = Note("");
    private bool navigating;
    private string? sectionStage,sectionRule;
    private int activeViewMode,activeReferencePhase=8;
    private bool activeExpandedGroups;
    private string[]? pendingFrame;
    private bool framing;
    private RowDefinition? canvasRow;
    private Grid? workArea;
    private record NavigationChoice(string Id,string Label) { public override string ToString()=>Label; }
    private static readonly string[] PhaseKeys = { "junior_early", "junior_late", "junior_unspecified", "classic_domestic", "classic_expedition", "after_first_arc", "senior_domestic", "senior_expedition", "baseline" };
    private string? groupOwner;
    private string? scopeRoot;
    private JObject graph = UmaWeightGraphModel.Empty();
    private JObject? schema;
    private Action<JObject>? readProperties;
    private string? editingNode;
    private bool draftDirty;
    private bool loading;
    private (string Node, string Port)[] outputChoices = Array.Empty<(string, string)>();
    private (string Node, string Port)[] inputChoices = Array.Empty<(string, string)>();
    public event Action? Changed;
    public event Action? DraftChanged;
    public event Action? ScorePreviewRequested;
    public GraphSurface Canvas => surface;

    public UmaWeightGraphEditor()
    {
        Name = "UmaWeightGraphEditor"; MinHeight = 680;
        ForegroundBrush = UmaWeightTheme.Brush(this, "text"); MutedBrush = UmaWeightTheme.Brush(this, "muted"); PanelBrush = UmaWeightTheme.Brush(this, "panel");
        Background = UmaWeightTheme.Brush(this, "canvas"); BorderBrush = UmaWeightTheme.Brush(this, "border"); BorderThickness = new Thickness(1); CornerRadius = new CornerRadius(10);
        surface = new GraphSurface(() => graph) { Name = "WeightGraphSurface", MinHeight = 480, Focusable = true, ReferencePhase="baseline" };
        surface.SizeChanged+=(_,_)=>{if(pendingFrame!=null&&surface.Bounds.Width>=10&&surface.Bounds.Height>=10)FrameNow(pendingFrame);};
        var body = new Grid { RowDefinitions = new("Auto,Auto,480,Auto"), RowSpacing = 1 };canvasRow=body.RowDefinitions[2];
        var toolbar = new WrapPanel { Margin = new Thickness(12, 10), Orientation = Orientation.Horizontal };
        toolbar.Children.Add(new TextBlock { Text = "节点权重画布", FontSize = 20, FontWeight = FontWeight.SemiBold, Foreground = ForegroundBrush, Margin = new Thickness(0, 0, 18, 0) });
        enabled.Margin = new Thickness(0, 0, 16, 0); toolbar.Children.Add(enabled);
        toolbar.Children.Add(Button("WeightGraphExample", "载入三年六组模板", LoadExample));
        toolbar.Children.Add(Button("WeightGraphBaseWeights", "查看共用基础", FocusBaseWeights));
        toolbar.Children.Add(Button("WeightGraphShowScores", "示例得分对比", () => ScorePreviewRequested?.Invoke()));
        toolbar.Children.Add(Button("WeightGraphFit", "适应当前视图", FrameCurrentView));
        toolbar.Children.Add(Button("WeightGraphActualSize", "100% 看选中", () => surface.FocusActualSize()));
        toolbar.Children.Add(viewMode); expandedGroups.Margin = new Thickness(4, 0, 12, 0); expandedGroups.MinWidth = 140; toolbar.Children.Add(expandedGroups);
        var referenceLabel = Note("系数参考（手动选择）："); referenceLabel.Margin = new Thickness(6, 8, 4, 0); toolbar.Children.Add(referenceLabel); referencePhase.Width = 170; toolbar.Children.Add(referencePhase);
        toolbar.Children.Add(Button("WeightGraphZoomOut", "−", () => surface.ZoomAt(.85, new Point(surface.Bounds.Width / 2, surface.Bounds.Height / 2))));
        toolbar.Children.Add(Button("WeightGraphZoomIn", "＋", () => surface.ZoomAt(1.15, new Point(surface.Bounds.Width / 2, surface.Bounds.Height / 2))));
        zoom.Margin = new Thickness(8, 6); toolbar.Children.Add(zoom); body.Children.Add(toolbar);
        var calendar = BuildCalendarGuide(); Grid.SetRow(calendar, 1); body.Children.Add(calendar);
        var work = new Grid { Name="WeightGraphWorkArea",ColumnDefinitions = new("166,*,354"), Height = 480,MaxHeight=480,ClipToBounds=true };workArea=work;
        Grid.SetRow(work, 2); body.Children.Add(work);
        work.Children.Add(BuildPalette());
        surface.BeforeMutation = Prepare;
        surface.SelectionRequested = Select;
        surface.ConnectionRequested = Connect;
        surface.GraphChanged += () => { RefreshPorts(); Changed?.Invoke(); ShowWarnings(); };
        surface.ViewChanged += () => {zoom.Text = $"{surface.Zoom * 100:0}%";if(!framing)pendingFrame=null;};
        surface.Error += text => status.Text = text;
        surface.GroupRequested = (owner, key) => { if (!Prepare()) return; Select(owner, null); OpenWeightGroup(UmaWeightGraphModel.WeightGroups.First(g => g.Key == key)); };
        Grid.SetColumn(surface, 1); work.Children.Add(surface);
        var side = new StackPanel { Margin = new Thickness(12), Spacing = 12 };
        side.Children.Add(new TextBlock { Text = "选中节点 / 连线", FontSize = 16, FontWeight = FontWeight.SemiBold, Foreground = ForegroundBrush });
        side.Children.Add(Note("修改属性后点“应用节点属性”；保存也会检查当前编辑。"));
        side.Children.Add(properties);
        side.Children.Add(new Separator());
        side.Children.Add(new TextBlock { Text = "用按钮连接端口", Foreground = ForegroundBrush, FontWeight = FontWeight.SemiBold });
        side.Children.Add(Note("输出 → 输入；端口颜色代表数据类型。"));
        side.Children.Add(outputs); side.Children.Add(inputs);
        side.Children.Add(Button("WeightGraphConnectPorts", "创建连线", ConnectChoices));
        side.Children.Add(Button("WeightGraphDisconnect", "断开选中连线", DeleteSelection));
        side.Children.Add(Button("WeightGraphDeleteNode", "删除选中节点", DeleteSelection));
        var inspector = new ScrollViewer { Content = side, Background = PanelBrush, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        Grid.SetColumn(inspector, 2); work.Children.Add(inspector);
        var footer = new Border { Background = PanelBrush, Padding = new Thickness(12, 9), Child = status };
        Grid.SetRow(footer, 3); body.Children.Add(footer);
        Child = body;
        outputs.SelectionChanged += (_, _) => RefreshInputPorts();
        enabled.IsCheckedChanged += (_, _) =>
        {
            if (loading) return;
            if (!Prepare()) { SetEnabled(); return; }
            var candidate = (JObject)graph.DeepClone(); candidate["enabled"] = enabled.IsChecked == true;
            ApplyGraph(candidate, "节点方案启用状态已修改。");
        };
        surface.KeyDown += (_, e) => { if (e.Key is Key.Delete or Key.Back) { DeleteSelection(); e.Handled = true; } };
        viewMode.SelectionChanged += (_, _) => { if(navigating||loading)return;if(!Prepare()){SetNavigation(()=>viewMode.SelectedIndex=activeViewMode);return;}activeViewMode=viewMode.SelectedIndex;RefreshPresentation();FrameCurrentView(); };
        expandedGroups.IsCheckedChanged += (_, _) => { if(navigating||loading)return;if(!Prepare()){SetNavigation(()=>expandedGroups.IsChecked=activeExpandedGroups);return;}activeExpandedGroups=expandedGroups.IsChecked==true;RefreshPresentation();FrameCurrentView(); };
        referencePhase.SelectionChanged += (_, _) => { if(navigating||loading)return;if(!Prepare()){SetNavigation(()=>referencePhase.SelectedIndex=activeReferencePhase);return;}activeReferencePhase=referencePhase.SelectedIndex;surface.ReferencePhase=PhaseKeys[Math.Clamp(activeReferencePhase,0,PhaseKeys.Length-1)];BuildProperties();surface.InvalidateVisual(); };
        yearPicker.SelectionChanged+=(_,_)=> {
            if(navigating||yearPicker.SelectedItem is not NavigationChoice picked)return;
            if(!Prepare()){SyncSectionPickers();return;}
            var year=picked.Id=="other"?(int?)null:int.Parse(picked.Id,CultureInfo.InvariantCulture);
            var stage=UmaWeightCanvasSections.Stages(graph).FirstOrDefault(s=>s.Year==year);
            if(stage!=null)FocusStage(stage.Id);
        };
        stagePicker.SelectionChanged+=(_,_)=> {if(navigating||stagePicker.SelectedItem is not NavigationChoice picked)return;if(!Prepare()){SyncSectionPickers();return;}FocusStage(picked.Id);};
        arrangementPicker.SelectionChanged+=(_,_)=> {if(navigating||arrangementPicker.SelectedItem is not NavigationChoice picked||sectionStage==null)return;if(!Prepare()){SyncSectionPickers();return;}FocusArrangement(sectionStage,picked.Id);};
        Load(null);
    }

    private Control BuildPalette()
    {
        var palette = new StackPanel { Margin = new Thickness(12), Spacing = 10 };
        palette.Children.Add(new TextBlock { Text = "节点库", Foreground = ForegroundBrush, FontSize = 16, FontWeight = FontWeight.SemiBold });
        palette.Children.Add(Note("点击添加到画布\n拖动标题自由排版"));
        foreach (var type in new[] { "stage", "condition", "priority", "weights" }.Concat(UmaWeightGraphModel.WeightGroups.Select(g => g.Key)).Concat(new[] { "attributes", "adaptation", "rule" }))
        {
            var captured = type;
            var button = Button("WeightGraphAdd_" + type, TypeLabel(type), () => AddNode(captured));
            button.HorizontalAlignment = HorizontalAlignment.Stretch; button.BorderBrush = UmaWeightTheme.Category(button, type); button.Padding = new Thickness(9, 8); palette.Children.Add(button);
        }
        palette.Children.Add(new Separator());
        palette.Children.Add(Note("实线：阶段 / 安排顺序\n彩线：条件 / 行动 / 倍率\n\n滚轮缩放\n中键或右键拖动画布\n空白处左键也可平移\n\n位置不改变执行次序。\n每回合按连线重新检查，只采用首条成立的安排。"));
        return new ScrollViewer { Background = PanelBrush, Content = palette, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
    }

    public void Configure(JObject? value) { schema = value; surface.ReferenceSchema = value; surface.ComponentNames = value?["components"]?.OfType<JObject>().Where(c => c["key"] != null).ToDictionary(c => (string)c["key"]!, c => (string?)c["label"] ?? (string)c["key"]!) ?? new Dictionary<string, string>(); BuildProperties(); }
    public JObject Read() => UmaWeightGraphModel.Compile(ReadGraph());
    public JObject ReadGraph() { CommitProperties(); return (JObject)graph.DeepClone(); }
    public void FocusNode(string id)
    {
        if(!Prepare() || Node(id) == null)return;
        scopeRoot = null;SetNavigation(()=>viewMode.SelectedIndex=1);activeViewMode=1;RefreshPresentation(); Select(id, null); surface.FocusActualSize();
    }
    /// <summary>Enter from the simple editor without expanding all three years.</summary>
    public void FocusTimelineNode(string? stageId, string? nodeId)
    {
        if(!Prepare())return;
        if(stageId != null && Node(stageId) is JObject stage && (string?)stage["type"] == "stage") {
            var rule=nodeId==null?null:UmaWeightCanvasSections.FindArrangement(graph,stageId,nodeId);
            if(!FocusArrangement(stageId,rule??UmaWeightCanvasSections.Arrangements(graph,stageId).FirstOrDefault()?.Id))return;
            if(nodeId != null && Node(nodeId) != null) {
                if(nodeId==stageId){FocusNode(nodeId);return;}
                if(!UmaWeightCanvasSections.Dependencies(graph,sectionRule??stageId,false).Contains(nodeId)) {FocusWeights(nodeId);return;}
                surface.PinNode(nodeId); Select(nodeId,null);RequestFrame(UmaWeightCanvasSections.Dependencies(graph,sectionRule??stageId,false));
            }
            return;
        }
        if(nodeId != null)FocusNode(nodeId);
    }
    public void Load(JObject? workflow)
    {
        surface.CancelInteraction();
        readProperties = null; draftDirty = false; graph = UmaWeightGraphModel.FromWorkflow(workflow);
        sectionStage=sectionRule=scopeRoot=null;SetNavigation(()=>{viewMode.SelectedIndex=0;expandedGroups.IsChecked=false;});activeViewMode=0;activeExpandedGroups=false;
        SetEnabled(); surface.SelectedNode = (string?)graph["entry"]; surface.SelectedEdge = null;
        surface.SetScope(Nodes().Select(n => (string)n["id"]!));
        BuildProperties(); RefreshPresentation(); surface.InvalidateVisual(); ShowWarnings();
        if (StagesInOrder().FirstOrDefault() is JObject firstStage) FocusStage((string)firstStage["id"]!);else SyncSectionPickers();
    }
    public void LoadExample()
    {
        surface.CancelInteraction();
        graph = UmaWeightGraphModel.GroupedCalendar(); graph["enabled"] = false; readProperties = null; draftDirty = false;
        sectionStage=sectionRule=scopeRoot=null;SetNavigation(()=>{viewMode.SelectedIndex=0;expandedGroups.IsChecked=false;});activeViewMode=0;activeExpandedGroups=false;
        SetEnabled(); surface.SelectedNode = (string?)graph["nodes"]?.OfType<JObject>().FirstOrDefault(n => (string?)n["type"] == "rule")?["id"]; surface.SelectedEdge = null;
        BuildProperties(); RefreshPorts(); FocusYear(1); status.Text = "三年六组模板已载入，默认关闭。全年基础共享，本段可单独覆盖；启用后旧高级权重暂存。"; Changed?.Invoke();
    }
    private Control BuildCalendarGuide()
    {
        var guide=new StackPanel{Spacing=6,Margin=new Thickness(12,0,12,10)};
        guide.Children.Add(Note("先选实际时间段，再选一条安排。六组评分默认收起；切换只改变视图，不重排节点。"));
        var picks=new WrapPanel();
        foreach(var pair in new[]{("育成年份",yearPicker),("实际时间段",stagePicker),("具体安排",arrangementPicker)}) {
            var field=new StackPanel{Spacing=3,Margin=new Thickness(0,0,10,5)};field.Children.Add(Note(pair.Item1));field.Children.Add(pair.Item2);picks.Children.Add(field);
        }
        picks.Children.Add(Button("WeightGraphReturnArrangement","回到当前安排",()=>{if(sectionStage!=null)FocusArrangement(sectionStage,sectionRule);}));
        guide.Children.Add(picks);guide.Children.Add(sectionSummary);return guide;
    }
    private void SetNavigation(Action update) {var previous=navigating;navigating=true;try{update();}finally{navigating=previous;}}
    private void SyncSectionPickers()
    {
        var stages=UmaWeightCanvasSections.Stages(graph);var current=stages.FirstOrDefault(s=>s.Id==sectionStage)??stages.FirstOrDefault();
        if(current!=null)sectionStage=current.Id;
        if(sectionStage!=null&&!UmaWeightCanvasSections.Arrangements(graph,sectionStage).Any(r=>r.Id==sectionRule))sectionRule=UmaWeightCanvasSections.Arrangements(graph,sectionStage).FirstOrDefault()?.Id;
        SetNavigation(()=> {
            var years=stages.Select(s=>s.Year).Distinct().OrderBy(y=>y??4).Select(y=>new NavigationChoice(y?.ToString(CultureInfo.InvariantCulture)??"other",y.HasValue?$"第{y}年":"跨年 / 未限定")).ToArray();
            yearPicker.ItemsSource=years;yearPicker.SelectedItem=years.FirstOrDefault(y=>y.Id==(current?.Year?.ToString(CultureInfo.InvariantCulture)??"other"));yearPicker.IsEnabled=years.Length>0;
            var choices=stages.Where(s=>current==null||s.Year==current.Year).Select(s=>new NavigationChoice(s.Id,s.Label+" · "+s.Window)).ToArray();
            stagePicker.ItemsSource=choices;stagePicker.SelectedItem=choices.FirstOrDefault(s=>s.Id==sectionStage);stagePicker.IsEnabled=choices.Length>0;
            var rules=sectionStage==null?Array.Empty<UmaWeightCanvasSections.Arrangement>():UmaWeightCanvasSections.Arrangements(graph,sectionStage);
            var arrangements=rules.Select(r=>new NavigationChoice(r.Id,$"{r.Priority} · {r.Label}")).ToArray();
            arrangementPicker.ItemsSource=arrangements;arrangementPicker.SelectedItem=arrangements.FirstOrDefault(r=>r.Id==sectionRule);arrangementPicker.IsEnabled=arrangements.Length>0;
            var rule=rules.FirstOrDefault(r=>r.Id==sectionRule);
            var shared=rule!=null&&UmaWeightCanvasSections.Dependencies(graph,rule.Id).Any(id=>Edges().Any(e=>(string?)e["from"]==id&&(string?)e["input"]=="base"));
            sectionSummary.Text=current==null?"当前没有实际时间段；现有图保留，可通过全部画布查看。":current.Window+(rule==null?" · 本段尚无安排":" · "+rule.Condition+" · 同阶段采用首条匹配安排")+(shared?" · 共用基础可单独查看":"");
        });
    }
    private void FocusYear(int year)
    {
        if (!Prepare()) return;
        var stages = StagesInOrder().Where(n => StageYear(n) == year).ToArray();
        if (stages.Length == 0) { status.Text = "当前图没有这一年的时间段；可载入三年模板或添加阶段。"; return; }
        FocusStage((string)stages[0]["id"]!); status.Text = $"正在编辑第{year}年；可从实际时间段选择器切换。";
    }
    private static int? StageYear(JObject stage)
    {
        var window = stage["data"]?["window"] as JObject;
        if ((string?)window?["phase"] == "pre_debut") return 1;
        return (int?)window?["year"] ?? (int?)(window?["from"] as JObject)?["year"] ?? (int?)(window?["to"] as JObject)?["year"];
    }
    private IEnumerable<JObject> StagesInOrder()
    {
        var seen = new HashSet<string>(); var current = (string?)graph["entry"];
        while (current != null)
        {
            var next = Edges().FirstOrDefault(e => (string?)e["from"] == current && (string?)e["port"] == "next"); var id = (string?)next?["to"];
            if (id == null || !seen.Add(id) || Node(id) is not JObject stage || (string?)stage["type"] != "stage") break;
            yield return stage; current = id;
        }
        foreach (var stage in Nodes().Where(n => (string?)n["type"] == "stage" && !seen.Contains((string)n["id"]!))) yield return stage;
    }
    private void FocusStage(string id)
    {
        FocusArrangement(id,UmaWeightCanvasSections.Arrangements(graph,id).FirstOrDefault()?.Id);
    }
    private bool FocusArrangement(string stageId,string? ruleId)
    {
        if(!Prepare()||!UmaWeightCanvasSections.Stages(graph).Any(s=>s.Id==stageId))return false;
        var rules=UmaWeightCanvasSections.Arrangements(graph,stageId);if(ruleId!=null&&!rules.Any(r=>r.Id==ruleId))return false;
        scopeRoot=sectionStage=stageId;sectionRule=ruleId;
        SetNavigation(()=>viewMode.SelectedIndex=0);activeViewMode=0;
        surface.SetScope(UmaWeightCanvasSections.VisibleNodes(graph,stageId,ruleId));SyncSectionPickers();RefreshPresentation();
        Select(ruleId??stageId,null);
        var framed=ruleId==null?new HashSet<string>{stageId}:UmaWeightCanvasSections.Dependencies(graph,ruleId,false);
        RequestFrame(framed);return true;
    }
    private void RequestFrame(IEnumerable<string> ids)
    {
        pendingFrame=ids.ToArray();if(surface.Bounds.Width<10||surface.Bounds.Height<10)return;
        FrameNow(pendingFrame);
    }
    private void FrameNow(IEnumerable<string> ids)
    {
        framing=true;try{surface.FrameNodes(ids);}finally{framing=false;}
    }
    private void FrameCurrentView()
    {
        var ids=viewMode.SelectedIndex==1?Nodes().Where(n=>surface.IsNodeVisible((string)n["id"]!)).Select(n=>(string)n["id"]!)
            :scopeRoot!=null&&(string?)Node(scopeRoot)?["type"]=="stage"&&sectionRule!=null?UmaWeightCanvasSections.Dependencies(graph,sectionRule,false)
            :scopeRoot!=null?UmaWeightCanvasSections.Dependencies(graph,scopeRoot,false)
            :Nodes().Where(n=>surface.IsNodeVisible((string)n["id"]!)).Select(n=>(string)n["id"]!);
        RequestFrame(ids);
    }
    private void SetEnabled() { loading = true; enabled.IsChecked = (bool?)graph["enabled"] == true; loading = false; }
    private void FocusBaseWeights()
    {
        if (!Prepare()) return;
        var dependencies=sectionRule==null?new HashSet<string>():UmaWeightCanvasSections.Dependencies(graph,sectionRule);
        var currentBase=Edges().FirstOrDefault(e=>(string?)e["input"]=="base"&&dependencies.Contains((string)e["to"]!));
        var node = Node((string?)currentBase?["from"])??Node("base_weights");
        if (node == null) { status.Text = "当前方案尚未设置全年共用基础。可从权重节点的“基础配置”端口连接另一份权重；原方案保持不变。"; return; }
        scopeRoot = (string)node["id"]!;SetNavigation(()=>viewMode.SelectedIndex=0);activeViewMode=0;
        var ids = UmaWeightCanvasSections.Dependencies(graph,scopeRoot);
        surface.SetScope(ids); RefreshPresentation(); Select(scopeRoot, null);RequestFrame(UmaWeightCanvasSections.Dependencies(graph,scopeRoot,false));
    }
    private void RefreshPresentation()
    {
        var workHeight=viewMode.SelectedIndex==1?650:480;
        if(canvasRow!=null)canvasRow.Height=new GridLength(workHeight);
        if(workArea!=null){workArea.Height=workHeight;workArea.MaxHeight=workHeight;}
        surface.MinHeight=workHeight;MinHeight=viewMode.SelectedIndex==1?840:680;
        if (scopeRoot != null && Node(scopeRoot) != null)
        {
            var visible=(string?)Node(scopeRoot)?["type"]=="stage"
                ?UmaWeightCanvasSections.VisibleNodes(graph,scopeRoot,sectionRule)
                :UmaWeightCanvasSections.Dependencies(graph,scopeRoot,false);
            surface.SetScope(visible, true);
        }
        surface.ConfigureView(viewMode.SelectedIndex == 1, expandedGroups.IsChecked == true);
        var reset = false;
        if (surface.SelectedNode != null && !surface.IsNodeVisible(surface.SelectedNode)) { surface.SelectedNode = null; reset = true; }
        if (surface.SelectedEdge != null && !surface.IsEdgeVisible(surface.SelectedEdge)) { surface.SelectedEdge = null; reset = true; }
        if (reset) BuildProperties();
        RefreshPorts(); surface.InvalidateVisual();
    }
    private void FocusWeights(string id)
    {
        if (!Prepare()) return;
        scopeRoot = id;SetNavigation(()=>viewMode.SelectedIndex=0);activeViewMode=0;
        var ids = UmaWeightCanvasSections.Dependencies(graph,id);
        surface.SetScope(ids); RefreshPresentation(); Select(id, null);RequestFrame(UmaWeightCanvasSections.Dependencies(graph,id,false));
    }
    private IEnumerable<JObject> Nodes() => graph["nodes"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>();
    private IEnumerable<JObject> Edges() => graph["edges"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>();
    private JObject? Node(string? id) => Nodes().FirstOrDefault(n => (string?)n["id"] == id);
    private bool Prepare()
    {
        try { CommitProperties(); return true; } catch (Exception error) { status.Text = error.Message; return false; }
    }
    private void CommitProperties()
    {
        if (!draftDirty || readProperties == null || editingNode == null) return;
        var candidate = (JObject)graph.DeepClone(); var node = candidate["nodes"]!.OfType<JObject>().First(n => (string?)n["id"] == editingNode);
        readProperties(node); UmaWeightGraphModel.Compile(candidate); graph = candidate; draftDirty = false; SyncSectionPickers(); surface.InvalidateVisual(); DraftChanged?.Invoke();
    }
    private bool ApplyGraph(JObject candidate, string message)
    {
        try
        {
            UmaWeightGraphModel.Compile(candidate); graph = candidate; draftDirty = false; status.Text = message;
            SetEnabled();SyncSectionPickers(); RefreshPresentation(); surface.InvalidateVisual(); Changed?.Invoke(); return true;
        }
        catch (Exception error) { status.Text = error.Message; return false; }
    }
    private bool Select(string? node, string? edge)
    {
        if (!Prepare()) return false;
        surface.SelectedNode = node; surface.SelectedEdge = edge; BuildProperties(); surface.InvalidateVisual(); return true;
    }
    private void AddNode(string type)
    {
        if (!Prepare()) return;
        if (type == "start") { Select((string?)graph["entry"], null); status.Text = "入口节点已存在，从它的“下一阶段”端口接入阶段。"; return; }
        var point = surface.WorldAt(new Point(surface.Bounds.Width / 2, surface.Bounds.Height / 2));
        var candidate = (JObject)graph.DeepClone(); var node = UmaWeightGraphModel.NewNode(type, Math.Max(0, point.X - 110), Math.Max(0, point.Y - 60));
        if (type == "weights") node["data"] = new JObject { ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() };
        ((JArray)candidate["nodes"]!).Add(node);
        if (ApplyGraph(candidate, "已添加节点；未连接的节点作为草稿保留。")) { surface.PinNode((string)node["id"]!); RefreshPorts(); Select((string?)node["id"], null); }
    }
    private bool Connect(string from, string port, string to, string input)
    {
        if (!Prepare()) return false;
        var candidate = (JObject)graph.DeepClone(); ((JArray)candidate["edges"]!).Add(UmaWeightGraphModel.NewEdge(from, port, to, input));
        if (!ApplyGraph(candidate, "连线已创建；执行顺序与设置由端口连接决定。")) return false;
        BuildProperties(); return true;
    }
    private void DeleteSelection()
    {
        if (!Prepare()) return;
        surface.CancelInteraction();
        var candidate = (JObject)graph.DeepClone(); var nodes = (JArray)candidate["nodes"]!; var edges = (JArray)candidate["edges"]!;
        if (surface.SelectedEdge != null) edges.OfType<JObject>().FirstOrDefault(e => (string?)e["id"] == surface.SelectedEdge)?.Remove();
        else if (surface.SelectedNode != null)
        {
            if (surface.SelectedNode == (string?)graph["entry"]) { status.Text = "每回合开始是入口，请保留此节点。"; return; }
            foreach (var edge in edges.OfType<JObject>().Where(e => (string?)e["from"] == surface.SelectedNode || (string?)e["to"] == surface.SelectedNode).ToArray()) edge.Remove();
            nodes.OfType<JObject>().FirstOrDefault(n => (string?)n["id"] == surface.SelectedNode)?.Remove();
        }
        else { status.Text = "先选中节点或连线。"; return; }
        if (ApplyGraph(candidate, "已删除选中项，其他节点与连线保留。")) Select(null, null);
    }
    private void RefreshPorts()
    {
        outputChoices = Nodes().Where(n => surface.IsNodeVisible((string)n["id"]!)).SelectMany(n => UmaWeightGraphModel.OutputPorts((string)n["type"]!).Select(p => ((string)n["id"]!, p.Key))).ToArray();
        outputs.ItemsSource = outputChoices.Select(p => $"{(string?)Node(p.Node)?["label"]} · {PortLabel(p.Node, p.Port, true)}").ToArray(); outputs.SelectedIndex = outputChoices.Length > 0 ? 0 : -1;
        RefreshInputPorts();
    }
    private void RefreshInputPorts()
    {
        var selected = outputs.SelectedIndex >= 0 && outputs.SelectedIndex < outputChoices.Length ? outputChoices[outputs.SelectedIndex] : default;
        var source = Node(selected.Node); var type = source == null ? null : UmaWeightGraphModel.OutputPorts((string)source["type"]!).FirstOrDefault(p => p.Key == selected.Port)?.Type;
        inputChoices = Nodes().Where(n => surface.IsNodeVisible((string)n["id"]!)).SelectMany(n => UmaWeightGraphModel.InputPorts((string)n["type"]!).Where(p => p.Type == type && surface.IsInputVisible((string)n["id"]!, p.Key)).Select(p => ((string)n["id"]!, p.Key))).ToArray();
        inputs.ItemsSource = inputChoices.Select(p => $"{(string?)Node(p.Node)?["label"]} · {PortLabel(p.Node, p.Port, false)}").ToArray(); inputs.SelectedIndex = inputChoices.Length > 0 ? 0 : -1;
    }
    private void ConnectChoices()
    {
        if (outputs.SelectedIndex < 0 || inputs.SelectedIndex < 0) { status.Text = "请选择输出端口和相同类型的输入端口。"; return; }
        var source = outputChoices[outputs.SelectedIndex]; var target = inputChoices[inputs.SelectedIndex]; Connect(source.Node, source.Port, target.Node, target.Port);
    }
    private string PortLabel(string nodeId, string key, bool output)
    {
        var node = Node(nodeId); if (node == null) return "端口";
        var ports = output ? UmaWeightGraphModel.OutputPorts((string)node["type"]!) : UmaWeightGraphModel.InputPorts((string)node["type"]!);
        return ports.FirstOrDefault(port => port.Key == key)?.Label ?? "端口";
    }
    private void ShowWarnings()
    {
        try { var warnings = UmaWeightGraphModel.Warnings(graph); status.Text = warnings.Length == 0 ? "端口拖动或点击均可连线；点击连线后可断开。节点位置只影响排版。" : string.Join("；", warnings.Take(2)); }
        catch (Exception error) { status.Text = error.Message; }
    }

    private void BuildProperties()
    {
        properties.Children.Clear(); readProperties = null; draftDirty = false; editingNode = surface.SelectedNode;
        var node = Node(editingNode);
        if (node == null)
        {
            var edge = Edges().FirstOrDefault(e => (string?)e["id"] == surface.SelectedEdge);
            properties.Children.Add(Note(edge == null ? "选中节点后编辑属性；选中连线后可断开。" : $"{Node((string?)edge["from"])?["label"]} · {PortLabel((string)edge["from"]!, (string)edge["port"]!, true)}\n↓\n{Node((string?)edge["to"])?["label"]} · {PortLabel((string)edge["to"]!, (string)edge["input"]!, false)}")); return;
        }
        var type = (string)node["type"]!; var data = node["data"] as JObject ?? new JObject();
        var heading = new TextBlock { Text = TypeLabel(type), FontSize = 16, FontWeight = FontWeight.SemiBold }; heading.Foreground = UmaWeightTheme.Category(heading, type); properties.Children.Add(heading);
        var label = new TextBox { Name = "WeightGraphNodeLabel", Text = (string?)node["label"] }; properties.Children.Add(Labeled("节点名称", label));
        var readers = new List<Action<JObject>>();
        if (type is "stage" or "rule")
        {
            var active = new CheckBox { Name = "WeightGraphNodeEnabled", Content = "启用这个" + TypeLabel(type), IsChecked = (bool?)data["enabled"] ?? true }; properties.Children.Add(active); readers.Add(d => d["enabled"] = active.IsChecked == true);
        }
        if (type == "stage") BuildStageProperties(data, readers);
        if (type == "condition") BuildConditionProperties(data, readers);
        if (type == "priority") BuildPriorityProperties(data, readers);
        if (type == "weights") BuildWeightProperties(data, readers);
        if (UmaWeightGraphModel.WeightGroups.FirstOrDefault(g => g.Key == type) is WeightGroup group) BuildGroupProperties(group, data, readers);
        if (type == "attributes") BuildAttributeProperties(data, readers);
        if (type == "adaptation") BuildAdaptationProperties(data, readers);
        if (type == "rule") properties.Children.Add(Note("“安排顺序”接入前一安排；条件、行动、训练类型倍率、属性收益和可选适应性安排可分别接入。未接项目沿当前策略。"));
        if (type == "start") properties.Children.Add(Note("每个回合重新从这个入口开始。“下一阶段”接入首个育成阶段；每回合都会重新检查。"));
        readProperties = target => { target["label"] = label.Text?.Trim() ?? ""; var value = target["data"] is JObject existing ? (JObject)existing.DeepClone() : new JObject(); foreach (var reader in readers) reader(value); target["data"] = value; };
        properties.Children.Add(Button("WeightGraphApplyProperties", "应用节点属性", () => { if (Prepare()) { RefreshPorts(); surface.InvalidateVisual(); Changed?.Invoke(); status.Text = "节点属性已应用，连线保留。"; } }));
        foreach (var control in properties.GetLogicalDescendants().OfType<Control>().Where(c => c is TextBox or ComboBox or CheckBox or NumericUpDown)) control.PropertyChanged += (_, e) => { if (e.Property.Name is "Text" or "SelectedIndex" or "IsChecked" or "Value") MarkDraftDirty(); };
    }
    private void BuildStageProperties(JObject data, List<Action<JObject>> readers)
    {
        var sourceRule = (string?)Edges().FirstOrDefault(e => (string?)e["from"] == editingNode && (string?)e["port"] == "rules")?["to"];
        if (sourceRule != null) properties.Children.Add(Note("本阶段的权重安排：点击一段，集中查看它的六组积木。"));
        var seenRules = new HashSet<string>();
        while (sourceRule != null && seenRules.Add(sourceRule))
        {
            var rule = Node(sourceRule); var weightsId = (string?)Edges().FirstOrDefault(e => (string?)e["to"] == sourceRule && (string?)e["input"] == "weights")?["from"];
            if (weightsId != null)
            {
                var target = weightsId;
                properties.Children.Add(Button("WeightGraphEditSegment_" + sourceRule, "编辑：" + (string?)rule?["label"], () => FocusWeights(target)));
            }
            sourceRule = (string?)Edges().FirstOrDefault(e => (string?)e["from"] == sourceRule && (string?)e["port"] == "next")?["to"];
        }
        var window = data["window"] as JObject ?? new JObject();
        var phase = Choice("WeightGraphStagePhase", new[] { "整个育成", "出道前", "日期区间", "第一年（含出道前）", "第二年", "第三年" }, (string?)window["phase"] switch { "pre_debut" => 1, "dated" => 2, "year" => 2 + ((int?)window["year"] ?? 1), _ => 0 }); properties.Children.Add(Labeled("生效阶段", phase));
        var from = new DateFields("WeightGraphFrom", window["from"]); var to = new DateFields("WeightGraphTo", window["to"]);
        var dates = new StackPanel { Spacing = 10 }; dates.Children.Add(Labeled("起始（包含）", from.Root)); dates.Children.Add(Labeled("结束（包含）", to.Root));
        void RefreshDates() { dates.IsVisible = phase.SelectedIndex >= 2; var year = phase.SelectedIndex >= 3 ? phase.SelectedIndex - 2 : (int?)null; from.SetYear(year); to.SetYear(year); }
        RefreshDates(); phase.SelectionChanged += (_, _) => RefreshDates(); properties.Children.Add(dates);
        properties.Children.Add(Note("全年不限定两端；第一年没有起始日期下界时包含出道前。第二、三年7—9月为远征。"));
        readers.Add(d => { var value = new JObject { ["phase"] = phase.SelectedIndex switch { 1 => "pre_debut", 2 => "dated", >= 3 => "year", _ => "all" }, ["from"] = phase.SelectedIndex >= 2 ? from.Read() : null, ["to"] = phase.SelectedIndex >= 2 ? to.Read() : null }; if (phase.SelectedIndex >= 3) value["year"] = phase.SelectedIndex - 2; d["window"] = value; });
        var cutYear = Numeric("WeightGraphSplitYear", (int?)window["year"] ?? (int?)(window["from"] as JObject)?["year"] ?? 2, 1, 3);
        var middleMonth = (((int?)(window["from"] as JObject)?["month"] ?? 1) + ((int?)(window["to"] as JObject)?["month"] ?? 12) + 1) / 2;
        var cutMonth = Numeric("WeightGraphSplitMonth", middleMonth, 1, 12); var cutHalf = Choice("WeightGraphSplitHalf", new[] { "前半开始", "后半开始" }, 0);
        var cut = new Grid { ColumnDefinitions = new("52,52,*"), ColumnSpacing = 5 }; cut.Children.Add(cutYear); Grid.SetColumn(cutMonth, 1); cut.Children.Add(cutMonth); Grid.SetColumn(cutHalf, 2); cut.Children.Add(cutHalf);
        var splitPanel = new StackPanel { Spacing = 7 }; splitPanel.Children.Add(Note("从这个日期开始拆成新时间段。两段各有独立的安排副本，可分别改权重。")); splitPanel.Children.Add(cut);
        splitPanel.Children.Add(Button("WeightGraphSplitStage", "在此日期切分时间段", () => { if (!Prepare() || editingNode == null) return; try { var sourceId = editingNode; var result = UmaWeightGraphModel.SplitStage(graph, sourceId, (int)(cutYear.Value ?? 2), (int)(cutMonth.Value ?? 7), cutHalf.SelectedIndex == 1 ? "second" : "first"); var rightId = (string)result["edges"]!.OfType<JObject>().First(e => (string?)e["from"] == sourceId && (string?)e["port"] == "next")["to"]!; if (ApplyGraph(result, "已切分成相邻时间段；当前定位新段，可独立调整。")) FocusStage(rightId); } catch(Exception error) { status.Text = error.Message; } }));
        properties.Children.Add(new Expander { Name = "WeightGraphSplitSection", Header = "继续切分这个时间段", Content = splitPanel, IsExpanded = false, HorizontalContentAlignment = HorizontalAlignment.Stretch });
    }
    private void BuildConditionProperties(JObject data, List<Action<JObject>> readers)
    {
        var when = data["when"] as JObject ?? new JObject(); var mode = Choice("WeightGraphConditionMode", new[] { "全部满足（并且）", "任一满足（或者）" }, when["any"] is JArray ? 1 : 0); properties.Children.Add(mode);
        var rows = new StackPanel { Spacing = 10 }; var entries = new List<ConditionFields>();
        void Add(JObject value) { var entry = new ConditionFields(value); entries.Add(entry); rows.Children.Add(entry.Root); entry.Remove.Click += (_, _) => { rows.Children.Remove(entry.Root); entries.Remove(entry); MarkDraftDirty(); }; foreach (var c in entry.Root.GetLogicalDescendants().OfType<Control>().Where(c => c is TextBox or ComboBox)) c.PropertyChanged += (_, e) => { if (e.Property.Name is "Text" or "SelectedIndex") MarkDraftDirty(); }; }
        foreach (var value in when[mode.SelectedIndex == 1 ? "any" : "all"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>()) Add(value);
        properties.Children.Add(rows); properties.Children.Add(Button("WeightGraphAddCondition", "＋条件", () => { Add(new JObject { ["field"] = "vitality", ["op"] = "lt", ["value"] = 40 }); MarkDraftDirty(); }));
        properties.Children.Add(Note("空条件表示总是；所需数值未知时跳过这条安排，继续找下一条。"));
        readers.Add(d => d["when"] = new JObject { [mode.SelectedIndex == 1 ? "any" : "all"] = new JArray(entries.Select(e => e.Read())) });
    }
    private void BuildPriorityProperties(JObject data, List<Action<JObject>> readers)
    {
        var order = data["preferred_actions"]?.Values<string>().Cast<string>().ToList() ?? new List<string>();
        foreach (var action in ActionKeys) if (!order.Contains(action)) order.Add(action);
        var selected = new HashSet<string>(data["preferred_actions"]?.Values<string>().Cast<string>() ?? Enumerable.Empty<string>());
        var items = new StackPanel { Spacing = 6 };
        void Render()
        {
            items.Children.Clear(); foreach (var action in order.ToArray())
            {
                var row = new Grid { ColumnDefinitions = new("*,Auto,Auto"), ColumnSpacing = 4 }; var use = new CheckBox { Content = ActionLabels[Array.IndexOf(ActionKeys, action)], IsChecked = selected.Contains(action) };
                use.IsCheckedChanged += (_, _) => { if (use.IsChecked == true) selected.Add(action); else selected.Remove(action); MarkDraftDirty(); }; row.Children.Add(use);
                var up = Button("WeightGraphPriorityUp_" + action, "↑", () => { var index = order.IndexOf(action); if (index > 0) { order.RemoveAt(index); order.Insert(index - 1, action); MarkDraftDirty(); Render(); } });
                var down = Button("WeightGraphPriorityDown_" + action, "↓", () => { var index = order.IndexOf(action); if (index < order.Count - 1) { order.RemoveAt(index); order.Insert(index + 1, action); MarkDraftDirty(); Render(); } }); Grid.SetColumn(up, 1); Grid.SetColumn(down, 2); row.Children.Add(up); row.Children.Add(down); items.Children.Add(row);
            }
        }
        Render(); properties.Children.Add(items); properties.Children.Add(Note("勾选类别按从上到下比较；同类按分数选。未列类别并列放在后面，原不可执行动作不会被解禁。"));
        readers.Add(d => d["preferred_actions"] = new JArray(order.Where(selected.Contains)));
    }
    private void BuildWeightProperties(JObject data, List<Action<JObject>> readers)
    {
        var baseEdge = Edges().FirstOrDefault(e => (string?)e["to"] == editingNode && (string?)e["input"] == "base");
        var baseId = (string?)baseEdge?["from"];
        var inherited = baseId == null ? new JObject() : UmaWeightGraphModel.ResolveWeights(graph, baseId);
        properties.Children.Add(Note(baseId == null ? "基础来源：原策略与基础取舍。×1沿用各阶段原评分，并不表示各项同权。" : $"基础来源：{Node(baseId)?["label"]}（共用）。未覆盖项随这份基础一起变化。"));
        if (baseId != null) properties.Children.Add(Button("WeightGraphInspectBase", "查看这份共用基础", () => FocusWeights(baseId)));
        var groups = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var group in UmaWeightGraphModel.WeightGroups)
        {
            var captured = group;
            groups.Children.Add(Button("WeightGraphOpen_" + group.Key, group.Label, () => OpenWeightGroup(captured)));
        }
        properties.Children.Add(groups);
        properties.Children.Add(Note("点上面的分组直接编辑；缺少积木时会添加并连好。本段分组优先于此节点保留的旧分项。"));
        var training = new Dictionary<string, FactorField>();
        foreach (var pair in StatKeys.Select((key, index) => (key, index)))
        {
            var field = new FactorField("WeightGraphMultiplier_" + pair.key, data["training_multipliers"]?[pair.key], (decimal?)inherited["training_multipliers"]?[pair.key] ?? 1, "训练类别最终分倍率"); training[pair.key] = field;
            properties.Children.Add(Labeled(StatLabels[pair.index] + "训练", field.Root));
        }
        properties.Children.Add(Note("训练类别倍率在分项评分之后计算，会同时缩放收益与扣分。×0将分数置零，仍可能被优先行动规则选中。"));
        var components = new Dictionary<string, FactorField>(); var componentPanel = new StackPanel { Spacing = 8 };
        foreach (var component in schema?["components"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>())
        {
            var key = (string?)component["key"]; if (key == null) continue; var reference = ReferenceFor(key); var value = new FactorField("WeightGraphComponent_" + key, data["component_factors"]?[key], (decimal?)inherited["component_factors"]?[key] ?? 1, (string?)component["description"] ?? "", reference.Text, reference.Coefficient); components[key] = value; componentPanel.Children.Add(Labeled((string?)component["label"] ?? key, value.Root));
        }
        properties.Children.Add(new Expander { Header = "高级：本节点保留的直接分项", Content = componentPanel, IsExpanded = false, HorizontalContentAlignment = HorizontalAlignment.Stretch });
        readers.Add(d =>
        {
            var multipliers = new JObject();
            foreach (var pair in training)
            {
                if (pair.Value.Read() is JToken factor) multipliers[pair.Key] = factor;
            }
            d["training_multipliers"] = multipliers;
            var factors = d["component_factors"] is JObject existing ? (JObject)existing.DeepClone() : new JObject();
            foreach (var pair in components)
            {
                if (pair.Value.Read() is JToken factor) factors[pair.Key] = factor; else factors.Remove(pair.Key);
            }
            d["component_factors"] = factors;
        });
    }
    private void MarkDraftDirty() { draftDirty = true; DraftChanged?.Invoke(); }
    private void OpenWeightGroup(WeightGroup group)
    {
        if (!Prepare() || editingNode == null) return;
        var owner = editingNode; groupOwner = owner; var linked = Edges().FirstOrDefault(e => (string?)e["to"] == owner && (string?)e["input"] == group.Key);
        if (linked != null) { surface.PinNode((string)linked["from"]!); RefreshPorts(); Select((string?)linked["from"], null); surface.FrameNodes(new[] { owner, (string)linked["from"]! }); return; }
        var candidate = (JObject)graph.DeepClone(); var parent = Node(owner)!;
        var created = UmaWeightGraphModel.NewNode(group.Key, (double)parent["x"]! - 320, (double)parent["y"]!);
        ((JArray)candidate["nodes"]!).Add(created); ((JArray)candidate["edges"]!).Add(UmaWeightGraphModel.NewEdge((string)created["id"]!, "value", owner, group.Key));
        if (ApplyGraph(candidate, "已添加并连接分组，基准数值已展示。")) { surface.PinNode((string)created["id"]!); RefreshPorts(); Select((string?)created["id"], null); surface.FrameNodes(new[] { owner, (string)created["id"]! }); }
    }
    private void BuildGroupProperties(WeightGroup group, JObject data, List<Action<JObject>> readers)
    {
        var owners = Edges().Where(e => (string?)e["from"] == editingNode && (string?)e["input"] == group.Key).Select(e => Node((string)e["to"]!)).Where(n => n != null).Cast<JObject>().ToArray();
        var inherited = new JObject();
        if (owners.Length > 0)
        {
            var owner = owners.FirstOrDefault(n => (string?)n["id"] == groupOwner) ?? owners[0]; var ownerId = (string)owner["id"]!;
            if (owners.Length > 1)
            {
                var context = Choice("WeightGraphGroupOwner", owners.Select(n => (string?)n["label"] ?? (string)n["id"]!).ToArray(), Array.IndexOf(owners, owner));
                context.SelectionChanged += (_, _) => { if (!Prepare() || context.SelectedIndex < 0) return; groupOwner = (string)owners[context.SelectedIndex]["id"]!; BuildProperties(); };
                properties.Children.Add(Labeled("该积木为多处共用；选择继承值参考来源", context));
            }
            var baseId = (string?)Edges().FirstOrDefault(e => (string?)e["to"] == ownerId && (string?)e["input"] == "base")?["from"];
            if (baseId != null) inherited = (JObject?)UmaWeightGraphModel.ResolveWeights(graph, baseId)["component_factors"] ?? new JObject();
            foreach (var property in (owner["data"]?["component_factors"] as JObject)?.Properties() ?? Enumerable.Empty<JProperty>()) inherited[property.Name] = property.Value.DeepClone();
            properties.Children.Add(Note("连接到：" + string.Join("、", owners.Select(n => (string?)n["label"])) + (owners.Length > 1 ? $"。修改影响全部连接；当前参考 {owner["label"]}。" : "。可直接改数值，重置恢复共用值。")));
            properties.Children.Add(Button("WeightGraphReturnWeights", "收起分组，返回汇总", () => FocusWeights(ownerId)));
        }
        else properties.Children.Add(Note("这块尚未连接，作为草稿保留；接入权重汇总的同名端口后参与评分。"));
        properties.Children.Add(Note("下面直接列出原策略的系数或公式。调整倍率可直接填写；负分项×2会扣得更多，原贡献为0时倍率无法激活它。"));
        var fields = new Dictionary<string, FactorField>();
        foreach (var key in group.Components)
        {
            var definition = schema?["components"]?.OfType<JObject>().FirstOrDefault(c => (string?)c["key"] == key);
            var reference = ReferenceFor(key);
            var field = new FactorField("WeightGraphComponent_" + key, data["component_factors"]?[key], (decimal?)inherited[key] ?? 1, (string?)definition?["description"] ?? "随当前阶段和候选动作计算；在示例得分中查看实际贡献。", reference.Text, reference.Coefficient);
            fields[key] = field; properties.Children.Add(Labeled((string?)definition?["label"] ?? key, field.Root));
        }
        readers.Add(d => { var factors = new JObject(); foreach (var pair in fields) if (pair.Value.Read() is JToken factor) factors[pair.Key] = factor; d["component_factors"] = factors; });
    }
    private (string Text, decimal? Coefficient) ReferenceFor(string key)
    {
        var reference = schema?["components"]?.OfType<JObject>().FirstOrDefault(c => (string?)c["key"] == key)?["reference"] as JObject;
        if (reference == null) return ("原策略参考信息尚未加载；倍率基准为1。", null);
        var phase = PhaseKeys[Math.Clamp(referencePhase.SelectedIndex, 0, PhaseKeys.Length - 1)];
        var coefficient = (decimal?)reference["phase_values"]?[phase];
        var text = coefficient.HasValue ? $"原策略基准：{coefficient:0.###} {reference["unit"]}（{referencePhase.SelectedItem}）\n" : "原策略公式：";
        text += (string?)reference["summary"] ?? "随状态计算。";
        var parameters = reference["parameters"]?.OfType<JObject>().Select(p => $"{p["label"]} {p["value"]}{p["unit"]}").ToArray() ?? Array.Empty<string>();
        if (parameters.Length > 0) text += "\n参数：" + string.Join("；", parameters);
        return (text, coefficient);
    }
    private sealed class FactorField
    {
        public StackPanel Root { get; } = new() { Spacing = 5 };
        private readonly ComboBox mode;
        private readonly NumericUpDown number;
        private readonly JToken? original;
        public FactorField(string name, JToken? value, decimal inherited, string explanation, string? reference = null, decimal? coefficient = null)
        {
            original = value?.DeepClone();
            mode = Choice(name + "Mode", new[] { "沿用基础", "本段覆盖" }, value == null ? 0 : 1);
            number = Numeric(name, (decimal?)value ?? inherited, 0, 10, .1m);
            var result = Note(""); var updating = false;
            void Refresh() { var factor = mode.SelectedIndex == 1 ? number.Value ?? 1 : inherited; result.Text = $"共用倍率 {inherited:0.###} · 当前倍率 {factor:0.###}" + (mode.SelectedIndex == 1 ? "（已修改）" : "（未修改）") + (coefficient.HasValue ? $"\n原系数 {coefficient:0.###} × {factor:0.###} = {coefficient * factor:0.###}（基础取舍及训练总倍率前）" : ""); }
            mode.IsVisible = false; mode.SelectionChanged += (_, _) => Refresh();
            number.ValueChanged += (_, _) => { if (!updating) mode.SelectedIndex = 1; Refresh(); };
            var reset = Button(name + "Reset", "重置", () => { updating = true; mode.SelectedIndex = 0; number.Value = inherited; updating = false; Refresh(); });
            ToolTip.SetTip(reset, "恢复共用倍率，不再添加本段覆盖。");
            var row = new Grid { ColumnDefinitions = new("*,90,Auto"), ColumnSpacing = 5 }; row.Children.Add(Note("调整倍率")); Grid.SetColumn(number, 1); row.Children.Add(number); Grid.SetColumn(reset, 2); row.Children.Add(reset);
            if (reference != null) Root.Children.Add(ReferenceCard(reference));
            Root.Children.Add(mode); Root.Children.Add(row); Root.Children.Add(result); Root.Children.Add(Note(explanation)); Refresh();
        }
        public JToken? Read() => mode.SelectedIndex != 1 ? null : original != null && number.Value == (decimal?)original ? original.DeepClone() : new JValue(number.Value ?? 1);
    }
    private void BuildAttributeProperties(JObject data, List<Action<JObject>> readers)
    {
        properties.Children.Add(ReferenceCard("原策略基准：速度1、耐力1、力量1、毅力1、智力1。\n" + ((string?)schema?["attribute_defaults"]?["summary"] ?? "达标后折扣随当前策略；留空继续使用实际设置。")));
        var values = new Dictionary<string, TextBox>();
        foreach (var pair in StatKeys.Select((key, index) => (key, index)))
        {
            var box = new TextBox { Name = "WeightGraphAttribute_" + pair.key, Text = data["attribute_weights"]?[pair.key]?.ToString(Newtonsoft.Json.Formatting.None) ?? "", Watermark = "沿当前策略" }; values[pair.key] = box;
            properties.Children.Add(Labeled(StatLabels[pair.index] + "收益权重", box));
        }
        var credit = new TextBox { Name = "WeightGraphAfterTargetCredit", Text = data["after_target_credit"]?.ToString(Newtonsoft.Json.Formatting.None) ?? "", Watermark = "沿当前策略" }; properties.Children.Add(Labeled("达标后收益保留比例（可选）", credit));
        properties.Children.Add(Note("任意训练、SS等行动获得这项属性时，都会使用该属性权重。留空沿当前策略；与“训练类型倍率”分别计算。属性目标仍是偏好，不是硬上限。"));
        readers.Add(d => { var weights = new JObject(); foreach (var pair in values) if (!string.IsNullOrWhiteSpace(pair.Value.Text)) weights[pair.Key] = PositiveNumber(pair.Value.Text!, 10, "属性收益权重"); d["attribute_weights"] = weights; if (string.IsNullOrWhiteSpace(credit.Text)) d.Remove("after_target_credit"); else d["after_target_credit"] = PositiveNumber(credit.Text!, 1, "达标后收益保留比例"); });
    }
    private static JToken PositiveNumber(string text, double maximum, string label)
    {
        if (!double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) || !double.IsFinite(value) || value <= 0 || value > maximum) throw new InvalidOperationException($"{label}请输入大于0、不超过{maximum}的有效数值。");
        var token = JToken.Parse(text); if (token.Type is not (JTokenType.Integer or JTokenType.Float)) throw new InvalidOperationException(label + "请输入有效数字。"); return token;
    }
    private void BuildAdaptationProperties(JObject data, List<Action<JObject>> readers)
    {
        var mode = Choice("WeightGraphAdaptationMode", new[] { "按当前策略自动购买", "优先指定升级", "暂缓可选成长升级" }, (string?)data["mode"] switch { "prefer" => 1, "defer" => 2, _ => 0 }); properties.Children.Add(Labeled("可选适应性购买", mode));
        var reserve = new TextBox { Name = "WeightGraphAdaptationReserve", Text = data["reserve_points"]?.ToString(Newtonsoft.Json.Formatting.None) ?? "0" }; properties.Children.Add(Labeled("购买后至少保留适应性Pt", reserve));
        var order = data["priorities"]?.Values<string>().Cast<string>().ToList() ?? new List<string>(); foreach (var phase in AdaptationKeys) if (!order.Contains(phase)) order.Add(phase);
        var selected = new HashSet<string>(data["priorities"]?.Values<string>().Cast<string>() ?? Enumerable.Empty<string>()); var items = new StackPanel { Spacing = 6 };
        void Render()
        {
            items.Children.Clear(); foreach (var phase in order.ToArray())
            {
                var row = new Grid { ColumnDefinitions = new("*,Auto,Auto"), ColumnSpacing = 3 }; var use = new CheckBox { Name = "WeightGraphAdaptation_" + phase, Content = AdaptationLabels[Array.IndexOf(AdaptationKeys, phase)], IsChecked = selected.Contains(phase) };
                use.IsCheckedChanged += (_, _) => { if (use.IsChecked == true) selected.Add(phase); else selected.Remove(phase); MarkDraftDirty(); }; row.Children.Add(use);
                var up = Button("WeightGraphAdaptationUp_" + phase, "↑", () => { var index = order.IndexOf(phase); if (index > 0) { order.RemoveAt(index); order.Insert(index - 1, phase); MarkDraftDirty(); Render(); } }); var down = Button("WeightGraphAdaptationDown_" + phase, "↓", () => { var index = order.IndexOf(phase); if (index < order.Count - 1) { order.RemoveAt(index); order.Insert(index + 1, phase); MarkDraftDirty(); Render(); } }); Grid.SetColumn(up, 1); row.Children.Add(up); Grid.SetColumn(down, 2); row.Children.Add(down); items.Children.Add(row);
            }
        }
        Render(); properties.Children.Add(items); properties.Children.Add(Note("勾选的项目从上到下优先考虑，采用已有3级升级方案。只影响当前已解锁、可支付的可选成长购买；必要参赛准备继续按原流程。第一年或尚未开放时暂不适用。"));
        readers.Add(d => { JToken amount; try { amount = JToken.Parse(reserve.Text ?? "0"); } catch { throw new InvalidOperationException("保留适应性Pt请输入非负整数。"); } if (amount.Type != JTokenType.Integer || (double)amount < 0) throw new InvalidOperationException("保留适应性Pt请输入非负整数。"); d["mode"] = mode.SelectedIndex switch { 1 => "prefer", 2 => "defer", _ => "auto" }; d["priorities"] = new JArray(order.Where(selected.Contains)); d["reserve_points"] = amount; });
    }

    private static readonly string[] StatKeys = { "speed", "stamina", "power", "guts", "wisdom" };
    private static readonly string[] StatLabels = { "速度", "耐力", "力量", "毅力", "智力" };
    private static readonly string[] ActionKeys = { "train", "ss", "rest", "outing" };
    private static readonly string[] ActionLabels = { "训练", "SS 对抗", "休息", "外出" };
    private static readonly string[] ConditionKeys = { "vitality", "speed", "stamina", "power", "guts", "wisdom", "skill_pt", "calendar_year", "calendar_month", "calendar_turn", "remaining_turns" };
    private static readonly string[] ConditionLabels = { "体力", "速度", "耐力", "力量", "毅力", "智力", "技能 Pt", "游戏年份（1／2／3）", "游戏月份", "日历回合（日期换算）", "距当前目标剩余回合" };
    private static readonly string[] AdaptationKeys = { "guts_expedition", "stamina_expedition", "power_expedition", "speed_expedition", "french_three", "expedition_skill_pt", "courage_three", "willpower_three" };
    private static readonly string[] AdaptationLabels = { "海外草地 · 毅力", "隆尚适应性 · 耐力", "生活节奏 · 力量", "营养管理 · 速度", "法语能力 · 智力", "海外远征 · 技能Pt", "胆魄", "意志力" };
    private static readonly string[] OpKeys = { "lt", "lte", "eq", "gte", "gt" };
    private static readonly string[] OpLabels = { "低于", "不高于", "等于", "不低于", "高于" };
    public static string TypeLabel(string type) => type switch { "start" => "每回合开始", "stage" => "游戏时间段", "condition" => "条件／回合", "priority" => "优先行动", "weights" => "权重汇总", "attributes" => "属性收益权重", "adaptation" => "适应性购买安排", "rule" => "安排输出", _ => UmaWeightGraphModel.WeightGroups.FirstOrDefault(g => g.Key == type)?.Label ?? type };
    private static TextBlock Note(string text) { var note = new TextBlock { Text = text, FontSize = 12, TextWrapping = TextWrapping.Wrap }; note.Foreground = UmaWeightTheme.Brush(note, "muted"); return note; }
    private static Border ReferenceCard(string text) { var card = new Border { Padding = new Thickness(8), CornerRadius = new CornerRadius(5), Child = Note(text) }; card.Background = UmaWeightTheme.Brush(card, "reference"); return card; }
    private static Button Button(string name, string text, Action action) { var button = new Button { Name = name, Content = text, FontSize = 14, Height=32,MinHeight=0, Padding = new Thickness(10, 5), Margin = new Thickness(0, 0, 5, 0) }; button.Click += (_, _) => action(); return button; }
    private static Control Labeled(string label, Control child) { var stack = new StackPanel { Spacing = 5 }; stack.Children.Add(Note(label)); stack.Children.Add(child); return stack; }
    private static ComboBox Choice(string name, string[] options, int value) => new() { Name = name, ItemsSource = options, SelectedIndex = value,Height=34,MinHeight=0,FontSize=14, HorizontalAlignment = HorizontalAlignment.Stretch };
    private static NumericUpDown Numeric(string name, decimal value, decimal min, decimal max, decimal step = 1) => new() { Name = name, Minimum = min, Maximum = max, Value = Math.Clamp(value, min, max), Increment = step, ShowButtonSpinner = false, FormatString = "0.############################" };
    private sealed class DateFields
    {
        public StackPanel Root { get; } = new() { Spacing = 5 };
        private readonly CheckBox open;
        private readonly NumericUpDown year;
        private readonly NumericUpDown month;
        private readonly ComboBox half;
        public DateFields(string name, JToken? token)
        {
            var date = token as JObject; open = new CheckBox { Name = name + "Open", Content = "不限定这一端", IsChecked = date == null }; year = Numeric(name + "Year", (decimal?)date?["year"] ?? 1, 1, 3); month = Numeric(name + "Month", (decimal?)date?["month"] ?? 1, 1, 12); half = Choice(name + "Half", new[] { "前半", "后半" }, (string?)date?["half"] == "second" ? 1 : 0);
            var row = new Grid { ColumnDefinitions = new("58,58,*"), ColumnSpacing = 5 }; row.Children.Add(year); Grid.SetColumn(month, 1); row.Children.Add(month); Grid.SetColumn(half, 2); row.Children.Add(half); row.IsEnabled = date != null; open.IsCheckedChanged += (_, _) => row.IsEnabled = open.IsChecked != true; Root.Children.Add(open); Root.Children.Add(row);
        }
        public JObject? Read() => open.IsChecked == true ? null : new JObject { ["year"] = (int)(year.Value ?? 1), ["month"] = (int)(month.Value ?? 1), ["half"] = half.SelectedIndex == 1 ? "second" : "first" };
        public void SetYear(int? value) { if (value.HasValue) year.Value = value.Value; year.IsEnabled = !value.HasValue; }
    }
    private sealed class ConditionFields
    {
        public StackPanel Root { get; } = new() { Spacing = 5 };
        public Button Remove { get; } = new() { Content = "删除条件", FontSize = 11 };
        private readonly ComboBox field;
        private readonly ComboBox op;
        private readonly TextBox value;
        public ConditionFields(JObject condition)
        {
            field = Choice("WeightGraphConditionField", ConditionLabels, Math.Max(0, Array.IndexOf(ConditionKeys, (string?)condition["field"]))); op = Choice("WeightGraphConditionOperator", OpLabels, Math.Max(0, Array.IndexOf(OpKeys, (string?)condition["op"]))); value = new TextBox { Name = "WeightGraphConditionValue", Text = condition["value"]?.ToString(Newtonsoft.Json.Formatting.None) ?? "40" };
            var row = new Grid { ColumnDefinitions = new("*,*"), ColumnSpacing = 5 }; row.Children.Add(field); Grid.SetColumn(op, 1); row.Children.Add(op); Root.Children.Add(row); Root.Children.Add(value); Root.Children.Add(Remove);
        }
        public JObject Read()
        {
            if (!double.TryParse(value.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out var number) || !double.IsFinite(number) || number < 0 || number > 100000) throw new InvalidOperationException("条件阈值请输入0～100000的有效数值。");
            var token = JToken.Parse(value.Text!); if (field.SelectedIndex >= 7) { if (number != Math.Truncate(number)) throw new InvalidOperationException("游戏日期和剩余回合条件请输入整数。"); token = new JValue((int)number); } return new JObject { ["field"] = ConditionKeys[field.SelectedIndex], ["op"] = OpKeys[op.SelectedIndex], ["value"] = token };
        }
    }

    /// <summary>Actual Avalonia drawing and pointer interaction surface.</summary>
    public sealed class GraphSurface : Control
    {
        private const double NodeWidth = 254;
        private const double HeaderHeight = 35;
        private const double PortStep = 25;
        private readonly Func<JObject> graph;
        private Point offset = new(24, 40);
        private Point lastPointer;
        private Point? panOrigin;
        private string? draggingNode;
        private Point dragOrigin;
        private Point originalNode;
        private bool moved;
        private (string Node, string Port)? wire;
        private bool wireDragged;
        private HashSet<string>? scope;
        private readonly HashSet<string> pins = new();
        private bool allNodes;
        private bool expandGroups;
        private string? hoveredNode;
        private readonly Dictionary<string, IBrush> themeBrushes = new();
        private IBrush Themed(string role)
        {
            var key = "role:" + role; if (!themeBrushes.TryGetValue(key, out var brush)) themeBrushes[key] = brush = UmaWeightTheme.Brush(this, role); return brush;
        }
        private IBrush CategoryBrush(string type)
        {
            var key = "category:" + type; if (!themeBrushes.TryGetValue(key, out var brush)) themeBrushes[key] = brush = UmaWeightTheme.Category(this, type); return brush;
        }
        private IBrush ForegroundBrush => Themed("text");
        private IBrush MutedBrush => Themed("muted");
        public double Zoom { get; private set; } = .8;
        public bool Overview => Zoom < .65;
        public double TitleFontSize => Math.Max(11, 14 * Zoom);
        private double ScreenHeader => Math.Max(24, HeaderHeight * Zoom);
        public string ReferencePhase { get; set; } = "junior_early";
        public JObject? ReferenceSchema { get; set; }
        public int VisibleNodeCount => VisibleNodes.Count();
        public int VisibleEdgeCount => VisibleEdges.Count();
        public string? SelectedNode { get; set; }
        public string? SelectedEdge { get; set; }
        public Func<bool>? BeforeMutation { get; set; }
        public Func<string?, string?, bool>? SelectionRequested { get; set; }
        public Func<string, string, string, string, bool>? ConnectionRequested { get; set; }
        public Action<string, string>? GroupRequested { get; set; }
        public event Action? GraphChanged;
        public event Action? ViewChanged;
        public event Action<string>? Error;
        public IReadOnlyDictionary<string, string> ComponentNames { get; set; } = new Dictionary<string, string>();
        public GraphSurface(Func<JObject> source)
        {
            // Canvas focus must not scroll the calendar out of its outer viewport.
            AddHandler(Control.RequestBringIntoViewEvent, (_, e) => e.Handled = true);
            graph = source; ClipToBounds = true;
            PointerPressed += Pressed; PointerMoved += Moved; PointerReleased += Released;
            PointerWheelChanged += (_, e) => { ZoomAt(Math.Pow(1.12, e.Delta.Y), e.GetPosition(this)); e.Handled = true; };
        }
        public void CancelInteraction()
        {
            wire = null; wireDragged = false; draggingNode = null; panOrigin = null; InvalidateVisual();
        }
        private IEnumerable<JObject> Nodes => graph()["nodes"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>();
        private IEnumerable<JObject> Edges => graph()["edges"]?.OfType<JObject>() ?? Enumerable.Empty<JObject>();
        private IEnumerable<JObject> VisibleNodes => Nodes.Where(n => IsNodeVisible((string)n["id"]!));
        private IEnumerable<JObject> VisibleEdges => Edges.Where(e => IsEdgeVisible((string)e["id"]!));
        private JObject? Node(string id) => Nodes.FirstOrDefault(n => (string?)n["id"] == id);
        private static bool IsGroup(string type) => UmaWeightGraphModel.WeightGroups.Any(g => g.Key == type);
        public void SetScope(IEnumerable<string> identifiers, bool preservePins = false) { scope = new HashSet<string>(identifiers); if (!preservePins) pins.Clear(); CancelInteraction(); }
        public void PinNode(string id) { pins.Add(id); InvalidateVisual(); }
        public void ConfigureView(bool all, bool expanded) { if (all != allNodes || expanded != expandGroups) pins.Clear(); allNodes = all; expandGroups = expanded; CancelInteraction(); }
        public bool IsNodeVisible(string id) => Node(id) is JObject node && (pins.Contains(id) || (allNodes || scope == null || scope.Contains(id)) && (expandGroups || !IsGroup((string)node["type"]!)));
        public bool IsInputVisible(string id, string key) => !IsGroup(key) || expandGroups || Edges.Any(e => (string?)e["to"] == id && (string?)e["input"] == key && IsNodeVisible((string)e["from"]!));
        public bool IsEdgeVisible(string id) => Edges.FirstOrDefault(e => (string?)e["id"] == id) is JObject edge && IsNodeVisible((string)edge["from"]!) && IsNodeVisible((string)edge["to"]!) && IsInputVisible((string)edge["to"]!, (string)edge["input"]!);
        public Point WorldAt(Point screen) => new((screen.X - offset.X) / Zoom, (screen.Y - offset.Y) / Zoom);
        private Point Screen(Point world) => new(world.X * Zoom + offset.X, world.Y * Zoom + offset.Y);
        public Point NodePosition(string id) { var node = Node(id) ?? throw new ArgumentException(id); var rect = NodeRect(node); return new Point(rect.X + Math.Min(rect.Width / 2, 80 * Zoom), rect.Y + ScreenHeader / 2); }
        public Point PortPosition(string id, string key, bool output)
        {
            var node = Node(id) ?? throw new ArgumentException(id); var ports = output ? UmaWeightGraphModel.OutputPorts((string)node["type"]!) : UmaWeightGraphModel.InputPorts((string)node["type"]!); var index = Array.FindIndex(ports, p => p.Key == key); if (index < 0) throw new ArgumentException(key);
            var rect = NodeRect(node);
            return new Point(output ? rect.Right : rect.Left, Overview ? rect.Y + ScreenHeader / 2 : rect.Y + ScreenHeader + 16 * Zoom + index * PortStep * Zoom);
        }
        public void ZoomAt(double factor, Point anchor)
        {
            var world = WorldAt(anchor); Zoom = Math.Clamp(Zoom * factor, .32, 1.6); if (Overview) wire = null; offset = new Point(anchor.X - world.X * Zoom, anchor.Y - world.Y * Zoom); InvalidateVisual(); ViewChanged?.Invoke();
        }
        public void FocusActualSize()
        {
            var node = SelectedNode == null ? VisibleNodes.FirstOrDefault() : Node(SelectedNode);
            if (node == null) return; Zoom = 1;
            offset = new Point(Bounds.Width / 2 - ((double)node["x"]! + NodeWidth / 2), Bounds.Height / 2 - ((double)node["y"]! + NodeHeight(node) / 2));
            InvalidateVisual(); ViewChanged?.Invoke();
        }
        public void Fit()
        {
            FrameNodes(VisibleNodes.Select(n => (string)n["id"]!));
        }
        public void FrameNodes(IEnumerable<string> identifiers)
        {
            var ids = new HashSet<string>(identifiers); var nodes = VisibleNodes.Where(n => ids.Contains((string)n["id"]!)).ToArray(); if (nodes.Length == 0 || Bounds.Width < 10 || Bounds.Height < 10) return;
            var left = nodes.Min(n => (double?)n["x"] ?? 0); var top = nodes.Min(n => (double?)n["y"] ?? 0); var right = nodes.Max(n => ((double?)n["x"] ?? 0) + NodeWidth); var bottom = nodes.Max(n => ((double?)n["y"] ?? 0) + NodeHeight(n));
            Zoom = Math.Clamp(Math.Min((Bounds.Width - 70) / (right - left), (Bounds.Height - 90) / (bottom - top)), .32, 1);
            offset = new Point((Bounds.Width - (right - left) * Zoom) / 2 - left * Zoom, (Bounds.Height - (bottom - top) * Zoom) / 2 - top * Zoom); InvalidateVisual(); ViewChanged?.Invoke();
        }
        public static IBrush TypeBrush(string type) => Brush.Parse(type switch { "start" => "#7188B4", "stage" => "#9B79D2", "condition" => "#56C2B4", "priority" => "#E5AE58", "weights" => "#D38FD6", "attributes" or "score_attributes" => "#77C99A", "score_bond" => "#E3B86E", "score_charge" => "#7CB6EC", "score_recovery" => "#79C9BA", "score_stage" => "#AB9AE5", "score_risk" => "#E49393", "adaptation" => "#D69A66", "rule" => "#79A7E3", "stage_flow" => "#A78BD2", "rule_flow" => "#83ADE4", _ => "#9CA8BC" });
        private IBrush PortBrush(string type) => CategoryBrush(type);
        private string ReferenceSummary(string key)
        {
            var phase = ReferenceSchema?["phase_coefficients"]?[ReferencePhase] as JObject;
            string Value(string name) => (double?)phase?[name] is double amount ? amount.ToString("0.###", CultureInfo.InvariantCulture) : "?";
            return key switch { "score_attributes" => $"{Value("stats")}/{Value("skill_pt")}", "score_bond" => Value("effective_bond"), "score_charge" => $"{Value("net_charge")}/{Value("newly_full")}/{Value("ss_preparation")}", "score_recovery" => "体力势能／心情公式", "score_stage" => "随阶段动态计算", "score_risk" => "−0.5×失败率等", _ => "见右侧基准" };
        }
        private string[] Summary(JObject node)
        {
            var type = (string?)node["type"]; var data = node["data"] as JObject ?? new JObject();
            if (type == "start") return new[] { "每回合重新检查", "连接“下一阶段”开始" };
            if (type == "stage") { var window = data["window"] as JObject ?? new JObject(); var phase = (string?)window["phase"]; return new[] { phase switch { "pre_debut" => "出道前", "dated" => "指定游戏日期", "year" => $"第{window["year"]}年" + ((int?)window["year"] == 1 && window["from"] is not JObject ? "（含出道前）" : ""), _ => "整个育成期间" }, phase is "year" or "dated" ? $"{DateText(window["from"], "开始")} — {DateText(window["to"], "结束")}" : "沿“阶段内安排”查找" }; }
            if (type == "condition")
            {
                var when = data["when"] as JObject ?? new JObject(); var any = when["any"] is JArray; var rows = when[any ? "any" : "all"]?.OfType<JObject>().ToArray() ?? Array.Empty<JObject>();
                if (rows.Length == 0) return new[] { "总是满足" };
                return rows.Take(3).Select(c => $"{Lookup(ConditionKeys, ConditionLabels, (string?)c["field"])}{Lookup(OpKeys, OpLabels, (string?)c["op"])} {c["value"]}").Prepend(any ? "任一满足（或者）" : "全部满足（并且）").ToArray();
            }
            if (type == "priority") { var actions = data["preferred_actions"]?.Values<string>().Select(a => Lookup(ActionKeys, ActionLabels, a)).ToArray() ?? Array.Empty<string>(); return new[] { actions.Length == 0 ? "沿原策略评分" : string.Join(" › ", actions), "只比较原本可执行的动作" }; }
            if (type == "weights") { var factors = data["training_multipliers"] as JObject; var changed = factors?.Properties().Select(p => $"{Lookup(StatKeys, StatLabels, p.Name)} ×{(double)p.Value:0.###}").ToArray() ?? Array.Empty<string>(); var hasBase = Edges.Any(e => (string?)e["to"] == (string?)node["id"] && (string?)e["input"] == "base"); return new[] { hasBase ? "继承共用基础 + 本段六组覆盖" : "原策略 + 六组权重", changed.Length == 0 ? "训练类别倍率：沿用基础" : string.Join("  ", changed.Take(2)), "点选后可直接打开六组积木" }; }
            if (UmaWeightGraphModel.WeightGroups.FirstOrDefault(g => g.Key == type) is WeightGroup group) { var factors = (data["component_factors"] as JObject)?.Properties().ToArray() ?? Array.Empty<JProperty>(); return new[] { "基准：" + ReferenceSummary(group.Key), $"{group.Components.Length}项 · 已改{factors.Length}项" }.Concat(factors.Take(2).Select(p => $"{ComponentNames.GetValueOrDefault(p.Name, p.Name)} ×{(double)p.Value:0.###}")).ToArray(); }
            if (type == "attributes") { var factors = data["attribute_weights"] as JObject; var rows = StatKeys.Select((key, i) => factors?[key] == null ? $"{StatLabels[i]} 沿原值" : $"{StatLabels[i]} ×{(double)factors[key]!:0.###}").Chunk(2).Select(p => string.Join("   ", p)); return rows.Append(data["after_target_credit"] == null ? "达标折扣沿当前策略" : $"达标后保留 {(double)data["after_target_credit"]!:0.###}").ToArray(); }
            if (type == "adaptation") { var mode = (string?)data["mode"]; var order = data["priorities"]?.Values<string>().Select(v => Lookup(AdaptationKeys, AdaptationLabels, v)).Take(2).ToArray() ?? Array.Empty<string>(); return new[] { mode switch { "defer" => "暂缓可选成长购买", "prefer" => "优先：" + (order.Length == 0 ? "按当前策略" : string.Join(" › ", order)), _ => "按当前策略自动购买" }, $"购买后保留 {data["reserve_points"] ?? 0} Pt", "必要参赛准备继续照常" }; }
            return new[] { (bool?)data["enabled"] == false ? "这条安排已停用" : "首条条件成立的安排生效", "未接条件＝总是" };
        }
        private static string DateText(JToken? token, string fallback) => token is JObject date ? $"{date["month"]}月{((string?)date["half"] == "second" ? "后半" : "前半")}" : fallback;
        private static string Lookup(string[] keys, string[] labels, string? key) { var index = Array.IndexOf(keys, key); return index < 0 ? key ?? "" : labels[index]; }
        private double NodeHeight(JObject node) => HeaderHeight + Math.Max(UmaWeightGraphModel.InputPorts((string)node["type"]!).Length, UmaWeightGraphModel.OutputPorts((string)node["type"]!).Length) * PortStep + Summary(node).Length * 19 + 18;
        private Rect NodeRect(JObject node)
        {
            var location = Screen(new Point((double?)node["x"] ?? 0, (double?)node["y"] ?? 0));
            var count = Math.Max(UmaWeightGraphModel.InputPorts((string)node["type"]!).Length, UmaWeightGraphModel.OutputPorts((string)node["type"]!).Length);
            var height = Overview ? ScreenHeader + 22 : ScreenHeader + count * PortStep * Zoom + Summary(node).Length * Math.Max(14, 19 * Zoom) + 18 * Zoom;
            return new Rect(new Point(Math.Round(location.X), Math.Round(location.Y)), new Size(NodeWidth * Zoom, height));
        }
        public override void Render(DrawingContext context)
        {
            base.Render(context); context.FillRectangle(Themed("canvas"), new Rect(Bounds.Size));
            var step = 26 * Zoom;
            for (var x = offset.X % step; x < Bounds.Width; x += step) for (var y = offset.Y % step; y < Bounds.Height; y += step) context.DrawEllipse(Themed("grid"), null, new Point(x, y), .9, .9);
            foreach (var edge in VisibleEdges)
            {
                try { var from = PortPosition((string)edge["from"]!, (string)edge["port"]!, true); var to = PortPosition((string)edge["to"]!, (string)edge["input"]!, false); var source = Node((string)edge["from"]!)!; var port = UmaWeightGraphModel.OutputPorts((string)source["type"]!).First(p => p.Key == (string?)edge["port"]); DrawWire(context, from, to, (string?)edge["id"] == SelectedEdge ? Themed("wireSelected") : PortBrush(port.Type), (string?)edge["id"] == SelectedEdge ? 4 : 2.5); } catch (ArgumentException) { }
            }
            foreach (var node in VisibleNodes)
            {
                var rect = NodeRect(node); var type = (string)node["type"]!; var selected = (string?)node["id"] == SelectedNode;
                context.DrawRectangle(Themed("card"), new Pen(selected ? Themed("selected") : Themed("nodeBorder"), selected ? 2 : 1), rect, 7 * Zoom, 7 * Zoom);
                context.DrawRectangle(TypeBrush(type), null, new Rect(rect.X, rect.Y, rect.Width, ScreenHeader), 7 * Zoom, 7 * Zoom);
                using var clip = context.PushClip(rect);
                DrawText(context, (string?)node["label"] ?? TypeLabel(type), new Point(rect.X + 6, rect.Y + (ScreenHeader - TitleFontSize * 1.5) / 2), TitleFontSize, Themed("headerText"), true, rect.Width - 12);
                if (Overview) { DrawText(context, OverviewSummary(node), new Point(rect.X + 5, rect.Y + ScreenHeader + 2), 10, MutedBrush, false, rect.Width - 10); continue; }
                var ins = UmaWeightGraphModel.InputPorts(type); var outs = UmaWeightGraphModel.OutputPorts(type);
                for (var i = 0; i < ins.Length; i++)
                {
                    if (!expandGroups && type == "weights" && IsGroup(ins[i].Key)) DrawGroupEntry(context, node, ins[i]);
                    else if (IsInputVisible((string)node["id"]!, ins[i].Key)) DrawPort(context, node, ins[i], false);
                }
                for (var i = 0; i < outs.Length; i++) DrawPort(context, node, outs[i], true);
                var summaryY = rect.Y + ScreenHeader + (Math.Max(ins.Length, outs.Length) * PortStep + 7) * Zoom;
                foreach (var line in Summary(node)) { DrawText(context, line, new Point(rect.X + 8, summaryY), Math.Max(11, 12 * Zoom), MutedBrush, false, rect.Width - 16); summaryY += Math.Max(14, 19 * Zoom); }
            }
            if (wire is { } pending) { try { DrawWire(context, PortPosition(pending.Node, pending.Port, true), lastPointer, Themed("wirePending"), 2); } catch (ArgumentException) { } }
            DrawText(context, $"显示 {VisibleNodeCount}/{Nodes.Count()} 块 · " + (Overview ? "概览：点击选择，100%看选中后编辑端口" : "点彩色分组行编辑 · 滚轮缩放 · 空白处平移"), new Point(15, Bounds.Height - 25), 11, MutedBrush);
        }
        private string OverviewSummary(JObject node)
        {
            if ((string?)node["type"] == "weights") return "六组参数 · 点击查看";
            if (IsGroup((string)node["type"]!)) return $"已改 {(node["data"]?["component_factors"] as JObject)?.Count ?? 0} 项";
            return Summary(node).FirstOrDefault() ?? "";
        }
        private Rect GroupEntryRect(JObject node, string key)
        {
            var point = PortPosition((string)node["id"]!, key, false); var rect = NodeRect(node);
            return new Rect(rect.X + 9, point.Y - Math.Max(8, 10 * Zoom), Math.Max(0, rect.Width - 18), Math.Max(16, 20 * Zoom));
        }
        public Point GroupEntryPosition(string id, string key) => GroupEntryRect(Node(id) ?? throw new ArgumentException(id), key).Center;
        private void DrawGroupEntry(DrawingContext context, JObject node, GraphPort port)
        {
            var area = GroupEntryRect(node, port.Key); var id = (string)node["id"]!;
            var sourceId = (string?)Edges.FirstOrDefault(e => (string?)e["to"] == id && (string?)e["input"] == port.Key)?["from"];
            var count = sourceId == null ? 0 : (Node(sourceId)?["data"]?["component_factors"] as JObject)?.Count ?? 0;
            context.DrawRectangle(Themed("entry"), null, area, 3, 3);
            context.FillRectangle(CategoryBrush(port.Key), new Rect(area.X, area.Y, 3, area.Height));
            DrawText(context, port.Label + (count > 0 ? $" · 改{count}项" : " · " + ReferenceSummary(port.Key)), new Point(area.X + 7, area.Y + 2), Math.Max(11, 11 * Zoom), ForegroundBrush, false, area.Width - 14);
            if (sourceId != null && IsNodeVisible(sourceId)) { var point = PortPosition(id, port.Key, false); context.DrawEllipse(CategoryBrush(port.Key), null, point, 4 * Zoom, 4 * Zoom); }
        }
        private void DrawPort(DrawingContext context, JObject node, GraphPort port, bool output)
        {
            var position = PortPosition((string)node["id"]!, port.Key, output); context.DrawEllipse(PortBrush(port.Type), new Pen(Themed("nodeBorder"), 1.5), position, 6 * Zoom, 6 * Zoom);
            var size = Math.Max(11, 11 * Zoom); var width = NodeWidth * Zoom * .6; var text = TextToFit(port.Label, size, ForegroundBrush, false, width);
            context.DrawText(text, new Point(position.X + (output ? -text.Width - 8 : 8), position.Y - text.Height / 2));
        }
        private static FormattedText TextToFit(string text, double size, IBrush brush, bool bold, double width)
        {
            FormattedText Format(string value) => new(value, CultureInfo.CurrentCulture, FlowDirection.LeftToRight, new Typeface("Microsoft YaHei UI", FontStyle.Normal, bold ? FontWeight.SemiBold : FontWeight.Normal), size, brush);
            var result = Format(text); if (result.Width <= width) return result;
            var lower = 0; var upper = text.Length;
            while (lower < upper) { var middle = (lower + upper + 1) / 2; if (Format(text[..middle] + "…").Width <= width) lower = middle; else upper = middle - 1; }
            if (lower > 0 && char.IsHighSurrogate(text[lower - 1])) lower--;
            return Format(text[..lower] + "…");
        }
        private static void DrawText(DrawingContext context, string text, Point point, double size, IBrush brush, bool bold = false, double width = double.MaxValue) => context.DrawText(TextToFit(text, size, brush, bold, width), point);
        private static (Point C1, Point C2) Controls(Point from, Point to) { var span = Math.Max(65, Math.Abs(to.X - from.X) * .5); return (new Point(from.X + span, from.Y), new Point(to.X - span, to.Y)); }
        private static void DrawWire(DrawingContext context, Point from, Point to, IBrush brush, double width)
        {
            var curve = Controls(from, to); var geometry = new StreamGeometry(); using (var path = geometry.Open()) { path.BeginFigure(from, false); path.CubicBezierTo(curve.C1, curve.C2, to); path.EndFigure(false); } context.DrawGeometry(null, new Pen(brush, width), geometry);
        }
        private (string Node, GraphPort Port, bool Output)? HitPort(Point point)
        {
            if (Overview) return null;
            var top = VisibleNodes.Reverse().FirstOrDefault(n => NodeRect(n).Contains(point));
            foreach (var node in top == null ? VisibleNodes.Reverse() : new[] { top }) foreach (var output in new[] { true, false }) foreach (var port in output ? UmaWeightGraphModel.OutputPorts((string)node["type"]!) : UmaWeightGraphModel.InputPorts((string)node["type"]!))
            { if (!output && !IsInputVisible((string)node["id"]!, port.Key)) continue; var position = PortPosition((string)node["id"]!, port.Key, output); if (Distance(position, point) <= Math.Max(7, 7 * Zoom)) return ((string)node["id"]!, port, output); }
            return null;
        }
        private string? HitEdge(Point point)
        {
            foreach (var edge in VisibleEdges.Reverse())
            {
                var from = PortPosition((string)edge["from"]!, (string)edge["port"]!, true); var to = PortPosition((string)edge["to"]!, (string)edge["input"]!, false); var c = Controls(from, to);
                for (var i = 0; i <= 64; i++) { var t = i / 64.0; var s = 1 - t; var p = new Point(s * s * s * from.X + 3 * s * s * t * c.C1.X + 3 * s * t * t * c.C2.X + t * t * t * to.X, s * s * s * from.Y + 3 * s * s * t * c.C1.Y + 3 * s * t * t * c.C2.Y + t * t * t * to.Y); if (Distance(p, point) < 8) return (string?)edge["id"]; }
            }
            return null;
        }
        private void Pressed(object? sender, PointerPressedEventArgs e)
        {
            Focus(); var point = e.GetPosition(this); lastPointer = point; var keys = e.GetCurrentPoint(this).Properties;
            if (keys.IsMiddleButtonPressed || keys.IsRightButtonPressed) { panOrigin = point; e.Pointer.Capture(this); e.Handled = true; return; }
            if (!keys.IsLeftButtonPressed) return;
            var topNode = VisibleNodes.Reverse().FirstOrDefault(n => NodeRect(n).Contains(point));
            if (!expandGroups && !Overview && (string?)topNode?["type"] == "weights")
            {
                foreach (var group in UmaWeightGraphModel.WeightGroups)
                    if (GroupEntryRect(topNode!, group.Key).Contains(point)) { if (BeforeMutation?.Invoke() != false) GroupRequested?.Invoke((string)topNode!["id"]!, group.Key); e.Handled = true; return; }
            }
            var port = HitPort(point);
            if (port is { } hit)
            {
                if (BeforeMutation?.Invoke() == false) return;
                if (hit.Output) { wire = (hit.Node, hit.Port.Key); wireDragged = false; e.Pointer.Capture(this); }
                else if (wire is { } source) { ConnectionRequested?.Invoke(source.Node, source.Port, hit.Node, hit.Port.Key); wire = null; }
                else Error?.Invoke("先选中输出圆点，再点击相同类型的输入圆点。");
                InvalidateVisual(); e.Handled = true; return;
            }
            wire = null;
            var node = topNode;
            if (node != null)
            {
                if (SelectionRequested?.Invoke((string?)node["id"], null) == false) return;
                if (point.Y <= NodeRect(node).Y + ScreenHeader && BeforeMutation?.Invoke() != false)
                { draggingNode = (string?)node["id"]; dragOrigin = point; var current = Node(draggingNode!)!; originalNode = new Point((double?)current["x"] ?? 0, (double?)current["y"] ?? 0); moved = false; e.Pointer.Capture(this); }
                e.Handled = true; return;
            }
            var edge = HitEdge(point); if (edge != null) { SelectionRequested?.Invoke(null, edge); InvalidateVisual(); e.Handled = true; return; }
            if (SelectionRequested?.Invoke(null, null) == false) return;
            panOrigin = point; e.Pointer.Capture(this); e.Handled = true;
        }
        private void Moved(object? sender, PointerEventArgs e)
        {
            var point = e.GetPosition(this); lastPointer = point;
            var hovered = VisibleNodes.Reverse().FirstOrDefault(n => NodeRect(n).Contains(point));
            if ((string?)hovered?["id"] != hoveredNode) { hoveredNode = (string?)hovered?["id"]; ToolTip.SetTip(this, hovered == null ? null : (string?)hovered["label"] + "\n" + string.Join("\n", Summary(hovered))); }
            if (panOrigin is Point previous) { offset += point - previous; panOrigin = point; InvalidateVisual(); }
            if (draggingNode != null && Node(draggingNode) is JObject node) { var delta = (point - dragOrigin) / Zoom; node["x"] = originalNode.X + delta.X; node["y"] = originalNode.Y + delta.Y; moved |= Distance(point, dragOrigin) > .5; InvalidateVisual(); }
            if (wire != null) { wireDragged |= Distance(point, PortPosition(wire.Value.Node, wire.Value.Port, true)) > 8; InvalidateVisual(); }
        }
        private void Released(object? sender, PointerReleasedEventArgs e)
        {
            if (wire != null && wireDragged && e.InitialPressMouseButton == MouseButton.Left)
            {
                var target = HitPort(e.GetPosition(this)); if (target is { Output: false } input) ConnectionRequested?.Invoke(wire.Value.Node, wire.Value.Port, input.Node, input.Port.Key); else Error?.Invoke("请把线放到相同类型的输入圆点上。"); wire = null;
            }
            if (draggingNode != null && moved) GraphChanged?.Invoke();
            draggingNode = null; panOrigin = null; e.Pointer.Capture(null); InvalidateVisual();
        }
        private static double Distance(Point a, Point b) => Math.Sqrt((a.X - b.X) * (a.X - b.X) + (a.Y - b.Y) * (a.Y - b.Y));
    }
}
