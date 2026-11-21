using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Read-only daily projection; every editor still writes the original document.</summary>
public sealed partial class UmaCareerWorkflowEditor
{
    private bool planReturnHooked, planMode;
    private Action<string>? planOpenStrategy;
    private Action? planOpenEquipment;
    public event Action? PlanReturnRequested;

    public void ShowPlan(Action<string>? openStrategy=null,Action? openEquipment=null)
    {
        try { SavePolicy(); }
        catch(Exception error) { status.Text=error.Message;status.IsVisible=true;return; }
        editingFilter=null;editingPolicy=editingPolicyMode=null;editingPreferencesBaseline=null;
        back.Height=32;back.Padding=new Thickness(10,5);
        planMode=true;planOpenStrategy=openStrategy;planOpenEquipment=openEquipment;
        if(!planReturnHooked) {
            back.Click+=(_,_)=>ReturnToPlanAfterClose();planReturnHooked=true;
        }
        content.Content=null;content.Content=BuildPlan();back.IsVisible=false;
        RefreshChrome();flowTools.IsVisible=flowActions.IsVisible=status.IsVisible=false;
    }

    private void ReturnToPlanAfterClose()
    {
        if(!planMode||busy)return;
        // The original button handler runs first. Failed validation keeps the editor open.
        if(editingPolicy!=null||editingFilter!=null||!ReferenceEquals(content.Content,flowPage)) {status.IsVisible=true;return;}
        try {if(PlanReturnRequested!=null)PlanReturnRequested();else ShowPlan(planOpenStrategy,planOpenEquipment);}
        catch(Exception error) {status.Text=error.Message;status.IsVisible=true;}
    }

    /// <summary>Uses the existing graph view and keeps its saved layout selection untouched.</summary>
    public void ShowAdvancedFlow()
    {
        planMode=false;ShowFlow();
    }

    public string PlanRouteSummary()=>PlanPath((string?)document["run_graph"]?["entry"]);
    public string PlanStrategySummary(string nodeId)=>Node(nodeId) is JObject node?PlanPolicySummary(node):"当前培养步骤未指定";

    private Control BuildPlan()
    {
        var panel=new StackPanel{Name="CareerWorkflowPlan",Spacing=14};
        var route=new StackPanel{Spacing=8};
        route.Children.Add(new TextBlock{Text="当前路线",FontSize=18,FontWeight=FontWeight.SemiBold});
        route.Children.Add(new TextBlock{Name="CareerWorkflowPlanRoute",Text=PlanRouteSummary(),TextWrapping=TextWrapping.Wrap,FontSize=15});
        if(planOpenEquipment!=null)route.Children.Add(LaneButton("CareerWorkflowPlanEquipment","目标、偏好与配卡",()=>planOpenEquipment()));
        panel.Children.Add(PlanCard(route));
        var cultivation=Nodes.Where(n=>(string?)n["kind"] is "career" or "cut").ToArray();
        var strategies=new StackPanel{Spacing=10};
        strategies.Children.Add(new TextBlock{Text="培养策略",FontSize=18,FontWeight=FontWeight.SemiBold});
        foreach(var node in cultivation) {
            var id=(string)node["id"]!;
            var row=new StackPanel{Spacing=4};
            row.Children.Add(new TextBlock{Text=StepLabel(node),FontWeight=FontWeight.SemiBold});
            row.Children.Add(Note(PlanPolicySummary(node)));
            row.Children.Add(LaneButton("CareerWorkflowPlanStrategy_"+id,"调整本步骤的分段策略",()=> {
                if(planOpenStrategy!=null)planOpenStrategy(id);else SelectPolicy(id);
            }));strategies.Children.Add(row);
        }
        if(cultivation.Length==0)strategies.Children.Add(Note("此模式没有自动培养步骤；原流程完整保留，可在高级流程编辑中查看。"));
        panel.Children.Add(PlanCard(strategies));
        var checks=new StackPanel{Spacing=12};
        checks.Children.Add(new TextBlock{Text="检查点与后续处理",FontSize=18,FontWeight=FontWeight.SemiBold});
        foreach(var node in Nodes.Where(n=>(string?)n["kind"] is "cut" or "condition"))checks.Children.Add(PlanCheckpoint(node));
        foreach(var node in Nodes.Where(n=>n["ss_sieve"] is JObject))checks.Children.Add(PlanSsSieve(node));
        foreach(var node in Nodes.Where(n=>n["ace_cycle"] is JObject))checks.Children.Add(PlanAceCycle(node));
        if(checks.Children.Count==1)checks.Children.Add(Note("本模式没有筛选检查点，按上方路线完成培养。"));
        panel.Children.Add(PlanCard(checks));
        return panel;
    }

    private Control PlanAceCycle(JObject node)
    {
        var ace=(JObject)node["ace_cycle"]!;
        var body=new StackPanel{Name="CareerWorkflowPlanAceCycle_"+(string?)node["id"],Spacing=7};
        body.Children.Add(new TextBlock{Text="大赛成品连开循环",FontWeight=FontWeight.SemiBold});
        body.Children.Add(Note("目标毕业成品数："+(ace["target_graduates"]?.ToString()??"1")+" 只"));
        var grad=ace["graduation"] as JObject;
        if((bool?)grad?["enabled"]==true) {
            var parts=new List<string>();
            if(grad?["min_stat_sum"] is JValue sumVal && sumVal.Type!=JTokenType.Null)parts.Add("五维总和 ≥ "+sumVal);
            if(grad?["min_stats"] is JObject ms && ms.Count>0)parts.Add(string.Join(" / ",ms.Properties().Select(p=>p.Name+" ≥ "+p.Value)));
            if(grad?["distances"] is JObject ds && ds.Count>0)parts.Add("距离适性 "+string.Join(" / ",ds.Properties().Select(p=>p.Name+" ≥ "+p.Value)));
            body.Children.Add(Note("毕业验货门槛："+(parts.Count>0?string.Join("；",parts):"已启用")));
        } else body.Children.Add(Note("毕业验货门槛：未启用（每局完整养完即算毕业）。"));
        var limits=CutSettings(node);
        body.Children.Add(Note(BudgetText(limits,"max_attempts","本批最多","局")+"；"+
            BudgetText(limits,"max_tp_items","TP 药上限","瓶")+"；"+BudgetText(limits,"max_carrots","萝卜上限","")));
        body.Children.Add(Note("年中未达续养门槛走放弃重开；终局验货未读清适性/属性时保底不弃。"));
        if(planMode&&planOpenEquipment!=null)body.Children.Add(LaneButton("CareerWorkflowPlanEditAceCycle","打开常用设置",()=>planOpenEquipment()));
        return body;
    }

    private Control PlanSsSieve(JObject node)
    {
        var sieve=(JObject)node["ss_sieve"]!;
        var body=new StackPanel{Name="CareerWorkflowPlanSsSieve_"+(string?)node["id"],Spacing=7};
        body.Children.Add(new TextBlock{Text="SS筛局循环",FontWeight=FontWeight.SemiBold});
        body.Children.Add(Note((bool?)sieve["pre_debut"]==true?"出道前：至少完成一次五人SS。":"出道前：跳过检查。"));
        body.Children.Add(Note("初级年末：至少完成 "+sieve["year1_min"]+" 次五人SS。"+
            ((bool?)sieve["allow_ready_five"]==true?"恰少一次且已满员也通过。":"")));
        var limits=CutSettings(node);
        body.Children.Add(Note(BudgetText(limits,"max_attempts","本批最多","局")+"；"+
            BudgetText(limits,"max_tp_items","TP 药上限","瓶")+"；"+BudgetText(limits,"max_carrots","萝卜上限","")));
        body.Children.Add(Note("判定不明继续原局。未达标在原限额内弃养重开，首个正常结算、达到限额或实际失败后结束。"));
        if(planMode&&planOpenEquipment!=null)body.Children.Add(LaneButton("CareerWorkflowPlanEditSsSieve","打开常用设置",()=>planOpenEquipment()));
        return body;
    }

    private Border PlanCard(Control child)
    {
        var card=new Border{Padding=new Thickness(16),CornerRadius=new CornerRadius(9),BorderThickness=new Thickness(1),Child=child};
        card.Background=UmaWeightTheme.Brush(card,"card");card.BorderBrush=UmaWeightTheme.Brush(card,"border");return card;
    }

    private string PlanPolicySummary(JObject node)
    {
        var policy=document["policies"]?[(string?)node["policy"]??"default"] as JObject;
        var cut=(string?)node["kind"]=="cut";var options=policy?[cut?"cut_options":"career_options"] as JObject;
        var preferences=(cut?options?["cut"]?["decision_preferences"]:options?["decision_preferences"]) as JObject;
        var workflow=preferences?["workflow"] as JObject;var stages=workflow?["stages"] as JArray;
        var enabled=workflow?["enabled"]?.Type==JTokenType.Boolean&&(bool)workflow["enabled"]!;
        var revision=(options?["larc_policy"] as JObject)?["revision"];
        var version=revision?.Type==JTokenType.String?"策略 "+revision:"沿用保存的策略版本";
        var shared=Nodes.Where(other=>(string?)other["id"]!=(string?)node["id"]
            &&(string?)other["kind"]==(string?)node["kind"]
            &&((string?)other["policy"]??"default")==((string?)node["policy"]??"default")).ToArray();
        return version+" · "+(enabled?"分段安排已启用":"分段安排未启用")
            +(stages?.Count>0?"（"+stages.Count+"段）":"")+"；修改仅作用于当前命名配置。"
            +(shared.Length==0?"":" 与"+string.Join("、",shared.Select(other=>"「"+StepLabel(other)+"」"))+"共用策略，修改会同时影响这些培养步骤。需要独立时：高级流程编辑 → 步骤属性 → 复制为本块独立策略。");
    }

    private Control PlanCheckpoint(JObject node)
    {
        var id=(string)node["id"]!;var body=new StackPanel{Name="CareerWorkflowPlanCheck_"+id,Spacing=7};
        body.Children.Add(new TextBlock{Text=StepLabel(node),FontSize=15,FontWeight=FontWeight.SemiBold});
        if((string?)node["kind"]=="cut") {
            var limits=CutSettings(node);body.Children.Add(Note(BudgetText(limits,"max_attempts","本批最多","局")+"；"+BudgetText(limits,"max_tp_items","TP 药上限","瓶")+"；"+BudgetText(limits,"max_carrots","萝卜上限","")));
        }
        if((string?)node["kind"]=="cut"&&CustomScreening(node)) {
            var raw=node["screening"]?["checkpoints"];
            if(raw is JArray points) {
                for(var index=0;index<points.Count;index++) {
                    if(points[index] is not JObject point) {body.Children.Add(Note($"{index+1}. 高级检查点格式完整保留"));continue;}
                    var detail=new StackPanel{Spacing=4,Margin=new Thickness(8,4,0,4)};
                    detail.Children.Add(new TextBlock{Name=$"CareerWorkflowPlanCheckTime_{id}_{index}",Text=$"{index+1}. "+CheckpointLabel(point),FontWeight=FontWeight.SemiBold});
                    detail.Children.Add(new TextBlock{Name=$"CareerWorkflowPlanCheckRule_{id}_{index}",Text=PlanCondition(point["when"]),TextWrapping=TextWrapping.Wrap});
                    detail.Children.Add(Note("满足："+PlanCheckpointAction(node,point,"on_pass")));
                    detail.Children.Add(Note("不满足："+PlanCheckpointAction(node,point,"on_fail")));
                    detail.Children.Add(Note("未知：暂停并保留当前局"));body.Children.Add(detail);
                }
                if(points.Count==0)body.Children.Add(Note("自定义筛选已启用，但尚无检查点；没有代填时间或条件。"));
            } else body.Children.Add(Note("高级检查点格式完整保留，请打开编辑查看。"));
        } else {
            body.Children.Add(Note((string?)node["kind"]=="cut"?CutTimingDescription(node):ScreeningTimeLabel((string?)node["check_at"])));
            body.Children.Add(new TextBlock{Name="CareerWorkflowPlanCheckRule_"+id,Text=(string?)node["kind"]=="cut"?"沿用当前配置的切状态筛选":PlanCondition(node["when"]),TextWrapping=TextWrapping.Wrap});
            foreach(var outcome in new[]{"matched","not_matched","unknown"})
                body.Children.Add(new TextBlock{Name="CareerWorkflowPlanBranch_"+id+"_"+outcome,Text=ScreeningOutcomeLabel(node,outcome)+"："+PlanBranch(node,outcome),TextWrapping=TextWrapping.Wrap});
        }
        body.Children.Add(LaneButton("CareerWorkflowPlanEditCheck_"+id,"编辑检查时间与条件",()=>SelectFilter(id)));
        return body;
    }

    private string PlanCheckpointAction(JObject node,JObject point,string field)
    {
        var raw=point[field];if(raw!=null&&raw.Type!=JTokenType.String)return "高级处理原样保留";
        var action=(string?)raw;
        return action switch {
            "matched_branch"=>PlanBranch(node,"matched"),
            "not_matched_branch"=>PlanBranch(node,"not_matched")+"（允许重开已有切的局）",
            "hold"=>"暂停并保留当前局",
            "next_checkpoint"=>"继续下一检查点；末点暂停保留",
            null or ""=>field=="on_pass"?"继续下一检查点；末点 → "+PlanBranch(node,"matched")
                :PlanBranch(node,"not_matched")+"；已有切按原保护保留",
            _=>"高级处理："+action
        };
    }

    private string PlanBranch(JObject node,string outcome)
    {
        var edges=Edges.Where(e=>(string?)e["from"]==(string?)node["id"]&&(string?)e["outcome"]==outcome).ToArray();
        return edges.Length==1?PlanPath((string?)edges[0]["to"]):edges.Length==0?"未连接（原样保留）":"多条连接，详见高级流程";
    }

    private string PlanPath(string? cursor)
    {
        var labels=new List<string>();var seen=new HashSet<string>();
        for(var i=0;i<16&&cursor!=null;i++) {
            if(!seen.Add(cursor)){labels.Add("返回「"+TargetLabel(cursor)+"」");break;}
            if(Node(cursor) is not JObject node){labels.Add("未连接");break;}
            var kind=(string?)node["kind"];if(kind!="entry")labels.Add(StepLabel(node));
            var outgoing=Edges.Where(e=>(string?)e["from"]==cursor).ToArray();
            if(kind is "cut" or "condition"){labels.Add("按检查结果分别处理");break;}
            if(outgoing.Length!=1){if(outgoing.Length>1)labels.Add("分支详见高级流程");break;}
            cursor=(string?)outgoing[0]["to"];
        }
        return labels.Count==0?"当前没有可显示的执行路线，原文仍保留。":string.Join(" → ",labels);
    }

    private string PlanCondition(JToken? raw)
    {
        if(raw is not JObject when||when.Properties().Count()!=1||when.Properties().First().Value is not JArray rows)
            return "高级组合条件原样保留";
        var mode=when.Properties().First().Name;if(mode is not ("all" or "any"))return "高级组合条件原样保留";
        return (mode=="all"?"全部满足：":"任一满足：")+string.Join("；",rows.Select(PlanClause));
    }

    private string PlanClause(JToken raw)
    {
        if(raw is not JObject row)return "高级条件";
        if(row["all"]!=null||row["any"]!=null)return "（"+PlanCondition(row)+"）";
        var kind=(string?)row["kind"]??"高级条件";
        var label=ConditionLabel(kind,(string?)(schema?["condition_kinds"] as JArray)?.OfType<JObject>().FirstOrDefault(k=>(string?)k["kind"]==kind)?["label"]);
        string Value(JToken? token)=>token?.Type==JTokenType.String?(string?)token switch {
            "speed"=>"速度","stamina"=>"耐力","power"=>"力量","guts"=>"毅力","wisdom"=>"智力",
            "short"=>"短距离","mile"=>"英里","medium"=>"中距离","long"=>"长距离","small"=>"小切","big"=>"大切",_=>token.ToString()
        }:token?.ToString(Formatting.None)??"";
        var op=(string?)row["op"] switch {"lt"=>" < ","lte"=>" ≤ ","eq"=>" = ","gt"=>" > ",_=>" ≥ "};
        if(kind=="stat_min")return Value(row["stat"])+op+Value(row["minimum"]??row["value"]);
        if(kind=="distance_min")return Value(row["distance"])+"适性 "+Value(row["rank"]);
        var extras=row.Properties().Where(p=>p.Name is not ("kind" or "op" or "minimum" or "value"))
            .Select(p=>ParameterLabel(p.Name)+"："+Value(p.Value));
        return label+(extras.Any()?"（"+string.Join("，",extras)+"）":"")
            +(row["minimum"]!=null||row["value"]!=null?op+Value(row["minimum"]??row["value"]):"");
    }
}
