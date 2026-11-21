using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed record UmaSupportDeckCardChoice(string CardId,string Label,string SearchText,string? Type)
{ public override string ToString()=>Label; }

/// <summary>Optional, user-declared descriptions of five owned cards and one borrowed card.</summary>
public sealed class UmaSupportDeckInputEditor : StackPanel
{
    public const string ProfileSchema="uma_run_support_profile.v1";
    private static readonly string?[] Types={null,"speed","stamina","power","guts","wisdom","friend","group"};
    private static readonly string[] TypeLabels={"未填","速度","耐力","力量","毅力","智力","友人","小组"};
    private readonly Action<JToken?> write;
    private readonly Func<bool>? externalRestore;
    private readonly UmaSupportDeckCardChoice[] catalog;
    private readonly List<Slot> slots=new();
    private readonly TextBlock provenance=Note("");
    private readonly TextBlock status=Note("");
    private readonly Button useReference;
    private JToken? original;
    private bool loading,referenceOnly,copyReference;
    public event Action? PendingChanged;
    public string Message=>status.Text??"";

    private sealed class Slot
    {
        public int Index;
        public readonly HashSet<string> Dirty=new();
        public ComboBox Type=null!,Cards=null!;
        public TextBox Search=null!,Level=null!,Breaks=null!;
        public Expander CardSection=null!;
        public Grid Row=null!;
        public string? CardId;
        public bool Filtering,ClearAll,ReplaceCard;
    }

    public UmaSupportDeckInputEditor(JToken? value,Action<JToken?> changed,IEnumerable<UmaSupportDeckCardChoice>? cards=null,string prefix="SupportDeck",Func<bool>? suppressCatalogAutofill=null)
    {
        Name=prefix;Spacing=8;write=changed;externalRestore=suppressCatalogAutofill;catalog=(cards??Array.Empty<UmaSupportDeckCardChoice>()).Where(c=>!string.IsNullOrWhiteSpace(c.CardId)).GroupBy(c=>c.CardId).Select(g=>g.First()).ToArray();
        Children.Add(new TextBlock{Text="本次实际卡组 · 手动填写",FontSize=16,FontWeight=FontWeight.SemiBold});
        Children.Add(Note("只描述已配好的5张自有卡和1张借卡；不换卡、不借卡、不升级。类型可单独填写，等级、突破未知可留空。"));
        Children.Add(provenance);
        useReference=Button(prefix+"UseReference","用作本次填写",()=>{copyReference=true;referenceOnly=false;SetEditable();PendingChanged?.Invoke();TryCommitPending();});Children.Add(useReference);
        var table=new StackPanel{Spacing=6,MinWidth=760};var header=RowGrid();
        var labels=new[]{"位置","类型","具体卡片（可选）","等级","突破",""};
        for(var i=0;i<labels.Length;i++){var label=Note(labels[i]);Grid.SetColumn(label,i);header.Children.Add(label);}table.Children.Add(header);
        for(var i=0;i<6;i++){var slot=BuildSlot(i,prefix);slots.Add(slot);table.Children.Add(slot.Row);}
        Children.Add(new ScrollViewer{Content=table,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Auto,VerticalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled});
        Children.Add(status);Load(value);
    }

    public static IReadOnlyList<UmaSupportDeckCardChoice> FromChoices(IEnumerable<UmaSupportChoice> values)
    {
        var types=new Dictionary<string,string>{{"速度","speed"},{"耐力","stamina"},{"力量","power"},{"毅力","guts"},{"智力","wisdom"},{"友人","friend"},{"小组","group"},{"intelligence","wisdom"}};
        foreach(var type in Types.Where(t=>t!=null))types[type!]=type!;
        return values.Where(v=>v.CardId>0).Select(v=>new UmaSupportDeckCardChoice(v.CardId.ToString(CultureInfo.InvariantCulture),v.Label,v.SearchText,
            v.Label.Split('·').Select(p=>p.Trim()).Select(p=>types.GetValueOrDefault(p)).FirstOrDefault(t=>t!=null))).ToArray();
    }

    public void Load(JToken? value)
    {
        original=value?.DeepClone();copyReference=false;var profile=original as JObject;
        var kind=(string?)(profile?["source"] as JObject)?["kind"];
        referenceOnly=profile!=null&&((kind!=null&&kind!="user_declared")
            ||(profile["schema_version"]?.Type==JTokenType.String&&(string?)profile["schema_version"]!=ProfileSchema)
            ||kind==null&&profile["cards"] is JArray facts&&facts.Count>0);
        loading=true;foreach(var slot in slots)Reload(slot);loading=false;SetEditable();
        provenance.Text=referenceOnly?"历史来源仅作参考，不代表本局已观测；转换后会保留原始副本。":"来源：用户声明，未经过游戏识别验证。空位不补成已知卡，也不默认等级50。";
        status.Text="";
    }
    public JToken? Read()=>original?.DeepClone();

    private Slot BuildSlot(int index,string prefix)
    {
        var slot=new Slot{Index=index};var key=prefix+"Slot"+index;var row=slot.Row=RowGrid();
        row.Children.Add(new TextBlock{Text=index==5?"借卡位":"自有 "+(index+1),VerticalAlignment=VerticalAlignment.Center});
        slot.Type=new ComboBox{Name=key+"Type",ItemsSource=TypeLabels,Height=34,MinHeight=0,FontSize=14,SelectedIndex=0};Grid.SetColumn(slot.Type,1);row.Children.Add(slot.Type);
        slot.Search=Field(key+"Search");slot.Search.Watermark="搜卡名、类型或编号";
        slot.Cards=new ComboBox{Name=key+"Card",MinHeight=0,Height=34,FontSize=13,HorizontalAlignment=HorizontalAlignment.Stretch};
        var cardBody=new StackPanel{Spacing=5,Children={slot.Search,slot.Cards,Button(key+"ClearCard","清除具体卡资料",()=>{if(referenceOnly)return;ClearIdentity(slot,false);})}};
        slot.CardSection=new Expander{Name=key+"CardSearch",Header="选择具体卡",Content=cardBody,HorizontalContentAlignment=HorizontalAlignment.Stretch};Grid.SetColumn(slot.CardSection,2);row.Children.Add(slot.CardSection);
        slot.Level=Field(key+"Level");slot.Level.Watermark="未知";Grid.SetColumn(slot.Level,3);row.Children.Add(slot.Level);
        slot.Breaks=Field(key+"Breaks");slot.Breaks.Watermark="未知";Grid.SetColumn(slot.Breaks,4);row.Children.Add(slot.Breaks);
        var clear=Button(key+"Clear","清空",()=>{if(referenceOnly)return;ClearIdentity(slot,true);});Grid.SetColumn(clear,5);row.Children.Add(clear);
        slot.Type.SelectionChanged+=(_,_)=>Mark(slot,"type");
        slot.Level.PropertyChanged+=(_,e)=>{if(e.Property==TextBox.TextProperty)Mark(slot,"level");};
        slot.Breaks.PropertyChanged+=(_,e)=>{if(e.Property==TextBox.TextProperty)Mark(slot,"limit_breaks");};
        slot.Search.PropertyChanged+=(_,e)=>{if(e.Property==TextBox.TextProperty&&!loading)RefreshCards(slot);};
        slot.Cards.SelectionChanged+=(_,_)=>{
            if(loading||slot.Filtering||referenceOnly||slot.Cards.SelectedItem is not UmaSupportDeckCardChoice selected)return;
            var nextId=selected.CardId.Length==0?null:selected.CardId;var changed=nextId!=slot.CardId;slot.CardId=nextId;
            if(changed&&externalRestore?.Invoke()!=true){
                loading=true;slot.Type.SelectedIndex=Math.Max(0,Array.IndexOf(Types,selected.Type));slot.Level.Text=slot.Breaks.Text="";loading=false;
                slot.ReplaceCard=true;foreach(var field in new[]{"type","level","limit_breaks"})slot.Dirty.Add(field);
            } else if(selected.Type!=null&&externalRestore?.Invoke()!=true){var type=Array.IndexOf(Types,selected.Type);if(type>=0)slot.Type.SelectedIndex=type;}
            Mark(slot,"card_id");
            slot.CardSection.Header=slot.CardId==null?"选择具体卡":selected.Label;
        };
        return slot;
    }

    private void Mark(Slot slot,string field){if(loading||referenceOnly)return;if(slot.ClearAll&&(slot.Type.SelectedIndex>0||slot.CardId!=null||!string.IsNullOrWhiteSpace(slot.Level.Text)||!string.IsNullOrWhiteSpace(slot.Breaks.Text)))slot.ClearAll=false;slot.Dirty.Add(field);status.Text="卡组说明有未保存修改，可直接保存当前配置。";PendingChanged?.Invoke();}
    private void ClearIdentity(Slot slot,bool wholeSlot)
    {
        loading=true;slot.Type.SelectedIndex=0;slot.CardId=null;slot.Level.Text=slot.Breaks.Text="";RefreshCards(slot);loading=false;
        slot.ClearAll=wholeSlot;slot.ReplaceCard=true;foreach(var field in new[]{"type","card_id","level","limit_breaks"})slot.Dirty.Add(field);PendingChanged?.Invoke();
    }
    private void SetEditable(){foreach(var slot in slots)slot.Row.IsEnabled=!referenceOnly;useReference.IsVisible=referenceOnly;}
    private JObject? Card(JToken? profile,int index)=>(profile as JObject)?["cards"] is JArray cards?cards.OfType<JObject>().FirstOrDefault(c=>c["slot"]?.Type==JTokenType.Integer&&(int)c["slot"]! ==index):null;
    private void Reload(Slot slot)
    {
        var card=Card(original,slot.Index);var type=(string?)card?["type"];
        slot.Type.SelectedIndex=Math.Max(0,Array.IndexOf(Types,type));slot.CardId=Empty(card?["card_id"])?null:card!["card_id"]!.ToString();
        slot.Level.Text=Empty(card?["level"])?"":card!["level"]!.ToString();slot.Breaks.Text=Empty(card?["limit_breaks"])?"":card!["limit_breaks"]!.ToString();
        RefreshCards(slot);slot.Dirty.Clear();slot.ClearAll=slot.ReplaceCard=false;
    }
    private void RefreshCards(Slot slot)
    {
        slot.Filtering=true;
        var query=(slot.Search.Text??"").Split(' ',StringSplitOptions.RemoveEmptyEntries|StringSplitOptions.TrimEntries);
        var current=catalog.FirstOrDefault(c=>c.CardId==slot.CardId)??(slot.CardId==null?null:new UmaSupportDeckCardChoice(slot.CardId,"#"+slot.CardId+"（库中未收录）",slot.CardId,null));
        var matches=catalog.Where(c=>query.All(token=>c.SearchText.Contains(token,StringComparison.OrdinalIgnoreCase))).Take(40);
        var choices=new[]{new UmaSupportDeckCardChoice("","未指定具体卡","",null)}.Concat(current==null?Array.Empty<UmaSupportDeckCardChoice>():new[]{current}).Concat(matches).GroupBy(c=>c.CardId).Select(g=>g.First()).ToArray();
        slot.Cards.ItemsSource=choices;slot.Cards.SelectedItem=choices.FirstOrDefault(c=>c.CardId==(slot.CardId??""));slot.CardSection.Header=current?.Label??"选择具体卡";slot.Filtering=false;
    }

    public bool TryCommitPending()
    {
        if(referenceOnly||!copyReference&&slots.All(s=>s.Dirty.Count==0))return true;
        var patches=new Dictionary<int,JObject>();var cleared=new HashSet<int>();
        foreach(var slot in slots) {
            if(slot.ClearAll){if(Card(original,slot.Index)!=null)cleared.Add(slot.Index);continue;}
            var patch=new JObject();var dirty=copyReference?new HashSet<string>(new[]{"type","card_id","level","limit_breaks"}):slot.Dirty;
            foreach(var field in dirty) {
                JToken? value=null;
                if(field=="type")value=Types[Math.Clamp(slot.Type.SelectedIndex,0,Types.Length-1)] is string type?new JValue(type):null;
                else if(field=="card_id")value=slot.CardId==null?null:new JValue(slot.CardId);
                else {var text=field=="level"?slot.Level.Text:slot.Breaks.Text;if(!string.IsNullOrWhiteSpace(text)){var min=field=="level"?1:0;var max=field=="level"?50:4;if(!int.TryParse(text,NumberStyles.Integer,CultureInfo.InvariantCulture,out var number)||number<min||number>max){status.Text=$"第{slot.Index+1}位{(field=="level"?"等级须为1—50":"突破须为0—4")}，未知可留空；输入已保留。";return false;}value=new JValue(number);}}
                var old=Card(original,slot.Index)?[field];
                if(copyReference||slot.ReplaceCard||!Same(old,value))patch[field]=value?.DeepClone()??JValue.CreateNull();
            }
            if(patch.Count>0)patches[slot.Index]=patch;
        }
        if(patches.Count==0&&cleared.Count==0&&!copyReference){loading=true;foreach(var slot in slots)Reload(slot);loading=false;return true;}
        var candidate=copyReference?new JObject{["reference_snapshot"]=original?.DeepClone()}:(original as JObject is JObject prior?(JObject)prior.DeepClone():new JObject());
        candidate["schema_version"]=ProfileSchema;
        if(candidate["source"] is not JObject source){source=new JObject();candidate["source"]=source;}source["kind"]="user_declared";
        if(candidate["cards"] is not JArray cards){cards=new JArray();candidate["cards"]=cards;}
        foreach(var index in cleared)cards.OfType<JObject>().FirstOrDefault(c=>c["slot"]?.Type==JTokenType.Integer&&(int)c["slot"]! ==index)?.Remove();
        foreach(var pair in patches) {
            var card=cards.OfType<JObject>().FirstOrDefault(c=>c["slot"]?.Type==JTokenType.Integer&&(int)c["slot"]! ==pair.Key);
            var state=slots[pair.Key];
            var identityChanged=pair.Value.Property("card_id")!=null&&!Same(card?["card_id"],pair.Value["card_id"]);
            if(card!=null&&(state.ReplaceCard||identityChanged)){var fresh=new JObject{["slot"]=pair.Key};card.Replace(fresh);card=fresh;}
            if(card==null){card=new JObject{["slot"]=pair.Key};cards.Add(card);}
            if(pair.Value.Properties().Any(p=>p.Name is "type" or "level" or "limit_breaks"))foreach(var key in new[]{"actual_effects","effects","source","observed","verified","run_session_id"})card.Remove(key);
            foreach(var field in pair.Value.Properties())card[field.Name]=field.Value.DeepClone();
            if(new[]{"type","card_id","level","limit_breaks"}.All(key=>Empty(card[key])))card.Remove();
        }
        try{write(candidate.DeepClone());}catch(Exception error){status.Text=error.Message;return false;}
        Load(candidate);status.Text="卡组说明已写入当前配置草稿；来源仍是用户声明，不是本局观测。";return true;
    }
    private static bool Empty(JToken? value)=>value==null||value.Type==JTokenType.Null||value.Type==JTokenType.String&&string.IsNullOrWhiteSpace((string?)value);
    private static bool Same(JToken? first,JToken? second)=>Empty(first)&&Empty(second)||JToken.DeepEquals(first,second);
    private static Grid RowGrid()=>new(){ColumnDefinitions=new("70,115,*,75,75,65"),ColumnSpacing=9};
    private static TextBlock Note(string text)=>new(){Text=text,FontSize=12,TextWrapping=TextWrapping.Wrap};
    private static TextBox Field(string name)=>new(){Name=name,Height=34,MinHeight=0,FontSize=14};
    private static Button Button(string name,string text,Action click){var button=new Button{Name=name,Content=text,Height=32,MinHeight=0,Padding=new Thickness(9,4)};button.Click+=(_,_)=>click();return button;}
}
