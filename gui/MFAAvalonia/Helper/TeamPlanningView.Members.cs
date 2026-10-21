using System;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Views.UserControls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class TeamPlanningView
{
    private static string Style(string value)=>value switch{"nige"=>"逃","senko"=>"先行","sashi"=>"差","oikomi"=>"追",_=>"未指定"};
    private static string SkillAcquisitionLabel(string value)=>value switch
    {
        "inherited_unique"=>"继承固有", "character_native"=>"本体固有",
        "training_or_support"=>"育成/支援卡取得", "inheritable_white"=>"白因子",
        "training_or_unknown"=>"育成或来源待核对", _=>"获取方式待核对"
    };
    private void RenderTeam()
    {
        members.Children.Clear();var plan=team["team_plan"] as JObject??new();var rows=(plan["members"] as JArray??new()).OfType<JObject>().ToArray();teamSummary.Text=rows.Length==0?"尚无团队。请先在比赛需求与资料中生成，或载入已保存的草稿。":TeamPlanningUi.Str(plan["title"])+" · "+rows.Length+" 匹成员";
        teamSummary.IsVisible=rows.Length==0;if(workspaces.ItemsSource is TabItem[] workspaceTabs)workspaceTabs[1].Header=rows.Length==0?"团队系谱":"团队系谱 · "+rows.Length+" 匹";
        foreach(var member in rows){var identity=new Grid{ColumnDefinitions=new("Auto,*"),ColumnSpacing=7};var variant=TeamPlanningUi.Str(member["variant_id"]);var portrait=variant.Length==6&&variant.All(char.IsDigit)?Path.Combine(AppContext.BaseDirectory,"resource","portraits",variant+".png"):"";identity.Children.Add(new DisplayIcon{IconSource=File.Exists(portrait)?portrait:"?",IconSize=34});var labels=new StackPanel{Spacing=3};var chosen=selectedMember!=null&&MemberId(member)==MemberId(selectedMember);var title=TeamPlanningUi.Title((chosen?"当前 · ":"")+MemberName(member),13);title.TextWrapping=TextWrapping.NoWrap;title.TextTrimming=TextTrimming.CharacterEllipsis;title.MaxWidth=130;labels.Children.Add(title);var subtitle=TeamPlanningUi.Note(Style(TeamPlanningUi.Str(member["running_style"]))+" · "+TeamPlanningUi.Str(member["role"]));subtitle.TextWrapping=TextWrapping.NoWrap;subtitle.TextTrimming=TextTrimming.CharacterEllipsis;subtitle.MaxWidth=130;labels.Children.Add(subtitle);Grid.SetColumn(labels,1);identity.Children.Add(labels);
            var use=new Button{Name="TeamMemberSelect",Tag=MemberId(member),Content=identity,Width=200,MinHeight=64,Margin=new Thickness(0,0,7,5),HorizontalContentAlignment=HorizontalAlignment.Stretch};if(chosen){use.BorderBrush=Brushes.DodgerBlue;use.BorderThickness=new Thickness(2);}ToolTip.SetTip(use,MemberName(member)+" · "+Style(TeamPlanningUi.Str(member["running_style"])));use.Click+=async(_,_)=>await SelectMember(member);members.Children.Add(use);
        }
        RenderMemberDetails();
    }
    private void RenderMemberDetails()
    {
        var member=selectedMember;if(member==null){memberDetails.Content=null;return;}
        var body=new StackPanel{Spacing=9};body.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(team["team_plan"]?["summary"])));
        var role=new TextBox{Name="TeamMemberRole",Text=TeamPlanningUi.Str(member["role"]),Width=240,Watermark="主力、领跑、辅助等分工"};role.TextChanged+=(_,_)=>{var value=role.Text??"";if(value==TeamPlanningUi.Str(member["role"]))return;member["role"]=value;RefreshMemberLabel(member);Invalidate("当前成员分工已修改，重新匹配时采用当前方案。",false);};
        var styles=new[]{new Choice("nige","逃"),new Choice("senko","先行"),new Choice("sashi","差"),new Choice("oikomi","追")};var style=new ComboBox{Name="TeamMemberStyle",ItemsSource=styles,Width=130};style.SelectedItem=styles.FirstOrDefault(x=>x.Id==TeamPlanningUi.Str(member["running_style"]));style.SelectionChanged+=(_,_)=>{var value=(style.SelectedItem as Choice)?.Id;if(value==null||value==TeamPlanningUi.Str(member["running_style"]))return;member["running_style"]=value;RefreshMemberLabel(member);Invalidate("当前成员跑法已修改，重新匹配时采用当前方案。",false);};var edit=new WrapPanel();edit.Children.Add(TeamPlanningUi.Field("分工",role));edit.Children.Add(TeamPlanningUi.Field("跑法",style));body.Children.Add(edit);
        var aptitudes=(member["target_aptitudes"] as JArray??new()).OfType<JObject>().Select(x=>TeamPlanningUi.Str(x["factor_key"]).Replace("red:","")+" "+TeamPlanningUi.Str(x["target_rank"]));body.Children.Add(TeamPlanningUi.Note("适性目标："+string.Join("、",aptitudes)));
        var skills=(member["core_skills"] as JArray??new()).OfType<JObject>().Select(x=>(TeamPlanningUi.Str(x["name"]) is string name&&name.Length>0?name:TeamPlanningUi.Str(x["raw_name"]))+"（"+SkillAcquisitionLabel(TeamPlanningUi.Str(x["acquisition"]))+"）");body.Children.Add(TeamPlanningUi.Note("核心技能："+string.Join("、",skills)));
        body.Children.Add(TeamPlanningUi.Note("配卡参考："+string.Join("；",(member["support_cards"] as JArray??new()).OfType<JObject>().Select(CardLabel))));var gaps=TeamPlanningUi.Lines(member["missing_data"]);if(gaps.Length>0)body.Children.Add(TeamPlanningUi.Note("缺口 / 未知："+gaps));var notes=TeamPlanningUi.Lines(member["notes"]);if(notes.Length>0)body.Children.Add(TeamPlanningUi.Note(notes));memberDetails.Content=body;
    }
    private void RefreshMemberLabel(JObject member)
    {
        var button=members.Children.OfType<Button>().FirstOrDefault(x=>x.Tag?.ToString()==MemberId(member));if(button?.Content is Grid grid&&grid.Children.OfType<StackPanel>().FirstOrDefault() is StackPanel labels&&labels.Children.OfType<TextBlock>().LastOrDefault() is TextBlock subtitle)subtitle.Text=Style(TeamPlanningUi.Str(member["running_style"]))+" · "+TeamPlanningUi.Str(member["role"]);
        if(selectedMember==member)memberHeading.Text=MemberName(member)+" · "+Style(TeamPlanningUi.Str(member["running_style"]));
    }
    private static string CardLabel(JObject card)
    {
        var name=TeamPlanningUi.Str(card["name"]);if(name.Length==0)name=TeamPlanningUi.Str(card["raw_name"]);if(name.Length==0)name="支援卡 #"+TeamPlanningUi.Str(card["card_id"]);var availability=TeamPlanningUi.Str(card["availability"]) switch{"owned"=>"持有","borrowable"=>"可借","not_owned"=>"未持有",_=>"持有未知"};return name+" · "+((bool?)card["borrowed"]==true?"借卡 · ":"")+availability+" · 等级 "+(TeamPlanningUi.Int(card["level"])?.ToString()??"未知")+" / 突破 "+(TeamPlanningUi.Int(card["limit_break"])?.ToString()??"未知");
    }
    private async Task<bool> AiReady(JObject snapshot,int at)
    {
        var response=await request(new JObject{["action"]="parent_planning_ai_status"});if(!Current(snapshot,at)){status.Text="等待接口状态期间上下文变化，已取消本次请求。";return false;}if((bool?)response["configured"]!=true){status.Text="尚未配置 AI 接口；请使用现有“育成中心 → AI 决策 → AI 接口设置”后重试。";return false;}return true;
    }
    private bool InputsReady()
    {
        if(!inventory.IsReady){status.Text="当前账号持有资料尚未读取完成，请稍后或刷新后重试。";return false;}if(inventory.HasUnsavedChanges){status.Text="账号持有资料尚未保存，请先保存该类别再生成方案。";return false;}return true;
    }
    public async Task ProposeTeam()
    {
        if(busy||!InputsReady())return;if(research==null||TeamPlanningUi.Str(research["research_id"]).Length==0||researchContext!=requirementsEditor.Read().ToString()){status.Text="请先整理需求卡并检索本地参考或攻略，采用与当前需求一致的资料。";return;}var snapshot=Snapshot();var at=generation;Lock(true);status.Text="正在检查已配置的 AI 接口…";try{
            if(!await AiReady(snapshot,at))return;status.Text="正在生成一套团队方案…";var command=new JObject{["action"]="parent_planning_ai_team_propose",["confirmed"]=true,["request_text"]=description.Text??"",["requirements"]=requirementsEditor.Read(),["research_id"]=research["research_id"]!.DeepClone(),["selected_source_ids"]=SelectedSourceIds(),["account_id"]=AccountId,["library_id"]=planningState["library_id"]?.DeepClone()};if(planningState["library_revision"]?.Type==JTokenType.Integer)command["expected_library_revision"]=planningState["library_revision"]!.DeepClone();if(AccountId!=null)command["expected_inventory_revision"]=inventory.Revision;var response=await request(command);if(!Current(snapshot,at)){status.Text="生成期间需求、账号或草稿变化，旧团队方案未套用。";return;}var plan=response["team_plan"] as JObject;if(plan==null){status.Text="本次未返回完整团队方案，当前草稿保留。";return;}var rows=(plan["members"] as JArray??new()).OfType<JObject>().ToArray();var count=TeamPlanningUi.Int(requirementsEditor.Read()["team_size"]);if(count==null||rows.Length!=count||rows.Any(x=>MemberId(x).Length==0)||rows.Select(MemberId).Distinct().Count()!=rows.Length){status.Text="返回的成员数量或编号与需求不一致，未采用旧结果。";return;}
            team=new JObject{["id"]=null,["name"]=(teamName.Text??"").Trim(),["account_id"]=AccountId,["library_id"]=planningState["library_id"]?.DeepClone(),["requirements"]=requirementsEditor.Read(),["research_id"]=research["research_id"]!.DeepClone(),["selected_source_ids"]=SelectedSourceIds(),["team_plan"]=plan.DeepClone(),["member_routes"]=new JObject()};selectedMember=null;memberWorkspace.IsVisible=false;generation++;RenderTeam();if(string.IsNullOrWhiteSpace(teamName.Text))teamName.Text=TeamPlanningUi.Str(plan["title"]);await SelectMember(((JArray)team["team_plan"]!["members"]!).OfType<JObject>().First());status.Text="已生成一套 "+rows.Length+" 匹团队，当前成员的系谱草稿已打开。";ShowMessages(response);
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="团队方案未生成，需求、资料与现有草稿已保留。";}finally{Lock(false);}
    }
    public async Task MatchMember()
    {
        if(busy||selectedMember==null||team["team_plan"] is not JObject plan||!InputsReady())return;if(TeamPlanningUi.Str(team["account_id"])!=(AccountId??"")){status.Text="此团队属于另一个规划账号，请载入对应账号或重新生成团队。";return;}StoreMemberRoute();var memberId=MemberId(selectedMember);var snapshot=Snapshot();var at=generation;Lock(true);status.Text="正在检查接口并匹配此成员的种马目标…";try{
            if(!await AiReady(snapshot,at))return;var command=new JObject{["action"]="parent_planning_ai_team_member_seed",["confirmed"]=true,["team_plan"]=plan.DeepClone(),["member_id"]=memberId,["account_id"]=AccountId,["library_id"]=planningState["library_id"]?.DeepClone(),["local_route"]=route.ReadDraft(),["research_id"]=team["research_id"]?.DeepClone()};var response=await request(command);if(!Current(snapshot,at)||selectedMember==null||MemberId(selectedMember)!=memberId){status.Text="成员、账号或草稿已变化，旧种马候选未采用。";return;}RenderSeedCandidates(response,snapshot,at,memberId);candidatePanel.IsVisible=true;candidatePanel.IsExpanded=true;Dispatcher.UIThread.Post(()=>candidatePanel.BringIntoView());status.Text="成员种马候选已返回；应用只载入该成员的七节点草稿。";ShowMessages(response);
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="种马匹配未完成，此成员的系谱草稿保留。";}finally{Lock(false);}
    }
    private void ShowMessages(JObject response)
    {
        var notes=(response["missing_data"] as JArray??new()).Concat(response["warnings"] as JArray??new()).Select(TeamPlanningUi.Str).Where(x=>x.Length>0).ToArray();if(notes.Length>0)status.Text+="\n"+string.Join("\n",notes);
    }
    private static Control MemberReview(JObject review)
    {
        var body=new StackPanel{Spacing=5};
        foreach(var item in (review["aptitudes"] as JArray??new()).OfType<JObject>()){
            var plan=item["plan"] as JObject??new();var state=TeamPlanningUi.Str(plan["status"]) switch{"satisfied"=>"已满足本地检查","unreachable_at_start"=>"开局不可达","unknown_baseline"=>"基准未知",_=>"仍需补充因子或资料"};
            body.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(item["factor_key"]).Replace("red:","")+" → "+TeamPlanningUi.Str(item["target_rank"])+"："+state+"；已确认 "+(TeamPlanningUi.Int(plan["known_stars"])?.ToString()??"未知")+" 星"));
        }
        foreach(var item in (review["skills"] as JArray??new()).OfType<JObject>()){
            var state=TeamPlanningUi.Str(item["status"]) switch{"factor_present"=>"已有因子素材，继承仍需核对","factor_missing"=>"缺少因子素材","factor_unknown"=>"因子资料未齐","skill_unverified"=>"技能未收录，获取方式待核对",_=>"需本体、训练或支援卡条件"};body.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(item["name"])+"："+state));
        }
        body.Children.Add(TeamPlanningUi.Note("未来培养目标不计为已获得；下位白因子不等于金技能本体。"));return body;
    }
    private void RenderSeedCandidates(JObject response,JObject snapshot,int at,string memberId)
    {
        seedCandidates.Children.Clear();foreach(var row in (response["candidates"] as JArray??new()).OfType<JObject>()){
            var candidateRoute=row["local_route"] as JObject;var review=row["local_review"] as JObject;var body=new StackPanel{Spacing=5};body.Children.Add(TeamPlanningUi.Title(TeamPlanningUi.Str(row["title"]),15));var summary=TeamPlanningUi.Note(TeamPlanningUi.Str(row["summary"]));summary.MaxLines=2;summary.TextTrimming=TextTrimming.CharacterEllipsis;body.Children.Add(summary);
            var details=new StackPanel{Spacing=5};details.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(row["summary"])));foreach(var node in (review?["route_nodes"] as JArray??new()).OfType<JObject>())details.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(node["label"])+"："+TeamPlanningUi.Str(node["status_label"])));
            var memberReview=row["member_review"] as JObject??response["member_review"] as JObject;if(memberReview!=null)details.Children.Add(MemberReview(memberReview));
            foreach(var warning in (row["warnings"] as JArray??new()).Select(TeamPlanningUi.Str))details.Children.Add(TeamPlanningUi.Note(warning));body.Children.Add(new Expander{Name="TeamCandidateDetails",Header="材料状态、缺口与本地复核",Content=details,IsExpanded=false});
            var apply=new Button{Name="TeamMemberSeedApply",Content="应用到此成员草稿",IsEnabled=candidateRoute!=null&&review!=null};apply.Click+=async(_,_)=>{if(!Current(snapshot,at)||selectedMember==null||MemberId(selectedMember)!=memberId){status.Text="成员或上下文已变化，请重新请求此成员的种马候选。";return;}if(candidateRoute==null||review==null)return;suppressRoute=true;try{route.LoadDraft(candidateRoute);StoreMemberRoute();}finally{suppressRoute=false;}generation++;seedCandidates.Children.Clear();candidatePanel.IsVisible=false;candidatePanel.IsExpanded=false;var appliedMember=selectedMember;var appliedGeneration=generation;await route.Evaluate();if(selectedMember!=appliedMember||generation!=appliedGeneration)return;teamScroll.Offset=new Vector(0,0);status.Text="已载入此成员草稿，尚未保存团队；原配种方案与种马库保持独立。";};body.Children.Add(apply);seedCandidates.Children.Add(TeamPlanningUi.Card(body));
        }
        if(seedCandidates.Children.Count==0)seedCandidates.Children.Add(TeamPlanningUi.Note("本次没有可应用候选，当前成员草稿保留。"));
    }
}
