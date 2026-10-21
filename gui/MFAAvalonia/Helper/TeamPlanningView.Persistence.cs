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
    private JArray SelectedSourceIds()=>sourceChoices.Count>0?new JArray(sourceChoices.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key)):research?["research_id"]!=null&&JToken.DeepEquals(research["research_id"],team["research_id"])?(JArray)(team["selected_source_ids"] as JArray??new()).DeepClone():new JArray();
    private JObject TeamForSave()
    {
        if(selectedMember!=null)StoreMemberRoute();return new JObject{["id"]=team["id"]?.DeepClone(),["name"]=(teamName.Text??"").Trim(),["request_text"]=description.Text??"",["account_id"]=AccountId,["library_id"]=planningState["library_id"]?.DeepClone(),["requirements"]=requirementsEditor.Read(),["research_id"]=research?["research_id"]?.DeepClone(),["selected_source_ids"]=SelectedSourceIds(),["team_plan"]=team["team_plan"]?.DeepClone(),["member_routes"]=team["member_routes"]?.DeepClone()??new JObject()};
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
        if(busy)return;var value=TeamForSave();if(TeamPlanningUi.Str(value["name"]).Length==0){status.Text="请给团队草稿填写名称。";return;}var snapshot=Snapshot();var at=generation;Lock(true);try{
            var response=await request(new JObject{["action"]="parent_planning_team_save",["expected_revision"]=teamRevision,["team"]=value});ApplySavedTeams(response);if(!Current(snapshot,at)){status.Text="点击时的团队快照已保存；当前编辑已变化，保留当前编辑。";return;}var memberId=selectedMember==null?null:MemberId(selectedMember);team=Clone(response["saved_team"] as JObject??value);selectedMember=(team["team_plan"]?["members"] as JArray??new()).OfType<JObject>().FirstOrDefault(x=>MemberId(x)==memberId);RenderTeam();generation++;status.Text="团队草稿已保存在本机；成员系谱与原配种方案分别保留。";
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="团队保存未完成，当前草稿保留。若其他窗口已修改，请刷新后再保存。";}finally{Lock(false);}
    }
    public async Task LoadTeam()
    {
        if(busy||savedTeams.SelectedItem is not Choice choice||choice.Row==null)return;var priorMemberId=selectedMember==null?null:MemberId(selectedMember);StoreMemberRoute();loading=true;try{
            team=Clone(choice.Row);teamName.Text=TeamPlanningUi.Str(team["name"]);requirements=Clone(team["requirements"] as JObject??new());requirementsEditor.Load(requirements);displayedDescription=TeamPlanningUi.Str(team["request_text"]);description.Text=displayedDescription;displayedGuideUrls="";guideUrls.Text=displayedGuideUrls;research=team["research_id"]==null||team["research_id"]!.Type==JTokenType.Null?null:new JObject{["research_id"]=team["research_id"]!.DeepClone()};researchContext="";sourceChoices.Clear();sources.Children.Clear();if(research!=null)sources.Children.Add(TeamPlanningUi.Note("此草稿保留检索编号与所选证据；重新检索后可查看日期和适用性。"));var wanted=TeamPlanningUi.Str(team["account_id"]);var options=(account.ItemsSource as System.Collections.Generic.IEnumerable<Choice>??[]).ToArray();if(wanted.Length>0&&options.All(x=>x.Id!=wanted)){options=options.Append(new Choice(wanted,"原账号（本地目录未收录） · "+wanted)).ToArray();account.ItemsSource=options;}account.SelectedItem=options.FirstOrDefault(x=>x.Id==wanted)??options.FirstOrDefault();selectedMember=null;memberWorkspace.IsVisible=false;seedCandidates.Children.Clear();clarifications.Text="";
        }finally{loading=false;}generation++;RefreshRouteEntries();await inventory.Load();RenderTeam();var rows=(team["team_plan"]?["members"] as JArray??new()).OfType<JObject>().ToArray();var chosen=rows.FirstOrDefault(x=>MemberId(x)==priorMemberId)??rows.FirstOrDefault();if(chosen!=null)await SelectMember(chosen);else workspaces.SelectedIndex=0;draftPanel.IsVisible=false;status.Text="已载入团队与各成员草稿。";
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
        if(selectedMember==null)return;var id=MemberId(selectedMember);if(id.Length==0)return;if(team["member_routes"] is not JObject routes){routes=new JObject();team["member_routes"]=routes;}routes[id]=route.ReadDraft();
    }
    private async Task SelectMember(JObject member)
    {
        StoreMemberRoute();selectedMember=member;generation++;seedCandidates.Children.Clear();memberWorkspace.IsVisible=true;candidatePanel.IsVisible=false;memberDetails.IsExpanded=false;memberHeading.Text=MemberName(member)+" · "+Style(TeamPlanningUi.Str(member["running_style"]));suppressRoute=true;try{await route.LoadCatalog();if(selectedMember!=member)return;RefreshRouteEntries();route.CloseInspector();var saved=(team["member_routes"] as JObject)?[MemberId(member)] as JObject;route.LoadDraft(saved);route.SetTargetEditor(TeamPlanningUi.Note("当前成员："+MemberName(member)+"。分工与跑法可在当前成员详情中编辑。"));if((route.ReadDraft()["nodes"] as JArray??new()).Any(x=>x.Type!=JTokenType.Null))await route.Evaluate();if(selectedMember!=member)return;}finally{suppressRoute=false;}RenderTeam();workspaces.SelectedIndex=1;teamScroll.Offset=new Vector(0,0);Dispatcher.UIThread.Post(()=>{if(selectedMember==member)teamScroll.Offset=new Vector(0,0);});
    }
    private string MemberName(JObject member)
    {
        var name=TeamPlanningUi.Str(member["name"]);if(name.Length>0)return name;var id=TeamPlanningUi.Int(member["character_id"]);var row=characters.FirstOrDefault(x=>TeamPlanningUi.Int(x["character_id"])==id);return TeamPlanningUi.Str(row?["name"]) is string label&&label.Length>0?label:id is int number?"角色 #"+number:TeamPlanningUi.Str(member["member_id"]);
    }
}
