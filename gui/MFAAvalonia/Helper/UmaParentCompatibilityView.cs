using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using MFAAvalonia.Views.UserControls;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>Basic compatibility over real local finished-horse records; no inheritance probability model.</summary>
public sealed class UmaParentCompatibilityView : UserControl
{
    private sealed record EntryChoice(string Id,string Label,JObject Row){public override string ToString()=>Label;}
    private sealed record AccountChoice(string? Id,string Label){public override string ToString()=>Label;}
    private sealed record PlanChoice(string Id,string Label,JObject Row){public override string ToString()=>Label;}
    private sealed record CharacterChoice(int? Id,string Name,JObject? Row=null){public override string ToString()=>Name;}
    private sealed class CharacterSelector : StackPanel
    {
        private CharacterChoice[] choices=[];private bool applying;
        private readonly TextBox search=new(){Watermark="输入角色名筛选"};
        private readonly ComboBox combo=new(){HorizontalAlignment=HorizontalAlignment.Stretch};
        public event Action? Changed;
        public int? SelectedId=>(combo.SelectedItem as CharacterChoice)?.Id;
        public CharacterChoice? Selected=>combo.SelectedItem as CharacterChoice;
        public CharacterSelector(string title,string name)
        {
            Spacing=4;Children.Add(Label(title,12));search.Name=name+"Search";combo.Name=name;Children.Add(search);Children.Add(combo);
            search.TextChanged+=(_,_)=>Filter();combo.SelectionChanged+=(_,_)=>{if(!applying)Changed?.Invoke();};
        }
        public void Load(CharacterChoice[] values){choices=values;SetId(SelectedId);}
        public void SetId(int? id,string? fallback=null)
        {
            applying=true;try{search.Text="";var selected=choices.FirstOrDefault(x=>x.Id==id)??new CharacterChoice(id,fallback??(id==null?"未指定 / 资料未知":"未收录角色 "+id));
                combo.ItemsSource=new[]{new CharacterChoice(null,"未指定 / 资料未知")}.Concat(choices).Append(selected).DistinctBy(x=>x.Id).ToArray();combo.SelectedItem=((CharacterChoice[])combo.ItemsSource).First(x=>x.Id==id);
            }finally{applying=false;}
        }
        private void Filter()
        {
            if(applying)return;var selected=combo.SelectedItem as CharacterChoice;var query=(search.Text??"").Trim();
            var filtered=choices.Where(x=>query.Length==0||x.Name.Contains(query,StringComparison.OrdinalIgnoreCase)||(x.Row?["aliases"] as JArray??new()).Any(a=>a.ToString().Contains(query,StringComparison.OrdinalIgnoreCase)));
            applying=true;try{var rows=new[]{new CharacterChoice(null,"未指定 / 资料未知")}.Concat(filtered).Concat(selected==null?[]:new[]{selected}).DistinctBy(x=>x.Id).ToArray();combo.ItemsSource=rows;combo.SelectedItem=rows.FirstOrDefault(x=>x.Id==selected?.Id);}finally{applying=false;}
        }
    }
    private static readonly string[] NodeKeys=["parent_a","gp_a1","gp_a2","parent_b","gp_b1","gp_b2"];
    private static readonly string[] NodeLabels=["亲代 A","A 的亲代 1（祖辈）","A 的亲代 2（祖辈）","亲代 B","B 的亲代 1（祖辈）","B 的亲代 2（祖辈）"];
    private const string KeepOwner="__keep_owner__";
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<JObject> planningGoals;
    private readonly Action<JObject>? restoreGoals;
    private readonly ComboBox account=new(){Name="CompatAccount",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly CheckBox includeUnassigned=new(){Name="CompatIncludeUnassigned",Content="包含未关联成品（仅作参考）",IsChecked=false};
    private readonly TextBlock accountStatus=Label("正在读取账号与方案…",12);
    private readonly ComboBox plans=new(){Name="CompatPlans",PlaceholderText="选择已保存方案",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly TextBox planName=new(){Name="CompatPlanName",Watermark="给当前家系起个名字；未填完也能存草稿"};
    private readonly TextBlock planStatus=Label("方案保存在本机，与种马库独立；不会写成已育成成品。",12);
    private readonly Button savePlan=new(){Name="CompatSavePlan",Content="保存方案 / 草稿"};
    private readonly Button loadPlan=new(){Name="CompatLoadPlan",Content="载入方案"};
    private readonly Button deletePlan=new(){Name="CompatDeletePlan",Content="删除方案"};
    private readonly Button newPlan=new(){Name="CompatNewPlan",Content="新建方案"};
    private readonly TextBox accountName=new(){Name="CompatNewAccountName",Watermark="账号备注，例如：另一台电脑的小号"};
    private readonly Button addAccount=new(){Name="CompatAddAccount",Content="添加手动账号"};
    private readonly ComboBox entryOwner=new(){Name="CompatEntryOwner",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly CheckBox borrowByCurrent=new(){Name="CompatBorrowByCurrent",Content="允许当前规划账号借用",IsChecked=false};
    private readonly Button bindEntries=new(){Name="CompatBindEntries",Content="保存所选成品归属"};
    private readonly StackPanel bindingList=new(){Name="CompatBindingList",Spacing=4};
    private readonly Dictionary<string,CheckBox> bindingBoxes=new(StringComparer.Ordinal);
    private readonly TextBlock bindingStatus=Label("只绑定明确选中的成品，不根据历史运行推断归属。",12);
    private readonly Expander ownershipPanel=new(){Name="CompatOwnershipPanel",Header="管理成品归属与可借账号",IsExpanded=false};
    private readonly CharacterSelector target=new("育成目标角色","CompatTargetCharacter");
    private readonly CharacterSelector[] nodes=NodeKeys.Select((key,index)=>new CharacterSelector(NodeLabels[index],"CompatNode_"+key)).ToArray();
    private readonly ComboBox parentA=new(){Name="CompatParentA",PlaceholderText="从种马库选亲代 A",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly ComboBox parentB=new(){Name="CompatParentB",PlaceholderText="从种马库选亲代 B",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly Grid parentGrid=new(){ColumnSpacing=12,RowSpacing=12};
    private readonly StackPanel[] parentCards=[new(){Spacing=8},new(){Spacing=8}];
    private readonly Grid[] ancestorGrids=[new(){ColumnSpacing=8,RowSpacing=8},new(){ColumnSpacing=8,RowSpacing=8}];
    private readonly Border targetVisual=new(){Name="CompatTargetCard"};
    private readonly Border[] nodeVisuals=NodeKeys.Select(key=>new Border{Name="CompatCard_"+key}).ToArray();
    private readonly Expander targetEditor=new(){Name="CompatTargetEditor",Header="选择育成目标",IsExpanded=true};
    private readonly Expander manualEditor=new(){Name="CompatManualEditor",Header="手动补选 / 调整亲代与祖辈",IsExpanded=false};
    private readonly Grid manualGrid=new(){ColumnSpacing=12,RowSpacing=12};
    private readonly TextBlock progress=Label("第 1 步：先选育成目标，再选两匹库内种马。",13);
    private readonly TextBlock scoreSummary=Label("尚未计算基本相性",16);
    private readonly TextBlock coverageSummary=Label("选好亲代后，可一起核对目标因子覆盖。",12);
    private readonly StackPanel candidateList=new(){Name="CompatCandidateList",Spacing=5};
    private readonly Dictionary<string,CheckBox> candidateBoxes=new(StringComparer.Ordinal);
    private readonly HashSet<string> candidateIds=new(StringComparer.Ordinal);
    private readonly Button evaluate=new(){Name="CompatEvaluate",Content="计算基本相性"};
    private readonly Button suggest=new(){Name="CompatSuggest",Content="联合筛选：相性与目标因子"};
    private readonly TextBlock status=Label("正在读取本地角色与种马库…",12);
    private readonly TextBlock modeStatus=Label("从种马库选择两匹成品，四个祖辈会自动带入；未知的节点可点卡片补选。",12);
    private readonly StackPanel result=new(){Name="CompatEvaluation",Spacing=8};
    private readonly StackPanel suggestions=new(){Name="CompatSuggestions",Spacing=8};
    private EntryChoice[] entries=[],allEntries=[];private AccountChoice[] accounts=[];private JObject[] savedPlans=[];
    private bool loaded,applying,manual,busy,planningAvailable,planningEverAvailable,candidateExplicit;private int revision,stateRevision;
    private string? activePlanId,libraryId,planLibraryId;private string?[] unresolvedParentIds=[];private JObject? lastMutation;
    private string? AccountId=>(account.SelectedItem as AccountChoice)?.Id;
    private JToken? evaluatedGoals,suggestedGoals;private int? suggestedTarget;
    public UmaParentCompatibilityView(Func<JObject,Task<JObject>> request,Func<JObject> planningGoals,Action<JObject>? restoreGoals=null)
    {
        this.request=request;this.planningGoals=planningGoals;this.restoreGoals=restoreGoals;
        var frame=new Grid{RowDefinitions=new("Auto,*"),RowSpacing=10,Margin=new Thickness(0,10,0,0)};
        var summary=new StackPanel{Spacing=4};progress.Name="CompatSelectionProgress";scoreSummary.Name="CompatScoreSummary";coverageSummary.Name="CompatCoverageSummary";
        summary.Children.Add(progress);summary.Children.Add(scoreSummary);summary.Children.Add(coverageSummary);
        var summaryRow=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=12};summaryRow.Children.Add(summary);
        evaluate.VerticalAlignment=VerticalAlignment.Center;Grid.SetColumn(evaluate,1);summaryRow.Children.Add(evaluate);frame.Children.Add(Card(summaryRow));
        var root=new StackPanel{Spacing=10};
        root.Children.Add(BuildPlanningToolbar());
        root.Children.Add(Label("1 选目标  →  2 选两匹种马  →  3 看相性与目标覆盖",16));
        targetVisual.MaxWidth=620;targetVisual.HorizontalAlignment=HorizontalAlignment.Center;root.Children.Add(targetVisual);
        targetEditor.Content=target;
        // Suki's collapsing transition clips still-visible content; hide this editor immediately.
        targetEditor.PropertyChanged+=(_,change)=>{if(change.Property==Expander.IsExpandedProperty)target.IsVisible=targetEditor.IsExpanded;};
        root.Children.Add(targetEditor);
        var branch=Label("↓  由两匹亲代与各自的祖辈组成家系",12);branch.HorizontalAlignment=HorizontalAlignment.Center;root.Children.Add(branch);
        foreach(var side in Enumerable.Range(0,2))
        {
            var card=parentCards[side];card.Children.Add(nodeVisuals[side*3]);card.Children.Add(side==0?parentA:parentB);
            card.Children.Add(Label(side==0?"↓  亲代 A 的双亲":"↓  亲代 B 的双亲",12));
            for(var j=0;j<2;j++){var visual=nodeVisuals[side*3+j+1];Grid.SetColumn(visual,j);ancestorGrids[side].Children.Add(visual);}card.Children.Add(ancestorGrids[side]);parentGrid.Children.Add(Card(card));
            var editor=new StackPanel{Spacing=8};foreach(var node in nodes.Skip(side*3).Take(3))editor.Children.Add(node);manualGrid.Children.Add(editor);
        }
        root.Children.Add(parentGrid);root.Children.Add(modeStatus);
        manualEditor.Content=manualGrid;root.Children.Add(manualEditor);root.Children.Add(ownershipPanel);
        var actions=new WrapPanel{Orientation=Orientation.Horizontal};var reload=new Button{Name="CompatRestoreLibraryNodes",Content="重新从库带入祖辈",Margin=new Thickness(0,0,8,0)};reload.Click+=(_,_)=>FillFromLibrary();actions.Children.Add(reload);root.Children.Add(actions);root.Children.Add(status);root.Children.Add(result);
        var candidateBody=new StackPanel{Spacing=8};candidateBody.Children.Add(Label("父母从同一份库内候选范围中组合，不区分两套候选池。",12));var all=new WrapPanel{Orientation=Orientation.Horizontal};
        foreach(var option in new[]{("全选",true),("清空",false)}){var button=new Button{Content=option.Item1,Margin=new Thickness(0,0,8,0)};button.Click+=(_,_)=>{applying=true;foreach(var row in candidateBoxes)row.Value.IsChecked=option.Item2;candidateExplicit=!option.Item2;candidateIds.Clear();if(option.Item2)foreach(var entry in entries)candidateIds.Add(entry.Id);applying=false;Invalidate();};all.Children.Add(button);}
        candidateBody.Children.Add(all);candidateBody.Children.Add(new ScrollViewer{Content=candidateList,MaxHeight=210});
        root.Children.Add(new Expander{Name="CompatCandidateScope",Header="候选范围（父母共用）",Content=candidateBody,IsExpanded=false});root.Children.Add(suggest);root.Children.Add(suggestions);
        var scroll=new ScrollViewer{Name="CompatFamilyScroll",Content=root,HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled};Grid.SetRow(scroll,1);frame.Children.Add(scroll);Content=frame;
        parentA.SelectionChanged+=(_,_)=>{if(!applying){unresolvedParentIds=[];planLibraryId=libraryId;FillFromLibrary();}};parentB.SelectionChanged+=(_,_)=>{if(!applying){unresolvedParentIds=[];planLibraryId=libraryId;FillFromLibrary();}};target.Changed+=()=>{if(target.SelectedId!=null)targetEditor.IsExpanded=false;Invalidate();};
        foreach(var node in nodes)node.Changed+=()=>{if(applying)return;manual=true;unresolvedParentIds=[];planLibraryId=libraryId;modeStatus.Text="已手动修改家系节点；本次不沿用库记录的已确认因子覆盖。";Invalidate();};
        evaluate.Click+=async(_,_)=>await Evaluate();suggest.Click+=async(_,_)=>await Suggest();SizeChanged+=(_,_)=>Reflow();
        account.SelectionChanged+=(_,_)=>{if(applying)return;RebuildEntries();Invalidate();};
        includeUnassigned.IsCheckedChanged+=(_,_)=>{if(applying)return;RebuildEntries();Invalidate();};
        savePlan.Click+=async(_,_)=>await SavePlan();loadPlan.Click+=(_,_)=>LoadPlan();deletePlan.Click+=async(_,_)=>await DeletePlan();newPlan.Click+=(_,_)=>NewPlan();
        addAccount.Click+=async(_,_)=>await SaveAccount();bindEntries.Click+=async(_,_)=>await BindEntries();
        plans.SelectionChanged+=(_,_)=>UpdatePlanningButtons();
        Loaded+=async(_,_)=>{if(!loaded)await LoadCatalog();else if((evaluatedGoals!=null&&!JToken.DeepEquals(evaluatedGoals,planningGoals()))||(suggestedGoals!=null&&!JToken.DeepEquals(suggestedGoals,GoalFields())))Invalidate();Reflow();};RefreshFamily();
    }
    private static TextBlock Label(string value,int size=13)=>new(){Text=value,FontSize=size,TextWrapping=TextWrapping.Wrap};
    private static string Text(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer?value.ToString():"";
    private static int? Id(JToken? value)=>value?.Type==JTokenType.Integer?(int?)value:null;
    private static string EntryLabel(JObject row)=>string.Join(" · ",new[]{Text(row["character_name"]),Text(row["variant_name"])}.Where(x=>x.Length>0));
    private Control BuildPlanningToolbar()
    {
        var body=new StackPanel{Spacing=8};body.Children.Add(Label("账号与家系方案",16));
        body.Children.Add(Label("先选账号；未关联成品可作为参考，其他账号的成品需明确允许借用。",12));
        body.Children.Add(account);body.Children.Add(includeUnassigned);body.Children.Add(accountStatus);
        var refresh=new Button{Name="CompatRefreshPlanning",Content="刷新账号、种马与方案"};refresh.Click+=async(_,_)=>{if(!busy)await LoadCatalog();};body.Children.Add(refresh);
        var planBody=new StackPanel{Spacing=7};planBody.Children.Add(plans);planBody.Children.Add(planName);
        var planActions=new WrapPanel{Orientation=Orientation.Horizontal};foreach(var button in new[]{newPlan,savePlan,loadPlan,deletePlan}){button.Margin=new Thickness(0,0,8,5);planActions.Children.Add(button);}planBody.Children.Add(planActions);planBody.Children.Add(planStatus);
        body.Children.Add(new Expander{Name="CompatPlanPanel",Header="保存 / 载入家系方案与草稿",Content=planBody,IsExpanded=false});
        var bindingBody=new StackPanel{Spacing=7};bindingBody.Children.Add(Label("勾选成品后指定所属账号；也能把它标记为当前规划账号可借。",12));
        bindingBody.Children.Add(new ScrollViewer{Content=bindingList,MaxHeight=190});bindingBody.Children.Add(Label("所选成品属于",12));bindingBody.Children.Add(entryOwner);
        borrowByCurrent.IsThreeState=true;borrowByCurrent.IsChecked=null;borrowByCurrent.Content="当前账号可借（横杠表示保留原设置）";bindingBody.Children.Add(borrowByCurrent);bindingBody.Children.Add(bindEntries);bindingBody.Children.Add(bindingStatus);
        bindingBody.Children.Add(new Separator());bindingBody.Children.Add(Label("添加其他电脑 / 非日活账号的备注",12));bindingBody.Children.Add(accountName);bindingBody.Children.Add(addAccount);
        ownershipPanel.Content=bindingBody;
        account.ItemsSource=new[]{new AccountChoice(null,"所有资料（仅参考）")};account.SelectedIndex=0;UpdatePlanningButtons();return Card(body);
    }
    private void UpdatePlanningButtons()
    {
        savePlan.IsEnabled=addAccount.IsEnabled=bindEntries.IsEnabled=planningAvailable&&!busy;
        loadPlan.IsEnabled=deletePlan.IsEnabled=planningAvailable&&!busy&&plans.SelectedItem is PlanChoice;
        borrowByCurrent.IsEnabled=planningAvailable&&AccountId!=null;
    }
    private static EntryChoice[] ReadEntries(JToken? token)
    {
        var rows=(token as JArray??new()).OfType<JObject>().Where(x=>Text(x["entry_id"]).Length>0).Select(x=>new EntryChoice(Text(x["entry_id"]),EntryLabel(x).Length>0?EntryLabel(x):"角色未确认的种马",x)).ToArray();
        var repeated=rows.GroupBy(x=>x.Label).Where(g=>g.Count()>1).Select(g=>g.Key).ToHashSet();
        return rows.Select(x=>repeated.Contains(x.Label)?x with{Label=x.Label+" · 记录 "+x.Id[..Math.Min(8,x.Id.Length)]}:x).ToArray();
    }
    private bool IsAvailable(EntryChoice entry)
    {
        if(!planningEverAvailable||AccountId==null)return true;var owner=Text(entry.Row["owner_account_id"]);
        return owner==AccountId||(entry.Row["borrowable_by"] as JArray??new()).Any(x=>Text(x)==AccountId)||(owner.Length==0&&includeUnassigned.IsChecked==true);
    }
    private void RebuildEntries(bool preserveMissing=false)
    {
        var oldApplying=applying;applying=true;try
        {
            var a=(parentA.SelectedItem as EntryChoice)?.Id;var b=(parentB.SelectedItem as EntryChoice)?.Id;
            entries=allEntries.Where(IsAvailable).ToArray();parentA.ItemsSource=parentB.ItemsSource=entries;
            parentA.SelectedItem=entries.FirstOrDefault(x=>x.Id==a);parentB.SelectedItem=entries.FirstOrDefault(x=>x.Id==b);
            candidateBoxes.Clear();candidateList.Children.Clear();if(!candidateExplicit){candidateIds.Clear();foreach(var entry in entries)candidateIds.Add(entry.Id);}
            foreach(var entry in entries)
            {
                var availability=Text(entry.Row["availability_label"]);var check=new CheckBox{Content=entry.Label+(availability.Length>0?" · "+availability:""),IsChecked=candidateIds.Contains(entry.Id)};candidateBoxes[entry.Id]=check;
                check.IsCheckedChanged+=(_,_)=>{if(applying)return;candidateExplicit=true;if(check.IsChecked==true)candidateIds.Add(entry.Id);else candidateIds.Remove(entry.Id);Invalidate();};candidateList.Children.Add(check);
            }
            var unassigned=allEntries.Count(x=>Text(x.Row["owner_account_id"]).Length==0);
            accountStatus.Text=planningAvailable?$"库内 {allEntries.Length} 匹 · 当前范围 {entries.Length} 匹 · 未关联 {unassigned} 匹。"+(AccountId==null?"当前为资料参考，未确认任何账号持有。":"仅列本账号自有、明确可借与可选的未关联参考。"):
                "账号与存档服务暂不可用；仍可查看基本家系，相性计算不确认账号持有。";
            borrowByCurrent.IsEnabled=planningAvailable&&AccountId!=null;
            if((a!=null&&parentA.SelectedItem==null)||(b!=null&&parentB.SelectedItem==null))
            {
                if(preserveMissing&&!manual){unresolvedParentIds=[a,b];modeStatus.Text="原亲代记录缺失或不再可用；保留原编号与节点，请重新选成品。";}
                else{unresolvedParentIds=[];manual=true;modeStatus.Text="所选亲代不在当前账号可用范围；已保留节点供参考，请重新选择成品。";}
            }
        }finally{applying=oldApplying;}RefreshFamily();
    }
    private void ApplyPlanningState(JObject state)
    {
        var oldAccount=AccountId;var oldOwner=entryOwner.SelectedItem is AccountChoice ownerChoice?ownerChoice.Id:KeepOwner;var selectedPlan=(plans.SelectedItem as PlanChoice)?.Id;
        var checkedBindings=bindingBoxes.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key).ToHashSet(StringComparer.Ordinal);
        var oldApplying=applying;applying=true;try
        {
            var nextLibrary=Text(state["library_id"]) is string id&&id.Length>0?id:null;
            if(libraryId!=null&&nextLibrary!=libraryId&&!manual)
            {
                var previousIds=new[]{(parentA.SelectedItem as EntryChoice)?.Id,(parentB.SelectedItem as EntryChoice)?.Id};
                if(previousIds.Any(x=>x!=null)){unresolvedParentIds=previousIds;parentA.SelectedItem=parentB.SelectedItem=null;planStatus.Text="种马库已变化，原亲代保留为参考；请重新选择当前库内成品。";}
            }
            stateRevision=Id(state["revision"])??0;libraryId=nextLibrary;if(activePlanId==null&&unresolvedParentIds.Length==0)planLibraryId=libraryId;
            accounts=(state["accounts"] as JArray??new()).OfType<JObject>().Where(x=>Text(x["id"]).Length>0).Select(x=>new AccountChoice(Text(x["id"]),Text(x["label"]))).ToArray();
            var accountOptions=new[]{new AccountChoice(null,"所有资料（仅参考）")}.Concat(accounts).ToList();if(oldAccount!=null&&accounts.All(x=>x.Id!=oldAccount))accountOptions.Add(new AccountChoice(oldAccount,"原账号已缺失 · "+oldAccount));
            account.ItemsSource=accountOptions;account.SelectedItem=accountOptions.First(x=>x.Id==oldAccount);
            var ownerOptions=new[]{new AccountChoice(KeepOwner,"保留每匹成品原有归属"),new AccountChoice(null,"未关联任何账号")}.Concat(accounts).ToArray();entryOwner.ItemsSource=ownerOptions;entryOwner.SelectedItem=ownerOptions.FirstOrDefault(x=>x.Id==oldOwner)??ownerOptions[0];
            allEntries=ReadEntries(state["entries"]);savedPlans=(state["plans"] as JArray??new()).OfType<JObject>().ToArray();
            var planOptions=savedPlans.Select(x=>new PlanChoice(Text(x["id"]),Text(x["name"])+(IsDraft(x)?"（草稿）":""),x)).ToArray();plans.ItemsSource=planOptions;plans.SelectedItem=planOptions.FirstOrDefault(x=>x.Id==selectedPlan||x.Id==activePlanId);
            bindingBoxes.Clear();bindingList.Children.Clear();foreach(var entry in allEntries){var check=new CheckBox{Content=entry.Label+" · "+(Text(entry.Row["availability_label"]) is string label&&label.Length>0?label:"未关联"),IsChecked=checkedBindings.Contains(entry.Id)};bindingBoxes[entry.Id]=check;bindingList.Children.Add(check);}
            planningAvailable=planningEverAvailable=true;RebuildEntries(true);
        }finally{applying=oldApplying;}UpdatePlanningButtons();
    }
    private static bool IsDraft(JObject plan)=>Id(plan["target_character_id"])==null||((Text(plan["selection_mode"])=="entries")?((plan["parent_entry_ids"] as JArray)?.Count!=2||(plan["parent_entry_ids"] as JArray??new()).Any(x=>x.Type==JTokenType.Null)):(plan["lineage"] as JArray??new()).Any(x=>x.Type==JTokenType.Null));
    private async Task<bool> MutatePlanning(JObject command,string success)
    {
        if(!planningAvailable||busy)return false;busy=true;UpdatePlanningButtons();command["expected_revision"]=stateRevision;
        try{var response=await request(command);lastMutation=response;ApplyPlanningState(response);planStatus.Text=success;return true;}
        catch(InvalidOperationException e){planStatus.Text=e.Message+" 如其他窗口已修改，请先刷新后重试。";return false;}
        catch{planStatus.Text="本地方案操作未完成；请刷新账号与方案后重试。";return false;}
        finally{busy=false;UpdatePlanningButtons();}
    }
    public async Task SaveAccount()
    {
        var name=(accountName.Text??"").Trim();if(name.Length==0){bindingStatus.Text="先填写便于辨认的账号备注。";return;}
        if(await MutatePlanning(new JObject{["action"]="parent_planning_account_save",["label"]=name},"手动账号已保存；可选择后关联成品。")){accountName.Text="";bindingStatus.Text="账号备注已保存；此处不保存账号或密码。";}
    }
    public async Task BindEntries()
    {
        if(busy||!planningAvailable)return;var selected=bindingBoxes.Where(x=>x.Value.IsChecked==true).Select(x=>x.Key).ToHashSet(StringComparer.Ordinal);if(selected.Count==0){bindingStatus.Text="请先勾选需要关联的成品。";return;}
        var chosenOwner=(entryOwner.SelectedItem as AccountChoice)?.Id;var current=AccountId;var borrow=borrowByCurrent.IsChecked;
        var grouped=allEntries.Where(x=>selected.Contains(x.Id)).Select(entry=>
        {
            var borrowers=(entry.Row["borrowable_by"] as JArray??new()).Select(Text).Where(x=>x.Length>0).ToHashSet(StringComparer.Ordinal);
            var owner=chosenOwner==KeepOwner?Text(entry.Row["owner_account_id"]):chosenOwner;if(owner?.Length==0)owner=null;
            if(current!=null&&borrow!=null){if(borrow==true)borrowers.Add(current);else borrowers.Remove(current);}if(owner!=null)borrowers.Remove(owner);
            return new{entry.Id,Owner=owner,Borrowers=borrowers.OrderBy(x=>x,StringComparer.Ordinal).ToArray()};
        }).GroupBy(x=>(x.Owner??"")+"\n"+string.Join("\n",x.Borrowers)).ToArray();
        foreach(var group in grouped)
        {
            if(!await MutatePlanning(new JObject{["action"]="parent_planning_bind",["entry_ids"]=new JArray(group.Select(x=>x.Id)),["owner_account_id"]=group.First().Owner==null?JValue.CreateNull():new JValue(group.First().Owner),["borrowable_by"]=new JArray(group.First().Borrowers)},"成品归属已保存。")){bindingStatus.Text="本次关联尚未全部完成；已完成的分组已保存，可刷新后继续。";return;}
        }
        borrowByCurrent.IsChecked=null;bindingStatus.Text=$"已保存 {selected.Count} 匹成品的归属；其他账号原有借用设置保留。";Invalidate();
    }
    private JObject CurrentPlan()
    {
        var goals=GoalFields();var ids=manual?Array.Empty<string?>():unresolvedParentIds.Length==2?unresolvedParentIds:new[]{(parentA.SelectedItem as EntryChoice)?.Id,(parentB.SelectedItem as EntryChoice)?.Id};
        return new JObject{["id"]=activePlanId==null?JValue.CreateNull():new JValue(activePlanId),["name"]=(planName.Text??"").Trim(),["account_id"]=AccountId==null?JValue.CreateNull():new JValue(AccountId),["library_id"]=planLibraryId==null?JValue.CreateNull():new JValue(planLibraryId),["target_character_id"]=target.SelectedId is int targetId?new JValue(targetId):JValue.CreateNull(),["selection_mode"]=manual?"manual":"entries",["parent_entry_ids"]=new JArray(ids.Select(x=>x==null?JValue.CreateNull():new JValue(x))),["lineage"]=new JArray(nodes.Select(x=>x.SelectedId is int id?(JToken)new JValue(id):JValue.CreateNull())),["targets"]=goals["targets"]!.DeepClone(),["raw_targets"]=goals["raw_targets"]!.DeepClone(),["candidate_entry_ids"]=candidateExplicit?new JArray(candidateIds.OrderBy(x=>x,StringComparer.Ordinal)):JValue.CreateNull(),["include_unassigned"]=includeUnassigned.IsChecked==true};
    }
    public async Task SavePlan()
    {
        var plan=CurrentPlan();if(Text(plan["name"]).Length==0){planStatus.Text="请给方案或草稿填写一个名字。";return;}
        if(await MutatePlanning(new JObject{["action"]="parent_planning_save",["plan"]=plan},IsDraft(plan)?"草稿已保存；可以稍后继续补齐。":"家系方案已保存。"))
        {
            activePlanId=Text(lastMutation?["saved_plan"]?["id"]);if(activePlanId.Length==0)activePlanId=null;
            plans.SelectedItem=plans.Items.OfType<PlanChoice>().FirstOrDefault(x=>x.Id==activePlanId);
        }
    }
    public void NewPlan()
    {
        if(busy)return;applying=true;try{activePlanId=null;planLibraryId=libraryId;unresolvedParentIds=[];planName.Text="";plans.SelectedItem=null;parentA.SelectedItem=parentB.SelectedItem=null;target.SetId(null);foreach(var node in nodes)node.SetId(null);manual=false;candidateExplicit=false;candidateIds.Clear();RebuildEntries();}finally{applying=false;}
        Invalidate();planStatus.Text="已新建空白家系，保留当前账号与外侧目标清单；填写后保存即可。";UpdatePlanningButtons();
    }
    public void LoadPlan()
    {
        if(busy||plans.SelectedItem is not PlanChoice choice)return;var plan=choice.Row;var requestedGoals=new JObject{["targets"]=(plan["targets"] as JArray??new()).DeepClone(),["raw_targets"]=(plan["raw_targets"] as JArray??new()).DeepClone()};
        if(restoreGoals==null&&!JToken.DeepEquals(requestedGoals,GoalFields())){planStatus.Text="当前入口暂不能恢复目标清单；请从新版种马规划入口载入，避免以旧目标计算。";return;}
        applying=true;var warnings=new List<string>();try
        {
            activePlanId=choice.Id;planName.Text=Text(plan["name"]);var aid=Text(plan["account_id"]);var accountChoice=account.Items.OfType<AccountChoice>().FirstOrDefault(x=>(x.Id??"")==aid);
            if(accountChoice==null&&aid.Length>0){var added=new AccountChoice(aid,"原账号已缺失 · "+aid);account.ItemsSource=account.Items.OfType<AccountChoice>().Append(added).ToArray();accountChoice=added;warnings.Add("原账号资料缺失，未切换到其他账号。");}account.SelectedItem=accountChoice??account.Items.OfType<AccountChoice>().First(x=>x.Id==null);
            includeUnassigned.IsChecked=(bool?)plan["include_unassigned"]==true;candidateExplicit=plan["candidate_entry_ids"] is JArray;candidateIds.Clear();foreach(var id in (plan["candidate_entry_ids"] as JArray??new()).Select(Text))candidateIds.Add(id);RebuildEntries();
            target.SetId(Id(plan["target_character_id"]));var lineage=(plan["lineage"] as JArray??new()).ToArray();for(var i=0;i<nodes.Length;i++)nodes[i].SetId(Id(lineage.ElementAtOrDefault(i)));
            var ids=(plan["parent_entry_ids"] as JArray??new()).Select(x=>x.Type==JTokenType.Null?null:Text(x)).ToArray();planLibraryId=Text(plan["library_id"]) is string lid&&lid.Length>0?lid:null;var sameLibrary=planLibraryId==libraryId;
            parentA.SelectedItem=sameLibrary?entries.FirstOrDefault(x=>x.Id==ids.ElementAtOrDefault(0)):null;parentB.SelectedItem=sameLibrary?entries.FirstOrDefault(x=>x.Id==ids.ElementAtOrDefault(1)):null;
            manual=Text(plan["selection_mode"])=="manual";unresolvedParentIds=[];
            if(!manual&&ids.Length==2&&((ids[0]!=null&&parentA.SelectedItem==null)||(ids[1]!=null&&parentB.SelectedItem==null))){unresolvedParentIds=ids;warnings.Add(sameLibrary?"原亲代记录缺失或不在当前账号可用范围；保留原记录编号和节点，请补选。":"本方案来自另一份种马库；保留原节点，不以同名成品替代。");}
            if(!manual&&unresolvedParentIds.Length==0)
            {
                foreach(var side in Enumerable.Range(0,2))
                {
                    var selected=(side==0?parentA.SelectedItem:parentB.SelectedItem) as EntryChoice;if(selected==null)continue;
                    nodes[side*3].SetId(Id(selected.Row["character_id"]),Text(selected.Row["character_name"]));var parents=(selected.Row["parents"] as JArray??new()).OfType<JObject>().ToArray();
                    for(var j=0;j<2;j++){var ancestor=parents.ElementAtOrDefault(j);nodes[side*3+j+1].SetId(Id(ancestor?["character_id"]),Text(ancestor?["character_name"]));}
                }
            }
            if(candidateExplicit&&candidateIds.Any(id=>allEntries.All(x=>x.Id!=id)))warnings.Add("候选范围中部分成品已缺失，未自动补入其他成品。");
            foreach(var warning in (plan["warnings"] as JArray??new()).Select(Text).Where(x=>x.Length>0))if(!warnings.Contains(warning))warnings.Add(warning);
            restoreGoals?.Invoke(requestedGoals);targetEditor.IsExpanded=target.SelectedId==null;manualEditor.IsExpanded=warnings.Count>0;
        }finally{applying=false;}
        Invalidate();modeStatus.Text=manual?"载入手动家系；不声明账号持有，也不沿用成品因子覆盖。":"已载入保存的成品编号与家系快照；计算时重新核对当前种马库。";
        planStatus.Text="已载入“"+choice.Label+"”。"+(warnings.Count>0?string.Join(" ",warnings):"目标、账号和候选范围均已恢复。");UpdatePlanningButtons();
    }
    public async Task DeletePlan()
    {
        if(plans.SelectedItem is not PlanChoice choice)return;if(await MutatePlanning(new JObject{["action"]="parent_planning_delete",["plan_id"]=choice.Id},"已删除保存的方案；当前编辑家系保留。")){if(activePlanId==choice.Id)activePlanId=null;UpdatePlanningButtons();}
    }
    private void Reflow()
    {
        var narrow=Bounds.Width<820;LayoutColumns(parentGrid,narrow);LayoutColumns(manualGrid,narrow);
        foreach(var grid in ancestorGrids)LayoutColumns(grid,Bounds.Width<430);
    }
    private static void LayoutColumns(Grid grid,bool single)
    {
        grid.ColumnDefinitions=new(single?"*":"*,*");grid.RowDefinitions=new(single?"Auto,Auto":"Auto");
        for(var i=0;i<grid.Children.Count;i++){Grid.SetColumn(grid.Children[i],single?0:i);Grid.SetRow(grid.Children[i],single?i:0);}
    }
    private void Invalidate()
    {
        if(applying)return;revision++;result.Children.Clear();suggestions.Children.Clear();evaluatedGoals=null;suggestedGoals=null;suggestedTarget=null;
        scoreSummary.Text="选择已调整 · 尚未计算基本相性";coverageSummary.Text=manual?"手动家系仅计算基本相性；不沿用成品记录的已确认因子。":"计算后显示目标因子已满足、待补与未确认。";
        status.Text="选择已调整，点击上方“计算基本相性”查看结果。";RefreshFamily();
    }
    private void RefreshFamily()
    {
        var parentCount=new[]{parentA,parentB}.Count(x=>x.SelectedItem is EntryChoice);
        var knownAncestors=new[]{1,2,4,5}.Count(i=>nodes[i].SelectedId!=null);
        progress.Text=$"育成目标 {(target.SelectedId==null?"待选":"已选")}  ·  库内亲代 {parentCount}/2  ·  祖辈已确认 {knownAncestors}/4";
        targetVisual.Child=NodeCard("育成目标",target.Selected,"本次准备育成的角色",()=>{targetEditor.IsExpanded=!targetEditor.IsExpanded;if(targetEditor.IsExpanded)target.BringIntoView();},true);
        for(var i=0;i<nodes.Length;i++)
        {
            var index=i;var entry=(i<3?parentA.SelectedItem:parentB.SelectedItem) as EntryChoice;var source=manual?"手动家系 · 未绑定成品因子":entry==null?"尚未选择库内种马":i%3==0?"库内成品 · 记录 "+entry.Id[..Math.Min(8,entry.Id.Length)]:"来自亲代 "+(i<3?"A":"B")+" 的库内记录";
            if(!manual&&i%3==0&&unresolvedParentIds.ElementAtOrDefault(i<3?0:1) is string missing)source="原成品 "+missing+" 当前不可用 · 请重新选择";
            if(nodes[i].SelectedId==null)source="角色未确认 · 点击补选";
            var title=i%3==0?NodeLabels[i]:"祖辈 "+(i<3?"A":"B")+(i%3);
            nodeVisuals[i].Child=NodeCard(title,nodes[i].Selected,source,()=>{manualEditor.IsExpanded=true;nodes[index].BringIntoView();},false,i%3==0&&!manual?Text(entry?.Row["variant_name"]):"");
        }
    }
    private static Control NodeCard(string title,CharacterChoice? choice,string source,Action edit,bool isTarget=false,string variant="")
    {
        var content=new Grid{ColumnDefinitions=new("Auto,*,Auto"),ColumnSpacing=9,MinHeight=isTarget?66:62};
        var portrait=PortraitPath(choice,variant);var icon=new DisplayIcon{IconSource=portrait.Length>0?portrait:"?",IconSize=isTarget?56:46,VerticalAlignment=VerticalAlignment.Center};
        ToolTip.SetTip(icon,portrait.Length>0?"本地角色头像，衣装仅用于辨认角色。":"本地没有对应头像；不会根据相似名字猜图。");content.Children.Add(icon);
        var labels=new StackPanel{Spacing=2,VerticalAlignment=VerticalAlignment.Center};var caption=Label(title,11);caption.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));labels.Children.Add(caption);
        labels.Children.Add(new TextBlock{Text=choice?.Id!=null?choice.Name:"未指定角色",FontSize=isTarget?17:14,FontWeight=FontWeight.SemiBold,TextWrapping=TextWrapping.Wrap});
        var provenance=Label(source,11);provenance.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));labels.Children.Add(provenance);Grid.SetColumn(labels,1);content.Children.Add(labels);
        var change=new Button{Content=choice?.Id==null?"选择":"修改",Padding=new Thickness(7,4),VerticalAlignment=VerticalAlignment.Center};change.Click+=(_,_)=>edit();Grid.SetColumn(change,2);content.Children.Add(change);
        var card=new Border{Child=content,Padding=new Thickness(10),CornerRadius=new CornerRadius(10),BorderThickness=new Thickness(1),HorizontalAlignment=HorizontalAlignment.Stretch};
        card.Bind(Border.BorderBrushProperty,new DynamicResourceExtension(isTarget?"SukiPrimaryColor":"SukiControlBorderBrush"));card.Bind(Border.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));return card;
    }
    private static string PortraitPath(CharacterChoice? choice,string variant)
    {
        if(choice?.Id==null)return "";var outfits=(choice.Row?["outfits"] as JArray??new()).OfType<JObject>().ToArray();
        var ordered=outfits.OrderByDescending(x=>variant.Length>0&&Text(x["name"])==variant);
        foreach(var outfit in ordered)
        {
            var id=Text(outfit["variant_id"]);if(id.Length!=6||!id.All(char.IsDigit))continue;
            var path=Path.Combine(AppContext.BaseDirectory,"resource","portraits",id+".png");if(File.Exists(path))return path;
        }
        return "";
    }
    public async Task LoadCatalog()
    {
        var wasLoaded=loaded;
        try
        {
            var data=await request(new JObject{["action"]="parent_compatibility_catalog"});applying=true;
            var characters=(data["characters"] as JArray??new()).OfType<JObject>().Where(x=>Id(x["character_id"])!=null).Select(x=>new CharacterChoice(Id(x["character_id"]),Text(x["name"])+((bool?)x["supported"]==false?"（关系资料待补）":""),x)).ToArray();
            target.Load(characters);foreach(var node in nodes)node.Load(characters);
            var catalogEntries=ReadEntries(data["entries"]);
            try{var state=await request(new JObject{["action"]="parent_planning_state"});ApplyPlanningState(state);}
            catch
            {
                planningAvailable=false;if(!planningEverAvailable){allEntries=catalogEntries;RebuildEntries();}
                accountStatus.Text=planningEverAvailable?"账号服务暂不可读；当前资料保留供查看，请刷新后再计算或保存。":"账号与存档服务暂不可用；基本家系仍可使用。更新完整组件后可关联账号和保存方案。";UpdatePlanningButtons();
            }
            loaded=true;status.Text=entries.Length>0?$"已读取 {entries.Length} 匹库内种马。请选择育成目标与亲代。":"种马库暂无成品记录；可先在种马库录入，也可手动补选节点查看基本分。";
        }
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="本地相性资料暂不可读，请检查资料安装后重试。";}finally{applying=false;if(wasLoaded)Invalidate();else RefreshFamily();}
    }
    private void FillFromLibrary()
    {
        applying=true;try
        {
            foreach(var side in Enumerable.Range(0,2))
            {
                var entry=(side==0?parentA.SelectedItem:parentB.SelectedItem) as EntryChoice;var row=entry?.Row;nodes[side*3].SetId(Id(row?["character_id"]),Text(row?["character_name"]));
                var parents=(row?["parents"] as JArray??new()).OfType<JObject>().ToArray();for(var j=0;j<2;j++){var ancestor=parents.ElementAtOrDefault(j);nodes[side*3+j+1].SetId(Id(ancestor?["character_id"]),Text(ancestor?["character_name"]));}
            }
            manual=false;modeStatus.Text="已从当前库记录带入亲代与祖辈；资料未知的节点可手动补选。";
        }finally{applying=false;}Invalidate();
    }
    private JObject GoalFields(){var goals=planningGoals();return new JObject{["targets"]=(goals["targets"] as JArray??new()).DeepClone(),["raw_targets"]=(goals["raw_targets"] as JArray??new()).DeepClone()};}
    private bool UsesEntries=>!manual&&unresolvedParentIds.Length==0&&parentA.SelectedItem is EntryChoice&&parentB.SelectedItem is EntryChoice;
    public async Task Evaluate()
    {
        if(busy)return;if(planningEverAvailable&&!planningAvailable){status.Text="账号资料暂不可读，请刷新后再计算，避免使用过期的账号范围。";return;}if(target.SelectedId is not int targetId){status.Text="请先选择育成目标角色。";return;}if(!manual&&unresolvedParentIds.Length>0){status.Text="保存的亲代记录当前不可用；请重新选择库内成品，或明确改为手动家系参考。";return;}
        var version=revision;var automatic=UsesEntries;var goals=GoalFields();var command=new JObject{["action"]=planningAvailable?"parent_planning_evaluate":"parent_compatibility_evaluate",["target_character_id"]=targetId};
        if(automatic){command["parent_entry_ids"]=new JArray(((EntryChoice)parentA.SelectedItem!).Id,((EntryChoice)parentB.SelectedItem!).Id);foreach(var field in goals.Properties())command[field.Name]=field.Value.DeepClone();}
        else command["lineage"]=new JArray(nodes.Select(x=>x.SelectedId is int id?(JToken)new JValue(id):JValue.CreateNull()));
        if(planningAvailable){command["account_id"]=AccountId==null?JValue.CreateNull():new JValue(AccountId);command["include_unassigned"]=includeUnassigned.IsChecked==true;foreach(var field in goals.Properties())command[field.Name]=field.Value.DeepClone();}
        busy=true;evaluate.IsEnabled=suggest.IsEnabled=false;status.Text="正在计算本地基本相性…";
        try{var response=await request(command);if(version!=revision||!JToken.DeepEquals(goals,GoalFields()))return;RenderEvaluation(response,automatic);evaluatedGoals=goals;status.Text="已按当前七节点读取基本相性；缺少资料的项目单独列出。";}
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="基本相性暂未计算成功，请检查所选角色与资料。";}finally{busy=false;evaluate.IsEnabled=suggest.IsEnabled=true;}
    }
    private static string Score(JObject value)=>Id(value["total"]) is int total?$"基本相性 {total} · {Text(value["grade"])}":$"已知基本分 {Id(value["known_subtotal"])??0} · 资料未齐，总分未确认";
    private static string Reason(string code)=>code switch{"missing_character_id"=>"未确认角色","ambiguous_character_name"=>"名字有歧义，请手动选择","invalid_character_id"=>"原记录角色需要补选","character_not_in_dataset"=>"关系资料尚未收录","ancestor_is_target"=>"祖辈与目标同角色，本项按规则为0","unresolved_node"=>"本项节点不完整",_=>"相关资料尚未齐全"};
    private static string NodeName(string key)=>key=="target"?"育成目标":Array.IndexOf(NodeKeys,key) is int i&&i>=0?NodeLabels[i]:"家系节点";
    private void RenderEvaluation(JObject value,bool fromEntries)
    {
        result.Children.Clear();scoreSummary.Text=Score(value);var coverage=fromEntries?value["factor_coverage"] as JObject:null;
        coverageSummary.Text=!fromEntries?"手动家系：本次未统计已确认因子覆盖。":coverage==null?"本次未附目标覆盖资料。":$"目标因子：已满足 {(coverage["matched"] as JArray)?.Count??0} 项  ·  待补 {(coverage["missing"] as JArray)?.Count??0} 项  ·  未确认 {(coverage["unknown"] as JArray)?.Count??0} 项";
        var body=new StackPanel{Spacing=7};body.Children.Add(Label(Score(value),18));
        foreach(var issue in (value["selection_issues"] as JArray??new()).Select(Text).Where(x=>x.Length>0))body.Children.Add(Label("游戏选择提示："+issue,12));
        var breakdown=new StackPanel{Spacing=5};foreach(var row in (value["detail"] as JArray??new()).OfType<JObject>()){var score=Id(row["score"]);breakdown.Children.Add(Label(Text(row["label"])+"："+(score is int n?n.ToString():"资料不足")));}
        body.Children.Add(new Expander{Name="CompatScoreBreakdown",Header="查看基本分构成",Content=breakdown,IsExpanded=false});
        foreach(var row in (value["unresolved"] as JArray??new()).OfType<JObject>())body.Children.Add(Label(NodeName(Text(row["node"]))+" · "+Reason(Text(row["reason"])),12));
        foreach(var note in (value["limitations"] as JArray??new()).Select(Text).Where(x=>x.Length>0))body.Children.Add(Label(note,11));result.Children.Add(Card(body));
        result.Children.Add(Coverage(fromEntries?value["factor_coverage"] as JObject:null,fromEntries));
    }
    private static Control Coverage(JObject? coverage,bool bound)
    {
        var body=new StackPanel{Spacing=5};body.Children.Add(Label("规划目标因子覆盖",16));
        if(coverage==null){body.Children.Add(Label(bound?"当前结果未附目标覆盖资料；可在种马素材页设置目标后重新计算。":"手动节点没有绑定成品种马记录，本次未统计已确认因子覆盖。",12));return Card(body);}
        foreach(var field in new[]{("matched","已满足"),("missing","待补"),("unknown","未确认")}){var names=(coverage[field.Item1] as JArray??new()).Select(Text).Where(x=>x.Length>0).ToArray();if(names.Length>0)body.Children.Add(Label(field.Item2+"："+string.Join("、",names),12));}
        foreach(var check in (coverage["checks"] as JArray??new()).OfType<JObject>()){var known=Id(check["known_stars"]);body.Children.Add(Label(Text(check["name"])+" · 已确认 "+(known is int n?n+"星":"未知")+" / 目标 "+(Id(check["required_stars"])??0)+"星",12));}
        var message=Text(coverage["message"]);if(message.Length>0)body.Children.Add(Label(message,11));return Card(body);
    }
    public async Task Suggest()
    {
        if(busy)return;if(planningEverAvailable&&!planningAvailable){status.Text="账号资料暂不可读，请刷新后再筛选，避免使用过期的账号范围。";return;}if(target.SelectedId is not int targetId){status.Text="请先选择育成目标角色。";return;}if(candidateIds.Count==0){status.Text="请先选择库内候选种马；父母共用同一份候选范围。";return;}
        var version=revision;var goals=GoalFields();var command=new JObject{["action"]=planningAvailable?"parent_planning_suggest":"parent_compatibility_suggest",["target_character_id"]=targetId,["candidate_entry_ids"]=new JArray(entries.Where(x=>candidateIds.Contains(x.Id)).Select(x=>x.Id)),["limit"]=20};foreach(var field in goals.Properties())command[field.Name]=field.Value.DeepClone();
        if(planningAvailable){command["account_id"]=AccountId==null?JValue.CreateNull():new JValue(AccountId);command["include_unassigned"]=includeUnassigned.IsChecked==true;}
        busy=true;suggest.IsEnabled=evaluate.IsEnabled=false;status.Text="正在比较已选库内父母组合…";
        try
        {
            var response=await request(command);if(version!=revision||!JToken.DeepEquals(goals,GoalFields()))return;suggestedGoals=goals;suggestedTarget=targetId;suggestions.Children.Clear();suggestions.Children.Add(Label(planningAvailable?"账号范围内的联合组合参考":"库内组合参考（账号归属未确认）",18));
            var ranking=Text(response["ranking_description"]);if(ranking.Length>0)suggestions.Children.Add(Label(ranking,12));
            var resourceMessage=Text(response["resource_filter_message"]);if(resourceMessage.Length>0)suggestions.Children.Add(Label(resourceMessage,12));
            var rows=(response["suggestions"] as JArray??new()).OfType<JObject>().ToArray();if(rows.Length==0)suggestions.Children.Add(Label("当前范围没有可比较的父母组合，可补充种马库记录或调整候选。"));
            foreach(var row in rows){var ids=(row["parent_entry_ids"] as JArray??new()).Values<string>().OfType<string>().ToArray();if(ids.Length!=2)continue;var body=new StackPanel{Spacing=6};body.Children.Add(Label(string.Join(" ＋ ",ids.Select(id=>entries.FirstOrDefault(x=>x.Id==id)?.Label??"库内记录")),15));body.Children.Add(Label(Score(row)));body.Children.Add(Coverage(row["factor_coverage"] as JObject,true));var use=new Button{Name="CompatUseSuggestion",Content="选用这组父母"};use.Click+=(_,_)=>UseSuggestion(row,ids);body.Children.Add(use);suggestions.Children.Add(Card(body));}
            foreach(var note in (response["limitations"] as JArray??new()).Select(Text).Where(x=>x.Length>0))suggestions.Children.Add(Label(note,11));status.Text=$"已比较 {Text(response["pair_count"])} 组，仅限所选库内成品候选。";
        }
        catch(InvalidOperationException e){status.Text=e.Message;}catch{status.Text="库内组合暂未读取成功，请检查资料后重试。";}finally{busy=false;suggest.IsEnabled=evaluate.IsEnabled=true;}
    }
    private void UseSuggestion(JObject response,string[] ids)
    {
        if(suggestedTarget!=target.SelectedId||!JToken.DeepEquals(suggestedGoals,GoalFields())){status.Text="目标已变化，请重新比较组合。";return;}
        var a=entries.FirstOrDefault(x=>x.Id==ids[0]);var b=entries.FirstOrDefault(x=>x.Id==ids[1]);if(a==null||b==null){status.Text="库记录已变化，请刷新资料。";return;}
        applying=true;parentA.SelectedItem=a;parentB.SelectedItem=b;applying=false;FillFromLibrary();RenderEvaluation(response,true);evaluatedGoals=GoalFields();status.Text="已回填两匹库内亲代及祖辈；可继续核对基本分和因子覆盖。";
    }
    private static GlassCard Card(Control content)=>new(){Content=content,Padding=new Thickness(14),CornerRadius=new CornerRadius(10),IsAnimated=false,IsOpaque=true};
}
