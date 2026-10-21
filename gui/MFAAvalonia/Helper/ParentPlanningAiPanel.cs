using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Explicit AI request and reviewed draft suggestions. No persistence or game execution.</summary>
public sealed class ParentPlanningAiPanel : StackPanel
{
    private readonly Func<JObject,Task<JObject>> request;
    private readonly Func<JObject> currentContext;
    private readonly Action<JObject> applyDraft;
    private readonly Button propose=new(){Name="ParentPlanningAiPropose",Content="AI 帮我规划"};
    private readonly Button refresh=new(){Name="ParentPlanningAiRefresh",Content="刷新接口状态"};
    private readonly TextBox instruction=new(){Name="ParentPlanningAiRequest",Watermark="补充偏好，例如：优先利用已有材料，减少待培养的代数",MaxLength=2000,AcceptsReturn=true,MinHeight=64,TextWrapping=TextWrapping.Wrap};
    private readonly TextBlock state=Note("尚未检查 AI 接口状态。"),privacy=Note("点击后使用已配置的 AI 接口，仅发送本地整理的账号库存摘要与规划目标。建议经过本地复核，应用时只改当前草稿。" );
    private readonly StackPanel candidates=new(){Name="ParentPlanningAiCandidates",Spacing=8};
    private bool configured,busy;private int generation;
    public ParentPlanningAiPanel(Func<JObject,Task<JObject>> request,Func<JObject> currentContext,Action<JObject> applyDraft)
    {
        this.request=request;this.currentContext=currentContext;this.applyDraft=applyDraft;Name="ParentPlanningAiPanel";Spacing=8;
        var buttons=new WrapPanel();propose.Margin=new Thickness(0,0,8,4);refresh.Margin=new Thickness(0,0,8,4);buttons.Children.Add(propose);buttons.Children.Add(refresh);Children.Add(buttons);Children.Add(state);Children.Add(new Expander{Header="AI 规划偏好与发送范围",Content=new StackPanel{Spacing=7,Children={instruction,privacy}},IsExpanded=false});Children.Add(candidates);
        propose.Click+=async(_,_)=>await Propose();refresh.Click+=async(_,_)=>await RefreshStatus();instruction.TextChanged+=(_,_)=>ContextChanged();
    }
    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private static string Str(JToken? value)=>value?.Type is JTokenType.String or JTokenType.Integer?value.ToString():"";
    private static int? Int(JToken? value)=>value?.Type==JTokenType.Integer?(int?)value:null;
    private JObject Snapshot(){var value=(JObject)currentContext().DeepClone();value["request_text"]=(instruction.Text??"").Trim();return value;}
    public void ContextChanged(){generation++;candidates.Children.Clear();if(!busy&&configured)state.Text="目标、账号或草稿已变化；可重新请求 AI 建议。";}
    public async Task RefreshStatus()
    {
        try{var response=await request(new JObject{["action"]="parent_planning_ai_status"});configured=(bool?)response["configured"]==true;var model=Str(response["model"]);state.Text=configured?"AI 接口已设置"+(model.Length>0?" · "+model:"")+"；尚未据此确认本次请求连通。":"尚未配置 AI 接口。请在“育成中心 → AI 决策 → AI 接口设置”中保存后刷新；仍可继续本地手动规划。";var scope=Str(response["data_scope"]);if(scope.Length>0)privacy.Text=scope;}
        catch(InvalidOperationException e){configured=false;state.Text=e.Message;}catch{configured=false;state.Text="AI 接口状态暂不可读；本地手动规划仍可使用。";}
    }
    public async Task Propose()
    {
        if(busy)return;var snapshot=Snapshot();var at=generation;if(snapshot["target_character_id"]?.Type!=JTokenType.Integer){state.Text="请先选择育成目标，再请求 AI 规划。";return;}busy=true;propose.IsEnabled=refresh.IsEnabled=false;
        try{await RefreshStatus();if(!configured)return;if(at!=generation||!JToken.DeepEquals(snapshot,Snapshot())){state.Text="规划上下文已变化，已取消本次请求；请重新点击生成。";return;}state.Text="正在请求 AI 建议并做本地复核…";var command=(JObject)snapshot.DeepClone();command["action"]="parent_planning_ai_propose";var response=await request(command);if(at!=generation||!JToken.DeepEquals(snapshot,Snapshot())){state.Text="规划上下文已变化，旧建议未套用；请重新请求。";return;}Render(response,snapshot,at);var provider=response["provider"] as JObject;state.Text="AI 建议已返回，下面同时列出本地复核结果。"+(Str(provider?["model"]) is string model&&model.Length>0?" · "+model:"");}
        catch(InvalidOperationException e){state.Text=e.Message;}catch{state.Text="AI 建议未完成，当前草稿保留；可继续手动规划。";}finally{busy=false;propose.IsEnabled=refresh.IsEnabled=true;}
    }
    private void Render(JObject response,JObject snapshot,int at)
    {
        candidates.Children.Clear();foreach(var missing in (response["missing_data"] as JArray??new()).Select(Str).Where(x=>x.Length>0))candidates.Children.Add(Note("资料待补："+missing));
        var rows=(response["candidates"] as JArray??new()).OfType<JObject>().ToArray();if(rows.Length==0)candidates.Children.Add(Note("本次没有可应用的建议；可补充资料或调整偏好。"));
        foreach(var row in rows){var review=row["local_review"] as JObject;var route=row["local_route"] as JObject;var id=Str(row["candidate_id"]);var body=new StackPanel{Spacing=6};body.Children.Add(new TextBlock{Text=Str(row["title"]),FontSize=15,FontWeight=FontWeight.SemiBold,TextWrapping=TextWrapping.Wrap});body.Children.Add(Note(Str(row["summary"])));
            if(review!=null){var plan=(review["aptitude"] as JObject)?["plan"] as JObject;var known=Int(plan?["known_stars"]);var gap=Int(plan?["additional_stars"]);var unknown=Int(plan?["unknown_nodes"]);var verdict=Str(plan?["status"]) switch{"satisfied"=>"当前适性目标已满足","unknown_baseline"=>"基准适性未知，需选准确衣装或手动提供","unreachable_at_start"=>"开局不可达，不能按普通补星目标处理","missing"=>gap is int missing?$"仍缺 {missing} 星":"缺星资料未齐",_=>"尚有未确认资料，最终缺星待确认"};body.Children.Add(Note("本地复核："+verdict+" · 已确认红星 "+(known?.ToString()??"未知")+(unknown>0?$" · 未确认节点 {unknown}":"")));
                foreach(var issue in (review["selection_issues"] as JArray??new()).Select(Str).Where(x=>x.Length>0))body.Children.Add(Note("需要调整："+issue));
                foreach(var node in (review["route_nodes"] as JArray??new()).OfType<JObject>().Where(x=>(bool?)x["usable"]!=true))body.Children.Add(Note((Str(node["label"]) is string label&&label.Length>0?label:"家系节点")+"："+Str(node["status_label"])));
            }else body.Children.Add(Note("缺少本地复核结果，不能应用这条建议。"));
            foreach(var warning in (row["warnings"] as JArray??new()).Select(Str).Where(x=>x.Length>0))body.Children.Add(Note("提示："+warning));var steps=(row["training_steps"] as JArray??new()).Select(Str).Where(x=>x.Length>0).ToArray();if(steps.Length>0)body.Children.Add(new Expander{Header="建议的培养顺序",Content=Note(string.Join("\n",steps.Select((step,index)=>(index+1)+". "+step))),IsExpanded=false});
            var apply=new Button{Name="ParentPlanningAiApply",Content="应用为当前草稿",IsEnabled=review!=null&&route!=null&&id.Length>0};apply.Click+=(_,_)=>{if(at!=generation||!JToken.DeepEquals(snapshot,Snapshot())){state.Text="目标、账号或库上下文已变化，请重新请求建议。";return;}if(route==null||review==null)return;applyDraft((JObject)route.DeepClone());state.Text="建议已载入当前草稿，尚未保存。可继续编辑并重新核对。";};body.Children.Add(apply);candidates.Children.Add(new Border{Child=body,Padding=new Thickness(12),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(10)});
        }
    }
}
