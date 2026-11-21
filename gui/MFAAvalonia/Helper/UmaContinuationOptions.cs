using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Shared native editor; no separate window, device call or token storage.</summary>
public sealed class UmaContinuationOptions : StackPanel
{
    public bool EnabledForRun => enabled.IsChecked == true;
    public bool HasUnsubmittedDraft { get; private set; }
    public string DraftNotice => HasUnsubmittedDraft ? validation.Text ?? "" : "";
    public event Action? EnabledChanged;
    private readonly CheckBox enabled = new() { Content="启用自定义续养（最多自动到首次远征前）" };
    private readonly ComboBox juniorMode = Choice("筛选", "仅记录，不筛选");
    private readonly ComboBox combine = Choice("已选条件全部达标", "至少 N 项达标");
    private readonly TextBox minimum = Input("1", "最低达标项数");
    private readonly ComboBox juniorFailure = Choice("暂停保留并通知", "结束本局后回刷切");
    private readonly ComboBox inheritanceMode = Choice("筛选", "仅记录，不筛选");
    private readonly TextBox skills = new() { AcceptsReturn=true, MinHeight=80, TextWrapping=TextWrapping.Wrap,
        Watermark="目标技能：一行一项，可填国服名称或技能ID" };
    private readonly TextBox skillMinimum = Input("1", "技能最低命中数");
    private readonly CheckBox distanceEnabled = new() { Content="启用目标距离路线（与技能路线二选一）" };
    private readonly ComboBox distance = Choice("短距离", "英里", "中距离", "长距离");
    private readonly ComboBox rank = Choice("G", "F", "E", "D", "C", "B", "A", "S");
    private readonly ComboBox inheritanceFailure = Choice("暂停保留并通知", "结束本局后回刷切");
    private readonly CheckBox notify = new() { Content="使用已有通知设置提醒号主", IsChecked=true };
    private readonly StackPanel conditionPanel = new() { Spacing=8 };
    private readonly List<ConditionRow> rows = new();
    private readonly TextBlock validation = new() { Name="PacerContinuationValidation", TextWrapping=TextWrapping.Wrap };

    public UmaContinuationOptions()
    {
        Spacing=10;
        enabled.IsCheckedChanged+=(_,_)=>EnabledChanged?.Invoke();
        Children.Add(enabled);
        Children.Add(validation);
        Children.Add(Note("门槛由你定义；育成目标不会自动当成筛选线。当前版本到首次远征前暂停，保留现场等待号主。"));
        Children.Add(Section("第一节点 · 初级12月下", Labelled("节点模式",juniorMode),
            Labelled("条件组合",combine), Labelled("最低达标项数",minimum), conditionPanel,
            AddButton(), Labelled("明确未通过时",juniorFailure)));
        Children.Add(Section("第二节点 · 继承结束后", Labelled("节点模式",inheritanceMode), skills,
            Labelled("技能最低命中数",skillMinimum), Note("已学和已可学习都算，不购买；满足技能数量或指定距离等级任一条即继续。"),
            distanceEnabled, Labelled("目标距离",distance), Labelled("要求等级",rank),
            Labelled("明确未通过时",inheritanceFailure)));
        Children.Add(notify);
        Children.Add(Note("大小切和事件进度默认只统计。通用大切映射尚未齐全，未识别会显示未知。未知不是未通过；已确认切者保护不变。自动回刷切只在已有刷切批次和原预算内执行。"));
        rank.SelectedIndex=7;
        juniorMode.SelectedIndex=inheritanceMode.SelectedIndex=1;
    }

    private Button AddButton()
    {
        var button=new Button { Content="添加首年条件" };
        button.Click+=(_,_)=>AddRow(new JObject { ["kind"]="stat_sum_min" });
        return button;
    }
    private void AddRow(JObject value)
    {
        var row=new ConditionRow(value); rows.Add(row); conditionPanel.Children.Add(row);
        row.Remove.Click+=(_,_)=>{rows.Remove(row);conditionPanel.Children.Remove(row);};
    }
    public JObject Read() => Read(true);
    public JObject ReadDraft() => Read(false);
    public JObject ReadForSubmission(JObject? saved)
    {
        try {
            var result=Read();HasUnsubmittedDraft=false;validation.Text="";return result;
        }
        catch(FormatException error) {
            if(EnabledForRun) {
                HasUnsubmittedDraft=false;validation.Text="续养配置未完成："+error.Message;
                throw;
            }
            // Disabled incomplete input stays in this window. Keep the last
            // saved condition values in requests; never send placeholders.
            HasUnsubmittedDraft=true;
            validation.Text="续养未启用：未完成的续养草稿仅保留在本窗口，不会提交；已保存的条件保留。";
            var result=saved==null?new JObject { ["schema_version"]="larc_continuation.v1" }:(JObject)saved.DeepClone();
            result["enabled"]=false;return result;
        }
    }
    private JObject Read(bool validate)
    {
        var distances=new JObject();
        if(distanceEnabled.IsChecked==true) distances[new[] {"short","mile","medium","long"}[distance.SelectedIndex]]=rank.SelectedItem?.ToString();
        return new JObject { ["schema_version"]="larc_continuation.v1", ["enabled"]=enabled.IsChecked==true,
            ["notify"]=notify.IsChecked==true,
            ["junior"]=new JObject { ["mode"]=juniorMode.SelectedIndex==0 ? "filter":"observe",
                ["combine"]=combine.SelectedIndex==0 ? "all":"at_least", ["min_pass"]=Value(minimum,validate,1),
                ["conditions"]=new JArray(rows.Select(row=>row.Read(validate))),
                ["on_fail"]=juniorFailure.SelectedIndex==1 ? "restart":"pause" },
            ["inheritance"]=new JObject { ["mode"]=inheritanceMode.SelectedIndex==0 ? "filter":"observe",
                ["skills"]=new JArray((skills.Text??"").Split('\n').Select(s=>s.Trim()).Where(s=>s.Length>0).Distinct()),
                ["min_skills"]=Value(skillMinimum,validate,1), ["distances"]=distances,
                ["on_fail"]=inheritanceFailure.SelectedIndex==1 ? "restart":"pause" } };
    }
    public void Load(JObject? value)
    {
        HasUnsubmittedDraft=false;validation.Text="";
        value??=new JObject(); enabled.IsChecked=(bool?)value["enabled"]==true;
        notify.IsChecked=(bool?)value["notify"]!=false;
        var junior=value["junior"] as JObject ?? new();
        juniorMode.SelectedIndex=(string?)junior["mode"]=="filter" ? 0:1;
        combine.SelectedIndex=(string?)junior["combine"]=="at_least" ? 1:0;
        minimum.Text=junior["min_pass"]?.ToString()??"1";
        juniorFailure.SelectedIndex=(string?)junior["on_fail"]=="restart" ? 1:0;
        rows.Clear();conditionPanel.Children.Clear();
        foreach(var row in junior["conditions"]?.OfType<JObject>()??Enumerable.Empty<JObject>()) AddRow(row);
        var after=value["inheritance"] as JObject ?? new();
        inheritanceMode.SelectedIndex=(string?)after["mode"]=="filter" ? 0:1;
        skills.Text=string.Join("\n",after["skills"]?.Values<string>()??Array.Empty<string>());
        skillMinimum.Text=after["min_skills"]?.ToString()??"1";
        inheritanceFailure.SelectedIndex=(string?)after["on_fail"]=="restart" ? 1:0;
        var pair=(after["distances"] as JObject)?.Properties().FirstOrDefault();
        distanceEnabled.IsChecked=pair!=null;
        if(pair!=null) {distance.SelectedIndex=Array.IndexOf(new[]{"short","mile","medium","long"},pair.Name);
            rank.SelectedIndex=Array.IndexOf(new[]{"G","F","E","D","C","B","A","S"},pair.Value.ToString());}
    }
    public void SetMode(bool allowRestart, bool aceCycle = false)
    {
        var restartLabel = aceCycle ? "放弃本局并重开下一把" : "结束本局后回刷切";
        var jIdx = juniorFailure.SelectedIndex;
        var iIdx = inheritanceFailure.SelectedIndex;
        juniorFailure.ItemsSource = new[] { "暂停保留并通知", restartLabel };
        inheritanceFailure.ItemsSource = new[] { "暂停保留并通知", restartLabel };
        juniorFailure.IsEnabled = inheritanceFailure.IsEnabled = allowRestart;
        juniorFailure.SelectedIndex = allowRestart ? Math.Clamp(jIdx, 0, 1) : 0;
        inheritanceFailure.SelectedIndex = allowRestart ? Math.Clamp(iIdx, 0, 1) : 0;
    }
    private static int Number(TextBox box,int fallback=0) => string.IsNullOrWhiteSpace(box.Text) ? fallback :
        int.TryParse(box.Text,out var n) && n>=0 ? n:throw new FormatException("续养门槛请填写非负整数。");
    private static JToken Value(TextBox box,bool validate,int fallback=0) =>
        validate ? new JValue(Number(box,fallback)) : new JValue(box.Text??"");
    private static TextBox Input(string text,string hint)=>new(){Text=text,Watermark=hint,MinWidth=110};
    private static ComboBox Choice(params string[] values)=>new(){ItemsSource=values,SelectedIndex=0,HorizontalAlignment=HorizontalAlignment.Stretch};
    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private static Control Labelled(string label,Control field)=>new StackPanel {Spacing=4,Children={Note(label),field}};
    private static Expander Section(string label,params Control[] children)
    {var panel=new StackPanel {Spacing=8};foreach(var child in children)panel.Children.Add(child);
        return new Expander {Header=label,IsExpanded=false,Content=panel};}

    private sealed class ConditionRow : StackPanel
    {
        private static readonly string[] Keys={"stat_sum_min","stat_min","adaptation_points_min","supporter_points_min",
            "bond_ready_min","bond_card_min","friend_outing_unlocked","chain_stage_min","chain_complete","cut_event_completed","cut_present"};
        private readonly ComboBox kind=Choice("五维总和最低值","指定属性最低值","适应性Pt最低值","协助者积分最低值",
            "羁绊达线最低张数","指定卡羁绊最低值","友人外出已解锁","指定卡事件段数","指定卡事件已完走","大小切事件已走完","已确认才能出众");
        private readonly ComboBox stat=Choice("速度","耐力","力量","毅力","智力");
        private readonly TextBox minimum=Input("","最低值 / 张数 / 段数");
        private readonly TextBox card=Input("","支援卡ID（指定卡条件必填）");
        private readonly TextBox bond=Input("80","羁绊线");
        private readonly TextBox chain=Input("","事件链ID（同卡有多个链时填写）");
        private readonly ComboBox cut=Choice("大切","小切");
        public Button Remove {get;}=new(){Content="移除条件"};
        public ConditionRow(JObject value) {Spacing=5;Children.Add(kind);Children.Add(stat);Children.Add(minimum);
            Children.Add(card);Children.Add(bond);Children.Add(chain);Children.Add(cut);Children.Add(Remove);
            kind.SelectedIndex=Math.Max(0,Array.IndexOf(Keys,(string?)value["kind"]));
            stat.SelectedIndex=Math.Max(0,Array.IndexOf(new[]{"speed","stamina","power","guts","wisdom"},(string?)value["stat"]));
            minimum.Text=value["minimum"]?.ToString()??"";card.Text=(string?)value["card_id"]??"";
            bond.Text=value["bond_threshold"]?.ToString()??"80";chain.Text=(string?)value["chain_id"]??"";
            cut.SelectedIndex=(string?)value["event_kind"]=="small" ? 1:0;
            kind.SelectionChanged+=(_,_)=>Render();Render();}
        private void Render() {if(kind.SelectedIndex<0||kind.SelectedIndex>=Keys.Length)return;
            var key=Keys[kind.SelectedIndex];stat.IsVisible=key=="stat_min";
            minimum.IsVisible=!new[]{"friend_outing_unlocked","chain_complete","cut_event_completed","cut_present"}.Contains(key);
            card.IsVisible=key is "bond_card_min" or "chain_stage_min" or "chain_complete";
            bond.IsVisible=key=="bond_ready_min";chain.IsVisible=key is "chain_stage_min" or "chain_complete";
            cut.IsVisible=key=="cut_event_completed";}
        public JObject Read(bool validate) {var key=Keys[kind.SelectedIndex];var value=new JObject { ["kind"]=key };
            if(minimum.IsVisible) {if(validate&&string.IsNullOrWhiteSpace(minimum.Text))throw new FormatException("已添加条件需要填写最低值。");
                value["minimum"]=Value(minimum,validate);}
            if(stat.IsVisible)value["stat"]=new[]{"speed","stamina","power","guts","wisdom"}[stat.SelectedIndex];
            if(card.IsVisible)value["card_id"]=card.Text?.Trim()??"";
            if(bond.IsVisible)value["bond_threshold"]=Value(bond,validate,80);
            if(chain.IsVisible&&!string.IsNullOrWhiteSpace(chain.Text))value["chain_id"]=chain.Text.Trim();
            if(cut.IsVisible)value["event_kind"]=cut.SelectedIndex==0 ? "big":"small";return value;}
    }
}
