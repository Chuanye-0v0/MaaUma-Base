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
    private readonly TextBox description=new(){Name="TeamPlanningDescription",AcceptsReturn=true,MinHeight=100,MaxLength=2000,TextWrapping=TextWrapping.Wrap,Watermark="例如：国服下个月大赛，东京草地 2400 米，优先我已有的马。跑法不确定可以不填，也可以先贴攻略链接。"};
    private readonly TeamPlanningRequirementsEditor requirementsEditor=new();
    private readonly TextBlock status=TeamPlanningUi.Note("可以直接贴链接读取，或描述比赛后检索；需求卡的待定项可稍后补充。"),clarifications=TeamPlanningUi.Note(""),contextStatus=TeamPlanningUi.Note("正在读取本地规划账号…");
    private readonly TextBox guideUrls=new(){Name="TeamGuideUrls",AcceptsReturn=true,MinHeight=56,MaxLength=6000,TextWrapping=TextWrapping.Wrap,Watermark="可选：粘贴攻略链接，每行一个；联网检索由下面按钮明确触发。"};
    private readonly Button parse=new(){Name="TeamRequirementsParse",Content="整理 / 更新需求卡"},localResearch=new(){Name="TeamResearchLocal",Content="仅查本地参考"},networkResearch=new(){Name="TeamResearchNetwork",Content="读取链接 / 检索攻略"},propose=new(){Name="TeamAiPropose",Content="生成一套团队方案"},refresh=new(){Name="TeamContextRefresh",Content="刷新账号与本地资料"},save=new(){Name="TeamDraftSave",Content="保存团队草稿"},load=new(){Name="TeamDraftLoad",Content="载入团队草稿"};
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
    public TeamPlanningView(Func<JObject,Task<JObject>> request,UmaKnowledgeToolsClient? toolboxClient=null)
    {
        this.request=request;this.toolboxClient=toolboxClient;Name="TeamPlanningView";
        inventory=new TeamPlanningInventoryEditor(request,()=>AccountId);inventory.Changed+=()=>Invalidate("账号持有资料已变化，已有建议需要重新生成。",true);
        route=new ParentLocalRouteView(request,MemberContext,()=>[],RecordsSaved,memberMode:true);
        route.DraftChanged+=()=>{if(suppressRoute||selectedMember==null)return;StoreMemberRoute();Invalidate("成员系谱草稿已修改。",false);};
        var root=new Grid{RowDefinitions=new("Auto,Auto,*,Auto"),RowSpacing=8,Margin=new Thickness(0,8,0,0)};
        var toolbar=new StackPanel{Spacing=5};
        var top=new WrapPanel{Name="TeamFixedToolbar"};account.Width=240;teamName.Width=240;
        foreach(var control in new Control[]{account,teamName,save})control.Margin=new Thickness(0,0,8,5);
        save.Content="保存草稿 / 方案";top.Children.Add(account);top.Children.Add(teamName);top.Children.Add(save);
        var openDraft=new Button{Name="TeamOpenSavedDrafts",Content="打开已保存的草稿",Margin=new Thickness(0,0,8,5)};
        openDraft.Click+=(_,_)=>{draftPanel.IsVisible=!draftPanel.IsVisible;draftPanel.IsExpanded=true;};top.Children.Add(openDraft);
        restoreDraft.Click+=async(_,_)=>await RestorePlanningDraft();top.Children.Add(restoreDraft);toolbar.Children.Add(top);toolbar.Children.Add(BuildToolboxActions());
        var persisted=new WrapPanel();savedTeams.Width=280;
        foreach(var control in new Control[]{savedTeams,load,refresh})control.Margin=new Thickness(0,0,8,5);
        load.Content="打开所选草稿";refresh.Content="刷新本地资料";
        persisted.Children.Add(savedTeams);persisted.Children.Add(load);persisted.Children.Add(refresh);
        draftPanel.Content=persisted;draftPanel.IsVisible=false;toolbar.Children.Add(draftPanel);root.Children.Add(toolbar);
        var workflow=BuildPlanningWorkflow();Grid.SetRow(workflow,1);root.Children.Add(workflow);
        var requirementsBody=new StackPanel{Spacing=12,Margin=new Thickness(0,8,0,16)};
        var goal=new StackPanel{Spacing=8};goal.Children.Add(TeamPlanningUi.Title("1  说说你想准备什么比赛",18));
        goal.Children.Add(TeamPlanningUi.Note("写你知道的就够了。场地、跑法和持有资料可以先留空；生成前会自动整理。"));
        var examples=new WrapPanel{Name="TeamGoalExamples"};
        void Example(string label,string text){var button=new Button{Content=label,Margin=new Thickness(0,0,8,4)};button.Click+=(_,_)=>{description.Text=text;description.Focus();};examples.Children.Add(button);}
        Example("试试：有马 2500","国服，中山草地2500米，大赛3匹。跑法由AI推荐，优先已有角色和可借种马；未知条件先作参考。");
        Example("试试：LOH 三匹","国服LOH，准备3匹。赛场尚未确定，请先给参考阵容与需要补充的条件。");
        goal.Children.Add(examples);description.MinHeight=82;goal.Children.Add(description);
        parse.Content="手动整理描述";goal.Children.Add(new Expander{Name="TeamGoalDetails",Header="手动调整比赛条件（可选）",IsExpanded=false,
            Content=new StackPanel{Spacing=8,Children={parse,requirementsEditor,clarifications}}});requirementsBody.Children.Add(TeamPlanningUi.Card(goal));
        var researchBody=new StackPanel{Spacing=8,Name="TeamResearchStep"};researchBody.Children.Add(TeamPlanningUi.Title("2  补充攻略或本地参考（可选）",17));
        guideUrls.MinHeight=50;researchBody.Children.Add(guideUrls);
        var researchActions=new WrapPanel();foreach(var button in new[]{networkResearch,localResearch}){button.Margin=new Thickness(0,0,8,4);researchActions.Children.Add(button);}
        researchBody.Children.Add(researchActions);researchBody.Children.Add(TeamPlanningUi.Note("不查攻略也可生成参考方案。读取链接需要点击按钮；视频网页通常只有标题和简介，不等于已读视频内容。"));
        researchBody.Children.Add(sources);requirementsBody.Children.Add(TeamPlanningUi.Card(researchBody));
        requirementsBody.Children.Add(new Expander{Name="TeamInventoryPanel",Header="补充账号角色与支援卡（可选，未录入保持未知）",Content=inventory,IsExpanded=false});
        var generate=new StackPanel{Name="TeamGenerateStep",Spacing=8};generate.Children.Add(TeamPlanningUi.Title("3  生成团队，再为成员匹配种马",17));
        generate.Children.Add(TeamPlanningUi.Note("将使用已配置的AI接口；点击后才发送本次请求。未指定人数时先按3匹参考，可在比赛条件中修改。"));
        propose.MinHeight=44;generate.Children.Add(propose);var inlineStatus=TeamPlanningUi.Note("");inlineStatus.Name="TeamGenerationInlineStatus";
        inlineStatus.Bind(TextBlock.TextProperty,status.GetObservable(TextBlock.TextProperty));generate.Children.Add(inlineStatus);requirementsBody.Children.Add(TeamPlanningUi.Card(generate));
        var teamBody=new StackPanel{Spacing=8,Margin=new Thickness(0,8,0,16)};teamBody.Children.Add(BuildEmptyTeam());
        teamSummary.MaxLines=2;teamSummary.TextTrimming=TextTrimming.CharacterEllipsis;memberHeading.MaxLines=2;
        teamBody.Children.Add(teamSummary);teamBody.Children.Add(members);
        var memberBody=new StackPanel{Spacing=8};var memberBar=new WrapPanel();memberHeading.MaxWidth=500;memberHeading.Margin=new Thickness(0,0,12,5);
        memberBar.Children.Add(memberHeading);match.Content="为这个成员匹配种马";memberBar.Children.Add(match);memberBody.Children.Add(memberBar);
        memberBody.Children.Add(TeamPlanningUi.Note("选成员 → 匹配种马 → 查看已有／可借／待培养节点。点击节点可编辑，每位成员单独保留系谱。"));
        memberBody.Children.Add(route);candidatePanel.Content=seedCandidates;memberBody.Children.Add(candidatePanel);memberBody.Children.Add(memberDetails);memberWorkspace.Child=memberBody;teamBody.Children.Add(memberWorkspace);teamScroll.Content=teamBody;
        workspaces.ItemsSource=new[]{new TabItem{Header="目标与资料",Content=new ScrollViewer{Name="TeamRequirementsScroll",Content=requirementsBody,HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled}},new TabItem{Header="团队与系谱",Content=teamScroll}};
        Grid.SetRow(workspaces,2);root.Children.Add(workspaces);
        contextStatus.FontSize=11;contextStatus.MaxLines=2;Grid.SetRow(contextStatus,3);root.Children.Add(contextStatus);Content=root;
        members.Children.CollectionChanged+=(_,_)=>RefreshPlanningSteps();
        status.PropertyChanged+=(_,args)=>{if(args.Property==TextBlock.TextProperty)RefreshPlanningSteps();};
        RefreshPlanningSteps();
        description.TextChanged+=(_,_)=>{var value=description.Text??"";if(loading||value==displayedDescription)return;displayedDescription=value;Invalidate("描述已修改；可直接检索，会一起整理需求卡。",true);};requirementsEditor.Changed+=()=>{requirements=requirementsEditor.Read();Invalidate("需求卡已修改；可继续检索，待定项稍后补充。",true);};guideUrls.TextChanged+=(_,_)=>{var value=guideUrls.Text??"";if(loading||value==displayedGuideUrls)return;displayedGuideUrls=value;Invalidate("链接已修改，可直接读取链接；不必先填完需求卡。",true);};
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
        if(loading)return;generation++;seedCandidates.Children.Clear();candidatePanel.IsVisible=false;if(dropResearch)researchContext="";status.Text=message;planningError=false;RefreshPlanningSteps();
    }
    private void Lock(bool value){busy=value;if(value)planningError=false;foreach(var button in new[]{parse,localResearch,networkResearch,propose,match,refresh,save,load,restoreDraft})button.IsEnabled=!value;planningProgress.IsVisible=value;RefreshPlanningSteps();}
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
            var command=new JObject{["action"]="team_requirements_parse",["request_text"]=(description.Text??"").Trim()};var overrides=ReadPlanningOverrides();if(overrides.Properties().Any())command["overrides"]=overrides;var response=await request(command);if(!Current(snapshot,at)){status.Text="输入或账号已变化，旧需求未套用；请重新整理。";return;}
            requirements=response["requirements"] as JObject??new();loading=true;try{requirementsEditor.LoadPreservingEdits(requirements);}finally{loading=false;}generation++;researchContext="";RenderClarifications(response);status.Text=(response["conflicts"] as JArray)?.Count>0?"需求里有几处条件需要核对；也可以先读取攻略帮助确认。":"需求卡已整理，待定项可由攻略补充；可以继续检索。";
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="需求整理未完成，输入已保留。";}finally{Lock(false);}
    }
    private void RenderClarifications(JObject response)
    {
        var lines=new List<string>();foreach(var item in (response["clarification_questions"] as JArray??new())){var question=item is JObject row?TeamPlanningUi.Str(row["question"]):TeamPlanningUi.Str(item);if(question.Length>0)lines.Add("可补充（不影响先检索）："+question);}foreach(var item in (response["conflicts"] as JArray??new()))lines.Add("条件需核对："+(item is JObject row?TeamPlanningUi.Str(row["message"]):TeamPlanningUi.Str(item)));foreach(var item in (response["assumptions"] as JArray??new()))lines.Add("按此理解："+TeamPlanningUi.Str(item));clarifications.Text=string.Join("\n",lines.Where(x=>x.Length>0));
    }
    public async Task RetrieveGuides(bool allowNetwork)
    {
        if(busy)return;var snapshot=Snapshot();var at=generation;var overrides=ReadPlanningOverrides();Lock(true);status.Text=allowNetwork?"正在整理描述并读取链接或检索攻略…":"正在整理描述并读取本地参考…";try{
            var parsed=await request(new JObject{["action"]="team_requirements_parse",["request_text"]=description.Text??"",["overrides"]=overrides.DeepClone()});
            if(!Current(snapshot,at)){status.Text="描述或需求已变化，未采用旧的整理结果；可以重新检索。";return;}
            requirements=Clone(parsed["requirements"] as JObject??new());loading=true;try{requirementsEditor.LoadPreservingEdits(requirements);requirementsEditor.ApplyDefaultTeamSize(TeamPlanningUi.Int(requirements["team_size"])??3);requirements=requirementsEditor.Read();}finally{loading=false;}
            RenderClarifications(parsed);researchContext="";generation++;snapshot=Snapshot();at=generation;
            overrides=ReadPlanningOverrides();
            var command=new JObject{["action"]="team_guides_retrieve",["requirements"]=requirementsEditor.Read(),["request_text"]=description.Text??"",["guide_urls"]=new JArray(TeamPlanningUi.Tokens(guideUrls.Text)),["allow_network"]=allowNetwork};if(overrides.Properties().Any())command["overrides"]=overrides.DeepClone();
            var response=await request(command);if(!Current(snapshot,at)){status.Text="检索期间需求或账号变化，旧结果未采用。";return;}
            if(response["requirements"] is JObject canonical){requirements=Clone(canonical);loading=true;try{requirementsEditor.LoadPreservingEdits(requirements);}finally{loading=false;}}
            research=Clone(response);RenderResearch(response);researchContext=requirementsEditor.Read().ToString();generation++;status.Text=ResearchSummary(response);
        }catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="检索暂未完成；当前本地参考与链接已保留。";}finally{Lock(false);}
    }
    private static string ResearchSummary(JObject response)
    {
        var guides=(response["guides"] as JArray??new()).OfType<JObject>().ToArray();var facts=(response["facts"] as JArray??new()).Count;
        if(guides.Length==0)return facts>0?$"已读取 {facts} 项本地参考；本次尚未取得攻略来源。可补充链接后重试，或先做参考方案。":"本次还没找到可引用资料。可以补充攻略链接后重试，待定项不会阻止检索。";
        var pages=guides.Count(x=>TeamPlanningUi.Str(x["content_scope"])=="public_page_text");
        return pages>0?$"找到 {guides.Length} 条来源，其中 {pages} 条已取得公开页面文字；请勾选本次采用的来源，再生成团队。":$"找到 {guides.Length} 条来源资料，但尚未取得攻略正文（可能仅有摘要或视频页面信息）；可核对后选择，或补充文字攻略链接。";
    }
    private void RenderResearch(JObject response)
    {
        var selected=sourceChoices.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key).ToHashSet(StringComparer.Ordinal);
        sources.Children.Clear();sourceChoices.Clear();
        var facts=(response["facts"] as JArray??new()).OfType<JObject>().ToArray();
        if(facts.Length>0){var factsBody=new StackPanel{Spacing=6};foreach(var fact in facts){factsBody.Children.Add(TeamPlanningUi.Title(TeamPlanningUi.Str(fact["title"]),13));factsBody.Children.Add(TeamPlanningUi.Note(TeamPlanningUi.Str(fact["snippet"])));}sources.Children.Add(new Expander{Header=$"本地参考 · {facts.Length} 项（展开查看）",Content=factsBody,IsExpanded=false});}
        foreach(var row in (response["guides"] as JArray??new()).OfType<JObject>()){
            var id=TeamPlanningUi.Str(row["source_id"]);var applicability=row["applicability"] as JObject;
            var title=TeamPlanningUi.Note(TeamPlanningUi.Str(row["title"]));title.FontWeight=FontWeight.SemiBold;title.MaxLines=2;title.TextTrimming=TextTrimming.CharacterEllipsis;
            var check=new CheckBox{Name="TeamSourceSelect",Content=title,IsChecked=selected.Contains(id),IsEnabled=id.Length>0,HorizontalContentAlignment=HorizontalAlignment.Stretch};
            if(id.Length>0){sourceChoices[id]=check;check.IsCheckedChanged+=(_,_)=>Invalidate("采用的资料已变化；可直接重新生成。",false);}
            var scope=TeamPlanningUi.Str(row["content_scope"]) switch{"public_page_text"=>"网页正文","video_page_metadata_only"=>"仅视频标题 / 简介","search_metadata_only"=>"仅搜索摘要",_=>"内容范围待核对"};
            var date=TeamPlanningUi.Str(row["date"]);var body=new StackPanel{Spacing=5};body.Children.Add(check);body.Children.Add(TeamPlanningUi.Note(scope+" · "+(date.Length>0?date:"日期未标注")));
            var detail=new StackPanel{Spacing=5};detail.Children.Add(TeamPlanningUi.Note("页面摘要："+TeamPlanningUi.Str(row["snippet"])));detail.Children.Add(TeamPlanningUi.Note("链接："+TeamPlanningUi.Str(row["url"])));
            var server=TeamPlanningUi.Str(row["server"]) switch{"cn"=>"国服","jp"=>"日服","tw"=>"台服",_=>"未标注"};detail.Children.Add(TeamPlanningUi.Note("来源服务器："+server+"；具体适用条件请结合资料日期核对。"));
            var reasons=TeamPlanningUi.Lines(applicability?["reasons"]);if(reasons.Length>0)detail.Children.Add(TeamPlanningUi.Note(reasons));
            body.Children.Add(new Expander{Name="TeamSourceDetails",Header="查看摘要、链接与适用范围",Content=detail,IsExpanded=false});sources.Children.Add(TeamPlanningUi.Card(body));
        }
        foreach(var error in (response["errors"] as JArray??new()))sources.Children.Add(TeamPlanningUi.Note("读取提示："+(error is JObject row?TeamPlanningUi.Str(row["message"]):TeamPlanningUi.Str(error))));
        if(sourceChoices.Count==0)sources.Children.Add(TeamPlanningUi.Note("未选攻略也可以继续；将用本地资料生成带未知条件的参考方案。"));
    }
}
