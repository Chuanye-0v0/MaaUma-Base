using System;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>Offline skill reference; exact catalog mappings alone can add a planning target.</summary>
public sealed class UmaSkillReferenceView : UserControl
{
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<string,bool> addTarget;
    private readonly TextBox query=new(){Name="PlannerSkillQuery",Watermark="搜索技能名称或关键词，标点从结果中选择"};
    private readonly Button search=new(){Name="PlannerSkillSearch",Content="查询"};
    private readonly ListBox list=new(){Name="PlannerSkillResults",MaxHeight=235,MinHeight=0,VerticalAlignment=VerticalAlignment.Top,IsVisible=false};
    private readonly StackPanel detail=new(){Name="PlannerSkillDetail",Spacing=8};
    private readonly TextBlock status=Label("输入技能名称或关键词后查询；资料在本机读取。",12);
    private int searchVersion,detailVersion;
    public UmaSkillReferenceView(Func<JObject,Task<JObject>> request,Func<string,bool> addTarget)
    {
        this.request=request;this.addTarget=addTarget;
        var root=new StackPanel{Spacing=10,Margin=new Thickness(0,10,0,0)};
        var row=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};row.Children.Add(query);Grid.SetColumn(search,1);row.Children.Add(search);
        root.Children.Add(Label("技能查询与来源",18));root.Children.Add(row);root.Children.Add(status);root.Children.Add(list);root.Children.Add(detail);
        list.ItemTemplate=new FuncDataTemplate<JObject>((skill,_)=>new StackPanel{Spacing=3,Children={Label(Scalar(skill?["name"]),14),Label(string.Join(" · ",new[]{Friendly(Scalar(skill?["rarity"])),Friendly(Scalar(skill?["type"])),Scalar(skill?["db_id"]).Length>0?"编号 "+Scalar(skill?["db_id"]):"",Scalar(skill?["description"])}.Where(x=>x.Length>0)),11)}});
        list.SelectionChanged+=async(_,_)=>{if(list.SelectedItem is JObject skill)await LoadDetail(skill);};
        search.Click+=async(_,_)=>await Search();query.KeyDown+=async(_,e)=>{if(e.Key==Avalonia.Input.Key.Enter){e.Handled=true;await Search();}};
        Content=new ScrollViewer{Content=root,HorizontalScrollBarVisibility=Avalonia.Controls.Primitives.ScrollBarVisibility.Disabled};
    }
    private static TextBlock Label(string value,int size=13)=>new(){Text=value,FontSize=size,TextWrapping=TextWrapping.Wrap};
    private static string Scalar(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer or JTokenType.Float?value.ToString():"";
    private static string Lines(JToken? value)=>value is JArray rows?string.Join("；",rows.Select(x=>Scalar(x).Length>0?Scalar(x):Scalar((x as JObject)?["text"])).Where(x=>x.Length>0)):Scalar(value);
    private static string Friendly(string value)=>value switch {
        "unique"=>"固有","gold"=>"稀有","white" or "normal"=>"普通","green"=>"绿色技能",
        "speed"=>"速度","acceleration"=>"加速度","recovery" or "heal"=>"回复","passive"=>"被动","debuff"=>"妨碍",
        "stamina"=>"耐力","power"=>"力量","guts"=>"毅力","intelligence" or "wisdom"=>"智力",_=>value};
    private static string ConditionToken(string value)=>value switch {
        "any"=>"不限","short" or "sprint"=>"短途","mile"=>"英里","medium" or "middle"=>"中距离","long"=>"长距离",
        "front_runner" or "nige" or "escape"=>"逃","leader" or "senko"=>"先行","betweener" or "sashi"=>"差","chaser" or "oikomi"=>"追",
        "corner"=>"弯道","straight"=>"直线","final_straight"=>"最终直线","final_corner"=>"最终弯道","early"=>"序盘","late"=>"后半段","end"=>"终盘",_=>"其他约束（详见触发说明）"};
    private static string Conditions(JToken? value)
    {
        if(value is JObject fields)return string.Join("；",fields.Properties().Where(p=>p.Name is "distance" or "style" or "phase_hint").Select(p=>
            (p.Name=="distance"?"距离":p.Name=="style"?"跑法":"赛段")+"："+string.Join("、",(p.Value is JArray rows?rows:new JArray(p.Value.DeepClone())).Select(x=>ConditionToken(Scalar(x))).Distinct())));
        if(value is JArray list)return string.Join("；",list.Select(x=>x is JObject?Conditions(x):Scalar(x)).Where(x=>x.Length>0));
        return Scalar(value);
    }
    public async Task Search()
    {
        var version=++searchVersion;++detailVersion;search.IsEnabled=false;detail.Children.Clear();status.Text="正在查询本地技能资料…";
        try
        {
            var result=await request(new JObject{["action"]="parent_planner_skill_search",["query"]=query.Text??"",["limit"]=30});
            if(version!=searchVersion)return;
            var rows=(result["skills"] as JArray??new()).OfType<JObject>().ToArray();list.SelectedItem=null;list.ItemsSource=rows;list.Height=Math.Min(235,rows.Length*72+8);list.IsVisible=rows.Length>0;
            status.Text=rows.Length==0?"没有找到对应技能，可试试名称中的几个字。":$"找到 {Scalar(result["total"])} 项，显示 {rows.Length} 项；选择技能查看详情。";
        }
        catch(InvalidOperationException e){if(version==searchVersion)status.Text=e.Message;}
        catch{if(version==searchVersion)status.Text="本地技能资料暂不可读，请检查资料安装后重试。";}
        finally{if(version==searchVersion)search.IsEnabled=true;}
    }
    private async Task LoadDetail(JObject choice)
    {
        var version=++detailVersion;detail.Children.Clear();status.Text="正在读取技能详情…";
        try
        {
            var result=await request(new JObject{["action"]="parent_planner_skill_detail",["skill_id"]=choice["id"]?.DeepClone()});
            if(version!=detailVersion)return;
            var skill=result["skill"] as JObject??throw new InvalidOperationException("技能详情未读完整，请重新选择。");
            var body=new StackPanel{Spacing=7};body.Children.Add(Label(Scalar(skill["name"]),19));
            foreach(var field in new[]{("description","效果"),("trigger","触发"),("precondition","前置条件"),("conditions","条件"),("type","类型"),("rarity","稀有度"),("cost","本技能Pt"),("total_cost","含前置总Pt")})
            {var value=field.Item1=="conditions"?Conditions(skill[field.Item1]):Lines(skill[field.Item1]);if(field.Item1 is "type" or "rarity")value=Friendly(value);if(value.Length>0)body.Children.Add(Label(field.Item2+"："+value));}
            var mapped=Scalar(result["mapped_target_id"]);if(mapped.Length==0)mapped=Scalar(skill["mapped_target_id"]);
            var add=new Button{Name="PlannerSkillAddTarget",Content="加入规划目标",IsEnabled=mapped.Length>0};var mapping=Label(mapped.Length>0?"已对应本地规划目录，加入后可设置星数与优先级。":"此技能暂未映射到本地规划目录，可查看资料；不会按相似名称自动加入目标。",12);
            var targetId=mapped;add.Click+=(_,_)=>{if(targetId.Length>0)mapping.Text=addTarget(targetId)?"已加入种马素材目标清单。":"该技能未在当前规划目录中找到，请核对资料。";};
            body.Children.Add(add);body.Children.Add(mapping);var mappedName=Scalar(result["mapped_target_name"]);if(mappedName.Length>0)body.Children.Add(Label("规划目录："+mappedName,12));detail.Children.Add(Card(body));
            RenderSources(result["sources"] as JObject??new());
            var versionText=Scalar(result["data_version"]);status.Text="本地技能资料"+(versionText.Length>0?" · "+versionText:"")+"；来源仅供查找与补种参考。";
        }
        catch(InvalidOperationException e){if(version==detailVersion)status.Text=e.Message;}
        catch{if(version==detailVersion)status.Text="技能详情暂未读取成功，请重新选择。";}
    }
    private void RenderSources(JObject sources)
    {
        var body=new StackPanel{Spacing=6};body.Children.Add(Label("马娘与支援卡来源",17));body.Children.Add(Label("静态来源不代表账号已持有对应卡片，也不保证事件或技能一定获得。",12));
        var trainees=(sources["trainees"] as JArray??new()).OfType<JObject>().DistinctBy(row=>string.Join("|",new[]{Scalar(row["card_id"]),Scalar(row["name"]),Scalar(row["outfit"]),Scalar(row["awakening_rank"])})).ToArray();body.Children.Add(Label("马娘",15));
        if(trainees.Length==0)body.Children.Add(Label("暂未整理对应马娘来源。",12));
        foreach(var row in trainees){var title=string.Join(" · ",new[]{Scalar(row["name"]),Scalar(row["outfit"])}.Where(x=>x.Length>0));var rank=Scalar(row["awakening_rank"]);body.Children.Add(Label(title+(rank.Length>0?" · 觉醒 "+rank:"")));}
        var cards=(sources["support_cards"] as JArray??new()).OfType<JObject>().ToArray();body.Children.Add(Label("支援卡",15));if(cards.Length==0)body.Children.Add(Label("暂未整理对应支援卡来源。",12));
        foreach(var row in cards){body.Children.Add(Label(string.Join(" · ",new[]{Friendly(Scalar(row["rarity"])),Scalar(row["name"]),Friendly(Scalar(row["type"]))}.Where(x=>x.Length>0))));foreach(var key in new[]{"methods","event_notes"}){var notes=Lines(row[key]);if(notes.Length>0)body.Children.Add(Label(notes,12));}}
        foreach(var note in (sources["source_notes"] as JArray??new()).Select(Scalar).Where(x=>x.Length>0))body.Children.Add(Label(note,12));detail.Children.Add(Card(body));
    }
    private static GlassCard Card(Control content)=>new(){Content=content,Padding=new Thickness(14),CornerRadius=new CornerRadius(10),IsAnimated=false,IsOpaque=true};
}
