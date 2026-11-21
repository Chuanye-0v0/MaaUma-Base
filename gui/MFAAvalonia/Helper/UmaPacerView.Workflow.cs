using System;
using System.Linq;
using System.Threading.Tasks;
using Avalonia.Controls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    private UmaWorkflowLibraryView? workflowLibrary;
    private Control? workflowCustomRacesPage;
    private Control? workflowDeckPage;
    private Control? workflowStrategyTargetsPage;
    private Control? workflowCyclePage;
    private PacerUiProvenanceStore? provenanceStore;
    private string? workflowRoot;
    internal UmaWorkflowLibraryView? WorkflowLibraryView => workflowLibrary;
    public event Action? OverviewSettingsRequested;
    public event Action? WorkflowConfigurationRequested;
    public event Action? RaceScheduleSettingsRequested;
    public Control? WorkflowConfigurationPage => workflowLibrary?.ConfigurationPage;
    public Control? WorkflowCustomRacesPage => workflowCustomRacesPage ??= workflowLibrary?.CreateConfigurationPage("custom_races");
    public Control? WorkflowDeckPage => workflowDeckPage ??= workflowLibrary?.CreateConfigurationPage("deck");
    public Control? WorkflowStrategyTargetsPage => workflowStrategyTargetsPage ??= workflowLibrary?.CreateConfigurationPage("strategy_targets");
    public Control? WorkflowCyclePage => workflowCyclePage ??= workflowLibrary?.CreateConfigurationPage("cycle");
    private readonly Button resumeWorkflowSnapshot = new() {Name="PacerResumeWorkflowSnapshot",Content="继续原工作流",IsVisible=false};

    private Control BuildWorkflowPage()
    {
        if(workflowLibrary != null)return workflowLibrary;
        workflowLibrary = new UmaWorkflowLibraryView(RequestWorkflow, AcceptWorkflowSnapshot, CaptureWorkflowPolicies);
        workflowLibrary.SkillLearningRequested+=()=>MidSkillSettingsRequested?.Invoke();
        workflowLibrary.RaceScheduleRequested+=()=>RaceScheduleSettingsRequested?.Invoke();
        workflowLibrary.OrdinarySettingsRequested+=()=>OverviewSettingsRequested?.Invoke();
        workflowLibrary.ConfigurationRequested+=()=>WorkflowConfigurationRequested?.Invoke();
        workflowLibrary.SelectionChanged+=SyncIdleModeFromWorkflowLibrary;
        if(workflowLibrary.LibraryReady)SyncIdleModeFromWorkflowLibrary();
        return workflowLibrary;
    }
    private bool ApplyIdleModeFromWorkflowLibrary()
    {
        if(!pacerCenter || fixedMode == "cut" || workflowLibrary is not { LibraryReady: true })return false;
        var state = (string?)snapshot?["control"]?["state"] ?? "idle";
        if(Active.Contains(state))return false;
        var targetIndex = workflowLibrary.SelectedPrimaryMode == "cut" ? 1 : 0;
        var previous = applying;
        applying = true;
        try { mode.SelectedIndex = targetIndex; displayedModeIndex = targetIndex; }
        finally { applying = previous; }
        ApplyModePresentation();
        return true;
    }
    private void SyncIdleModeFromWorkflowLibrary()
    {
        ApplyIdleModeFromWorkflowLibrary();
        if(snapshot != null)Render(context());
    }
    public void RefreshOverviewSettings()=>workflowLibrary?.RefreshOverviewSettings();
    internal void SetCustomWeightsPage(bool visible)=>workflowLibrary?.SetCustomWeightsPage(visible);
    internal void BindEventPreferencesEditor(UmaCutPanel.EventPreferenceEditor editor)
    {
        boundEventEditor = editor;
        provenanceStore ??= new PacerUiProvenanceStore(editor.PacerRoot);
        if(workflowLibrary==null)return;
        workflowLibrary.AttachProvenance(provenanceStore);
        editor.BindWorkflowProfile(
            ()=>workflowLibrary.ReadPrimaryEventPreferences(),
            prefs=>workflowLibrary.ApplyPrimaryEventPreferences(prefs),
            ()=>workflowLibrary.SelectedConfigurationLabel,
            ()=>workflowLibrary.SaveAsync());
        editor.PreferencesChanged+=RefreshOverviewPreflightCard;
        void SyncMeiContext()
        {
            if(workflowLibrary==null)return;
            var supported=workflowLibrary.PrimarySupportsMeiGamble;
            editor.SetMeiGambleContext(
                supported,
                supported?null:"当前育成模式暂不生效（仅未开启组合筛选的纯刷切模式生效，现将稳妥选1）。");
        }
        workflowLibrary.SelectionChanged+=()=>{editor.ReloadFromBoundProfile();SyncMeiContext();};
        workflowLibrary.SavedEventPreferences+=prefs=>{
            if(prefs is {Count:>0})editor.PersistPortableFallback(prefs);
        };
        if(workflowLibrary.LibraryReady){editor.ReloadFromBoundProfile();SyncMeiContext();}
        RefreshOverviewPreflightCard();
    }
    internal void BindExchangeView(UmaCutPanel.ScoringExchangeView exchange)
    {
        if(workflowLibrary==null)return;
        exchange.BindWorkflowProvenance(
            ()=>{
                var (stepId,curRev)=workflowLibrary.PrimaryStepContext;
                var entry=stepId!=null
                    ? provenanceStore?.Read(workflowLibrary.SelectedModeId,workflowLibrary.SelectedProfileId,stepId)
                    : null;
                return (entry,curRev);
            },
            ()=>workflowLibrary.PrimaryStepContext.Revision,
            scheme=>{
                var (stepId,_)=workflowLibrary.PrimaryStepContext;
                var (baseLabel,baseFamily,_)=PacerUiProvenanceStore.InferBaseline(scheme);
                var modeId=workflowLibrary.SelectedModeId;
                var profileId=workflowLibrary.SelectedProfileId;
                if(provenanceStore!=null&&!string.IsNullOrEmpty(modeId)&&!string.IsNullOrEmpty(profileId)&&!string.IsNullOrEmpty(stepId))
                {
                    provenanceStore.Record(modeId,profileId,stepId,new ProvenanceEntry(
                        Source:"exchange_scheme",
                        SchemeId:(string?)scheme["id"],
                        Title:(string?)scheme["title"],
                        SchemeRevision:(int?)scheme["revision"],
                        BaselineRevision:baseFamily??baseLabel,
                        AppVersion:(string?)scheme["app_version"],
                        AppliedAt:DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm"),
                        CarriedAcrossSwitch:false));
                }
                workflowLibrary.RefreshPurityStatus();
                exchange.RefreshSourceBar();
            },
            ()=>{
                workflowLibrary.PurifyCurrentDraft(PurityLayer.DecisionPreferences,recordUndo:true);
                exchange.RefreshSourceBar();
            });
        workflowLibrary.SelectionChanged+=exchange.RefreshSourceBar;
    }
    private async Task<bool> PreviewLibraryWeights(Window owner,JObject profile,string title)
    {
        var library=workflowLibrary!;var selection=library.SelectionKey;
        if(!CanApplySharedWeights)throw new InvalidOperationException("请先结束本轮，再把方案填入配置。");
        if((profile["preferences"] as JObject)?["workflow"] is JObject||(string?)profile["schema_version"]=="uma.scoring_profile.v3")
            throw new InvalidOperationException("此方案包含画布；请在当前配置的分段策略中读取完整文件。");
        var schema=await bridge.Request(new JObject{["action"]="decision_preferences_schema"});
        var checkedValue=await bridge.Request(new JObject{["action"]="validate_decision_preferences",["profile"]=profile.DeepClone()});
        var draft=checkedValue["preferences"] as JObject??throw new InvalidOperationException("方案兼容性校验未完成。");
        if(selection!=library.SelectionKey)throw new InvalidOperationException("所选配置已变化，方案未填入。");
        var current=library.PrimaryDecisionPreferences;
        var modal=new UmaScoreEditor(schema["tuning_schema"] as JObject??throw new InvalidOperationException("评分信息尚未加载。"),draft,
            value=>bridge.Request(new JObject{["action"]="validate_decision_preferences",["profile"]=value}),
            (string?)draft["tuning"]?["mode"]=="formula"?"formula":"detailed",preserveMode:true){Title=$"预览：{title} → {library.SelectedConfigurationLabel}"};
        var result=await modal.ShowDialog<JObject?>(owner);if(result==null)return false;
        if(!CanApplySharedWeights||selection!=library.SelectionKey)throw new InvalidOperationException("所选配置或运行状态已变化，方案未填入。");
        if(current["workflow"] is JObject workflow){workflow["enabled"]=false;if(workflow["editor_graph"] is JObject graph)graph["enabled"]=false;result["workflow"]=workflow.DeepClone();}
        library.ApplyPrimaryDecisionPreferences(result);hint.Text="方案已填入当前命名配置的前期策略；保存后新一轮生效。";return true;
    }
    private async Task ResumeWorkflowSnapshot()
    {
        if(workflowRoot==null||NewCareerSelected(context())||IsSieveRun&&(ManualHandoffWaiting||ManualHandoffTerminal))return;
        try {
            var result=await RequestWorkflow(new JObject{["action"]="start_workflow",["workflow_root"]=workflowRoot});
            AcceptWorkflowSnapshot(result);hint.Text="已继续原工作流，沿用原局配置、次数和资源额度。";
        }catch(Exception error){commandError=error.Message;hint.Text=error.Message;}
        finally{await Refresh();}
    }
    private async Task<JObject> CaptureWorkflowPolicies()
    {
        var current = Options(); var otherMode = IsCut ? "career" : "cut";
        JObject? other = modeSwitchDrafts.TryGetValue((otherMode,presetId), out var draft) ? (JObject)draft.Settings.DeepClone() : null;
        if(other == null) {
            var response = await bridge.Request(new JObject { ["action"] = "configuration_snapshot", ["mode"] = otherMode, ["endpoint"] = context().Endpoint });
            var presets = response[otherMode + "_presets"] as JObject;
            var selectedId = (int?)presets?["selected_id"] ?? presetId;
            other = (presets?["slots"] as JArray)?.OfType<JObject>().FirstOrDefault(row => (int?)row["id"] == selectedId)?["settings"] as JObject;
            if(other == null)throw new InvalidOperationException("另一模式的运行配置尚未读取，工作流未覆盖原设置。请先在运行总览读取配置后重试。");
        }
        return new JObject { ["default"] = new JObject { ["career_options"] = IsCut ? other.DeepClone() : current, ["cut_options"] = IsCut ? current : other.DeepClone() } };
    }
    private async Task<JObject> RequestWorkflow(JObject payload)
    {
        var action = (string?)payload["action"];
        var request = (JObject)payload.DeepClone();
        request["endpoint"] = context().Endpoint;
        if(action == "start_workflow") {
            if(NewCareerSelected(context())) {
                if(request["workflow_root"] is JValue)throw new InvalidOperationException("已选择开始新一轮；如需继续旧工作流，请先取消该选择。");
                await EndPreviousHandoffTaskAsync();
            }
            var state=(string?)snapshot?["control"]?["state"]??"idle";
            if(submitting || presetBusy || pushBusy || Active.Contains(state)&&!(request["workflow_root"] is JValue&&state=="paused"))
                throw new InvalidOperationException("请先结束当前运行，再开始工作流。继续原局仍使用已有记录。");
            request["request_id"] = Guid.NewGuid().ToString("N");
            // A saved document starts a new workflow. Resuming an original
            // workflow uses its recorded root through the existing run controls.
            submitting = true;
        }
        try { return await bridge.Request(request); }
        finally { if(action == "start_workflow")submitting = false; }
    }
    private void AcceptWorkflowSnapshot(JObject value)
    {
        snapshot = value; runRoot = (string?)value["run_root"]; currentRecord = value["current_record"] as JObject;
        var workflow = value["workflow"] as JObject;
        workflowRoot = workflow?["state"] is JObject ? (string?)workflow["root"] : null;
        commandError = null;
        ApplyReturnedMode(value); Render(context());
    }
    private void ApplyReturnedMode(JObject value)
    {
        var actual = (string?)value["mode"];
        if(actual is not ("career" or "cut"))return;
        applying = true;
        try { mode.SelectedIndex = actual == "cut" ? 1 : 0; displayedModeIndex = mode.SelectedIndex; }
        finally { applying = false; }
        ApplyModePresentation();
    }
}
