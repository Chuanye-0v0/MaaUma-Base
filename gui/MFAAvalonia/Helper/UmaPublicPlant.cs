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
    // Explicit plant setup choices; omitted legacy fields are not migrated.
    private static readonly string[] SetupKeys = { "support_deck", "borrow_card_id", "borrow_min_level", "borrow_min_break",
        "inheritance_selection", "inheritance_borrow_friend" };
    public static JObject BuildSettings(bool reuseInheritance,bool borrowInheritance,int deck,int card,int level,int stars) {
        if(deck<0 || deck>10 || card<0 || (card>0 && (level<1 || level>50 || stars<0 || stars>4)))
            throw new InvalidOperationException("请选择有效的卡组与好友卡条件。");
        var draft=new JObject { ["inheritance_selection"]=reuseInheritance ? "keep" : "auto",
            ["inheritance_borrow_friend"]=borrowInheritance };
        if(deck>0)draft["support_deck"]=deck;
        if(card>0){draft["borrow_card_id"]=card;draft["borrow_min_level"]=level;draft["borrow_min_break"]=stars;}
        return draft;
    }
    public static void ClearSetupOptions(MaaProcessor.NodeAndParam node) {
        var value=JToken.Parse(node.Param ?? "[]");
        IEnumerable<JToken> rows=value is JArray array ? array.Children() : new[]{value};
        foreach(var part in rows)if(part["UmaPlant"]?["custom_action_param"] is JObject old)
            foreach(var key in SetupKeys)old.Remove(key);
        node.Param=value.ToString(Formatting.None);
    }
    public static JObject Selection() {
        var path=Path.Combine(UmaPublicRuntime.BackendRoot,"runs","plant-support-settings.json");
        return File.Exists(path) ? JObject.Parse(File.ReadAllText(path)) : new JObject();
    }
    public static void Apply(List<MaaProcessor.NodeAndParam> nodes) {
        var selected=Selection();
        if(!selected.HasValues)return;
        foreach(var node in nodes.Where(n=>n.Entry=="UmaPlant")) {
            // A saved explicit choice also removes stale specified-card values
            // restored by an old standalone seed node. Legacy files stay unchanged.
            if(selected["inheritance_selection"]!=null || selected["inheritance_borrow_friend"]!=null)ClearSetupOptions(node);
            UmaSeedCycle.Inject(node,"UmaPlant",selected);
        }
    }
    public static Control CreateControls(Func<bool> running) {
        var root=new StackPanel { Spacing=8,Margin=new Thickness(0,8,0,15) };
        TextBlock Text(string text)=>new() { Text=text,TextWrapping=TextWrapping.Wrap };
        root.Children.Add(Text("种马选择"));
        var reuseInheritance=new CheckBox { Content="已配齐则沿用，未配齐自动选择",IsChecked=true };
        var borrowInheritance=new CheckBox { Content="自动选择种马时，借用好友种马",IsChecked=true,Margin=new Thickness(18,0,0,0) };
        root.Children.Add(reuseInheritance);root.Children.Add(borrowInheritance);
        root.Children.Add(Text("指定支援卡组 / 指定借卡"));
        var deck=new ComboBox { ItemsSource=new[]{"沿用当前卡组"}.Concat(Enumerable.Range(1,10).Select(i=>$"第 {i} 套卡组")).ToArray(),SelectedIndex=0 };
        var search=new TextBox { Watermark="搜索借卡：马娘、卡名或ID" };
        var card=new ComboBox { HorizontalAlignment=HorizontalAlignment.Stretch };
        var level=new NumericUpDown { Minimum=1,Maximum=50,Value=1,Width=100 };
        var stars=new ComboBox { ItemsSource=new[]{"最低0破","最低1破","最低2破","最低3破","最低4破"},SelectedIndex=0 };
        var save=new Button { Content="保存种马、卡组与借卡",HorizontalAlignment=HorizontalAlignment.Stretch };
        var hint=Text("已配齐种马则沿用，未配齐自动选择；好友种马借用开关对自动选择生效。卡组与第六张好友支援卡独立选择；未指定时沿用，未配齐则自动补齐，指定卡找不到会停止。");
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
            if(running()){hint.Text="请停止任务后再修改种马、卡组与借卡。";return;}
            if(deck.SelectedIndex<0 || chosen==null){hint.Text="请选择有效的卡组与好友卡。";return;}
            JObject draft;
            try {draft=BuildSettings(reuseInheritance.IsChecked==true,borrowInheritance.IsChecked==true,
                deck.SelectedIndex,chosen.Id,(int)(level.Value ?? 1),stars.SelectedIndex);}
            catch(Exception error){hint.Text="未保存："+error.Message;return;}
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
                reuseInheritance.IsChecked=(string?)values["inheritance_selection"]!="auto";
                borrowInheritance.IsChecked=(bool?)values["inheritance_borrow_friend"] ?? true;
                level.Value=(decimal?)values["borrow_min_level"] ?? 1;stars.SelectedIndex=(int?)values["borrow_min_break"] ?? 0;Render();
            } catch(Exception error){hint.Text="设置读取失败："+error.Message;save.IsEnabled=false;}
        };
        return root;
    }
}
