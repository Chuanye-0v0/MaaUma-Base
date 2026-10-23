using System;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia;
using Avalonia.Threading;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class TeamPlanningView
{
    private JObject? memberRouteEditorBaseline,memberRouteEditorMember;
    private bool memberRouteEdited;
    private JObject? generatedRequestTeam;
    private string generatedRequestText="";
    private JObject? savedPriorTeam;
    private string savedPriorId="";
    private void BindGeneratedRequestOrigin(){generatedRequestTeam=team;generatedRequestText=TeamPlanningUi.Str(team["request_text"]);}
    private bool OriginalRequest(out string original)
    {
        if(ReferenceEquals(generatedRequestTeam,team)){original=generatedRequestText;return true;}
        var saved=team["request_text"];var planText=TeamPlanningData.Plan(team)?["request_text"];
        original=TeamPlanningUi.Str(saved);
        // A legacy save may contain edited prose rather than its generation
        // input. Only an existing matching nonempty plan field establishes it.
        return saved?.Type==JTokenType.String&&planText?.Type==JTokenType.String&&original.Length>0&&original==TeamPlanningUi.Str(planText);
    }
    private JArray SelectedSourceIds()=>sourceChoices.Count>0?new JArray(sourceChoices.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key)):research?["research_id"]!=null&&JToken.DeepEquals(research["research_id"],team["research_id"])?(JArray)(team["selected_source_ids"] as JArray??new()).DeepClone():new JArray();
    private bool UnchangedMemberRoute(JObject current)=>ReferenceEquals(memberRouteEditorMember,selectedMember)&&!memberRouteEdited&&memberRouteEditorBaseline!=null&&JToken.DeepEquals(current,memberRouteEditorBaseline);
    private JObject MemberRoutesForSave()
    {
        var routes=Clone(team["member_routes"] as JObject??new());
        if(selectedMember!=null&&MemberId(selectedMember).Length>0){var current=route.ReadDraft();if(!UnchangedMemberRoute(current))routes[MemberId(selectedMember)]=current;}
        return routes;
    }
    private JObject TeamForSave()=>new()
    {
        ["id"]=team["id"]?.DeepClone(),["name"]=(teamName.Text??"").Trim(),["request_text"]=description.Text??"",["account_id"]=AccountId,["library_id"]=planningState["library_id"]?.DeepClone(),["requirements"]=requirementsEditor.Read(),["research_id"]=research?["research_id"]?.DeepClone(),["selected_source_ids"]=SelectedSourceIds(),["team_plan"]=team["team_plan"]?.DeepClone(),["member_routes"]=MemberRoutesForSave()
    };
    private bool NeedsSeparateDraft(JObject edited)
    {
        var plan=TeamPlanningData.Plan(team);if(plan==null)return false;
        static string[] Sources(JToken? token)=>(token as JArray??new()).Select(TeamPlanningUi.Str).OrderBy(x=>x,StringComparer.Ordinal).ToArray();
        return !OriginalRequest(out var original)||TeamPlanningUi.Str(edited["request_text"])!=original||!JToken.DeepEquals(edited["requirements"],plan["requirements"])||TeamPlanningUi.Str(edited["research_id"])!=TeamPlanningUi.Str(plan["research_id"])||!Sources(edited["selected_source_ids"]).SequenceEqual(Sources(plan["selected_source_ids"]))||TeamPlanningUi.Str(edited["account_id"])!=TeamPlanningUi.Str(team["account_id"])||TeamPlanningUi.Str(edited["library_id"])!=TeamPlanningUi.Str(team["library_id"]);
    }
    private JObject PriorPlanForSave()
    {
        var prior=Clone(team);var plan=TeamPlanningData.Plan(team)!;
        if(ReferenceEquals(savedPriorTeam,team)&&TeamPlanningUi.Str(prior["id"]).Length==0&&savedPriorId.Length>0)prior["id"]=savedPriorId;
        prior["requirements"]=plan["requirements"]?.DeepClone();prior["research_id"]=plan["research_id"]?.DeepClone();prior["selected_source_ids"]=plan["selected_source_ids"]?.DeepClone();prior["member_routes"]=MemberRoutesForSave();
        if(OriginalRequest(out var original))prior["request_text"]=original;
        if(TeamPlanningUi.Str(prior["name"]).Length==0)prior["name"]=TeamPlanningUi.Str(plan["title"]) is string title&&title.Length>0?title:"上次团队方案";
        return prior;
    }
    private async Task ReadSavedTeams()
    {
        var response=await request(new JObject{["action"]="parent_planning_team_state"});ApplySavedTeams(response);
    }
    private void ApplySavedTeams(JObject response)
    {
        teamRevision=TeamPlanningUi.Int(response["revision"])??0;var selected=(savedTeams.SelectedItem as Choice)?.Id;var values=(response["teams"] as JArray??new()).OfType<JObject>().Select(x=>new Choice(TeamPlanningUi.Str(x["id"]),TeamPlanningUi.Str(x["name"]),x)).ToArray();savedTeams.ItemsSource=values;savedTeams.SelectedItem=values.FirstOrDefault(x=>x.Id==selected||x.Id==TeamPlanningUi.Str(team["id"]));
    }
    public async Task SaveTeam()
    {
        if(busy)return;var value=TeamForSave();if(TeamPlanningUi.Str(value["name"]).Length==0){status.Text="请给团队草稿填写名称。";return;}
        var split=NeedsSeparateDraft(value);var prior=split?PriorPlanForSave():null;
        if(split){value["id"]=null;value["team_plan"]=null;value["member_routes"]=new JObject();var name=TeamPlanningUi.Str(value["name"]);const string suffix=" · 待生成草稿";value["name"]=(name.Length>160-suffix.Length?name[..(160-suffix.Length)]:name)+suffix;}
        var snapshot=Snapshot();var at=generation;var priorSaved=false;Lock(true);try{
            if(prior!=null){var kept=await request(new JObject{["action"]="parent_planning_team_save",["expected_revision"]=teamRevision,["team"]=prior});if(TeamPlanningData.IsFailure(kept)||kept["saved_team"] is not JObject savedPrior){ShowPlanningFailure(TeamPlanningData.Failure(kept,"原团队方案尚未保存，当前编辑保留；请重试。"));return;}ApplySavedTeams(kept);savedPriorTeam=team;savedPriorId=TeamPlanningUi.Str(savedPrior["id"]);priorSaved=true;}
            var response=await request(new JObject{["action"]="parent_planning_team_save",["expected_revision"]=teamRevision,["team"]=value});if(TeamPlanningData.IsFailure(response)||response["saved_team"] is not JObject){ShowPlanningFailure(TeamPlanningData.Failure(response,priorSaved?"原团队方案和成员系谱已保存；待生成草稿尚未保存，当前编辑保留，请重试。":"团队草稿尚未保存，当前编辑保留；请重试。"));return;}ApplySavedTeams(response);
            if(!Current(snapshot,at)){status.Text="点击时的团队快照已保存；当前编辑已变化，保留当前编辑。";return;}
            var memberId=selectedMember==null?null:MemberId(selectedMember);var originKnown=OriginalRequest(out var originText);team=Clone(response["saved_team"] as JObject??value);if(!split&&originKnown){generatedRequestTeam=team;generatedRequestText=originText;}selectedMember=TeamPlanningData.Members(team).FirstOrDefault(x=>MemberId(x)==memberId);if(!split&&selectedMember!=null)memberRouteEditorMember=selectedMember;
            if(split){loading=true;try{teamName.Text=TeamPlanningUi.Str(team["name"]);}finally{loading=false;}memberWorkspace.IsVisible=false;workspaces.SelectedIndex=0;}
            RenderTeam();generation++;status.Text=split?"当前编辑已另存为待生成草稿；原团队方案与各成员系谱已分别保留在已保存团队中。":"团队草稿已保存在本机；成员系谱与原配种方案分别保留。";
        }catch(InvalidOperationException e){ShowPlanningFailure(e.Message+(priorSaved?"\n原团队方案和成员系谱已保存；当前待生成编辑仍在界面，请重试保存。":""));}catch{ShowPlanningFailure(priorSaved?"原团队方案和成员系谱已保存；待生成草稿尚未保存，当前编辑保留，请重试。":"团队保存未完成，当前草稿保留。若其他窗口已修改，请刷新后再保存。");}finally{Lock(false);}
    }
    public async Task LoadTeam()
    {
        if(busy||savedTeams.SelectedItem is not Choice choice||choice.Row==null)return;var priorMemberId=selectedMember==null?null:MemberId(selectedMember);RememberPlanningDraft();StoreMemberRoute();loading=true;try{
            team=Clone(choice.Row);teamName.Text=TeamPlanningUi.Str(team["name"]);requirements=Clone(team["requirements"] as JObject??new());requirementsEditor.Load(requirements);displayedDescription=TeamPlanningUi.Str(team["request_text"]);description.Text=displayedDescription;displayedGuideUrls="";guideUrls.Text=displayedGuideUrls;research=team["research_id"]==null||team["research_id"]!.Type==JTokenType.Null?null:new JObject{["research_id"]=team["research_id"]!.DeepClone()};researchContext="";sourceChoices.Clear();sources.Children.Clear();if(research!=null)sources.Children.Add(TeamPlanningUi.Note("此草稿保留检索编号与所选证据；重新检索后可查看日期和适用性。"));var wanted=TeamPlanningUi.Str(team["account_id"]);var options=(account.ItemsSource as System.Collections.Generic.IEnumerable<Choice>??[]).ToArray();if(wanted.Length>0&&options.All(x=>x.Id!=wanted)){options=options.Append(new Choice(wanted,"原账号（本地目录未收录） · "+wanted)).ToArray();account.ItemsSource=options;}account.SelectedItem=options.FirstOrDefault(x=>x.Id==wanted)??options.FirstOrDefault();selectedMember=null;memberWorkspace.IsVisible=false;seedCandidates.Children.Clear();clarifications.Text="";
        }finally{loading=false;}generation++;RefreshRouteEntries();await inventory.Load();RenderTeam();var rows=TeamPlanningData.Members(team);var chosen=rows.FirstOrDefault(x=>MemberId(x)==priorMemberId)??rows.FirstOrDefault();if(chosen!=null)await SelectMember(chosen);else workspaces.SelectedIndex=0;draftPanel.IsVisible=false;status.Text=rows.Length==0?"已载入比赛需求草稿；尚未生成团队，可继续补充需求或生成参考方案。":"已载入团队与各成员草稿。";
    }
    private async Task RecordsSaved()
    {
        StoreMemberRoute();var selected=selectedMember;var id=AccountId;
        var response=await request(new JObject{["action"]="parent_planning_state"});
        planningState=Clone(response);generation++;seedCandidates.Children.Clear();RefreshRouteEntries();
        if(selectedMember==selected&&id==AccountId)status.Text="实际胜场已保存并刷新本地资料；团队建议需按最新资料重新生成。";
    }
    private string MemberId(JObject member)=>TeamPlanningUi.Str(member["member_id"]);
    private void StoreMemberRoute()
    {
        if(selectedMember==null)return;var id=MemberId(selectedMember);if(id.Length==0)return;var current=route.ReadDraft();if(UnchangedMemberRoute(current))return;if(team["member_routes"] is not JObject routes){routes=new JObject();team["member_routes"]=routes;}routes[id]=current;memberRouteEdited=true;
    }
    private async Task SelectMember(JObject member)
    {
        StoreMemberRoute();selectedMember=member;generation++;seedCandidates.Children.Clear();memberWorkspace.IsVisible=true;candidatePanel.IsVisible=false;memberDetails.IsExpanded=false;memberHeading.Text=MemberName(member)+" · "+Style(TeamPlanningUi.Str(member["running_style"]));suppressRoute=true;try{await route.LoadCatalog();if(selectedMember!=member)return;RefreshRouteEntries();route.CloseInspector();var saved=(team["member_routes"] as JObject)?[MemberId(member)] as JObject;route.LoadDraft(saved);route.SetTargetEditor(TeamPlanningUi.Note("当前成员："+MemberName(member)+"。分工与跑法可在当前成员详情中编辑。"));memberRouteEditorMember=member;memberRouteEditorBaseline=Clone(route.ReadDraft());memberRouteEdited=false;if((route.ReadDraft()["nodes"] as JArray??new()).Any(x=>x.Type!=JTokenType.Null))await route.Evaluate();if(selectedMember!=member)return;}finally{suppressRoute=false;}RenderTeam();workspaces.SelectedIndex=1;teamScroll.Offset=new Vector(0,0);Dispatcher.UIThread.Post(()=>{if(selectedMember==member)teamScroll.Offset=new Vector(0,0);});
    }
    private string MemberName(JObject member)
    {
        var name=TeamPlanningUi.Str(member["name"]);if(name.Length>0)return name;var id=TeamPlanningUi.Int(member["character_id"]);var row=characters.FirstOrDefault(x=>TeamPlanningUi.Int(x["character_id"])==id);return TeamPlanningUi.Str(row?["name"]) is string label&&label.Length>0?label:id is int number?"角色 #"+number:TeamPlanningUi.Str(member["member_id"]);
    }
}
