using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>A team is N members with separate drafts. No game or finished-library writes.</summary>
public sealed partial class TeamPlanningView : UserControl
{
    private sealed record Choice(string Id,string Label,JObject? Row=null){public override string ToString()=>Label;}
    private readonly Func<JObject,Task<JObject>> request;
    private readonly TextBox description=new(){Name="TeamPlanningDescription",AcceptsReturn=true,MinHeight=100,MaxLength=2000,TextWrapping=TextWrapping.Wrap,Watermark="例如：国服下个月冠军赛，东京草地 2400 米，3 匹，想用 1 逃 2 差；优先我已有的角色和卡。"};
    private readonly TeamPlanningRequirementsEditor requirementsEditor=new();
    private readonly TextBlock status=TeamPlanningUi.Note("先描述比赛，再确认下面的需求卡。"),clarifications=TeamPlanningUi.Note(""),contextStatus=TeamPlanningUi.Note("正在读取本地规划账号…");
    private readonly TextBox guideUrls=new(){Name="TeamGuideUrls",AcceptsReturn=true,MinHeight=56,MaxLength=6000,TextWrapping=TextWrapping.Wrap,Watermark="可选：粘贴攻略链接，每行一个；联网检索由下面按钮明确触发。"};
    private readonly Button parse=new(){Name="TeamRequirementsParse",Content="整理 / 更新需求卡"},localResearch=new(){Name="TeamResearchLocal",Content="仅查本地参考"},networkResearch=new(){Name="TeamResearchNetwork",Content="联网检索攻略"},propose=new(){Name="TeamAiPropose",Content="生成一套团队方案"},refresh=new(){Name="TeamContextRefresh",Content="刷新账号与本地资料"},save=new(){Name="TeamDraftSave",Content="保存团队草稿"},load=new(){Name="TeamDraftLoad",Content="载入团队草稿"};
    private readonly ComboBox account=new(){Name="TeamPlanningAccount",HorizontalAlignment=HorizontalAlignment.Stretch},savedTeams=new(){Name="TeamSavedDrafts",HorizontalAlignment=HorizontalAlignment.Stretch,PlaceholderText="选择本地团队草稿"};
    private readonly TextBox teamName=new(){Name="TeamDraftName",Watermark="团队草稿名称"};
    private readonly WrapPanel members=new(){Name="TeamMemberCards"};
    private readonly StackPanel sources=new(){Name="TeamResearchSources",Spacing=7},seedCandidates=new(){Name="TeamMemberSeedCandidates",Spacing=7};
    private readonly TextBlock teamSummary=TeamPlanningUi.Note("尚未生成团队方案。"),memberHeading=TeamPlanningUi.Title("选择成员后，在这里匹配种马",17);
    private readonly Button match=new(){Name="TeamMemberSeed",Content="为此成员匹配种马"};
    private readonly Border memberWorkspace=new(){Name="TeamMemberWorkspace",IsVisible=false};
    private readonly TabControl workspaces=new(){Name="TeamWorkspaceTabs",SelectedIndex=0,HorizontalContentAlignment=HorizontalAlignment.Stretch,VerticalContentAlignment=VerticalAlignment.Stretch};
    private readonly ScrollViewer teamScroll=new(){Name="TeamPlanningScroll",HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled};
    private readonly Expander memberDetails=new(){Name="TeamCurrentMemberDetails",Header="当前成员 · 目标、技能、配卡与缺口",IsExpanded=false};
    private readonly Expander candidatePanel=new(){Name="TeamSeedCandidatesPanel",Header="种马候选",IsExpanded=false,IsVisible=false};
    private readonly Expander draftPanel=new(){Name="TeamDraftPersistence",Header="载入已保存的团队",IsExpanded=false};
    private readonly ParentLocalRouteView route;
    private readonly TeamPlanningInventoryEditor inventory;
    private readonly Dictionary<string,CheckBox> sourceChoices=new(StringComparer.Ordinal);
    private JObject planningState=new(),team=new(),requirements=new();
    private JObject? research,selectedMember;
    private JObject[] characters=[];
    private int generation,teamRevision;
    private bool busy,loading,loaded,suppressRoute;
    private string researchContext="",displayedDescription="",displayedGuideUrls="";
    public TeamPlanningView(Func<JObject,Task<JObject>> request)
    {
        this.request=request;Name="TeamPlanningView";
        inventory=new TeamPlanningInventoryEditor(request,()=>AccountId);inventory.Changed+=()=>Invalidate("账号持有资料已变化，已有建议需要重新生成。",true);
        route=new ParentLocalRouteView(request,MemberContext,()=>[],RecordsSaved,memberMode:true);
        route.DraftChanged+=()=>{if(suppressRoute||selectedMember==null)return;StoreMemberRoute();Invalidate("成员系谱草稿已修改。",false);};
        var root=new Grid{RowDefinitions=new("Auto,*,Auto"),RowSpacing=8,Margin=new Thickness(0,8,0,0)};
        var toolbar=new StackPanel{Spacing=5};var top=new WrapPanel{Name="TeamFixedToolbar"};account.Width=240;teamName.Width=240;account.Margin=teamName.Margin=save.Margin=new Thickness(0,0,8,5);save.Content="保存团队";top.Children.Add(account);top.Children.Add(teamName);top.Children.Add(save);var openDraft=new Button{Name="TeamOpenSavedDrafts",Content="载入 / 刷新",Margin=new Thickness(0,0,0,5)};openDraft.Click+=(_,_)=>{draftPanel.IsVisible=!draftPanel.IsVisible;draftPanel.IsExpanded=true;};top.Children.Add(openDraft);toolbar.Children.Add(top);
        var persisted=new WrapPanel();savedTeams.Width=280;savedTeams.Margin=load.Margin=refresh.Margin=new Thickness(0,0,8,5);load.Content="载入";refresh.Content="刷新本地资料";persisted.Children.Add(savedTeams);persisted.Children.Add(load);persisted.Children.Add(refresh);draftPanel.Content=persisted;draftPanel.IsVisible=false;toolbar.Children.Add(draftPanel);root.Children.Add(toolbar);
        var requirementsBody=new StackPanel{Spacing=12,Margin=new Thickness(0,8,0,16)};requirementsBody.Children.Add(TeamPlanningUi.Title("比赛条件与团队目标",18));requirementsBody.Children.Add(description);requirementsBody.Children.Add(parse);requirementsBody.Children.Add(TeamPlanningUi.Card(requirementsEditor));requirementsBody.Children.Add(clarifications);
        var researchBody=new StackPanel{Spacing=8};researchBody.Children.Add(TeamPlanningUi.Title("攻略与本地参考",17));researchBody.Children.Add(guideUrls);var researchActions=new WrapPanel();foreach(var button in new[]{localResearch,networkResearch}){button.Margin=new Thickness(0,0,8,4);researchActions.Children.Add(button);}researchBody.Children.Add(researchActions);researchBody.Children.Add(TeamPlanningUi.Note("选择要采用的证据；日期与国服适用性请一起参考。本地事实与攻略分别列出。"));researchBody.Children.Add(sources);requirementsBody.Children.Add(TeamPlanningUi.Card(researchBody));
        requirementsBody.Children.Add(new Expander{Name="TeamInventoryPanel",Header="账号角色与支援卡资料（可先做参考方案）",Content=inventory,IsExpanded=false});requirementsBody.Children.Add(propose);
        var teamBody=new StackPanel{Spacing=8,Margin=new Thickness(0,8,0,16)};teamSummary.MaxLines=1;teamSummary.TextTrimming=TextTrimming.CharacterEllipsis;memberHeading.MaxLines=1;memberHeading.TextTrimming=TextTrimming.CharacterEllipsis;teamBody.Children.Add(teamSummary);teamBody.Children.Add(members);
        var memberBody=new StackPanel{Spacing=8};var memberBar=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=10};memberBar.Children.Add(memberHeading);match.Content="匹配此成员种马";Grid.SetColumn(match,1);memberBar.Children.Add(match);memberBody.Children.Add(memberBar);ToolTip.SetTip(memberHeading,"点击系谱节点查看或编辑；各成员的材料和草稿分别保留。");memberBody.Children.Add(route);candidatePanel.Content=seedCandidates;memberBody.Children.Add(candidatePanel);memberBody.Children.Add(memberDetails);memberWorkspace.Child=memberBody;teamBody.Children.Add(memberWorkspace);teamScroll.Content=teamBody;
        workspaces.ItemsSource=new[]{new TabItem{Header="比赛需求与资料",Content=new ScrollViewer{Name="TeamRequirementsScroll",Content=requirementsBody,HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled}},new TabItem{Header="团队系谱",Content=teamScroll}};Grid.SetRow(workspaces,1);root.Children.Add(workspaces);
        var footer=new StackPanel{Spacing=3};contextStatus.FontSize=11;contextStatus.TextWrapping=TextWrapping.NoWrap;contextStatus.TextTrimming=TextTrimming.CharacterEllipsis;footer.Children.Add(contextStatus);footer.Children.Add(new ScrollViewer{Name="TeamStatusScroll",MaxHeight=52,Content=status,HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled});Grid.SetRow(footer,2);root.Children.Add(footer);Content=root;
        description.TextChanged+=(_,_)=>{var value=description.Text??"";if(loading||value==displayedDescription)return;displayedDescription=value;Invalidate("描述已修改，请更新需求卡。",true);};requirementsEditor.Changed+=()=>{requirements=requirementsEditor.Read();Invalidate("需求卡已修改，请重新核对并检索。",true);};guideUrls.TextChanged+=(_,_)=>{var value=guideUrls.Text??"";if(loading||value==displayedGuideUrls)return;displayedGuideUrls=value;Invalidate("攻略链接已修改，请重新检索。",true);};
        account.SelectionChanged+=async(_,_)=>{if(loading)return;StoreMemberRoute();selectedMember=null;memberWorkspace.IsVisible=false;workspaces.SelectedIndex=0;RefreshRouteEntries();Invalidate("规划账号已变化，请重新生成此账号的建议。",true);await inventory.Load();};
        parse.Click+=async(_,_)=>await ParseRequirements();localResearch.Click+=async(_,_)=>await RetrieveGuides(false);networkResearch.Click+=async(_,_)=>await RetrieveGuides(true);propose.Click+=async(_,_)=>await ProposeTeam();match.Click+=async(_,_)=>await MatchMember();refresh.Click+=async(_,_)=>await LoadContext();save.Click+=async(_,_)=>await SaveTeam();load.Click+=async(_,_)=>await LoadTeam();
        Loaded+=async(_,_)=>{if(!loaded)await LoadContext();};
        account.ItemsSource=new[]{new Choice("","所有资料（参考方案，持有状态未知）")};account.SelectedIndex=0;
    }
    private string? AccountId=>(account.SelectedItem as Choice)?.Id is string id&&id.Length>0?id:null;
    private JObject BaseContext()=>new(){["account_id"]=AccountId,["library_id"]=planningState["library_id"]?.DeepClone(),["library_revision"]=planningState["library_revision"]?.DeepClone(),["inventory_revision"]=inventory.Revision,["include_unassigned"]=false};
    private JObject MemberContext(){var value=BaseContext();value["current_library_id"]=planningState["library_id"]?.DeepClone();if(team["library_id"]?.Type==JTokenType.String)value["library_id"]=team["library_id"]!.DeepClone();value["target_character_id"]=selectedMember?["character_id"]?.DeepClone();value["account_label"]=(account.SelectedItem as Choice)?.Label??"参考方案";return value;}
    private JObject Snapshot(){var value=BaseContext();value["requirements"]=requirementsEditor.Read();value["request_text"]=description.Text??"";value["guide_urls"]=new JArray(TeamPlanningUi.Tokens(guideUrls.Text));value["research_id"]=research?["research_id"]?.DeepClone();value["selected_source_ids"]=SelectedSourceIds();value["team"]=team.DeepClone();value["team_name"]=teamName.Text??"";value["selected_member_id"]=selectedMember==null?null:MemberId(selectedMember);value["current_member_route"]=selectedMember==null?null:route.ReadDraft();return value;}
    private void Invalidate(string message,bool dropResearch)
    {
        if(loading)return;generation++;seedCandidates.Children.Clear();candidatePanel.IsVisible=false;if(dropResearch)researchContext="";status.Text=message;
    }
    private void Lock(bool value){busy=value;foreach(var button in new[]{parse,localResearch,networkResearch,propose,match,refresh,save,load})button.IsEnabled=!value;}
    private bool Current(JObject snapshot,int at)=>at==generation&&JToken.DeepEquals(snapshot,Snapshot());
    private static JObject Clone(JObject row)=>(JObject)row.DeepClone();
    public async Task LoadContext()
    {
        if(busy)return;Lock(true);var oldAccount=AccountId;var at=generation;try{
            var state=await request(new JObject{["action"]="parent_planning_state"});if(at!=generation){status.Text="账号或输入已变化，未套用刷新前的上下文。";return;}planningState=Clone(state);loading=true;try{var choices=new[]{new Choice("","所有资料（参考方案，持有状态未知）")}.Concat((state["accounts"] as JArray??new()).OfType<JObject>().Select(x=>new Choice(TeamPlanningUi.Str(x["id"]),TeamPlanningUi.Str(x["label"])))).ToArray();account.ItemsSource=choices;account.SelectedItem=choices.FirstOrDefault(x=>x.Id==oldAccount)??choices[0];}finally{loading=false;}
            contextStatus.Text="团队与各成员草稿独立于原配种方案；当前账号："+(account.SelectedItem as Choice)?.Label;generation++;seedCandidates.Children.Clear();researchContext="";
            await route.LoadCatalog();var catalog=await request(new JObject{["action"]="parent_planning_route_catalog"});characters=(catalog["characters"] as JArray??new()).OfType<JObject>().ToArray();RefreshRouteEntries();await inventory.Load();await ReadSavedTeams();loaded=true;status.Text="本地资料已刷新。可先生成参考方案，之后再补充持有角色与卡。";
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="部分本地资料暂不可用；已保留当前输入和草稿。";}finally{Lock(false);}
    }
    private void RefreshRouteEntries()
    {
        contextStatus.Text="团队与各成员草稿独立于原配种方案；当前账号："+(account.SelectedItem as Choice)?.Label;
        var entries=(planningState["entries"] as JArray??new()).OfType<JObject>().Where(x=>AccountId==null||TeamPlanningUi.Str(x["owner_account_id"])==AccountId||(x["borrowable_by"] as JArray??new()).Any(v=>TeamPlanningUi.Str(v)==AccountId)).ToArray();suppressRoute=true;try{route.SetEntries(entries);}finally{suppressRoute=false;}
    }
    public async Task ParseRequirements()
    {
        if(busy)return;var snapshot=Snapshot();var at=generation;Lock(true);status.Text="正在整理比赛需求…";try{
            var command=new JObject{["action"]="team_requirements_parse",["request_text"]=(description.Text??"").Trim()};var overrides=requirementsEditor.ReadOverrides();if(overrides.Properties().Any())command["overrides"]=overrides;var response=await request(command);if(!Current(snapshot,at)){status.Text="输入或账号已变化，旧需求未套用；请重新整理。";return;}
            requirements=response["requirements"] as JObject??new();loading=true;try{requirementsEditor.Load(requirements);}finally{loading=false;}generation++;researchContext="";RenderClarifications(response);status.Text=(bool?)response["ready_for_retrieval"]==true?"需求卡已整理，可检索参考资料。":"需求卡已整理，请在卡片中补充下列关键信息。";
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="需求整理未完成，输入已保留。";}finally{Lock(false);}
    }
    private void RenderClarifications(JObject response)
    {
        var lines=new List<string>();foreach(var item in (response["clarification_questions"] as JArray??new()))lines.Add(item is JObject row?TeamPlanningUi.Str(row["question"]):TeamPlanningUi.Str(item));foreach(var item in (response["conflicts"] as JArray??new()))lines.Add("需确认："+(item is JObject row?TeamPlanningUi.Str(row["message"]):TeamPlanningUi.Str(item)));foreach(var item in (response["assumptions"] as JArray??new()))lines.Add("按此理解："+TeamPlanningUi.Str(item));clarifications.Text=string.Join("\n",lines.Where(x=>x.Length>0));
    }
    public async Task RetrieveGuides(bool allowNetwork)
    {
        if(busy)return;var snapshot=Snapshot();var at=generation;Lock(true);status.Text=allowNetwork?"正在联网检索比赛攻略…":"正在读取本地事实与参考…";try{
            var response=await request(new JObject{["action"]="team_guides_retrieve",["requirements"]=requirementsEditor.Read(),["guide_urls"]=new JArray(TeamPlanningUi.Tokens(guideUrls.Text)),["allow_network"]=allowNetwork});if(!Current(snapshot,at)){status.Text="检索期间需求或账号变化，旧结果未采用。";return;}research=Clone(response);RenderResearch(response);researchContext=requirementsEditor.Read().ToString();generation++;status.Text="参考资料已列出；请选择采用的攻略证据，再生成一套团队方案。";
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="检索暂未完成；当前本地参考与链接已保留。";}finally{Lock(false);}
    }
    private void RenderResearch(JObject response)
    {
        sources.Children.Clear();sourceChoices.Clear();var facts=(response["facts"] as JArray??new()).OfType<JObject>().ToArray();if(facts.Length>0){var factBody=new StackPanel{Spacing=5};foreach(var fact in facts){factBody.Children.Add(TeamPlanningUi.Title(TeamPlanningUi.Str(fact["title"]),13));factBody.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(fact["snippet"])));factBody.Children.Add(TeamPlanningUi.Note("日期："+(TeamPlanningUi.Str(fact["date"]) is string date&&date.Length>0?date:"未标注")+" · 适用性："+TeamPlanningUi.Str(fact["applicability"]?["status"])));}sources.Children.Add(new Expander{Header="本地事实（非攻略）",Content=factBody,IsExpanded=false});}
        foreach(var row in (response["guides"] as JArray??new()).OfType<JObject>()){
            var id=TeamPlanningUi.Str(row["source_id"]);var applicability=row["applicability"] as JObject;var state=TeamPlanningUi.Str(applicability?["status"]);var body=new StackPanel{Spacing=4};var check=new CheckBox{Name="TeamSourceSelect",Content=TeamPlanningUi.Str(row["title"]),IsChecked=false,IsEnabled=id.Length>0};if(id.Length>0){sourceChoices[id]=check;check.IsCheckedChanged+=(_,_)=>Invalidate("采用的证据已变化，可重新生成团队方案。",false);}body.Children.Add(check);
            body.Children.Add(TeamPlanningUi.Note("日期："+(TeamPlanningUi.Str(row["date"]) is string date&&date.Length>0?date:"未标注")+" · 服务器："+TeamPlanningUi.Str(row["server"])+" · 适用性："+(state.Length>0?state:"待核对")));body.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(row["snippet"])));body.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(row["url"])));
            var reasons=TeamPlanningUi.Lines(applicability?["reasons"]);if(reasons.Length>0)body.Children.Add(TeamPlanningUi.Note(reasons));sources.Children.Add(TeamPlanningUi.Card(body));
        }
        foreach(var error in (response["errors"] as JArray??new()))sources.Children.Add(TeamPlanningUi.Note("检索提示："+(error is JObject row?TeamPlanningUi.Str(row["message"]):TeamPlanningUi.Str(error))));foreach(var missing in (response["missing_data"] as JArray??new()))sources.Children.Add(TeamPlanningUi.Note("资料待补："+TeamPlanningUi.Str(missing)));
        if(sourceChoices.Count==0)sources.Children.Add(TeamPlanningUi.Note("当前没有可采用的攻略证据。可用本地事实做参考方案，或补充链接后重试。"));
    }
}
