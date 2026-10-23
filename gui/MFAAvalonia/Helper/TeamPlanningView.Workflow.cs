using System;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class TeamPlanningView
{
    private readonly Button restoreDraft=new(){Name="TeamRestorePreviousDraft",Content="恢复切换前的编辑",IsVisible=false,Margin=new Thickness(0,0,8,5)};
    private readonly TextBlock flowHeading=TeamPlanningUi.Title("从一句目标开始",15);
    private readonly ProgressBar planningProgress=new(){Name="TeamPlanningProgress",IsIndeterminate=true,Height=4,IsVisible=false};
    private readonly WrapPanel planningSteps=new(){Name="TeamPlanningSteps"};
    private readonly Border emptyTeam=new(){Name="TeamPlanningEmptyState"};
    private JObject? previousPlanningDraft;
    private bool planningError;

    private Control BuildPlanningWorkflow()
    {
        foreach(var label in new[]{"1  目标","2  攻略（可选）","3  生成团队","4  成员系谱"}){
            var index=planningSteps.Children.Count;var button=new Button{Name="TeamStep"+index,Content=label,Margin=new Thickness(0,0,8,4)};
            button.Click+=(_,_)=>{
                workspaces.SelectedIndex=index==3?1:0;
                if(index==0)description.Focus();
                if(index==1)DispatcherPost("TeamResearchStep");
                if(index==2)DispatcherPost("TeamGenerateStep");
            };planningSteps.Children.Add(button);
        }
        var body=new StackPanel{Spacing=5,Children={planningSteps,flowHeading,planningProgress}};
        var text=TeamPlanningUi.Note("");text.Name="TeamPlanningFeedback";text.MaxLines=5;
        text.Bind(TextBlock.TextProperty,status.GetObservable(TextBlock.TextProperty));body.Children.Add(text);
        return TeamPlanningUi.Card(body);
    }

    private void DispatcherPost(string name)
        =>Avalonia.Threading.Dispatcher.UIThread.Post(()=>{
            var control=this.GetLogicalDescendants().OfType<Control>().FirstOrDefault(x=>x.Name==name);
            control?.BringIntoView();
        });

    private Control BuildEmptyTeam()
    {
        var body=new StackPanel{Spacing=10};body.Children.Add(TeamPlanningUi.Title("还没有团队方案，从目标开始就可以",18));
        body.Children.Add(TeamPlanningUi.Note("① 写比赛或点一个示例  →  ② 生成成员与配卡建议  →  ③ 选成员匹配种马系谱"));
        body.Children.Add(TeamPlanningUi.Note("账号资料、攻略和部分比赛条件可以后补。保存过的空草稿也能继续编辑。"));
        var button=new Button{Name="TeamEmptyStart",Content="去描述目标 / 生成团队"};button.Click+=(_,_)=>{workspaces.SelectedIndex=0;description.Focus();};body.Children.Add(button);
        emptyTeam.Child=TeamPlanningUi.Card(body);return emptyTeam;
    }

    private void RefreshPlanningSteps()
    {
        var count=((team["team_plan"] as JObject)?["members"] as JArray)?.OfType<JObject>().Count()??0;
        emptyTeam.IsVisible=count==0;restoreDraft.IsVisible=previousPlanningDraft!=null;restoreDraft.IsEnabled=!busy;
        if(workspaces.ItemsSource is TabItem[] tabs)tabs[1].Header=count>0?$"团队与系谱 · {count} 匹":"团队与系谱";
        flowHeading.Text=planningError?"本次没有生成可用结果，编辑仍保留":busy?"正在处理，请看下方进度":count>0?$"已有 {count} 位成员，选择成员继续匹配种马":"先写目标，攻略和账号资料可以后补";
        if(!busy)propose.Content=count>0?"按当前目标重新生成团队":"生成一套团队方案";
    }

    private void ShowPlanningFailure(string text)
    {
        planningError=true;status.Text=text;RefreshPlanningSteps();
    }

    private JObject ReadPlanningOverrides()
    {
        var value=requirementsEditor.ReadOverrides();
        // “AI 推荐” is an unset count, not an instruction to erase a number
        // which the user explicitly wrote in the description.
        if(value["team_size"]?.Type==JTokenType.Null)value.Remove("team_size");
        return value;
    }

    private async Task<bool> PrepareTeamGeneration()
    {
        if(busy)return false;
        if(string.IsNullOrWhiteSpace(description.Text)&&!string.IsNullOrWhiteSpace(teamName.Text))description.Text=teamName.Text;
        if(research==null||TeamPlanningUi.Str(research["research_id"]).Length==0||researchContext!=requirementsEditor.Read().ToString()){
            status.Text="正在自动整理目标与本地参考；不会自动联网。";
            await RetrieveGuides(false);
        }
        if(research==null||TeamPlanningUi.Str(research["research_id"]).Length==0||researchContext!=requirementsEditor.Read().ToString()){
            ShowPlanningFailure("本地准备没有完成。输入与上次方案已保留，可点“仅查本地参考”重试并查看具体提示。");return false;
        }
        return true;
    }

    private JObject CapturePlanningDraft()
    {
        StoreMemberRoute();var originKnown=OriginalRequest(out var originText);return new JObject{
            ["team"]=team.DeepClone(),["name"]=teamName.Text??"",["description"]=description.Text??"",["urls"]=guideUrls.Text??"",
            ["requirements"]=requirementsEditor.Read(),["research"]=research?.DeepClone(),["research_context"]=researchContext,
            ["source_ids"]=SelectedSourceIds(),["account_id"]=AccountId,["selected_member"]=selectedMember==null?null:MemberId(selectedMember),
            ["origin_known"]=originKnown,["origin_text"]=originText,
            ["saved_prior_id"]=ReferenceEquals(savedPriorTeam,team)?savedPriorId:""
        };
    }

    private void RememberPlanningDraft()
    {
        if(string.IsNullOrWhiteSpace(description.Text)&&string.IsNullOrWhiteSpace(teamName.Text)&&team.Count==0&&string.IsNullOrWhiteSpace(guideUrls.Text))return;
        previousPlanningDraft=CapturePlanningDraft();RefreshPlanningSteps();
    }

    private async Task RestorePlanningDraft()
    {
        if(busy||previousPlanningDraft==null)return;
        var previous=previousPlanningDraft;var current=CapturePlanningDraft();loading=true;
        try{
            team=Clone(previous["team"] as JObject??new());teamName.Text=TeamPlanningUi.Str(previous["name"]);
            generatedRequestTeam=(bool?)previous["origin_known"]==true?team:null;
            generatedRequestText=TeamPlanningUi.Str(previous["origin_text"]);
            savedPriorId=TeamPlanningUi.Str(previous["saved_prior_id"]);
            savedPriorTeam=savedPriorId.Length>0?team:null;
            displayedDescription=TeamPlanningUi.Str(previous["description"]);description.Text=displayedDescription;
            displayedGuideUrls=TeamPlanningUi.Str(previous["urls"]);guideUrls.Text=displayedGuideUrls;
            requirements=Clone(previous["requirements"] as JObject??new());requirementsEditor.Load(requirements);
            research=(previous["research"] as JObject)?.DeepClone() as JObject;researchContext=TeamPlanningUi.Str(previous["research_context"]);
            var wanted=TeamPlanningUi.Str(previous["account_id"]);var choices=(account.ItemsSource as System.Collections.Generic.IEnumerable<Choice>??[]).ToArray();
            if(wanted.Length>0&&choices.All(x=>x.Id!=wanted)){choices=choices.Append(new Choice(wanted,"原账号 · "+wanted)).ToArray();account.ItemsSource=choices;}
            account.SelectedItem=choices.FirstOrDefault(x=>x.Id==wanted)??choices.FirstOrDefault();
            selectedMember=null;memberWorkspace.IsVisible=false;seedCandidates.Children.Clear();sources.Children.Clear();sourceChoices.Clear();
            if(research!=null){RenderResearch(research);var ids=(previous["source_ids"] as JArray??new()).Select(TeamPlanningUi.Str).ToHashSet(StringComparer.Ordinal);foreach(var entry in sourceChoices)entry.Value.IsChecked=ids.Contains(entry.Key);}
        }finally{loading=false;}
        previousPlanningDraft=current;generation++;RefreshRouteEntries();await inventory.Load();RenderTeam();
        var selected=((team["team_plan"] as JObject)?["members"] as JArray??new()).OfType<JObject>().FirstOrDefault(x=>MemberId(x)==TeamPlanningUi.Str(previous["selected_member"]));
        if(selected!=null)await SelectMember(selected);else workspaces.SelectedIndex=0;
        planningError=false;status.Text="已恢复切换前的编辑；尚未自动写入保存记录。";RefreshPlanningSteps();
    }
}
