using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Layout;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    private readonly UmaCareerPauseDates presetDates=new();
    private readonly UmaCareerPauseDates planDates=new();
    private readonly CheckBox planFirstArc=new(){Content="首次凯旋门前手动学技能"};
    private readonly CheckBox planFinalArc=new(){Content="末次凯旋门前交还操作"};
    private readonly Button applyPlan=new(){Name="PacerApplyCurrentPausePlan",Content="应用本局暂停计划"};
    private readonly Button reloadPlan=new(){Name="PacerReloadCurrentPausePlan",Content="重新读取本局计划"};
    private readonly TextBlock planStatus=new(){Name="PacerCurrentPausePlanStatus",TextWrapping=Avalonia.Media.TextWrapping.Wrap};
    private readonly TextBlock planRules=new(){TextWrapping=Avalonia.Media.TextWrapping.Wrap};
    private readonly Expander currentPlan=new(){Name="PacerCurrentPausePlan",Header="本局暂停计划",HorizontalAlignment=HorizontalAlignment.Stretch,IsVisible=false};
    private string? planRun,planEndpoint;
    private long? planRevision;
    private bool planDirty,planBusy,loadingPlan,planStale;

    private Control BuildCurrentPausePlan()
    {
        var actions=new WrapPanel {Children={applyPlan,reloadPlan}};
        currentPlan.Content=new StackPanel {Spacing=9,Children={
            new TextBlock {Text="只修改本局的暂停安排；不更换角色、策略或卡组。删除停点不会继续已暂停的局。",TextWrapping=Avalonia.Media.TextWrapping.Wrap},
            planFirstArc,planFinalArc,planDates,actions,planStatus,planRules}};
        void Dirty(){if(!loadingPlan){planDirty=true;planStatus.Text="本局计划有未应用修改。";if(snapshot!=null)RenderManualPlan(context());}}
        planFirstArc.IsCheckedChanged+=(_,_)=>Dirty();planFinalArc.IsCheckedChanged+=(_,_)=>Dirty();planDates.Changed+=Dirty;
        presetDates.Changed+=()=>{if(!applying)savePreset.Content="保存配置";};
        applyPlan.Click+=async(_,_)=>await ApplyCurrentPausePlan();
        reloadPlan.Click+=async(_,_)=>{if(planBusy)return;await Refresh();if(snapshot!=null)LoadCurrentPausePlan(true);};
        return currentPlan;
    }
    private static string DisplayDate(JToken? date)
    {
        if(date is not JObject)return "未核验";
        var year=(int?)date?["year_index"];var month=(int?)date?["month"];var half=(string?)date?["half"];
        if(year is not (1 or 2 or 3)||month is <1 or >12||month==null||half is not ("first" or "second"))return "未核验";
        return new[]{"初级","经典","高级"}[year.Value-1]+$" {month}月"+(half=="first"?"上半月":"下半月");
    }
    private void LoadCurrentPausePlan(bool force=false)
    {
        var manual=snapshot?["manual_handoff"] as JObject;var plan=manual?["plan"] as JObject;
        if(plan==null)return;
        var run=(string?)snapshot?["run_root"];var endpoint=(string?)snapshot?["endpoint"];
        var changed=run!=planRun||endpoint!=planEndpoint;
        if(!force&&!changed&&planDirty)return;
        var config=plan["config"] as JObject??manual?["config"] as JObject??new JObject();
        loadingPlan=true;
        try {planFirstArc.IsChecked=(bool?)config["first_arc"]==true;planFinalArc.IsChecked=(bool?)config["final_arc"]==true;
            planDates.Load(config["dates"] as JArray);planRun=run;planEndpoint=endpoint;planRevision=(long?)plan["revision"];
            planDirty=false;planStale=false;}
        finally {loadingPlan=false;}
        RenderManualPlan(context());
    }
    private void RenderManualPlan(UmaPacerContext current)
    {
        var plan=(snapshot?["manual_handoff"] as JObject)?["plan"] as JObject;
        currentPlan.IsVisible=pacerCenter&&!IsCut&&plan!=null&&!string.IsNullOrWhiteSpace((string?)snapshot?["run_root"]);
        if(!currentPlan.IsVisible)return;
        if(planRun!=(string?)snapshot?["run_root"]||planEndpoint!=(string?)snapshot?["endpoint"]||!planDirty&&(long?)plan?["revision"]!=planRevision) {
            LoadCurrentPausePlan();return;
        }
        planStale=planDirty&&(long?)plan?["revision"]!=planRevision;
        applyPlan.IsEnabled=!planBusy&&!planStale&&planRevision!=null&&planEndpoint==current.Endpoint;
        reloadPlan.IsEnabled=!planBusy;
        planDates.IsEnabled=planFirstArc.IsEnabled=planFinalArc.IsEnabled=!planBusy;
        if(planStale)planStatus.Text="后台计划已更新；当前修改尚未应用，请重新读取后编辑。";
        else if(!planDirty&&!planBusy)planStatus.Text=$"本局计划版本 {planRevision}；在后续安全检查点生效。";
        var date=DisplayDate(plan?["observed_date"]);
        var dateText=(string?)plan?["date_status"]=="verified"?"已核验日期："+date:"当前日期未核验；上次已核验日期："+date;
        var states=new System.Collections.Generic.Dictionary<string,string>{{"disabled","未启用"},{"pending","待到达"},
            {"waiting","等待人工继续"},{"acknowledged","已确认"},{"missed","已错过，不补停"},{"date_unknown","日期未核验"}};
        var rules=(plan?["rules"] as JArray??new()).OfType<JObject>().Select((rule,index)=>
            (DisplayDate(rule) is var label&&label!="未核验"?label:$"停点 {index+1}")+"："+states.GetValueOrDefault((string?)rule["status"]??"","状态待确认"));
        planRules.Text=dateText+"\n"+string.Join("\n",rules);
    }
    private async Task ApplyCurrentPausePlan()
    {
        if(planBusy||planStale||IsCut||planRevision==null||snapshot==null||planRun!=(string?)snapshot["run_root"]
            ||planEndpoint!=(string?)snapshot["endpoint"]||planEndpoint!=context().Endpoint)return;
        var run=planRun;var endpoint=planEndpoint;var revision=planRevision;var id=Guid.NewGuid().ToString("N");
        JObject value;
        try {value=new JObject {["first_arc"]=planFirstArc.IsChecked==true,["final_arc"]=planFinalArc.IsChecked==true,["dates"]=planDates.Read()};}
        catch(Exception error){planStatus.Text=error.Message;return;}
        planBusy=true;RenderManualPlan(context());
        try {
            var result=await bridge.Request(new JObject {["action"]="update_manual_handoff_plan",["request_id"]=id,
                ["mode"]="career",["endpoint"]=endpoint,["run_root"]=run,["expected_revision"]=revision,["plan"]=value});
            if(planRun!=run||planEndpoint!=endpoint||(string?)snapshot?["run_root"]!=run)return;
            var receipt=result["plan_receipt"] as JObject;
            if((bool?)receipt?["accepted"]!=true||(string?)receipt?["request_id"]!=id||(bool?)receipt?["pause_unchanged"]!=true)
                throw new IOException("本局计划保存尚未确认；不会自动重试，当前暂停状态未由界面更改。");
            snapshot=result;planDirty=false;LoadCurrentPausePlan(true);
            planStatus.Text="本局暂停计划已应用；在后续安全检查点生效。当前人工暂停不会自动解除。";
            hint.Text=planStatus.Text;
        }catch(Exception error){planStatus.Text="计划未应用："+error.Message;}
        finally {planBusy=false;RenderManualPlan(context());}
    }
}
