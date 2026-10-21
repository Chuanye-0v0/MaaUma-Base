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
    private readonly Dictionary<string,TextBox> fields=new();
    private readonly HashSet<string> edits=new();
    private readonly Dictionary<string,string> displayedText=new();
    private bool loading;
    private JObject basis=new();
    public event Action? Changed;
    public TeamPlanningRequirementsEditor()
    {
        Name="TeamRequirementsCard";Spacing=8;
        Children.Add(TeamPlanningUi.Title("比赛需求卡",17));
        Children.Add(TeamPlanningUi.Note("直接修改有误的信息；空白表示未知。缺项和冲突会在卡片下方一次列出。"));
        var primary=new WrapPanel();
        foreach(var field in new[]{("server","服务器：国服 / 日服 / 台服",110),("event_name","比赛名称",200),("event_mode","比赛模式：冠军赛 / 英雄纪 / 自定",230),("venue","场地：如东京",150),("surface","地面：草地 / 泥地",160),("distance_m","距离（米）",110),("team_size","团队人数 1～9",120)})Add(primary,field.Item1,field.Item2,field.Item3);
        Children.Add(primary);
        var optional=new WrapPanel();
        foreach(var field in new[]{("edition.year","年份",95),("edition.month","月份",85),("edition.index","届次",85),("distance_category","距离类型：短 / 英里 / 中 / 长",235),("direction","方向：左回 / 右回 / 直线",200),("season","季节",105),("weather","天气",105),("track_condition","场地状况",120),("styles.allowed","允许跑法（空白＝不限）",220),("styles.preferred","优先跑法",190),("styles.excluded","排除跑法",190),("styles.counts.nige","逃：人数",100),("styles.counts.senko","先行：人数",100),("styles.counts.sashi","差：人数",100),("styles.counts.oikomi","追：人数",100)})Add(optional,field.Item1,field.Item2,field.Item3);
        Children.Add(new Expander{Header="届次、天气与跑法约束",Content=optional,IsExpanded=false});
        Children.Add(TeamPlanningUi.Note("跑法可填：逃 / 先行 / 差 / 追，或 nige / senko / sashi / oikomi；多项用逗号分隔。"));
    }
    private void Add(Panel parent,string key,string title,int width)
    {
        var box=new TextBox{Name="TeamRequirement_"+key.Replace('.','_'),Watermark="未知",Width=width};fields[key]=box;displayedText[key]="";
        box.TextChanged+=(_,_)=>{var text=box.Text??"";if(loading||displayedText[key]==text)return;displayedText[key]=text;edits.Add(key);Changed?.Invoke();};parent.Children.Add(TeamPlanningUi.Field(title,box));
    }
    private static readonly Dictionary<string,Dictionary<string,string>> Labels=new(){
        ["server"]=new(){["cn"]="国服",["jp"]="日服",["tw"]="台服"},
        ["event_mode"]=new(){["champions_meeting"]="冠军赛",["loh"]="英雄纪",["team_arena"]="竞技场",["custom"]="自定"},
        ["surface"]=new(){["turf"]="草地",["dirt"]="泥地"},
        ["venue"]=new(){["tokyo"]="东京",["nakayama"]="中山",["kyoto"]="京都",["hanshin"]="阪神",["chukyo"]="中京",["sapporo"]="札幌",["hakodate"]="函馆",["fukushima"]="福岛",["niigata"]="新潟",["kokura"]="小仓",["oi"]="大井"},
        ["distance_category"]=new(){["sprint"]="短距离",["mile"]="英里",["middle"]="中距离",["long"]="长距离"},
        ["direction"]=new(){["left"]="左回",["right"]="右回",["straight"]="直线"},
        ["season"]=new(){["spring"]="春",["summer"]="夏",["autumn"]="秋",["winter"]="冬"},
        ["weather"]=new(){["sunny"]="晴",["cloudy"]="多云",["rainy"]="雨",["snowy"]="雪"},
        ["track_condition"]=new(){["good"]="良",["yielding"]="稍重",["soft"]="重",["heavy"]="不良"}
    };
    private static readonly HashSet<string> Numeric=new(){"distance_m","team_size","edition.year","edition.month","edition.index","styles.counts.nige","styles.counts.senko","styles.counts.sashi","styles.counts.oikomi"};
    public void Load(JObject value)
    {
        loading=true;try{basis=(JObject)value.DeepClone();edits.Clear();foreach(var field in fields){var token=value.SelectToken(field.Key);var display=token is JArray array?string.Join("，",array.Select(TeamPlanningUi.Str)):Labels.TryGetValue(field.Key,out var labels)&&labels.TryGetValue(TeamPlanningUi.Str(token),out var label)?label:TeamPlanningUi.Str(token);displayedText[field.Key]=display;field.Value.Text=display;}}finally{loading=false;}
    }
    public JObject Read()
    {
        var result=(JObject)basis.DeepClone();result["schema_version"]="uma.team_requirements.v1";
        foreach(var field in fields){var text=(field.Value.Text??"").Trim();if(Labels.TryGetValue(field.Key,out var labels))text=labels.FirstOrDefault(x=>x.Value==text).Key??text;JToken token;
            if(field.Key is "styles.allowed" or "styles.preferred" or "styles.excluded"){
                var styles=TeamPlanningUi.Tokens(text).Select(x=>x switch{"逃"=>"nige","先" or "先行"=>"senko","差" or "差行"=>"sashi","追" or "追込" or "追入"=>"oikomi",_=>x}).Distinct().ToArray();token=styles.Length==0&&field.Key=="styles.allowed"?JValue.CreateNull():new JArray(styles);
            }else if(Numeric.Contains(field.Key))token=text.Length==0?JValue.CreateNull():int.TryParse(text,out var number)?new JValue(number):new JValue(text);
            else token=text.Length==0?JValue.CreateNull():new JValue(text);
            var parts=field.Key.Split('.');JObject owner=result;for(var i=0;i<parts.Length-1;i++){if(owner[parts[i]] is not JObject child){child=new JObject();owner[parts[i]]=child;}owner=child;}if(field.Key.StartsWith("styles.counts.",StringComparison.Ordinal)&&text.Length==0)owner.Remove(parts[^1]);else owner[parts[^1]]=token;
        }
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
    public static TextBlock Note(string text)=>new(){Text=text,FontSize=12,TextWrapping=TextWrapping.Wrap};
    public static TextBlock Title(string text,int size=16)=>new(){Text=text,FontSize=size,FontWeight=FontWeight.SemiBold,TextWrapping=TextWrapping.Wrap};
    public static Control Field(string title,Control body)=>new StackPanel{Spacing=4,Margin=new Thickness(0,0,10,7),Children={Note(title),body}};
    public static Border Card(Control body){var card=new Border{Child=body,Padding=new Thickness(12),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(10),Margin=new Thickness(0,2)};card.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));card.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));return card;}
    public static string Lines(JToken? value)=>value is JArray array?string.Join("、",array.Select(x=>x is JObject row?Str(row["name"]) is string n&&n.Length>0?n:Str(row["label"]):Str(x)).Where(x=>x.Length>0)):Str(value);
}
