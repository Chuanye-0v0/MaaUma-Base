using Avalonia.LogicalTree;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;
using UmaCutPanel;

namespace MFAAvalonia.Helper;

/// <summary>Native independent page. Hiding it never stops the career.</summary>
public sealed partial class UmaPacerView : UserControl
{
    private readonly IUmaPacerBridge bridge;
    private readonly Func<UmaPacerContext> context;
    private readonly Func<string,bool> endpointAllowed;
    private readonly string? fixedMode;
    private readonly bool standaloneCutProfiles;
    private readonly bool pacerCenter;
    private int displayedModeIndex;

    // MaaUma mode-switch drafts v1
    private readonly Dictionary<(string Mode,int Slot),ModeSwitchDraft> modeSwitchDrafts=new();
    private readonly Dictionary<(string Mode,int Slot),JObject> savedContinuationSettings=new();
    private readonly Dictionary<(string Mode,int Slot),JObject> unsubmittedContinuationDrafts=new();
    private bool restoringContinuationModeDraft;
    private sealed record ModeFormValues(string?[] Text,
        (System.Collections.IEnumerable? Items,object? Selected,int Index)[] Choices,
        bool?[] Checks,decimal?[] Numbers,double[] Sliders);
    private sealed record ModeSwitchDraft(JObject Settings,ModeFormValues Values,JObject? CutDraft,object? SaveCaption);

    private IEnumerable<Control> ModeFormTree(Control root)
    {
        yield return root;
        // Date rows are dynamic and already roundtrip in the structured draft.
        // Do not mix hidden-mode row counts into the fixed raw-form snapshot.
        if(root is UmaCareerPauseDates or MidSkillLearningEditor or UmaDecisionPreferences)yield break;
        if(root is TextBox or ComboBox or ToggleButton or NumericUpDown or Slider)yield break;
        // Closed/unrealized Expanders do not expose Content as logical children.
        // Walk the declared form content, without opening sections or templates.
        IEnumerable<Control> children=root switch {
            Panel panel=>panel.Children,
            Decorator decorator when decorator.Child is Control child=>new[]{child},
            ContentControl content when content.Content is Control child=>new[]{child},
            _=>root.GetLogicalChildren().OfType<Control>()
        };
        foreach(var child in children)foreach(var nested in ModeFormTree(child))yield return nested;
    }
    private Control[] ModeFormControls()
    {
        return new Control[]{careerOptions,cutOptions,continuation,strategy,midSkillLearning}
            .SelectMany(ModeFormTree)
            .Distinct().Where(control=>control!=mode&&control!=presetSlots&&control!=records
                &&control!=cutPushToken&&control!=cutTokenFile
                &&control.Name is not ("PacerAiBaseUrl" or "PacerAiModel" or "PacerAiKey")
                &&(control is not TextBox text||text.PasswordChar==default(char))).ToArray();
    }
    private ModeFormValues CaptureModeForm()
    {
        var controls=ModeFormControls();
        return new(controls.OfType<TextBox>().Select(box=>box.Text).ToArray(),
            controls.OfType<ComboBox>().Select(box=>(box.ItemsSource,box.SelectedItem,box.SelectedIndex)).ToArray(),
            controls.OfType<ToggleButton>().Select(box=>box.IsChecked).ToArray(),
            controls.OfType<NumericUpDown>().Select(box=>box.Value).ToArray(),
            controls.OfType<Slider>().Select(box=>box.Value).ToArray());
    }
    private void RestoreModeForm(ModeFormValues values)
    {
        var controls=ModeFormControls();
        var texts=controls.OfType<TextBox>().ToArray();var choices=controls.OfType<ComboBox>().ToArray();
        var checks=controls.OfType<ToggleButton>().ToArray();var numbers=controls.OfType<NumericUpDown>().ToArray();
        var sliders=controls.OfType<Slider>().ToArray();
        if(texts.Length!=values.Text.Length||choices.Length!=values.Choices.Length||checks.Length!=values.Checks.Length
            ||numbers.Length!=values.Numbers.Length||sliders.Length!=values.Sliders.Length)
            throw new InvalidOperationException("表单结构已变化，模式草稿未被覆盖。");
        for(var i=0;i<checks.Length;i++)checks[i].IsChecked=values.Checks[i];
        for(var i=0;i<numbers.Length;i++)numbers[i].Value=values.Numbers[i];
        for(var i=0;i<sliders.Length;i++)sliders[i].Value=values.Sliders[i];
        for(var i=0;i<choices.Length;i++) {
            choices[i].ItemsSource=values.Choices[i].Items;
            if(values.Choices[i].Selected!=null)choices[i].SelectedItem=values.Choices[i].Selected;
            else choices[i].SelectedIndex=values.Choices[i].Index;
        }
        // Search inputs can rebuild picker ItemsSource. Restore selected items
        // first, so the existing search handler keeps that selected choice.
        for(var i=0;i<texts.Length;i++)texts[i].Text=values.Text[i];
    }
    private ModeSwitchDraft CaptureModeDraft()
    {
        var previous=applying;applying=true;
        var raw=CaptureModeForm();
        try {
            var value=OptionsCore();
            // Backend preset responses normalize this input to an array; the
            // draft restore path must provide that shape without validating it.
            if(!IsCut)value["arc_policy"]=arcSkillPolicy.ReadDraft();
            var policy=IsCut?(JObject)value["cut"]!:value;
            // These are provider/global notification inputs, not mode drafts.
            policy.Remove("pushplus_token_file");
            if(decisionPreferencesAvailable)policy["decision_preferences"]=decisionPreferences.Read();
            if(aiAvailable)policy["ai_options"]=aiOptions.Read();
            if(continuationAvailable) {
                policy["continuation"]=continuation.ReadDraft();
            }
            return new(value,raw,cutDraft==null?null:(JObject)cutDraft.DeepClone(),savePreset.Content);
        }
        finally {try {RestoreModeForm(raw);} finally {applying=previous;}}
    }
    private void ApplyModeDraftSettings(JObject settings)
    {
        var currentTokenFile=cutTokenFile.Text;
        try {ApplySettings(settings);}
        finally {cutTokenFile.Text=currentTokenFile;}
    }
    private void RestoreModeDraft(ModeSwitchDraft draft)
    {
        var previous=applying;var previousDraft=restoringContinuationModeDraft;applying=true;restoringContinuationModeDraft=true;
        try {
            ApplyModeDraftSettings(draft.Settings);
            RestoreModeForm(draft.Values);
            cutDraft=draft.CutDraft==null?null:(JObject)draft.CutDraft.DeepClone();
            savePreset.Content=draft.SaveCaption;
        } finally {restoringContinuationModeDraft=previousDraft;applying=previous;}
    }
    private void LoadModeSwitchPresets(JObject payload)
    {
        var target=(int?)payload["selected_id"]??1;
        presetId=target is >=1 and <=3?target:1;
        LoadPresets(payload,false);
        var selected=(payload["slots"] as JArray??new()).OfType<JObject>()
            .Single(row=>(int?)row["id"]==presetId);
        var settings=selected["settings"] as JObject??new();
        ApplyModeDraftSettings(settings);
        cutDraft=(JObject)settings.DeepClone();savePreset.Content="保存配置";
    }

    private JObject? currentRecord;
    private readonly TextBlock presetModeCaption=Label("常用配置",12);
    private readonly Button resumeRecord=new(){Name="PacerResumeRecord",Content="按原记录续跑",IsVisible=false};
    private readonly Button continueFound=new(){Name="PacerContinueFound",Content="原局转自动育成",IsVisible=false};
    private readonly Button resumeManualHandoff=new(){Name="PacerResumeManualHandoff",Content="技能已学完，继续本局",IsVisible=false};
    private readonly CheckBox newCareerAfterHandoff=new(){Name="PacerNewCareerAfterHandoff",Content="旧局已手动结束，按当前配置开始新一轮",IsVisible=false};
    private string? newCareerHandoffRun;
    private string? newCareerHandoffEndpoint;
    public string CurrentMode=>Mode;
    private string PresetKind=>pacerCenter&&!IsCut?"career":"cut";
    private readonly ComboBox presetSlots = new() {Name="PacerPresetSlot",HorizontalAlignment=HorizontalAlignment.Stretch};
    private readonly Button savePreset = new() {Name="PacerSavePreset",Content="保存配置"};
    private readonly Grid presetRow = new() {ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};
    private bool settingsHostedSeparately;
    public bool SupportsSettingsPages => standaloneCutProfiles;
    private int presetId=1;
    private bool presetBusy;
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly ComboBox records = new() { Name="PacerRecords", HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly ComboBox mode = new() { Name="PacerMode", ItemsSource=new[] { "普通育成", "刷切 · 才能出众" }, SelectedIndex=0 };
    private readonly TextBlock recordLabel = Label("当前局记录",12);
    private readonly TextBlock recordNote = Label("记录只建立本地档案；点击开始时根据现场继续育成或按配置开局。",12);
    private readonly TextBlock subtitle = Label("接管当前育成 / 按配置开局 · 凯旋门",12);
    private readonly StackPanel careerOptions = new() { Spacing=12 };
    private readonly StackPanel cutOptions = new() { Spacing=10, IsVisible=false };
    private readonly UmaDecisionPreferences decisionPreferences = new();
    private readonly Expander decisionPreferencesSection;
    private bool decisionPreferencesAvailable;
    private bool decisionPreferencesQueried;
    private readonly UmaAiOptions aiOptions=new();
    private readonly MaaUmaAiStatusPanel aiStatus=new();
    private readonly Expander aiSection;
    private bool aiAvailable,aiQueried;
    public JObject CurrentDecisionPreferences => workflowLibrary?.LibraryReady==true?workflowLibrary.PrimaryDecisionPreferences:decisionPreferences.Read();
    public JObject CurrentScoringProfile
    {
        get
        {
            if(CurrentDecisionPreferences["workflow"] is JObject)
                throw new InvalidOperationException("横向工作流暂未接入在线分享，请使用“保存工作流文件”后分享本地 JSON 文件。");
            return workflowLibrary?.LibraryReady==true?workflowLibrary.PrimaryScoringProfile:decisionPreferences.ReadProfile();
        }
    }
    public string CurrentPresetLabel=>workflowLibrary?.LibraryReady==true ? workflowLibrary.SelectedConfigurationLabel : (pacerCenter?(IsCut?"刷切":"育成"):"")+$"配置{presetId}";
    public bool CanApplySharedWeights=>standaloneCutProfiles&&decisionPreferencesAvailable&&!submitting&&!presetBusy&&!pushBusy
        &&snapshot!=null&&(string?)snapshot["control"]?["state"] is "idle" or "stopped";
    public async Task<bool> PreviewSharedWeights(Window owner,JObject profile,string title)
    {
        if(workflowLibrary?.LibraryReady==true)return await PreviewLibraryWeights(owner,profile,title);
        if(!CanApplySharedWeights)throw new InvalidOperationException("请先结束本轮，再把方案填入配置。");
        if((profile["preferences"] as JObject)?["workflow"] is JObject || (string?)profile["schema_version"]=="uma.scoring_profile.v3")
            throw new InvalidOperationException("横向工作流暂未接入在线预览，请在时间轴界面使用“读取工作流文件”。");
        var slot=presetId;var selectedMode=Mode;
        var schema=await bridge.Request(new JObject {["action"]="decision_preferences_schema"});
        if((bool?)schema["available"]!=true||schema["tuning_schema"] is not JObject tuningSchema)
            throw new InvalidOperationException("当前版本不支持自定义权重，请使用测试版。");
        var checkedValue=await bridge.Request(new JObject {["action"]="validate_decision_preferences",["profile"]=profile.DeepClone()});
        var preferences=checkedValue["preferences"] as JObject??throw new InvalidOperationException("方案兼容性校验未完成。");
        if(preferences["workflow"] is JObject)
            throw new InvalidOperationException("此方案包含横向工作流，请在时间轴界面使用“读取工作流文件”。");
        var workflowDraft=decisionPreferences.Read()["workflow"]?.DeepClone();
        if(!CanApplySharedWeights||slot!=presetId||selectedMode!=Mode)throw new InvalidOperationException("当前配置或运行状态已变化，请重新预览。");
        var editor=new UmaScoreEditor(tuningSchema,(JObject)preferences.DeepClone(),
            value=>bridge.Request(new JObject {["action"]="validate_decision_preferences",["profile"]=value}),
            (string?)(preferences["tuning"] as JObject)?["mode"]=="formula"?"formula":"detailed",preserveMode:true) {Title=$"预览群友方案：{title} → 配置{slot}"};
        var result=await editor.ShowDialog<JObject?>(owner);
        if(result==null)return false;
        if(!CanApplySharedWeights||slot!=presetId||selectedMode!=Mode)throw new InvalidOperationException("当前配置或运行状态已变化，尚未填入方案。");
        if(workflowDraft is JObject keptWorkflow){keptWorkflow["enabled"]=false;if(keptWorkflow["editor_graph"] is JObject keptGraph)keptGraph["enabled"]=false;result["workflow"]=keptWorkflow;}
        decisionPreferences.Load(result);cutDraft=Options();savePreset.Content="保存配置";
        hint.Text=$"已填入配置{slot}的高级权重，时间轴草稿已保留并关闭；请点击保存配置，新一轮生效。";
        return true;
    }
    private readonly UmaContinuationOptions continuation = new() { Name="PacerContinuationOptions" };
    private readonly Expander continuationSection;
    private readonly TextBlock continuationStatus = Label("续养节点：尚未启用",12);
    private readonly TextBlock continuationDetails = Label("",12);
    private readonly Expander continuationResults;
    private bool continuationAvailable;
    private bool continuationQueried;
    private readonly ComboBox careerCharacter = new() { Name="PacerCareerCharacter" };
    private readonly IReadOnlyList<UmaTraineeChoice> careerCharacters;
    private readonly TextBox careerCharacterName = new() { Name="PacerCareerCharacterName", Watermark="当前角色的确切中文名" };
    private readonly ComboBox careerDeck = new() { Name="PacerCareerDeck", ItemsSource=Enumerable.Range(0,11).Select(i=>i==0 ? "沿用当前卡组" : $"卡组 {i}").ToArray(), SelectedIndex=0 };
    private readonly ComboBox careerBorrow = new() { Name="PacerCareerBorrow" };
    private readonly IReadOnlyList<UmaSupportChoice> careerBorrowChoices;
    private readonly TextBox careerBorrowLevel = new() { Name="PacerCareerBorrowLevel", Text="1" };
    private readonly ComboBox careerBorrowBreak = new() { Name="PacerCareerBorrowBreak", ItemsSource=new[] {
        "不限突破", "至少 1 破", "至少 2 破", "至少 3 破", "满破（4 破）" }, SelectedIndex=0 };
    private readonly ComboBox cutCharacter = new() { Name="PacerCutCharacter" };
    private readonly TextBox cutCharacterSearch=new() {Name="PacerCutCharacterSearch",Watermark="输入角色名、服装名或编号"};
    private readonly IReadOnlyList<UmaTraineeChoice> cutCharacters;
    private readonly TextBlock cutCharacterHint = Label("",12);
    private readonly TextBox cutName = new() { Name="PacerCutName", Watermark="当前所选角色的确切中文名" };
    private readonly TextBox cutAttempts = new() { Name="PacerCutAttempts", Text="20" };
    private readonly TextBox cutItems = new() { Name="PacerCutItems", Text="0" };
    private readonly TextBox cutCarrots = new() { Name="PacerCutCarrots", Text="0" };
    private readonly ComboBox cutCheckMode = new() { ItemsSource=new[] { "事件触发＋间隔补查", "每回合检查（对照）" }, SelectedIndex=0 };
    private readonly TextBox cutInterval = new() { Text="4" };
    private readonly CheckBox cutDebutRetry = new() { Content="出道失利用已有闹钟重赛", IsChecked=true };
    private readonly CheckBox cutCharacterMiss = new() { Name="PacerCutCharacterMiss", Content=new TextBlock {Text="角色专属小切事件后，查无切则重开",TextWrapping=TextWrapping.Wrap}, IsChecked=false };
    private readonly CheckBox cutTrainingFailure = new() { Name="PacerCutTrainingFailure", Content="训练失败后弃养重开", IsChecked=false };
    private readonly CheckBox cutRetain = new() { Content="详细过程记录（时限见组件维护说明）", IsChecked=false };
    private readonly CheckBox cutDiscardFound = new() { Name="PacerCutDiscardFound", Content="耐久测试：有切通知后弃养并继续（仅流程验证）", IsChecked=false };
    private readonly TextBox cutTokenFile = new() { Watermark="可留空；选择已保存的 PushPlus 令牌文件" };
    private readonly TextBox cutPushToken = new() {Name="PacerPushToken",PasswordChar='●',Watermark="直接粘贴 PushPlus token；保存后隐藏"};
    private readonly TextBlock pushStatus = new() {Name="PacerPushStatus",Text="推送设置加载中……",TextWrapping=TextWrapping.Wrap,FontSize=12};
    private readonly Button pushSave = new() {Name="PacerPushSave",Content="保存令牌"};
    private readonly Button pushClear = new() {Name="PacerPushClear",Content="关闭推送"};
    private readonly Button pushTest = new() {Name="PacerPushTest",Content="发送测试通知"};
    private bool pushBusy;
    private readonly TextBox cutTemplate = new() { Watermark="可留空；已有育成记录目录" };
    private readonly ComboBox cutDeck = new() { Name="PacerCutDeck", ItemsSource=Enumerable.Range(0,11).Select(i=>i==0 ? "沿用当前卡组" : $"卡组 {i}").ToArray(), SelectedIndex=0 };
    private readonly ComboBox cutBorrow = new() { Name="PacerCutBorrow" };
    private readonly TextBox cutBorrowSearch=new() {Name="PacerCutBorrowSearch",Watermark="输入角色名、卡名、类型或编号"};
    private readonly IReadOnlyList<UmaSupportChoice> cutBorrowChoices;
    private readonly TextBox cutBorrowLevel = new() { Name="PacerCutBorrowLevel", Text="1" };
    private readonly ComboBox cutBorrowBreak = new() { Name="PacerCutBorrowBreak", ItemsSource=new[] {
        "不限突破", "至少 1 破", "至少 2 破", "至少 3 破", "满破（4 破）" }, SelectedIndex=0 };
    private readonly CheckBox cutConfirmed = new() { Name="PacerCutSetupConfirmed", Content="已配好角色、卡组和种马，当前在该局第一回合" };
    private readonly StackPanel cutRuntime = new() { Spacing=7, IsVisible=false };
    private readonly TextBlock cutProgress = Label("刷切尚未开始",18);
    private readonly TextBlock cutTiming = Label("已完成局平均耗时 —",12);
    private readonly TextBlock cutCheck = Label("查切结果：待检查",12);
    private readonly TextBlock cutResources = Label("TP 药 0 · 萝卜 0",12);
    private readonly TextBlock cutNotification = Label("通知：有切时保留本局并通知",12);
    private readonly TextBlock ssSieveRuntime = Label("",12);
    private readonly ComboBox strategy = new() { Name="PacerStrategy", ItemsSource=StrategyRevisionItems, SelectedIndex=FactoryStrategyIndex, HorizontalAlignment=HorizontalAlignment.Stretch };
    private readonly TextBlock strategyStatus = Label("策略版本只对下次新建生效；继续原局沿用开局时的版本。",12);
    private readonly Dictionary<string, TextBox> targets = new();
    private readonly TextBox risk = new() { Text = "25" };
    private readonly TextBox mei = new() { Watermark = "未知可留空" };
    private readonly TextBox limit = new() { Text = "72" };
    private readonly MidSkillLearningEditor midSkillLearning;
    private readonly UmaArcSkillPolicyEditor arcSkillPolicy;
    private readonly Expander arcSkillSection;
    private readonly Expander midSkillSection;
    private readonly TextBlock midSkillSummary=Label("中途学习：未启用",12);
    private readonly Button midSkillOpen=new() {Name="PacerOpenMidSkillSettings",Content="前往技能学习",IsVisible=false};
    public event Action? MidSkillSettingsRequested;
    private readonly CheckBox retain = new() { Content = "详细五页训练记录（时限见组件维护说明）" };
    private readonly CheckBox terminal = new() { Content = "末尾自动学习技能并正常结算", IsChecked = true };
    private readonly CheckBox manualFirstArc = new() { Name="PacerManualFirstArc", Content="首次凯旋门前停下，手动学技能", IsChecked=false };
    private readonly CheckBox manualFinalArc = new() { Name="PacerManualFinalArc", Content="末次凯旋门前交还操作", IsChecked=false };
    private readonly TextBlock manualHandoffStatus = new() { Name="PacerManualHandoffStatus",TextWrapping=TextWrapping.Wrap,IsVisible=false };
    private readonly TextBlock device = Label("设备：尚未选择");
    private readonly TextBlock account = Label("账号：未从游戏确认（不登录、不切号）", 12);
    private readonly TextBlock owner = Label("占用：等待读取", 12);
    private readonly TextBlock stateText = Label("待开始", 16);
    private readonly TextBlock career = Label("等待读取当前育成", 20);
    private readonly TextBlock date = Label("启动后从当前局记录显示阶段与回合", 12);
    private readonly TextBlock stats = Label("速度 —     耐力 —     力量 —     毅力 —     智力 —", 16);
    private readonly TextBlock currentStep = Label("当前步骤：等待开始",12);
    private readonly TextBlock action = Label("尚未连接运行记录");
    private readonly TextBlock lastSegmentText=new() {Name="PacerLastSegmentResult",TextWrapping=TextWrapping.Wrap,FontSize=12,IsVisible=false};

    // MaaUma Pacer entry help and stop report v1
    private readonly Button submitStopReport = new() { Name="PacerSubmitStopReport", Content="查看并提交停止报告", IsEnabled=false };
    private readonly MaaUmaNativeStopDiagnosticsButton exportStopDiagnostics = new();
    private readonly Button openGettingStarted = new() { Name="PacerOpenGettingStarted", Content="查看新手指南" };
    public event Action? GettingStartedRequested;
    private bool stopReportBusy, stopReportAvailable;

    private readonly TextBlock timing = Label("本次运行 —   暂停 —   墙钟时间 —", 12);
    private readonly TextBlock midSkillRuntimeStatus = Label("", 12);
    private readonly TextBlock log = Label("尚无运行记录。", 12);
    private readonly TextBlock hint = Label("已有育成就继续；主页没有育成时按选马、卡组与借卡配置开局。", 12);
    private readonly TextBlock overviewProgress = Label("本轮 · 尚未开始", 15);
    private readonly TextBlock overviewEquipment = Label("", 14);
    private readonly Button start = new() { Name="PacerStart", Content = "开始育成", IsEnabled = false };
    private readonly Button pause = new() { Name="PacerPause", Content = "暂停", IsEnabled = false };
    private readonly Button stop = new() { Name="PacerStop", Content = "停止脚本", IsEnabled = false };
    private readonly StackPanel configuration = new() { Spacing = 12 };
    private readonly Grid body = new() { ColumnDefinitions = new("360,*"), RowDefinitions = new("*") };
    private readonly Control configCard;
    private readonly Control runtimeCard;
    private readonly WrapPanel footerButtons;
    private PacerOverviewPreflightCard? overviewPreflightCard;
    private StackPanel? overviewRightColumn;
    private EventPreferenceEditor? boundEventEditor;
    public event Action? CustomWeightsRequested;
    public event Action? ConfigStrategyRequested;
    public event Action? ConfigEventsRequested;
    public event Action<string, bool, bool>? PrimaryRunStateChanged;
    public async Task TriggerSidebarPrimaryRunAsync()
    {
        if (start.IsEnabled)
            await Submit("start");
    }
    internal void RefreshOverviewPreflightCard()
    {
        if (overviewPreflightCard == null || workflowLibrary == null) return;
        var ov = workflowLibrary.ReadOverviewSummary();
        var (evScope, evManual, withStr) = boundEventEditor?.ReadPreflightSummary() ?? ("全局事件库", 0, "自动选择");
        overviewPreflightCard.Update(
            ov.ModeProfile,
            ov.EquipmentBatch,
            ov.State,
            ov.PuritySummary,
            ov.TargetsText,
            $"{evScope} · 已指定 {evManual} 项事件",
            $"With 优先：{withStr}");
    }
    private JObject? snapshot;
    private JObject? cutDraft;
    private JObject? loadedLarcPolicy;
    private string? commandError;
    private string listedEndpoint = "";
    private string listedMode = "";
    private bool refreshing;
    private bool submitting;
    private bool applying;
    private string? runRoot;
    private static readonly HashSet<string> Active = new() { "starting", "running", "pause_requested", "paused", "resume_requested", "stop_requested", "unresponsive" };
    private static readonly Dictionary<string, string> States = new() {
        ["idle"]="待开始", ["starting"]="正在启动", ["running"]="运行中", ["pause_requested"]="正在暂停",
        ["paused"]="已暂停", ["resume_requested"]="正在继续", ["stop_requested"]="正在停止", ["stopped"]="已停止", ["unresponsive"]="任务无响应"
    };
    private bool IsCut => mode.SelectedIndex==1;
    private string Mode => IsCut ? "cut" : "career";
    private UmaTraineeChoice? CutCharacter => cutCharacter.SelectedItem as UmaTraineeChoice;
    private UmaTraineeChoice? CareerCharacter => careerCharacter.SelectedItem as UmaTraineeChoice;
    private static readonly Dictionary<string,string> CutStates = new() {
        ["ready"]="待开始", ["running"]="运行中", ["found"]="已确认才能出众 · 本局已保留",
        ["unknown"]="状态未读清 · 本局已保留", ["stopped"]="已停止 · 当前局已保留",
        ["limit_reached"]="已达到本批次次数上限", ["route_failed"]="流程中断 · 查看停点后可续跑"
    };

    private static readonly Dictionary<string,string> RuntimeSteps = new() {
        ["ai_decision"]="正在等待AI选择", ["larc_ai_pause"]="AI暂不可用，已暂停保留；恢复后重新观察当前页面",
        ["starting"]="启动育成流程", ["turn_start"]="读取本回合状态", ["capture"]="采集游戏画面",
        ["read_training"]="读取训练选项", ["execute_training"]="执行训练",
        ["execute_mei_outing"]="安排黄帽外出", ["execute_rest"]="安排休息",
        ["execute_low_vitality_rest_from_main_menu"]="安排休息", ["execute_clinic"]="前往医务室",
        ["career_license_start"]="验证本机授权", ["career_batch_next"]="准备下一局",
        ["career_batch_current"]="准备当前局", ["ss_sieve_attempt_start"]="开始群星赛筛选",
        ["ss_sieve_opening"]="准备开局", ["ss_sieve_abandon"]="结束未达标的育成局",
        ["before_input"]="准备操作游戏画面", ["waiting"]="等待画面或当前操作完成",
        ["loading"]="等待游戏画面加载", ["enter_race_from_race_day"]="从比赛日进入赛事",
        ["go_to_race_from_detail"]="进入本场比赛", ["confirm_race_entry"]="确认参赛",
        ["view_race_result"]="查看比赛结果", ["race_result_continue"]="继续比赛结算",
        ["cut_attempt_start"]="开始本局刷切", ["cut_before_turn"]="检查下一回合的续养条件",
        ["cut_restart"]="结束上一局并准备重新开局", ["cut_safe_boundary"]="等待当前操作结束",
        ["cut_license_start"]="验证本机授权", ["skills_start"]="准备读取技能列表",
        ["skills_capture"]="识别技能列表", ["run_once"]="分析本回合并选择行动",
        ["race_freeze_observation"]="检查比赛画面是否卡住", ["race_freeze_restart"]="恢复卡住的比赛流程",
        ["larc_ss_information.open"]="打开群星赛信息", ["larc_ss_information.scroll_up"]="向上翻阅群星赛信息",
        ["larc_ss_information.scroll_down"]="向下翻阅群星赛信息", ["larc_ss_information.close"]="关闭群星赛信息并返回训练页",
        ["tap"]="点击游戏画面", ["tap_no_wait"]="点击游戏画面", ["swipe"]="滑动游戏列表",
        ["training"]="执行训练", ["train"]="执行训练", ["race"]="处理比赛流程",
        ["event_choice"]="选择事件选项", ["rest"]="安排休息", ["outing"]="处理外出",
        ["adaptation"]="检查海外适应性", ["ss_reward_information"]="读取群星赛奖励信息",
        ["main_menu_state"]="读取育成主页状态", ["home_preparation"]="准备进入训练页",
        ["cut_status"]="检查才能出众", ["finished"]="本次运行已结束"
    };
    private static readonly Dictionary<string,string> RuntimeReasons = new() {
        ["larc_ss_information_failed"]="读取群星赛信息失败",
        ["unsupported_screen:loading"]="比赛画面仍在加载，尚未识别到下一步操作",
        ["unsupported_screen"]="当前页面尚未识别到可继续的操作",
        ["input_not_confirmed"]="尚未确认操作是否执行成功",
        ["information_open_frozen"]="打开群星赛信息后，画面持续没有变化",
        ["information_open_deferred"]="等待群星赛信息页打开",
        ["information_entry_unverified"]="尚未识别到群星赛信息入口",
        ["information_page_lost"]="当前画面已不在群星赛信息页",
        ["return_training_unverified"]="尚未确认已返回训练页",
        ["training_hud_changed"]="返回后的回合或属性发生变化，需要重新识别",
        ["scroll_coverage_incomplete"]="群星赛信息尚未完整读取",
        ["returned_after_collection"]="群星赛信息读取完成，已返回训练页",
        ["ss_not_unlocked"]="群星赛尚未解锁",
        ["freeze_navigation_not_proved"]="暂未满足自动恢复条件",
        ["race_transition_no_progress_timeout"]="比赛页面长时间没有推进",
        ["career_transition_no_progress_timeout"]="育成页面长时间没有推进",
        ["restart_page_unrecognized"]="重新开局时未识别当前页面",
        ["restart_action_repeated_surface"]="重新开局操作后仍停在同一页面",
        ["refill_page_unrecognized"]="未识别到训练值恢复页面",
        ["tp_refill_limit_or_item_not_visible"]="补给额度不足，或未找到可用的TP药和宝石",
        ["tp_item_stock_unknown"]="未读清TP药库存",
        ["tp_insufficient_no_allowed_resources"]="当前TP不足，没有可用药品或已允许的后备补给",
        ["tp_carrots_insufficient"]="萝卜不足，本次TP补给已结束",
        ["tp_refill_limit"]="本次TP补给次数或消费额度已用完",
        ["refill_unit_cost_unknown"]="尚未识别本次萝卜花费，当前页面已保留",
        ["event_route_preflight"]="事件选项操作前检查未通过",
        ["transition_contract_missing"]="页面切换状态尚未确认",
        ["terminal_rating_without_finish_receipt"]="已到结算评分页，但结算完成尚未确认",
        ["cut_cutoff_absent"]="已到刷切截止点，未发现才能出众",
        ["cut_character_event_absent"]="角色事件后无切，按批次规则处理重开",
        ["cut_found"]="已确认才能出众，当前局已保留",
        ["cut_condition_found"]="已确认才能出众，当前局已保留",
        ["cut_character_mismatch"]="游戏角色与配置不一致",
        ["new_cycle_current_scene_unverified"]="尚未识别到可接手的育成画面，本局已保留；请保持育成主页面稳定，无弹窗时可提交报告。",
        ["larc_adaptation_unreadable"]="海外适应性页面读取未完成，已保留本局，可提交诊断。",
        ["training_failed"]="检测到训练失败",
        ["mei_unlock_failed"]="黄帽外出解锁失败",
        ["supporter_deadline_missed"]="期限所需协助者积分未达标",
        ["no_cut_after_character_event"]="角色切事件结束，未发现才能出众",
        ["ordinary_abandon_confirmed"]="已确认放弃上一局",
        ["attempt_limit_after_abandon"]="本轮次数已用完",
        ["user_stop"]="已按要求停止，当前育成保留",
        ["execution_exception"]="执行过程出现异常",
        ["coordinate_unverified"]="操作位置尚未识别清楚",
        ["capture_failed"]="未能取得游戏截图", ["unsupported_capture"]="不支持当前游戏截图尺寸",
        ["adb_connect_failed"]="未能连接模拟器", ["device_busy"]="设备正被其他任务使用",
        ["backend_running"]="后台任务尚未退出", ["application_running"]="程序或更新助手仍在运行",
        ["ready_main_menu"]="已回到育成主页", ["route_state_out_of_scope"]="当前页面尚未识别到可继续的流程",
        ["max_steps_reached"]="达到本段处理步数上限", ["started"]="已启动本轮任务",
        ["paused"]="已暂停", ["stopped"]="已停止", ["resumed"]="已继续运行",
        ["ready"]="准备就绪", ["completed"]="本段处理已完成",
        ["resume_partial_action_requires_reconciliation"]="暂停前的操作结果尚未确认，需要重新读取现场",
        ["activation_required"]="请先激活本机", ["access_revoked"]="本机授权已停用",
        ["service_unavailable"]="暂时无法连接服务", ["device_changed"]="授权与当前电脑不匹配",
        ["ModuleNotFoundError"]="缺少运行所需模块", ["ImportError"]="无法加载运行所需模块",
        ["FileNotFoundError"]="缺少运行所需文件", ["TimeoutError"]="操作等待超时",
        ["TimeoutExpired"]="操作等待超时", ["PermissionError"]="没有权限访问所需文件或设备",
        ["ValueError"]="读取的参数或状态不符合预期", ["RuntimeError"]="运行过程出现异常"
    };
    private static string RuntimeText(string? value,bool step=false)
    {
        if(string.IsNullOrWhiteSpace(value))return step ? "" : "等待本局运行记录";
        value=value.Trim();
        if(RuntimeSteps.TryGetValue(value,out var operation))return operation;
        if(RuntimeReasons.TryGetValue(value,out var reason))return reason;
        if(value.Contains("No such file or directory",StringComparison.OrdinalIgnoreCase) || value.Contains("[Errno 2]"))
            return "缺少运行所需文件，请在详细记录中查看具体路径。";
        if(value.Any(c=>c is >= '\u3400' and <= '\u9fff')) {
            var separator=value.IndexOfAny(new[]{':','：'});
            if(separator>0) {
                var detail=value[(separator+1)..].Trim();
                var code=value[..separator].Trim();
                if(RuntimeReasons.TryGetValue(code,out var prefix))return prefix+"："+detail;
                if(code.All(c=>char.IsAsciiLetterOrDigit(c) || c is '_' or '-' or '.'))return detail;
            }
            return value;
        }
        var parts=value.Split(new[]{':','：'},StringSplitOptions.RemoveEmptyEntries);
        var known=parts.Select(part=>part.Trim()).Where(RuntimeReasons.ContainsKey).ToArray();
        var specific=known.Where(part=>part is not ("execution_exception" or "ValueError" or "RuntimeError")).ToArray();
        if(specific.Length>0)known=specific;
        if(known.Length>0)return string.Join("；",known.Select(part=>RuntimeReasons[part]).Distinct());
        return step ? "" : "请展开下方的详细记录查看运行信息。";
    }

    private static string? WorkflowStage(JObject? workflow, string? nodeId, bool active, bool running)
    {
        var status=(string?)workflow?["state"]?["status"];
        if(status=="completed")return "本轮已完成";
        if(status=="preserved")return "本局已保留";
        if(status=="waiting")return "等待继续育成";
        if(!active || string.IsNullOrWhiteSpace(nodeId) || workflow?["document"]?["run_graph"]?["nodes"] is not JArray nodes)
            return null;
        var node=nodes.OfType<JObject>().FirstOrDefault(item=>(string?)item["id"]==nodeId);
        return (string?)node?["kind"] switch {
            "entry"=>"准备开始",
            "career" when workflow?["ace_cycle_counts"] is JObject=>running ? "正在连养大赛成品马" : "成品马连养阶段",
            "career" when workflow?["ss_sieve_counts"] is JObject=>running ? "正在筛选群星赛" : "群星赛筛选阶段",
            "career"=>running ? "正在育成" : "育成阶段",
            "cut"=>running ? "正在刷切筛选" : "刷切筛选阶段",
            "condition"=>"检查育成条件", "continue_current"=>"继续本局",
            "restart"=>"准备重开", "hold"=>"保留本局", "finish"=>"收尾阶段",
            _=>null
        };
    }

    public UmaPacerView() : this(UmaPacerBridge.Instance, UmaPacerBridge.ReadContext) { }
    public UmaPacerView(IUmaPacerBridge bridge, Func<UmaPacerContext> context, IReadOnlyList<UmaTraineeChoice>? traineeChoices=null,
        string? fixedMode=null, string? pageTitle=null, IReadOnlyList<UmaSupportChoice>? supportChoices=null,
        Func<string,bool>? endpointAllowed=null, bool standaloneCutProfiles=false,bool pacerCenter=false)
    {
        if (fixedMode is not (null or "career" or "cut")) throw new ArgumentException("Unknown Pacer mode.", nameof(fixedMode));
        this.bridge = bridge;
        this.endpointAllowed=endpointAllowed ?? (endpoint => endpoint is "127.0.0.1:16384" or "127.0.0.1:16416" or "127.0.0.1:16448");
        this.context = context;
        var midSkillCatalog=EventSkillPicker.LoadCatalog(AppContext.BaseDirectory,out var midSkillCatalogError);
        midSkillLearning=new MidSkillLearningEditor(midSkillCatalog,midSkillCatalogError);
        arcSkillPolicy=new UmaArcSkillPolicyEditor();
        arcSkillSection=new Expander {Name="PacerArcCmSkillPolicy",Header="凯旋门与终盘 · CM 大赛目标自动选技",Content=arcSkillPolicy};
        var midSkillContent=new StackPanel {Spacing=12,Children={midSkillLearning,arcSkillSection}};
        midSkillSection=new Expander {Name="PacerMidSkillSection",Header="育成中学技能",Content=midSkillContent};
        this.fixedMode = fixedMode;
        this.pacerCenter=pacerCenter;
        this.standaloneCutProfiles=standaloneCutProfiles && (fixedMode=="cut" || pacerCenter);
        midSkillLearning.SetPresetSaveAvailable(this.standaloneCutProfiles);
        if(pacerCenter)mode.ItemsSource=new[]{"自动育成","刷切筛选"};
        mode.SelectedIndex = fixedMode == "cut" ? 1 : 0;
        displayedModeIndex=mode.SelectedIndex;
        mode.IsEnabled = fixedMode == null;
        cutCharacters=traineeChoices ?? UmaTraineePicker.Load(AppContext.BaseDirectory);
        UmaTraineePicker.Configure(cutCharacter,cutCharacters);
        cutCharacter.SelectedItem=cutCharacters.FirstOrDefault(choice=>choice.CardId=="101001") ?? cutCharacters.Last();
        cutBorrowChoices=supportChoices ?? UmaSupportPicker.Load(AppContext.BaseDirectory);
        UmaTraineePicker.Configure(cutBorrow,cutBorrowChoices);
        SukiUI.Extensions.ComboBoxExtensions.SetSearchWatermark(cutBorrow,"搜索借卡角色、卡名或类型");
        cutBorrow.SelectedItem=cutBorrowChoices[0];
        careerCharacters=new[] { new UmaTraineeChoice("current","","沿用游戏当前角色","↻",
            "仅接管已有育成；需要新开局时请选择具体角色和衣装。") }.Concat(cutCharacters).ToArray();
        UmaTraineePicker.Configure(careerCharacter,careerCharacters);
        careerCharacter.SelectedItem=careerCharacters[0];
        careerBorrowChoices=cutBorrowChoices.Select(choice=>choice.CardId==0
            ? choice with { Label="沿用当前局借卡", Description="接管当前育成时不更换已经借到的卡。" }
            : choice).ToArray();
        UmaTraineePicker.Configure(careerBorrow,careerBorrowChoices);
        SukiUI.Extensions.ComboBoxExtensions.SetSearchWatermark(careerBorrow,"搜索借卡角色、卡名或类型");
        careerBorrow.SelectedItem=careerBorrowChoices[0];
        var root = new Grid { RowDefinitions = new("Auto,Auto,*,Auto"), Margin = new Thickness(20) };
        var title = new StackPanel { Spacing = 4, Margin = new Thickness(0,0,0,14) };
        var heading = Label(pageTitle ?? "育成助手 · Pacer", 24);
        title.Children.Add(heading);
        title.Children.Add(subtitle);
        if(pacerCenter) {
            overviewProgress.Name="PacerOverviewRunCount";
            overviewProgress.FontWeight=FontWeight.SemiBold;
            overviewEquipment.Name="PacerOverviewCurrentEquipment";
            overviewEquipment.IsVisible=false;
            title.Children.Add(overviewProgress);
            title.Children.Add(overviewEquipment);
            var modeBar=new StackPanel {Orientation=Orientation.Horizontal,Spacing=12,Margin=new Thickness(0,8,0,0)};
            modeBar.Children.Add(Label("运行模式",13));mode.MinWidth=210;modeBar.Children.Add(mode);
            // The workflow library owns user-facing mode selection. Keep the
            // engine picker attached but hidden for old runtime readouts.
            modeBar.IsVisible=false;title.Children.Add(modeBar);
        }
        root.Children.Add(title);
        var top = new WrapPanel { Margin = new Thickness(0,0,0,14) };
        foreach (var text in new[] { device, account, owner }) { text.Margin = new Thickness(0,0,20,6); top.Children.Add(text); }
        Grid.SetRow(top,1); root.Children.Add(top);

        configuration.Children.Add(Label(pacerCenter ? "下次开始使用的配置" : this.standaloneCutProfiles ? "刷切配置" : "运行配置", 18));
        if(this.standaloneCutProfiles) {
            configuration.IsEnabled=false;
            presetRow.Children.Add(presetSlots);Grid.SetColumn(savePreset,1);presetRow.Children.Add(savePreset);
            configuration.Children.Add(presetRow);
        } else {
            configuration.Children.Add(Field("模式",mode));
            configuration.Children.Add(recordLabel);
            configuration.Children.Add(records);
        }
        configuration.Children.Add(recordNote);
        configuration.Children.Add(Field("策略版本", strategy));
        configuration.Children.Add(strategyStatus);
        configuration.Children.Add(midSkillSummary); configuration.Children.Add(midSkillOpen);
        midSkillOpen.Click+=(_,_)=>MidSkillSettingsRequested?.Invoke();
        careerOptions.Children.Add(Label("目标属性", 14));
        // Give four-digit goals room under the native theme/font scale. Wrap
        // whole fields instead of squeezing five text boxes into one row.
        var targetRow = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var pair in new[] { ("speed","速度","1550"), ("stamina","耐力","1200"), ("power","力量","1200"), ("guts","毅力","1200"), ("wisdom","智力","1200") })
        {
            var input = new TextBox { Name="PacerTarget_"+pair.Item1, Text = pair.Item3,
                MinWidth = 96, MinHeight = 40, TextAlignment = TextAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Stretch };
            targets[pair.Item1] = input;
            var column = new StackPanel { Width = 96, Spacing = 5, Margin = new Thickness(0,0,8,10) };
            column.Children.Add(Label(pair.Item2, 12)); column.Children.Add(input); targetRow.Children.Add(column);
        }
        careerOptions.Children.Add(targetRow);
        careerOptions.Children.Add(Field("最高训练失败率（%）", risk));
        var setupOptions=new StackPanel { Spacing=10 };
        setupOptions.Children.Add(Field("育成角色",careerCharacter));
        var manualCareerName=Field("角色名称",careerCharacterName); manualCareerName.IsVisible=false;
        setupOptions.Children.Add(manualCareerName);
        setupOptions.Children.Add(Field("使用卡组",careerDeck));
        setupOptions.Children.Add(Field("指定借卡",careerBorrow));
        var careerBorrowRequirements=new Grid { ColumnDefinitions=new("*,*"), ColumnSpacing=10 };
        careerBorrowRequirements.Children.Add(Field("最低等级（1～50）",careerBorrowLevel));
        var careerBreakField=Field("最低突破",careerBorrowBreak);
        Grid.SetColumn(careerBreakField,1); careerBorrowRequirements.Children.Add(careerBreakField);
        setupOptions.Children.Add(careerBorrowRequirements);
        careerSupportProfile=CreateSupportProfile(false,careerBorrowChoices);
        setupOptions.Children.Add(new Expander{Name="PacerCareerSupportProfileSection",Header="本次实际卡组（可选填写）",Content=careerSupportProfile,IsExpanded=false});
        void CareerCharacterChanged() {
            manualCareerName.IsVisible=CareerCharacter?.Manual==true;
            if(CareerCharacter is { Manual:false } selected)careerCharacterName.Text=selected.CharacterName;
            if(snapshot!=null)Render(context());
        }
        void CareerBorrowChanged()=>careerBorrowRequirements.IsEnabled=(careerBorrow.SelectedItem as UmaSupportChoice)?.CardId>0;
        careerCharacter.SelectionChanged+=(_,_)=>CareerCharacterChanged(); CareerCharacterChanged();
        careerBorrow.SelectionChanged+=(_,_)=>CareerBorrowChanged(); CareerBorrowChanged();
        careerCharacterName.TextChanged+=(_,_)=>{ if(snapshot!=null)Render(context()); };
        setupOptions.Children.Add(Label("已有育成直接继续，不中途换马、换卡或重开；无育成时按以上配置开普通局。种马沿用游戏中已配好的设置；TP 不足会停止，不购买资源。",12));
        careerOptions.Children.Insert(0,new Expander { Name="PacerCareerSetup", Header="选马、卡组与借卡", IsExpanded=true, Content=setupOptions });
        careerOptions.Children.Add(terminal);
        if(pacerCenter) {
            var handoffOptions=new StackPanel {Spacing=8};
            handoffOptions.Children.Add(manualFirstArc);handoffOptions.Children.Add(manualFinalArc);
            handoffOptions.Children.Add(presetDates);
            handoffOptions.Children.Add(Label("未勾选时沿用原流程。首次停点后由你确认继续；末次交还后不自动恢复。",12));
            careerOptions.Children.Add(new Expander {Name="PacerManualHandoffOptions",Header="人工交接停点",Content=handoffOptions});
        }
        var advanced = new StackPanel { Spacing = 10, Margin = new Thickness(0,10,0,0) };
        advanced.Children.Add(Field("黄帽 SSR 突破数（0～4）", mei));
        advanced.Children.Add(Field("最多推进回合（1～72）", limit));
        advanced.Children.Add(retain);
        advanced.Children.Add(Label("默认只保存必要现场与回执；勾选后额外保存五页训练，回收规则以当前组件的维护说明为准，当前动作与续跑现场保留。", 12));
        careerOptions.Children.Add(new Expander { Header = "高级设置与记录保留", Content = advanced });
        configuration.Children.Add(careerOptions);
        configuration.Children.Add(midSkillSection);
        cutOptions.Children.Add(SearchablePicker("当前角色",cutCharacterSearch,cutCharacter,cutCharacters));
        var validationName=Field("流程验证角色名称",cutName); validationName.IsVisible=false;
        cutOptions.Children.Add(validationName);
        void CharacterChanged() {
            validationName.IsVisible=CutCharacter?.Manual==true;
            if(CutCharacter is { Manual:false } selected)cutName.Text=selected.CharacterName;
            cutDiscardFound.IsEnabled=CutCharacter?.CutPreset!=true;
            if(CutCharacter?.CutPreset==true)cutDiscardFound.IsChecked=false;
            cutCharacterHint.Text=CutCharacter?.CutPreset==true ? "使用本角色的刷切事件预设。" : "使用通用状态检查进行流程验证。";
            if(this.standaloneCutProfiles)cutCharacterHint.Text+=" 请与游戏当前角色保持一致。";
        }
        cutCharacter.SelectionChanged += (_,_) => CharacterChanged();
        CharacterChanged();
        cutOptions.Children.Add(cutCharacterHint);
        cutOptions.Children.Add(Field("使用卡组",cutDeck));
        cutOptions.Children.Add(SearchablePicker("指定借卡",cutBorrowSearch,cutBorrow,cutBorrowChoices));
        var borrowRequirements=new Grid { ColumnDefinitions=new("*,*"), ColumnSpacing=10 };
        borrowRequirements.Children.Add(Field("最低等级（1～50）",cutBorrowLevel));
        var breakField=Field("最低突破",cutBorrowBreak); Grid.SetColumn(breakField,1); borrowRequirements.Children.Add(breakField);
        cutOptions.Children.Add(borrowRequirements);
        cutSupportProfile=CreateSupportProfile(true,cutBorrowChoices);
        cutOptions.Children.Add(new Expander{Name="PacerCutSupportProfileSection",Header="本次实际卡组（可选填写）",Content=cutSupportProfile,IsExpanded=false});
        void BorrowChanged()=>borrowRequirements.IsEnabled=(cutBorrow.SelectedItem as UmaSupportChoice)?.CardId>0;
        cutBorrow.SelectionChanged+=(_,_)=>BorrowChanged(); BorrowChanged();
        cutOptions.Children.Add(Label(this.standaloneCutProfiles ? "当前育成的配卡保持原样；后续重开按此配置选卡，找不到合格借卡时停止。" : "首局请先按这些设置配好；后续重开自动选回卡组和借卡，找不到合格借卡时停止。",12));
        cutOptions.Children.Add(Field("最多尝试局数",cutAttempts));
        var resources=new Grid { ColumnDefinitions=new("*,*"), ColumnSpacing=10 };
        resources.Children.Add(Field("TP 药最多用几瓶",cutItems));
        var carrotField=Field("萝卜最多消耗多少",cutCarrots); Grid.SetColumn(carrotField,1); resources.Children.Add(carrotField);
        cutOptions.Children.Add(resources);
        cutOptions.Children.Add(Label("优先能量饮料30；药用完后才用允许的萝卜。0 表示不使用，不充值。",12));
        cutOptions.Children.Add(cutConfirmed);
        cutOptions.Children.Add(Label(this.standaloneCutProfiles ? "当前局计入本轮第1次；初级12月下无切则重开，确认有切立即保留并停止。" : "首局由你配好；初级12月下无切则弃养重开，确认有切立即保留并停止。",12));
        var cutAdvanced=new StackPanel { Spacing=9, Margin=new Thickness(0,10,0,0) };
        cutAdvanced.Children.Add(Field("查切方式",cutCheckMode));
        cutAdvanced.Children.Add(Field("每几回合补查（0 关闭周期补查）",cutInterval));
        cutOptions.Children.Add(cutCharacterMiss);
        cutOptions.Children.Add(new TextBlock {Text="仅在已确认走完当前服装的专属小切事件后检查；有切保留，未知结果不弃养。通用切不触发提前重开。",TextWrapping=TextWrapping.Wrap});
        cutOptions.Children.Add(cutTrainingFailure);
        cutOptions.Children.Add(Label("出现明确训练失败事件时，处理选项后重开；计入本轮次数，沿用补给额度。",12));
        cutAdvanced.Children.Add(cutDebutRetry); cutAdvanced.Children.Add(cutRetain); cutAdvanced.Children.Add(cutDiscardFound);
        cutDiscardFound.IsCheckedChanged+=(_,_)=>ApplyModePresentation();
        if(pacerCenter) {
            var notifications=new Button {Content="前往系统设置 · 消息推送"};
            notifications.Click+=(_,_)=>MaaUmaSettingsNavigation.Open(this,"notifications");
            cutAdvanced.Children.Add(notifications);
            cutAdvanced.Children.Add(Label("消息推送在系统设置中统一管理。",12));
        } else if(this.standaloneCutProfiles) {
            cutAdvanced.Children.Add(Label("PushPlus token（可选，三组配置共用）"));
            cutAdvanced.Children.Add(cutPushToken);
            var pushActions=new WrapPanel();
            foreach(var button in new[]{pushSave,pushTest,pushClear}){button.Margin=new Thickness(0,0,8,6);pushActions.Children.Add(button);}
            cutAdvanced.Children.Add(pushActions);cutAdvanced.Children.Add(pushStatus);
            cutAdvanced.Children.Add(Label("直接填写并保存，无需令牌文件。只有点击测试按钮才会发送测试通知。",12));
            pushSave.Click+=async(_,_)=>await PushCommand("save");
            pushClear.Click+=async(_,_)=>await PushCommand("clear");
            pushTest.Click+=async(_,_)=>await PushCommand("test");
            cutPushToken.TextChanged+=(_,_)=>{
                if(!string.IsNullOrWhiteSpace(cutPushToken.Text)){pushStatus.Text="令牌尚未保存。";savePreset.Content="保存配置";}
            };
        } else cutAdvanced.Children.Add(PathField("PushPlus 令牌文件（可选）",cutTokenFile,false));
        if(!this.standaloneCutProfiles)cutAdvanced.Children.Add(PathField("复用目标和配卡资料（可选）",cutTemplate,true));
        cutOptions.Children.Add(new Expander { Header="查切、通知与记录设置", Content=cutAdvanced });
        aiStatus.RequestAsync=request=>bridge.Request(request);
        aiSection=new Expander {Name="PacerAiSection",Header="AI 决策 · 测试版",
            Content=new StackPanel {Spacing=16,Children={aiOptions,aiStatus}},IsVisible=false};
        cutOptions.Children.Add(aiSection);
        aiOptions.RefreshStatus=()=>bridge.Request(new JObject {["action"]="ai_schema"});
        aiOptions.ConfigureConnection=async request=> {
            var response=await bridge.Request(request);
            if(response["ai_status"] is JObject status)aiStatus.Render(status);
            else await aiStatus.RefreshAsync();
            return response;
        };
        aiOptions.Changed+=()=> {
            aiStatus.SetSelectedMode((string?)aiOptions.Read()["mode"]);
            if(!applying && (IsCut||pacerCenter) && string.IsNullOrEmpty((records.SelectedItem as Choice)?.Path))
            {cutDraft=Options();if(standaloneCutProfiles)savePreset.Content="保存配置";}
        };
        decisionPreferencesSection=new Expander {Name="PacerDecisionPreferences",Header="自定义取舍 · 测试版",
            Content=decisionPreferences,IsVisible=false};
        cutOptions.Children.Add(decisionPreferencesSection);
        decisionPreferences.Changed+=()=> {
            if(!applying && (IsCut||pacerCenter) && string.IsNullOrEmpty((records.SelectedItem as Choice)?.Path))
            {cutDraft=Options();if(standaloneCutProfiles)savePreset.Content="保存配置";}
        };
        configuration.Children.Add(cutOptions);
        continuationSection = new Expander { Name="PacerContinuationSection",
            Header="续养筛选 · 测试版", Content=continuation, IsVisible=false };
        configuration.Children.Add(continuationSection);
        continuation.EnabledChanged+=()=> {
            if(!applying&&continuation.EnabledForRun)unsubmittedContinuationDrafts.Remove((Mode,presetId));
            ApplyModePresentation();
        };
        var runtime = new StackPanel { Spacing = 16 };
        var runtimeHeading=new WrapPanel {Name="PacerRuntimeHeading",Orientation=Orientation.Horizontal};
        var runtimeTitle=Label("运行现场",18);
        runtimeTitle.Margin=new Thickness(0,0,10,5);
        runtimeTitle.VerticalAlignment=VerticalAlignment.Center;
        runtimeHeading.Children.Add(runtimeTitle);
        if(pacerCenter) {
            openGettingStarted.Margin=new Thickness(0,0,8,5);
            runtimeHeading.Children.Add(openGettingStarted);
            openGettingStarted.Click+=(_,_)=>GettingStartedRequested?.Invoke();
        }
        runtime.Children.Add(runtimeHeading);
        runtime.Children.Add(stateText);runtime.Children.Add(manualHandoffStatus);
        if(pacerCenter)runtime.Children.Add(BuildCaptureLatencyBar());
        if(pacerCenter)runtime.Children.Add(BuildCurrentPausePlan());
        runtime.Children.Add(career);
        if(!pacerCenter)cutRuntime.Children.Add(cutProgress);
        foreach(var label in new[] {cutTiming,cutCheck,cutResources,cutNotification})cutRuntime.Children.Add(label);
        runtime.Children.Add(cutRuntime);
        ssSieveRuntime.Name="PacerSsSieveRuntime";ssSieveRuntime.IsVisible=false;runtime.Children.Add(ssSieveRuntime);
        currentStep.Name="PacerCurrentStep";action.Name="PacerRuntimeExplanation";log.Name="PacerRuntimeDetails";
        date.Name="PacerCurrentTurnProgress";runtime.Children.Add(date);
        if(pacerCenter)runtime.Children.Add(BuildRealtimeLuckSummary());
        runtime.Children.Add(stats); runtime.Children.Add(currentStep);
        var stopReasonHeader=new WrapPanel {Name="PacerStopReasonHeader",Orientation=Orientation.Horizontal};
        var stopReasonTitle=Label("运行说明 / 停机原因",12);
        stopReasonTitle.Margin=new Thickness(0,0,10,5);
        stopReasonTitle.VerticalAlignment=VerticalAlignment.Center;
        stopReasonHeader.Children.Add(stopReasonTitle);
        if(pacerCenter) {
            submitStopReport.Click+=async(_,_)=>await SubmitStopReportAsync();
            exportStopDiagnostics.Bind(
                ()=>MaaUmaNativeStopDiagnosticsButton.Target(snapshot,currentRecord,Mode,
                    snapshot["endpoint"] is JValue {Type:JTokenType.String} exportEndpoint ? exportEndpoint.Value<string>()??"" : ""),
                request=>bridge.Request(request));
            exportStopDiagnostics.Margin=new Thickness(0,0,8,5);
            submitStopReport.Margin=new Thickness(0,0,8,5);
            stopReasonHeader.Children.Add(exportStopDiagnostics);
            stopReasonHeader.Children.Add(submitStopReport);
        }
        runtime.Children.Add(stopReasonHeader);
        runtime.Children.Add(action); runtime.Children.Add(lastSegmentText); runtime.Children.Add(timing);
        midSkillRuntimeStatus.Name="PacerMidSkillRuntimeStatus";midSkillRuntimeStatus.IsVisible=false;
        runtime.Children.Add(midSkillRuntimeStatus);
        continuationStatus.Name="PacerContinuationStatus";
        continuationStatus.IsVisible=false;
        runtime.Children.Add(continuationStatus);
        continuationResults = new Expander { Name="PacerContinuationResults", Header="续养节点结果",
            Content=continuationDetails, IsVisible=false };
        runtime.Children.Add(continuationResults);
        runtime.Children.Add(new Expander { Header = "决策与详细记录", Content = log });
        var openRecords = new Button { Content = "查看记录位置" };
        openRecords.Click += (_,_) => {
            var selected=(records.SelectedItem as Choice)?.Path;
            var folder=string.IsNullOrEmpty(selected) ? runRoot : selected;
            if(folder!=null)Process.Start(new ProcessStartInfo(folder) { UseShellExecute=true });
        };
        runtime.Children.Add(openRecords);
        configCard = pacerCenter ? CenterCard(configuration) : Card(configuration);
        runtimeCard = pacerCenter ? CenterCard(runtime) : Card(runtime);runtimeCard.Margin = new Thickness(16,0,0,0);
        body.Children.Add(configCard); Grid.SetColumn(runtimeCard,1); body.Children.Add(runtimeCard);
        var scroller = new ScrollViewer { Name="PacerPageScroller", Content = body, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
        Grid.SetRow(scroller,2); root.Children.Add(scroller);
        var bottom = new StackPanel { Name="PacerFooter",Margin = new Thickness(0,10,0,0),Spacing=5 };
        var buttons = footerButtons = new WrapPanel { Name="PacerFooterButtons",Orientation = Orientation.Horizontal,
            HorizontalAlignment=HorizontalAlignment.Stretch };
        if(pacerCenter){buttons.Children.Add(resumeWorkflowSnapshot);buttons.Children.Add(resumeRecord);buttons.Children.Add(continueFound);buttons.Children.Add(resumeManualHandoff);}
        buttons.Children.Add(start); buttons.Children.Add(pause); buttons.Children.Add(stop);
        resumeRecord.Click+=async(_,_)=>await ResumeSavedCareer();
        continueFound.Click+=async(_,_)=>await ContinueFoundCareer();
        resumeManualHandoff.Click+=async(_,_)=>await ResumeManualHandoff();
        resumeWorkflowSnapshot.Click+=async(_,_)=>await ResumeWorkflowSnapshot();
        newCareerAfterHandoff.IsCheckedChanged+=(_,_)=> {
            if(applying)return;
            if(newCareerAfterHandoff.IsChecked==true&&CanChooseNewCareer(context())) {
                newCareerHandoffRun=(string?)snapshot?["run_root"];
                newCareerHandoffEndpoint=(string?)snapshot?["endpoint"];
                hint.Text="已选择新建下一局；点击开始后使用当前配置，原局不会恢复。";
            } else {newCareerHandoffRun=null;newCareerHandoffEndpoint=null;}
            Render(context());
        };
        foreach(var button in buttons.Children.OfType<Button>())
        {
            button.Margin=new Thickness(0,0,8,5);
            button.VerticalAlignment=VerticalAlignment.Top;
            button.Height=36;
            button.MinHeight=36;
            button.Padding=new Thickness(12,5);
        }
        if(pacerCenter) {
            newCareerAfterHandoff.Content=Label("旧局已手动结束，按当前配置开始新一轮");
            bottom.Children.Add(newCareerAfterHandoff);
        }
        bottom.Children.Add(buttons);
        hint.MaxLines=2;
        hint.TextTrimming=TextTrimming.CharacterEllipsis;
        ToolTip.SetTip(hint,hint.Text);
        hint.PropertyChanged+=(_,change)=> {
            if(change.Property==TextBlock.TextProperty)ToolTip.SetTip(hint,hint.Text);
        };
        bottom.Children.Add(hint);
        bottom.Children.Add(Label("停止脚本会保留育成 · 切换页面后任务继续",12));
        Grid.SetRow(bottom,3); root.Children.Add(bottom); Content = root;
        start.Click += async (_,_) => await Submit("start");
        pause.Click += async (_,_) => await Submit("pause");
        stop.Click += async (_,_) => await Submit("stop");
        savePreset.Click+=async(_,_)=>await SavePreset();
        ConnectStrategyRevisionSelector();
        presetSlots.SelectionChanged+=async(_,_)=> {
            var selectedId=presetSlots.SelectedIndex+1;
            if(!applying && !presetBusy && selectedId is >=1 and <=3 && selectedId!=presetId)
                await SwitchPreset(selectedId);
        };
        records.SelectionChanged += (_,_) => {
            if (!applying && records.SelectedItem is Choice selected)
                ApplySettings(IsCut && string.IsNullOrEmpty(selected.Path) && cutDraft!=null ? cutDraft : selected.Settings);
        };
        mode.SelectionChanged += async (_,_) => {
            if(applying)return;
            if(pacerCenter)await SwitchMode(mode.SelectedIndex);
            else {ApplyModePresentation();await Refresh();}
        };
        cutConfirmed.IsCheckedChanged += (_,_) => { if(snapshot!=null)Render(context()); };
        ApplyModePresentation();
        if(this.standaloneCutProfiles)cutConfirmed.IsVisible=false;
        // Remember the new-batch form, including edits made while the first
        // background list request is still pending. Polling is not a reset.
        foreach(var input in new Control[] {cutCharacter,cutName,cutDeck,cutBorrow,cutBorrowLevel,cutBorrowBreak,
            cutAttempts,cutItems,cutCarrots,cutCheckMode,cutInterval,cutDebutRetry,cutCharacterMiss,cutRetain,cutDiscardFound,
            cutTokenFile,cutTemplate,cutConfirmed})
            input.PropertyChanged+=(_,change)=> {
                if(!applying && IsCut && string.IsNullOrEmpty((records.SelectedItem as Choice)?.Path)
                    && change.Property.Name is "Text" or "SelectedIndex" or "SelectedItem" or "IsChecked")
                { cutDraft=Options();if(standaloneCutProfiles)savePreset.Content="保存配置"; }
            };
        if(pacerCenter)foreach(var input in new Control[]{careerCharacter,careerCharacterName,careerDeck,careerBorrow,careerBorrowLevel,
            careerBorrowBreak,risk,mei,limit,retain,terminal,manualFirstArc,manualFinalArc}.Concat(targets.Values))
            input.PropertyChanged+=(_,change)=> {
                if(!applying&&!IsCut&&change.Property.Name is "Text" or "SelectedIndex" or "SelectedItem" or "IsChecked")savePreset.Content="保存配置";
            };
        midSkillLearning.SaveRequested=async()=> {
            await SavePreset();
            midSkillLearning.SetSavedMessage(commandError==null
                ? $"{(IsCut?"刷切筛选":"自动育成")} · 配置{presetId}已保存，下次开始生效；续跑沿用原局配置。"
                : "保存未完成："+commandError);
        };
        midSkillLearning.Changed+=()=> {
            if(applying)return;
            midSkillSummary.Text=IsCut?midSkillLearning.Summary:midSkillLearning.Summary+" · "+arcSkillPolicy.Summary;
            cutDraft=Options(); if(standaloneCutProfiles)savePreset.Content="保存配置";
        };
        arcSkillPolicy.Changed+=()=> {
            if(applying)return;
            midSkillSummary.Text=IsCut?midSkillLearning.Summary:midSkillLearning.Summary+" · "+arcSkillPolicy.Summary;
            cutDraft=Options(); if(standaloneCutProfiles)savePreset.Content="保存配置";
        };
        SizeChanged += (_,_) => Reflow();
        Loaded += async (_,_) => { timer.Start(); await Refresh();
            if(standaloneCutProfiles&&!pacerCenter)try {var info=await bridge.Request(new JObject {["action"]="pushplus_settings"});if(string.IsNullOrWhiteSpace(cutPushToken.Text))pushStatus.Text=(string?)info["message"] ?? "可直接填写推送令牌。";}
                catch {pushStatus.Text="推送设置暂未读取，可以重新填写后保存。";}
        };
        Unloaded += (_,_) => {if(!settingsHostedSeparately)timer.Stop();}; // Presentation only. No stop request.
        timer.Tick += RuntimeTimerTick;
    }

    /// <summary>Reparent the existing editors; their values and save/start handlers stay shared.</summary>
    public (Control Presets,Control Continuation,Control Weights,Control AI,Control MidSkills) SeparateSettingsPages()
    {
        if(!standaloneCutProfiles || settingsHostedSeparately)
            throw new InvalidOperationException("Settings pages are only available once for the standalone cut panel.");
        settingsHostedSeparately=true;
        configuration.Children.Remove(presetRow);
        configuration.Children.Remove(midSkillSection);
        midSkillSection.IsExpanded=true;midSkillOpen.IsVisible=true;
        cutOptions.Children.Remove(decisionPreferencesSection);
        configuration.Children.Remove(continuationSection);
        if(pacerCenter)cutOptions.Children.Remove(aiSection);
        if(pacerCenter)aiSection.IsExpanded=true;
        presetRow.IsEnabled=configuration.IsEnabled;
        decisionPreferencesSection.Header="自定义权重 · 测试版";
        continuationSection.Header="继续育成 · 测试版";
        decisionPreferencesSection.IsExpanded=continuationSection.IsExpanded=true;
        decisionPreferencesSection.HorizontalAlignment=continuationSection.HorizontalAlignment=HorizontalAlignment.Stretch;
        decisionPreferencesSection.HorizontalContentAlignment=continuationSection.HorizontalContentAlignment=HorizontalAlignment.Stretch;
        var presets=new Grid {Name="CutSharedPresetBar",ColumnDefinitions=new("Auto,*"),ColumnSpacing=12,
            MaxWidth=720,HorizontalAlignment=HorizontalAlignment.Left,Margin=new Thickness(12,0,12,6)};
        presetModeCaption.VerticalAlignment=VerticalAlignment.Center;presets.Children.Add(presetModeCaption);
        Grid.SetColumn(presetRow,1);presets.Children.Add(presetRow);
        if(pacerCenter) {
            var workflowPage=BuildWorkflowPage();
            workflowLibrary!.SetHostedMode(true);
            configuration.Children.Clear();configCard.IsVisible=false;
            realtimeLuckCard=CenterCard(BuildRealtimeLuckCard());
            realtimeLuckCard.Name="PacerRealtimeLuckCard";
            realtimeLuckCard.VerticalAlignment=VerticalAlignment.Top;
            body.Children.Add(realtimeLuckCard);
            body.Children.Remove(runtimeCard);
            runtimeCard.Margin=new Thickness(0);
            overviewPreflightCard=new PacerOverviewPreflightCard();
            overviewPreflightCard.OpenStrategyRequested+=()=>ConfigStrategyRequested?.Invoke();
            overviewPreflightCard.OpenEventsRequested+=()=>ConfigEventsRequested?.Invoke();
            overviewRightColumn=new StackPanel { Spacing=14, Children={ overviewPreflightCard, runtimeCard } };
            body.Children.Add(overviewRightColumn);
            workflowLibrary.SelectionChanged+=RefreshOverviewPreflightCard;
            workflowLibrary.PurityReportsChanged+=_=>RefreshOverviewPreflightCard();
            RefreshOverviewPreflightCard();
            Reflow();
            return (workflowLibrary!.SelectorBar,new Border(),workflowPage,
                workflowLibrary.CreateConfigurationPage("ai"),workflowLibrary.CreateConfigurationPage("mid_skills"));
        }
        return (presets,continuationSection,decisionPreferencesSection,aiSection,midSkillSection);
    }

    public void SetSettingsHostVisible(bool visible)
    {
        if(!settingsHostedSeparately)return;
        if(visible)timer.Start();else timer.Stop();
    }

    private sealed record Choice(string Path, string Label, JObject Settings, bool CanResume=true, string? ResumeReason=null, JObject? Cut=null)
    {
        public override string ToString() => Label;
    }
    private static TextBlock Label(string text, double size=14) => new() { Text=text, FontSize=size, TextWrapping=TextWrapping.Wrap };
    private static Border Card(Control content)
    {
        var card=new Border {Child=content,Padding=new Thickness(18),CornerRadius=new CornerRadius(10),BorderThickness=new Thickness(1)};
        card.Bind(Border.BorderBrushProperty,new DynamicResourceExtension("SukiControlBorderBrush"));return card;
    }
    private static GlassCard CenterCard(Control content)=>new(){Content=content,Padding=new Thickness(22),
        CornerRadius=new CornerRadius(14),IsAnimated=false,IsOpaque=true};
    private static StackPanel Field(string label, Control control)
    {
        var panel = new StackPanel { Spacing=5 }; panel.Children.Add(Label(label,12)); panel.Children.Add(control); return panel;
    }
    private sealed record PresetChoice(int Id,string Label,JObject Settings,bool DecisionPreferencesSaved=true)
    {
        public override string ToString()=>Label;
    }
    private Control SearchablePicker<T>(string label,TextBox query,ComboBox picker,IReadOnlyList<T> choices)
        where T:class,IUmaPortraitChoice
    {
        var controls=new StackPanel {Spacing=5};controls.Children.Add(query);controls.Children.Add(picker);
        var count=Label("",11);count.IsVisible=false;controls.Children.Add(count);
        query.PropertyChanged+=(_,change)=> {
            if(change.Property!=TextBox.TextProperty)return;
            var terms=(query.Text ?? "").Split((char[]?)null,StringSplitOptions.RemoveEmptyEntries|StringSplitOptions.TrimEntries);
            var matches=choices.Where(choice=> {
                var id=choice switch {UmaTraineeChoice trainee=>trainee.CardId,UmaSupportChoice card=>card.CardId.ToString(),_=>""};
                var text=choice.SearchText+" "+id;
                return terms.All(term=>text.Contains(term,StringComparison.OrdinalIgnoreCase));
            }).ToList();
            var selected=picker.SelectedItem as T;
            var shown=selected!=null && !matches.Contains(selected) ? new[] {selected}.Concat(matches).ToList() : matches;
            var previous=applying;applying=true;
            try {picker.ItemsSource=shown;picker.SelectedItem=selected;}
            finally {applying=previous;}
            count.IsVisible=terms.Length>0;
            count.Text=matches.Count==0 ? "没有匹配项；当前选择已保留" : $"匹配 {matches.Count} 项，展开下方列表选择";
        };
        return Field(label,controls);
    }
    private Control PathField(string label, TextBox text, bool folder)
    {
        var row=new DockPanel { LastChildFill=true };
        var browse=new Button { Content="选择", Margin=new Thickness(6,0,0,0) };
        DockPanel.SetDock(browse,Dock.Right); row.Children.Add(browse); row.Children.Add(text);
        browse.Click += async (_,_) => {
            var storage=TopLevel.GetTopLevel(this)?.StorageProvider;
            if(storage==null)return;
            if(folder) {
                var chosen=await storage.OpenFolderPickerAsync(new FolderPickerOpenOptions { Title=label, AllowMultiple=false });
                if(chosen.Count>0)text.Text=chosen[0].TryGetLocalPath();
            } else {
                var chosen=await storage.OpenFilePickerAsync(new FilePickerOpenOptions { Title=label, AllowMultiple=false });
                if(chosen.Count>0)text.Text=chosen[0].TryGetLocalPath();
            }
        };
        return Field(label,row);
    }
    private void ApplyModePresentation()
    {
        presetModeCaption.Text=pacerCenter?(IsCut?"刷切筛选 · 常用配置":"自动育成 · 常用配置"):"常用配置";
        careerOptions.IsVisible=!IsCut; cutOptions.IsVisible=IsCut; cutRuntime.IsVisible=IsCut;
        arcSkillSection.IsVisible=!IsCut;
        continuation.SetMode(IsCut);
        midSkillLearning.SetMode(IsCut,presetId);midSkillSummary.Text=IsCut?midSkillLearning.Summary:midSkillLearning.Summary+" · "+arcSkillPolicy.Summary;
        recordLabel.Text=IsCut ? "刷切批次" : "当前局记录";
        recordNote.Text=IsCut ? "新建批次从当前第一回合接管；已有中断批次可选择后续跑。" : "记录只建立本地档案；点击开始时根据现场继续育成或按配置开局。";
        if(standaloneCutProfiles)recordNote.Text=pacerCenter&&!IsCut ? "上方配置用于下一次开始；续跑原局沿用原记录，暂停后继续不更换配置。" : "切换配置会保存当前设置。开始新一轮重新计数，当前局算第1次；暂停后继续不重计。";
        subtitle.Text=IsCut ? cutDiscardFound.IsChecked==true ? "刷切耐久测试 · 有切通知后弃养继续" : "凯旋门刷切 · 12月下截止 · 有切保留停止" : "接管当前育成 / 按配置开局 · 凯旋门";
        if(workflowLibrary == null && continuationAvailable && continuation.EnabledForRun)
            subtitle.Text="自定义续养 · 初级12月下 → 继承后 → 首次远征前交接 · 有切保护不变";
        if(pacerCenter)subtitle.Text=(IsCut?"刷切筛选":"自动育成")+" · "+subtitle.Text;
    }
    private bool? lastShowSideLuck, lastLegacyNarrow;
    private void Reflow()
    {
        if(pacerCenter&&workflowLibrary!=null) {
            if(Bounds.Width<=0)return;
            var rawSideLuck=PacerLayoutGuard.NextNarrow(Bounds.Width,950,lastShowSideLuck.HasValue?!lastShowSideLuck.Value:null,24)==false;
            var showSideLuck=rawSideLuck&&realtimeLuckHasScore;
            if(lastShowSideLuck!=showSideLuck) {
                lastShowSideLuck=showSideLuck;
                body.ColumnDefinitions=new(showSideLuck?"356,*":"*");body.RowDefinitions=new("Auto");
                if(realtimeLuckCard!=null)realtimeLuckCard.IsVisible=showSideLuck;
                realtimeLuckSummary.IsVisible=realtimeLuckHasScore&&!showSideLuck;
                var rightHost=(Control?)overviewRightColumn??runtimeCard;
                Grid.SetColumn(rightHost,showSideLuck?1:0);Grid.SetRow(rightHost,0);
                rightHost.Margin=showSideLuck?new Thickness(16,0,0,0):new Thickness(0);
            }
            RenderRealtimeLuckMascot(showSideLuck);
            return;
        }
        if(Bounds.Width<=0)return;
        var narrow = PacerLayoutGuard.NextNarrow(Bounds.Width, 770, lastLegacyNarrow, 24);
        if(lastLegacyNarrow==narrow)return;
        lastLegacyNarrow=narrow;
        body.ColumnDefinitions = new(narrow ? "*" : "360,*"); body.RowDefinitions = new(narrow ? "Auto,Auto" : "*");
        Grid.SetRow(configCard,narrow&&pacerCenter?1:0);
        Grid.SetColumn(runtimeCard,narrow ? 0 : 1); Grid.SetRow(runtimeCard,narrow&&!pacerCenter?1:0);
        runtimeCard.Margin = narrow ? (pacerCenter?new Thickness(0,0,0,16):new Thickness(0,16,0,0)) : new Thickness(16,0,0,0);
    }
    private void ApplySettings(JObject value)
    {
        var previous=applying;applying=true;
        try { ApplySettingsCore(value); }
        finally { applying=previous; }
    }
    private void ApplySettingsCore(JObject value)
    {
        strategyFactoryPreferences=false;
        var policySettings=IsCut ? value["cut"] as JObject ?? value : value;
        midSkillLearning.Load(value,IsCut,presetId);arcSkillPolicy.Load(value["arc_policy"]);arcSkillSection.IsVisible=!IsCut;midSkillSummary.Text=IsCut?midSkillLearning.Summary:midSkillLearning.Summary+" · "+arcSkillPolicy.Summary;
        // Keep advanced settings; the selector changes only the next-run revision.
        loadedLarcPolicy=(value["larc_policy"] as JObject ?? policySettings["larc_policy"] as JObject)?.DeepClone() as JObject;
        LoadStrategyRevision(value);
        LoadSupportProfile(value);
        cutConfirmed.IsChecked=false;
        LoadContinuation((IsCut ? (value["cut"] as JObject ?? value) : value)["continuation"] as JObject);
        decisionPreferences.LoadForStrategy((value["cut"] as JObject ?? value)["decision_preferences"] as JObject
            ?? loadedLarcPolicy?["decision_preferences"] as JObject,StrategyPreferenceRevision);
        aiOptions.Load((value["cut"] as JObject ?? value)["ai_options"] as JObject);
        aiStatus.SetSelectedMode((string?)aiOptions.Read()["mode"]);
        continuation.SetMode(IsCut);
        if(!IsCut) {
            var handoff=value["manual_handoff"] as JObject;
            manualFirstArc.IsChecked=(bool?)handoff?["first_arc"]==true;
            manualFinalArc.IsChecked=(bool?)handoff?["final_arc"]==true;
            presetDates.Load(handoff?["dates"] as JArray);
        }
        if (value.Count == 0) { if(snapshot!=null)Render(context()); return; }
        if(IsCut) {
            cutCharacterSearch.Text="";cutBorrowSearch.Text="";
            var settings=value["cut"] as JObject ?? value;
            var id=(string?)settings["character_id"];
            cutCharacter.SelectedItem=cutCharacters.FirstOrDefault(choice=>choice.CardId==id) ?? cutCharacters.Last();
            cutName.Text=CutCharacter?.Manual==true ? (string?)settings["validation_character_name"] ?? "" : CutCharacter?.CharacterName ?? "";
            cutAttempts.Text=settings["max_attempts"]?.ToString() ?? "20";
            cutItems.Text=settings["max_tp_items"]?.ToString() ?? "0";
            cutCarrots.Text=settings["max_carrots"]?.ToString() ?? "0";
            cutCheckMode.SelectedIndex=(string?)settings["check_mode"]=="every_turn" ? 1 : 0;
            cutCharacterMiss.IsChecked=(bool?)settings["restart_after_character_cut_miss"]==true;
            cutTrainingFailure.IsChecked=(bool?)settings["restart_after_training_failure"]==true;
            cutInterval.Text=settings["fallback_interval_turns"]?.ToString() ?? "4";
            cutDebutRetry.IsChecked=(bool?)settings["debut_clock_retry"]!=false;
            cutRetain.IsChecked=(bool?)settings["retain_screenshots"]==true;
            cutDiscardFound.IsChecked=(bool?)settings["discard_found_for_validation"]==true && CutCharacter?.CutPreset!=true;
            cutTokenFile.Text=(string?)settings["pushplus_token_file"] ?? "";
            cutTemplate.Text=(string?)settings["template_root"] ?? "";
            cutDeck.SelectedIndex=Math.Clamp((int?)settings["support_deck"] ?? 0,0,10);
            var borrowId=(int?)settings["borrow_card_id"] ?? 0;
            cutBorrow.SelectedItem=cutBorrowChoices.FirstOrDefault(choice=>choice.CardId==borrowId) ?? cutBorrowChoices[0];
            cutBorrowLevel.Text=settings["borrow_min_level"]?.ToString() ?? "1";
            cutBorrowBreak.SelectedIndex=Math.Clamp((int?)settings["borrow_min_break"] ?? 0,0,4);
            if(snapshot!=null)Render(context());
            return;
        }
        foreach (var pair in targets) pair.Value.Text = value["targets"]?[pair.Key]?.ToString() ?? pair.Value.Text;
        risk.Text = value["failure_limit"]?.ToString() ?? "25";
        mei.Text = value["mei_breaks"]?.ToString() ?? "";
        limit.Text = value["max_actions"]?.ToString() ?? "72";
        retain.IsChecked = (bool?)value["retain_training"] == true;
        terminal.IsChecked = (bool?)value["terminal_skills"] != false;
        var setup=value["setup"] as JObject ?? new JObject();
        careerCharacter.SelectedItem=careerCharacters.FirstOrDefault(choice=>choice.CardId==(string?)setup["character_id"]) ?? careerCharacters[0];
        careerCharacterName.Text=CareerCharacter?.Manual==true ? (string?)setup["character_name"] ?? "" : CareerCharacter?.CharacterName ?? "";
        careerDeck.SelectedIndex=Math.Clamp((int?)setup["support_deck"] ?? 0,0,10);
        var careerBorrowId=(int?)setup["borrow_card_id"] ?? 0;
        careerBorrow.SelectedItem=careerBorrowChoices.FirstOrDefault(choice=>choice.CardId==careerBorrowId) ?? careerBorrowChoices[0];
        careerBorrowLevel.Text=setup["borrow_min_level"]?.ToString() ?? "1";
        careerBorrowBreak.SelectedIndex=Math.Clamp((int?)setup["borrow_min_break"] ?? 0,0,4);
    }
    private void LoadContinuation(JObject? value)
    {
        var key=(Mode,presetId);
        if(!restoringContinuationModeDraft&&unsubmittedContinuationDrafts.TryGetValue(key,out var draft)) {
            continuation.Load((JObject)draft.DeepClone());
            savedContinuationSettings.TryGetValue(key,out var saved);
            if(!continuation.EnabledForRun)continuation.ReadForSubmission(saved);
        } else continuation.Load(value);
    }
    private JObject Options()=>BuildOptions(forSubmission:false);
    private JObject SubmissionOptions()=>BuildOptions(forSubmission:true);
    private JObject BuildOptions(bool forSubmission)
    {
        if(forSubmission)CommitSupportProfileForSubmission();
        var value=OptionsCore();
        if(forSubmission)value["mid_skill_learning"]=midSkillLearning.Read();
        if(!IsCut)value["arc_policy"]=forSubmission?arcSkillPolicy.Read():arcSkillPolicy.ReadDraft();
        if((IsCut||pacerCenter) && decisionPreferencesAvailable)(IsCut?(JObject)value["cut"]!:value)["decision_preferences"]=decisionPreferences.Read();
        if((IsCut||pacerCenter) && aiAvailable)(IsCut?(JObject)value["cut"]!:value)["ai_options"]=aiOptions.Read();
        if(continuationAvailable) {
            var key=(Mode,presetId);
            JObject settings;
            if(forSubmission) {
                savedContinuationSettings.TryGetValue(key,out var saved);
                settings=continuation.ReadForSubmission(saved);
                if(continuation.HasUnsubmittedDraft)unsubmittedContinuationDrafts[key]=continuation.ReadDraft();
                else unsubmittedContinuationDrafts.Remove(key);
            } else {
                settings=continuation.ReadDraft();
                if(continuation.EnabledForRun)unsubmittedContinuationDrafts.Remove(key);
                else if(unsubmittedContinuationDrafts.ContainsKey(key))unsubmittedContinuationDrafts[key]=(JObject)settings.DeepClone();
            }
            if(IsCut) ((JObject)value["cut"]!)["continuation"]=settings;
            else value["continuation"]=settings;
        }
        return value;
    }
    private JObject OptionsCore()
    {
        if(IsCut)return WithLoadedLarcPolicy(new JObject { ["strategy"]="baseline",
            ["mid_skill_learning"]=midSkillLearning.ReadDraft(),["early_skill_allowlist"]=midSkillLearning.SkillValues,["cut"]=new JObject {
            ["character_id"]=CutCharacter?.CardId ?? "validation",
            ["validation_mode"]=CutCharacter?.CutPreset!=true, ["validation_character_name"]=cutName.Text,
            ["max_attempts"]=cutAttempts.Text, ["max_tp_items"]=cutItems.Text, ["max_carrots"]=cutCarrots.Text,
            ["check_mode"]=cutCheckMode.SelectedIndex==1 ? "every_turn" : "event_triggered", ["fallback_interval_turns"]=cutInterval.Text,
            ["debut_clock_retry"]=cutDebutRetry.IsChecked==true, ["retain_screenshots"]=cutRetain.IsChecked==true,
            ["restart_after_character_cut_miss"]=cutCharacterMiss.IsChecked==true,
            ["restart_after_training_failure"]=cutTrainingFailure.IsChecked==true,
            ["discard_found_for_validation"]=cutDiscardFound.IsChecked==true,
            ["support_deck"]=cutDeck.SelectedIndex, ["borrow_card_id"]=(cutBorrow.SelectedItem as UmaSupportChoice)?.CardId ?? 0,
            ["borrow_min_level"]=cutBorrowLevel.Text, ["borrow_min_break"]=cutBorrowBreak.SelectedIndex,
            ["pushplus_token_file"]=cutTokenFile.Text, ["template_root"]=cutTemplate.Text, ["setup_confirmed"]=cutConfirmed.IsChecked==true } });
        var stats = new JObject(); foreach (var pair in targets) stats[pair.Key] = pair.Value.Text ?? "";
        var options=new JObject { ["targets"]=stats, ["strategy"]="baseline", ["failure_limit"]=risk.Text,
            ["mei_breaks"]=mei.Text, ["max_actions"]=limit.Text, ["mid_skill_learning"]=midSkillLearning.ReadDraft(),
            ["retain_training"]=retain.IsChecked == true, ["terminal_skills"]=terminal.IsChecked == true,
            ["early_skill_allowlist"]=midSkillLearning.SkillValues, ["setup"]=new JObject {
                ["character_id"]=CareerCharacter?.CardId ?? "current", ["character_name"]=careerCharacterName.Text,
                ["support_deck"]=careerDeck.SelectedIndex,
                ["borrow_card_id"]=(careerBorrow.SelectedItem as UmaSupportChoice)?.CardId ?? 0,
                ["borrow_min_level"]=careerBorrowLevel.Text, ["borrow_min_break"]=careerBorrowBreak.SelectedIndex } };
        if(pacerCenter)options["manual_handoff"]=new JObject {
            ["first_arc"]=manualFirstArc.IsChecked==true,["final_arc"]=manualFinalArc.IsChecked==true,["dates"]=presetDates.Read() };
        return WithLoadedLarcPolicy(options);
    }

    private JObject WithLoadedLarcPolicy(JObject options)
    {
        // Deep clones isolate mode/slot drafts and backend requests from loaded settings.
        return WithDeclaredSupportProfile(ApplyStrategySelection(options));
    }

    private void LoadPresets(JObject payload,bool applySettings)
    {
        if(pacerCenter)currentRecord=payload["current_record"] as JObject;
        if(payload["metadata_warning"] is JValue warning && !string.IsNullOrWhiteSpace((string?)warning))
            throw new InvalidOperationException((string)warning!);
        var rows=(payload["slots"] as JArray ?? new()).OfType<JObject>()
            .Select(row=>new PresetChoice((int?)row["id"] ?? 0,(string?)row["label"] ?? "配置",
                row["settings"] as JObject ?? new(),(bool?)row["decision_preferences_saved"]!=false)).OrderBy(row=>row.Id).ToArray();
        if(rows.Length!=3 || rows.Select(row=>row.Id).Distinct().Count()!=3 || rows.Any(row=>row.Id is <1 or >3))
            throw new InvalidOperationException("配置未读完整，请确认已覆盖全部补丁文件。");
        foreach(var row in rows) {
            var policy=IsCut?(row.Settings["cut"] as JObject??row.Settings):row.Settings;
            savedContinuationSettings[(Mode,row.Id)]=policy["continuation"] is JObject saved?(JObject)saved.DeepClone():new JObject { ["schema_version"]="larc_continuation.v1",["enabled"]=false };
        }
        var selectedId=applySettings ? (int?)payload["selected_id"] ?? presetId : presetId;
        var selected=rows.SingleOrDefault(row=>row.Id==selectedId) ?? rows[0];
        var previous=applying;applying=true;
        try {
            presetSlots.ItemsSource=rows.Select(row=>row.Label).ToArray();presetSlots.SelectedIndex=selected.Id-1;presetId=selected.Id;
            if(applySettings){
                ApplySettings(selected.Settings);strategyFactoryPreferences=!selected.DecisionPreferencesSaved;
                decisionPreferences.SetStrategyContext(StrategyPreferenceRevision,factoryDefaults:strategyFactoryPreferences);
                cutDraft=Options();savePreset.Content="保存配置";
            }
        } finally {applying=previous;}
    }

    private async Task<JObject> PersistPreset()
    {
        var settings=SubmissionOptions();
        await SaveEnteredPushToken();
        return await bridge.Request(new JObject {["action"]=$"save_{PresetKind}_preset",["mode"]=Mode,["endpoint"]=context().Endpoint,
            ["slot_id"]=presetId,["settings"]=settings});
    }

    private async Task SaveEnteredPushToken()
    {
        if(pacerCenter || !standaloneCutProfiles || string.IsNullOrWhiteSpace(cutPushToken.Text))return;
        var entered=cutPushToken.Text ?? "";
        var result=await bridge.Request(new JObject {["action"]="save_pushplus_token",["token"]=entered.Trim()});
        if(cutPushToken.Text==entered){cutPushToken.Text="";pushStatus.Text=(string?)result["message"] ?? "令牌已保存并隐藏。";}
    }

    private async Task PushCommand(string command)
    {
        if(pushBusy || presetBusy || submitting)return;pushBusy=true;pushSave.IsEnabled=pushClear.IsEnabled=pushTest.IsEnabled=cutPushToken.IsEnabled=false;
        try {
            if(command=="clear") {
                var cleared=await bridge.Request(new JObject {["action"]="save_pushplus_token",["token"]=""});
                cutPushToken.Text="";pushStatus.Text=(string?)cleared["message"] ?? "推送已关闭。";
            } else {
                await SaveEnteredPushToken();
                if(command=="test") {
                    var result=await bridge.Request(new JObject {["action"]="test_pushplus_notification"});
                    pushStatus.Text=(string?)result["status"] switch {
                        "accepted"=>"推送服务已受理，请查看是否收到测试通知。",
                        "not_configured"=>"请先填写并保存 PushPlus token。",
                        "saved_token_unreadable"=>"本机无法读取已保存令牌，请重新填写。",
                        "rejected"=>"推送服务未接受请求，请核对 token 和推送账号设置。",
                        _=>"测试通知未确认发送，请检查网络后再试。"};
                }
            }
        } catch {pushStatus.Text="推送设置未完成，请稍后重试；令牌不会显示在错误信息中。";}
        finally {pushBusy=false;pushSave.IsEnabled=pushClear.IsEnabled=pushTest.IsEnabled=cutPushToken.IsEnabled=true;}
    }

    private async Task SavePreset()
    {
        if(!standaloneCutProfiles || presetBusy || submitting || pushBusy)return;
        presetBusy=true;
        try {
            if(snapshot!=null)Render(context());
            LoadPresets(await PersistPreset(),false);commandError=null;savePreset.Content="已保存";hint.Text=$"配置{presetId}已保存。"+continuation.DraftNotice;
        }
        catch(Exception error){commandError=error.Message;hint.Text=error.Message;}
        finally {
            presetBusy=false;
            // A presentation failure must not escape an async click or retain the save lock.
            try {if(snapshot!=null)Render(context());}
            catch(Exception error){commandError=error.Message;hint.Text=error.Message;}
        }
    }

    private async Task SwitchPreset(int selectedId)
    {
        if(!standaloneCutProfiles || presetBusy || submitting || pushBusy)return;
        if(Active.Contains((string?)snapshot?["control"]?["state"]??"idle")) {
            applying=true;presetSlots.SelectedIndex=presetId-1;applying=false;return;
        }
        presetBusy=true;if(snapshot!=null)Render(context());
        try {
            await PersistPreset();
            var response=await bridge.Request(new JObject {["action"]=$"select_{PresetKind}_preset",["mode"]=Mode,["endpoint"]=context().Endpoint,["slot_id"]=selectedId});
            LoadPresets(response,true);commandError=null;
        } catch(Exception error) {
            commandError=error.Message;
            applying=true;
            presetSlots.SelectedIndex=presetId-1;
            applying=false;
        } finally {presetBusy=false;if(snapshot!=null)Render(context());}
    }
    private async Task SwitchMode(int selectedIndex)
    {
        var previous=displayedModeIndex;
        applying=true;mode.SelectedIndex=previous;applying=false;
        if(selectedIndex==previous||selectedIndex is <0 or >1)return;
        if(presetBusy||submitting||pushBusy||snapshot==null||Active.Contains((string?)snapshot["control"]?["state"]??"idle")) {
            hint.Text="请先停止当前任务，再切换运行模式；暂停后继续仍沿用原局。";return;
        }
        var oldSlot=presetId;var oldSnapshot=snapshot;var oldRecord=currentRecord;
        var oldListedMode=listedMode;var oldListedEndpoint=listedEndpoint;
        var oldSlots=presetSlots.ItemsSource;var oldSelectedSlot=presetSlots.SelectedIndex;
        var sourceMode=Mode;var endpoint=context().Endpoint;
        ModeSwitchDraft leaving;
        try {leaving=CaptureModeDraft();}
        catch(Exception error){commandError=error.Message;hint.Text=error.Message;return;}
        modeSwitchDrafts[(sourceMode,oldSlot)]=leaving;
        presetBusy=true;Render(context());
        try {
            applying=true;mode.SelectedIndex=selectedIndex;applying=false;ApplyModePresentation();
            var targetMode=Mode;
            var result=await bridge.Request(new JObject {["action"]=endpointAllowed(endpoint)?"list":"configuration_snapshot",
                ["mode"]=targetMode,["endpoint"]=endpoint});
            var presets=result[$"{PresetKind}_presets"] as JObject??await bridge.Request(new JObject {
                ["action"]=$"{PresetKind}_presets",["mode"]=targetMode,["endpoint"]=endpoint});
            var nextSnapshot=result["snapshot"] as JObject??throw new InvalidOperationException("模式状态未读完整，尚未开始任务。");
            if(context().Endpoint!=endpoint)throw new InvalidOperationException("连接已变化，模式尚未切换，请重新操作。");
            if(Active.Contains((string?)nextSnapshot["control"]?["state"]??"idle"))
                throw new InvalidOperationException("后台任务尚未停止，模式未切换。");
            applying=true;
            try {
                LoadModeSwitchPresets(presets);
                if(modeSwitchDrafts.TryGetValue((targetMode,presetId),out var draft))RestoreModeDraft(draft);
                ApplyModePresentation();
            } finally {applying=false;}
            snapshot=nextSnapshot;displayedModeIndex=selectedIndex;listedMode=targetMode;listedEndpoint=endpoint;
            commandError=null;hint.Text="模式已切换；未完成表单仅保留在本次窗口，保存或开始时才校验。";
        }
        catch(Exception error) {
            applying=true;
            try {
                mode.SelectedIndex=previous;displayedModeIndex=previous;presetId=oldSlot;
                snapshot=oldSnapshot;currentRecord=oldRecord;listedMode=oldListedMode;listedEndpoint=oldListedEndpoint;
                presetSlots.ItemsSource=oldSlots;presetSlots.SelectedIndex=oldSelectedSlot;
                ApplyModePresentation();RestoreModeDraft(leaving);
            } finally {applying=false;}
            commandError=error.Message;hint.Text=error.Message;
        }
        finally {presetBusy=false;if(snapshot!=null)Render(context());}
    }

    private async Task ResumeSavedCareer()
    {
        if(!pacerCenter||IsCut||submitting||presetBusy||pushBusy||Active.Contains((string?)snapshot?["control"]?["state"]??"idle")
            ||ManualHandoffWaiting||ManualHandoffTerminal
            ||(bool?)currentRecord?["can_resume"]!=true||(string?)currentRecord?["path"] is not string path)return;
        submitting=true;Render(context());
        try {
            snapshot=await bridge.Request(new JObject {["action"]="start",["mode"]="career",["endpoint"]=context().Endpoint,
                ["request_id"]=Guid.NewGuid().ToString("N"),["run_root"]=path});
            runRoot=(string?)snapshot["run_root"];commandError=null;hint.Text="已续跑原记录；本局沿用原设置，上方配置未覆盖当前局。";
        } catch(Exception error){commandError=error.Message;hint.Text=error.Message;}
        finally{submitting=false;await Refresh();}
    }
    // MaaUma optional handoff display v1
    private JObject? ManualHandoff => snapshot?["manual_handoff"] as JObject;
    private bool IsSieveRun => (string?)snapshot?["mode"]=="cut" && snapshot?["workflow"] is JObject workflow
        && (string?)(workflow["document"] as JObject)?["template_id"] is "ss_sieve_cycle" or "ace_career_cycle"
        && (string?)snapshot?["run_root"] is string batch && string.Equals(batch,(string?)workflow["root"],StringComparison.OrdinalIgnoreCase);
    private string? SieveHandoffRun => (string?)(snapshot?["cut"] as JObject)?["current_attempt_root"] is string attempt
        && string.Equals(Path.GetFileName(attempt),(string?)ManualHandoff?["run_session_id"],StringComparison.OrdinalIgnoreCase)
        && string.Equals((string?)snapshot?["endpoint"],(string?)ManualHandoff?["device"],StringComparison.OrdinalIgnoreCase) ? attempt : null;
    private bool ManualHandoffWaiting => pacerCenter&&(!IsCut||IsSieveRun)&&(string?)ManualHandoff?["status"]=="waiting";
    private bool ManualHandoffTerminal => pacerCenter&&(!IsCut||IsSieveRun)&&((bool?)ManualHandoff?["terminal"]==true
        ||(string?)ManualHandoff?["status"]=="handed_off");
    private bool CanChooseNewCareer(UmaPacerContext current)
    {
        if(IsSieveRun||!(ManualHandoffWaiting||ManualHandoffTerminal)||submitting||presetBusy||pushBusy||current.DailyRunning
            ||(bool?)snapshot?["daily_pending"]==true||!endpointAllowed(current.Endpoint)
            ||string.IsNullOrWhiteSpace((string?)snapshot?["run_root"])
            ||!string.Equals((string?)snapshot?["endpoint"],current.Endpoint,StringComparison.OrdinalIgnoreCase))return false;
        var state=(string?)snapshot?["control"]?["state"];
        var occupant=snapshot?["device_owner"] as JObject;
        if(state=="paused")return (bool?)snapshot?["control"]?["tap_in_flight"]==false
            &&(occupant==null||(string?)occupant["owner"]=="pacer");
        return state is "idle" or "stopped" && (bool?)snapshot?["control"]?["tap_in_flight"]!=true && occupant==null;
    }
    private bool NewCareerSelected(UmaPacerContext current) => newCareerAfterHandoff.IsChecked==true
        &&CanChooseNewCareer(current)
        &&string.Equals(newCareerHandoffRun,(string?)snapshot?["run_root"],StringComparison.OrdinalIgnoreCase)
        &&string.Equals(newCareerHandoffEndpoint,current.Endpoint,StringComparison.OrdinalIgnoreCase);
    private bool CanResumeManualHandoff(UmaPacerContext current)
    {
        if(!ManualHandoffWaiting||ManualHandoffTerminal||NewCareerSelected(current)||submitting||presetBusy||pushBusy||current.DailyRunning
            ||(bool?)ManualHandoff?["can_resume"]!=true
            ||(string?)ManualHandoff?["stage"] is not ("first_arc" or "date")
            ||(string?)ManualHandoff?["stage"]=="date"&&string.IsNullOrWhiteSpace((string?)ManualHandoff?["pause_id"])
            ||(bool?)snapshot?["daily_pending"]==true||!endpointAllowed(current.Endpoint)
            ||!string.Equals((string?)snapshot?["endpoint"],current.Endpoint,StringComparison.OrdinalIgnoreCase)
            ||string.IsNullOrWhiteSpace((string?)snapshot?["run_root"]))return false;
        var state=(string?)snapshot?["control"]?["state"];
        if(state is not ("paused" or "idle" or "stopped"))return false;
        if(state=="paused" ? (bool?)snapshot?["control"]?["tap_in_flight"]!=false
                         : (bool?)snapshot?["control"]?["tap_in_flight"]==true)return false;
        var occupant=snapshot?["device_owner"] as JObject;
        if(occupant!=null&&(state!="paused"||(string?)occupant["owner"]!="pacer"))return false;
        if(IsSieveRun)return SieveHandoffRun!=null;
        if(currentRecord?["path"] is JValue path&&!string.Equals((string?)path,(string?)snapshot?["run_root"],StringComparison.OrdinalIgnoreCase))return false;
        return true;
    }
    private async Task EndPreviousHandoffTaskAsync()
    {
        var current=context();
        if(!NewCareerSelected(current))throw new InvalidOperationException("请先选择旧局已手动结束，并等待当前操作安全停下。");
        var previousRun=(string)snapshot!["run_root"]!;var previousEndpoint=current.Endpoint;
        if((string?)snapshot?["control"]?["state"] is "idle" or "stopped"
            &&(bool?)snapshot?["control"]?["tap_in_flight"]!=true&&snapshot?["device_owner"] is not JObject)return;
        var watch=System.Diagnostics.Stopwatch.StartNew();var budget=TimeSpan.FromSeconds(10);
        async Task<JObject> BeforeDeadline(JObject request){
            var remaining=budget-watch.Elapsed;
            if(remaining<=TimeSpan.Zero)throw new TimeoutException("旧局脚本仍在结束中，配置已保留；请等待连接释放后再开始。");
            return await bridge.Request(request).WaitAsync(remaining);
        }
        submitting=true;Render(current);
        hint.Text="正在结束旧局脚本；连接释放后再按当前配置开始新一轮。";
        try {
            snapshot=await BeforeDeadline(new JObject { ["action"]="end_manual_handoff_task",["mode"]="career",
                ["endpoint"]=previousEndpoint,["run_root"]=previousRun,["request_id"]=Guid.NewGuid().ToString("N") });
            while(true) {
                var latest=context();var state=(string?)snapshot?["control"]?["state"];
                if(!string.Equals(latest.Endpoint,previousEndpoint,StringComparison.OrdinalIgnoreCase)
                    ||!string.Equals((string?)snapshot?["endpoint"],previousEndpoint,StringComparison.OrdinalIgnoreCase)
                    ||!string.Equals((string?)snapshot?["run_root"],previousRun,StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("连接或当前局已变化，未启动新一轮；当前配置保留。");
                var occupant=snapshot?["device_owner"] as JObject;
                if(latest.DailyRunning||(bool?)snapshot?["daily_pending"]==true
                    ||occupant!=null&&(string?)occupant["owner"]!="pacer")
                    throw new InvalidOperationException("连接仍被其他任务使用，未启动新一轮；当前配置保留。");
                if(state is "idle" or "stopped" && (bool?)snapshot?["control"]?["tap_in_flight"]!=true&&occupant==null)return;
                if(state is "starting" or "running" or "resume_requested")
                    throw new InvalidOperationException("旧局尚未确认停止，未启动新一轮；当前配置保留。");
                if(watch.Elapsed>=budget)throw new TimeoutException("旧局脚本仍在结束中，配置已保留；请等待连接释放后再开始。");
                await Task.Delay(TimeSpan.FromMilliseconds(Math.Max(0,Math.Min(250,(budget-watch.Elapsed).TotalMilliseconds))));
                snapshot=await BeforeDeadline(new JObject { ["action"]="snapshot",["mode"]="career",["endpoint"]=previousEndpoint,["run_root"]=previousRun });
            }
        } finally {submitting=false;}
    }
    private async Task ResumeManualHandoff()
    {
        var current=context();
        if(!CanResumeManualHandoff(current))return;
        var originalRun=IsSieveRun?SieveHandoffRun:(string?)snapshot!["run_root"];
        submitting=true;Render(current);
        try {
            var request=new JObject { ["action"]="resume_manual_handoff",["mode"]="career",
                ["request_id"]=Guid.NewGuid().ToString("N"),["endpoint"]=current.Endpoint,["run_root"]=originalRun };
            if((string?)ManualHandoff?["pause_id"] is string pauseId)request["pause_id"]=pauseId;
            snapshot=await bridge.Request(request);
            runRoot=(string?)snapshot["run_root"];commandError=null;
            hint.Text="已确认继续原局；沿用本局原设置，重新读取当前页面。";
        }catch(Exception error){commandError=error.Message;hint.Text=error.Message;}
        finally{submitting=false;await Refresh();}
    }
    private async Task ContinueFoundCareer()
    {
        if(!pacerCenter||!IsCut||submitting||presetBusy||pushBusy||snapshot==null||
            Active.Contains((string?)snapshot["control"]?["state"]??"idle")||
            !((bool?)snapshot["can_continue_as_career"]??(bool?)currentRecord?["can_continue_as_career"]??false))return;
        submitting=true;Render(context());
        try {
            snapshot=await bridge.Request(new JObject {["action"]="continue_cut_as_career",["mode"]="cut",
                ["endpoint"]=context().Endpoint,["request_id"]=Guid.NewGuid().ToString("N")});
            runRoot=(string?)snapshot["run_root"];currentRecord=snapshot["current_record"] as JObject;
            commandError=null;ApplyReturnedMode(snapshot);Render(context());
            hint.Text="已转为自动育成，继续原局并沿用本局设置。";
        }catch(Exception error){commandError=error.Message;hint.Text=error.Message;}
        finally{submitting=false;await Refresh();}
    }
    public async Task Refresh()
    {
        if (refreshing || submitting || presetBusy) return;
        refreshing = true;
        try
        {
            // Legacy dedicated panels keep their target restriction. The
            // shared center can prepare application-local settings first.
            if (!standaloneCutProfiles && !endpointAllowed(context().Endpoint)) {
                start.IsEnabled = false;
                hint.Text = "请先在系统设置中选择模拟器，任务会沿用同一连接。";
                return;
            }
            if(!decisionPreferencesQueried) {
                decisionPreferencesQueried=true;
                try {var schema=await bridge.Request(new JObject {["action"]="decision_preferences_schema"});
                    decisionPreferencesAvailable=(bool?)schema["available"]==true;
                    if(decisionPreferencesAvailable)decisionPreferences.Configure(schema["tuning_schema"] as JObject,
                        value=>bridge.Request(new JObject {["action"]="validate_decision_preferences",["profile"]=value}),
                        value=>bridge.Request(new JObject {["action"]="preview_decision_preferences",["profile"]=value["profile"],["scenario"]=value["scenario"]}));}
                catch {decisionPreferencesAvailable=false;}
                decisionPreferencesSection.IsVisible=decisionPreferencesAvailable;
            }
            if(!aiQueried) {
                aiQueried=true;
                try {
                    var schema=await bridge.Request(new JObject {["action"]="ai_schema"});
                    aiAvailable=(bool?)schema["available"]==true;
                    aiOptions.SetProvider(schema["provider"] as JObject);
                } catch {aiAvailable=false;}
                aiSection.IsVisible=aiAvailable;
            }
            if(!continuationQueried) {
                continuationQueried=true;
                try {
                    var schema=await bridge.Request(new JObject {["action"]="continuation_schema"});
                    continuationAvailable=(bool?)schema["available"]==true;
                } catch { continuationAvailable=false; } // Older and stable backends keep the original page.
                continuationSection.IsVisible=continuationAvailable;
            }
            var current = context();
            var endpoint = current.Endpoint;
            ApplyIdleModeFromWorkflowLibrary();
            var requestedMode=Mode;
            if(standaloneCutProfiles && !endpointAllowed(endpoint))
            {
                var local=await bridge.Request(new JObject {["action"]="configuration_snapshot",["mode"]=requestedMode});
                if(submitting || Mode!=requestedMode)return;
                snapshot=local["snapshot"] as JObject??throw new InvalidOperationException("本地配置状态未读完整，请稍后重试。");
                if(listedMode!=requestedMode) {
                    var profiles=local[$"{PresetKind}_presets"] as JObject??throw new InvalidOperationException("本地配置未读完整，请稍后重试。");
                    LoadPresets(profiles,true);listedMode=requestedMode;
                }
                listedEndpoint="";
                Render(context());
                if(!Active.Contains((string?)snapshot["control"]?["state"]??"idle")&&commandError==null)
                    hint.Text="可以先编辑并保存配置；开始任务前，请在系统设置中选择模拟器连接。";
                return;
            }
            if (!endpointAllowed(endpoint))
            {
                start.IsEnabled = false; hint.Text="请先在系统设置中选择 MuMu 设备，随后回到育成中心。";
                // Continue polling an already running career even if selection changes.
                if (snapshot?["control"]?["state"]?.ToString() is not string prior || !Active.Contains(prior)) return;
                endpoint = (string?)snapshot?["endpoint"] ?? "";
            }
            if ((listedEndpoint != endpoint || listedMode != Mode) && !Active.Contains((string?)snapshot?["control"]?["state"] ?? "idle"))
            {
                var result = await bridge.Request(new JObject { ["action"]="list", ["endpoint"]=endpoint, ["mode"]=Mode });
                if(submitting || context().Endpoint!=endpoint || Mode!=requestedMode)return;
                var defaults=result["defaults"] as JObject ?? new();
                var choices = new List<Choice> { new("", IsCut ? "新建刷切批次（接管首局）" : "新建当前局记录（不是开新局）", defaults) };
                if(!standaloneCutProfiles)foreach (var row in (JArray)result["records"]!) choices.Add(new((string)row["path"]!, (string)row["label"]!, (JObject)row["settings"]!,
                    (bool?)row["can_resume"]!=false,(string?)row["resume_reason"],row["cut"] as JObject));
                var confirmed=cutConfirmed.IsChecked==true && (listedEndpoint=="" || listedEndpoint==endpoint);
                applying=true; records.ItemsSource=choices; records.SelectedIndex=0; applying=false;
                listedEndpoint=endpoint; listedMode=Mode; snapshot=(JObject)result["snapshot"]!;
                if(pacerCenter&&Active.Contains((string?)snapshot["control"]?["state"]??"idle")&&
                    (string?)snapshot["mode"] is "career" or "cut") {
                    applying=true;mode.SelectedIndex=(string?)snapshot["mode"]=="cut"?1:0;displayedModeIndex=mode.SelectedIndex;applying=false;
                    listedMode=Mode;ApplyModePresentation();
                }
                if(standaloneCutProfiles) {
                    var profiles=result[$"{PresetKind}_presets"] as JObject ?? await bridge.Request(new JObject {["action"]=$"{PresetKind}_presets",["mode"]=Mode,["endpoint"]=endpoint});
                    LoadPresets(profiles,true);
                } else if(IsCut) {
                    ApplySettings(cutDraft ?? defaults);
                    if(cutDraft!=null)cutConfirmed.IsChecked=confirmed;
                }
            }
            else {
                var result=await bridge.Request(new JObject { ["action"]="snapshot", ["endpoint"]=endpoint, ["mode"]=Mode,
                    ["run_root"]=string.IsNullOrEmpty((records.SelectedItem as Choice)?.Path) ? null : (records.SelectedItem as Choice)!.Path });
                if(submitting || context().Endpoint!=endpoint || Mode!=requestedMode)return;
                snapshot=result;
                if(pacerCenter&&result["current_record"] is JObject record)currentRecord=record;
            }
            Render(current);
        }
        catch (Exception error) { FreezeRuntimeClock(); hint.Text=error.Message; start.IsEnabled=false; }
        finally { refreshing=false; }
    }
    public async Task Submit(string command)
    {
        if (submitting || presetBusy || pushBusy) return;
        var freshCareerSelected=NewCareerSelected(context());
        if(pacerCenter&&workflowLibrary is {LibraryReady:false}&&command=="start"&&((string?)snapshot?["control"]?["state"]!="paused"||freshCareerSelected)) {
            hint.Text="模式和配置尚未读完整，请等待本地读取完成后开始；原局继续仍可使用原记录。";return;
        }
        if((command is "start" or "resume")&&(ManualHandoffWaiting||ManualHandoffTerminal)
            &&!(command=="start"&&freshCareerSelected)) {
            hint.Text=ManualHandoffWaiting
                ? (IsSieveRun?"请在运行总览确认本局人工交接后继续。":"请完成手动学技能后，点击“技能已学完，继续本局”。")
                : "本局已交还，不再恢复脚本。";
            return;
        }
        if(command=="start"&&freshCareerSelected) {
            try {await EndPreviousHandoffTaskAsync();}
            catch(Exception error){commandError=error.Message;hint.Text=error.Message;try{ToastHelper.Warn("育成启动失败",error.Message);}catch{}if(snapshot!=null)Render(context());return;}
        }
        if(pacerCenter&&workflowLibrary?.LibraryReady==true&&command=="start"&&
            ((string?)snapshot?["control"]?["state"]!="paused"||freshCareerSelected)) {
            try {await workflowLibrary.StartSelectedAsync();}
            catch(Exception error){commandError=error.Message;hint.Text=error.Message;try{ToastHelper.Warn("育成启动失败",error.Message);}catch{}}
            finally{if(!freshCareerSelected)await Refresh();else if(snapshot!=null)Render(context());}
            return;
        }
        submitting=true; start.IsEnabled=pause.IsEnabled=stop.IsEnabled=false;
        commandError=null;
        hint.Text=command=="start" ? "正在提交启动请求…" : "正在提交控制请求…";
        try
        {
            var current=context();
            var actualCommand=standaloneCutProfiles && command=="start" && !freshCareerSelected && (string?)snapshot?["control"]?["state"]=="paused" ? "resume" : command;
            var request=new JObject { ["action"]=actualCommand, ["request_id"]=Guid.NewGuid().ToString("N"), ["mode"]=Mode };
            if(actualCommand=="resume")request["endpoint"]=current.Endpoint;
            if (command=="start" && (string?)snapshot?["control"]?["state"] != "paused")
            {
                if(standaloneCutProfiles)LoadPresets(await PersistPreset(),false);
                request["endpoint"]=current.Endpoint; request["daily_running"]=current.DailyRunning;
                if(!standaloneCutProfiles)request["run_root"]=(records.SelectedItem as Choice)?.Path;
                request["options"]=SubmissionOptions();
                if(freshCareerSelected){request.Remove("run_root");request["new_career"]=true;}
                if(standaloneCutProfiles && IsCut) {
                    request["new_cycle"]=true;request["take_over_current"]=true;
                }
            }
            snapshot=await bridge.Request(request);
            runRoot=(string?)snapshot["run_root"];
            if (!standaloneCutProfiles && command=="start" && runRoot != null && (records.SelectedItem as Choice)?.Path != runRoot)
            {
                var choices=(records.ItemsSource as IEnumerable<Choice>)?.ToList() ?? new List<Choice>();
                var selected=choices.FirstOrDefault(choice=>choice.Path==runRoot);
                if (selected==null) { selected=new(runRoot,IsCut ? "当前刷切批次" : "当前育成记录",Options()); choices.Add(selected); }
                applying=true; records.ItemsSource=choices; records.SelectedItem=selected; applying=false;
            }
            Render(current); hint.Text="控制请求已发送；以上状态以后台实际响应为准。"+continuation.DraftNotice;
        }
        catch (Exception error) { commandError=error.Message;hint.Text=commandError;try{ToastHelper.Warn(command=="start" ? "育成启动失败" : "育成控制失败",commandError);}catch{} }
        finally { submitting=false; await Refresh(); }
    }
    private static string Duration(JToken? value)
    {
        // An explicit JSON null is a JValue, not a C# null reference.
        if(value?.Type is not (JTokenType.Integer or JTokenType.Float))return "—";
        var seconds=value.Value<double>();
        if(!double.IsFinite(seconds) || seconds>=TimeSpan.MaxValue.TotalSeconds)return "—";
        return TimeSpan.FromSeconds(Math.Max(0,seconds)).ToString(@"hh\:mm\:ss");
    }
    private static string Period(JToken? value)
    {
        if(value is not JObject)return value?.ToString() ?? "—";
        value=value["value"] ?? value;
        var year=(int?)value["year_index"];
        var label=year switch { 1=>"初级", 2=>"经典", 3=>"高级", _=>"" };
        if((bool?)value["pre_debut"]==true)return label+" · 出道前";
        return value["month"]==null ? "—" : $"{label} {value["month"]}月{((string?)value["half"]=="first" ? "上" : "下")}";
    }
    private void RenderContinuation(JObject? saved)
    {
        continuationStatus.IsVisible=continuationAvailable || saved!=null;
        continuationResults.IsVisible=saved!=null;
        if(saved==null) {continuationStatus.Text="续养节点：尚无本局记录";continuationDetails.Text="";return;}
        static string State(string? key)=>key switch {
            "passed"=>"已通过", "observed"=>"仅记录", "failed"=>"未通过", "unknown"=>"信息不足",
            "handoff"=>"等待号主接管", "user_continued"=>"人工放行", "historical_not_observed"=>"接管前节点未记录",
            _=>"尚未到达" };
        var lines=new List<string>();
        var nodes=saved["nodes"] as JObject ?? new();
        foreach(var pair in new[] {("junior","初级12月下"),("inheritance","第2年经典4月"),("pre_expedition","经典6月下"),("classic_arc","经典10月上"),("inheritance2","第3年高级4月"),("handoff","首次远征前")}) {
            var node=nodes[pair.Item1] as JObject;
            var result=node?["result"] as JObject;
            var status=(string?)(node?["flow_status"] ?? result?["status"]);
            lines.Add(pair.Item2+"："+State(status)+(node?["action"]?.ToString()=="user_continued" ? "（人工继续，保留原判断）":""));
        }
        continuationStatus.Text=string.Join("\n",lines);
        lines.Clear();
        foreach(var pair in new[] {("junior","初级12月下"),("inheritance","第2年经典4月"),("pre_expedition","经典6月下"),("classic_arc","经典10月上"),("inheritance2","第3年高级4月"),("handoff","远征前交接")}) {
            if(nodes[pair.Item1] is not JObject node)continue;
            var facts=node["facts"] as JObject ?? new();
            var result=node["result"] as JObject ?? new();
            lines.Add(pair.Item2+" · "+Period(node["period"]));
            if(pair.Item1=="classic_arc") {
                var ar=result["actual_rank"];
                var actualLabel=(ar!=null && ar.Type==JTokenType.Integer) ? $"第 {ar} 名" : "未知";
                lines.Add($"首次凯旋门名次：{actualLabel} / 要求 ≤ 第 {result["max_rank"] ?? 1} 名");
            }
            lines.Add($"适应性 Pt {facts["adaptation_points"] ?? "未知"} · 协助者积分 {facts["supporter_points"] ?? "未知"}");
            if(facts["stats"] is JObject values)lines.Add(string.Join(" · ",new[] {("speed","速"),("stamina","耐"),("power","力"),("guts","毅"),("wisdom","智")}.Select(p=>$"{p.Item2} {values[p.Item1] ?? "未知"}")));
            foreach(var row in (result["conditions"] as JArray ?? new()).OfType<JObject>()) {
                var condition=row["condition"] as JObject ?? new();
                var label=(string?)condition["kind"] switch {
                    "stat_sum_min"=>"五维总和", "stat_min"=>"单项属性", "adaptation_points_min"=>"适应性 Pt",
                    "supporter_points_min"=>"协助者积分", "bond_ready_min"=>"羁绊达线张数", "bond_card_min"=>"指定卡羁绊",
                    "friend_outing_unlocked"=>"友人外出", "chain_stage_min"=>"事件段数", "chain_complete"=>"事件完走",
                    "cut_event_completed"=>"切事件完走", "cut_present"=>"才能出众", "realtime_luck_min"=>"实时运气分", _=>"条件" };
                var actual=row["actual"];
                var text=actual==null || actual.Type==JTokenType.Null ? "未知" : actual.ToString(Newtonsoft.Json.Formatting.None);
                lines.Add($"{label} {condition["card_id"] ?? ""}：{((bool?)row["passed"] is bool pass ? pass ? "达标" : "未达标" : "未知")} · 当前 {text}"+(condition["minimum"]!=null ? $" / 最低 {condition["minimum"]}":""));
            }
            if(result["skill_matches"] is JArray matched)lines.Add("技能命中："+(matched.Count==0 ? "无已确认命中":string.Join("、",matched.Values<string>())));
            foreach(var row in (result["distances"] as JArray ?? new()).OfType<JObject>()) {
                var distName=(string?)row["distance"] switch {
                    "short"=>"短距离", "mile"=>"英里", "medium"=>"中距离", "middle"=>"中距离", "long"=>"长距离",
                    var raw=>raw ?? "距离"
                };
                lines.Add($"距离 {distName}：{row["actual"] ?? "未知"} / 要求 {row["required"]}");
            }
            foreach(var row in (facts["cut_events"] as JArray ?? new()).OfType<JObject>())
                lines.Add($"切事件 {row["kind"] ?? "未分类"}：{row["title"] ?? row["event_id"]} · {((bool?)row["completed"]==true ? "已走完":"结果未确认")}");
            lines.Add("");
        }
        continuationDetails.Text=string.Join("\n",lines);
    }

    private static string FormatContinuationNote(JObject? note)
    {
        if(note==null)return "";
        var kind=(string?)note["kind"];
        if(kind!="continuation_gate" && kind!="continuation_handoff")return "";
        var nodeLabel=(string?)note["node"] switch {
            "junior"=>"初级12月下", "inheritance"=>"第2年经典4月", "pre_expedition"=>"经典6月下",
            "classic_arc"=>"经典10月上", "inheritance2"=>"第3年高级4月", "handoff"=>"首次远征前",
            var raw=>raw ?? "续养检查点"
        };
        if(kind=="continuation_handoff")return $"已到达 {nodeLabel}，等待号主手动接管；确认继续后可点击恢复。";
        var result=note["result"] as JObject;
        if((string?)result?["status"]=="unknown")
            return $"在 {nodeLabel} 暂停：适性或面板信息读取不足（请在详情页确认后继续）";
        var summary=new List<string>();
        foreach(var row in (result?["conditions"] as JArray ?? new()).OfType<JObject>())
            if((bool?)row["passed"]==false)
                summary.Add((string?)row["condition"]?["kind"] switch {
                    "stat_sum_min"=>"五维总和", "stat_min"=>"单项属性", "adaptation_points_min"=>"适应性 Pt",
                    "supporter_points_min"=>"协助者积分", "bond_ready_min"=>"羁绊达线张数", "bond_card_min"=>"指定卡羁绊",
                    "friend_outing_unlocked"=>"友人外出", "chain_stage_min"=>"事件段数", "chain_complete"=>"事件完走",
                    "cut_event_completed"=>"切事件完走", "cut_present"=>"才能出众", "realtime_luck_min"=>"实时运气分",
                    var raw=>raw ?? "条件未达标"
                });
        foreach(var row in (result?["distances"] as JArray ?? new()).OfType<JObject>())
            if((bool?)row["passed"]==false) {
                var dist=(string?)row["distance"] switch {
                    "short"=>"短距离", "mile"=>"英里", "medium"=>"中距离", "middle"=>"中距离", "long"=>"长距离",
                    var raw=>raw ?? "距离"
                };
                summary.Add($"{dist} {row["actual"] ?? "未知"}/{row["required"]}");
            }
        var tail=summary.Count>0 ? $"（{string.Join("、",summary.Take(3))}）" : "";
        return $"在 {nodeLabel} 门禁暂停{tail}，请确认是否继续。";
    }

    private static string StopReportScalar(JToken? token,int limit=160)
    {
        if(token is not JValue value || value.Type is JTokenType.Null or JTokenType.Undefined)return "";
        var text=value.ToString();return text.Length<=limit ? text : text[..limit];
    }
    private static string StopReportField(JObject? value,string name,int limit=160)=>StopReportScalar(value?[name],limit);
    private static bool StopReportFlag(JObject? value,string name)=>value?[name] is JValue {Type:JTokenType.Boolean} flag && flag.Value<bool>();
    private static string StopReportArray(JToken? value,int count=8,int limit=80)=>value is JArray rows
        ? string.Join(", ",rows.Take(count).Select(row=>StopReportScalar(row,limit))) : "";
    private static string StopReportLevels(JToken? value)=>value is JArray rows
        ? string.Join(", ",rows.Take(20).Select(row=>row.Type is JTokenType.Integer or JTokenType.Float
            ? StopReportScalar(row,12) : "null")) : "";

    public string BuildStopReportDetails()
    {
        var control=snapshot?["control"] as JObject;
        var record=snapshot?["record"] as JObject;
        var stopContext=snapshot?["current_stop_context"] as JObject;
        var segment=snapshot?["last_segment_result"] as JObject;
        var stopPresentation=MaaUmaPacerStopReason.Resolve(snapshot?["cut"] as JObject,record,control,
            IsCut,Active.Contains(StopReportField(control,"state",80)),stopContext,segment);
        var entry=stopPresentation.CurrentFlow&&!stopPresentation.CurrentSegmentDiagnostics?null:record?["entry_diagnostics"] as JObject;
        var diagnostic=stopPresentation.CurrentFlow&&!stopPresentation.CurrentSegmentDiagnostics?null:record?["stop_diagnostics"] as JObject;
        var adaptation=diagnostic?["larc_adaptation"] as JObject;
        var notRecorded=StopReportFlag(diagnostic,"not_recorded");
        var priorTurn=StopReportFlag(diagnostic,"source_is_prior_turn");
        var details=new List<string> {
            "模式："+CurrentMode,
            "控制状态："+StopReportField(control,"state",80),
            "控制步骤："+StopReportField(control,"step",120),
            "停止原因："+stopPresentation.Reason,
            "异常类型："+stopPresentation.ErrorType,
            "异常信息："+stopPresentation.Error
        };
        details.Add("停止原因来源："+stopPresentation.Source);
        details.Add("当前异常归类："+(stopPresentation.ReportableFailure?"异常":"非异常 / 正常检查点或控制状态"));
        if(stopPresentation.Message.Length>0)details.Add("当前运行说明："+stopPresentation.Message);
        foreach(var name in new[]{"state","source","reason","message","step","pause_source","batch_id","attempt_id","batch_status","restart_pending","reportable_failure"}) {
            var value=StopReportField(stopContext,name,240);if(value.Length>0)details.Add("外层 "+name+"："+value);
        }
        var runtime=control?["runtime"] as JObject;
        foreach(var name in new[]{"step","note"}) { var value=StopReportField(runtime,name,240);if(value.Length>0)details.Add("外层运行 "+name+"："+value); }
        foreach(var section in new[]{"batch_stop","pending_action"})if(stopContext?[section] is JObject nested)
            foreach(var name in new[]{"state","reason","message","normal_stop"}) {var value=StopReportField(nested,name,240);if(value.Length>0)details.Add("外层 "+section+" "+name+"："+value);}
        if(segment!=null)foreach(var name in new[]{"run_id","segment_id","status","reason","message","is_current_attempt","is_normal_checkpoint"}) {
            var value=StopReportField(segment,name,240);if(value.Length>0)details.Add("最近育成段 "+name+"："+value);
        }
        if(stopPresentation.CurrentSegmentDiagnostics)details.Add("以下入口 / 停止诊断来自当前育成段，不覆盖外层暂停、资源或待恢复状态。");
        var resourceStatus=(stopContext?["batch_stop"] as JObject)?["resource_status"];
        if(resourceStatus is JObject resource)foreach(var name in new[]{"status","reason","message"}) {
            var value=StopReportField(resource,name,240);if(value.Length>0)details.Add("批次资源 "+name+"："+value);
        }
        else if(StopReportScalar(resourceStatus,240) is {Length:>0} resourceText)details.Add("批次资源："+resourceText);
        if(segment==null||StopReportFlag(segment,"is_current_attempt")) {
            if(record?["failure_source"] is JObject failure)foreach(var name in new[]{"type","code","reason","field","stage"}) {
                var value=StopReportField(failure,name,240);if(value.Length>0)details.Add("当前子局 failure_source "+name+"："+value);
            }
            else if(StopReportField(record,"failure_source",240) is {Length:>0} failureText)details.Add("当前子局 failure_source："+failureText);
        }
        if(stopPresentation.PreviousReason.Length>0)details.Add(MaaUmaPacerStopReason.PreviousText(stopPresentation)+"（原始："+stopPresentation.PreviousReason+"）");
        foreach(var name in new[]{"owner","state","entered","safe_reason","race_reason","cut_debut_race","ordinary_clock_retry"}) {
            var value=StopReportField(entry,name,100);
            if(value.Length>0)details.Add("入口识别 "+name+"："+value);
        }
        foreach(var name in new[]{"stage","reason","diagnostic_only","not_recorded","source_is_prior_turn","source_turn_index","stop_turn_index"}) {
            var value=StopReportField(diagnostic,name,80);
            if(value.Length>0)details.Add("停止诊断 "+name+"："+value);
        }
        if(notRecorded)details.Add("停止诊断：该次停止未保存完整读数。");
        if(priorTurn)details.Add("适应性读数来自此前回合，不代表该次停止时的状态。");
        var prefix=notRecorded || priorTurn ? "最近保存适应性（未确认等同停机现场） " : "本次适应性 ";
        foreach(var name in new[]{"points","pending_cost","selected_slot"}) {
            var value=StopReportField(adaptation,name,80);
            if(value.Length>0)details.Add(prefix+name+"："+value);
        }
        var errors=StopReportArray(adaptation?["errors"],6,80);
        var levels=StopReportLevels(adaptation?["levels"]);
        if(errors.Length>0)details.Add(prefix+"errors："+errors);
        if(levels.Length>0)details.Add(prefix+"levels："+levels);
        var providerError=StopReportField(diagnostic,"provider_error",240);
        if(providerError.Length>0)details.Add("底层 state_context_provider.error："+providerError);
        var observation=diagnostic?["training_page_observation"] as JObject;
        var observationErrors=StopReportArray(observation?["errors"],8,240);
        if(observationErrors.Length>0)details.Add("底层 training_page_observation.errors："+observationErrors);
        if(StopReportFlag(diagnostic,"pipeline_source_is_prior_turn"))details.Add("上述流水线诊断来自此前回合，不代表当前任务仍有同一故障。");
        var summary=log.Text??"";
        details.Add("已有运行摘要：\n"+(summary.Length<=1200?summary:summary[..1200]));
        return string.Join("\n",details);
    }

    public Window CreateStopReportWindow(MaaUmaRuntimeStopReportPanel? feedbackPanel=null)
    {
        var panel=feedbackPanel??new MaaUmaRuntimeStopReportPanel(AppContext.BaseDirectory,
            request=>bridge.Request(request));
        var target=MaaUmaNativeStopDiagnosticsButton.Target(snapshot,currentRecord,Mode,string.Empty);
        panel.SetReport(Mode,(string?)target["run_id"],"育成中心停止",BuildStopReportDetails());
        var window=new Window {Title="育成中心停止报告",Width=800,Height=780,MinWidth=560,MinHeight=460,
            WindowStartupLocation=WindowStartupLocation.CenterOwner};
        window.Bind(TemplatedControl.BackgroundProperty,new DynamicResourceExtension("SukiStrongBackground"));
        window.Bind(TemplatedControl.ForegroundProperty,new DynamicResourceExtension("SukiText"));
        var close=new Button {Content="关闭",Name="PacerCloseStopReport",HorizontalAlignment=HorizontalAlignment.Right};
        close.Click+=(_,_)=>window.Close();
        window.Closed+=(_,_)=>panel.Dispose();
        var body=new DockPanel {Margin=new Thickness(20)};
        DockPanel.SetDock(close,Dock.Bottom);body.Children.Add(close);
        body.Children.Add(new ScrollViewer {Content=new StackPanel {Spacing=14,Children={
            new TextBlock {Text="预览本次停止时已留档的游戏图；可取消附图后只提交脱敏文字。仅维护者可见，不含配置或凭据。",
                TextWrapping=TextWrapping.Wrap},panel}}});
        window.Content=body;
        return window;
    }

    public async Task<Window?> SubmitStopReportAsync(MaaUmaRuntimeStopReportPanel? feedbackPanel=null)
    {
        if(stopReportBusy || !stopReportAvailable || snapshot==null)return null;
        stopReportBusy=true;submitStopReport.IsEnabled=false;
        try {
            var panel=feedbackPanel??new MaaUmaRuntimeStopReportPanel(AppContext.BaseDirectory,
                request=>bridge.Request(request));
            var window=CreateStopReportWindow(panel);
            if(TopLevel.GetTopLevel(this) is Window owner && owner.IsVisible)window.Show(owner);
            else window.Show();
            // Local evidence/draft only. The window's explicit Submit button is the network action.
            await panel.LoadEvidenceAsync();
            return window;
        }
        catch {
            ToolTip.SetTip(submitStopReport,"报告窗口暂未准备好，可稍后重试。当前运行状态未改变。");
            return null;
        }
        finally {stopReportBusy=false;submitStopReport.IsEnabled=stopReportAvailable;}
    }

    private void Render(UmaPacerContext current)
    {
        if(pacerCenter)RenderRealtimeLuck(snapshot);
        if(pacerCenter)RenderCaptureLatency(current, snapshot);
        if (snapshot == null) return;
        var learning=snapshot["mid_skill_learning_status"] as JObject
            ??(snapshot["record"] as JObject)?["mid_skill_learning_status"] as JObject;
        var learningMessage=(string?)learning?["message"];
        midSkillRuntimeStatus.IsVisible=!string.IsNullOrWhiteSpace(learningMessage);
        midSkillRuntimeStatus.Text=midSkillRuntimeStatus.IsVisible
            ? ((string?)learning?["mode"]=="legacy_speed_startup"&&(string?)learning?["status"]!="disabled"
                ? "中途学习（本局沿用旧速度专项）：" : "中途学习：")+learningMessage
            : "";
        workflowLibrary?.UpdateRuntime(snapshot);
        var flowSnapshot = snapshot["workflow"] as JObject;
        var sieveCounts=flowSnapshot?["ss_sieve_counts"] as JObject;
        var aceCounts=flowSnapshot?["ace_cycle_counts"] as JObject;
        ssSieveRuntime.IsVisible=sieveCounts!=null||aceCounts!=null;
        ssSieveRuntime.Text=aceCounts!=null
            ? $"成品连养 · 毕业 {aceCounts["graduated"] ?? 0} · 未达标 {aceCounts["unqualified"] ?? 0} · 中途淘汰 {aceCounts["screened_out"] ?? 0} · 失败 {aceCounts["failed"] ?? 0}"
            : sieveCounts==null?"":$"SS筛局 · 淘汰 {sieveCounts["screened_out"] ?? 0} · 失败 {sieveCounts["failed"] ?? 0} · 成品 {sieveCounts["completed"] ?? 0}";
        workflowRoot = flowSnapshot?["state"] is JObject && snapshot["control"] is JObject ? (string?)flowSnapshot["root"] : null;
        var workflowStatus=(string?)flowSnapshot?["state"]?["status"];
        aiStatus.Render(snapshot["ai_status"] as JObject);
        aiStatus.SetSelectedMode((string?)aiOptions.Read()["mode"]);
        var control=snapshot["control"]!; var state=(string?)control["state"] ?? "idle";
        var active=Active.Contains(state); var occupant=snapshot["device_owner"] as JObject;
        if(active && snapshot["mode"] is JValue actual && (string?)actual is "career" or "cut") {
            var wasApplying=applying; applying=true;
            try { mode.SelectedIndex=(string?)actual=="cut" ? 1 : 0; displayedModeIndex=mode.SelectedIndex; }
            finally { applying=wasApplying; }
            ApplyModePresentation();
        }
        else if(!active) {
            ApplyIdleModeFromWorkflowLibrary();
        }
        cutRuntime.IsVisible=IsCut&&sieveCounts==null&&aceCounts==null;
        var busy=occupant != null && (!active || (string?)occupant["owner"] != "pacer");
        var pending=(bool?)snapshot["daily_pending"] == true;
        device.Text=fixedMode == null
            ? $"共用设备：{snapshot["endpoint"] ?? current.Endpoint} · {(current.Connected ? "连接检查通过" : "开始时检查连接")}"
            : $"固定设备：{current.Endpoint} · 独立测试后台（开始时检查连接）";
        owner.Text="占用：" + ((string?)occupant?["owner"] switch { "pacer"=>"育成助手", "umaa_daily"=>"日活任务", null=>"空闲", _=>"其他任务" });
        account.Text=(string?)snapshot["account"]?["label"] ?? "账号：未确认";
        stateText.Text=States.GetValueOrDefault(state,"状态待确认");
        var editable=!active && !submitting && !presetBusy;
        configuration.IsEnabled=presetRow.IsEnabled=editable;
        if(WorkflowConfigurationPage is Control workflowConfiguration)workflowConfiguration.IsEnabled=editable;
        if(WorkflowCustomRacesPage is Control workflowCustomRaces)workflowCustomRaces.IsEnabled=editable;
        if(workflowDeckPage is Control wd)wd.IsEnabled=editable;
        if(workflowStrategyTargetsPage is Control ws)ws.IsEnabled=editable;
        if(workflowCyclePage is Control wc)wc.IsEnabled=editable;
        midSkillSection.IsEnabled=editable;
        mode.IsEnabled=fixedMode==null&&editable;
        if(pacerCenter)stateText.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension(
            state=="running"?"SukiPrimaryColor":"SukiText"));
        var selected=records.SelectedItem as Choice;
        var selectedBatch=!standaloneCutProfiles && IsCut && !string.IsNullOrEmpty(selected?.Path);
        var newCutBatch=!standaloneCutProfiles && IsCut && !active && !selectedBatch;
        cutOptions.IsEnabled=!selectedBatch;
        continuationSection.IsEnabled=decisionPreferencesSection.IsEnabled=editable && !selectedBatch;
        // Runtime AI status is read-only; only next-run options keep the lock.
        aiSection.IsEnabled=true;
        aiOptions.IsEnabled=editable && !selectedBatch;
        if(IsCut && !standaloneCutProfiles)recordNote.Text=selectedBatch ? "续跑沿用原批次参数；要更换角色或资源额度，请选择新建批次。" : "新建批次从当前第一回合接管；已有中断批次可选择后续跑。";
        var cut=snapshot["cut"] as JObject;
        if(newCutBatch)cut=null;
        if(IsCut && !active && selectedBatch && selected!.Path!=(string?)snapshot["run_root"])cut=selected.Cut;
        var freshSelectedBatch=selectedBatch && selected!.Path==(string?)snapshot["run_root"];
        var canResume=(freshSelectedBatch ? (bool?)cut?["can_resume"] : null) ?? selected?.CanResume ?? true;
        var ready=workflowLibrary!=null?workflowLibrary.LibraryReady:(IsCut ? (standaloneCutProfiles ? CutCharacter!=null && (CutCharacter.Manual!=true || !string.IsNullOrWhiteSpace(cutName.Text)) : selectedBatch ? canResume : cutConfirmed.IsChecked==true)
            : CareerCharacter?.Manual!=true || !string.IsNullOrWhiteSpace(careerCharacterName.Text));
        start.Content=state=="paused" ? (IsCut ? "继续识别" : "继续育成") : IsCut ? (selectedBatch ? "续跑本批次" : "开始刷切") : "开始育成";
        if(standaloneCutProfiles)start.Content=state=="paused" ? (IsCut?"继续本轮":"继续当前局") : IsCut?"开始新一轮":"按当前配置开始";
        if(workflowLibrary?.LibraryReady==true&&state!="paused")start.Content="按所选配置开始新一轮";
        var workflowCanResumeToken = flowSnapshot?["can_resume"];
        var sameWorkflowCutBatch = workflowRoot != null && string.Equals(workflowRoot, (string?)snapshot["run_root"], StringComparison.OrdinalIgnoreCase);
        var workflowCanResume = workflowRoot != null
            && workflowStatus is not ("completed" or "preserved")
            && (workflowCanResumeToken?.Type == JTokenType.Boolean
                ? (bool)workflowCanResumeToken
                : !sameWorkflowCutBatch || (bool?)cut?["can_resume"] != false);
        var legacyCareerCanResume = workflowRoot == null && !IsCut && currentRecord?["path"]?.Type == JTokenType.String
            && (bool?)currentRecord?["can_resume"] == true;
        if(workflowLibrary!=null) {
            resumeRecord.IsVisible=false;
            var canUnifiedResume=!active&&!ManualHandoffWaiting&&!ManualHandoffTerminal&&(workflowCanResume||legacyCareerCanResume);
            resumeUsesLegacyRecord=!workflowCanResume&&legacyCareerCanResume;
            resumeWorkflowSnapshot.IsVisible=canUnifiedResume;
            var flowExecMode=(string?)flowSnapshot?["execution_mode"];
            var isCycleResume=workflowCanResume&&(sieveCounts!=null||aceCounts!=null||flowExecMode=="cut");
            resumeWorkflowSnapshot.Content=isCycleResume?"继续本轮":"继续原局";
            resumeWorkflowSnapshot.IsEnabled=canUnifiedResume&&editable&&!busy&&!pending&&!NewCareerSelected(current);
            ToolTip.SetTip(resumeWorkflowSnapshot,resumeUsesLegacyRecord
                ? (string?)currentRecord?["resume_reason"]??"续跑已保存的原局，不使用当前表单覆盖原设置。"
                : (string?)flowSnapshot?["resume_reason"]??(isCycleResume
                    ? "沿用上次停机前的本轮配置、计数与资源额度继续运行。"
                    : "沿用上次停机前的原局配置继续运行，不新开局。"));
        } else {
            resumeUsesLegacyRecord=false;
            resumeWorkflowSnapshot.IsVisible=workflowCanResume&&!active&&!ManualHandoffWaiting&&!ManualHandoffTerminal;
            resumeWorkflowSnapshot.Content="继续原工作流";
            resumeWorkflowSnapshot.IsEnabled=resumeWorkflowSnapshot.IsVisible&&editable&&!busy&&!pending&&!NewCareerSelected(current);
            ToolTip.SetTip(resumeWorkflowSnapshot,(string?)flowSnapshot?["resume_reason"]??"沿用原工作流配置、次数和资源额度继续运行。");
            resumeRecord.IsVisible=pacerCenter&&!IsCut&&!active&&currentRecord?["path"]?.Type==JTokenType.String;
            resumeRecord.IsEnabled=editable&&!busy&&!pending&&!ManualHandoffWaiting&&!ManualHandoffTerminal&&(bool?)currentRecord?["can_resume"]==true;
            ToolTip.SetTip(resumeRecord,(string?)currentRecord?["resume_reason"]??"续跑已保存的原局，不使用当前表单覆盖原设置。");
        }
        continueFound.IsVisible=pacerCenter&&IsCut&&!active&&
            ((bool?)snapshot["can_continue_as_career"]??(bool?)currentRecord?["can_continue_as_career"]??false);
        continueFound.IsEnabled=editable&&!busy&&!pending;
        ToolTip.SetTip(continueFound,(string?)snapshot["career_handoff_reason"]??(string?)currentRecord?["career_handoff_reason"]??
            "沿用当前已找到切的原局与设置继续育成，不新开局。");
        var dateHandoff=(string?)ManualHandoff?["stage"]=="date";
        var handoffManualSkills=(bool?)(ManualHandoff?["intent"] as JObject)?["first_arc_manual_skills"]
            ??(string?)ManualHandoff?["stage"]=="first_arc";
        resumeManualHandoff.IsVisible=ManualHandoffWaiting&&((string?)ManualHandoff?["stage"] is "first_arc" or "date")&&!ManualHandoffTerminal;
        resumeManualHandoff.Content=handoffManualSkills?"技能已学完，继续本局":"已处理完，继续本局";
        resumeManualHandoff.IsEnabled=CanResumeManualHandoff(current);
        if(!(ManualHandoffWaiting||ManualHandoffTerminal)
            ||!string.Equals(newCareerHandoffRun,(string?)snapshot["run_root"],StringComparison.OrdinalIgnoreCase)
            ||!string.Equals(newCareerHandoffEndpoint,current.Endpoint,StringComparison.OrdinalIgnoreCase)) {
            var wasApplying=applying;applying=true;
            try {newCareerAfterHandoff.IsChecked=false;newCareerHandoffRun=null;newCareerHandoffEndpoint=null;}
            finally {applying=wasApplying;}
        }
        newCareerAfterHandoff.IsVisible=!IsSieveRun&&(ManualHandoffWaiting||ManualHandoffTerminal);
        newCareerAfterHandoff.IsEnabled=CanChooseNewCareer(current);
        if(NewCareerSelected(current))start.Content="开始新育成";
        manualHandoffStatus.IsVisible=ManualHandoffWaiting||ManualHandoffTerminal;
        var handoffStopped=(state is "idle" or "stopped") && (bool?)control["tap_in_flight"]!=true;
        manualHandoffStatus.Text=ManualHandoffTerminal ? handoffStopped
            ? "末次凯旋门前已交还操作，本局脚本已结束，不会自动恢复。"
            : "末次凯旋门交接：等待后台确认停止，确认后再手动操作。" :
            resumeManualHandoff.IsEnabled ? handoffManualSkills?"首次凯旋门技能交接已停下。请在游戏中手动学技能，完成后点击“技能已学完，继续本局”。":"已到达日期停点。手动操作完成后，点击“已处理完，继续本局”。" :
            (string?)ManualHandoff?["stage"]=="final_arc" ? "末次凯旋门交接：等待后台确认停止。" :
            dateHandoff?"日期停点：等待后台确认安全暂停。":"首次凯旋门交接：等待后台确认安全暂停。";
        if(NewCareerSelected(current))manualHandoffStatus.Text="已选择新一轮；开始时先结束旧局脚本，确认连接释放后再按当前配置新建，不续跑旧局。";
        RenderManualPlan(current);
        pause.Content=IsCut ? "暂停查看" : "暂停";
        stop.Content=IsCut ? "停止并保留" : "停止脚本";
        var freshCareer=NewCareerSelected(current);
        start.IsEnabled=endpointAllowed(current.Endpoint) && !submitting && !presetBusy && !busy && !pending
            &&(!(ManualHandoffWaiting||ManualHandoffTerminal)||freshCareer)
            &&(freshCareer?ready&&!current.DailyRunning:state=="paused" || (ready && !current.DailyRunning && state is "idle" or "stopped"));
        if(start.IsEnabled){if(!start.Classes.Contains("Accent"))start.Classes.Add("Accent");start.FontWeight=FontWeight.SemiBold;}
        else{start.Classes.Remove("Accent");start.FontWeight=FontWeight.Normal;}
        pause.IsEnabled=!submitting && state=="running";
        stop.IsEnabled=!submitting && active && state!="stop_requested";
        PrimaryRunStateChanged?.Invoke(start.Content?.ToString()??"开始运行",start.IsEnabled,active);
        runRoot=(string?)snapshot["run_root"];
        if(pacerCenter)RenderOverviewTop(cut,sieveCounts,aceCounts,flowSnapshot?["state"]?["career_batch"] as JObject,active,workflowStatus);
        var viewingOldBatch=!standaloneCutProfiles && IsCut && !active && selectedBatch && selected!.Path!=(string?)snapshot["run_root"];
        var record=viewingOldBatch || newCutBatch ? new JObject() : snapshot["record"]!;
        var currentStopContext=viewingOldBatch||newCutBatch?null:snapshot["current_stop_context"] as JObject;
        var lastSegment=viewingOldBatch||newCutBatch?null:snapshot["last_segment_result"] as JObject;
        var stopPresentation=MaaUmaPacerStopReason.Resolve(cut,record as JObject,
            viewingOldBatch||newCutBatch?null:control as JObject,IsCut,active,currentStopContext,lastSegment);
        RenderContinuation(record["continuation"] as JObject);
        var runtime=viewingOldBatch || newCutBatch ? null : control["runtime"] as JObject;
        var activeNode=(string?)flowSnapshot?["state"]?["active_node"];
        var stage=WorkflowStage(flowSnapshot,activeNode,active,state=="running");
        career.IsVisible=!pacerCenter || stage!=null;
        career.Text=stage ?? (IsCut ? "刷切 · 凯旋门" : runRoot == null ? "等待读取当前育成" : "当前局 · 凯旋门");
        var before=record["last_turn"]?["before"] as JObject;
        date.Text=$"已推进 {record["actions_completed"] ?? "—"} 步 · 当前回合 {runtime?["turn"] ?? record["next_turn"] ?? "—"} · {Period(before?["period"])}";
        var currentStats=before?["current_stats"];
        stats.Text=string.Join("    ",new[] { ("speed","速度"), ("stamina","耐力"), ("power","力量"), ("guts","毅力"), ("wisdom","智力") }.Select(pair=>$"{pair.Item2} {currentStats?[pair.Item1] ?? "—"}"));
        action.Text=(string?)control["last_error"] ?? (string?)(control["last_result"] as JObject)?["reason"]
            ?? (string?)record["stop_reason"] ?? (state=="running" ? "当前任务正在运行。" : "等待本局运行记录");
        if(newCutBatch)action.Text="新批次待开始";
        var continuationPauseNote=FormatContinuationNote(runtime?["note"] as JObject);
        ObserveRuntimeClock(current,runtime);
        if(IsCut&&sieveCounts==null&&aceCounts==null) {
            var status=(string?)cut?["status"] ?? "ready";
            cutProgress.Text=$"第 {cut?["attempts"] ?? 0} / {cut?["max_attempts"] ?? cutAttempts.Text} 局 · 已完成 {cut?["completed_attempts"] ?? 0} 局";
            cutTiming.Text=$"已完成局平均 {Duration(cut?["avg_attempt_seconds"])} · 最近一局 {Duration(cut?["last_attempt_seconds"])}";
            cutCheck.Text=$"查切 {cut?["checks"] ?? 0} 次 · 最近结果：{cut?["last_check_result"] ?? "待检查"}";
            cutResources.Text=$"本批次用量（含未确认预留）：TP 药 {cut?["tp_items_reserved"] ?? 0} 瓶 / {cut?["max_tp_items"] ?? cutItems.Text} · 萝卜 {cut?["carrots_reserved"] ?? 0} / {cut?["max_carrots"] ?? cutCarrots.Text}";
            if(standaloneCutProfiles)cutResources.Text=cutResources.Text.Replace("本批次用量","本轮用量");
            cutNotification.Text=$"通知：{cut?["notification"] ?? "未发送"}";
            if(!active)stateText.Text=status=="unknown" && freshSelectedBatch && canResume
                ? "状态待重新检查 · 本局已保留" : CutStates.GetValueOrDefault(status,"状态待确认");
            if(standaloneCutProfiles && !active)stateText.Text=status=="route_failed" ? "本轮中断 · 再开始会重新计数" : stateText.Text?.Replace("本批次","本轮");
            if(runtime?["note"] is JValue note && !string.IsNullOrEmpty((string?)note))action.Text=(string)note!;
            else if(continuationPauseNote.Length>0)action.Text=continuationPauseNote;
            else if(!active && cut?["stop_reason"] is JValue why)action.Text=(string?)why ?? action.Text;
        }
        if(stopPresentation.CurrentFlow)action.Text=stopPresentation.Message.Length>0?stopPresentation.Message:stopPresentation.Reason;
        else if(!active && continuationPauseNote.Length>0)action.Text=continuationPauseNote;
        var previousText=MaaUmaPacerStopReason.PreviousText(stopPresentation);
        if(stopPresentation.PreviousReason.Length>0&&previousText=="上一育成段结果："+stopPresentation.PreviousReason) {
            var previousSummary=RuntimeText(stopPresentation.PreviousReason,step:true);
            lastSegmentText.Text=previousSummary.Length>0 ? "上一育成段结果："+previousSummary : "";
        }
        else lastSegmentText.Text=previousText;
        lastSegmentText.IsVisible=lastSegmentText.Text.Length>0;
        lastSegmentText.Bind(TextBlock.ForegroundProperty,new DynamicResourceExtension("SukiLowText"));
        if(!stopPresentation.CurrentFlow && record["error"] is JValue detail && !string.IsNullOrWhiteSpace((string?)detail))
            action.Text=$"{record["stop_reason"]}：{detail}";
        if(!stopPresentation.CurrentFlow && standaloneCutProfiles && (string?)record["stop_reason"]=="new_cycle_current_scene_unverified")
            action.Text="尚未识别到可接手的育成画面，本局已保留；请保持育成主页面稳定，无弹窗时可提交报告。";
        if(!stopPresentation.CurrentFlow && standaloneCutProfiles && (string?)record["stop_reason"]=="cut_character_mismatch")
            action.Text="游戏当前角色与配置不一致，已保留当前育成。请核对角色后再开始。";
        if(commandError!=null)action.Text=commandError;
        hint.Text=bridge.CloseError ?? commandError ?? (busy || pending || current.DailyRunning && !active ? "同设备仍有日活或未完成记录，已阻止新启动。" :
            IsCut ? state=="pause_requested" ? "等显示“已暂停”后再查看游戏；继续时会重新识别当前页面。" :
                selectedBatch && !canResume ? (freshSelectedBatch ? (string?)cut?["resume_reason"] : selected?.ResumeReason) ?? "这份记录不能续跑；已保留游戏现场。" :
                cutDiscardFound.IsChecked==true ? "耐久测试已开启：有切通知受理后弃养继续；手动停止仍保留当前局。" :
                "平时不保留截图；异常停机保留现场供复盘。有切或手动停止均保留当前局。" : "已有育成就继续；无育成按配置开局。暂停与停止由后台确认。");
        if(commandError==null && runtime?["runtime_license"] is JObject license &&
            (string?)license["status"] is "pending" or "checking" or "paused") {
            var licenseStatus=(string?)license["status"];
            var licenseMessage=(string?)license["message"];
            action.Text=string.IsNullOrWhiteSpace(licenseMessage) ? "暂时无法验证本机授权，当前育成已保留。" : licenseMessage;
            if(active)stateText.Text=licenseStatus=="paused" ? "授权验证未通过 · 已暂停并保留本局" : "正在验证本机授权";
            if(licenseStatus=="paused") {
                hint.Text=(string?)license["code"] is "activation_required" or "device_binding_required" or "device_changed" or
                    "device_already_bound" or "device_key_mismatch" or "access_revoked"
                    ? "请到“更新与服务”检查或激活本机授权；换机、重装或授权停用请联系维护者。处理后点继续，本局不会弃养。"
                    : "请检查服务连接，恢复后点继续重试授权；当前育成和进度已保留，也可停止并保留。";
            }
        }
        if(commandError==null && MaaUmaPacerRuntimeLicense.TryDescribeOffline(runtime?["runtime_license"],out var offlineLicenseMessage)) {
            action.Text=offlineLicenseMessage;
            if(active)stateText.Text="离线运行 · 授权宽限中";
            hint.Text="后台会自动重试授权；宽限到期或明确停用会暂停并保留本局。重开程序仍需联网验证。";
        }
        var originalAction=action.Text;
        var contextStep=StopReportField(currentStopContext,"step",120);
        var originalStep=viewingOldBatch || newCutBatch ? null : contextStep.Length>0?contextStep:(string?)control["step"] ?? (string?)runtime?["step"];
        var stepText=RuntimeText(originalStep,step:true);
        currentStep.IsVisible=stepText.Length>0;
        currentStep.Text=currentStep.IsVisible ? (active ? "当前步骤：" : "最后步骤：")+stepText : "";
        action.Text=RuntimeText(originalAction);
        log.Text=$"局号：{(runRoot==null ? "未开始" : System.IO.Path.GetFileName(runRoot))}\n{currentStep.Text}\n说明：{action.Text}\n"+
            $"原始流程节点：{activeNode ?? "—"}\n原始步骤：{originalStep ?? "—"}\n原始停止原因：{(stopPresentation.Reason.Length>0?stopPresentation.Reason:"—")}\n"+
            $"记录状态：{(stopPresentation.CurrentFlow?cut?["status"]:record["summary_status"]) ?? "—"}\n原始信息：{originalAction ?? "—"}";

        var reportRecord=record as JObject;
        var reportEntry=stopPresentation.CurrentFlow&&!stopPresentation.CurrentSegmentDiagnostics?null:reportRecord?["entry_diagnostics"] as JObject;
        if(reportEntry!=null)log.Text+="\n接手识别："+StopReportField(reportEntry,"safe_reason")+
            "\n比赛识别："+StopReportField(reportEntry,"race_reason");
        var reportStop=stopPresentation.CurrentFlow&&!stopPresentation.CurrentSegmentDiagnostics?null:reportRecord?["stop_diagnostics"] as JObject;
        if(StopReportFlag(reportStop,"not_recorded")) {
            action.Text+=" 该次停止未保存完整读数。";
            log.Text+="\n停止诊断：该次停止未保存完整读数。";
        }
        if(reportStop?["larc_adaptation"] is JObject reportAdaptation) {
            var priorTurn=StopReportFlag(reportStop,"source_is_prior_turn");
            var uncertain=priorTurn || StopReportFlag(reportStop,"not_recorded");
            log.Text+=uncertain ? "\n最近保存适应性细分（未确认等同停机现场）：" : "\n本次适应性细分：";
            log.Text+=StopReportArray(reportAdaptation["errors"],6,80);
        }
        stopReportAvailable=pacerCenter && !viewingOldBatch && !newCutBatch &&
            (stopPresentation.Reason.Length>0 || stopPresentation.PreviousReason.Length>0 || StopReportField(reportRecord,"error").Length>0 ||
             StopReportField(control as JObject,"last_error").Length>0);
        log.Text+="\n当前说明来源："+stopPresentation.Source+"\n当前异常："+(stopPresentation.ReportableFailure?"是":"否");
        if(stopPresentation.PreviousReason.Length>0)log.Text+="\n"+MaaUmaPacerStopReason.PreviousText(stopPresentation)+"\n上一育成段原始结果："+stopPresentation.PreviousReason;
        submitStopReport.IsEnabled=stopReportAvailable&&!stopReportBusy;
        exportStopDiagnostics.RefreshAvailability();
    }

    private void RenderOverviewTop(JObject? cut,JObject? sieveCounts,JObject? aceCounts,JObject? careerBatch,bool active,string? workflowStatus)
    {
        if(!IsCut && careerBatch?["max_careers"] is JValue {Type:JTokenType.Integer} batchLimit && (int)batchLimit>1) {
            var started=(int?)careerBatch["started"]??0;
            var completed=(int?)careerBatch["completed"]??0;
            var position=started>0?$"第 {started} / {batchLimit} 局":$"计划 {batchLimit} 局";
            overviewProgress.Text=$"普通育成 · {position} · 已完成 {completed} 局";
        }
        else if(IsCut&&cut?["attempts"] is JValue attempts&&cut["max_attempts"] is JValue maximum) {
            var result=aceCounts!=null?$"已毕业 {aceCounts["graduated"] ?? 0} 只":sieveCounts==null?$"已完成 {cut["completed_attempts"] ?? 0} 局":$"成品 {sieveCounts["completed"] ?? 0} 局";
            overviewProgress.Text=$"本轮第 {attempts} / {maximum} 局 · {result}";
        }
        else if(runRoot==null)overviewProgress.Text=active?"本轮 · 等待读取局数":"本轮 · 尚未开始";
        else if(IsCut)overviewProgress.Text="本轮 · 等待读取局数";
        else overviewProgress.Text=workflowStatus=="completed"?"普通育成 · 已完成本局":"普通育成 · 本轮 1 局";

        var settings=runRoot!=null&&string.Equals((string?)currentRecord?["path"],runRoot,StringComparison.OrdinalIgnoreCase)
            ? currentRecord?["settings"] as JObject : null;
        if(IsCut)settings=settings?["cut"] as JObject;
        overviewEquipment.IsVisible=settings!=null;
        if(settings!=null)overviewEquipment.Text="本局记录 · "+UmaWorkflowLibraryView.EquipmentText(settings,careerCharacters);
    }
}
