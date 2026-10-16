using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Extensions.MaaFW;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public static class UmaPublicPlant
{
    private sealed record Card(int Id,string Label) { public override string ToString()=>Label; }
    public static JObject Selection() {
        var path=Path.Combine(UmaPublicRuntime.BackendRoot,"runs","plant-support-settings.json");
        return File.Exists(path) ? JObject.Parse(File.ReadAllText(path)) : new JObject();
    }
    public static void Apply(List<MaaProcessor.NodeAndParam> nodes) {
        var selected=Selection();
        if(!selected.HasValues)return;
        foreach(var node in nodes.Where(n=>n.Entry=="UmaPlant"))
            UmaSeedCycle.Inject(node,"UmaPlant",selected);
    }
    public static Control CreateControls(Func<bool> running) {
        var root=new StackPanel { Spacing=8,Margin=new Thickness(0,8,0,15) };
        TextBlock Text(string text)=>new() { Text=text,TextWrapping=TextWrapping.Wrap };
        root.Children.Add(Text("指定支援卡组 / 指定借卡"));
        var deck=new ComboBox { ItemsSource=new[]{"沿用当前卡组"}.Concat(Enumerable.Range(1,10).Select(i=>$"第 {i} 套卡组")).ToArray(),SelectedIndex=0 };
        var search=new TextBox { Watermark="搜索借卡：马娘、卡名或ID" };
        var card=new ComboBox { HorizontalAlignment=HorizontalAlignment.Stretch };
        var level=new NumericUpDown { Minimum=1,Maximum=50,Value=1,Width=100 };
        var stars=new ComboBox { ItemsSource=new[]{"最低0破","最低1破","最低2破","最低3破","最低4破"},SelectedIndex=0 };
        var save=new Button { Content="保存卡组与借卡",HorizontalAlignment=HorizontalAlignment.Stretch };
        var hint=Text("仅切换已保存卡组和第六张好友卡，不重配五张自有卡；找不到指定卡会停止。未指定则沿用当前设置。");
        foreach(var control in new Control[]{deck,search,card,Text("好友卡最低等级"),level,stars,save,hint})root.Children.Add(control);
        Card[] cards=Array.Empty<Card>();Card? chosen=null;
        void Render() {
            var keyword=search.Text ?? "";
            card.ItemsSource=cards.Where(c=>c.Id==0 || c.Id==chosen?.Id || c.Label.Contains(keyword,StringComparison.OrdinalIgnoreCase)).ToArray();
            card.SelectedItem=chosen ?? cards.FirstOrDefault();
        }
        search.TextChanged+=(_,_)=>Render();
        card.SelectionChanged+=(_,_)=>{if(card.SelectedItem is Card value)chosen=value;};
        save.Click+=async(_,_)=> {
            if(running()){hint.Text="请停止任务后再修改卡组与借卡。";return;}
            var draft=new JObject();if(deck.SelectedIndex>0)draft["support_deck"]=deck.SelectedIndex;
            if(chosen?.Id>0){draft["borrow_card_id"]=chosen.Id;draft["borrow_min_level"]=(int)(level.Value ?? 1);draft["borrow_min_break"]=stars.SelectedIndex;}
            save.IsEnabled=false;
            try {await UmaPublicRuntime.Request(new JObject { ["action"]="save",["settings"]=draft },"desktop.umaa_public_plant");hint.Text="已保存；单次开种与连续刷种马共用这组设置，启动时固定。";}
            catch(Exception error){hint.Text="未保存："+error.Message;}
            finally{save.IsEnabled=true;}
        };
        root.AttachedToVisualTree+=async(_,_)=> {
            try {
                var response=await UmaPublicRuntime.Request(new JObject { ["action"]="load" },"desktop.umaa_public_plant");
                var data=response[0]!;var values=data["settings"]!;
                cards=new[]{new Card(0,"沿用卡组当前好友卡")}.Concat(((JArray)data["cards"]!).Select(row=>new Card((int)row["id"]!,(string)row["label"]!))).ToArray();
                var id=(int?)values["borrow_card_id"] ?? 0;
                chosen=cards.FirstOrDefault(c=>c.Id==id) ?? throw new InvalidOperationException("原指定借卡已不在图鉴中，请重新选择；不会替换旧设置。");
                deck.SelectedIndex=(int?)values["support_deck"] ?? 0;
                level.Value=(decimal?)values["borrow_min_level"] ?? 1;stars.SelectedIndex=(int?)values["borrow_min_break"] ?? 0;Render();
            } catch(Exception error){hint.Text="设置读取失败："+error.Message;save.IsEnabled=false;}
        };
        return root;
    }
}
