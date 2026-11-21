using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Diagnostics;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

/// <summary>Read-only local re-scoring; never applies the received preferences.</summary>
internal sealed class ScoringComparisonWindow:Window
{
    private readonly IUmaPacerBridge bridge;
    private readonly JObject profile;
    private readonly ComboBox runs=new() {Name="ExchangeCompareRuns",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly Button calculate=new() {Name="ExchangeCompareRun",Content="开始试算",IsEnabled=false};
    private readonly Button refresh=new() {Name="ExchangeCompareRefresh",Content="刷新记录"};
    private readonly Button open=new() {Name="ExchangeCompareOpen",Content="打开详细对比",IsVisible=false};
    private readonly TextBlock status=Text("正在读取本机复盘……");
    private readonly StackPanel results=new() {Name="ExchangeCompareResults",Spacing=12};
    private string? path;
    private bool busy;

    internal ScoringComparisonWindow(IUmaPacerBridge bridge,JObject profile,string title)
    {
        this.bridge=bridge;this.profile=(JObject)profile.DeepClone();
        Title="用我的复盘试算 · "+title;Width=900;Height=720;MinWidth=620;MinHeight=500;
        WindowStartupLocation=WindowStartupLocation.CenterOwner;
        var body=new Grid {Margin=new Thickness(22),RowDefinitions=new("Auto,Auto,Auto,*"),RowSpacing=12};
        body.Children.Add(Text("只使用本机完整回合快照，比较本局原参数与选中方案。不会修改配置、上传复盘或操作游戏；不预测后续随机结果。"));
        runs.ItemTemplate=new FuncDataTemplate<JObject>((row,_)=>Text($"{row?["started_at"]} · {row?["character_name"]??row?["character_id"]??"角色未知"} · {row?["status_label"]??row?["id"]}"));
        var filters=new Grid {ColumnDefinitions=new("*,Auto,Auto"),ColumnSpacing=8};filters.Children.Add(runs);
        Grid.SetColumn(refresh,1);filters.Children.Add(refresh);Grid.SetColumn(calculate,2);filters.Children.Add(calculate);Grid.SetRow(filters,1);body.Children.Add(filters);
        var notes=new StackPanel {Spacing=8};status.Name="ExchangeCompareStatus";notes.Children.Add(status);notes.Children.Add(open);Grid.SetRow(notes,2);body.Children.Add(notes);
        var scroll=new ScrollViewer {Content=results,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};Grid.SetRow(scroll,3);body.Children.Add(scroll);Content=body;
        Opened+=async(_,_)=>await Refresh();refresh.Click+=async(_,_)=>await Refresh();
        runs.SelectionChanged+=(_,_)=>calculate.IsEnabled=!busy&&runs.SelectedItem is JObject;
        calculate.Click+=async(_,_)=>await Compare();
        open.Click+=(_,_)=>{if(path!=null&&File.Exists(path))Process.Start(new ProcessStartInfo(path){UseShellExecute=true});};
    }
    private static TextBlock Text(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=14};
    private void Busy(bool value){busy=value;runs.IsEnabled=refresh.IsEnabled=!value;calculate.IsEnabled=!value&&runs.SelectedItem is JObject;}
    private async Task Refresh()
    {
        if(busy)return;Busy(true);
        try {
            var value=await bridge.Request(new JObject { ["action"]="list_reviews" });
            var rows=(value["records"] as JArray??new()).OfType<JObject>().Where(r=>!string.IsNullOrEmpty((string?)r["id"]))
                .OrderByDescending(r=>(string?)r["started_at"],StringComparer.Ordinal).ToArray();
            runs.ItemsSource=rows;runs.SelectedIndex=rows.Length>0?0:-1;
            status.Text=rows.Length>0?"选择一局，然后开始试算；进行中的局只使用已保存的完整快照。":"本机暂无复盘记录，已有育成记录后可在这里试算。";
        }catch(Exception e){status.Text="无法读取复盘："+e.Message;}finally{Busy(false);}
    }
    private async Task Compare()
    {
        if(busy||runs.SelectedItem is not JObject run)return;
        Busy(true);open.IsVisible=false;path=null;results.Children.Clear();status.Text="正在用同一份已保存现场试算……";
        try {
            var value=await bridge.Request(new JObject { ["action"]="compare_review_preferences",["run_id"]=run["id"], ["profile"]=profile.DeepClone() });
            path=(string?)value["path"];open.IsVisible=path!=null&&File.Exists(path);
            status.Text=$"已比较 {value["compared"]} 份快照，{value["changed"]} 份会改选，跳过 {value["skipped"]} 份。分差仅代表评分接近程度，不代表训练成功率或最终成品强度。";
            if(value["comparison"] is not JObject comparison){results.Children.Add(Text("当前后台未返回摘要，可打开详细对比；不会因此修改配置。"));return;}
            var observations=(comparison["observations"] as JArray??new()).OfType<JObject>().ToArray();
            if(observations.Length==0)results.Children.Add(Text("没有可重算的完整快照。导出的文字摘要不能替代原始五页决策输入。"));
            foreach(var row in observations.OrderByDescending(r=>(bool?)r["changed"]==true)) {
                var panel=new StackPanel {Spacing=6};
                panel.Children.Add(Text($"第 {row["turn"]} 回合 · {((bool?)row["changed"]==true?"会改选":"选择不变")} · {row["observation"]}"));
                panel.Children.Add(Text(Choice("本局原参数",row["before"] as JObject)));
                panel.Children.Add(Text(Choice("选中方案",row["after"] as JObject)));
                results.Children.Add(new Border {Padding=new Thickness(12),BorderThickness=new Thickness(1),BorderBrush=Brushes.LightGray,Child=panel});
            }
            foreach(var skip in (comparison["skipped"] as JArray??new()).OfType<JObject>())results.Children.Add(Text($"未参与：{skip["observation"]} · {skip["reason"]}"));
        }catch(Exception e){status.Text="无法试算："+e.Message;}finally{Busy(false);}
    }
    private static string Label(string? value)=>value switch {
        "train"=>"训练","train:speed" or "speed"=>"速度训练","train:stamina" or "stamina"=>"耐力训练", "train:power" or "power"=>"力量训练",
        "train:guts" or "guts"=>"毅力训练","train:wisdom" or "wisdom"=>"智力训练","rest" or "rest.normal"=>"休息","outing" or "outing.normal"=>"外出","ss"=>"SS","stop"=>"停止",_=>value??"未知"};
    private static string Choice(string caption,JObject? value)
    {
        if(value==null)return caption+"：缺少结果";
        var text=caption+"："+Label((string?)value["action"])+((string?)value["target"] is string target?" · "+Label(target):"");
        text+=value["score_gap"]?.Type is JTokenType.Integer or JTokenType.Float?$"\n可选评分前两名分差：{value["score_gap"]}":"\n不足两个可选评分，无法计算分差。";
        foreach(var candidate in (value["top_candidates"] as JArray??new()).OfType<JObject>()) {
            text+=$"\n{Label((string?)candidate["action_id"])} {candidate["score"]} 分：";
            text+=string.Join("；",(candidate["components"] as JArray??new()).OfType<JObject>().Select(p=>$"{p["label"]} {(double?)p["value"]:+0.####;-0.####;0}"));
        }
        var unknown=(value["unknown_fields"] as JArray??new()).Values<string>().ToArray();
        if(unknown.Length>0)text+="\n未读清（保留未知，不补数值）："+string.Join("；",unknown);
        return text;
    }
}
