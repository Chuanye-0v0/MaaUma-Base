using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

public sealed class AcupuncturePolicyWindow:Window
{
    private sealed record DateItem(int Day,string Label) {public override string ToString()=>Label;}
    private static readonly string[] Choices={"1 · 成为强大的优俊少女","2 · 赢下比赛","3 · 健康有活力","4 · 提升魅力","5 · 不太放心，还是算了吧（拒绝）"};
    private static readonly DateItem[] Dates=new[]{new DateItem(-1,"出道前")}.Concat(Enumerable.Range(12,60).Select(day=>
        new DateItem(day,$"{new[]{"初级","经典级","高级"}[day/24]} {day%24/2+1}月{(day%2==0?"上":"下")}半月"))).ToArray();
    private readonly ComboBox mode=new() {Name="AcupunctureMode",ItemsSource=new[]{"默认拒绝","固定选项","按条件选择"},SelectedIndex=0};
    private readonly ComboBox fixedChoice=new() {Name="AcupunctureFixedChoice",ItemsSource=Choices,SelectedIndex=4};
    private readonly ListBox list=new() {Name="AcupunctureRules",MinHeight=100,MaxHeight=190};
    private readonly ComboBox start=new() {Name="AcupunctureStartDate",ItemsSource=Dates,SelectedIndex=0,MinWidth=180};
    private readonly ComboBox end=new() {Name="AcupunctureEndDate",ItemsSource=Dates,SelectedIndex=12,MinWidth=180};
    private readonly NumericUpDown minimum=new() {Name="AcupunctureMinVitality",Minimum=0,Maximum=100,Increment=5,Value=0,Width=115,FormatString="0.#"};
    private readonly NumericUpDown maximum=new() {Name="AcupunctureMaxVitality",Minimum=0,Maximum=100,Increment=5,Value=50,Width=115,FormatString="0.#"};
    private readonly ComboBox choice=new() {Name="AcupunctureRuleChoice",ItemsSource=Choices,SelectedIndex=2};
    private readonly List<JObject> rules=new();
    private readonly TextBlock status=Note("保存后对后续针灸事件生效，不会立即操作游戏。");
    private bool loading;

    public static JObject PolicyFromPreferences(JObject? preferences)
    {
        if(preferences?["acupuncture_policy"] is JObject policy)return (JObject)policy.DeepClone();
        var legacy=(int?)preferences?["acupuncture_choice"]??5;
        return new JObject {["mode"]=legacy is >=1 and <=4?"fixed":"decline",["fixed_choice"]=legacy is >=1 and <=5?legacy:5,["rules"]=new JArray()};
    }
    public static string Summary(JObject? preferences)
    {
        var policy=PolicyFromPreferences(preferences);
        return (string?)policy["mode"] switch {"fixed"=>$"固定选第{(int?)policy["fixed_choice"]??5}项",
            "rules"=>$"按条件选择 · {(policy["rules"] as JArray)?.Count??0}条规则 · 未命中时拒绝",_=>"默认拒绝（第5项）"};
    }
    public AcupuncturePolicyWindow(JObject? preferences,Func<JObject,Task> save)
    {
        Title="庸医扎针 · 选项与条件";Width=950;Height=850;MinWidth=760;MinHeight=600;WindowStartupLocation=WindowStartupLocation.CenterOwner;
        var root=new Grid {Margin=new Thickness(20),RowDefinitions=new("Auto,*,Auto"),RowSpacing=12};
        var header=new StackPanel {Spacing=8};header.Children.Add(new TextBlock {Text="庸医扎针设置",FontSize=24});
        header.Children.Add(Note("默认拒绝。选项按游戏中的五项顺序对应；成功或失败由游戏决定。此窗口只保存针灸设置。"));header.Children.Add(mode);root.Children.Add(header);
        var content=new StackPanel {Spacing=12,Margin=new Thickness(0,4,16,10)};
        var fixedPanel=new StackPanel {Spacing=7};fixedPanel.Children.Add(Note("遇到针灸事件时，固定选择以下选项："));fixedPanel.Children.Add(fixedChoice);content.Children.Add(fixedPanel);
        var rulePanel=new StackPanel {Spacing=10};
        rulePanel.Children.Add(Note("规则从上到下检查，执行第一条同时满足时间和体力范围的规则。范围包含上下限；未命中或条件未读清时，选第5项拒绝。"));
        rulePanel.Children.Add(list);
        var actions=new WrapPanel {Orientation=Orientation.Horizontal};
        Button Action(string name,string text,Action clicked) {var button=new Button {Name=name,Content=text,Margin=new Thickness(0,0,8,4)};button.Click+=(_,_)=>clicked();actions.Children.Add(button);return button;}
        Action("AcupunctureAdd","添加条件",()=> {if(rules.Count>=20){status.Text="最多添加20条规则。";return;}rules.Add(CurrentRule());RefreshList(rules.Count-1);});
        Action("AcupunctureRemove","删除选中",()=> {var index=list.SelectedIndex;if(index>=0){rules.RemoveAt(index);RefreshList(Math.Min(index,rules.Count-1));}});
        void Move(int step) {var index=list.SelectedIndex;var target=index+step;if(index<0 || target<0 || target>=rules.Count)return;(rules[index],rules[target])=(rules[target],rules[index]);RefreshList(target);}
        Action("AcupunctureUp","上移",()=>Move(-1));Action("AcupunctureDown","下移",()=>Move(1));rulePanel.Children.Add(actions);
        rulePanel.Children.Add(Note("选中一条后修改下面内容；修改会同步到该条规则。无选中项时，可先填写再点“添加条件”。"));
        var dateRow=new StackPanel {Orientation=Orientation.Horizontal,Spacing=10};dateRow.Children.Add(start);dateRow.Children.Add(Note("至"));dateRow.Children.Add(end);rulePanel.Children.Add(dateRow);
        var energyRow=new StackPanel {Orientation=Orientation.Horizontal,Spacing=10};energyRow.Children.Add(Note("当前体力百分比"));energyRow.Children.Add(minimum);energyRow.Children.Add(Note("% 至"));energyRow.Children.Add(maximum);energyRow.Children.Add(Note("%"));rulePanel.Children.Add(energyRow);
        rulePanel.Children.Add(choice);
        var examples=new WrapPanel {Orientation=Orientation.Horizontal};
        void Example(string name,string label,int selected,decimal low,decimal high) {
            var button=new Button {Name=name,Content=label,Margin=new Thickness(0,0,8,4)};
            button.Click+=(_,_)=> {rules.Clear();rules.Add(new JObject {["start"]=-1,["end"]=23,["vitality_min"]=low,["vitality_max"]=high,["choice"]=selected});mode.SelectedIndex=2;RefreshList(0);status.Text="已填入示例，保存后才会生效。";};examples.Children.Add(button);
        }
        Example("AcupunctureExampleRecovery","填入示例：初级、体力0～50%，选健康活力",3,0,50);
        Example("AcupunctureExampleCharm","填入示例：初级、任意体力，选提升魅力",4,0,100);
        rulePanel.Children.Add(examples);
        rulePanel.Children.Add(Note("算例：第一条是初级、体力0～50%选第3项；第二条是初级、体力0～100%选第4项。初级体力40%选3，体力70%选4；进入经典级后都不符合，拒绝。体力恰好50%会命中第一条。"));
        content.Children.Add(rulePanel);
        content.Children.Add(Note("时间使用当前事件画面的日期，不用上一回合日期猜测。出道前单独列为一个时间点。条件模式需要读清日期和体力；固定模式不检查这两项。"));
        content.Children.Add(Note("初次五个选项未读齐时仍拒绝。后续确认沿用刚才的选择；拒绝页点“我拒绝”，不会因为体力或设置变化重新选针。"));
        content.Children.Add(Note("事件页里目标技能等其他未保存的修改，仍需回到事件页点“保存事件设置”。"));
        var scroll=new ScrollViewer {Content=content,HorizontalContentAlignment=HorizontalAlignment.Stretch};Grid.SetRow(scroll,1);root.Children.Add(scroll);
        var footer=new StackPanel {Spacing=8};status.Name="AcupunctureStatus";footer.Children.Add(status);
        var buttons=new StackPanel {Orientation=Orientation.Horizontal,Spacing=10,HorizontalAlignment=HorizontalAlignment.Right};
        var saveButton=new Button {Name="AcupunctureSave",Content="保存针灸设置"};var cancel=new Button {Name="AcupunctureCancel",Content="取消"};buttons.Children.Add(saveButton);buttons.Children.Add(cancel);footer.Children.Add(buttons);Grid.SetRow(footer,2);root.Children.Add(footer);Content=root;
        void ModeChanged(){fixedPanel.IsVisible=mode.SelectedIndex==1;rulePanel.IsVisible=mode.SelectedIndex==2;}
        mode.SelectionChanged+=(_,_)=>ModeChanged();
        list.SelectionChanged+=(_,_)=> {if(!loading)LoadRule();};
        foreach(var box in new[]{start,end,choice})box.SelectionChanged+=(_,_)=>EditRule();
        minimum.ValueChanged+=(_,_)=>EditRule();maximum.ValueChanged+=(_,_)=>EditRule();
        LoadPolicy(PolicyFromPreferences(preferences));ModeChanged();
        cancel.Click+=(_,_)=>Close(false);
        saveButton.Click+=async(_,_)=> {root.IsEnabled=false;try {await save(ReadPolicy());Close(true);}catch(Exception error){status.Text=error.Message;}finally{root.IsEnabled=true;}};
    }
    private static TextBlock Note(string text)=>new() {Text=text,TextWrapping=TextWrapping.Wrap,FontSize=13};
    private JObject CurrentRule()=>new() {["start"]=(start.SelectedItem as DateItem)?.Day??-1,["end"]=(end.SelectedItem as DateItem)?.Day??23,
        ["vitality_min"]=minimum.Value??0,["vitality_max"]=maximum.Value??100,["choice"]=choice.SelectedIndex+1};
    private void EditRule(){if(loading || list.SelectedIndex<0)return;var index=list.SelectedIndex;rules[index]=CurrentRule();RefreshList(index,false);}
    private static string DateName(int day)=>Dates.FirstOrDefault(d=>d.Day==day)?.Label??"未设置";
    private void RefreshList(int selected,bool load=true) {
        loading=true;list.ItemsSource=rules.Select((r,i)=>$"{i+1}. {DateName((int)r["start"]!)} → {DateName((int)r["end"]!)}；体力{r["vitality_min"]}～{r["vitality_max"]}%；选{r["choice"]}").ToArray();list.SelectedIndex=selected;loading=false;
        if(load)LoadRule();
    }
    private void LoadRule() {
        if(list.SelectedIndex<0)return;loading=true;var row=rules[list.SelectedIndex];
        start.SelectedItem=Dates.First(d=>d.Day==(int)row["start"]!);end.SelectedItem=Dates.First(d=>d.Day==(int)row["end"]!);
        minimum.Value=(decimal)row["vitality_min"]!;maximum.Value=(decimal)row["vitality_max"]!;choice.SelectedIndex=(int)row["choice"]!-1;loading=false;
    }
    private void LoadPolicy(JObject policy) {
        loading=true;mode.SelectedIndex=(string?)policy["mode"] switch {"fixed"=>1,"rules"=>2,_=>0};fixedChoice.SelectedIndex=((int?)policy["fixed_choice"]??5)-1;
        rules.Clear();rules.AddRange((policy["rules"] as JArray??new JArray()).OfType<JObject>().Select(r=>(JObject)r.DeepClone()));loading=false;RefreshList(rules.Count>0?0:-1);
    }
    public JObject ReadPolicy()=>new() {["mode"]=mode.SelectedIndex switch {1=>"fixed",2=>"rules",_=>"decline"},["fixed_choice"]=fixedChoice.SelectedIndex+1,["rules"]=new JArray(rules.Select(r=>r.DeepClone()))};
}
