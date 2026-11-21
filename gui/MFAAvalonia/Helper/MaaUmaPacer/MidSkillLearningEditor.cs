using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

/// <summary>Next-run skill settings, owned by the mode/slot form rather than global event preferences.</summary>
public sealed class MidSkillLearningEditor : StackPanel
{
    private sealed record DateRow(ComboBox Year, ComboBox Month, ComboBox Half, Control View);
    private readonly CheckBox enabled = new() { Name="MidSkillEnabled", Content="启用中途学技能" };
    private readonly CheckBox interval = new() { Name="MidSkillIntervalEnabled", Content="按行动间隔检查", IsChecked=true };
    private readonly NumericUpDown intervalTurns = Number("MidSkillIntervalTurns", 6, 1, int.MaxValue);
    private readonly NumericUpDown discount = Number("MidSkillMinDiscount", 10, 0, 100);
    private readonly NumericUpDown maxCost = Number("MidSkillMaxCost", 180, 0, int.MaxValue);
    private readonly NumericUpDown spend = Number("MidSkillSpendPercent", 60, 0, 100);
    private readonly EventSkillPicker skills;
    private readonly StackPanel dateRows = new() { Spacing=8 };
    private readonly List<DateRow> dates = new();
    private readonly TextBlock status = Note("");
    private readonly TextBlock scope = Note("");
    private readonly Button save = new() { Name="MidSkillSave", Content="保存中途学习设置" };
    private bool loading;
    private JObject loadedOptions=new();
    private bool presetSaveAvailable=true;
    public event Action? Changed;
    public Func<Task>? SaveRequested { get; set; }
    public JArray SkillValues => skills.Values;
    public string Summary => enabled.IsChecked != true ? "中途学习：未启用" : skills.Values.Count == 0
        ? "中途学习：已启用，尚未选择技能" : interval.IsChecked != true && dates.Count == 0
        ? "中途学习：已启用，尚未设置学习时机" : $"中途学习：已启用 · {skills.Values.Count} 个候选";

    public MidSkillLearningEditor(IReadOnlyList<EventSkillChoice> catalog, string? catalogError=null)
    {
        Name="PacerMidSkillLearning"; Spacing=10;
        this.Bind(TextElement.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        skills=new EventSkillPicker("MidSkillAllowlist", catalog);
        Children.Add(new TextBlock { Text="育成中学技能 · 名单与时间", FontSize=18 });
        Children.Add(scope); Children.Add(enabled);
        Children.Add(Note("新局按下面的名单学习，可选择各类型技能；到检查时间后按折扣、单价和预算筛选，跳过已学技能，不额外挑选名单外技能。名单为空、点数不足或暂无合适候选时继续育成，运行总览会显示原因。"));
        Children.Add(skills);
        if(catalogError!=null)Children.Add(Note(catalogError));
        var intervalRow=new WrapPanel(); interval.Margin=new Thickness(0,0,10,6);
        intervalRow.Children.Add(interval); intervalRow.Children.Add(Field("每几次行动（默认6）",intervalTurns));
        Children.Add(intervalRow);
        Children.Add(Note("也可指定多个育成日期；与行动间隔可同时启用，同回合最多检查一次。只在该日期检查，不补做已错过的日期。"));
        Children.Add(dateRows);
        var addDate=new Button { Name="MidSkillAddDate", Content="添加学习日期" };
        addDate.Click+=(_,_)=> { AddDate(1,6,"first"); Notify(); }; Children.Add(addDate);
        var limits=new WrapPanel(); limits.Children.Add(Field("最低折扣（%）",discount));
        limits.Children.Add(Field("单技能最高Pt",maxCost)); limits.Children.Add(Field("单次预算 / 当前Pt（%）",spend)); Children.Add(limits);
        Children.Add(Note("最低折扣0%允许按原价学习；最高Pt或预算设为0时不购买。不会自动加入事件里的“优先取得 / 本次不需要”名单。旧局续跑沿用原局学习参数。"));
        Children.Add(save); Children.Add(status);
        enabled.IsCheckedChanged+=(_,_)=>Notify(); interval.IsCheckedChanged+=(_,_)=>Notify();
        foreach(var number in new[]{intervalTurns,discount,maxCost,spend})number.ValueChanged+=(_,_)=>Notify();
        skills.Changed+=Notify;
        save.Click+=async(_,_)=> { if(SaveRequested==null)return; save.IsEnabled=false;
            try { await SaveRequested(); } catch(Exception error) { status.Text="保存未完成："+error.Message; }
            finally { save.IsEnabled=true; } };
        SetMode(false,1); RefreshStatus();
    }

    private static TextBlock Note(string text)=>new() { Text=text, FontSize=12, TextWrapping=TextWrapping.Wrap };
    private static NumericUpDown Number(string name, decimal value, decimal min, decimal max)=>new() {
        Name=name, Value=value, Minimum=min, Maximum=max, Increment=1, FormatString="0.##", MinWidth=150 };
    private static Control Field(string label, Control control)=>new StackPanel { Spacing=4, Margin=new Thickness(0,0,12,8),
        Children={Note(label),control} };
    private void Notify() { if(loading)return; RefreshStatus(); Changed?.Invoke(); }
    private void RefreshStatus()=>status.Text=Summary+(presetSaveAvailable
        ? "。点击本页保存或上方“保存配置”，与本模式当前配置一起保存，下一次开始生效；续跑沿用原局配置。"
        : "。以上设置在下一次开始时使用；续跑沿用原局配置。");
    public void SetPresetSaveAvailable(bool available) { presetSaveAvailable=available;save.IsVisible=available;RefreshStatus(); }
    public void SetSavedMessage(string text)=>status.Text=text;
    public void SetMode(bool cut,int slot)=>scope.Text=$"当前编辑：{(cut?"刷切筛选":"自动育成")} · 配置{slot}。各模式和配置分别保存；未连接模拟器时也可填写。";
    private void AddDate(int year,int month,string half)
    {
        var y=new ComboBox { ItemsSource=new[]{"初级","经典","高级"}, SelectedIndex=year-1, MinWidth=90 };
        var m=new ComboBox { ItemsSource=Enumerable.Range(1,12).Select(i=>$"{i}月").ToArray(), SelectedIndex=month-1, MinWidth=75 };
        var h=new ComboBox { ItemsSource=new[]{"上半月","下半月"}, SelectedIndex=half=="second"?1:half=="first"?0:-1, MinWidth=90 };
        var remove=new Button { Content="删除" }; var row=new WrapPanel();
        foreach(var control in new Control[]{y,m,h,remove}) { control.Margin=new Thickness(0,0,6,5); row.Children.Add(control); }
        var item=new DateRow(y,m,h,row); dates.Add(item); dateRows.Children.Add(row);
        foreach(var choice in new[]{y,m,h})choice.SelectionChanged+=(_,_)=>Notify();
        remove.Click+=(_,_)=> { dates.Remove(item); dateRows.Children.Remove(row); Notify(); };
    }
    public void Load(JObject settings,bool cut,int slot)
    {
        loading=true;
        try {
            skills.SetValues(settings["early_skill_allowlist"]);
            var options=settings["mid_skill_learning"] as JObject;
            loadedOptions=options==null?new JObject():(JObject)options.DeepClone();
            enabled.IsChecked=options!=null ? (bool?)options["enabled"]==true
                : !cut && (bool?)settings["with_ramonu"]==true && skills.Values.Count>0;
            interval.IsChecked=(bool?)options?["interval_enabled"]!=false;
            intervalTurns.Value=(decimal?)options?["interval_turns"]??6;
            discount.Value=(decimal?)options?["min_discount"]??10;
            maxCost.Value=(decimal?)options?["max_cost"]??180;
            spend.Value=(decimal?)options?["spend_percent"]??60;
            dates.Clear(); dateRows.Children.Clear();
            foreach(var date in (options?["dates"] as JArray??new()).OfType<JObject>()) {
                var year=(int?)date["year_index"]??0;var month=(int?)date["month"]??0;var half=(string?)date["half"];
                AddDate(year,month,half??"");
            }
            SetMode(cut,slot); RefreshStatus();
        } finally { loading=false; }
    }
    private static JToken NumberValue(NumericUpDown field,int fallback)
    {
        var value=field.Value??fallback;
        return value==decimal.Truncate(value)?new JValue((int)value):new JValue(value);
    }
    public JObject ReadDraft()
    {
        var values=dates.Select(row=>(Year:row.Year.SelectedIndex+1,Month:row.Month.SelectedIndex+1,Half:row.Half.SelectedIndex));
        var normalized=values.Distinct().OrderBy(date=>date.Year).ThenBy(date=>date.Month).ThenBy(date=>date.Half);
        var list=new JArray(); foreach(var date in normalized)
            list.Add(new JObject { ["year_index"]=date.Year,["month"]=date.Month,["half"]=date.Half==1?"second":date.Half==0?"first":"" });
        var result=(JObject)loadedOptions.DeepClone();
        result["enabled"]=enabled.IsChecked==true;result["interval_enabled"]=interval.IsChecked==true;
        result["interval_turns"]=NumberValue(intervalTurns,6);result["dates"]=list;
        result["min_discount"]=discount.Value??10;result["max_cost"]=NumberValue(maxCost,180);
        result["spend_percent"]=spend.Value??60;
        return result;
    }
    public JObject Read()
    {
        var value=ReadDraft();
        if(value["interval_turns"]!.Type!=JTokenType.Integer||value["max_cost"]!.Type!=JTokenType.Integer)
            throw new FormatException("行动间隔和单技能最高Pt请填整数。");
        foreach(var date in (JArray)value["dates"]!)
            if((int)date["year_index"]! is <1 or >3 || (int)date["month"]! is <1 or >12 || (string?)date["half"] is not ("first" or "second"))
                throw new FormatException("请选完整学习日期。");
        return value;
    }
}
