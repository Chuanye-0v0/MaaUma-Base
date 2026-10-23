using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Compact, editable requirements. Unknown values stay null until confirmed.</summary>
public sealed class TeamPlanningRequirementsEditor : StackPanel
{
    private sealed record Selection(string? Id,string Label){public override string ToString()=>Label;}
    private readonly Dictionary<string,ComboBox> selections=new();
    private readonly Dictionary<string,TextBox> fields=new();
    private readonly Dictionary<string,Dictionary<string,CheckBox>> styleChoices=new();
    private readonly Dictionary<string,string?> displayedSelection=new();
    private readonly Dictionary<string,string> displayedStyles=new();
    private readonly HashSet<string> edits=new();
    private readonly Dictionary<string,string> displayedText=new();
    private readonly TextBlock serverLabel=TeamPlanningUi.Note("国服");
    private bool loading;
    private JObject basis=new();
    public event Action? Changed;
    public TeamPlanningRequirementsEditor()
    {
        Name="TeamRequirementsCard";Spacing=8;
        Children.Add(TeamPlanningUi.Title("比赛需求卡",17));
        Children.Add(TeamPlanningUi.Note("知道的条件直接选择；待定项可由攻略补充，不影响先读链接或检索。"));
        var primary=new WrapPanel();
        serverLabel.Name="TeamRequirement_server";serverLabel.Margin=new Thickness(0,9,0,0);serverLabel.MinWidth=75;
        primary.Children.Add(TeamPlanningUi.Field("服务器",serverLabel));
        AddSelect(primary,"event_mode","比赛模式",185,Labels["event_mode"]);
        AddSelect(primary,"venue","赛场",185,Labels["venue"]);
        AddSelect(primary,"surface","地面",175,Labels["surface"]);
        AddNumbers(primary,"distance_m","距离（米）",175,[1000,1200,1400,1500,1600,1700,1800,1900,2000,2100,2200,2400,2500,2600,3000,3200,3400,3600]);
        AddSelect(primary,"distance_category","距离类型",185,Labels["distance_category"]);
        AddSelect(primary,"direction","方向",185,Labels["direction"]);
        AddNumbers(primary,"team_size","队伍人数",150,Enumerable.Range(1,9),"AI 推荐",n=>n+" 匹");
        AddText(primary,"event_name","比赛名称（可选）",240,"可留空，或由描述和攻略补充");
        Children.Add(primary);
        Children.Add(TeamPlanningUi.Note("大赛、LOH 默认推荐 3 匹，也可自行调整。跑法与配比不知道怎么选可不填，AI 会按赛道和账号马库推荐。"));
        var optional=new WrapPanel();
        AddNumbers(optional,"edition.year","年份",185,Enumerable.Range(DateTime.Now.Year-1,4));
        AddNumbers(optional,"edition.month","月份",185,Enumerable.Range(1,12));
        AddNumbers(optional,"edition.index","届次",185,Enumerable.Range(1,24));
        foreach(var field in new[]{("season","季节"),("weather","天气"),("track_condition","场况")})AddSelect(optional,field.Item1,field.Item2,185,Labels[field.Item1]);
        Children.Add(new Expander{Name="TeamRaceConditionsAdvanced",Header="更多比赛条件（可留待攻略补充）",Content=optional,IsExpanded=false});
        var styles=new StackPanel{Spacing=8};styles.Children.Add(TeamPlanningUi.Note("默认由 AI 推荐。只有明确想限制阵容时，再选择偏好或指定人数。"));
        AddStyles(styles,"styles.allowed","只用这些跑法（不选＝不限）");
        AddStyles(styles,"styles.preferred","优先考虑（可不选）");
        AddStyles(styles,"styles.excluded","本次不使用（可不选）");
        var counts=new WrapPanel();foreach(var style in StyleLabels)AddNumbers(counts,"styles.counts."+style.Key,style.Value+"：指定人数",145,Enumerable.Range(0,10),"AI 推荐",n=>n==0?"0（不安排）":n.ToString());
        styles.Children.Add(TeamPlanningUi.Field("指定配比（可选）",counts));
        Children.Add(new Expander{Name="TeamRunningStyleAdvanced",Header="跑法偏好与配比（可选，默认 AI 推荐）",Content=styles,IsExpanded=false});
    }
    public void ApplyDefaultTeamSize(int count)
    {
        // Apply only after parsing. A number in the user's description wins.
        if((selections["team_size"].SelectedItem as Selection)?.Id!=null)return;
        SetSelection("team_size",count.ToString());basis["team_size"]=count;
    }
    private void AddText(Panel parent,string key,string title,int width,string watermark)
    {
        var box=new TextBox{Name="TeamRequirement_"+key.Replace('.','_'),Watermark=watermark,Width=width};fields[key]=box;displayedText[key]="";
        box.TextChanged+=(_,_)=>{var text=box.Text??"";if(loading||displayedText[key]==text)return;displayedText[key]=text;edits.Add(key);Changed?.Invoke();};parent.Children.Add(TeamPlanningUi.Field(title,box));
    }
    private void AddSelect(Panel parent,string key,string title,int width,IEnumerable<KeyValuePair<string,string>> values,string placeholder="待定 / 由攻略补充")
    {
        var options=new[]{new Selection(null,placeholder)}.Concat(values.Select(x=>new Selection(x.Key,x.Value))).ToArray();
        var box=new ComboBox{Name="TeamRequirement_"+key.Replace('.','_'),ItemsSource=options,SelectedIndex=0,Width=width};selections[key]=box;displayedSelection[key]=null;
        box.SelectionChanged+=(_,_)=>{
            var id=(box.SelectedItem as Selection)?.Id;if(loading||displayedSelection[key]==id)return;
            displayedSelection[key]=id;edits.Add(key);
            if(key=="event_mode"&&(id is "champions_meeting" or "loh")&&!edits.Contains("team_size")&&(selections["team_size"].SelectedItem as Selection)?.Id==null)SetSelection("team_size","3");
            Changed?.Invoke();
        };parent.Children.Add(TeamPlanningUi.Field(title,box));
    }
    private void AddNumbers(Panel parent,string key,string title,int width,IEnumerable<int> values,string placeholder="待定 / 由攻略补充",Func<int,string>? label=null)
        =>AddSelect(parent,key,title,width,values.Select(n=>new KeyValuePair<string,string>(n.ToString(),label?.Invoke(n)??n.ToString())),placeholder);
    private void SetSelection(string key,string? id)
    {
        var box=selections[key];var values=(box.ItemsSource as IEnumerable<Selection>??[]).ToArray();
        if(id!=null&&values.All(x=>x.Id!=id)){var label=key=="event_mode"&&id=="team_arena"?"竞技场":Labels.TryGetValue(key,out var labels)&&labels.TryGetValue(id,out var known)?known:id;values=values.Append(new Selection(id,label+"（已保存）")).ToArray();box.ItemsSource=values;}
        displayedSelection[key]=id;box.SelectedItem=values.FirstOrDefault(x=>x.Id==id)??values[0];
    }
    private void AddStyles(Panel parent,string key,string title)
    {
        var row=new WrapPanel();var boxes=new Dictionary<string,CheckBox>();styleChoices[key]=boxes;displayedStyles[key]="";
        foreach(var style in StyleLabels){var box=new CheckBox{Name="TeamRequirement_"+key.Replace('.','_')+"_"+style.Key,Content=style.Value,Margin=new Thickness(0,0,16,0)};boxes[style.Key]=box;
            box.IsCheckedChanged+=(_,_)=>{var state=string.Join(",",boxes.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key));if(loading||displayedStyles[key]==state)return;displayedStyles[key]=state;edits.Add(key);Changed?.Invoke();};row.Children.Add(box);}
        parent.Children.Add(TeamPlanningUi.Field(title,row));
    }
    private static readonly Dictionary<string,string> StyleLabels=new(){["nige"]="逃",["senko"]="先",["sashi"]="差",["oikomi"]="追"};
    private static readonly Dictionary<string,Dictionary<string,string>> Labels=new(){
        ["server"]=new(){["cn"]="国服",["jp"]="日服",["tw"]="台服"},
        ["event_mode"]=new(){["champions_meeting"]="大赛",["loh"]="LOH",["custom"]="自定"},
        ["surface"]=new(){["turf"]="草地",["dirt"]="泥地"},
        ["venue"]=new(){["tokyo"]="东京",["nakayama"]="中山",["kyoto"]="京都",["hanshin"]="阪神",["chukyo"]="中京",["sapporo"]="札幌",["hakodate"]="函馆",["fukushima"]="福岛",["niigata"]="新潟",["kokura"]="小仓",["oi"]="大井",["kawasaki"]="川崎",["funabashi"]="船桥",["morioka"]="盛冈",["longchamp"]="隆尚"},
        ["distance_category"]=new(){["sprint"]="短距离",["mile"]="英里",["middle"]="中距离",["long"]="长距离"},
        ["direction"]=new(){["right"]="顺时针",["left"]="逆时针",["straight"]="直线"},
        ["season"]=new(){["spring"]="春",["summer"]="夏",["autumn"]="秋",["winter"]="冬"},
        ["weather"]=new(){["sunny"]="晴",["cloudy"]="多云",["rainy"]="雨",["snowy"]="雪"},
        ["track_condition"]=new(){["good"]="良",["yielding"]="稍重",["soft"]="重",["heavy"]="不良"}
    };
    private static readonly HashSet<string> Numeric=new(){"distance_m","team_size","edition.year","edition.month","edition.index","styles.counts.nige","styles.counts.senko","styles.counts.sashi","styles.counts.oikomi"};
    public void Load(JObject value)
    {
        loading=true;try{
            basis=(JObject)value.DeepClone();edits.Clear();var server=TeamPlanningUi.Str(value["server"]);serverLabel.Text=server is "jp" or "tw"?Labels["server"][server]+"（原草稿）":"国服";
            foreach(var field in fields){var text=TeamPlanningUi.Str(value.SelectToken(field.Key));displayedText[field.Key]=text;field.Value.Text=text;}
            foreach(var key in selections.Keys)SetSelection(key,TeamPlanningUi.Str(value.SelectToken(key)) is string id&&id.Length>0?id:null);
            if((TeamPlanningUi.Str(value["event_mode"]) is "champions_meeting" or "loh")&&(value["team_size"]?.Type is null or JTokenType.Null))SetSelection("team_size","3");
            foreach(var group in styleChoices){var selected=(value.SelectToken(group.Key) as JArray??new()).Select(TeamPlanningUi.Str).ToHashSet();displayedStyles[group.Key]=string.Join(",",group.Value.Keys.Where(selected.Contains));foreach(var box in group.Value)box.Value.IsChecked=selected.Contains(box.Key);}
        }finally{loading=false;}
    }
    public void LoadPreservingEdits(JObject value)
    {
        var manual=edits.ToArray();Load(value);foreach(var key in manual)edits.Add(key);
    }
    private static void Put(JObject result,string key,JToken token)
    {
        var parts=key.Split('.');JObject owner=result;for(var i=0;i<parts.Length-1;i++){if(owner[parts[i]] is not JObject child){child=new JObject();owner[parts[i]]=child;}owner=child;}
        if(key.StartsWith("styles.counts.",StringComparison.Ordinal)&&token.Type==JTokenType.Null)owner.Remove(parts[^1]);else owner[parts[^1]]=token;
    }
    public JObject Read()
    {
        var result=(JObject)basis.DeepClone();result["schema_version"]="uma.team_requirements.v1";
        result["server"]=TeamPlanningUi.Str(basis["server"]) is string server&&server.Length>0?server:"cn";
        foreach(var field in fields)Put(result,field.Key,string.IsNullOrWhiteSpace(field.Value.Text)?JValue.CreateNull():new JValue(field.Value.Text!.Trim()));
        foreach(var field in selections){var id=(field.Value.SelectedItem as Selection)?.Id;Put(result,field.Key,id==null?JValue.CreateNull():Numeric.Contains(field.Key)&&int.TryParse(id,out var number)?new JValue(number):new JValue(id));}
        foreach(var group in styleChoices){var values=group.Value.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key).ToArray();Put(result,group.Key,group.Key=="styles.allowed"&&values.Length==0?JValue.CreateNull():new JArray(values));}
        return result;
    }
    public JObject ReadOverrides()
    {
        var result=new JObject();var all=Read();foreach(var key in edits){var parts=key.Split('.');JObject owner=result;for(var i=0;i<parts.Length-1;i++){if(owner[parts[i]] is not JObject child){child=new JObject();owner[parts[i]]=child;}owner=child;}owner[parts[^1]]=all.SelectToken(key)?.DeepClone()??JValue.CreateNull();}return result;
    }
}

internal static class TeamPlanningUi
{
    public static string Str(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer?value.ToString():"";
    public static int? Int(JToken? value)=>value?.Type==JTokenType.Integer?(int?)value:null;
    public static string[] Tokens(string? text)=>(text??"").Split([',','，','\n','\r','、'],StringSplitOptions.RemoveEmptyEntries).Select(x=>x.Trim()).Where(x=>x.Length>0).ToArray();
    public static TextBlock Note(string text){var label=new TextBlock{Text=text,FontSize=12,TextWrapping=TextWrapping.Wrap};label.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));return label;}
    public static TextBlock Title(string text,int size=16){var label=new TextBlock{Text=text,FontSize=size,FontWeight=FontWeight.SemiBold,TextWrapping=TextWrapping.Wrap};label.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiText"));return label;}
    public static Control Field(string title,Control body)=>new StackPanel{Spacing=4,Margin=new Thickness(0,0,10,7),Children={Note(title),body}};
    public static Border Card(Control body){var card=new Border{Child=body,Padding=new Thickness(12),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(10),Margin=new Thickness(0,2)};card.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));card.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));return card;}
    public static string Lines(JToken? value)=>value is JArray array?string.Join("、",array.Select(x=>x is JObject row?Str(row["name"]) is string n&&n.Length>0?n:Str(row["label"]):Str(x)).Where(x=>x.Length>0)):Str(value);
}
