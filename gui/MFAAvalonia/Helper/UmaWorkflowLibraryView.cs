using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>The mode library is shared by the run overview and the editor; each configuration owns its values.</summary>
public sealed class UmaWorkflowLibraryView : StackPanel
{
    private readonly Func<JObject, Task<JObject>> request;
    private readonly Action<JObject> started;
    private readonly Func<Task<JObject>>? seedPolicies;
    private readonly UmaDecisionPreferences preferences;
    private readonly UmaCareerWorkflowEditor editor;
    private readonly ComboBox modes = new() { Name="WorkflowLibraryMode", MinWidth=190, Height=34, HorizontalAlignment=HorizontalAlignment.Stretch };
    private readonly ComboBox profiles = new() { Name="WorkflowLibraryConfiguration", MinWidth=190, Height=34, HorizontalAlignment=HorizontalAlignment.Stretch };
    private readonly TextBlock summary = new() { Name="WorkflowNextEquipment", FontSize=14, FontWeight=FontWeight.SemiBold,
        TextWrapping=TextWrapping.Wrap, MaxLines=2, TextTrimming=TextTrimming.CharacterEllipsis };
    private readonly TextBlock status = Note("正在读取本地模式与配置。");
    private readonly ContentControl page = new() { HorizontalContentAlignment=HorizontalAlignment.Stretch };
    private readonly WrapPanel tabs = new();
    private readonly StackPanel strategyPanel = new() { Spacing=10 };
    private readonly TextBlock weightsTitle = new() { Text="自定义权重", FontSize=20, IsVisible=false };
    private readonly TextBlock weightsHelp = Note("选择要调整的培养阶段，保存配置后用于下次开局。正在运行的局沿用原设置。");
    private readonly StackPanel advancedFlowPanel = new() { Name="WorkflowAdvancedFlowPage", Spacing=8 };
    private readonly ComboBox strategySteps = new() { Name="WorkflowStrategyStep", Height=34, HorizontalAlignment=HorizontalAlignment.Stretch };
    private readonly Control strategyStepField;
    private readonly TextBlock strategySummary = Note("");
    private readonly Dictionary<string,string> strategySelections = new();
    private readonly Dictionary<string,string> ordinarySelections = new();
    private JObject? newPolicyDefaultsByMode;
    private Func<bool>? commitOrdinary;
    private ContentControl? overviewSettings;
    private bool committingOrdinary;
    private bool loadingStrategySteps;
    private readonly TextBox profileName = new() { Name="WorkflowConfigurationName", Watermark="配置名称" };
    private readonly TextBox saveAsName = new() { Name="WorkflowSaveAsName", Watermark="留空自动生成副本名" };
    private readonly TextBox modeName = new() { Name="WorkflowModeName", Watermark="模式名称" };
    private readonly NumericUpDown careerBatchCount = new() { Name="WorkflowCareerBatchCount", Increment=1, FormatString="0" };
    private readonly StackPanel careerBatchSection = new() { Name="WorkflowCareerBatchSection", Spacing=4, IsVisible=false };
    private readonly TextBlock careerBatchHint = Note("");
    private readonly Control careerBatchField;
    private readonly Button resetCareerBatch;
    private readonly ContentControl configurationEditor = new() { Name="WorkflowConfigurationEditor", HorizontalContentAlignment=HorizontalAlignment.Stretch };
    private readonly TextBlock configurationStatus = Note("");
    private readonly List<Button> actions = new();
    private readonly Dictionary<string, JObject> drafts = new();
    private readonly Dictionary<string, string> profileNames = new();
    private readonly HashSet<string> dirty = new();
    private readonly Dictionary<string, JObject> buildDrafts = new();
    private readonly Dictionary<string, string> buildNames = new();
    private readonly HashSet<string> dirtyBuilds = new();
    private readonly Dictionary<string, JObject> purityUndoBuilds = new();
    private bool isV2;
    private string? buildId;
    private JObject? builtinTemplates;
    private bool v2BuiltinMode => isV2 && UmaWorkflowLibraryModel.IsBuiltinMode(modeId);
    private readonly List<(ContentControl Host,string Section)> settingsPages = new();
    private readonly Button save;
    private readonly Button discardChanges;
    private readonly Button saveOnConfigurationPage;
    private readonly Button createBlank;
    private readonly Button deleteProfile;
    private readonly CheckBox saveAsPurify = new() { Name="WorkflowSaveAsPurify", Content="另存副本时恢复官方纯净权重（清除自定义倍率和高级评分）", IsChecked=false };
    private readonly PacerPurityBadge purityBadge = new();
    private readonly Dictionary<string, JObject> purityUndo = new();
    private readonly Border saveStateBadge = new()
    {
        Name = "WorkflowSaveStateBadge",
        CornerRadius = new CornerRadius(12),
        BorderThickness = new Thickness(1),
        Padding = new Thickness(9, 4),
        VerticalAlignment = VerticalAlignment.Center
    };
    private readonly TextBlock saveStateBadgeText = new()
    {
        Text = "✓ 已保存",
        FontSize = 11,
        FontWeight = FontWeight.SemiBold
    };
    private readonly WrapPanel multiStepBar = new()
    {
        Name = "WorkflowMultiStepBar",
        Orientation = Orientation.Horizontal,
        VerticalAlignment = VerticalAlignment.Center,
        IsVisible = false
    };
    private readonly Dictionary<string, Func<bool>> activeSectionCommits = new();
    private PacerUiProvenanceStore? provenanceStore;
    private bool saveAsPurifyTouched;
    private bool suppressDirty;
    private bool hostedMode;
    private readonly Button showEquipmentButton;
    private JObject library = new();
    private JObject savedLibrary = new();
    private (int Minimum,int Maximum)? careerBatchLimits;
    private string? modeId, profileId;
    private bool loading, busy, initialized, customWeightsPage;
    private string selectedPage = "plan";
    private string? lastInitError;
    private DateTime lastInitAttemptUtc = DateTime.MinValue;
    private int autoRetryCount;
    private readonly Button retryInitButton;
    public bool LibraryReady => initialized;
    public Control SelectorBar { get; }
    public Control ConfigurationPage { get; }
    public event Action? SelectionChanged;
    public event Action? SkillLearningRequested;
    public event Action? RaceScheduleRequested;
    public event Action? OrdinarySettingsRequested;
    public event Action? ConfigurationRequested;
    public event Action? CustomWeightsRequested;
    public event Action<JObject?>? SavedEventPreferences;
    public event Action<string, IReadOnlyList<PurityReport>>? PurityDrawerRequested;
    public event Action<IReadOnlyList<PurityReport>>? PurityReportsChanged;
    public event Action<string, bool, string>? ToastRequested;
    public JObject SelectedDocument => ReadSelectedDocument();
    public string SelectedModeLabel => v2BuiltinMode ? UmaWorkflowLibraryModel.BuiltinModeName(modeId) : ((string?)Mode()?["name"] ?? "育成模式");
    public string SelectedConfigurationLabel => v2BuiltinMode ? ((string?)ActiveBuild()?["name"] ?? "培养方案") : ((string?)Profile()?["name"] ?? "运行配置");
    public string SelectedPrimaryMode => ResolvePrimaryMode(ReadSelectedDocument(), modeId, SelectedModeLabel);
    public string SelectionKey => v2BuiltinMode ? (buildId ?? "") + "@" + (modeId ?? "") : Key;
    public string? SelectedModeId => modeId;
    public string? SelectedProfileId => v2BuiltinMode ? buildId : profileId;
    public string? SelectedBuildId => v2BuiltinMode ? buildId : profileId;
    public void RefreshPurityStatus() => UpdateSummary();
    public (string? StepId, string? Revision) PrimaryStepContext
    {
        get
        {
            var doc = ReadSelectedDocument();
            var node = (doc["run_graph"]?["nodes"] as JArray ?? new()).OfType<JObject>()
                .FirstOrDefault(n => (string?)n["kind"] is "career" or "cut");
            if (node == null) return (null, null);
            var stepId = (string?)node["id"];
            var policy = doc["policies"]?[(string?)node["policy"] ?? "default"] as JObject;
            var optKey = (string?)node["kind"] == "cut" ? "cut_options" : "career_options";
            var rev = policy?[optKey]?["larc_policy"]?["revision"];
            return (stepId, rev?.Type == JTokenType.String ? (string?)rev : null);
        }
    }
    internal static string ResolvePrimaryMode(JObject? document, string? selectedModeId = null, string? selectedModeName = null)
    {
        var graph = document?["run_graph"] as JObject;
        var nodes = (graph?["nodes"] as JArray)?.OfType<JObject>().ToArray() ?? Array.Empty<JObject>();
        var edges = (graph?["edges"] as JArray)?.OfType<JObject>().ToArray() ?? Array.Empty<JObject>();
        var templateId = (string?)document?["template_id"] ?? selectedModeId ?? "";
        if (templateId is "ss_sieve_cycle" or "ace_career_cycle"
            || nodes.Any(n => n.Property("ss_sieve") != null || n.Property("ace_cycle") != null))
            return "cut";
        if (nodes.Length > 0)
        {
            var byId = new Dictionary<string, JObject>(StringComparer.Ordinal);
            foreach (var node in nodes)
                if ((string?)node["id"] is { Length: > 0 } id && !byId.ContainsKey(id))
                    byId[id] = node;
            var currentId = (string?)graph?["entry"];
            var visited = new HashSet<string>(StringComparer.Ordinal);
            while (currentId != null && byId.TryGetValue(currentId, out var step) && visited.Add(currentId))
            {
                var kind = (string?)step["kind"];
                if (kind is "career" or "cut") return kind;
                if (kind is not ("entry" or "continue_current")) break;
                currentId = edges.FirstOrDefault(e =>
                    (string?)e["from"] == currentId && (string?)e["outcome"] == "next")?["to"]?.Value<string>();
            }
            if (nodes.Any(n => (string?)n["kind"] is "cut" or "restart")) return "cut";
            var firstExec = nodes.FirstOrDefault(n => (string?)n["kind"] is "career" or "cut");
            if ((string?)firstExec?["kind"] is ("career" or "cut") and var firstKind) return firstKind;
        }
        if (templateId is "cut_preserve" or "cut_then_career" || templateId.StartsWith("cut", StringComparison.OrdinalIgnoreCase))
            return "cut";
        if (selectedModeName?.Contains("刷切", StringComparison.Ordinal) == true)
            return "cut";
        return "career";
    }
    private static JObject PrimaryOptions(JObject document)
    {
        var node=(document["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>().FirstOrDefault(n=>(string?)n["kind"] is "career" or "cut")
            ?? throw new InvalidOperationException("当前流程没有可编辑的育成步骤。");
        var policy=document["policies"]?[(string?)node["policy"]??"default"] as JObject
            ?? throw new InvalidOperationException("当前步骤的策略尚未读完整。");
        var key=(string?)node["kind"]=="cut"?"cut_options":"career_options";
        if(policy[key] is not JObject)policy[key]=new JObject();var options=(JObject)policy[key]!;
        if(key=="cut_options"){if(options["cut"] is not JObject)options["cut"]=new JObject();options=(JObject)options["cut"]!;}
        return options;
    }
    public JObject PrimaryDecisionPreferences => (JObject)(PrimaryOptions(ReadSelectedDocument())["decision_preferences"]?.DeepClone()??new JObject());
    public JObject PrimaryScoringProfile => preferences.ProfileFor(PrimaryDecisionPreferences);
    public void AttachProvenance(PacerUiProvenanceStore store)
    {
        provenanceStore=store;
        UpdateSummary();
    }
    private static JObject? SecondaryCareerOptions(JObject document)
    {
        var nodes=(document["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>().ToArray();
        var primary=nodes.FirstOrDefault(n=>(string?)n["kind"] is "career" or "cut");
        if((string?)primary?["kind"]!="cut")return null;
        var careerNode=nodes.FirstOrDefault(n=>(string?)n["kind"]=="career");
        if(careerNode==null)return null;
        var policy=document["policies"]?[(string?)careerNode["policy"]??"default"] as JObject;
        if(policy==null)return null;
        if(policy["career_options"] is not JObject)policy["career_options"]=new JObject();
        return (JObject)policy["career_options"]!;
    }
    public bool PrimarySupportsMeiGamble
    {
        get
        {
            var doc=ReadSelectedDocument();
            if(SelectedPrimaryMode=="career")return true;
            if(SelectedPrimaryMode!="cut")return false;
            var nodes=(doc["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>();
            return !nodes.Any(n=>(string?)n["kind"]=="cut"&&(bool?)n["screening"]?["enabled"]==true);
        }
    }
    public JObject? ReadPrimaryEventPreferences()
    {
        var doc=ReadSelectedDocument();
        var node=(doc["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>().FirstOrDefault(n=>(string?)n["kind"] is "career" or "cut");
        if(node==null)return null;
        var policy=doc["policies"]?[(string?)node["policy"]??"default"] as JObject;
        if((string?)node["kind"]=="cut")
            return (policy?["cut_options"]?["cut"]?["event_preferences"] as JObject ?? policy?["cut_options"]?["event_preferences"] as JObject)?.DeepClone() as JObject;
        return (policy?["career_options"]?["event_preferences"] as JObject)?.DeepClone() as JObject;
    }
    private static bool ShouldMirrorSecondary(JToken? secPrev, JToken? primPrev) =>
        secPrev==null || secPrev.Type==JTokenType.Null
        || (primPrev!=null && JToken.DeepEquals(secPrev,primPrev))
        || (secPrev is JObject secObj && secObj.Count==0);
    public void ApplyPrimaryEventPreferences(JObject? value)
    {
        if(!initialized||!HasValidSelection())return;
        var document=ReadSelectedDocument();
        var options=PrimaryOptions(document);
        var primPrev=options["event_preferences"]?.DeepClone();
        var secondary=SecondaryCareerOptions(document);
        var secPrev=secondary?["event_preferences"]?.DeepClone();
        var changed=false;
        if(value==null||value.Count==0) {
            if(options.Property("event_preferences")!=null) { options.Remove("event_preferences"); changed=true; }
        } else if(!JToken.DeepEquals(options["event_preferences"],value)) {
            options["event_preferences"]=value.DeepClone();
            changed=true;
        }
        if(secondary!=null && ShouldMirrorSecondary(secPrev,primPrev)) {
            if(value==null||value.Count==0) {
                if(secondary.Property("event_preferences")!=null) { secondary.Remove("event_preferences"); changed=true; }
            } else if(!JToken.DeepEquals(secondary["event_preferences"],value)) {
                secondary["event_preferences"]=value.DeepClone();
                changed=true;
            }
        }
        if(!changed)return;
        if(v2BuiltinMode) {
            var db=EnsureBuildDraft(buildId!);
            UmaWorkflowLibraryModel.CaptureV2(db,modeId!,document);
            document=UmaWorkflowLibraryModel.CompileV2(db,modeId!,builtinTemplates);
        } else {
            drafts[Key]=document;
        }
        var previous=loading;loading=true;
        try{editor.LoadDocument(document);}finally{loading=previous;}
        MarkDirty();
    }
    public void ApplyPrimaryDecisionPreferences(JObject value)
    {
        if(!initialized||!HasValidSelection())return;
        var document=ReadSelectedDocument();
        var options=PrimaryOptions(document);
        var primPrev=options["decision_preferences"]?.DeepClone();
        var secondary=SecondaryCareerOptions(document);
        var secPrev=secondary?["decision_preferences"]?.DeepClone();
        options["decision_preferences"]=value.DeepClone();
        if(secondary!=null && ShouldMirrorSecondary(secPrev,primPrev))
            secondary["decision_preferences"]=value.DeepClone();
        if(v2BuiltinMode) {
            var db=EnsureBuildDraft(buildId!);
            UmaWorkflowLibraryModel.CaptureV2(db,modeId!,document);
            document=UmaWorkflowLibraryModel.CompileV2(db,modeId!,builtinTemplates);
        } else {
            drafts[Key]=document;
        }
        var previous=loading;loading=true;
        try{editor.LoadDocument(document);}finally{loading=previous;}
        MarkDirty();
    }

    public UmaWorkflowLibraryView(Func<JObject,Task<JObject>> bridge, Action<JObject> onStarted,
        Func<Task<JObject>>? initialPolicies=null, UmaDecisionPreferences? strategy=null)
    {
        Name="UmaWorkflowLibraryView"; Spacing=12; request=bridge; started=onStarted; seedPolicies=initialPolicies;
        preferences=strategy ?? new UmaDecisionPreferences();
        editor=new UmaCareerWorkflowEditor(request,()=>Task.FromResult((JObject)(ReadSelectedDocument()["policies"]?.DeepClone() ?? new JObject())),
            started,preferences,preferences.Read,value=>preferences.Load(value)) { EmbeddedLibraryMode=true };
        editor.Changed+=()=> { if(loading)return; CaptureEditor(); MarkDirty(); if(selectedPage=="strategy")RefreshStrategySummary(); };
        editor.PlanReturnRequested+=()=>ShowPage("plan");
        strategySummary.Name="WorkflowSelectedStrategySummary";
        strategyStepField=Field("权重适用阶段",strategySteps);strategyStepField.Name="WorkflowStrategyStepField";
        strategyPanel.Children.Add(weightsTitle);
        weightsHelp.IsVisible=false;strategyPanel.Children.Add(weightsHelp);
        strategyPanel.Children.Add(strategyStepField);
        strategyPanel.Children.Add(strategySummary);
        strategySteps.SelectionChanged+=(_,_)=> {
            if(loadingStrategySteps||loading||busy||strategySteps.SelectedItem is not LibraryChoice choice)return;
            if(editor.SelectPolicy(choice.Id,showReturn:!customWeightsPage)){strategySelections[Key]=choice.Id;RefreshStrategySummary();}
        };
        var leftPicks = new WrapPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        Control CompactField(string label, Control input)
        {
            var cap = new TextBlock { Text = label, FontSize = 12, VerticalAlignment = VerticalAlignment.Center };
            cap.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
            return new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Spacing = 6,
                Margin = new Thickness(0, 2, 10, 2),
                VerticalAlignment = VerticalAlignment.Center,
                Children = { cap, input }
            };
        }
        profiles.Width = 176; profiles.MinWidth = 176; profiles.Height = 32; profiles.MinHeight = 0;
        modes.Width = 198; modes.MinWidth = 198; modes.Height = 32; modes.MinHeight = 0;
        leftPicks.Children.Add(CompactField("培养方案", profiles));
        leftPicks.Children.Add(CompactField("育成模式", modes));
        var manageBtn = ActionButton("WorkflowManageConfigurations", "⚙ 方案管理", () => { _ = OpenProfileManagerModalAsync(); });
        manageBtn.Height = 32; manageBtn.Padding = new Thickness(11, 0); manageBtn.Margin = new Thickness(0, 2, 6, 2);
        leftPicks.Children.Add(manageBtn);

        saveStateBadge.Child = saveStateBadgeText;
        saveStateBadge.Margin = new Thickness(0, 2, 6, 2);
        purityBadge.Margin = new Thickness(0, 2, 6, 2);
        save = ActionButton("WorkflowSaveConfiguration", "保存方案", SaveAsync);
        save.Height = 32; save.Padding = new Thickness(12, 0); save.Margin = new Thickness(0, 2, 0, 2);
        discardChanges = ActionButton("WorkflowDiscardChanges", "↶ 放弃修改", DiscardDraft);
        discardChanges.Height = 32; discardChanges.Padding = new Thickness(10, 0); discardChanges.Margin = new Thickness(0, 2, 6, 2);
        discardChanges.IsVisible = false;

        retryInitButton = new Button { Name = "WorkflowSelectorRetryInit", Content = "↻ 重试读取", Height = 32, Padding = new Thickness(10, 0), Margin = new Thickness(0, 2, 6, 2), IsVisible = false };
        retryInitButton.Click += async (_, _) => { if (busy) return; autoRetryCount = 0; await InitializeAsync(); };

        var rightPicks = new WrapPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        rightPicks.Children.Add(retryInitButton);
        rightPicks.Children.Add(purityBadge);
        rightPicks.Children.Add(saveStateBadge);
        rightPicks.Children.Add(discardChanges);
        rightPicks.Children.Add(save);

        var topRowGrid = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 10, RowSpacing = 6 };
        topRowGrid.Children.Add(leftPicks);
        Grid.SetColumn(rightPicks, 1);
        topRowGrid.Children.Add(rightPicks);
        bool? lastTopNarrow = null;
        topRowGrid.SizeChanged += (_, _) =>
        {
            var w = topRowGrid.Bounds.Width;
            if (w <= 0) return;
            var narrow = PacerLayoutGuard.NextNarrow(w, 920, lastTopNarrow, 28);
            if (lastTopNarrow == narrow) return;
            lastTopNarrow = narrow;
            topRowGrid.ColumnDefinitions = new(narrow ? "*" : "*,Auto");
            topRowGrid.RowDefinitions = new(narrow ? "Auto,Auto" : "Auto");
            Grid.SetColumn(rightPicks, narrow ? 0 : 1);
            Grid.SetRow(rightPicks, narrow ? 1 : 0);
        };

        summary.FontSize = 12;
        summary.FontWeight = FontWeight.Normal;
        summary.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var bottomRowGrid = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 10, RowSpacing = 4 };
        bottomRowGrid.Children.Add(summary);
        Grid.SetColumn(multiStepBar, 1);
        bottomRowGrid.Children.Add(multiStepBar);

        var barStack = new StackPanel
        {
            Spacing = 6,
            Children = { topRowGrid, bottomRowGrid }
        };
        SelectorBar = new GlassCard
        {
            Name = "WorkflowLibrarySelectorBar",
            Padding = new Thickness(14, 10),
            CornerRadius = new CornerRadius(12),
            IsAnimated = false,
            IsOpaque = true,
            Content = barStack
        };
        purityBadge.RestoreRequested += () => PurifyCurrentDraft(PurityLayer.All, recordUndo: true);
        purityBadge.UndoRequested += UndoPurifyDraft;
        purityBadge.DetailsRequested += () => { if (CustomWeightsRequested != null) CustomWeightsRequested.Invoke(); else OpenStrategy(); };
        purityBadge.DrawerRequested += () => RequestPurityDrawer();
        barStack.Loaded += async (_, _) => { if (!initialized && !busy) await InitializeAsync(); };
        tabs.Children.Add(ActionButton("WorkflowShowFlow","育成方案",()=>ShowPage("plan")));
        showEquipmentButton=ActionButton("WorkflowShowEquipment","常用设置",()=>ShowPage("equipment"));
        tabs.Children.Add(showEquipmentButton);
        tabs.Children.Add(ActionButton("WorkflowShowStrategy","分段策略",OpenStrategy));
        tabs.Children.Add(ActionButton("WorkflowShowAdvancedFlow","高级流程编辑",()=>ShowPage("advanced")));
        Children.Add(tabs);
        var advancedModeTools=new WrapPanel();
        advancedModeTools.Children.Add(ActionButton("WorkflowApplyCurrentMode","应用到当前模式（待保存）",SaveMode));
        advancedModeTools.Children.Add(ActionButton("WorkflowCopyAdvancedMode","另存为新模式",CopyMode));
        advancedModeTools.Children.Add(ActionButton("WorkflowRenameMode","修改模式名称（待保存）",()=> {
            if(Mode() is JObject m&&!string.IsNullOrWhiteSpace(modeName.Text)){m["name"]=modeName.Text.Trim();RefreshPickers();MarkDirty();}
        }));
        advancedFlowPanel.Children.Add(Field("工作流模式名称",modeName));
        advancedFlowPanel.Children.Add(advancedModeTools);
        advancedFlowPanel.Children.Add(Note("这里安排培养、条件与后续分支。应用或另存只更新模式草稿，最后请点顶部“保存配置”。"));
        saveOnConfigurationPage=ActionButton("WorkflowSaveFromConfigurationPage","保存当前配置",SaveAsync);
        var copy=ActionButton("WorkflowSaveAsConfiguration","另存为新配置",SaveAsAsync);
        copy.FontWeight=FontWeight.SemiBold;
        copy.Classes.Add("Accent");
        createBlank=ActionButton("WorkflowCreateBlankConfiguration","+ 新建空白纯净配置",CreateBlankConfigurationAsync);
        deleteProfile=ActionButton("WorkflowDeleteConfiguration","🗑 删除当前配置",DeleteSelectedProfileAsync);
        saveAsPurify.IsCheckedChanged+=(_,_)=>{if(!loading)saveAsPurifyTouched=true;};
        configurationStatus.Name="WorkflowConfigurationStatus";
        configurationStatus.Text=status.Text;
        status.PropertyChanged+=(_,change)=> {if(change.Property==TextBlock.TextProperty)configurationStatus.Text=status.Text;};
        var names=new WrapPanel {Children={Field("当前配置名称",profileName),Field("另存为名称",saveAsName)}};
        careerBatchField=Field("连续育成局数",careerBatchCount);
        resetCareerBatch=ActionButton("WorkflowResetCareerBatch","恢复单局育成",()=> {
            ApplyCareerBatchCount(1);RefreshCareerBatch(ReadSelectedDocument());
        });
        careerBatchSection.Children.Add(careerBatchField);
        careerBatchSection.Children.Add(careerBatchHint);
        careerBatchSection.Children.Add(resetCareerBatch);
        careerBatchSection.Children.Add(Note("此模式下后续各局沿用当前勾选配置的基本设置。"));
        careerBatchCount.ValueChanged+=(_,_)=> {
            if(loading||!careerBatchSection.IsVisible||!careerBatchField.IsVisible)return;
            if(TryReadCareerBatchCount(out var count))ApplyCareerBatchCount(count);
            else MarkDirty();
        };
        ConfigurationPage=new StackPanel {Name="WorkflowConfigurationPage",Spacing=10,Children={
            new TextBlock{Text="配置管理",FontSize=20,FontWeight=FontWeight.SemiBold},
            Note("保存角色、卡组、目标和分段策略。另存为会用当前编辑内容建立独立配置，正在育成的局沿用原设置。"),
            names,saveAsPurify,new WrapPanel{Children={saveOnConfigurationPage,copy,createBlank,deleteProfile}},configurationStatus,careerBatchSection,configurationEditor}};
        ConfigurationPage.Loaded+=async(_,_)=> {
            if(!initialized&&!busy)await InitializeAsync();
            else if(initialized&&!busy&&CommitOrdinary())RefreshConfigurationEditor();
        };
        profileName.PropertyChanged+=(_,change)=> {
            if(change.Property!=TextBox.TextProperty||loading||!initialized||!HasValidSelection())return;
            if(v2BuiltinMode) buildNames[buildId!]=profileName.Text??"";
            else profileNames[Key]=profileName.Text??"";
            MarkDirty();
        };
        Children.Add(page);status.Name="WorkflowLibraryStatus";Children.Add(status);
        modes.SelectionChanged+=(_,_)=> {
            if(loading||busy||modes.SelectedItem is not LibraryChoice c)return;
            if(c.Id==modeId||(c.Id=="cut_preserve"&&modeId=="cut_then_career"))return;
            if(!CommitOrdinary()){RefreshPickers();return;}
            CaptureEditor();
            modeId=c.Id;
            library["selected_mode_id"]=modeId;
            if(isV2) {
                if(!UmaWorkflowLibraryModel.IsBuiltinMode(modeId))
                    profileId=(string?)Mode()?["selected_profile_id"]??(string?)(Mode()?["profiles"] as JArray)?.OfType<JObject>().FirstOrDefault()?["id"];
            } else {
                profileId=(string?)Mode()?["selected_profile_id"];
            }
            LoadSelection();
        };
        profiles.SelectionChanged+=(_,_)=> {
            if(loading||busy||profiles.SelectedItem is not LibraryChoice c)return;
            if(v2BuiltinMode) {
                if(c.Id==buildId)return;
                if(!CommitOrdinary()){RefreshPickers();return;}
                CaptureEditor();
                buildId=c.Id;
                library["selected_build_id"]=buildId;
                LoadSelection();
                return;
            }
            if(c.Id==profileId)return;
            if(!CommitOrdinary()){RefreshPickers();return;}
            CaptureEditor();
            profileId=c.Id;
            if(Mode() is JObject m)m["selected_profile_id"]=profileId;
            LoadSelection();
        };
        Loaded+=async(_,_)=> {if(!initialized&&!busy)await InitializeAsync();};
    }

    private sealed record LibraryChoice(string Id,string Label) { public override string ToString()=>Label; }
    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private static Control Field(string label,Control input)=>new StackPanel {Spacing=5,MinWidth=190,Margin=new Thickness(0,0,12,8),Children={Note(label),input}};
    private Button ActionButton(string name,string label,Action action)
    { var b=new Button{Name=name,Content=label,Height=34,Padding=new Thickness(12,6),Margin=new Thickness(0,0,8,6)};b.Click+=(_,_)=> {try {if(!busy)action();}catch(Exception e){status.Text=e.Message;}};actions.Add(b);return b; }
    private Button ActionButton(string name,string label,Func<Task> action)
    {var b=new Button{Name=name,Content=label,Height=34,Padding=new Thickness(12,6),Margin=new Thickness(0,0,8,6)};b.Click+=async(_,_)=> {if(busy)return;try{await action();}catch(Exception e){status.Text=e.Message;}};actions.Add(b);return b;}
    private IEnumerable<JObject> Modes => ((isV2 ? library["custom_modes"] : library["modes"]) as JArray ?? new()).OfType<JObject>();
    private JObject? Mode() => Modes.FirstOrDefault(m => (string?)m["id"] == modeId);
    private JObject? Profile() => (Mode()?["profiles"] as JArray ?? new()).OfType<JObject>().FirstOrDefault(p => (string?)p["id"] == profileId);
    private IEnumerable<JObject> Builds => (library["builds"] as JArray ?? new()).OfType<JObject>();
    private JObject? Build(string? id = null) => Builds.FirstOrDefault(b => string.Equals((string?)b["id"], id ?? buildId, StringComparison.Ordinal));
    private JObject? SavedBuild(string? id = null) => (savedLibrary["builds"] as JArray ?? new()).OfType<JObject>().FirstOrDefault(b => string.Equals((string?)b["id"], id ?? buildId, StringComparison.Ordinal));
    private JObject? ActiveBuild(string? id = null)
    {
        var target = id ?? buildId;
        if (string.IsNullOrEmpty(target)) return null;
        return buildDrafts.TryGetValue(target, out var d) ? d : Build(target);
    }
    private JObject EnsureBuildDraft(string id)
    {
        if (buildDrafts.TryGetValue(id, out var existing)) return existing;
        var src = Build(id) ?? throw new InvalidOperationException("培养方案不存在：" + id);
        var clone = (JObject)src.DeepClone();
        buildDrafts[id] = clone;
        return clone;
    }
    private bool HasValidSelection() => v2BuiltinMode ? ActiveBuild() != null : Profile() != null;
    private bool HasLegacyGrowOverrides() =>
        v2BuiltinMode && modeId == "cut_then_career"
        && (ActiveBuild()?["mode_settings"]?["cut_then_career"]?["grow_overrides"] as JObject)?.Count > 0;
    private string Key => modeId + "/" + profileId;

    private JObject ResolveCurrentDocument()
    {
        if (v2BuiltinMode)
        {
            var b = ActiveBuild();
            return b != null ? UmaWorkflowLibraryModel.CompileV2(b, modeId!, builtinTemplates) : new JObject();
        }
        if (drafts.TryGetValue(Key, out var draft)) return (JObject)draft.DeepClone();
        return Mode() is JObject m && Profile() is JObject p ? UmaWorkflowLibraryModel.Resolve(m, p) : new JObject();
    }

    private void InitSelectionFromLoadedLibrary()
    {
        UmaWorkflowLibraryModel.MigrateLegacySsSieveIntoAce(library);
        isV2 = UmaWorkflowLibraryModel.IsV2(library);
        buildDrafts.Clear(); buildNames.Clear(); dirtyBuilds.Clear(); purityUndoBuilds.Clear();
        drafts.Clear(); profileNames.Clear(); dirty.Clear(); purityUndo.Clear();
        if (isV2)
        {
            buildId = (string?)library["selected_build_id"] ?? (string?)Builds.FirstOrDefault()?["id"];
            var savedModeId = (string?)library["selected_mode_id"];
            var canUseSaved = !string.IsNullOrWhiteSpace(savedModeId) && (
                StrategyPackCatalog.IsWorkflowModeUnlocked(savedModeId)
                || (StrategyPackCatalog.AllowCustomWorkflowModes
                    && !UmaWorkflowLibraryModel.IsBuiltinMode(savedModeId)
                    && Modes.Any(m => string.Equals((string?)m["id"], savedModeId, StringComparison.Ordinal))));
            modeId = canUseSaved ? savedModeId! : "direct_career";
            library["selected_mode_id"] = modeId;
            if (!UmaWorkflowLibraryModel.IsBuiltinMode(modeId))
                profileId = (string?)Mode()?["selected_profile_id"] ?? (string?)(Mode()?["profiles"] as JArray)?.OfType<JObject>().FirstOrDefault()?["id"];
            else
                profileId = buildId;
        }
        else
        {
            var visibleV1 = Modes.Where(m =>
            {
                var id = (string?)m["id"];
                return StrategyPackCatalog.IsWorkflowModeUnlocked(id)
                    || (StrategyPackCatalog.AllowCustomWorkflowModes && !UmaWorkflowLibraryModel.IsBuiltinMode(id));
            }).ToList();
            if (visibleV1.Count == 0) visibleV1 = Modes.ToList();
            var savedModeId = (string?)library["selected_mode_id"];
            var selectedMode = visibleV1.FirstOrDefault(m => string.Equals((string?)m["id"], savedModeId, StringComparison.Ordinal))
                ?? visibleV1.FirstOrDefault(m => string.Equals((string?)m["id"], "direct_career", StringComparison.Ordinal))
                ?? visibleV1.FirstOrDefault();
            modeId = (string?)selectedMode?["id"];
            profileId = (string?)Mode()?["selected_profile_id"] ?? (string?)(Mode()?["profiles"] as JArray)?.OfType<JObject>().FirstOrDefault()?["id"];
            buildId = null;
        }
    }

    public async Task InitializeAsync()
    {
        if(busy)return;
        lastInitAttemptUtc = DateTime.UtcNow;
        retryInitButton.IsVisible = false;
        SetBusy(true);
        try {
            var schema=await request(new JObject{["action"]="workflow_schema"});ConfigureSchema(schema);
            var useV2=(bool?)schema["v2_supported"]==true;
            var payload=new JObject{["action"]=useV2?"workflow_library_v2":"workflow_library"};
            JObject? seeded = null;
            if(seedPolicies!=null)
            {
                try { seeded = await seedPolicies(); }
                catch { seeded = null; }
            }
            if(seeded!=null)payload["initial_policies"]=seeded;
            JObject response;
            try
            {
                response=await request(payload);
            }
            catch when (seeded != null)
            {
                payload.Remove("initial_policies");
                response=await request(payload);
            }
            library=(JObject)(response["library"] as JObject??throw new InvalidOperationException("模式库未读完整，原配置未覆盖。")).DeepClone();
            if(response["builtin_templates"] is JObject btpls) builtinTemplates=(JObject)btpls.DeepClone();
            savedLibrary=(JObject)library.DeepClone();
            InitSelectionFromLoadedLibrary();
            try {
                var scoring=await request(new JObject{["action"]="decision_preferences_schema"});
                if(scoring["tuning_schema"] is JObject tuning)preferences.Configure(tuning,
                    value=>request(new JObject{["action"]="validate_decision_preferences",["profile"]=value}),
                    value=>request(new JObject{["action"]="preview_decision_preferences",["profile"]=value["profile"],["scenario"]=value["scenario"]}));
            }catch(Exception){status.Text="配置已读入；高级评分服务暂未连接。";}
            lastInitError = null;
            autoRetryCount = 0;
            retryInitButton.IsVisible = false;
            initialized=true;LoadSelection();
            if(response["warnings"] is JArray warnArr)
            {
                var warns = warnArr.Select(x => x?.ToString()).Where(s => !string.IsNullOrWhiteSpace(s)).ToArray();
                if(warns.Length > 0)
                {
                    var warnText = string.Join(" ", warns!);
                    status.Text = warnText;
                    ToastRequested?.Invoke(warnText, false, "warn");
                }
                else
                {
                    status.Text=isV2?"培养方案保管马娘、配卡与策略，切育成模式只切换对应要求与预算；旧配置文件已保留。":"模式决定流程，配置保存战马、配卡与分段策略。旧配置文件保留。";
                }
            }
            else
            {
                status.Text=isV2?"培养方案保管马娘、配卡与策略，切育成模式只切换对应要求与预算；旧配置文件已保留。":"模式决定流程，配置保存战马、配卡与分段策略。旧配置文件保留。";
            }
        }catch(Exception e){
            lastInitError = e.Message;
            status.Text="本地配置尚未就绪："+e.Message;
            summary.Text="本地配置尚未就绪："+e.Message;
            ToolTip.SetTip(summary, summary.Text);
            retryInitButton.IsVisible = true;
            foreach(var item in settingsPages) RefreshSettingsPage(item.Host, item.Section);
            SelectionChanged?.Invoke();
        }
        finally{
            SetBusy(false);
            retryInitButton.IsEnabled = !busy;
            retryInitButton.IsVisible = !initialized && !busy && !string.IsNullOrWhiteSpace(lastInitError);
        }
    }

    public void LoadLibrary(JObject value,JObject schema)
    {
        library=(JObject)value.DeepClone();
        savedLibrary=(JObject)library.DeepClone();
        ConfigureSchema(schema);
        InitSelectionFromLoadedLibrary();
        initialized=true;LoadSelection();
    }
    private void ConfigureSchema(JObject schema)
    {
        newPolicyDefaultsByMode=schema["new_larc_policy_defaults"] as JObject;
        if(schema["builtin_templates"] is JObject btpls)builtinTemplates=(JObject)btpls.DeepClone();
        careerBatchLimits=(bool?)schema["career_batch"]?["available"]==true
            && schema["career_batch"]?["max_careers"] is JArray {Count:2} limits
            && (int?)limits[0] is int minimum && (int?)limits[1] is int maximum && minimum<=maximum
            ?(minimum,maximum):null;
        editor.ConfigureSchema(schema);
    }
    private void RefreshPickers()
    {
        var previous=loading;loading=true;
        try {
            if(isV2) {
                var visibleBuiltins = UmaWorkflowLibraryModel.VisibleModes().Select(m => new LibraryChoice(m.Id, m.Name));
                var modeChoices = (StrategyPackCatalog.AllowCustomWorkflowModes
                    ? visibleBuiltins.Concat(Modes.Select(m => new LibraryChoice((string)m["id"]!, (string?)m["name"] ?? "自定义流程")))
                    : visibleBuiltins).ToArray();
                modes.ItemsSource=modeChoices;
                var pickerModeId=modeId=="cut_then_career"?"cut_preserve":modeId;
                modes.SelectedItem=modeChoices.FirstOrDefault(x=>x.Id==pickerModeId) ?? modeChoices.FirstOrDefault();
                if(v2BuiltinMode) {
                    var bs=Builds.Select(b=>new LibraryChoice((string)b["id"]!,(string?)b["name"]??"方案")).ToArray();
                    profiles.ItemsSource=bs;
                    profiles.SelectedItem=bs.FirstOrDefault(x=>x.Id==buildId);
                    profileName.Text=buildId!=null&&buildNames.TryGetValue(buildId,out var pendingBuild)?pendingBuild:(string?)ActiveBuild()?["name"];
                    modeName.Text=UmaWorkflowLibraryModel.BuiltinModeName(modeId);
                } else {
                    var ps=(Mode()?["profiles"] as JArray??new()).OfType<JObject>().Select(p=>new LibraryChoice((string)p["id"]!,(string?)p["name"]??"配置")).ToArray();
                    profiles.ItemsSource=ps;
                    profiles.SelectedItem=ps.FirstOrDefault(x=>x.Id==profileId);
                    profileName.Text=profileNames.TryGetValue(Key,out var pendingName)?pendingName:(string?)Profile()?["name"];
                    modeName.Text=(string?)Mode()?["name"];
                }
            } else {
                var visibleV1 = Modes.Where(m =>
                {
                    var id = (string?)m["id"];
                    return StrategyPackCatalog.IsWorkflowModeUnlocked(id)
                        || (StrategyPackCatalog.AllowCustomWorkflowModes && !UmaWorkflowLibraryModel.IsBuiltinMode(id));
                }).ToList();
                if(visibleV1.Count==0)visibleV1=Modes.ToList();
                var ms=visibleV1.Select(m=>new LibraryChoice((string)m["id"]!,(string?)m["name"]??"模式")).ToArray();modes.ItemsSource=ms;modes.SelectedItem=ms.FirstOrDefault(x=>x.Id==modeId)??ms.FirstOrDefault();
                var ps=(Mode()?["profiles"] as JArray??new()).OfType<JObject>().Select(p=>new LibraryChoice((string)p["id"]!,(string?)p["name"]??"配置")).ToArray();profiles.ItemsSource=ps;profiles.SelectedItem=ps.FirstOrDefault(x=>x.Id==profileId);
                profileName.Text=profileNames.TryGetValue(Key,out var pendingName)?pendingName:(string?)Profile()?["name"];
                modeName.Text=(string?)Mode()?["name"];
            }
        }finally{loading=previous;}
        UpdateSummary();
    }
    private void LoadSelection()
    {
        if(v2BuiltinMode) {
            if(ActiveBuild()==null)buildId=(string?)Builds.FirstOrDefault()?["id"];
            if(ActiveBuild()==null)return;
            library["selected_build_id"]=buildId;
            library["selected_mode_id"]=modeId;
        } else {
            var m=Mode();if(m==null)return;
            if(Profile()==null)profileId=(string?)(m["profiles"] as JArray)?.OfType<JObject>().FirstOrDefault()?["id"];
            var p=Profile();if(p==null)return;m["selected_profile_id"]=profileId;
        }
        saveAsPurifyTouched=false;
        suppressDirty=true;
        var previous=loading;loading=true;
        try {
            RefreshPickers();
            var value=ResolveCurrentDocument();
            editor.LoadDocument(value);
        }
        finally{loading=previous;}
        try {
            commitOrdinary=null;
            activeSectionCommits.Clear();
            ShowPage(selectedPage,commitPending:false);
            RefreshConfigurationEditor();
            foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
            UpdateSummary();
        }
        finally{suppressDirty=false;}
        SelectionChanged?.Invoke();
    }
    private void CaptureEditor()
    {
        if(!initialized||loading||suppressDirty||!HasValidSelection())return;
        var edited=editor.ReadDocument();
        if(v2BuiltinMode) {
            var curBuild=ActiveBuild()!;
            var baseline=UmaWorkflowLibraryModel.CompileV2(curBuild,modeId!,builtinTemplates);
            if(JToken.DeepEquals(baseline,edited))return;
            var draftBuild=EnsureBuildDraft(buildId!);
            UmaWorkflowLibraryModel.CaptureV2(draftBuild,modeId!,edited);
            return;
        }
        drafts[Key]=edited;
    }
    private bool IsActuallyDirty(string key)
    {
        if(v2BuiltinMode) {
            var targetId=buildId??"";
            var saved=SavedBuild(targetId);
            if(saved==null)return true;
            if(buildNames.TryGetValue(targetId,out var pendingBuildName)) {
                var savedName=(string?)saved["name"]??"";
                if(!string.Equals(pendingBuildName.Trim(),savedName.Trim(),StringComparison.Ordinal))
                    return true;
            }
            if(!buildDrafts.TryGetValue(targetId,out var draftBuild))return false;
            return !JToken.DeepEquals(draftBuild,saved);
        }
        if(profileNames.TryGetValue(key,out var pendingName))
        {
            var curName=(string?)Profile()?.GetValue("name")??"";
            if(!string.Equals(pendingName.Trim(),curName.Trim(),StringComparison.Ordinal))
                return true;
        }
        if(!drafts.TryGetValue(key,out var draft))return false;
        var parts=key.Split('/',2);
        var sm=((isV2?savedLibrary["custom_modes"]:savedLibrary["modes"]) as JArray??new()).OfType<JObject>().FirstOrDefault(x=>(string?)x["id"]==parts[0]);
        var sp=(sm?["profiles"] as JArray??new()).OfType<JObject>().FirstOrDefault(x=>(string?)x["id"]==(parts.Length>1?parts[1]:""));
        if(sm==null||sp==null)return true;
        var savedDoc=UmaWorkflowLibraryModel.Resolve(sm,sp);
        return !JToken.DeepEquals(draft,savedDoc);
    }
    private void MarkDirty()=>MarkDirty(fromPendingInput:false);
    private void MarkDirty(bool fromPendingInput)
    {
        if(loading||suppressDirty)return;
        if(v2BuiltinMode) {
            if(string.IsNullOrEmpty(buildId))return;
            if(!fromPendingInput&&!IsActuallyDirty(buildId)) {
                dirtyBuilds.Remove(buildId);
                UpdateSummary();
                return;
            }
            dirtyBuilds.Add(buildId);
            UpdateSummary();
            return;
        }
        if(!fromPendingInput&&!IsActuallyDirty(Key))
        {
            dirty.Remove(Key);
            UpdateSummary();
            return;
        }
        dirty.Add(Key);
        UpdateSummary();
    }
    public void RequestPurityDrawer()=>PurityDrawerRequested?.Invoke($"{SelectedModeLabel} · {SelectedConfigurationLabel}",EvaluateCurrentPurity());
    public (string ModeProfile, string EquipmentBatch, PurityState State, string PuritySummary, string TargetsText) ReadOverviewSummary()
    {
        if (!initialized && !string.IsNullOrWhiteSpace(lastInitError))
            return ("配置暂未就绪", "请点击顶栏「↻ 重试读取」或稍候自动恢复", PurityState.Unknown, "⚠ 配置待读取", "—");
        var doc=ResolveCurrentDocument();
        var node=(doc["run_graph"]?["nodes"] as JArray)?.OfType<JObject>().FirstOrDefault(row=>(string?)row["kind"] is "career" or "cut");
        var policy=doc["policies"]?[(string?)node?["policy"]??"default"];
        var cut=(string?)node?["kind"]=="cut";
        var optObj=(cut?policy?["cut_options"]:policy?["career_options"]) as JObject;
        var settings=(cut?optObj?["cut"]:optObj?["setup"]) as JObject;
        var eq=EquipmentText(settings);
        if(cut&&settings?["max_attempts"] is JValue attempts)eq+=$" · 最多 {attempts} 局";
        else if(careerBatchLimits!=null&&CanRepeatCareer(doc))eq+=$" · 连续 {CareerCount(doc)} 局";
        var reports=EvaluateCurrentPurity(doc);
        var (state,badgeText,_)=PacerPurity.SummarizeDocument(reports);
        var tObj=optObj?["targets"] as JObject;
        int T(string k,int def)=>(int?)tObj?[k]??def;
        var targetsStr=$"{T("speed",1550)} / {T("stamina",1200)} / {T("power",1200)} / {T("guts",1000)} / {T("wisdom",1200)}";
        return ($"{SelectedModeLabel} · {SelectedConfigurationLabel}", eq, state, badgeText, targetsStr);
    }
    private IReadOnlyList<PurityReport> EvaluateCurrentPurity(JObject? doc=null)
    {
        var target=doc??ResolveCurrentDocument();
        var curMode=modeId;var curProfile=v2BuiltinMode?buildId:profileId;
        return PacerPurity.EvaluateDocument(target,stepId=>provenanceStore?.Read(curMode,curProfile,stepId));
    }
    private void UpdateSummary()
    {
        var doc=ResolveCurrentDocument();
        var node=(doc["run_graph"]?["nodes"] as JArray)?.OfType<JObject>()
            .FirstOrDefault(row=>(string?)row["kind"] is "career" or "cut");
        var policy=doc["policies"]?[(string?)node?["policy"]??"default"];
        var cut=(string?)node?["kind"]=="cut";
        var optObj=(cut?policy?["cut_options"]:policy?["career_options"]) as JObject;
        var settings=(cut?optObj?["cut"]:optObj?["setup"]) as JObject;
        var reports=EvaluateCurrentPurity(doc);
        var hasUndo=v2BuiltinMode?(buildId!=null&&purityUndoBuilds.ContainsKey(buildId)):purityUndo.ContainsKey(Key);
        purityBadge.Update(reports,hasUndo);
        PurityReportsChanged?.Invoke(reports);
        multiStepBar.Children.Clear();
        var hideStepPills=v2BuiltinMode&&!HasLegacyGrowOverrides();
        if(!hideStepPills&&reports.Count>1)
        {
            multiStepBar.IsVisible=true;
            multiStepBar.Children.Add(new TextBlock{Text="步骤：",FontSize=11,VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,4,0),Opacity=0.72});
            var activeStep=ordinarySelections.TryGetValue(Key,out var sid)?sid:reports[0].StepId;
            foreach(var sr in reports)
            {
                var stepCopy=sr;
                var isCur=string.Equals(sr.StepId,activeStep,StringComparison.Ordinal);
                var stepBtn=new Button{
                    Content=$"{sr.Emoji} {sr.StepLabel}",
                    Height=24,
                    MinHeight=0,
                    Padding=new Thickness(8,0),
                    Margin=new Thickness(0,0,4,0),
                    FontSize=11,
                    VerticalContentAlignment=VerticalAlignment.Center
                };
                if(isCur)stepBtn.Classes.Add("Accent");
                stepBtn.Click+=(_,_)=>{
                    ordinarySelections[Key]=stepCopy.StepId;
                    strategySelections[Key]=stepCopy.StepId;
                    suppressDirty=true;
                    try{
                        RefreshConfigurationEditor();
                        foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
                        if(selectedPage=="strategy")OpenStrategy(commitPending:false);
                        UpdateSummary();
                    }
                    finally{suppressDirty=false;}
                };
                multiStepBar.Children.Add(stepBtn);
            }
        }
        else multiStepBar.IsVisible=false;

        if(!saveAsPurifyTouched) {
            var priorLoading=loading;loading=true;
            try{saveAsPurify.IsChecked=reports.Any(r=>r.State==PurityState.Stale||r.HasLayer(PurityLayer.L3Tuning)||r.HasLayer(PurityLayer.L4Workflow));}
            finally{loading=priorLoading;}
        }
        summary.Text=node==null?"下次开局 · 请选择运行配置"
            :v2BuiltinMode?$"下次开局 · 方案「{SelectedConfigurationLabel}」· 模式「{SelectedModeLabel}」· "+EquipmentText(settings)
            :"下次开局 · "+EquipmentText(settings);
        if(cut&&settings?["max_attempts"] is JValue attempts)summary.Text+=$" · 最多 {attempts} 局";
        if(careerBatchLimits!=null&&CanRepeatCareer(doc))summary.Text+=$" · 连续 {CareerCount(doc)} 局";
        var larcPolicy=optObj?["larc_policy"] as JObject;
        var rev=larcPolicy?["revision"]?.Type==JTokenType.String?(string?)larcPolicy["revision"]:null;
        var firstReport=reports.FirstOrDefault();
        var purityTag=firstReport==null?"":firstReport.IsPure?"纯净":firstReport.State==PurityState.Stale?"需关注":"自定义权重";
        if(!string.IsNullOrEmpty(rev))summary.Text+=$" · 策略 {rev} ({purityTag})";
        if(optObj?["targets"] is JObject tObj&&tObj.Count>0) {
            int T(string k,int def)=>(int?)tObj[k]??def;
            summary.Text+=$" · 目标 {T("speed",1550)}/{T("stamina",1200)}/{T("power",1200)}/{T("guts",1200)}/{T("wisdom",1200)}";
        }
        ToolTip.SetTip(summary,summary.Text);
        var isDirty=v2BuiltinMode?(buildId!=null&&dirtyBuilds.Contains(buildId)):dirty.Contains(Key);
        save.Content=v2BuiltinMode?"保存方案":"保存配置";
        if(isDirty){if(!save.Classes.Contains("Accent"))save.Classes.Add("Accent");save.FontWeight=FontWeight.SemiBold;}
        else{save.Classes.Remove("Accent");save.FontWeight=FontWeight.Normal;}
        saveStateBadgeText.Text=isDirty?"● 未保存修改":"✓ 已保存";
        saveStateBadge.Background=new SolidColorBrush(isDirty?Color.Parse("#FEF3C7"):Color.Parse("#ECFDF5"));
        saveStateBadge.BorderBrush=new SolidColorBrush(isDirty?Color.Parse("#FCD34D"):Color.Parse("#A7F3D0"));
        saveStateBadgeText.Foreground=new SolidColorBrush(isDirty?Color.Parse("#92400E"):Color.Parse("#065F46"));
        saveOnConfigurationPage.Content=isDirty?"保存当前配置 · 有修改":"保存当前配置";
        discardChanges.IsVisible=isDirty;
        deleteProfile.IsEnabled=!busy&&(v2BuiltinMode?Builds.Count()>1:((Mode()?["profiles"] as JArray)?.Count??0)>1);
    }
    internal static string EquipmentText(JObject? settings,IReadOnlyList<UmaTraineeChoice>? choices=null)
    {
        var id=(string?)settings?["character_id"];
        var name=(string?)settings?["character_name"];
        if(string.IsNullOrWhiteSpace(name))name=(string?)settings?["validation_character_name"];
        if(string.IsNullOrWhiteSpace(name))name=choices?.FirstOrDefault(choice=>choice.CardId==id)?.Label;
        if(string.IsNullOrWhiteSpace(name))name=id is null or "" or "current"?"沿用当前角色":$"角色编号 {id}";
        var deck=(string?)settings?["support_deck"];
        return name+" · "+(string.IsNullOrWhiteSpace(deck)||deck=="0"?"沿用当前卡组":$"卡组 {deck}");
    }
    internal void SetHostedMode(bool hosted) { hostedMode = hosted; showEquipmentButton.IsVisible = !hosted; if (hosted) tabs.IsVisible = false; }
    public void OpenStrategyTimelineTab()
    {
        customWeightsPage=false;tabs.IsVisible=!hostedMode;weightsTitle.IsVisible=false;weightsHelp.IsVisible=false;
        preferences.ShowBaseWeightsOnly(false);
        ShowPage("strategy",commitPending:false);
    }
    public void OpenAdvancedFlowTab()
    {
        customWeightsPage=false;tabs.IsVisible=!hostedMode;weightsTitle.IsVisible=false;weightsHelp.IsVisible=false;
        preferences.ShowBaseWeightsOnly(false);
        ShowPage("advanced",commitPending:false);
    }
    public void SetCustomWeightsPage(bool visible)
    {
        if(customWeightsPage==visible)return;
        customWeightsPage=visible;
        tabs.IsVisible=!visible&&!hostedMode;
        weightsTitle.IsVisible=visible;
        weightsHelp.IsVisible=visible;
        preferences.ShowBaseWeightsOnly(visible);
        if(initialized)ShowPage(selectedPage,commitPending:false);
    }
    private void ShowPage(string id,bool commitPending=true)
    {
        if(customWeightsPage){OpenStrategy(commitPending:false);return;}
        if(commitPending&&!CommitOrdinary())return;
        CaptureEditor();
        if(id=="equipment"&&(ConfigurationRequested!=null||OrdinarySettingsRequested!=null)){OpenConfiguration();return;}
        selectedPage=id;page.Content=null;strategyPanel.Children.Remove(editor);advancedFlowPanel.Children.Remove(editor);
        if(id=="equipment")page.Content=ConfigurationPage;
        else if(id=="strategy")OpenStrategy(commitPending);
        else if(id=="advanced") {advancedFlowPanel.Children.Add(editor);page.Content=advancedFlowPanel;editor.ShowAdvancedFlow();}
        else {selectedPage="plan";page.Content=editor;editor.ShowPlan(OpenStrategyAt,()=>ShowPage("equipment"));}
    }
    private void OpenStrategyAt(string nodeId)
    {
        strategySelections[Key]=nodeId;OpenStrategy();
    }
    private void OpenStrategy() => OpenStrategy(commitPending:true);

    private void OpenStrategy(bool commitPending)
    {
        if(commitPending&&!CommitOrdinary())return;
        CaptureEditor();
        if(!customWeightsPage)selectedPage="strategy";
        page.Content=null;strategyPanel.Children.Remove(editor);advancedFlowPanel.Children.Remove(editor);
        var choices=editor.ReadPolicyChoices().OfType<JObject>().Select(item=>new LibraryChoice((string)item["node_id"]!,
            $"{(string?)item["phase"]} · {(string?)item["label"]}")).ToArray();
        var remembered=strategySelections.TryGetValue(Key,out var nodeId)?nodeId:null;
        var selectedChoice=choices.FirstOrDefault(item=>item.Id==remembered)??choices.FirstOrDefault();
        loadingStrategySteps=true;
        try{strategySteps.ItemsSource=choices;strategySteps.SelectedItem=selectedChoice;strategySteps.IsEnabled=!busy&&choices.Length>1;strategyStepField.IsVisible=choices.Length>1;}
        finally{loadingStrategySteps=false;}
        strategyPanel.Children.Add(editor);page.Content=strategyPanel;
        if(selectedChoice!=null){strategySelections[Key]=selectedChoice.Id;editor.SelectPolicy(selectedChoice.Id,showReturn:!customWeightsPage);RefreshStrategySummary();}
        else{strategySummary.Text="当前模式没有培养步骤；添加培养步骤后可以设置对应权重。";editor.ShowFlow();}
    }
    private void RefreshStrategySummary()
    {
        if(strategySteps.SelectedItem is not LibraryChoice choice)return;
        strategySummary.Text=editor.PlanStrategySummary(choice.Id);
    }
    private Control ConfigurationPanel(string? section)
    {
        var capturedIsV2=v2BuiltinMode;
        var capturedBuildId=buildId;
        var capturedModeId=modeId;
        var key=Key;
        var sectionKey=section??"ordinary";
        if(section is not (null or "deck" or "strategy_targets" or "cycle")) {
            var initial=ResolveCurrentDocument();var previous=(JObject)initial.DeepClone();
            string? singleStepId=null;
            string? singleEditedKind=null;
            if(capturedIsV2&&capturedModeId=="cut_then_career") {
                singleStepId=section is "custom_races" or "mid_skills"?"grow":"screen";
                singleEditedKind=section is "custom_races" or "mid_skills"?"career":"cut";
            }
            var settings=new UmaWorkflowRunConfigurationEditor(initial,value=> {
                if(capturedIsV2&&capturedBuildId!=null&&capturedModeId!=null) {
                    var draftBuild=EnsureBuildDraft(capturedBuildId);
                    var latestV2=UmaWorkflowLibraryModel.CompileV2(draftBuild,capturedModeId,builtinTemplates);
                    PatchOrdinaryOptions(latestV2,previous,value);
                    previous=(JObject)value.DeepClone();
                    UmaWorkflowLibraryModel.CaptureV2(draftBuild,capturedModeId,latestV2,singleEditedKind);
                    var recompiled=UmaWorkflowLibraryModel.CompileV2(draftBuild,capturedModeId,builtinTemplates);
                    if(capturedBuildId==buildId&&capturedModeId==modeId){editor.LoadDocument(recompiled);MarkDirty();}
                    return;
                }
                var latest=key==Key?ReadSelectedDocument():drafts.TryGetValue(key,out var stored)?(JObject)stored.DeepClone():(JObject)initial.DeepClone();
                PatchOrdinaryOptions(latest,previous,value);previous=(JObject)value.DeepClone();
                drafts[key]=latest;
                if(key==Key){editor.LoadDocument(latest);MarkDirty();}
            },request,section,singleStepId);
            settings.SkillLearningRequested+=RequestSkillLearning;
            settings.RaceScheduleRequested+=RequestRaceSchedule;
            settings.PendingChanged+=()=>{if((capturedIsV2?capturedBuildId==buildId&&capturedModeId==modeId:key==Key))MarkDirty(fromPendingInput:true);};
            HookRunEditorEvents(settings);
            activeSectionCommits[sectionKey]=()=>settings.TryCommitPending();
            return settings;
        }
        var current=ResolveCurrentDocument();
        var nodes=(current["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>()
            .Where(n=>(string?)n["kind"] is "career" or "cut")
            .GroupBy(n=>((string?)n["policy"]??"default")+"/"+(string?)n["kind"]).Select(g=>g.First()).ToArray();
        if(nodes.Length==0)return Note("此模式没有培养步骤；可在高级设置 → 流程编排中查看。");
        var unifySteps=capturedIsV2&&!HasLegacyGrowOverrides();
        if(unifySteps&&nodes.Length>1)
            nodes=new[]{nodes.FirstOrDefault(n=>(string?)n["kind"]=="cut")??nodes[0]};
        var choices=nodes.Select(n=>new LibraryChoice((string)n["id"]!,
            ((string?)n["kind"]=="cut"?"前期培养":"养完阶段")+" · "+((string?)n["label"]??(string)n["id"]!))).ToArray();
        var remembered=ordinarySelections.TryGetValue(key,out var rememberedId)?rememberedId:null;
        var selected=choices.FirstOrDefault(c=>c.Id==remembered)??choices[0];
        var picker=new ComboBox{Name="WorkflowOrdinaryStep_"+sectionKey,ItemsSource=choices,SelectedItem=selected,MinWidth=280,Height=34};
        var host=new ContentControl{HorizontalContentAlignment=HorizontalAlignment.Stretch};
        var body=new StackPanel{Spacing=10};if(!unifySteps&&choices.Length>1)body.Children.Add(Field("当前编辑步骤",picker));body.Children.Add(host);
        var cached=new Dictionary<string,UmaWorkflowRunConfigurationEditor>();
        void Open(LibraryChoice choice) {
            ordinarySelections[key]=choice.Id;
            if(!cached.TryGetValue(choice.Id,out var settings)) {
                var snapshot=ResolveCurrentDocument();var node=nodes.Single(n=>(string?)n["id"]==choice.Id);
                var policyId=(string?)node["policy"]??"default";var kind=(string)node["kind"]!;
                var optionKey=kind=="cut"?"cut_options":"career_options";
                var previous=snapshot["policies"]?[policyId]?[optionKey] is JObject priorOptions?(JObject)priorOptions.DeepClone():new JObject();
                JObject? FindStep(JObject value)=>(value["run_graph"]?["nodes"] as JArray)?.OfType<JObject>()
                    .FirstOrDefault(n=>(string?)n["id"]==choice.Id);
                var previousSieve=FindStep(snapshot)?["ss_sieve"]?.DeepClone() as JObject;
                var previousAce=FindStep(snapshot)?["ace_cycle"]?.DeepClone() as JObject;
                var previousScreening=FindStep(snapshot)?["screening"]?.DeepClone() as JObject;
                var hasPreviousCheckAt=FindStep(snapshot)?.Property("check_at")!=null;
                var previousCheckAt=FindStep(snapshot)?["check_at"]?.DeepClone();
                var previousCycleBudget=snapshot["policies"]?[policyId]?["cut_options"]?["cut"]?.DeepClone() as JObject??new JObject();
                var downstreamCareer=kind=="cut"?(snapshot["run_graph"]?["nodes"] as JArray)?.OfType<JObject>().FirstOrDefault(n=>(string?)n["kind"]=="career"):null;
                var careerPid=(string?)downstreamCareer?["policy"]??"default";
                var previousHandoff=downstreamCareer!=null?(snapshot["policies"]?[careerPid]?["career_options"]?["manual_handoff"] as JObject)?.DeepClone() as JObject:null;
                var hasPreviousPauseDates=downstreamCareer!=null&&(snapshot["policies"]?[careerPid]?["career_options"] as JObject)?.Property("manual_pause_dates")!=null;
                var previousPauseDates=downstreamCareer!=null?snapshot["policies"]?[careerPid]?["career_options"]?["manual_pause_dates"]?.DeepClone():null;
                settings=new UmaWorkflowRunConfigurationEditor(snapshot,value=> {
                    var nextToken=value["policies"]?[policyId]?[optionKey];var next=nextToken as JObject??new JObject();
                    var latest=capturedIsV2&&capturedBuildId!=null&&capturedModeId!=null
                        ?UmaWorkflowLibraryModel.CompileV2(EnsureBuildDraft(capturedBuildId),capturedModeId,builtinTemplates)
                        :key==Key?ReadSelectedDocument():drafts.TryGetValue(key,out var storedDraft)?(JObject)storedDraft.DeepClone():(JObject)snapshot.DeepClone();
                    var policies=OrdinaryObject(latest,"policies");
                    var policy=OrdinaryObject(policies,policyId);
                    var target=policy[optionKey] as JObject??new JObject();PatchOrdinaryOptions(target,previous,next);
                    if(target.Count==0&&nextToken is not JObject){if(nextToken==null)policy.Remove(optionKey);else policy[optionKey]=nextToken.DeepClone();}
                    else if(!ReferenceEquals(policy[optionKey],target))policy[optionKey]=target;
                    if(downstreamCareer!=null) {
                        var nextHandoff=value["policies"]?[careerPid]?["career_options"]?["manual_handoff"] as JObject;
                        if(!JToken.DeepEquals(previousHandoff,nextHandoff)) {
                            var targetCareerOpts=OrdinaryObject(OrdinaryObject(policies,careerPid),"career_options");
                            if(nextHandoff==null)targetCareerOpts.Remove("manual_handoff");
                            else targetCareerOpts["manual_handoff"]=nextHandoff.DeepClone();
                            previousHandoff=nextHandoff?.DeepClone() as JObject;
                        }
                        var nextPauseProp=(value["policies"]?[careerPid]?["career_options"] as JObject)?.Property("manual_pause_dates");
                        if(hasPreviousPauseDates!=(nextPauseProp!=null)||!JToken.DeepEquals(previousPauseDates,nextPauseProp?.Value)) {
                            var targetCareerOpts=OrdinaryObject(OrdinaryObject(policies,careerPid),"career_options");
                            if(nextPauseProp==null)targetCareerOpts.Remove("manual_pause_dates");
                            else targetCareerOpts["manual_pause_dates"]=nextPauseProp.Value.DeepClone();
                            hasPreviousPauseDates=nextPauseProp!=null;
                            previousPauseDates=nextPauseProp?.Value.DeepClone();
                        }
                    }
                    var nextStepNode=FindStep(value);
                    var nextScreening=nextStepNode?["screening"] as JObject;
                    if((previousScreening!=null||nextScreening!=null)&&!JToken.DeepEquals(previousScreening,nextScreening)&&FindStep(latest) is JObject targetScreenNode) {
                        if(nextScreening==null)targetScreenNode.Remove("screening");
                        else if(previousScreening!=null&&targetScreenNode["screening"] is JObject targetScreening)
                            PatchOrdinaryOptions(targetScreening,previousScreening,nextScreening);
                        else targetScreenNode["screening"]=nextScreening.DeepClone();
                        previousScreening=nextScreening?.DeepClone() as JObject;
                    }
                    var nextCheckProp=nextStepNode?.Property("check_at");
                    if((hasPreviousCheckAt!=(nextCheckProp!=null)||!JToken.DeepEquals(previousCheckAt,nextCheckProp?.Value))&&FindStep(latest) is JObject targetCheckNode) {
                        if(nextCheckProp==null)targetCheckNode.Remove("check_at");
                        else targetCheckNode["check_at"]=nextCheckProp.Value.DeepClone();
                        hasPreviousCheckAt=nextCheckProp!=null;
                        previousCheckAt=nextCheckProp?.Value.DeepClone();
                    }
                    var hasCycleBudget=false;
                    if(previousSieve!=null&&FindStep(value)?["ss_sieve"] is JObject nextSieve
                        && FindStep(latest)?["ss_sieve"] is JObject targetSieve) {
                        PatchOrdinaryOptions(targetSieve,previousSieve,nextSieve);
                        previousSieve=(JObject)nextSieve.DeepClone();
                        hasCycleBudget=true;
                    }
                    if(previousAce!=null&&FindStep(value)?["ace_cycle"] is JObject nextAce
                        && FindStep(latest)?["ace_cycle"] is JObject targetAce) {
                        PatchOrdinaryOptions(targetAce,previousAce,nextAce);
                        previousAce=(JObject)nextAce.DeepClone();
                        hasCycleBudget=true;
                    }
                    if(hasCycleBudget) {
                        var nextBudget=value["policies"]?[policyId]?["cut_options"]?["cut"] as JObject??new JObject();
                        if(!JToken.DeepEquals(previousCycleBudget,nextBudget))
                            PatchOrdinaryOptions(OrdinaryObject(OrdinaryObject(policy,"cut_options"),"cut"),previousCycleBudget,nextBudget);
                        previousCycleBudget=(JObject)nextBudget.DeepClone();
                    }
                    previous=(JObject)next.DeepClone();
                    if(capturedIsV2&&capturedBuildId!=null&&capturedModeId!=null) {
                        var draftBuild=EnsureBuildDraft(capturedBuildId);
                        UmaWorkflowLibraryModel.CaptureV2(draftBuild,capturedModeId,latest,kind);
                        var recompiled=UmaWorkflowLibraryModel.CompileV2(draftBuild,capturedModeId,builtinTemplates);
                        if(capturedBuildId==buildId&&capturedModeId==modeId){editor.LoadDocument(recompiled);MarkDirty();}
                    } else {
                        drafts[key]=latest;
                        if(key==Key){editor.LoadDocument(latest);MarkDirty();}
                    }
                },request,section,choice.Id,newPolicyDefaultsByMode?[kind] as JObject,
                onOpenPurityDrawer:RequestPurityDrawer,
                onPurifyCurrent:()=>PurifyCurrentDraft(PurityLayer.All,recordUndo:true),
                readPurity:()=>EvaluateCurrentPurity(),
                extraCycleControl:section=="cycle"?careerBatchSection:null,
                postCutContinueCareer:capturedModeId=="cut_then_career");
                settings.SkillLearningRequested+=RequestSkillLearning;
                settings.RaceScheduleRequested+=RequestRaceSchedule;
                settings.CutPostActionChanged+=continueCareer=> {
                    if(!capturedIsV2||capturedBuildId==null)return;
                    var fromMode=modeId;
                    var toMode=continueCareer?"cut_then_career":"cut_preserve";
                    if(string.Equals(fromMode,toMode,StringComparison.Ordinal))return;
                    CommitOrdinary();
                    CaptureEditor();
                    var draftBuild=EnsureBuildDraft(capturedBuildId);
                    if(fromMode is "cut_preserve" or "cut_then_career")
                        UmaWorkflowLibraryModel.SyncCutSettingsBetweenModes(draftBuild,fromMode,toMode);
                    modeId=toMode;
                    library["selected_mode_id"]=toMode;
                    LoadSelection();
                    MarkDirty();
                };
                HookRunEditorEvents(settings);
                settings.PendingChanged+=()=>{if((capturedIsV2?capturedBuildId==buildId&&capturedModeId==modeId:key==Key))MarkDirty(fromPendingInput:true);};cached.Add(choice.Id,settings);
            }
            host.Content=settings;
        }
        picker.SelectionChanged+=(_,_)=>{if(picker.SelectedItem is LibraryChoice choice)Open(choice);};
        Open(selected);
        bool CommitThisSection() {
            foreach(var pair in cached)if(!pair.Value.TryCommitPending()) {
                picker.SelectedItem=choices.Single(c=>c.Id==pair.Key);host.Content=pair.Value;return false;
            }
            return true;
        }
        if(section==null)commitOrdinary=CommitThisSection;
        activeSectionCommits[sectionKey]=CommitThisSection;
        return body;
    }
    private void RequestSkillLearning()
    {
        if(!CommitOrdinary())return;
        CaptureEditor();SkillLearningRequested?.Invoke();
    }
    private void RequestRaceSchedule()
    {
        if(!CommitOrdinary())return;
        CaptureEditor();RaceScheduleRequested?.Invoke();
    }
    private static void PatchOrdinaryOptions(JObject target,JObject previous,JObject next)
    {
        foreach(var property in previous.Properties().Where(p=>next.Property(p.Name)==null).ToArray())target.Remove(property.Name);
        foreach(var property in next.Properties()) {
            var before=previous[property.Name];if(JToken.DeepEquals(before,property.Value))continue;
            if(before is JObject prior&&property.Value is JObject value&&target[property.Name] is JObject original)PatchOrdinaryOptions(original,prior,value);
            else target[property.Name]=property.Value.DeepClone();
        }
    }
    private static JObject OrdinaryObject(JObject parent,string key)
    {
        if(parent[key] is JObject value)return value;
        var created=new JObject();parent[key]=created;return created;
    }
    private bool CommitOrdinary()
    {
        if(committingOrdinary)return true;
        committingOrdinary=true;
        try {
            if(careerBatchSection.IsVisible&&careerBatchField.IsVisible) {
                if(!TryReadCareerBatchCount(out var count)) {
                    var limits=careerBatchLimits!.Value;
                    status.Text=$"连续育成局数请填 {limits.Minimum}～{limits.Maximum} 的整数。";
                    return false;
                }
                ApplyCareerBatchCount(count);
            }
            if(commitOrdinary!=null&&!commitOrdinary())return false;
            foreach(var commit in activeSectionCommits.Values.ToArray())
                if(!commit())return false;
            return true;
        }
        catch(Exception error){status.Text=error.Message;return false;}
        finally{committingOrdinary=false;}
    }

    private static bool CanRepeatCareer(JObject document)
    {
        if(document["run_graph"] is not JObject graph||graph["nodes"] is not JArray {Count:3} nodes
            ||graph["edges"] is not JArray {Count:2} edges)return false;
        if((string?)document["template_id"] is "ss_sieve_cycle" or "ace_career_cycle"
            ||nodes.OfType<JObject>().Any(node=>node.Property("ss_sieve")!=null||node.Property("ace_cycle")!=null))return false;
        string? Id(string kind)=>(string?)nodes.OfType<JObject>().FirstOrDefault(row=>(string?)row["kind"]==kind)?["id"];
        var entry=Id("entry");var career=Id("career");var finish=Id("finish");
        bool HasEdge(string from,string to,string outcome)=>edges.OfType<JObject>().Any(edge=>
            (string?)edge["from"]==from&&(string?)edge["to"]==to&&(string?)edge["outcome"]==outcome);
        return entry!=null&&career!=null&&finish!=null&&(string?)graph["entry"]==entry
            &&HasEdge(entry,career,"next")&&HasEdge(career,finish,"completed");
    }
    private static int CareerCount(JObject document)=>(int?)document["career_batch"]?["max_careers"]??1;
    private bool TryReadCareerBatchCount(out int count)
    {
        count=1;
        if(careerBatchLimits is not { } limits||careerBatchCount.Value is not decimal value
            ||value!=decimal.Truncate(value)||value<limits.Minimum||value>limits.Maximum)return false;
        count=(int)value;return true;
    }
    private void ApplyCareerBatchCount(int count)
    {
        var draft=ReadSelectedDocument();
        if(CareerCount(draft)==count)return;
        OrdinaryObject(draft,"career_batch")["max_careers"]=count;
        if(v2BuiltinMode&&buildId!=null&&modeId!=null) {
            var db=EnsureBuildDraft(buildId);
            UmaWorkflowLibraryModel.CaptureV2(db,modeId,draft);
            editor.LoadDocument(UmaWorkflowLibraryModel.CompileV2(db,modeId,builtinTemplates));
            MarkDirty();
            return;
        }
        drafts[Key]=draft;editor.LoadDocument(draft);MarkDirty();
    }
    public Control CreateConfigurationPage(string section)
    {
        var host=new ContentControl{HorizontalContentAlignment=HorizontalAlignment.Stretch};
        settingsPages.Add((host,section));
        host.Loaded += async (_, _) =>
        {
            RefreshSettingsPage(host, section);
            if (!initialized && !busy && (DateTime.UtcNow - lastInitAttemptUtc).TotalSeconds >= 3)
                await InitializeAsync();
        };
        return host;
    }
    // The overview links to the single ordinary editor on the configuration page.
    public Control CreateOverviewSettings()
    {
        if(overviewSettings!=null)return overviewSettings;
        overviewSettings=new ContentControl{Name="WorkflowOverviewCommonSettings",HorizontalContentAlignment=HorizontalAlignment.Stretch};
        RefreshOverviewSettings();return overviewSettings;
    }
    public void RefreshOverviewSettings()
    {
        if(overviewSettings==null||overviewSettings.Content!=null)return;
        overviewSettings.Content=new StackPanel{Spacing=6,Children={
            Note("角色、配卡、育成局数和人工交接集中在配置管理。"),
            ActionButton("WorkflowOpenConfigurationFromOverview","打开配置管理",OpenConfiguration)}};
    }
    private void RefreshConfigurationEditor()
    {
        if(!initialized||!HasValidSelection())return;
        commitOrdinary=null;
        RefreshCareerBatch(ResolveCurrentDocument());
        configurationEditor.Content=ConfigurationPanel(null);
    }
    private void RefreshCareerBatch(JObject draft)
    {
        var previous=loading;loading=true;
        try {
            var supported=CanRepeatCareer(draft);
            careerBatchSection.IsVisible=careerBatchLimits!=null&&(supported||CareerCount(draft)>1);
            careerBatchField.IsVisible=supported;
            resetCareerBatch.IsVisible=!supported;
            careerBatchHint.Text=supported?"当前局计作第 1 局。后续开局需要在下面选定具体马娘。"
                :"当前流程只支持单局。恢复单局后可以继续保存流程。";
            if(careerBatchSection.IsVisible&&supported&&careerBatchLimits is { } limits) {
                careerBatchCount.Minimum=limits.Minimum;careerBatchCount.Maximum=limits.Maximum;
                careerBatchCount.Value=CareerCount(draft);
            }
        }finally{loading=previous;}
    }
    private void OpenConfiguration()
    {
        if(ConfigurationRequested!=null)ConfigurationRequested.Invoke();
        else if(OrdinarySettingsRequested!=null)OrdinarySettingsRequested.Invoke();
        else ShowPage("equipment");
    }
    public void ShowOrdinarySettings() => OpenConfiguration();
    private void RefreshSettingsPage(ContentControl host,string section)
    {
        if(!initialized)
        {
            if (!busy && !string.IsNullOrWhiteSpace(lastInitError))
            {
                var retryBtn = new Button
                {
                    Name = "WorkflowRetryInitialize_" + section,
                    Content = "↻ 重新读取配置",
                    HorizontalAlignment = HorizontalAlignment.Left
                };
                retryBtn.Click += async (_, _) => { autoRetryCount = 0; await InitializeAsync(); };
                host.Content = new StackPanel
                {
                    Spacing = 8,
                    Children = { Note("当前运行配置尚未读取完成：" + lastInitError), retryBtn }
                };
                return;
            }
            host.Content=Note("正在读取当前运行配置…");
            return;
        }
        var prevSuppress=suppressDirty;suppressDirty=true;
        try {
            if(section is "deck" or "strategy_targets" or "cycle" or "custom_races") {
                host.Content=ConfigurationPanel(section);
                return;
            }
            var body=new StackPanel{Spacing=12};body.Children.Add(Note((v2BuiltinMode?"当前培养方案：":"当前配置：")+SelectedConfigurationLabel+"；修改后由上方保存，新一轮生效。"));
            body.Children.Add(ConfigurationPanel(section));host.Content=body;
        }
        finally{suppressDirty=prevSuppress;}
    }
    public JObject ReadSelectedDocument()
    {CaptureEditor();return ResolveCurrentDocument();}
    public JObject ReadLibraryDraft()
    {
        CaptureEditor();var copy=(JObject)library.DeepClone();
        if(isV2) {
            if(copy["builds"] is JArray bs) {
                for(var i=0;i<bs.Count;i++)
                    if(bs[i] is JObject bo&&(string?)bo["id"] is string bid&&buildDrafts.TryGetValue(bid,out var db))
                        bs[i]=db.DeepClone();
            }
            foreach(var m in (copy["custom_modes"] as JArray??new()).OfType<JObject>())foreach(var p in (m["profiles"] as JArray??new()).OfType<JObject>())
                if(drafts.TryGetValue((string?)m["id"]+"/"+(string?)p["id"],out var draft))UmaWorkflowLibraryModel.Capture(m,p,draft);
            return copy;
        }
        foreach(var m in (copy["modes"] as JArray??new()).OfType<JObject>())foreach(var p in (m["profiles"] as JArray??new()).OfType<JObject>())
            if(drafts.TryGetValue((string?)m["id"]+"/"+(string?)p["id"],out var draft))UmaWorkflowLibraryModel.Capture(m,p,draft);
        return copy;
    }
    private static bool BuildNameExists(JObject libObj,string name,string? exceptId=null)=>
        (libObj["builds"] as JArray??new()).OfType<JObject>().Any(b=>
            (exceptId==null||(string?)b["id"]!=exceptId)
            &&string.Equals(((string?)b["name"])?.Trim(),name,StringComparison.OrdinalIgnoreCase));
    public async Task SaveAsync()
    {
        if(busy||!initialized)return;
        if(!CommitOrdinary()){OpenConfiguration();return;}
        CaptureEditor();
        if(v2BuiltinMode) {
            if(ActiveBuild() is not JObject active||string.IsNullOrEmpty(buildId))return;
            var bid=buildId!;
            var candidate=(JObject)savedLibrary.DeepClone();
            var targetBuild=(candidate["builds"] as JArray??new()).OfType<JObject>().FirstOrDefault(x=>(string?)x["id"]==bid);
            if(targetBuild==null) {
                targetBuild=(JObject)active.DeepClone();
                ((JArray)(candidate["builds"]??=new JArray())).Add(targetBuild);
            } else {
                var idx=((JArray)candidate["builds"]!).IndexOf(targetBuild);
                ((JArray)candidate["builds"]!)[idx]=active.DeepClone();
                targetBuild=(JObject)((JArray)candidate["builds"]!)[idx];
            }
            if(buildNames.TryGetValue(bid,out var pendingBuildName)) {
                var trimmed=pendingBuildName.Trim();
                if(trimmed.Length==0){status.Text="请填写培养方案名称。";OpenConfiguration();return;}
                if(BuildNameExists(candidate,trimmed,bid)){status.Text="已有同名培养方案。";OpenConfiguration();return;}
                targetBuild["name"]=trimmed;
            }
            candidate["selected_build_id"]=bid;
            candidate["selected_mode_id"]=modeId;
            SetBusy(true);
            try {
                var saved=await PersistLibrary(candidate);
                savedLibrary=(JObject)saved.DeepClone();
                library=(JObject)saved.DeepClone();
                buildDrafts.Remove(bid);dirtyBuilds.Remove(bid);buildNames.Remove(bid);purityUndoBuilds.Remove(bid);
                var prevSuppress=suppressDirty;suppressDirty=true;
                var prevLoading=loading;loading=true;
                try {
                    RefreshPickers();
                    editor.LoadDocument(ResolveCurrentDocument());
                    commitOrdinary=null;
                    activeSectionCommits.Clear();
                    RefreshConfigurationEditor();
                    foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
                    UpdateSummary();
                }
                finally{loading=prevLoading;suppressDirty=prevSuppress;}
                SavedEventPreferences?.Invoke(ReadPrimaryEventPreferences());
                status.Text="当前培养方案已保存（五种模式共用主体配置，各模式要求按方案记忆）；其他方案的未保存修改保留在草稿中。";
                ToastRequested?.Invoke($"已保存培养方案「{targetBuild["name"] ?? SelectedConfigurationLabel}」（{SelectedModeLabel}）。",false,"ok");
            }
            finally{SetBusy(false);}
            return;
        }
        if(Mode() is not JObject m||Profile()==null)return;
        var draft=ReadSelectedDocument();
        if(!UmaWorkflowLibraryModel.SameTopology((JObject)m["document"]!,draft)) {
            status.Text="流程结构有修改。请到高级流程编辑应用到当前模式或另存为新模式。当前配置草稿已保留。";return;
        }
        var candidateV1=(JObject)library.DeepClone();
        var modesArr=(isV2?candidateV1["custom_modes"]:candidateV1["modes"]) as JArray??new JArray();
        var targetMode=modesArr.OfType<JObject>().Single(x=>(string?)x["id"]==modeId);
        var targetProfile=(targetMode["profiles"] as JArray)!.OfType<JObject>().Single(x=>(string?)x["id"]==profileId);
        var key=Key;
        if(profileNames.TryGetValue(key,out var pendingName)) {
            var name=pendingName.Trim();
            if(name.Length==0){status.Text="请填写当前配置名称。";OpenConfiguration();return;}
            if(ProfileNameExists(targetMode,name,profileId)){status.Text="此育成模式已有同名配置。";OpenConfiguration();return;}
            targetProfile["name"]=name;
        }
        UmaWorkflowLibraryModel.Capture(targetMode,targetProfile,draft);SetBusy(true);
        try {
            library=await PersistLibrary(candidateV1);
            savedLibrary=(JObject)library.DeepClone();
            dirty.Remove(key);profileNames.Remove(key);purityUndo.Remove(key);RefreshPickers();
            SavedEventPreferences?.Invoke(ReadPrimaryEventPreferences());
            status.Text="当前配置已保存；其他配置的未保存编辑仍保留在各自草稿中。";
            ToastRequested?.Invoke($"已保存运行配置「{targetProfile["name"] ?? SelectedConfigurationLabel}」。",false,"ok");
        }
        finally{SetBusy(false);}
    }
    private static bool ProfileNameExists(JObject mode,string name,string? exceptId=null)=>
        (mode["profiles"] as JArray??new()).OfType<JObject>().Any(profile=>
            (exceptId==null||(string?)profile["id"]!=exceptId)
            &&string.Equals(((string?)profile["name"])?.Trim(),name,StringComparison.OrdinalIgnoreCase));
    private async Task<JObject> PersistLibrary(JObject candidate)
    {
        var act=isV2?"save_workflow_library_v2":"save_workflow_library";
        var result=await request(new JObject{["action"]=act,["library"]=candidate});
        return (JObject)(result["library"] as JObject??throw new InvalidOperationException("保存结果未返回配置库，请刷新确认。")).DeepClone();
    }
    public async Task SaveAsAsync()
    {
        if(busy||!initialized||!CommitOrdinary())return;
        if(v2BuiltinMode) {
            if(ActiveBuild() is not JObject origBuild)return;
            CaptureEditor();
            var curBuild=(JObject)(ActiveBuild()!).DeepClone();
            var candidate=(JObject)savedLibrary.DeepClone();
            bool BuildNameUsed(string n)=>BuildNameExists(library,n)||BuildNameExists(candidate,n);
            var bName=saveAsName.Text?.Trim()??"";
            if(bName.Length==0) {
                var stem=SelectedConfigurationLabel+" 副本";
                bName=stem;
                for(var number=2;BuildNameUsed(bName);number++)bName=stem+" "+number;
            } else if(BuildNameUsed(bName)){status.Text="已有同名培养方案，请换一个另存为名称。";return;}
            var copyBuild=UmaWorkflowLibraryModel.CopyBuild(curBuild,bName,saveAsPurify.IsChecked==true);
            var copyBid=(string)copyBuild["id"]!;
            ((JArray)(candidate["builds"]??=new JArray())).Add(copyBuild);
            candidate["selected_build_id"]=copyBid;
            candidate["selected_mode_id"]=modeId;
            SetBusy(true);
            try {
                var saved=await PersistLibrary(candidate);
                savedLibrary=(JObject)saved.DeepClone();
                library=(JObject)saved.DeepClone();
                buildId=copyBid;
                saveAsName.Text="";LoadSelection();
                status.Text=$"已另存为独立培养方案「{bName}」，原方案内容与各自草稿已保留。";
                ToastRequested?.Invoke($"已另存为独立培养方案「{bName}」。",false,"ok");
            } catch(Exception error) {
                status.Text="另存失败，当前草稿已保留。"+error.Message;
            } finally{SetBusy(false);}
            return;
        }
        if(Mode() is not JObject editingMode||Profile() is not JObject original)return;
        var draft=(JObject)ReadSelectedDocument().DeepClone();
        if(saveAsPurify.IsChecked==true)
            PacerPurity.PurifyDocument(draft,PurityLayer.DecisionPreferences|PurityLayer.L1Attribute);
        var candidateV1=(JObject)savedLibrary.DeepClone();
        var modesArr=(isV2?candidateV1["custom_modes"]:candidateV1["modes"]) as JArray??new JArray();
        var targetMode=modesArr.OfType<JObject>().FirstOrDefault(mode=>(string?)mode["id"]==modeId);
        if(targetMode?["document"] is not JObject topology||!UmaWorkflowLibraryModel.SameTopology(topology,draft)) {
            status.Text="请先保存这个模式的流程，再在该模式下另存运行配置。当前草稿已保留。";return;
        }
        bool NameUsed(string name)=>ProfileNameExists(editingMode,name)||ProfileNameExists(targetMode,name);
        var name=saveAsName.Text?.Trim()??"";
        if(name.Length==0) {
            var stem=SelectedConfigurationLabel+" 副本";
            name=stem;
            for(var number=2;NameUsed(name);number++)name=stem+" "+number;
        } else if(NameUsed(name)){status.Text="此育成模式已有同名配置，请换一个另存为名称。";return;}

        var copy=UmaWorkflowLibraryModel.CopyProfile(original,name);
        UmaWorkflowLibraryModel.Capture(targetMode,copy,draft);
        var copyId=(string)copy["id"]!;
        ((JArray)targetMode["profiles"]!).Add(copy);
        targetMode["selected_profile_id"]=copyId;
        candidateV1["selected_mode_id"]=modeId;
        SetBusy(true);
        try {
            var saved=await PersistLibrary(candidateV1);
            var savedArr=(isV2?saved["custom_modes"]:saved["modes"]) as JArray??new JArray();
            var savedMode=savedArr.OfType<JObject>().Single(mode=>(string?)mode["id"]==modeId);
            var savedCopy=(savedMode["profiles"] as JArray??new()).OfType<JObject>().Single(profile=>(string?)profile["id"]==copyId);
            savedLibrary=saved;
            ((JArray)editingMode["profiles"]!).Add(savedCopy.DeepClone());
            profileId=copyId;library["selected_mode_id"]=modeId;
            saveAsName.Text="";LoadSelection();
            status.Text=$"已另存为「{name}」，原配置的保存值和各自草稿已保留。";
            ToastRequested?.Invoke($"已另存为独立运行配置「{name}」。",false,"ok");
        } catch(Exception error) {
            status.Text="另存失败，当前草稿已保留。"+error.Message;
        } finally{SetBusy(false);}
    }
    private void CopyMode()
    {
        if(!CommitOrdinary())return;
        var draft=ReadSelectedDocument();
        if(!CanSaveBatchTopology(draft))return;
        if(v2BuiltinMode) {
            var desiredName=modeName.Text?.Trim();
            var baseName=!string.IsNullOrWhiteSpace(desiredName)&&desiredName!=SelectedModeLabel?desiredName:SelectedModeLabel+" 自定义副本";
            var stubMode=new JObject{["id"]=modeId,["name"]=SelectedModeLabel,["document"]=draft.DeepClone()};
            var stubProf=new JObject{["id"]="custom_1",["name"]=SelectedConfigurationLabel};
            var copy=UmaWorkflowLibraryModel.CopyMode(stubMode,stubProf,draft,baseName);
            var customArr=(library["custom_modes"] as JArray)??(JArray)(library["custom_modes"]=new JArray());
            customArr.Add(copy);
            modeId=(string)copy["id"]!;profileId=(string)copy["selected_profile_id"]!;
            library["selected_mode_id"]=modeId;
            LoadSelection();MarkDirty();
            status.Text="已创建自定义流程模式草稿。可在高级流程编辑中命名，再点顶部“保存”写入。";
            return;
        }
        if(Mode() is not JObject m||Profile() is not JObject p)return;
        var desired=modeName.Text?.Trim();
        var copyV1=UmaWorkflowLibraryModel.CopyMode(m,p,draft,!string.IsNullOrWhiteSpace(desired)&&desired!=(string?)m["name"]?desired:((string?)m["name"]??"工作流")+" 副本");
        var list=((isV2?library["custom_modes"]:library["modes"]) as JArray)??(JArray)(library[isV2?"custom_modes":"modes"]=new JArray());
        list.Add(copyV1);modeId=(string)copyV1["id"]!;profileId=(string)copyV1["selected_profile_id"]!;library["selected_mode_id"]=modeId;LoadSelection();MarkDirty();
        status.Text="已创建新模式草稿。可在高级流程编辑中命名，再点顶部“保存配置”写入。";
    }
    private void SaveMode()
    {
        if(v2BuiltinMode) {
            status.Text="内置育成模式流程结构固定；若修改了流程图，请点「另存为新模式」保存为自定义流程。";
            return;
        }
        if(!CommitOrdinary()||Mode() is not JObject m)return;CaptureEditor();
        var draft=ReadSelectedDocument();
        if(!CanSaveBatchTopology(draft))return;
        if(careerBatchLimits!=null&&!CanRepeatCareer(draft)&&(m["profiles"] as JArray??new()).OfType<JObject>()
            .Any(profile=>(string?)profile["id"]!=profileId&&CareerCount(UmaWorkflowLibraryModel.Resolve(m,profile))>1)) {
            status.Text="此模式的其他配置仍设置了连续育成。请先将它们恢复为单局，或另存为新模式。";return;
        }
        var rebased=UmaWorkflowLibraryModel.RebaseMode(m,draft,drafts);
        drafts.Clear();foreach(var pair in rebased)drafts[pair.Key]=pair.Value;
        editor.LoadDocument(drafts.TryGetValue(Key,out var current)?current:UmaWorkflowLibraryModel.Resolve(m,Profile()!));
        MarkDirty();SelectionChanged?.Invoke();status.Text="本模式流程已应用到草稿，尚未保存。其他配置参数保留；最后请点顶部“保存配置”写入。";
    }
    private bool CanSaveBatchTopology(JObject draft)
    {
        if(careerBatchLimits==null||CareerCount(draft)==1||CanRepeatCareer(draft))return true;
        status.Text="当前流程只支持单局。请在配置管理点“恢复单局育成”后再保存流程。";
        return false;
    }
    private void HookRunEditorEvents(UmaWorkflowRunConfigurationEditor settings)
    {
        settings.RevisionSwitchedWithOverrides+=(policyKind,oldRev,_)=> {
            var curMode=modeId;var curProfile=SelectedProfileId;
            if(provenanceStore==null||string.IsNullOrEmpty(curMode)||string.IsNullOrEmpty(curProfile))return;
            var doc=ResolveCurrentDocument();
            var node=(doc["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>().FirstOrDefault(n=>(string?)n["kind"]==policyKind);
            if((string?)node?["id"] is string stepId)
                provenanceStore.MarkCarriedAcrossSwitch(curMode,curProfile,stepId,oldRev);
            UpdateSummary();
        };
        settings.RevisionSwitchPurified+=policyKind=> {
            var curMode=modeId;var curProfile=SelectedProfileId;
            var doc=ResolveCurrentDocument();
            if(provenanceStore!=null&&!string.IsNullOrEmpty(curMode)&&!string.IsNullOrEmpty(curProfile)) {
                var node=(doc["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>().FirstOrDefault(n=>(string?)n["kind"]==policyKind);
                if((string?)node?["id"] is string stepId)
                    provenanceStore.Clear(curMode,curProfile,stepId);
            }
            if(selectedPage=="strategy")OpenStrategy(commitPending:false);
            UpdateSummary();
            SelectionChanged?.Invoke();
        };
    }
    public void PurifyCurrentDraft(PurityLayer layers,bool recordUndo=true)
    {
        if(!initialized||!HasValidSelection())return;
        CaptureEditor();
        var doc=ResolveCurrentDocument();
        if(v2BuiltinMode&&buildId!=null&&modeId!=null) {
            if(recordUndo)purityUndoBuilds[buildId]=(JObject)EnsureBuildDraft(buildId).DeepClone();
            PacerPurity.PurifyDocument(doc,layers);
            var db=EnsureBuildDraft(buildId);
            UmaWorkflowLibraryModel.CaptureV2(db,modeId,doc);
            doc=UmaWorkflowLibraryModel.CompileV2(db,modeId,builtinTemplates);
            if(layers==PurityLayer.All||(layers&PurityLayer.DecisionPreferences)==PurityLayer.DecisionPreferences)
                provenanceStore?.Clear(modeId,buildId);
            suppressDirty=true;
            var prevV2=loading;loading=true;
            try{editor.LoadDocument(doc);}finally{loading=prevV2;}
            try {
                RefreshConfigurationEditor();
                foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
                if(selectedPage=="strategy")OpenStrategy(commitPending:false);
            }
            finally{suppressDirty=false;}
            if(IsActuallyDirty(buildId))dirtyBuilds.Add(buildId);else dirtyBuilds.Remove(buildId);
            UpdateSummary();
            status.Text="当前培养方案草稿已恢复官方纯净；点顶栏“保存方案”生效，保存前可随时撤销。";
            ToastRequested?.Invoke($"已将「{SelectedConfigurationLabel}」恢复为官方纯净权重（五维目标、卡组与事件配置全部保留；尚未保存）。",recordUndo,"ok");
            SelectionChanged?.Invoke();
            return;
        }
        if(recordUndo)purityUndo[Key]=(JObject)doc.DeepClone();
        PacerPurity.PurifyDocument(doc,layers);
        drafts[Key]=doc;
        if(layers==PurityLayer.All||(layers&PurityLayer.DecisionPreferences)==PurityLayer.DecisionPreferences)
            provenanceStore?.Clear(modeId,profileId);
        suppressDirty=true;
        var previous=loading;loading=true;
        try{editor.LoadDocument(doc);}finally{loading=previous;}
        try {
            RefreshConfigurationEditor();
            foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
            if(selectedPage=="strategy")OpenStrategy(commitPending:false);
        }
        finally{suppressDirty=false;}
        if(IsActuallyDirty(Key))dirty.Add(Key);else dirty.Remove(Key);
        UpdateSummary();
        status.Text="草稿已恢复官方纯净；点顶栏“保存配置”生效，保存前可随时撤销。";
        ToastRequested?.Invoke($"已将「{SelectedConfigurationLabel}」恢复为官方纯净权重（五维目标、卡组与事件配置全部保留；尚未保存）。",recordUndo,"ok");
        SelectionChanged?.Invoke();
    }
    public void UndoPurifyDraft()
    {
        if(!initialized)return;
        if(v2BuiltinMode&&buildId!=null&&modeId!=null) {
            if(!purityUndoBuilds.TryGetValue(buildId,out var snapBuild))return;
            purityUndoBuilds.Remove(buildId);
            buildDrafts[buildId]=snapBuild;
            var docV2=UmaWorkflowLibraryModel.CompileV2(snapBuild,modeId,builtinTemplates);
            suppressDirty=true;
            var prevV2=loading;loading=true;
            try{editor.LoadDocument(docV2);}finally{loading=prevV2;}
            try {
                RefreshConfigurationEditor();
                foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
                if(selectedPage=="strategy")OpenStrategy(commitPending:false);
            }
            finally{suppressDirty=false;}
            if(IsActuallyDirty(buildId))dirtyBuilds.Add(buildId);else dirtyBuilds.Remove(buildId);
            UpdateSummary();
            status.Text="已撤销纯净恢复，回到恢复前的方案草稿内容。";
            ToastRequested?.Invoke("已撤销恢复操作，还原刚才的方案草稿。",false,"info");
            SelectionChanged?.Invoke();
            return;
        }
        if(!purityUndo.TryGetValue(Key,out var snapshot))return;
        purityUndo.Remove(Key);
        drafts[Key]=snapshot;
        suppressDirty=true;
        var previous=loading;loading=true;
        try{editor.LoadDocument(snapshot);}finally{loading=previous;}
        try {
            RefreshConfigurationEditor();
            foreach(var item in settingsPages)RefreshSettingsPage(item.Host,item.Section);
            if(selectedPage=="strategy")OpenStrategy(commitPending:false);
        }
        finally{suppressDirty=false;}
        if(IsActuallyDirty(Key))dirty.Add(Key);else dirty.Remove(Key);
        UpdateSummary();
        status.Text="已撤销纯净恢复，回到恢复前的草稿内容。";
        ToastRequested?.Invoke("已撤销恢复操作，还原刚才的草稿状态。",false,"info");
        SelectionChanged?.Invoke();
    }
    public void DiscardDraft()
    {
        if(!initialized||!HasValidSelection())return;
        if(v2BuiltinMode&&buildId!=null) {
            buildDrafts.Remove(buildId);dirtyBuilds.Remove(buildId);buildNames.Remove(buildId);purityUndoBuilds.Remove(buildId);
            LoadSelection();
            status.Text="已放弃当前培养方案的未保存修改，恢复到上次保存版本。";
            ToastRequested?.Invoke("已放弃未保存修改，还原到上次保存的培养方案。",false,"info");
            return;
        }
        drafts.Remove(Key);dirty.Remove(Key);profileNames.Remove(Key);purityUndo.Remove(Key);
        LoadSelection();
        status.Text="已放弃当前配置的未保存修改，恢复到上次保存版本。";
        ToastRequested?.Invoke("已放弃未保存修改，还原到上次保存的配置。",false,"info");
    }
    public async Task OpenProfileManagerModalAsync()
    {
        if(!initialized||(!v2BuiltinMode&&Mode()==null))return;
        if(Avalonia.Controls.TopLevel.GetTopLevel(SelectorBar) is not Window owner)
        {
            OpenConfiguration();
            return;
        }
        IReadOnlyList<PacerProfileManagerWindow.ProfileItemInfo> ReadItems()
        {
            var list=new List<PacerProfileManagerWindow.ProfileItemInfo>();
            if(v2BuiltinMode) {
                foreach(var b in Builds) {
                    var bid=(string?)b["id"]??"";
                    var bname=(string?)b["name"]??"培养方案";
                    var isCur=string.Equals(bid,buildId,StringComparison.Ordinal);
                    var doc=isCur?ReadSelectedDocument():UmaWorkflowLibraryModel.CompileV2(ActiveBuild(bid)??b,modeId!,builtinTemplates);
                    var node=(doc["run_graph"]?["nodes"] as JArray)?.OfType<JObject>().FirstOrDefault(r=>(string?)r["kind"] is "career" or "cut");
                    var pol=doc["policies"]?[(string?)node?["policy"]??"default"];
                    var isCut=(string?)node?["kind"]=="cut";
                    var opt=(isCut?pol?["cut_options"]:pol?["career_options"]) as JObject;
                    var st=(isCut?opt?["cut"]:opt?["setup"]) as JObject;
                    var eq=EquipmentText(st);
                    if(careerBatchLimits!=null&&CanRepeatCareer(doc))eq+=$" · 连续 {CareerCount(doc)} 局";
                    var reports=PacerPurity.EvaluateDocument(doc,stepId=>provenanceStore?.Read(modeId,bid,stepId));
                    list.Add(new PacerProfileManagerWindow.ProfileItemInfo(bid,bname,isCur,eq,reports));
                }
                return list;
            }
            var m=Mode();if(m==null)return Array.Empty<PacerProfileManagerWindow.ProfileItemInfo>();
            foreach(var p in (m["profiles"] as JArray??new()).OfType<JObject>())
            {
                var pid=(string?)p["id"]??"";
                var pname=(string?)p["name"]??"配置";
                var key=modeId+"/"+pid;
                var isCur=string.Equals(pid,profileId,StringComparison.Ordinal);
                var doc=isCur?ReadSelectedDocument():drafts.TryGetValue(key,out var d)?d:UmaWorkflowLibraryModel.Resolve(m,p);
                var node=(doc["run_graph"]?["nodes"] as JArray)?.OfType<JObject>().FirstOrDefault(r=>(string?)r["kind"] is "career" or "cut");
                var pol=doc["policies"]?[(string?)node?["policy"]??"default"];
                var isCut=(string?)node?["kind"]=="cut";
                var opt=(isCut?pol?["cut_options"]:pol?["career_options"]) as JObject;
                var st=(isCut?opt?["cut"]:opt?["setup"]) as JObject;
                var eq=EquipmentText(st);
                if(careerBatchLimits!=null&&CanRepeatCareer(doc))eq+=$" · 连续 {CareerCount(doc)} 局";
                var reports=PacerPurity.EvaluateDocument(doc,stepId=>provenanceStore?.Read(modeId,pid,stepId));
                list.Add(new PacerProfileManagerWindow.ProfileItemInfo(pid,pname,isCur,eq,reports));
            }
            return list;
        }
        var win=new PacerProfileManagerWindow(
            v2BuiltinMode?$"培养方案（当前：{SelectedModeLabel}）":SelectedModeLabel,
            ReadItems,
            async pid=>await SwitchProfileByIdAsync(pid),
            async pid=>await DeleteProfileByIdAsync(pid),
            async name=>{saveAsName.Text=name;await CreateBlankConfigurationAsync();},
            async newName=>await RenameCurrentProfileAsync(newName),
            async (copyName,purify)=>{saveAsName.Text=copyName;saveAsPurify.IsChecked=purify;saveAsPurifyTouched=true;await SaveAsAsync();}
        );
        await win.ShowDialog(owner);
    }
    private Task SwitchProfileByIdAsync(string targetId)
    {
        if(busy||!initialized||string.IsNullOrEmpty(targetId))return Task.CompletedTask;
        if(v2BuiltinMode) {
            if(string.Equals(targetId,buildId,StringComparison.Ordinal))return Task.CompletedTask;
            if(!CommitOrdinary())throw new InvalidOperationException(string.IsNullOrWhiteSpace(status.Text)?"当前方案存在未通过校验的输入，请先修正后再切换。":status.Text);
            CaptureEditor();
            buildId=targetId;
            library["selected_build_id"]=buildId;
            LoadSelection();
            return Task.CompletedTask;
        }
        if(string.Equals(targetId,profileId,StringComparison.Ordinal))return Task.CompletedTask;
        if(!CommitOrdinary())throw new InvalidOperationException(string.IsNullOrWhiteSpace(status.Text)?"当前配置存在未通过校验的输入，请先修正后再切换。":status.Text);
        CaptureEditor();
        profileId=targetId;
        if(Mode() is JObject m)m["selected_profile_id"]=profileId;
        LoadSelection();
        return Task.CompletedTask;
    }
    private async Task RenameCurrentProfileAsync(string newName)
    {
        var trimmed=newName.Trim();
        if(trimmed.Length==0)throw new InvalidOperationException("名称不能为空。");
        if(v2BuiltinMode) {
            if(Build() is not JObject currentBuild||string.IsNullOrEmpty(buildId))return;
            if(BuildNameExists(library,trimmed,buildId))throw new InvalidOperationException("已有同名培养方案。");
            var candidate=(JObject)savedLibrary.DeepClone();
            var target=(candidate["builds"] as JArray??new()).OfType<JObject>().FirstOrDefault(b=>(string?)b["id"]==buildId);
            if(target==null)throw new InvalidOperationException("尚未保存的新培养方案请先点“保存方案”。");
            target["name"]=trimmed;
            SetBusy(true);
            try {
                var saved=await PersistLibrary(candidate);
                savedLibrary=saved;
                currentBuild["name"]=trimmed;
                if(buildDrafts.TryGetValue(buildId,out var db))db["name"]=trimmed;
                buildNames.Remove(buildId);
                RefreshPickers();
                ToastRequested?.Invoke($"已将当前培养方案重命名为「{trimmed}」。",false,"ok");
            } finally{SetBusy(false);}
            return;
        }
        if(Mode() is not JObject editingMode||Profile() is not JObject currentProfile)return;
        if(ProfileNameExists(editingMode,trimmed,profileId))throw new InvalidOperationException("当前模式下已有同名配置。");
        var candidateV1=(JObject)savedLibrary.DeepClone();
        var modesArr=(isV2?candidateV1["custom_modes"]:candidateV1["modes"]) as JArray??new JArray();
        var targetMode=modesArr.OfType<JObject>().FirstOrDefault(m=>(string?)m["id"]==modeId);
        var targetProfile=(targetMode?["profiles"] as JArray??new()).OfType<JObject>().FirstOrDefault(p=>(string?)p["id"]==profileId);
        if(targetProfile==null)throw new InvalidOperationException("尚未保存的新配置请先点“保存配置”。");
        targetProfile["name"]=trimmed;
        SetBusy(true);
        try {
            var saved=await PersistLibrary(candidateV1);
            savedLibrary=saved;
            currentProfile["name"]=trimmed;
            profileNames.Remove(Key);
            RefreshPickers();
            ToastRequested?.Invoke($"已将当前配置重命名为「{trimmed}」。",false,"ok");
        }
        finally{SetBusy(false);}
    }
    private async Task DeleteProfileByIdAsync(string targetProfileId)
    {
        if(busy||!initialized)return;
        if(v2BuiltinMode) {
            if(Builds.Count()<=1)throw new InvalidOperationException("至少需保留一套培养方案，无法删除最后一套。");
            if(string.Equals(targetProfileId,buildId,StringComparison.Ordinal)){await DeleteSelectedProfileAsync();return;}
            var deletedName=(string?)Build(targetProfileId)?["name"]??"培养方案";
            var candidate=(JObject)savedLibrary.DeepClone();
            if(!UmaWorkflowLibraryModel.RemoveBuild(candidate,targetProfileId,out _))
                throw new InvalidOperationException("指定培养方案无法删除。");
            candidate["selected_mode_id"]=modeId;
            SetBusy(true);
            try {
                var saved=await PersistLibrary(candidate);
                savedLibrary=saved;
                UmaWorkflowLibraryModel.RemoveBuild(library,targetProfileId,out _);
                buildDrafts.Remove(targetProfileId);dirtyBuilds.Remove(targetProfileId);buildNames.Remove(targetProfileId);purityUndoBuilds.Remove(targetProfileId);
                UpdateSummary();
                ToastRequested?.Invoke($"已删除培养方案「{deletedName}」。",false,"info");
            } finally{SetBusy(false);}
            return;
        }
        if(Mode() is not JObject editingMode)return;
        if((editingMode["profiles"] as JArray)?.Count<=1)
            throw new InvalidOperationException("每个育成模式至少需保留一个运行配置，无法删除最后一个配置。");
        if(string.Equals(targetProfileId,profileId,StringComparison.Ordinal))
        {
            await DeleteSelectedProfileAsync();
            return;
        }
        var targetLocal=(editingMode["profiles"] as JArray)?.OfType<JObject>().FirstOrDefault(p=>(string?)p["id"]==targetProfileId);
        var deletedNameV1=(string?)targetLocal?["name"]??"配置";
        var candidateV1=(JObject)savedLibrary.DeepClone();
        var modesArr=(isV2?candidateV1["custom_modes"]:candidateV1["modes"]) as JArray??new JArray();
        var targetMode=modesArr.OfType<JObject>().FirstOrDefault(m=>(string?)m["id"]==modeId);
        if(targetMode==null||!UmaWorkflowLibraryModel.RemoveProfile(targetMode,targetProfileId,out _))
            throw new InvalidOperationException("指定配置无法删除。");
        candidateV1["selected_mode_id"]=modeId;
        SetBusy(true);
        try {
            var saved=await PersistLibrary(candidateV1);
            savedLibrary=saved;
            UmaWorkflowLibraryModel.RemoveProfile(editingMode,targetProfileId,out _);
            var deletedKey=modeId+"/"+targetProfileId;
            drafts.Remove(deletedKey);dirty.Remove(deletedKey);profileNames.Remove(deletedKey);purityUndo.Remove(deletedKey);
            provenanceStore?.Clear(modeId,targetProfileId);
            UpdateSummary();
            ToastRequested?.Invoke($"已删除配置「{deletedNameV1}」。",false,"info");
        }
        finally{SetBusy(false);}
    }
    public async Task CreateBlankConfigurationAsync()
    {
        if(busy||!initialized||!CommitOrdinary())return;
        if(v2BuiltinMode) {
            var candidate=(JObject)savedLibrary.DeepClone();
            bool BuildNameUsed(string n)=>BuildNameExists(library,n)||BuildNameExists(candidate,n);
            var stem=string.IsNullOrWhiteSpace(saveAsName.Text)?"纯净新方案":saveAsName.Text.Trim();
            var name=stem;
            for(var idx=2;BuildNameUsed(name);idx++)name=stem+" "+idx;
            var blank=UmaWorkflowLibraryModel.CreateBlankBuild(name,newPolicyDefaultsByMode);
            var blankId=(string)blank["id"]!;
            ((JArray)(candidate["builds"]??=new JArray())).Add(blank);
            candidate["selected_build_id"]=blankId;
            candidate["selected_mode_id"]=modeId;
            SetBusy(true);
            try {
                var saved=await PersistLibrary(candidate);
                savedLibrary=(JObject)saved.DeepClone();
                library=(JObject)saved.DeepClone();
                buildId=blankId;
                saveAsName.Text="";LoadSelection();
                status.Text=$"已新建空白纯净培养方案「{name}」并切换。";
                ToastRequested?.Invoke($"已新建空白纯净培养方案「{name}」。",false,"ok");
            } catch(Exception error) {
                status.Text="新建空白方案失败："+error.Message;
            } finally{SetBusy(false);}
            return;
        }
        if(Mode() is not JObject editingMode)return;
        var candidateV1=(JObject)savedLibrary.DeepClone();
        var modesArr=(isV2?candidateV1["custom_modes"]:candidateV1["modes"]) as JArray??new JArray();
        var targetMode=modesArr.OfType<JObject>().FirstOrDefault(m=>(string?)m["id"]==modeId);
        if(targetMode==null)return;
        bool NameUsed(string n)=>ProfileNameExists(editingMode,n)||ProfileNameExists(targetMode,n);
        var stemV1=string.IsNullOrWhiteSpace(saveAsName.Text)?"纯净新配置":saveAsName.Text.Trim();
        var nameV1=stemV1;
        for(var idx=2;NameUsed(nameV1);idx++)nameV1=stemV1+" "+idx;
        var blankV1=UmaWorkflowLibraryModel.CreateBlankProfile(targetMode,nameV1,newPolicyDefaultsByMode);
        var blankIdV1=(string)blankV1["id"]!;
        ((JArray)targetMode["profiles"]!).Add(blankV1);
        targetMode["selected_profile_id"]=blankIdV1;
        candidateV1["selected_mode_id"]=modeId;
        SetBusy(true);
        try {
            var saved=await PersistLibrary(candidateV1);
            var savedArr=(isV2?saved["custom_modes"]:saved["modes"]) as JArray??new JArray();
            var savedMode=savedArr.OfType<JObject>().Single(m=>(string?)m["id"]==modeId);
            var savedBlank=(savedMode["profiles"] as JArray??new()).OfType<JObject>().Single(p=>(string?)p["id"]==blankIdV1);
            savedLibrary=saved;
            ((JArray)editingMode["profiles"]!).Add(savedBlank.DeepClone());
            profileId=blankIdV1;library["selected_mode_id"]=modeId;
            saveAsName.Text="";LoadSelection();
            status.Text=$"已新建空白纯净配置「{nameV1}」并切换。";
            ToastRequested?.Invoke($"已新建空白纯净配置「{nameV1}」。",false,"ok");
        } catch(Exception error) {
            status.Text="新建空白配置失败："+error.Message;
        } finally{SetBusy(false);}
    }
    public async Task DeleteSelectedProfileAsync()
    {
        if(busy||!initialized)return;
        if(v2BuiltinMode) {
            if(Builds.Count()<=1){status.Text="至少需保留一套培养方案，无法删除最后一套。";return;}
            if(Build() is not JObject currentBuild||string.IsNullOrEmpty(buildId))return;
            var deletedId=buildId!;
            var deletedName=SelectedConfigurationLabel;
            var candidate=(JObject)savedLibrary.DeepClone();
            if(!UmaWorkflowLibraryModel.RemoveBuild(candidate,deletedId,out var nextBid)) {
                status.Text="当前培养方案尚未保存或无法删除。";return;
            }
            candidate["selected_mode_id"]=modeId;
            SetBusy(true);
            try {
                var saved=await PersistLibrary(candidate);
                savedLibrary=(JObject)saved.DeepClone();
                UmaWorkflowLibraryModel.RemoveBuild(library,deletedId,out var nextEdBid);
                buildDrafts.Remove(deletedId);dirtyBuilds.Remove(deletedId);buildNames.Remove(deletedId);purityUndoBuilds.Remove(deletedId);
                buildId=nextEdBid??nextBid;library["selected_build_id"]=buildId;
                LoadSelection();
                status.Text=$"已删除培养方案「{deletedName}」。";
            } catch(Exception error) {
                status.Text="删除培养方案失败："+error.Message;
            } finally{SetBusy(false);}
            return;
        }
        if(Mode() is not JObject editingMode||Profile() is not JObject currentProfile)return;
        if((editingMode["profiles"] as JArray)?.Count<=1) {
            status.Text="每个育成模式至少需保留一个运行配置，无法删除最后一个配置。";return;
        }
        var deletedIdV1=(string)currentProfile["id"]!;
        var deletedNameV1=SelectedConfigurationLabel;
        var candidateV1=(JObject)savedLibrary.DeepClone();
        var modesArr=(isV2?candidateV1["custom_modes"]:candidateV1["modes"]) as JArray??new JArray();
        var targetMode=modesArr.OfType<JObject>().FirstOrDefault(m=>(string?)m["id"]==modeId);
        if(targetMode==null||!UmaWorkflowLibraryModel.RemoveProfile(targetMode,deletedIdV1,out var nextId)) {
            status.Text="当前配置尚未保存或无法删除。";return;
        }
        candidateV1["selected_mode_id"]=modeId;
        SetBusy(true);
        try {
            var saved=await PersistLibrary(candidateV1);
            savedLibrary=saved;
            UmaWorkflowLibraryModel.RemoveProfile(editingMode,deletedIdV1,out var nextEditingId);
            var deletedKey=modeId+"/"+deletedIdV1;
            drafts.Remove(deletedKey);dirty.Remove(deletedKey);profileNames.Remove(deletedKey);purityUndo.Remove(deletedKey);
            provenanceStore?.Clear(modeId,deletedIdV1);
            profileId=nextEditingId??nextId;library["selected_mode_id"]=modeId;
            LoadSelection();
            status.Text=$"已删除配置「{deletedNameV1}」。";
        } catch(Exception error) {
            status.Text="删除配置失败："+error.Message;
        } finally{SetBusy(false);}
    }
    public async Task StartSelectedAsync()
    {
        if (!CommitOrdinary())
            throw new InvalidOperationException(string.IsNullOrWhiteSpace(status.Text)
                ? "当前育成配置存在未通过校验的输入，请切到「育成配置」检查并保存后再启动。"
                : status.Text);
        var document = ReadSelectedDocument();
        var validation = await request(new JObject { ["action"] = "validate_workflow_start", ["document"] = document });
        if (validation["ok"]?.Type == JTokenType.Boolean && !(bool)validation["ok"]!)
        {
            var errors = (validation["errors"] as JArray)?.Select(x => x?.ToString()).Where(s => !string.IsNullOrWhiteSpace(s)).ToArray();
            var reason = errors is { Length: > 0 } ? string.Join("；", errors) : "当前配置未通过启动前校验。";
            status.Text = reason;
            throw new InvalidOperationException(reason);
        }
        SavedEventPreferences?.Invoke(ReadPrimaryEventPreferences());
        var response = await request(new JObject { ["action"] = "start_workflow", ["document"] = document });
        started(response);
    }
    public void UpdateRuntime(JObject value)
    {
        editor.UpdateRuntime(value);
        if (!initialized && !busy && value != null && autoRetryCount < 6
            && (DateTime.UtcNow - lastInitAttemptUtc).TotalSeconds >= 5)
        {
            autoRetryCount++;
            _ = InitializeAsync();
        }
    }
    private void SetBusy(bool value)
    {
        busy=value;foreach(var action in actions)action.IsEnabled=!value;
        retryInitButton.IsEnabled=!value;
        if(initialized)retryInitButton.IsVisible=false;
        modes.IsEnabled=profiles.IsEnabled=profileName.IsEnabled=saveAsName.IsEnabled=saveAsPurify.IsEnabled=configurationEditor.IsEnabled=careerBatchCount.IsEnabled=!value;
        deleteProfile.IsEnabled=!value&&(v2BuiltinMode?Builds.Count()>1:((Mode()?["profiles"] as JArray)?.Count??0)>1);
        strategySteps.IsEnabled=!value&&strategySteps.ItemCount>1;
    }
}
