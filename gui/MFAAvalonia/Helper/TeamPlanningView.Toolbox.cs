using System;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class TeamPlanningView
{
    private UmaKnowledgeToolsClient? toolboxClient;
    private string? toolboxContextId;
    private JObject? toolboxPlanningSnapshot;
    private int toolboxGeneration;
    private Control BuildToolboxActions()
    {
        var row=new WrapPanel{Name="TeamToolboxActions"};
        var send=new Button{Name="TeamToolboxExport",Content="带当前账号与目标到工具箱",Margin=new Thickness(0,0,8,4)};
        var take=new Button{Name="TeamToolboxImport",Content="取回工具箱系谱草稿",Margin=new Thickness(0,0,8,4)};
        send.Click+=async(_,_)=>{send.IsEnabled=false;try{await SendPlanningToToolbox();}catch(Exception error){status.Text="带入未完成："+error.Message;}finally{send.IsEnabled=true;}};
        take.Click+=async(_,_)=>{take.IsEnabled=false;try{await TakePlanningFromToolbox();}catch(Exception error){status.Text="系谱草稿未采用："+error.Message;}finally{take.IsEnabled=true;}};
        row.Children.Add(send);row.Children.Add(take);ToolTip.SetTip(row,"只传本机资料副本；取回仅修改当前成员的未保存系谱，不更换游戏父母。");return row;
    }
    public async Task SendPlanningToToolbox()
    {
        if(!loaded)await LoadContext();
        StoreMemberRoute();var snapshot=Snapshot();var at=generation;
        var context=UmaKnowledgeToolsContext.Planning(planningState,AccountId,(account.SelectedItem as Choice)?.Label??"参考资料",requirementsEditor.Read(),selectedMember,selectedMember==null?null:route.ReadDraft());
        toolboxClient??=new();var id=await toolboxClient.ImportAsync(context);
        if(!Current(snapshot,at)){status.Text="账号或规划已变化；旧资料副本未绑定到当前草稿，请重新带入。";return;}
        toolboxContextId=id;toolboxPlanningSnapshot=snapshot;toolboxGeneration=at;
        await toolboxClient.OpenAsync(id);status.Text="已带入当前账号可用资料与目标；未选账号时所有条目仅作持有状态未知的参考。";
    }
    public async Task TakePlanningFromToolbox()
    {
        if(toolboxContextId==null||toolboxPlanningSnapshot==null){status.Text="请先带当前账号与目标到工具箱，并在工具箱保存供面板取回。";return;}
        if(selectedMember==null){status.Text="请先选择要承接系谱的团队成员，再带入工具箱；资料工具箱仍可独立查询。";return;}
        if(!Current(toolboxPlanningSnapshot,toolboxGeneration)){status.Text="账号、目标或成员草稿已修改；请重新带入后再取回，当前输入保留。";return;}
        var id=toolboxContextId;toolboxClient??=new();var draft=await toolboxClient.DraftAsync(id);
        if(id!=toolboxContextId||!Current(toolboxPlanningSnapshot,toolboxGeneration)){status.Text="等待期间规划已变化，未采用旧系谱。";return;}
        if(draft?["lineage"] is not JObject lineage){status.Text="工具箱还没有保存系谱草稿；请在工具箱点击保存供面板取回。";return;}
        var target=(lineage["nodes"] as JObject)?["target"] as JObject;
        if(target?["character_id"]!=null&&target["character_id"]!.ToString()!=selectedMember["character_id"]?.ToString()){status.Text="工具箱系谱目标不是当前成员，未采用；请重新带入当前成员。";return;}
        var value=UmaKnowledgeToolsContext.RouteDraft(route.ReadDraft(),lineage,planningState["entries"] as JArray??new(),AccountId,out var notice);
        route.ApplyDraft(value);StoreMemberRoute();toolboxPlanningSnapshot=null;workspaces.SelectedIndex=1;
        status.Text="已带回当前成员的未保存系谱草稿。"+notice+" 请检查后保存团队方案。";
    }
}
