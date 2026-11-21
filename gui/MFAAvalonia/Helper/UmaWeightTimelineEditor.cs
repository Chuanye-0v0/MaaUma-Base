using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>A projection of the original graph. Every edit patches stable node IDs.</summary>
public sealed class UmaWeightTimelineEditor : Border
{
    private readonly Func<JObject> readGraph;
    private readonly Action<JObject> writeGraph;
    private readonly Action<string?> openAdvanced;
    private readonly StackPanel stages = new() { Spacing = 8 };
    private readonly StackPanel rows = new() { Spacing = 10 };
    private readonly StackPanel inspector = new() { Spacing = 9 };
    private readonly TextBlock status = Note("");
    private readonly TextBlock sheetState = Note("");
    private readonly CheckBox enabled = new() { Name="WeightTimelineEnabled", Content="启用本配置的分段策略" };
    private string? selectedStage, selectedRule;
    private JObject graph = new();
    private JObject? schema;
    private bool refreshing;
    private bool settingEnabled;
    private bool detailsOpen;
    private Button? createCalendarButton;
    private readonly Dictionary<string,bool> expandedSections = new();
    public string? SelectedStageId => selectedStage;
    public string? SelectedRuleId => selectedRule;
    private static readonly string[] Fields = { "vitality", "speed", "stamina", "power", "guts", "wisdom", "skill_pt", "calendar_year", "calendar_month", "calendar_turn", "remaining_turns" };
    private static readonly string[] FieldLabels = { "体力", "速度", "耐力", "力量", "毅力", "智力", "技能 Pt", "育成年份", "月份", "日历回合", "距目标剩余回合" };
    private static readonly string[] Actions = { "train", "ss", "rest", "outing" };
    private static readonly string[] ActionLabels = { "训练", "SS／SSS", "休息", "外出" };
    private static readonly string[] Ops = { "lt", "lte", "eq", "gte", "gt" };
    public UmaWeightTimelineEditor(Func<JObject> read, Action<JObject> write, Action<string?> advanced)
    {
        Name = "UmaWeightTimelineEditor"; readGraph = read; writeGraph = write; openAdvanced = advanced;
        Padding = new Thickness(10); BorderThickness = new Thickness(1); CornerRadius = new CornerRadius(8);
        Background = UmaWeightTheme.Brush(this, "canvas"); BorderBrush = UmaWeightTheme.Brush(this, "border");
        var body = new StackPanel { Spacing = 8 };
        body.Children.Add(new TextBlock { Text = "阶段训练偏好", FontSize = 20, FontWeight = FontWeight.SemiBold });
        body.Children.Add(sheetState);
        body.Children.Add(Note("同一阶段按顺序采用首条满足条件的安排。本段倍率仅在对应时间和条件下采用；×1不额外放大原评分。"));
        var tools = new WrapPanel();
        createCalendarButton=Button("WeightTimelineTemplate", "创建三年安排", CreateCalendar);tools.Children.Add(createCalendarButton);
        tools.Children.Add(enabled);
        enabled.IsCheckedChanged+=(_,_)=>{if(settingEnabled)return;graph["enabled"]=enabled.IsChecked==true;Save();};
        body.Children.Add(tools); body.Children.Add(stages); body.Children.Add(rows); body.Children.Add(inspector);
        body.Children.Add(status); Child = body;
    }
    public void Configure(JObject? value) { schema = value; Refresh(); }
    public void OpenCurrentStage() => openAdvanced(selectedStage);
    private void CreateCalendar()
    {
        if (Nodes.Any(n => (string?)n["type"] != "start")) { status.Text = "已有安排保持不变。需要另一套策略时，请先复制运行配置再导入模板。"; return; }
        writeGraph(UmaWeightGraphModel.GroupedCalendar()); selectedStage = selectedRule = null; Refresh();
    }
    private static TextBlock Note(string value) => new() { Text = value, FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private static Button Button(string name, string label, Action action)
    { var b = new Button { Name = name, Content = label, Height=32, MinHeight=0, FontSize=14, Padding=new Thickness(10,5), Margin = new Thickness(0, 0, 8, 3) }; b.Click += (_, _) => action(); return b; }
    private IEnumerable<JObject> Nodes => ((JArray?)graph["nodes"] ?? new()).OfType<JObject>();
    private IEnumerable<JObject> Edges => ((JArray?)graph["edges"] ?? new()).OfType<JObject>();
    private JObject? Node(string? id) => Nodes.FirstOrDefault(n => (string?)n["id"] == id);
    private string? Next(string id, string port) => (string?)Edges.FirstOrDefault(e => (string?)e["from"] == id && (string?)e["port"] == port)?["to"];
    private JObject? Input(string id, string input) => Node((string?)Edges.FirstOrDefault(e => (string?)e["to"] == id && (string?)e["input"] == input)?["from"]);
    private IEnumerable<JObject> Chain(string? id, string port)
    { var seen = new HashSet<string>(); while (id != null && seen.Add(id) && Node(id) is JObject n) { yield return n; id = Next(id, port); } }
    public void Refresh()
    {
        stages.Children.Clear(); rows.Children.Clear(); inspector.Children.Clear(); enabled.IsEnabled=false;
        refreshing = true;
        try { graph = (JObject)readGraph().DeepClone(); }
        catch (Exception e) { rows.Children.Clear(); inspector.Children.Clear(); status.Text = "此图保留在高级编辑器中：" + e.Message; return; }
        finally { refreshing = false; }
        var ordered = Chain(Next((string)graph["entry"]!, "next"), "next").ToArray();
        if (!ordered.Any(s => (string?)s["id"] == selectedStage)) selectedStage = (string?)ordered.FirstOrDefault()?["id"];
        var sheet = UmaStrategySheetModel.Read(graph);
        sheetState.Text=(bool?)graph["enabled"]==true?"分段安排已启用 · 按阶段与条件匹配":"草稿 / 未启用 · 当前运行沿用基础策略";
        if(createCalendarButton!=null)createCalendarButton.IsVisible=!Nodes.Any(n=>(string?)n["type"]!="start");
        settingEnabled=true;enabled.IsChecked=(bool?)graph["enabled"]==true;enabled.IsEnabled=sheet.Any(r=>r.CanEdit);settingEnabled=false;
        if (selectedStage == null || sheet.Length == 0) { rows.Children.Add(Note("沿用当前基础策略，尚无可列出的阶段安排。需要分段调整时点“创建三年安排”；读取页面不会创建或启用策略。")); return; }
        var currentRules=sheet.Where(r=>r.StageId==selectedStage).ToArray();
        if(!currentRules.Any(r=>r.RuleId==selectedRule))selectedRule=currentRules.FirstOrDefault()?.RuleId;
        var table = new StackPanel {Spacing=10,MinWidth=850};
        foreach(var group in sheet.GroupBy(r=>r.StageId)) {
            var first=group.First();var stage = new StackPanel {Spacing=5};
            var heading=new DockPanel {LastChildFill=true};
            var split=Button("WeightTimelineSplit_"+first.StageId,"拆分日期",()=>{selectedStage=first.StageId;selectedRule=first.RuleId;detailsOpen=true;expandedSections["WeightTimelineSplitSection"]=true;Refresh();inspector.BringIntoView();});
            DockPanel.SetDock(split,Dock.Right);heading.Children.Add(split);
            var stageState=first.StageEnabled==false?" · 阶段停用":first.StageEnabled==null?" · 阶段启停未知":"";
            heading.Children.Add(new StackPanel{Spacing=1,Children={new TextBlock{Text=first.StageLabel+stageState,FontSize=16,FontWeight=FontWeight.SemiBold},Note(first.Window)}});
            stage.Children.Add(heading);stage.Children.Add(SheetHeader());
            foreach(var row in group)stage.Children.Add(SheetRow(row));
            table.Children.Add(stage);
        }
        rows.Children.Add(new ScrollViewer{Content=table,HorizontalScrollBarVisibility=ScrollBarVisibility.Auto,VerticalScrollBarVisibility=ScrollBarVisibility.Disabled});
        status.Text=((bool?)graph["enabled"]==true?"分段安排已启用。":"当前关闭，运行沿用基础策略；草稿保留。")+" 修改后保存当前配置。";
        if(detailsOpen&&selectedRule!=null) {
            inspector.Children.Add(Button("WeightTimelineCloseDetails","收起本段详情",()=>{detailsOpen=false;Refresh();}));
            if(sheet.FirstOrDefault(r=>r.RuleId==selectedRule)?.Notice=="高级图完整保留") {
                inspector.Children.Add(Note("此方案包含高级或未知结构，原图完整保留。请通过高级策略编辑查看，普通表不会改写这份结构。"));return;
            }
            inspector.Children.Add(BuildDateSplit());
            inspector.Children.Add(Button("WeightTimelineAddRule","＋ 本段特殊条件",AddRule));
            BuildInspector();
        }
    }
    private static Grid SheetGrid() => new() {ColumnDefinitions=new("2.2*,*,*,*,*,*,2.1*"),ColumnSpacing=9};
    private static Grid SheetHeader()
    {
        var grid=SheetGrid();var labels=new[]{"适用安排","速 ×","耐 ×","力 ×","根 ×","智 ×","本段设置"};
        for(var i=0;i<labels.Length;i++){var text=Note(labels[i]);Grid.SetColumn(text,i);grid.Children.Add(text);}return grid;
    }
    private Control SheetRow(UmaStrategySheetModel.Row row)
    {
        var body=new StackPanel{Spacing=4};var grid=SheetGrid();var title=new StackPanel{Spacing=2};
        title.Children.Add(new TextBlock{Text=row.Label,FontWeight=FontWeight.SemiBold,TextWrapping=TextWrapping.Wrap});
        var states=new List<string>();if(row.StageEnabled==false)states.Add("阶段停用");else if(row.StageEnabled==null)states.Add("阶段启停未知");
        if(row.RuleEnabled==false)states.Add("安排停用");else if(row.RuleEnabled==null)states.Add("安排启停未知");
        title.Children.Add(Note($"原优先级 {row.Priority}"+(states.Count==0?"":" · "+string.Join(" / ",states))));grid.Children.Add(title);
        var fields=new Dictionary<string,NumericUpDown>();
        for(var i=0;i<UmaStrategySheetModel.TrainingKeys.Length;i++) {
            var key=UmaStrategySheetModel.TrainingKeys[i];Control cell;
            if(row.Effective[key]==null)cell=Note("—");
            else {
                var number=new NumericUpDown{Name="StrategySheet_"+row.RuleId+"_"+key,Value=(decimal)row.Effective[key]!,Minimum=0,Maximum=10,Increment=.1m,FormatString="0.###",Height=34,MinHeight=0,FontSize=14,ShowButtonSpinner=false,MinWidth=66,IsEnabled=row.CanEdit};
                ToolTip.SetTip(number,"本段训练评分倍率；只在阶段与安排均启用且条件匹配时采用");fields[key]=number;cell=number;
            }
            Grid.SetColumn(cell,i+1);grid.Children.Add(cell);
        }
        var more=new StackPanel{Spacing=2};more.Children.Add(Note(row.Notice));
        more.Children.Add(Button("WeightTimelineSelect_"+row.RuleId,"更多调整",()=>{selectedStage=row.StageId;selectedRule=row.RuleId;detailsOpen=true;Refresh();inspector.BringIntoView();}));
        Grid.SetColumn(more,6);grid.Children.Add(more);body.Children.Add(grid);
        body.Children.Add(Note(row.ConditionSummary));
        if(row.CanEdit) {
            var controls=new WrapPanel();
            var preset=new ComboBox{Name="StrategySheetPreset_"+row.RuleId,ItemsSource=new[]{"快捷填值…","五项均为 ×1","速 ×1.2，其余 ×1","智 ×1.2，其余 ×1"},SelectedIndex=0,Height=34,MinHeight=0,FontSize=14,Padding=new Thickness(8,4),MinWidth=190,Margin=new Thickness(0,0,8,3)};
            preset.SelectionChanged+=(_,_)=>{if(preset.SelectedIndex<=0)return;foreach(var pair in fields)pair.Value.Value=(preset.SelectedIndex==2&&pair.Key=="speed"||preset.SelectedIndex==3&&pair.Key=="wisdom")?1.2m:1m;};
            controls.Children.Add(preset);
            controls.Children.Add(Button("StrategySheetApply_"+row.RuleId,"应用本行",()=>{try{var values=new JObject();foreach(var pair in fields){var value=pair.Value.Value??(decimal)row.Effective[pair.Key]!;if(value!=(decimal)row.Effective[pair.Key]!)values[pair.Key]=value;}writeGraph(UmaStrategySheetModel.PatchTraining(graph,row.RuleId,values));Refresh();}catch(Exception error){status.Text=error.Message;}}));
            controls.Children.Add(Button("StrategySheetReset_"+row.RuleId,"恢复基础",()=>{try{writeGraph(UmaStrategySheetModel.PatchTraining(graph,row.RuleId,new JObject(),true));Refresh();}catch(Exception error){status.Text=error.Message;}}));
            body.Children.Add(controls);
        }
        return new Border{Padding=new Thickness(8),CornerRadius=new CornerRadius(7),BorderThickness=new Thickness(1),BorderBrush=UmaWeightTheme.Brush(this,"border"),Background=UmaWeightTheme.Brush(this,"card"),Child=body};
    }
    private static string ActionName(string? key) { var index = Array.IndexOf(Actions, key); return index < 0 ? key ?? "策略" : ActionLabels[index]; }
    private static string ConditionText(JToken? when)
    {
        if (when is not JObject value || value.Properties().FirstOrDefault()?.Value is not JArray clauses || clauses.Count == 0) return "否则／始终匹配";
        return (value.Property("any") != null ? "任一：" : "全部：") + string.Join("；", clauses.OfType<JObject>().Select(c => {
            var index = Array.IndexOf(Fields, (string?)c["field"]); return (index < 0 ? (string?)c["field"] : FieldLabels[index]) + " " + c["op"] + " " + c["value"]; }));
    }
    private void Save() { writeGraph(graph); Refresh(); }
    private JObject EnsureInput(string port, string type)
    {
        var id = selectedRule ?? throw new InvalidOperationException("请先选择安排。");
        if (Input(id, port) is JObject found) return found;
        var node = UmaWeightGraphModel.NewNode(type, 800, 500); ((JArray)graph["nodes"]!).Add(node);
        if(type == "weights")node["data"] = new JObject { ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() };
        ((JArray)graph["edges"]!).Add(UmaWeightGraphModel.NewEdge((string)node["id"]!, "value", id, port)); return node;
    }
    private void BuildInspector()
    {
        if (selectedRule == null) return; var id = selectedRule;
        inspector.Children.Add(new TextBlock { Text = (string?)Node(selectedRule)?["label"] + " · 更多调整", FontSize = 18, FontWeight = FontWeight.SemiBold });
        var body = new StackPanel { Spacing = 9 };
        var condition = Input(id, "condition"); var when = condition?["data"]?["when"] as JObject;
        var clauses = when?.Properties().FirstOrDefault()?.Value as JArray; var mode = new ComboBox { Name = "WeightTimelineConditionCombine", ItemsSource = new[] { "全部满足", "任一满足" }, SelectedIndex = when?.Property("any") != null ? 1 : 0 };
        if (Shared(condition)) body.Children.Add(SharedNote(condition!));
        else {
        body.Children.Add(mode);
        mode.SelectionChanged += (_, _) => { if(refreshing)return; var n = EnsureInput("condition", "condition"); n["data"]!["when"] = new JObject { [mode.SelectedIndex == 1 ? "any" : "all"] = clauses?.DeepClone() ?? new JArray() }; Save(); };
        foreach (var clause in (clauses ?? new JArray()).OfType<JObject>().ToArray())
        {
            var field = new ComboBox { ItemsSource = FieldLabels, SelectedIndex = Array.IndexOf(Fields, (string?)clause["field"]), MinWidth=130 };
            var op = new ComboBox { ItemsSource = new[] { "小于", "不高于", "等于", "不低于", "大于" }, SelectedIndex = Array.IndexOf(Ops, (string?)clause["op"]), MinWidth=100 };
            var value = new TextBox { Text = clause["value"]?.ToString(), Width=100 };
            var line = new WrapPanel(); foreach(var input in new Control[] {field,op,value}) {input.Margin=new Thickness(0,0,8,6);line.Children.Add(input);}
            line.Children.Add(Button("WeightTimelineApplyCondition", "应用条件", () => {
                if (field.SelectedIndex < 0 || op.SelectedIndex < 0 || !double.TryParse(value.Text, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var number) || !double.IsFinite(number)) { status.Text = "条件需要有效数字。"; return; }
                var key = Fields[field.SelectedIndex]; if (key.StartsWith("calendar_") || key == "remaining_turns") { if (number != Math.Truncate(number)) { status.Text = "日期和回合条件需要整数。"; return; } }
                clause["field"] = key; clause["op"] = Ops[op.SelectedIndex]; clause["value"] = key.StartsWith("calendar_") || key == "remaining_turns" ? new JValue((int)number) : new JValue(number); Save(); }));
            line.Children.Add(Button("WeightTimelineRemoveCondition", "移除", () => {clause.Remove();Save();}));body.Children.Add(line);
        }
        body.Children.Add(Button("WeightTimelineAddCondition", "＋ 体力条件", () => { var n = EnsureInput("condition", "condition"); if (condition == null) { Save(); return; } var list = (JArray)((JObject)n["data"]!["when"]!).Properties().First().Value; list.Add(new JObject { ["field"] = "vitality", ["op"] = "lt", ["value"] = 40 }); Save(); }));
        }
        var actions = Input(id, "priority")?["data"]?["preferred_actions"] as JArray;
        var preferred = new ComboBox { Name = "WeightTimelinePreferredAction", ItemsSource = new[] { "沿用策略行动" }.Concat(ActionLabels), SelectedIndex = actions?.Count > 0 ? Array.IndexOf(Actions, (string?)actions[0]) + 1 : 0 };
        if(Shared(Input(id,"priority")))body.Children.Add(SharedNote(Input(id,"priority")!));
        else {
        body.Children.Add(Note("本条优先行动")); body.Children.Add(preferred);
        preferred.SelectionChanged += (_, _) => { if(refreshing)return; var n = EnsureInput("priority", "priority"); n["data"]!["preferred_actions"] = preferred.SelectedIndex <= 0 ? new JArray() : new JArray(new[] { Actions[preferred.SelectedIndex - 1] }.Concat((actions ?? new JArray(Actions)).Values<string>().Where(a => a != Actions[preferred.SelectedIndex - 1]))); Save(); };
        }
        inspector.Children.Add(Section("WeightTimelineConditionSection","什么时候执行 · 条件与优先行动",body));
        inspector.Children.Add(Section("WeightTimelineAttributeSection","属性收益权重 · 每一点属性有多重要",BuildAttributes(Input(id,"attributes"))));
        inspector.Children.Add(Section("WeightTimelineAdaptationSection","适应性购买安排",BuildAdaptation(Input(id,"adaptation"))));
    }
    private Expander Section(string name,string title,Control content,bool expanded=false)
    {
        var section=new Expander{Name=name,Header=title,Content=content,IsExpanded=expandedSections.GetValueOrDefault(name,expanded),HorizontalContentAlignment=HorizontalAlignment.Stretch,HorizontalAlignment=HorizontalAlignment.Stretch};
        section.PropertyChanged+=(_,e)=>{if(e.Property==Expander.IsExpandedProperty)expandedSections[name]=section.IsExpanded;};return section;
    }
    private bool Shared(JObject? node) => node!=null&&Edges.Count(e=>(string?)e["from"]==(string?)node["id"])>1;
    private Control SharedNote(JObject node) => new StackPanel {Spacing=8,Children={
        Note("此节点被多条规则共用，数值原样保留。请到高级画布处理共享关系后修改。"),
        Button("WeightTimelineShared_"+node["id"],"打开共用节点",()=>openAdvanced((string?)node["id"]))}};
    private static void SetMap(JObject node,string map,string key,JToken? value)
    {if(node["data"]![map] is not JObject values){values=new JObject();node["data"]![map]=values;}if(value==null)values.Remove(key);else values[key]=value;}
    private StackPanel FactorEditor(string name,string label,JToken? local,decimal inherited,decimal effective,Action<JToken?> apply,string? reference=null)
    {
        var panel=new StackPanel {Spacing=6,Margin=new Thickness(0,0,0,10)};
        panel.Children.Add(new TextBlock {Text=label,TextWrapping=TextWrapping.Wrap});
        panel.Children.Add(Note($"基础 ×{inherited:0.###} → 本段倍率 ×{effective:0.###}"+(local==null?" · 无本段覆盖":" · 已覆盖")));
        var field=new NumericUpDown {Name=name,Value=(decimal?)local??effective,Minimum=0,Maximum=10,Increment=.1m,FormatString="0.###",MinWidth=110};
        var row=new WrapPanel();row.Children.Add(field);row.Children.Add(Button(name+"Apply","应用",()=>apply(new JValue(field.Value??effective))));
        row.Children.Add(Button(name+"Reset","恢复基础",()=>apply(null)));panel.Children.Add(row);
        if(reference!=null)panel.Children.Add(Note(reference));return panel;
    }
    private static string WindowText(JToken? value)
    {
        if(value is not JObject window)return "当前时间段";
        string Date(JToken? date)=>date is JObject d?$"第{d["year"]}年{d["month"]}月{((string?)d["half"]=="second"?"下":"上")}":"";
        var text=(string?)window["phase"] switch{"year"=>$"第{window["year"]}年","pre_debut"=>"出道前","dated"=>"指定日期",_=>"全部回合"};
        return text+(window["from"] is JObject?" · 从"+Date(window["from"]):"")+(window["to"] is JObject?" 至"+Date(window["to"]):"")+" · 每回合检查";
    }
    private Control BuildDateSplit()
    {
        var panel=new StackPanel{Spacing=8};var row=new WrapPanel();
        var window=Node(selectedStage)?["data"]?["window"] as JObject;
        var first=window?["from"] as JObject;
        var year=new ComboBox{Name="WeightTimelineSplitYear",ItemsSource=new[]{"第一年","第二年","第三年"},SelectedIndex=Math.Clamp(((int?)window?["year"]??(int?)first?["year"]??1)-1,0,2)};
        var month=new NumericUpDown{Name="WeightTimelineSplitMonth",Minimum=1,Maximum=12,Value=6,Increment=1,FormatString="0",Width=80};
        var half=new ComboBox{Name="WeightTimelineSplitHalf",ItemsSource=new[]{"上旬","下旬"},SelectedIndex=0};
        foreach(var control in new Control[]{year,month,half}){control.Margin=new Thickness(0,0,8,5);row.Children.Add(control);}
        row.Children.Add(Button("WeightTimelineSplitApply","从此日期切成下一段",()=>{try{if(selectedStage==null)return;var split=UmaWeightGraphModel.SplitStage(graph,selectedStage,year.SelectedIndex+1,(int)(month.Value??6),half.SelectedIndex==1?"second":"first");writeGraph(split);Refresh();}catch(Exception error){status.Text=error.Message;}}));
        panel.Children.Add(row);panel.Children.Add(Note("切分会复制本段全部规则，左右两段之后可独立调整；原来的复杂规则和连接继续保留。"));
        return Section("WeightTimelineSplitSection","按游戏日期继续切分时间段",panel);
    }
    private Control OptionalNumber(string name,string label,JToken? local,decimal? baseline,double maximum,Action<JToken?> apply)
    {
        var panel=new StackPanel {Spacing=6,Margin=new Thickness(0,0,0,10)};panel.Children.Add(Note(label));
        panel.Children.Add(Note((baseline.HasValue?$"原策略参考 {baseline:0.###} · ":"")+(local==null?"未覆盖时沿用运行配置":$"本段设置 {local}")));
        var field=new TextBox{Name=name,Text=local?.ToString()??"",Watermark="不覆盖",Width=120};var row=new WrapPanel();row.Children.Add(field);
        row.Children.Add(Button(name+"Apply","应用",()=>{var text=field.Text?.Trim();if(string.IsNullOrEmpty(text)){apply(null);return;}
            if(!double.TryParse(text,System.Globalization.NumberStyles.Float,System.Globalization.CultureInfo.InvariantCulture,out var value)||!double.IsFinite(value)||value<=0||value>maximum){status.Text=$"请输入大于0、不超过{maximum}的数值。";return;}apply(new JValue(value));}));
        row.Children.Add(Button(name+"Reset","取消覆盖",()=>apply(null)));panel.Children.Add(row);return panel;
    }
    private Control BuildAttributes(JObject? node)
    {
        if(Shared(node))return SharedNote(node!);
        var body=new StackPanel{Spacing=9};body.Children.Add(Note("训练或SS等行动获得该属性时都会使用此权重。后台原策略参考与当前运行配置分开显示；未覆盖时不擅自填成默认值。"));
        foreach(var key in new[]{"speed","stamina","power","guts","wisdom"})body.Children.Add(OptionalNumber("WeightTimelineAttribute_"+key,
            FieldLabels[Array.IndexOf(Fields,key)]+"收益",node?["data"]?["attribute_weights"]?[key],(decimal?)schema?["attribute_defaults"]?["attribute_weights"]?[key],10,
            value=>{SetMap(EnsureInput("attributes","attributes"),"attribute_weights",key,value);Save();}));
        body.Children.Add(OptionalNumber("WeightTimelineAfterTargetCredit","达标后收益保留比例",node?["data"]?["after_target_credit"],(decimal?)schema?["attribute_defaults"]?["after_target_credit"],1,
            value=>{var n=EnsureInput("attributes","attributes");if(value==null)((JObject)n["data"]!).Remove("after_target_credit");else n["data"]!["after_target_credit"]=value;Save();}));return body;
    }
    private Control BuildComponentGroup(JObject? weights,WeightGroup group)
    {
        var body=new StackPanel {Spacing=9};var node=weights==null?null:Input((string)weights["id"]!,group.Key);
        if(Shared(weights)||Shared(node))return Section("WeightTimelineGroup_"+group.Key,group.Label,SharedNote(Shared(node)?node!:weights!));
        var resolved=weights==null?new JObject():UmaWeightGraphModel.ResolveWeights(graph,(string)weights["id"]!);
        var baseNode=weights==null?null:Input((string)weights["id"]!,"base");
        var inherited=baseNode==null?new JObject():UmaWeightGraphModel.ResolveWeights(graph,(string)baseNode["id"]!);
        foreach(var key in group.Components) {
            var definition=schema?["components"]?.OfType<JObject>().FirstOrDefault(c=>(string?)c["key"]==key);
            var local=node?["data"]?["component_factors"]?[key];
            var own=(decimal?)weights?["data"]?["component_factors"]?[key];var baseline=own??(decimal?)inherited["component_factors"]?[key]??1;
            body.Children.Add(FactorEditor("WeightTimelineComponent_"+key,(string?)definition?["label"]??key,local,baseline,(decimal?)resolved["component_factors"]?[key]??1,
                value=>{var owner=EnsureInput("weights","weights");var target=Input((string)owner["id"]!,group.Key);
                    if(target==null){target=UmaWeightGraphModel.NewNode(group.Key,600,700);((JArray)graph["nodes"]!).Add(target);((JArray)graph["edges"]!).Add(UmaWeightGraphModel.NewEdge((string)target["id"]!,"value",(string)owner["id"]!,group.Key));}
                    SetMap(target,"component_factors",key,value);Save();},ReferenceText(definition)));
        }
        return Section("WeightTimelineGroup_"+group.Key,group.Label,body);
    }
    private string ReferenceText(JObject? definition)
    {
        if(definition?["reference"] is not JObject reference)return "原策略参考尚未加载；此处是相对原得分的倍率。";
        var year=(int?)Node(selectedStage)?["data"]?["window"]?["year"];
        // A custom condition is not evidence that the whole stage is an expedition.
        // Only the explicitly labelled calendar template rule selects this reference.
        var overseas=((string?)Node(selectedRule)?["label"]??"").Contains("远征")
            && Input(selectedRule!,"condition")?["data"]?["when"]?.ToString().Contains("calendar_month")==true;
        var phase=year==1?"junior_unspecified":year==2?(overseas?"classic_expedition":"classic_domestic"):year==3?(overseas?"senior_expedition":"senior_domestic"):"baseline";
        var coefficient=reference["phase_values"]?[phase];
        return (coefficient==null?"":$"原策略参考系数 {coefficient} {reference["unit"]}。 ")+((string?)reference["summary"]??"随当前回合计算。");
    }
    private Control BuildAdaptation(JObject? node)
    {
        if(Shared(node))return SharedNote(node!);
        var body=new StackPanel {Spacing=9};
        var mode=new ComboBox{Name="WeightTimelineAdaptationMode",ItemsSource=new[]{"按当前策略自动购买","优先指定升级","暂缓可选成长升级"},SelectedIndex=(string?)node?["data"]?["mode"] switch{"prefer"=>1,"defer"=>2,_=>0}};
        body.Children.Add(mode);mode.SelectionChanged+=(_,_)=>{if(refreshing)return;EnsureInput("adaptation","adaptation")["data"]!["mode"]=mode.SelectedIndex switch{1=>"prefer",2=>"defer",_=>"auto"};Save();};
        var reserve=new TextBox{Name="WeightTimelineAdaptationReserve",Text=node?["data"]?["reserve_points"]?.ToString()??"0",Width=120};
        body.Children.Add(Note("购买后保留适应性Pt"));var row=new WrapPanel();row.Children.Add(reserve);row.Children.Add(Button("WeightTimelineApplyAdaptationReserve","应用",()=>{
            if(!int.TryParse(reserve.Text,System.Globalization.NumberStyles.Integer,System.Globalization.CultureInfo.InvariantCulture,out var value)||value<0){status.Text="保留Pt填写非负整数。";return;}
            EnsureInput("adaptation","adaptation")["data"]!["reserve_points"]=value;Save();}));body.Children.Add(row);
        var priorities=node?["data"]?["priorities"] as JArray;
        body.Children.Add(Note(priorities?.Count>0?"当前优先项："+string.Join(" → ",priorities.Values<string>()):"优先项沿用当前策略。"));
        body.Children.Add(Button("WeightTimelineEditAdaptationOrder","选择升级项目与顺序",()=>openAdvanced((string?)node?["id"]??selectedRule)));
        body.Children.Add(Note("现有升级项目和顺序完整保留。只影响已解锁、可支付的可选成长购买；必要参赛准备仍按原流程。"));return body;
    }
    private void AddRule()
    {
        if (selectedStage == null) { status.Text = "请先选择时间段。"; return; }
        var node = UmaWeightGraphModel.NewNode("rule", 900, 100); node["label"] = "新增条件安排";
        var condition = UmaWeightGraphModel.NewNode("condition", 900, 350); ((JArray)graph["nodes"]!).Add(node); ((JArray)graph["nodes"]!).Add(condition);
        var first = Edges.FirstOrDefault(e => (string?)e["from"] == selectedStage && (string?)e["port"] == "rules");
        if (first != null) { var old = (string)first["to"]!; first["to"] = node["id"]!.DeepClone(); ((JArray)graph["edges"]!).Add(UmaWeightGraphModel.NewEdge((string)node["id"]!, "next", old, "flow")); }
        else ((JArray)graph["edges"]!).Add(UmaWeightGraphModel.NewEdge(selectedStage, "rules", (string)node["id"]!, "flow"));
        ((JArray)graph["edges"]!).Add(UmaWeightGraphModel.NewEdge((string)condition["id"]!, "value", (string)node["id"]!, "condition")); selectedRule = (string)node["id"]!; Save();
    }
}
