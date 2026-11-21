using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Two public configuration interfaces over the same validated scoring schema.</summary>
public sealed class UmaScoreEditor : Window
{
    private readonly JObject schema;
    private readonly Func<JObject,Task<JObject>> validate;
    private JObject basis;
    private readonly Dictionary<string,NumericUpDown> factors=new();
    private readonly ComboBox mode=new() {Name="ScoreEditorMode",ItemsSource=new[]{"保持原策略（仅使用基础设置）","内置权重细调","自写评分公式"},SelectedIndex=0};
    private readonly TextBox formula=new() {Name="ScoreEditorFormula",AcceptsReturn=true,MinHeight=130,Text="base_score",TextWrapping=TextWrapping.Wrap,FontFamily=new FontFamily("Cascadia Mono, Consolas")};
    private readonly TextBlock status=Note("新一轮才会使用这些设置；可先在复盘页做取舍对比。");
    private readonly Button apply=new() {Name="ScoreEditorApply",Content="应用到当前配置"};
    private readonly Button check=new() {Name="ScoreEditorValidate",Content="校验配置"};
    private readonly Button import=new() {Name="ScoreEditorImport",Content="导入 JSON"};
    private readonly Button export=new() {Name="ScoreEditorExport",Content="导出 JSON"};
    private bool busy,loadingPreferences;

    public UmaScoreEditor(JObject schema,JObject preferences,Func<JObject,Task<JObject>> validate,string entry="detailed",bool preserveMode=false)
    {
        this.schema=schema;this.validate=validate;basis=(JObject)preferences.DeepClone();
        Title="自定义评分 · 测试版";Width=1000;Height=800;MinWidth=740;MinHeight=540;
        WindowStartupLocation=WindowStartupLocation.CenterOwner;
        var root=new Grid {Margin=new Thickness(20),RowDefinitions=new("Auto,*,Auto"),RowSpacing=12};
        var header=new StackPanel {Spacing=8};
        header.Children.Add(new TextBlock {Text="自定义评分",FontSize=24});
        header.Children.Add(Note("两种入口任选其一。倍率调整或公式都作用于基础设置之后的最终评分；不会修改游戏实际收益和原模型内部预测。"));
        header.Children.Add(mode);
        var actions=new WrapPanel {Orientation=Orientation.Horizontal};
        foreach(var button in new[]{import,export}){button.Margin=new Thickness(0,0,10,0);actions.Children.Add(button);}
        var reset=new Button {Name="ScoreEditorReset",Content="恢复高级设置默认值"};actions.Children.Add(reset);header.Children.Add(actions);
        root.Children.Add(header);
        var tabs=new TabControl {Name="ScoreEditorTabs"};
        var detail=new StackPanel {Spacing=12,Margin=new Thickness(5,12,16,12)};
        detail.Children.Add(Note("1.00＝保持这一项原得分，0＝不考虑，2.00＝加倍。负分项提高倍率会扣得更多。阶段和基础设置仍会决定原分数。"));
        detail.Children.Add(Note("先看一个例子：原总分 60，其中羁绊 20 分；羁绊倍率改成 1.2 后，总分是 60 + 20×0.2 = 64，不是总分乘 1.2。"));
        detail.Children.Add(ExamplePicker("detailed_templates","ScoreEditorDetailedTemplates","ScoreEditorUseDetailedTemplate",
            "套用此细调示例","ScoreEditorDetailedExample",row=> {
                foreach(var pair in factors)pair.Value.Value=(decimal?)row["factors"]?[pair.Key]??1;
                mode.SelectedIndex=1;
                status.Text="已填入细调示例，其他细项恢复为 1，基础设置保留。请检查后应用并保存。";
            }));
        detail.Children.Add(Note("仅查看示例不会改参数。套用会替换全部细项倍率；基础设置中的倍率和人头额外分保留。"));
        var groups=new Dictionary<string,string>{{"numeric","数值收益"},{"bond","羁绊与友人"},{"charge","充电与 SS"},{"energy","体力与恢复"},{"risk","失败风险"},{"other","其他收益与个人偏好"}};
        foreach(var group in (schema["components"]?.OfType<JObject>()??Enumerable.Empty<JObject>()).GroupBy(r=>(string?)r["group"]??"other")) {
            var stack=new StackPanel {Spacing=12};
            foreach(var row in group) {
                var key=(string)row["key"]!;
                var number=new NumericUpDown {Name="ScoreFactor_"+key,Minimum=0,Maximum=10,Increment=.1m,Value=1,FormatString="0.##",Width=112,VerticalAlignment=VerticalAlignment.Center};
                number.ValueChanged+=(_,_)=> {
                    if(!loadingPreferences&&mode.SelectedIndex==0&&number.Value is decimal v&&Math.Abs((double)v-1.0)>1e-5)
                        mode.SelectedIndex=1;
                };
                factors[key]=number;
                var grid=new Grid {ColumnDefinitions=new("*,Auto"),ColumnSpacing=16};
                var text=new StackPanel {Spacing=3};text.Children.Add(new TextBlock {Text=(string?)row["label"],FontSize=16});
                text.Children.Add(Note((string?)row["description"]??""));text.Children.Add(new SelectableTextBlock {Text=key,FontSize=11,Opacity=.55});
                grid.Children.Add(text);Grid.SetColumn(number,1);grid.Children.Add(number);stack.Children.Add(grid);
            }
            detail.Children.Add(new Expander {Header=groups.GetValueOrDefault(group.Key,group.Key),Content=stack,IsExpanded=group.Key is "numeric" or "bond",HorizontalAlignment=HorizontalAlignment.Stretch,HorizontalContentAlignment=HorizontalAlignment.Stretch});
        }
        var written=new StackPanel {Spacing=10,Margin=new Thickness(5,12,16,12)};
        written.Children.Add(Note("填写一条表达式作为最终得分。例如 base_score 保持原分数。支持 + - * / %、比较、and/or/not、条件表达式，以及 min/max/abs/clamp。"));
        written.Children.Add(formula);
        written.Children.Add(ExamplePicker("templates","ScoreEditorTemplates","ScoreEditorUseTemplate",
            "填入此示例公式","ScoreEditorFormulaExample",row=> {
                formula.Text=(string?)row["formula"];mode.SelectedIndex=2;
                status.Text="已填入示例公式，尚未应用。请查看算例，校验后再应用并保存。";
            }));
        written.Children.Add(Note("条件示例：base_score + 8 if mei else base_score。这里只计算分数，不运行 Python，也不能取消失败率上限、数据可靠性或比赛期限限制。"));
        written.Children.Add(Note("未读到的原始增益和人数按0提供给公式，仅表示已确认部分；不代表真实值为0。使用 numeric_score 等原评分组可沿用默认估值。"));
        var variables=new StackPanel {Spacing=8};
        foreach(var row in schema["variables"]?.OfType<JObject>()??Enumerable.Empty<JObject>()) {
            var item=new StackPanel {Spacing=2};item.Children.Add(new SelectableTextBlock {Text=(string?)row["name"],FontFamily=new FontFamily("Consolas"),FontSize=13});
            item.Children.Add(Note((string?)row["description"]??""));variables.Children.Add(item);
        }
        written.Children.Add(new Expander {Header="变量与评分项说明",Content=variables,HorizontalContentAlignment=HorizontalAlignment.Stretch});
        var guide=new StackPanel {Spacing=12,Margin=new Thickness(8,15)};
        guide.Children.Add(new TextBlock {Text="先看例子，再调一项",FontSize=20});
        foreach(var text in schema["quick_guide"]?.Values<string>()??Enumerable.Empty<string>())guide.Children.Add(Note(text??""));
        guide.Children.Add(new TextBlock {Text="保存与复盘对比",FontSize=18,Margin=new Thickness(0,10,0,0)});
        foreach(var text in new[]{"使用顺序：选择评分模式 → 调整倍率或公式 → 校验 → 应用 → 保存运行面板配置 → 开始新一轮。",
            "同一回合会比较可执行的训练、休息、外出和 SS。默认模式保留原来的 SS 节奏；自定义确实改变得分时，会按调整后的分数比较，五人和胜率等执行限制继续保留。",
            "公式只接受文档列出的变量与函数。输入错误会阻止保存；运行时发生除零等问题，本次比较会整体使用原评分并在复盘中记录原因。",
            "导入导出只包含评分设置。原有角色、卡组、连接、授权和消耗额度不会放进权重文件。三个面板配置各自保存；暂停续跑沿用已经开始的那一轮。",
            "在复盘导出页选中已有一局，点击“对比当前取舍参数”，可以查看哪些完整回合快照会改选。这是单回合对比，不模拟后续随机结果。"})guide.Children.Add(Note(text));
        tabs.ItemsSource=new[]{new TabItem {Header="内置权重细调",Content=Scroll(detail)},
            new TabItem {Header="自写评分公式",Content=Scroll(written)},
            new TabItem {Header="使用说明",Content=Scroll(guide)}};
        tabs.SelectedIndex=entry=="formula" ? 1:0;
        Grid.SetRow(tabs,1);root.Children.Add(tabs);
        var footer=new StackPanel {Spacing=8};status.Name="ScoreEditorStatus";footer.Children.Add(status);
        var footerButtons=new StackPanel {Orientation=Orientation.Horizontal,Spacing=10,HorizontalAlignment=HorizontalAlignment.Right};
        var cancel=new Button {Content="取消"};foreach(var button in new[]{check,apply,cancel})footerButtons.Children.Add(button);footer.Children.Add(footerButtons);
        Grid.SetRow(footer,2);root.Children.Add(footer);Content=root;
        formula.TextChanged+=(_,_)=> {
            if(!loadingPreferences&&mode.SelectedIndex==0&&!string.IsNullOrWhiteSpace(formula.Text)&&formula.Text.Trim()!="base_score")
                mode.SelectedIndex=2;
        };
        Load(preferences);
        mode.SelectionChanged+=(_,_)=>{if(mode.SelectedIndex>0)tabs.SelectedIndex=mode.SelectedIndex-1;};
        cancel.Click+=(_,_)=>Close(null);
        reset.Click+=(_,_)=>{Load(new JObject(basis.Properties().Where(p=>p.Name!="tuning").Select(p=>new JProperty(p.Name,p.Value.DeepClone()))));mode.SelectedIndex=0;};
        check.Click+=async(_,_)=>await Validate(false);
        apply.Click+=async(_,_)=>await Validate(true);
        import.Click+=async(_,_)=>await ImportFile();export.Click+=async(_,_)=>await ExportFile();
    }

    private static TextBlock Note(string text)=>new() {Text=text,TextWrapping=TextWrapping.Wrap,FontSize=13};
    private static ScrollViewer Scroll(Control content)=>new() {Content=content,HorizontalContentAlignment=HorizontalAlignment.Stretch,HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled};
    private Control ExamplePicker(string schemaKey,string selectorName,string buttonName,string buttonText,string exampleName,Action<JObject> use)
    {
        var examples=(schema[schemaKey]?.OfType<JObject>()??Enumerable.Empty<JObject>()).ToArray();
        var result=new StackPanel {Spacing=8,IsVisible=examples.Length>0};
        result.Children.Add(new TextBlock {Text="常见需求 · 选一个例子看怎么算",FontSize=16});
        var choices=new ComboBox {Name=selectorName,ItemsSource=examples.Select(x=>(string?)x["name"]).ToArray(),SelectedIndex=0,HorizontalAlignment=HorizontalAlignment.Stretch};
        var button=new Button {Name=buttonName,Content=buttonText};
        var row=new Grid {ColumnDefinitions=new("*,Auto"),ColumnSpacing=10};row.Children.Add(choices);Grid.SetColumn(button,1);row.Children.Add(button);result.Children.Add(row);
        var body=new StackPanel {Name=exampleName,Spacing=7};
        void ShowExample() {
            body.Children.Clear();
            if(choices.SelectedIndex<0 || choices.SelectedIndex>=examples.Length)return;
            var item=examples[choices.SelectedIndex];
            body.Children.Add(Note((string?)item["description"]??""));
            if(item["formula"] is JValue code)body.Children.Add(new SelectableTextBlock {Text=(string?)code,FontFamily=new FontFamily("Consolas"),TextWrapping=TextWrapping.Wrap});
            if(item["calculation"]!=null)body.Children.Add(Note("算一遍："+(string?)item["calculation"]));
            if(item["comparison"]!=null)body.Children.Add(Note("如何影响选择："+(string?)item["comparison"]));
            if(item["notice"]!=null)body.Children.Add(Note("注意："+(string?)item["notice"]));
            body.Children.Add(new TextBlock {Text="以上为假设分数，只演示改分；候选须满足实际执行条件。",FontSize=11,Opacity=.65,TextWrapping=TextWrapping.Wrap});
        }
        choices.SelectionChanged+=(_,_)=>ShowExample();ShowExample();
        button.Click+=(_,_)=> {if(choices.SelectedIndex>=0 && choices.SelectedIndex<examples.Length)use(examples[choices.SelectedIndex]);};
        result.Children.Add(new Border {BorderBrush=Brushes.LightGray,BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(6),Padding=new Thickness(12),Child=body});
        return result;
    }
    public void Load(JObject value)
    {
        loadingPreferences=true;
        try {
            basis=(JObject)value.DeepClone();var tuning=value["tuning"] as JObject??new();
            mode.SelectedIndex=(string?)tuning["mode"] switch {"detailed"=>1,"formula"=>2,_=>0};
            formula.Text=(string?)tuning["formula"]??"base_score";
            foreach(var pair in factors)pair.Value.Value=(decimal?)tuning["factors"]?[pair.Key]??1;
        } finally {loadingPreferences=false;}
    }
    public JObject ReadPreferences()
    {
        var value=(JObject)basis.DeepClone();var items=new JObject();
        foreach(var pair in factors)if(pair.Value.Value is decimal amount && amount!=1)items[pair.Key]=amount;
        value["tuning"]=new JObject {["mode"]=mode.SelectedIndex switch {1=>"detailed",2=>"formula",_=>"default"},
            ["factors"]=items,["formula"]=formula.Text??"base_score"};
        return value;
    }
    public static JObject Profile(JObject schema,JObject preferences)
    {
        if(schema["profile_schema_version"] is not JValue version || schema["scoring_semantics"] is not JObject semantics)
            throw new InvalidOperationException("后台未提供评分语义版本，请更新完整包后再导入导出。");
        return new JObject {["schema_version"]=version.DeepClone(),["scoring_semantics"]=semantics.DeepClone(),["preferences"]=preferences.DeepClone()};
    }
    public string ExportJson()=>Profile(schema,ReadPreferences()).ToString(Formatting.Indented);
    public async Task ImportJson(string text)
    {
        if(text.Length>131072)throw new InvalidOperationException("配置文件过大。");
        var profile=JObject.Parse(text);
        // Pass the whole profile to the backend: stripping metadata here would
        // turn an incompatible schema into apparently valid local preferences.
        var result=await validate(profile);
        Load((JObject?)result["preferences"]??throw new InvalidOperationException("后台没有返回有效配置。"));
        status.Text="已导入并校验；点击应用后再保存面板配置。";
    }
    private void Busy(bool value){busy=value;apply.IsEnabled=check.IsEnabled=import.IsEnabled=export.IsEnabled=!value;}
    private async Task Validate(bool close)
    {
        if(busy)return;Busy(true);
        try {var result=await validate(Profile(schema,ReadPreferences()));var preferences=result["preferences"] as JObject??throw new InvalidOperationException("配置校验未完成。");
            status.Text="配置有效。新一轮生效；可先用已有复盘对比。";if(close)Close(preferences);}
        catch(Exception error){status.Text=error.Message;}
        finally {Busy(false);}
    }
    private async Task ImportFile()
    {
        if(busy)return;
        try {var files=await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions {Title="导入评分配置",AllowMultiple=false,
                FileTypeFilter=new[]{new FilePickerFileType("JSON 配置"){Patterns=new[]{"*.json"}}}});
            if(files.Count==0)return;Busy(true);
            await using var stream=await files[0].OpenReadAsync();using var reader=new StreamReader(stream,Encoding.UTF8,true);
            var buffer=new char[131073];var count=await reader.ReadBlockAsync(buffer.AsMemory());
            if(count>131072)throw new InvalidOperationException("配置文件过大。");await ImportJson(new string(buffer,0,count));}
        catch(Exception error){status.Text=error.Message;}finally{Busy(false);}
    }
    private async Task ExportFile()
    {
        if(busy)return;Busy(true);
        try {var checkedValue=await validate(Profile(schema,ReadPreferences()));Load((JObject)checkedValue["preferences"]!);
            var file=await StorageProvider.SaveFilePickerAsync(new FilePickerSaveOptions {Title="导出评分配置",SuggestedFileName="MaaUma-评分配置.json",DefaultExtension="json",
                FileTypeChoices=new[]{new FilePickerFileType("JSON 配置"){Patterns=new[]{"*.json"}}}});
            if(file==null)return;await using var stream=await file.OpenWriteAsync();stream.SetLength(0);
            await using var writer=new StreamWriter(stream,new UTF8Encoding(false));await writer.WriteAsync(ExportJson());status.Text="已导出评分配置，可直接分享。";}
        catch(Exception error){status.Text=error.Message;}finally{Busy(false);}
    }
}
