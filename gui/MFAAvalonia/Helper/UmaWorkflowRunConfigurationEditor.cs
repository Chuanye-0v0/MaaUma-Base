using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.LogicalTree;
using Avalonia.Media;
using Newtonsoft.Json.Linq;
using UmaCutPanel;

namespace MFAAvalonia.Helper;

/// <summary>Edit the same option fields accepted by the existing career/cut owners, retaining all others.</summary>
public sealed class UmaWorkflowRunConfigurationEditor : StackPanel
{
    private readonly JObject document;
    private readonly JObject originalDocument;
    private readonly Action<JObject> changed;
    private readonly Func<JObject,Task<JObject>>? bridge;
    private readonly string? section;
    private readonly JObject? newPolicyDefaults;
    private readonly string? boundStepId;
    private readonly Action? onOpenPurityDrawer;
    private readonly Action? onPurifyCurrent;
    private readonly Func<IReadOnlyList<PurityReport>>? readPurity;
    private readonly Control? extraCycleControl;
    private readonly bool postCutContinueCareer;
    private UmaKnowledgeToolsClient? toolboxClient;
    private long toolboxEditEpoch;
    private bool building;
    private Action<JObject?>? applyScreeningTrainingFailure;
    private UmaTraineeChoice[] characters=Array.Empty<UmaTraineeChoice>();
    private UmaSupportChoice[] supports=Array.Empty<UmaSupportChoice>();
    private readonly List<UmaAttributeGoalEditor> attributeEditors=new();
    private readonly List<UmaSupportDeckInputEditor> supportDeckEditors=new();
    private readonly List<Func<bool>> handoffCommits=new();
    private readonly List<Action> refreshSummaries=new();
    private readonly List<string[]> readScaffolds=new();
    public event Action? PendingChanged;
    public event Action? SkillLearningRequested;
    public event Action? RaceScheduleRequested;
    public event Action<string,string?,string>? RevisionSwitchedWithOverrides;
    public event Action<string>? RevisionSwitchPurified;
    public event Action<bool>? CutPostActionChanged;
    public bool TryCommitPending(){foreach(var editor in attributeEditors)if(!editor.TryCommitPending())return false;foreach(var editor in supportDeckEditors)if(!editor.TryCommitPending())return false;foreach(var commit in handoffCommits)if(!commit())return false;return true;}

    public UmaWorkflowRunConfigurationEditor(JObject value,Action<JObject> onChanged,Func<JObject,Task<JObject>>? bridge=null,string? section=null,string? stepId=null,JObject? newPolicyDefaults=null,UmaKnowledgeToolsClient? toolboxClient=null,Action? onOpenPurityDrawer=null,Action? onPurifyCurrent=null,Func<IReadOnlyList<PurityReport>>? readPurity=null,Control? extraCycleControl=null,bool postCutContinueCareer=false)
    {
        Name="WorkflowRunConfigurationEditor";Spacing=12;document=(JObject)value.DeepClone();originalDocument=(JObject)value.DeepClone();changed=onChanged;this.bridge=bridge;this.section=section;this.newPolicyDefaults=newPolicyDefaults==null?null:(JObject)newPolicyDefaults.DeepClone();boundStepId=stepId;this.toolboxClient=toolboxClient;
        this.onOpenPurityDrawer=onOpenPurityDrawer;this.onPurifyCurrent=onPurifyCurrent;this.readPurity=readPurity;this.extraCycleControl=extraCycleControl;this.postCutContinueCareer=postCutContinueCareer;
        PendingChanged+=()=>toolboxEditEpoch++;
        // A rebuilt configuration owns a new editor. A response for the removed
        // editor must not be replayed into the same named configuration later.
        DetachedFromVisualTree+=(_,_)=>toolboxEditEpoch++;
        try {characters=UmaTraineePicker.Load(AppContext.BaseDirectory).ToArray();}catch(Exception){}
        try {supports=UmaSupportPicker.Load(AppContext.BaseDirectory).ToArray();}catch(Exception){}
        BuildEditors();
    }
    private void BuildEditors()
    {
        building=true;
        try {
            Children.Clear();attributeEditors.Clear();supportDeckEditors.Clear();handoffCommits.Clear();refreshSummaries.Clear();applyScreeningTrainingFailure=null;
            var nodes=(document["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>()
                .Where(n=>(string?)n["kind"] is "career" or "cut")
                .Where(n=>boundStepId==null||(string?)n["id"]==boundStepId)
                .GroupBy(n=>((string?)n["policy"]??"default")+"/"+(string?)n["kind"]).Select(g=>g.First()).ToArray();
            var compactCustomRaces=section=="custom_races";
            if(compactCustomRaces)Spacing=4;
            if(section is "ai" or "continuation" or "mid_skills")
                Children.Add(new TextBlock {Text=section switch {"ai"=>"当前配置 · AI 决策","continuation"=>"当前配置 · 续养条件","mid_skills"=>"当前配置 · 技能学习",_=>"普通设置"},FontSize=20});
            if(!compactCustomRaces && section is not ("deck" or "strategy_targets" or "cycle"))
                Children.Add(Note("修改当前配置后点上方“保存配置”；正在进行的育成沿用原局设置。"));
            var singleCustomRacesNode=compactCustomRaces&&nodes.Length==1;
            var attachedExtraCycle=false;
            foreach(var node in nodes)
            {
                BuildOptions(node,singleCustomRacesNode,useExtraCycle:!attachedExtraCycle);
                attachedExtraCycle=true;
            }
        }
        finally { building=false; }
    }
    private static TextBlock Note(string text)=>new(){Text=text,TextWrapping=TextWrapping.Wrap,FontSize=12};
    private void Emit(){if(building)return;toolboxEditEpoch++;foreach(var editor in attributeEditors)editor.RefreshContext();foreach(var refresh in refreshSummaries)refresh();var value=(JObject)document.DeepClone();RestoreReadScaffolds(value);changed(value);}
    private void RestoreReadScaffolds(JObject value)
    {
        foreach(var path in readScaffolds.AsEnumerable().Reverse()) {
            JObject? parent=value;JObject? prior=originalDocument;
            foreach(var key in path.Take(path.Length-1)){parent=parent?[key] as JObject;prior=prior?[key] as JObject;}
            var last=path[^1];if(parent?[last] is not JObject empty||empty.Count!=0)continue;
            if(prior?.Property(last)!=null)parent[last]=prior[last]!.DeepClone();else parent.Remove(last);
        }
    }
    private JObject ReadObject(JObject root,string key)
    {
        if(root[key] is JObject found)return found;
        var parts=new Stack<string>();parts.Push(key);JToken? current=root;
        while(current!=document&&current?.Parent is JProperty property){parts.Push(property.Name);current=property.Parent;}
        if(current==document)readScaffolds.Add(parts.ToArray());
        return Object(root,key);
    }
    private static JObject Object(JObject root,string key)
    {if(root[key] is not JObject)root[key]=new JObject();return (JObject)root[key]!;}
    private void BuildOptions(JObject node,bool singleCustomRacesNode=false,bool useExtraCycle=true)
    {
        var kind=(string)node["kind"]!;var id=(string?)node["policy"]??"default";
        var hasAceCycle=node["ace_cycle"] is JObject;
        var policies=ReadObject(document,"policies");var policy=ReadObject(policies,id);
        var options=ReadObject(policy,kind=="cut"?"cut_options":"career_options");
        var settings=kind=="cut"?ReadObject(options,"cut"):ReadObject(options,"setup");
        var body=new StackPanel{Spacing=12};
        var prefix="WorkflowConfig_"+id+"_"+kind+"_";
        var docNodes=(document["run_graph"]?["nodes"] as JArray??new()).OfType<JObject>().ToArray();
        var downstreamCareer=docNodes.FirstOrDefault(n=>(string?)n["kind"]=="career");
        var hasCutStep=docNodes.Any(n=>(string?)n["kind"] is "cut" or "restart");
        var requiresConcreteCharacter=kind=="cut"||node["ss_sieve"] is JObject||hasAceCycle||((int?)document["career_batch"]?["max_careers"]??1)>1;
        if(section is "deck")
        {
            var cardContent=new StackPanel{
                Spacing=12,
                Children={
                    new StackPanel{Spacing=2,Children={
                        new TextBlock{Text="马娘与支援卡组（99% 日常路径）",FontSize=16,FontWeight=FontWeight.SemiBold},
                        Note("设置角色、卡组与借卡条件；切换策略或「✦ 恢复官方策略」绝不会改动这里的配置。")
                    }}
                }
            };
            if(kind=="career"&&hasCutStep)
            {
                var reusedBanner=new Border{
                    Name=prefix+"CutReusedSetupBanner",
                    CornerRadius=new CornerRadius(8),
                    BorderThickness=new Thickness(1),
                    Background=new SolidColorBrush(Color.FromArgb(18,16,185,129)),
                    BorderBrush=new SolidColorBrush(Color.FromRgb(16,185,129)),
                    Padding=new Thickness(12,9),
                    Child=Note("出切确认后将沿用「前期培养」同一匹马与支援卡组继续养成，无需在此重复选择；请切到「前期培养」步骤调整马娘与卡组。")
                };
                cardContent.Children.Add(reusedBanner);
            }
            else
            {
                var deckCards=new WrapPanel();
                deckCards.Children.Add(Character(settings,kind,prefix,allowCurrent:!requiresConcreteCharacter));
                deckCards.Children.Add(Pick(settings,"support_deck","使用卡组",Enumerable.Range(0,11).Select(i=>i==0?"沿用当前卡组":"卡组 "+i).ToArray(),prefix));
                deckCards.Children.Add(Borrow(settings,prefix));
                var deckRequirements=new WrapPanel();
                deckRequirements.Children.Add(Text(settings,"borrow_min_level","借卡最低等级",prefix));
                deckRequirements.Children.Add(Pick(settings,"borrow_min_break","借卡最低突破",new[]{"0破","1破","2破","3破","满破"},prefix));
                cardContent.Children.Add(deckCards);
                cardContent.Children.Add(deckRequirements);
            }
            var deckDeclared=new UmaSupportDeckInputEditor(options["support_profile"],value=>{options["support_profile"]=value?.DeepClone()??JValue.CreateNull();Emit();},UmaSupportDeckInputEditor.FromChoices(supports),prefix+"SupportProfile");
            deckDeclared.PendingChanged+=()=>{if(!building)PendingChanged?.Invoke();};supportDeckEditors.Add(deckDeclared);
            var routingTip=new Border{
                CornerRadius=new CornerRadius(8),
                BorderThickness=new Thickness(1),
                Background=new SolidColorBrush(Color.FromArgb(18,59,130,246)),
                BorderBrush=new SolidColorBrush(Color.FromRgb(59,130,246)),
                Padding=new Thickness(12,9),
                Child=Note("可在此核对本套配置声明的支援卡阵容；当选用支持配卡自适应的外部策略扩展包时，开局会自动识别速/智卡数量并无缝衔接，默认开源基线（2.3）不改动配卡结构。")
            };
            cardContent.Children.Add(routingTip);
            cardContent.Children.Add(SettingsExpander(prefix+"ActualDeck","本局实际卡组（可选）",deckDeclared));
            cardContent.Children.Add(ToolboxActions(options,kind,id,(string?)node["id"]??"",prefix));
            var deckCard=new Border{Name=prefix+"DeckCard",CornerRadius=new CornerRadius(12),BorderThickness=new Thickness(1),Padding=new Thickness(16),Child=cardContent};
            deckCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
            Children.Add(deckCard);
            return;
        }
        if(section is "strategy_targets")
        {
            var strategyCardBody=new StackPanel{Spacing=12};
            var stepReport=PacerPurity.EvaluateStep((string?)node["id"]??"",(string?)node["label"]??id,kind,options);
            var isPure=stepReport.IsPure&&stepReport.State!=PurityState.EquivalentPure;
            var statusBtn=new Button{
                Name=prefix+"StrategyPurityBtn",
                Content=$"{stepReport.Emoji} {(stepReport.IsPure?(stepReport.State==PurityState.EquivalentPure?"等效纯净 · 含空壳":"官方纯净"):"已自定义 "+stepReport.EffectiveCount+" 项")} · 查看明细",
                Height=30,
                Padding=new Thickness(10,0),
                FontSize=12,
                VerticalContentAlignment=VerticalAlignment.Center
            };
            if(stepReport.State==PurityState.Stale)statusBtn.Foreground=new SolidColorBrush(Color.Parse("#DC2626"));
            else if(stepReport.State==PurityState.Custom)statusBtn.Foreground=new SolidColorBrush(Color.Parse("#D97706"));
            else if(stepReport.IsPure)statusBtn.Foreground=new SolidColorBrush(Color.Parse("#16A34A"));
            statusBtn.Click+=(_,_)=>onOpenPurityDrawer?.Invoke();

            var purifyBtn=new Button{
                Name=prefix+"StrategyPurifyBtn",
                Content=isPure?"✓ 当前已是官方纯净策略":stepReport.State==PurityState.EquivalentPure?"✦ 清理空壳恢复纯正":(string.IsNullOrWhiteSpace(stepReport.Revision)?"✦ 恢复官方策略":$"✦ 恢复 {stepReport.Revision} 官方策略"),
                IsEnabled=!isPure,
                Height=30,
                Padding=new Thickness(12,0),
                FontSize=12,
                FontWeight=FontWeight.SemiBold,
                VerticalContentAlignment=VerticalAlignment.Center
            };
            if(!isPure)purifyBtn.Classes.Add("Accent");
            purifyBtn.Click+=(_,_)=>onPurifyCurrent?.Invoke();

            refreshSummaries.Add(()=>{
                var latestReport=PacerPurity.EvaluateStep((string?)node["id"]??"",(string?)node["label"]??id,kind,options);
                var latestPure=latestReport.IsPure&&latestReport.State!=PurityState.EquivalentPure;
                statusBtn.Content=$"{latestReport.Emoji} {(latestReport.IsPure?(latestReport.State==PurityState.EquivalentPure?"等效纯净 · 含空壳":"官方纯净"):"已自定义 "+latestReport.EffectiveCount+" 项")} · 查看明细";
                statusBtn.Foreground=latestReport.State==PurityState.Stale?new SolidColorBrush(Color.Parse("#DC2626"))
                    :latestReport.State==PurityState.Custom?new SolidColorBrush(Color.Parse("#D97706"))
                    :latestReport.IsPure?new SolidColorBrush(Color.Parse("#16A34A"))
                    :new SolidColorBrush(Color.Parse("#64748B"));
                purifyBtn.Content=latestPure?"✓ 当前已是官方纯净策略":latestReport.State==PurityState.EquivalentPure?"✦ 清理空壳恢复纯正":(string.IsNullOrWhiteSpace(latestReport.Revision)?"✦ 恢复官方策略":$"✦ 恢复 {latestReport.Revision} 官方策略");
                purifyBtn.IsEnabled=!latestPure;
                if(!latestPure){if(!purifyBtn.Classes.Contains("Accent"))purifyBtn.Classes.Add("Accent");}
                else purifyBtn.Classes.Remove("Accent");
            });

            var topHead=new Grid{ColumnDefinitions=new("*,Auto"),ColumnSpacing=12};
            topHead.Children.Add(new StackPanel{Spacing=2,Children={
                new TextBlock{Text="培养策略与纯净度",FontSize=16,FontWeight=FontWeight.SemiBold},
                Note("选定版本、查看是否叠加了自定义权重，并可一键恢复本版官方策略（仅对下次新开局生效）。")
            }});
            var topActions=new WrapPanel{Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center};
            statusBtn.Margin=new Thickness(0,0,8,4);purifyBtn.Margin=new Thickness(0,0,0,4);
            topActions.Children.Add(statusBtn);topActions.Children.Add(purifyBtn);
            Grid.SetColumn(topActions,1);topHead.Children.Add(topActions);
            bool? lastHeadNarrow=null;
            topHead.SizeChanged+=(_,_)=>{
                var w=topHead.Bounds.Width;
                if(w<=0)return;
                var narrow=PacerLayoutGuard.NextNarrow(w,680,lastHeadNarrow,24);
                if(lastHeadNarrow==narrow)return;
                lastHeadNarrow=narrow;
                topHead.ColumnDefinitions=new(narrow?"*":"*,Auto");
                topHead.RowDefinitions=new(narrow?"Auto,Auto":"Auto");
                Grid.SetColumn(topActions,narrow?0:1);Grid.SetRow(topActions,narrow?1:0);
            };
            strategyCardBody.Children.Add(topHead);
            strategyCardBody.Children.Add(StrategyRevision(options,kind,prefix,splitLayout:true));
            var strategyCard=new Border{Name=prefix+"StrategyCard",CornerRadius=new CornerRadius(12),BorderThickness=new Thickness(1),Padding=new Thickness(16),Child=strategyCardBody};
            strategyCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
            Children.Add(strategyCard);

            var goalsCard=new Border{Name=prefix+"GoalsCard",CornerRadius=new CornerRadius(12),BorderThickness=new Thickness(1),Padding=new Thickness(16),Child=AttributeGoals(options,settings,kind,prefix,compactCards:true)};
            goalsCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
            Children.Add(goalsCard);
            return;
        }
        if(section is "cycle")
        {
            var cycleStack=new StackPanel{Spacing=12};
            cycleStack.Children.Add(new StackPanel{Spacing=2,Children={
                new TextBlock{Text="当前育成模式要求、连开与交接续养",FontSize=16,FontWeight=FontWeight.SemiBold},
                Note("「✦ 一键恢复官方策略」永远不会改动自动育成的连开局数、停点计划与预算设置。")
            }});
            var modeGuideText = node["ss_sieve"] is JObject
                ? "当前模式「SS筛局循环（旧版）」：第1年末未达指定五人SS次数自动放弃重开。"
                : hasAceCycle
                    ? "当前模式「筛选后养」：统一支持首年SS前筛、训练失败重开、多节点杀马线与终局属性/适性验货；门槛与预算仅对本培养方案生效。"
                    : kind == "cut" && (postCutContinueCareer || downstreamCareer != null)
                        ? "当前模式「刷切 · 出切后同局养完」：确认出切后自动停机；点确认继续即沿用同一套培养方案自动养完。"
                        : kind == "cut"
                            ? "当前模式「刷切 · 出切停机保留」：按下方刷切条件与预算重开；确认出切后直接停机并保留该局。"
                            : "当前模式「自动育成」：接管已有凯旋门育成或按当前培养方案开新局并养完（支持多节点胡度/条件监控，未达标仅暂停保留）。";
            var modeGuideBanner = new Border{
                Name=prefix+"ModeGuideBanner",
                CornerRadius=new CornerRadius(8),
                BorderThickness=new Thickness(1),
                Background=new SolidColorBrush(Color.FromArgb(18,59,130,246)),
                BorderBrush=new SolidColorBrush(Color.FromRgb(59,130,246)),
                Padding=new Thickness(12,9),
                Child=Note(modeGuideText)
            };
            cycleStack.Children.Add(modeGuideBanner);
            if(useExtraCycle&&extraCycleControl!=null)
            {
                if(extraCycleControl.Parent is Panel prevPanel)prevPanel.Children.Remove(extraCycleControl);
                else if(extraCycleControl.Parent is ContentControl prevContent)prevContent.Content=null;
                else if(extraCycleControl.Parent is Decorator prevDec)prevDec.Child=null;
                cycleStack.Children.Add(extraCycleControl);
            }
            if(node["ss_sieve"] is JObject cycleSieve)cycleStack.Children.Add(SsSieve(cycleSieve,policy,prefix));
            if(node["ace_cycle"] is JObject cycleAce)cycleStack.Children.Add(AceCycle(cycleAce,policy,prefix));
            if(kind=="cut") {
                var screenSetup=new StackPanel{Spacing=10};
                screenSetup.Children.Add(new TextBlock{Text="出切后行为、预算与重开判定",FontSize=14,FontWeight=FontWeight.SemiBold});
                var postCutPicker=new ComboBox{
                    Name=prefix+"CutPostAction",
                    ItemsSource=new[]{"出切停机保留（保留当前这匹）","出切先停，确认后同局养完"},
                    SelectedIndex=(postCutContinueCareer||downstreamCareer!=null)?1:0,
                    HorizontalAlignment=HorizontalAlignment.Stretch
                };
                postCutPicker.SelectionChanged+=(_,_)=> {
                    if(building||postCutPicker.SelectedIndex<0)return;
                    CutPostActionChanged?.Invoke(postCutPicker.SelectedIndex==1);
                };
                screenSetup.Children.Add(Field("出切后行为",postCutPicker));
                var budgets=new WrapPanel();
                foreach(var item in new[]{("max_attempts","最多尝试局数"),("max_tp_items","TP药最多用几瓶"),("max_carrots","萝卜最多消耗")})
                    budgets.Children.Add(Text(settings,item.Item1,item.Item2,prefix));
                screenSetup.Children.Add(budgets);
                screenSetup.Children.Add(Check(settings,"setup_confirmed","确认后续重开使用本配置的角色和配卡",prefix));
                var extra=new StackPanel{Spacing=10};
                extra.Children.Add(Check(settings,"restart_after_character_cut_miss","专属小切事件确认未获得后重开",prefix));
                extra.Children.Add(Check(settings,"restart_after_training_failure","明确训练失败后重开",prefix));
                extra.Children.Add(Text(settings,"fallback_interval_turns","每几回合补查",prefix));
                screenSetup.Children.Add(SettingsExpander(prefix+"CutAdvanced","刷切判定与重开偏好",extra,expanded:true));
                var screenCard=new Border{
                    Name=prefix+"CutScreenRequirements",
                    CornerRadius=new CornerRadius(10),
                    BorderThickness=new Thickness(1),
                    Padding=new Thickness(12),
                    Child=screenSetup
                };
                screenCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
                cycleStack.Children.Add(screenCard);
            }
            if(kind=="career")cycleStack.Children.Add(ManualHandoff(options,prefix));
            else if(downstreamCareer!=null)
            {
                var careerPid=(string?)downstreamCareer["policy"]??"default";
                var careerOpts=ReadObject(ReadObject(policies,careerPid),"career_options");
                cycleStack.Children.Add(ManualHandoff(careerOpts,prefix));
            }
            else cycleStack.Children.Add(CommonGroup(prefix+"ManualHandoff","人工交接",()=>"出切即停机保留",Note("此模式为刷切保留（出切即停机），无需设置后期人工交接。")));
            cycleStack.Children.Add(RaceRetry(options,settings,kind,prefix));
            if(kind=="cut")cycleStack.Children.Add(ContinuationSettings(settings,true,prefix,ordinary:true));
            else if(hasAceCycle)cycleStack.Children.Add(ContinuationSettings(options,true,prefix,ordinary:true,aceCycle:true));
            else if(kind=="career")cycleStack.Children.Add(ContinuationSettings(options,false,prefix,ordinary:true,aceCycle:false));
            if(kind=="career") {
                var cycleAdvanced=new StackPanel{Spacing=9};
                var limits=new WrapPanel();limits.Children.Add(Text(options,"failure_limit","最高训练失败率（%）",prefix));limits.Children.Add(Text(options,"max_actions","最多推进回合",prefix));cycleAdvanced.Children.Add(limits);
                cycleStack.Children.Add(Section(prefix+"AdvancedRunSettings","更多运行参数",cycleAdvanced));
            }
            var cycleCard=new Border{Name=prefix+"CycleCard",CornerRadius=new CornerRadius(12),BorderThickness=new Thickness(1),Padding=new Thickness(16),Child=cycleStack};
            cycleCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
            Children.Add(cycleCard);
            return;
        }
        if(section!=null&&section!="ordinary") {
            BuildRunEditors(body,options,settings,kind,prefix,hasAceCycle);
            if(singleCustomRacesNode)
                Children.Add(new Border{Name=prefix+"Options",Child=body});
            else
                Children.Add(SettingsExpander(prefix+"Options",(kind=="cut"?"前期刷切配置":"自动育成配置")+" · "+id,body,true));
            return;
        }
        body.Name=prefix+"CommonSettings";
        body.Children.Add(CommonGroup(prefix+"StrategyGroup","策略版本",()=>StrategySummary(options),StrategyRevision(options,kind,prefix)));
        if(node["ss_sieve"] is JObject sieve)body.Children.Add(SsSieve(sieve,policy,prefix));
        if(node["ace_cycle"] is JObject aceCycle)body.Children.Add(AceCycle(aceCycle,policy,prefix));
        if(kind=="career")body.Children.Add(ManualHandoff(options,prefix));
        else if(downstreamCareer!=null)
        {
            var careerPid=(string?)downstreamCareer["policy"]??"default";
            var careerOpts=ReadObject(ReadObject(policies,careerPid),"career_options");
            body.Children.Add(ManualHandoff(careerOpts,prefix));
        }
        else body.Children.Add(CommonGroup(prefix+"ManualHandoff","人工交接",()=>"出切即停机保留",Note("此模式为刷切保留（出切即停机），无需设置后期人工交接。")));
        body.Children.Add(RaceRetry(options,settings,kind,prefix));
        body.Children.Add(RaceScheduleLink(options,kind,prefix));
        if(kind=="cut")body.Children.Add(ContinuationSettings(settings,true,prefix,ordinary:true));
        else if(hasAceCycle)body.Children.Add(ContinuationSettings(options,true,prefix,ordinary:true,aceCycle:true));
        else if(kind=="career")body.Children.Add(ContinuationSettings(options,false,prefix,ordinary:true,aceCycle:false));
        body.Children.Add(SkillLearningLink(options,kind,prefix));
        var equipment=new StackPanel{Spacing=10};
        equipment.Children.Add(AttributeGoals(options,settings,kind,prefix));
        if(!(kind=="career"&&hasCutStep))
        {
            var cards=new WrapPanel();
            cards.Children.Add(Character(settings,kind,prefix,allowCurrent:!requiresConcreteCharacter));
            cards.Children.Add(Pick(settings,"support_deck","使用卡组",Enumerable.Range(0,11).Select(i=>i==0?"沿用当前卡组":"卡组 "+i).ToArray(),prefix));
            cards.Children.Add(Borrow(settings,prefix));equipment.Children.Add(cards);
            var requirements=new WrapPanel();
            requirements.Children.Add(Text(settings,"borrow_min_level","借卡最低等级",prefix));
            requirements.Children.Add(Pick(settings,"borrow_min_break","借卡最低突破",new[]{"0破","1破","2破","3破","满破"},prefix));equipment.Children.Add(requirements);
        }
        else
        {
            equipment.Children.Add(Note("出切确认后将沿用「前期培养」同一匹马与支援卡组继续养成，请在前期培养步骤调整马娘与卡组。"));
        }
        var declaredDeck=new UmaSupportDeckInputEditor(options["support_profile"],value=>{options["support_profile"]=value?.DeepClone()??JValue.CreateNull();Emit();},UmaSupportDeckInputEditor.FromChoices(supports),prefix+"SupportProfile");
        declaredDeck.PendingChanged+=()=>PendingChanged?.Invoke();supportDeckEditors.Add(declaredDeck);
        equipment.Children.Add(SettingsExpander(prefix+"ActualDeck","本局实际卡组（可选）",declaredDeck));
        equipment.Children.Add(ToolboxActions(options,kind,id,(string?)node["id"]??"",prefix));
        var advanced=new StackPanel{Spacing=9};
        if(kind=="career") {
            var limits=new WrapPanel();limits.Children.Add(Text(options,"failure_limit","最高训练失败率（%）",prefix));limits.Children.Add(Text(options,"max_actions","最多推进回合",prefix));advanced.Children.Add(limits);
        } else {
            var budgets=new WrapPanel();foreach(var item in new[]{("max_attempts","最多尝试局数"),("max_tp_items","TP药最多用几瓶"),("max_carrots","萝卜最多消耗")})budgets.Children.Add(Text(settings,item.Item1,item.Item2,prefix));advanced.Children.Add(budgets);
            advanced.Children.Add(Check(settings,"setup_confirmed","确认后续重开使用本配置的角色和配卡",prefix));
            var extra=new StackPanel{Spacing=10};extra.Children.Add(Check(settings,"restart_after_character_cut_miss","专属小切事件确认未获得后重开",prefix));extra.Children.Add(Check(settings,"restart_after_training_failure","明确训练失败后重开",prefix));
            extra.Children.Add(Text(settings,"fallback_interval_turns","每几回合补查",prefix));
            advanced.Children.Add(SettingsExpander(prefix+"CutAdvanced","刷切判定与重开偏好",extra));
        }
        equipment.Children.Add(Section(prefix+"AdvancedRunSettings","更多运行参数",advanced));
        body.Children.Add(CommonGroup(prefix+"Equipment","目标与配卡",()=>EquipmentSummary(options,settings),equipment));
        Children.Add(SettingsExpander(prefix+"Options",((string?)node["label"]??(kind=="cut"?"前期培养":"自动育成"))+" · 常用设置",body,true));
    }
    private Control ManualHandoff(JObject options,string prefix)
    {
        var body=new StackPanel{Spacing=8};var current=options["manual_handoff"] as JObject;
        var first=new CheckBox{Name=prefix+"ManualFirstArc",Content="首次凯旋门前停下，手动学技能",IsChecked=(bool?)current?["first_arc"]==true};
        var final=new CheckBox{Name=prefix+"ManualFinalArc",Content="末次凯旋门前交还操作",IsChecked=(bool?)current?["final_arc"]==true};
        first.IsCheckedChanged+=(_,_)=>{Object(options,"manual_handoff")["first_arc"]=first.IsChecked==true;Emit();};
        final.IsCheckedChanged+=(_,_)=>{Object(options,"manual_handoff")["final_arc"]=final.IsChecked==true;Emit();};
        body.Children.Add(first);body.Children.Add(final);
        var dates=new UmaCareerPauseDates{Name=prefix+"ManualPauseDates"};var status=Note("");
        try{dates.Load(current?["dates"] as JArray);}catch(FormatException error){dates.IsEnabled=false;status.Text=error.Message+" 原日期设置保留；本页仍可调整两个凯旋门停点。";}
        var projection=dates.IsEnabled?dates.Read():new JArray();var dirty=false;
        bool CommitDates(){
            if(!dirty)return true;
            JArray next;try{next=dates.Read();}catch(FormatException error){status.Text=error.Message;return false;}
            if(!JToken.DeepEquals(projection,next)){Object(options,"manual_handoff")["dates"]=next.DeepClone();projection=next;Emit();}
            dirty=false;status.Text="日期停点已修改到当前配置草稿，请保存配置。";return true;
        }
        dates.Changed+=()=>{dirty=true;PendingChanged?.Invoke();CommitDates();};handoffCommits.Add(CommitDates);
        body.Children.Add(dates);body.Children.Add(status);
        body.Children.Add(Note("随当前命名配置保存，为对应自动育成步骤设置交接计划。首次停点由你处理后继续；末次交还后不自动恢复。正在运行的局请在运行总览→本局暂停计划修改，已确认的停点不会因配置编辑而重置。"));
        return CommonGroup(prefix+"ManualHandoff","人工交接",()=>HandoffSummary(options),body);
    }
    private Control CommonGroup(string name,string title,Func<string> summary,Control content)
    {
        var label=new TextBlock{Text=title,FontSize=14,FontWeight=FontWeight.SemiBold,VerticalAlignment=VerticalAlignment.Center};
        var value=new TextBlock{Name=name+"Summary",TextWrapping=TextWrapping.NoWrap,TextTrimming=TextTrimming.CharacterEllipsis,FontSize=12,Opacity=.75,VerticalAlignment=VerticalAlignment.Center};
        void Refresh(){value.Text=summary();ToolTip.SetTip(value,value.Text);}refreshSummaries.Add(Refresh);Refresh();
        var header=new Grid{ColumnDefinitions=new("Auto,*"),ColumnSpacing=12,MinHeight=36};header.Children.Add(label);Grid.SetColumn(value,1);header.Children.Add(value);
        return SettingsExpander(name,header,content);
    }
    private static Expander SettingsExpander(string name,object header,Control content,bool expanded=false)
    {
        var expander=new Expander{Name=name,Header=header,Content=content,IsExpanded=expanded,
            HorizontalContentAlignment=HorizontalAlignment.Stretch};
        expander.TemplateApplied+=(_,args)=> {
            // Suki overlays the header and content with a fixed 48px offset.
            // Reserve the actual header height, including theme padding and divider.
            if(args.NameScope.Find<Control>("PART_ToggleTransform") is not { } frame ||
                args.NameScope.Find<Control>("PART_ContentExpand") is not { } presenter ||
                args.NameScope.Find<ToggleButton>("PART_ToggleButton") is not { } toggle)return;
            var icon=args.NameScope.Find<Control>("PART_Icon");
            double lastHeaderH=-1, lastMaxW=-1;
            void ReflowHeader() {
                if(frame.Bounds.Height>0) {
                    var targetTop=Math.Ceiling(frame.Bounds.Height)+6;
                    if(PacerLayoutGuard.ChangedBeyond(targetTop,lastHeaderH,2.0)) {
                        lastHeaderH=targetTop;
                        presenter.Margin=new Thickness(0,targetTop,0,0);
                    }
                }
                if(header is not Grid row||toggle.Bounds.Width<=0)return;
                var reserved=toggle.Padding.Left+toggle.Padding.Right+Math.Max(20,icon?.DesiredSize.Width??0);
                var targetMaxW=Math.Max(120,Math.Floor(toggle.Bounds.Width-reserved));
                if(PacerLayoutGuard.ChangedBeyond(targetMaxW,lastMaxW,2.0)) {
                    lastMaxW=targetMaxW;
                    row.MaxWidth=targetMaxW;
                }
            }
            frame.SizeChanged+=(_,_)=>ReflowHeader();
            toggle.SizeChanged+=(_,_)=>ReflowHeader();
            ReflowHeader();
        };
        return expander;
    }
    private JObject? DisplayStrategyPolicy(JToken? policy)
        => policy?.Type == JTokenType.Null ? newPolicyDefaults : policy as JObject;

    private string StrategySummary(JObject options)
    {
        var policy=DisplayStrategyPolicy(options["larc_policy"]);
        return policy?["revision"]?.ToString() is string revision&&revision.Length>0?"当前版本 "+revision:"沿用原策略";
    }
    private Control SsSieve(JObject sieve,JObject policy,string prefix)
    {
        prefix+="SsSieve_";
        var debut=new CheckBox{Name=prefix+"PreDebut",Content="出道前至少完成一次五人SS",IsChecked=(bool?)sieve["pre_debut"]==true};
        var minimum=new TextBox{Name=prefix+"Year1Min",Text=sieve["year1_min"]?.ToString(),Width=110};
        var readyFive=new CheckBox{Name=prefix+"AllowReadyFive",Content="年末恰少一次且五人SS已满员也通过",IsChecked=(bool?)sieve["allow_ready_five"]==true};
        var status=Note("");
        bool changingPreset=false,countDirty=false;
        bool CommitCount()
        {
            if(!countDirty)return true;
            if(!int.TryParse(minimum.Text,out var value)||value<1||value>24){status.Text="年末次数请填1—24的整数。";return false;}
            sieve["year1_min"]=value;
            countDirty=false;status.Text="筛局条件已更新，请保存配置。";Emit();
            return true;
        }
        minimum.PropertyChanged+=(_,e)=>{if(!changingPreset&&e.Property==TextBox.TextProperty){countDirty=true;PendingChanged?.Invoke();}};
        minimum.LostFocus+=(_,_)=>CommitCount();
        handoffCommits.Add(CommitCount);
        debut.IsCheckedChanged+=(_,_)=>{sieve["pre_debut"]=debut.IsChecked==true;Emit();};
        readyFive.IsCheckedChanged+=(_,_)=>{if(!changingPreset){sieve["allow_ready_five"]=readyFive.IsChecked==true;Emit();}};
        Button Preset(string name,string label,int count,bool allowReady)
        {
            var button=new Button{Name=prefix+name,Content=label,Margin=new Thickness(0,0,6,4)};
            button.Click+=(_,_)=> {
                changingPreset=true;
                minimum.Text=count.ToString();readyFive.IsChecked=allowReady;
                changingPreset=false;countDirty=false;
                sieve["year1_min"]=count;sieve["allow_ready_five"]=allowReady;
                status.Text="筛局条件已更新，请保存配置。";Emit();
            };
            return button;
        }
        var shortcuts=new WrapPanel{Children={Preset("Pure4","纯4",4,false),Preset("Standard4","标准4（差一次且满员也通过）",4,true),Preset("Strict5","强筛5",5,false)}};
        var settings=ReadObject(ReadObject(policy,"cut_options"),"cut");
        var budgets=new WrapPanel{Children={Text(settings,"max_attempts","最多尝试局数",prefix),Text(settings,"max_tp_items","TP药最多用几瓶",prefix),Text(settings,"max_carrots","萝卜最多消耗",prefix)}};
        var body=new StackPanel{Spacing=8,Children={debut,shortcuts,Field("初级年末至少完成的五人SS次数（1—24）",minimum),readyFive,status,
            Note("判定不明时继续原局。确认未达标会在局数和TP资源限额内弃养重开。首个正常结算、达到限额或实际失败后结束。请在目标与配卡中选择具体角色和衣装。"),
            Note("提示：当前循环模式暂不启用「黄帽低体选2重开」与「训练失败重开」（将在下期统一重开授权后开放）。"),budgets}};
        return CommonGroup(prefix+"Group","SS筛局",()=>
            ((bool?)sieve["pre_debut"]==true?"出道前检查 · ":"")+"年末至少 "+sieve["year1_min"]+" 次"+
            ((bool?)sieve["allow_ready_five"]==true?" · 差一次且满员也通过":""),body);
    }
    private Control AceCycle(JObject ace,JObject policy,string prefix)
    {
        prefix+="AceCycle_";
        var grad=ReadObject(ace,"graduation");
        var minStats=ReadObject(grad,"min_stats");
        var distances=ReadObject(grad,"distances");
        var surfaces=ReadObject(grad,"surfaces");
        var targetCount=new NumericUpDown{Name=prefix+"TargetGraduates",Minimum=1,Maximum=50,Increment=1,FormatString="0",Value=Math.Clamp((int?)ace["target_graduates"]??1,1,50),MinWidth=130,Height=34,MinHeight=0};
        var gradEnabled=new CheckBox{Name=prefix+"GraduationEnabled",Content="启用终局毕业验货门槛（不达标则继续下一把）",IsChecked=(bool?)grad["enabled"]==true};
        var sumBox=new TextBox{Name=prefix+"MinStatSum",Text=grad["min_stat_sum"]?.Type is JTokenType.Integer?(string?)grad["min_stat_sum"]!.ToString():"",MinWidth=120,Watermark="可留空 (0~15000)"};
        var statKeys=new[]{("speed","速度"),("stamina","耐力"),("power","力量"),("guts","毅力"),("wisdom","智力")};
        var statBoxes=statKeys.ToDictionary(pair=>pair.Item1,pair=>new TextBox{Name=prefix+"Min_"+pair.Item1,Text=minStats[pair.Item1]?.Type is JTokenType.Integer?minStats[pair.Item1]!.ToString():"",Width=92,Watermark="留空"});
        var firstDist=distances.Properties().FirstOrDefault();
        var distEnabled=new CheckBox{Name=prefix+"DistanceEnabled",Content="要求终局距离适性",IsChecked=firstDist!=null};
        var distChoices=new[]{"short","mile","medium","long"};
        var distPicker=new ComboBox{Name=prefix+"DistanceKey",ItemsSource=new[]{"短距离","英里","中距离","长距离"},SelectedIndex=firstDist!=null?Math.Max(0,Array.IndexOf(distChoices,firstDist.Name)):2,MinWidth=110};
        var rankChoices=new[]{"G","F","E","D","C","B","A","S"};
        var rankPicker=new ComboBox{Name=prefix+"DistanceRank",ItemsSource=rankChoices,SelectedIndex=firstDist!=null?Math.Max(0,Array.IndexOf(rankChoices,firstDist.Value?.ToString())):7,MinWidth=85};
        var firstSurf=surfaces.Properties().FirstOrDefault();
        var surfEnabled=new CheckBox{Name=prefix+"SurfaceEnabled",Content="要求终局场地适性",IsChecked=firstSurf!=null};
        var surfChoices=new[]{"turf","dirt"};
        var surfPicker=new ComboBox{Name=prefix+"SurfaceKey",ItemsSource=new[]{"草地","泥地"},SelectedIndex=firstSurf!=null?Math.Max(0,Array.IndexOf(surfChoices,firstSurf.Name)):0,MinWidth=100};
        var surfRankPicker=new ComboBox{Name=prefix+"SurfaceRank",ItemsSource=rankChoices,SelectedIndex=firstSurf!=null?Math.Max(0,Array.IndexOf(rankChoices,firstSurf.Value?.ToString())):6,MinWidth=85};
        var status=Note("");
        var dirty=false;
        bool CommitAce()
        {
            if(!dirty)return true;
            if(targetCount.Value is not decimal tVal||tVal!=decimal.Truncate(tVal)||tVal<1||tVal>50){status.Text="目标成品数量请填 1～50 的整数。";return false;}
            var nextGrad=new JObject{["enabled"]=gradEnabled.IsChecked==true};
            var nextStats=new JObject();
            foreach(var pair in statKeys) {
                var raw=statBoxes[pair.Item1].Text?.Trim()??"";
                if(raw.Length==0)continue;
                if(!int.TryParse(raw,out var sv)||sv<0||sv>3000){status.Text=$"{pair.Item2}最低门槛请填 0～3000 的整数或留空。";return false;}
                nextStats[pair.Item1]=sv;
            }
            nextGrad["min_stats"]=nextStats;
            var rawSum=sumBox.Text?.Trim()??"";
            if(rawSum.Length>0) {
                if(!int.TryParse(rawSum,out var sumVal)||sumVal<0||sumVal>15000){status.Text="五维总和门槛请填 0～15000 的整数或留空。";return false;}
                nextGrad["min_stat_sum"]=sumVal;
            } else nextGrad["min_stat_sum"]=JValue.CreateNull();
            var nextDist=new JObject();
            if(distEnabled.IsChecked==true) {
                var dk=distChoices[Math.Clamp(distPicker.SelectedIndex,0,distChoices.Length-1)];
                var rk=rankChoices[Math.Clamp(rankPicker.SelectedIndex,0,rankChoices.Length-1)];
                nextDist[dk]=rk;
            }
            nextGrad["distances"]=nextDist;
            var nextSurf=new JObject();
            if(surfEnabled.IsChecked==true) {
                var sk=surfChoices[Math.Clamp(surfPicker.SelectedIndex,0,surfChoices.Length-1)];
                var srk=rankChoices[Math.Clamp(surfRankPicker.SelectedIndex,0,rankChoices.Length-1)];
                nextSurf[sk]=srk;
            }
            nextGrad["surfaces"]=nextSurf;
            if((bool)nextGrad["enabled"]!&&nextStats.Count==0&&rawSum.Length==0&&nextDist.Count==0&&nextSurf.Count==0) {
                status.Text="启用毕业验货后请至少填写一项五维、距离或场地适性门槛。";return false;
            }
            ace["target_graduates"]=(int)tVal;
            ace["graduation"]=nextGrad;
            dirty=false;status.Text="筛选后养与毕业门槛已更新，请保存配置。";Emit();
            return true;
        }
        var statPanel=new WrapPanel();
        foreach(var pair in statKeys)statPanel.Children.Add(Field(pair.Item2+"最低",statBoxes[pair.Item1]));
        var distRow=new WrapPanel{Children={distEnabled,distPicker,rankPicker}};
        distEnabled.Margin=new Thickness(0,0,8,4);distPicker.Margin=new Thickness(0,0,8,4);
        var surfRow=new WrapPanel{Children={surfEnabled,surfPicker,surfRankPicker}};
        surfEnabled.Margin=new Thickness(0,0,8,4);surfPicker.Margin=new Thickness(0,0,8,4);
        var gradPanel=new StackPanel{Spacing=8,Children={gradEnabled,statPanel,Field("五维总和最低",sumBox),distRow,surfRow}};
        void RefreshGradEnabled() {
            var on=gradEnabled.IsChecked==true;
            sumBox.IsEnabled=on; foreach(var b in statBoxes.Values)b.IsEnabled=on;
            distEnabled.IsEnabled=on; distPicker.IsEnabled=on&&distEnabled.IsChecked==true; rankPicker.IsEnabled=on&&distEnabled.IsChecked==true;
            surfEnabled.IsEnabled=on; surfPicker.IsEnabled=on&&surfEnabled.IsChecked==true; surfRankPicker.IsEnabled=on&&surfEnabled.IsChecked==true;
        }
        void Changed(bool commitNow=false) {
            dirty=true;RefreshGradEnabled();PendingChanged?.Invoke();
            if(commitNow)CommitAce();
        }
        targetCount.ValueChanged+=(_,_)=>Changed(true);
        gradEnabled.IsCheckedChanged+=(_,_)=>Changed(true);
        distEnabled.IsCheckedChanged+=(_,_)=>Changed(true);
        distPicker.SelectionChanged+=(_,_)=>Changed(true);
        rankPicker.SelectionChanged+=(_,_)=>Changed(true);
        surfEnabled.IsCheckedChanged+=(_,_)=>Changed(true);
        surfPicker.SelectionChanged+=(_,_)=>Changed(true);
        surfRankPicker.SelectionChanged+=(_,_)=>Changed(true);
        sumBox.PropertyChanged+=(_,e)=>{if(e.Property==TextBox.TextProperty)Changed(false);};
        sumBox.LostFocus+=(_,_)=>CommitAce();
        foreach(var b in statBoxes.Values) {
            b.PropertyChanged+=(_,e)=>{if(e.Property==TextBox.TextProperty)Changed(false);};
            b.LostFocus+=(_,_)=>CommitAce();
        }
        handoffCommits.Add(CommitAce);
        RefreshGradEnabled();

        // Embedded SS Sieve controls inside ace_cycle["ss_sieve"]
        var existingSs=ace["ss_sieve"] as JObject;
        var ssEnabled=new CheckBox{Name=prefix+"SsSieveEnabled",Content="嵌入首年五人SS前筛（第1年末未达标直接放弃重开）",IsChecked=existingSs!=null};
        var ssDebut=new CheckBox{Name=prefix+"SsPreDebut",Content="出道前至少完成一次五人SS",IsChecked=(bool?)existingSs?["pre_debut"]==true};
        var ssMin=new TextBox{Name=prefix+"SsYear1Min",Text=existingSs?["year1_min"]?.ToString()??"4",Width=110};
        var ssReady=new CheckBox{Name=prefix+"SsAllowReadyFive",Content="年末恰少一次且五人SS已满员也通过",IsChecked=(bool?)existingSs?["allow_ready_five"]==true};
        var ssStatus=Note("");
        bool changingSs=false, ssCountDirty=false;
        JObject EnsureAceSs()=>ace["ss_sieve"] as JObject ?? (JObject)(ace["ss_sieve"]=new JObject{["pre_debut"]=ssDebut.IsChecked==true,["year1_min"]=int.TryParse(ssMin.Text,out var v)&&v>=1&&v<=24?v:4,["allow_ready_five"]=ssReady.IsChecked==true});
        bool CommitSsCount() {
            if(!ssCountDirty||ssEnabled.IsChecked!=true)return true;
            if(!int.TryParse(ssMin.Text,out var v)||v<1||v>24){ssStatus.Text="首年SS年末次数请填 1～24 的整数。";return false;}
            EnsureAceSs()["year1_min"]=v;ssCountDirty=false;ssStatus.Text="";Emit();return true;
        }
        Button SsPreset(string name,string label,int count,bool allowReady) {
            var b=new Button{Name=prefix+"Ss_"+name,Content=label,Margin=new Thickness(0,0,6,4)};
            b.Click+=(_,_)=>{
                changingSs=true;
                ssEnabled.IsChecked=true;ssMin.Text=count.ToString();ssReady.IsChecked=allowReady;
                changingSs=false;ssCountDirty=false;
                var target=EnsureAceSs();target["year1_min"]=count;target["allow_ready_five"]=allowReady;
                ssStatus.Text="";Emit();
            };
            return b;
        }
        var ssControls=new StackPanel{Spacing=6,IsVisible=existingSs!=null,Children={
            ssDebut,
            new WrapPanel{Children={SsPreset("Pure4","纯4",4,false),SsPreset("Standard4","标准4（差一次且满员也通过）",4,true),SsPreset("Strict5","强筛5",5,false)}},
            Field("初级年末至少完成的五人SS次数（1—24）",ssMin),
            ssReady,ssStatus
        }};
        ssEnabled.IsCheckedChanged+=(_,_)=>{
            if(changingSs)return;
            var on=ssEnabled.IsChecked==true;
            ssControls.IsVisible=on;
            if(on)EnsureAceSs();
            else { ace["ss_sieve"]=JValue.CreateNull(); ssCountDirty=false; }
            Emit();
        };
        ssDebut.IsCheckedChanged+=(_,_)=>{if(!changingSs&&ssEnabled.IsChecked==true){EnsureAceSs()["pre_debut"]=ssDebut.IsChecked==true;Emit();}};
        ssReady.IsCheckedChanged+=(_,_)=>{if(!changingSs&&ssEnabled.IsChecked==true){EnsureAceSs()["allow_ready_five"]=ssReady.IsChecked==true;Emit();}};
        ssMin.PropertyChanged+=(_,e)=>{if(!changingSs&&e.Property==TextBox.TextProperty){ssCountDirty=true;PendingChanged?.Invoke();}};
        ssMin.LostFocus+=(_,_)=>CommitSsCount();
        handoffCommits.Add(CommitSsCount);

        // Training failure auto-restart controls inside ace
        bool syncingTf=false;
        var tfRestart=new CheckBox{Name=prefix+"TfRestart",Content="训练失败时自动弃养重开（适合凹上限）",IsChecked=(bool?)ace["restart_after_training_failure"]==true};
        var tfIgnoreWisdom=new CheckBox{Name=prefix+"TfIgnoreWisdom",Content="容忍智力训练失败（仅体力不足失败不算致命，推荐开启）",IsChecked=(bool?)ace["training_failure_ignore_wisdom"]!=false,Margin=new Thickness(18,0,0,0)};
        var tfStopSenior=new CheckBox{Name=prefix+"TfStopSenior",Content="进入第3年远征（7月上半）后停止因训练失败弃养（保底养完，推荐开启）",IsChecked=(bool?)ace["training_failure_stop_at_senior_expedition"]!=false,Margin=new Thickness(18,0,0,0)};
        void RefreshTfEnabled(){var on=tfRestart.IsChecked==true;tfIgnoreWisdom.IsEnabled=on;tfStopSenior.IsEnabled=on;}
        RefreshTfEnabled();
        void WriteTf(){
            if(syncingTf)return;
            ace["restart_after_training_failure"]=tfRestart.IsChecked==true;
            ace["training_failure_ignore_wisdom"]=tfIgnoreWisdom.IsChecked==true;
            ace["training_failure_stop_at_senior_expedition"]=tfStopSenior.IsChecked==true;
            RefreshTfEnabled();Emit();
        }
        tfRestart.IsCheckedChanged+=(_,_)=>WriteTf();
        tfIgnoreWisdom.IsCheckedChanged+=(_,_)=>WriteTf();
        tfStopSenior.IsCheckedChanged+=(_,_)=>WriteTf();
        applyScreeningTrainingFailure=tf=>{
            if(tf==null)return;
            syncingTf=true;
            var en=(bool?)tf["enabled"]==true;
            var iw=(bool?)tf["ignore_wisdom"]!=false;
            var ss=(bool?)tf["stop_at_senior_expedition"]!=false;
            tfRestart.IsChecked=en;tfIgnoreWisdom.IsChecked=iw;tfStopSenior.IsChecked=ss;
            syncingTf=false;
            ace["restart_after_training_failure"]=en;
            ace["training_failure_ignore_wisdom"]=iw;
            ace["training_failure_stop_at_senior_expedition"]=ss;
            RefreshTfEnabled();Emit();
        };
        var tfPanel=new StackPanel{Spacing=4,Children={tfRestart,tfIgnoreWisdom,tfStopSenior}};

        var settings=ReadObject(ReadObject(policy,"cut_options"),"cut");
        var budgets=new WrapPanel{Children={Text(settings,"max_attempts","最多尝试局数",prefix),Text(settings,"max_tp_items","TP药最多用几瓶",prefix),Text(settings,"max_carrots","萝卜最多消耗",prefix)}};
        var body=new StackPanel{Spacing=10,Children={
            Field("目标毕业成品数（1～50 只）",targetCount),budgets,
            new Border{CornerRadius=new CornerRadius(8),BorderThickness=new Thickness(1),BorderBrush=Brushes.Gray,Padding=new Thickness(10),Child=new StackPanel{Spacing=6,Children={ssEnabled,ssControls}}},
            new Border{CornerRadius=new CornerRadius(8),BorderThickness=new Thickness(1),BorderBrush=Brushes.Gray,Padding=new Thickness(10),Child=tfPanel},
            gradPanel,status,
            Note("支持首年SS前筛、训练失败自动重开、多节点杀马线与终局属性/距离/场地验货；未识别适性或属性时保底保留马匹。")}};
        return CommonGroup(prefix+"Group","筛选后养 · 前筛 / 杀马重开与毕业门槛",()=> {
            var targetNum=ace["target_graduates"]?.ToString()??"1";
            var gEnabled=(bool?)ace["graduation"]?["enabled"]==true;
            var parts=new List<string>{"目标 "+targetNum+" 只"};
            if(ace["ss_sieve"] is JObject sObj)parts.Add("SS前筛"+(sObj["year1_min"]?.ToString()??"4")+"次");
            if((bool?)ace["restart_after_training_failure"]==true)parts.Add("失败重开");
            parts.Add(gEnabled?"毕业验货已开":"完整养完即毕业");
            return string.Join(" · ",parts);
        },body);
    }
    private Control RaceRetry(JObject options,JObject settings,string kind,string prefix)
    {
        var saved=options["race_retry"] as JObject;
        var legacy=saved==null&&(kind=="career"||kind=="cut"&&(bool?)settings["debut_clock_retry"]==true);
        var enabled=new CheckBox{Name=prefix+"RaceRetryEnabled",Content="启用比赛失败重赛（使用已有闹钟）",IsChecked=saved==null?legacy:(bool?)saved["enabled"]==true};
        var count=new NumericUpDown{Name=prefix+"RaceRetryMaxClockUses",Minimum=1,Maximum=5,Increment=1,FormatString="0",Value=saved?["max_clock_uses"]?.Type==JTokenType.Integer?(int)saved["max_clock_uses"]!:3,Height=34,MinHeight=0,Width=130};
        var status=Note(saved==null?"当前沿用旧版重赛设置；编辑此处后才按整局闹钟预算生效。":"");
        var body=new StackPanel{Spacing=8,Children={enabled,Field("本局育成最多使用闹钟（1—5次）",count),Note("整局累计，恢复或切换培养步骤不会重置。免费重赛不计入闹钟次数；只使用已有资源，不购买。"),status}};
        var dirty=false;
        bool Commit(){
            if(!dirty)return true;
            if(count.Value is not decimal value||value!=decimal.Truncate(value)||value<1||value>5){status.Text="请输入1—5之间的整数；当前输入保留。";return false;}
            var target=Object(options,"race_retry");target["enabled"]=enabled.IsChecked==true;target["max_clock_uses"]=(int)value;
            dirty=false;status.Text="已修改到当前配置草稿，请保存配置。";Emit();return true;
        }
        void Changed(){dirty=true;PendingChanged?.Invoke();Commit();}
        enabled.IsCheckedChanged+=(_,_)=>Changed();count.ValueChanged+=(_,_)=>Changed();handoffCommits.Add(Commit);
        string Summary(){
            if(options["race_retry"] is not JObject current)return legacy?(kind=="career"?"沿用旧设置 · 原育成重赛已开":"沿用旧设置 · 原新人赛重赛已开"):"沿用旧设置";
            return (bool?)current["enabled"]==true?"已开启 · 整局最多 "+(current["max_clock_uses"]?.ToString()??"未填")+" 次闹钟":"关闭";
        }
        return CommonGroup(prefix+"RaceRetry","失败重赛",Summary,body);
    }
    private Control RaceScheduleLink(JObject options,string kind,string prefix)
    {
        var summary=Note("");summary.Name=prefix+"RaceScheduleLinkSummary";
        void Refresh(){summary.Text=UmaCustomRaceCalendarEditor.FormatSummary(options,kind);}
        refreshSummaries.Add(Refresh);Refresh();
        var labels=new StackPanel{Spacing=3,Children={new TextBlock{Text="比赛跑法与赛程",FontSize=14,FontWeight=FontWeight.SemiBold},summary}};
        var open=new Button{Name=prefix+"OpenRaceSchedule",Content="前往「比赛跑法与赛程」设置",Height=34,Padding=new Thickness(12,5),VerticalAlignment=VerticalAlignment.Center};
        open.Click+=(_,_)=>RaceScheduleRequested?.Invoke();
        var row=new Grid{Name=prefix+"RaceScheduleEntry",ColumnDefinitions=new("*,Auto"),ColumnSpacing=16,Margin=new Thickness(16,6)};
        row.Children.Add(labels);Grid.SetColumn(open,1);row.Children.Add(open);return row;
    }
    private static string HandoffSummary(JObject options)
    {
        if(options["manual_handoff"] is not JObject handoff)return "未设置停点";
        string Flag(string key)=>handoff[key]?.Type==JTokenType.Boolean?((bool)handoff[key]! ?"开":"关"):"沿用";
        var dates=(handoff["dates"] as JArray)?.Count??0;
        return "首次凯旋门 "+Flag("first_arc")+" · 末次凯旋门 "+Flag("final_arc")+" · 日期 "+dates+" 条";
    }
    private static string EquipmentSummary(JObject options,JObject settings)
    {
        var name=(settings["character_name"]??settings["validation_character_name"]??settings["character_id"])?.ToString();
        if(string.IsNullOrWhiteSpace(name)||name=="current")name="沿用当前角色";
        var deck=settings["support_deck"]?.ToString();deck=string.IsNullOrEmpty(deck)||deck=="0"?"沿用当前卡组":"卡组 "+deck;
        return name+" · "+deck+(options["targets"] is JObject target?" · 速度目标 "+(target["speed"]?.ToString()??"未填"):" · 沿用原目标");
    }
    private Control SkillLearningLink(JObject options,string kind,string prefix)
    {
        var summary=Note("");summary.Name=prefix+"SkillListSummary";
        void Refresh(){var list=options["early_skill_allowlist"];var count=list is JArray rows?rows.Count:list?.Type==JTokenType.String?(list.ToString().Split('\n').Count(v=>!string.IsNullOrWhiteSpace(v))):0;
            var mid=options["mid_skill_learning"] as JObject;var enabled=mid==null?kind!="cut"&&(bool?)options["with_ramonu"]==true&&count>0:(bool?)mid["enabled"]==true;
            var arc=options["arc_policy"] as JObject;var arcMode=(string?)arc?["mode"];var cmLabel=kind!="cut"&&(bool?)arc?["enabled"]==true?(string.Equals(arcMode,"tiered_ladder",StringComparison.Ordinal)?" · 自定义技能梯队:开":((bool?)arc?["auto_cm"]!=false?" · CM智能选技:开":" · CM白名单:开")):"";
            summary.Text=count+" 个技能 · 学习"+(enabled?"已启用":"未启用")+cmLabel;}refreshSummaries.Add(Refresh);Refresh();
        var labels=new StackPanel{Spacing=3,Children={new TextBlock{Text="技能清单",FontSize=14,FontWeight=FontWeight.SemiBold},summary}};
        var open=new Button{Name=prefix+"OpenSkillLearning",Content="打开技能学习",Height=34,Padding=new Thickness(12,5),VerticalAlignment=VerticalAlignment.Center};open.Click+=(_,_)=>SkillLearningRequested?.Invoke();
        var row=new Grid{Name=prefix+"SkillListEntry",ColumnDefinitions=new("*,Auto"),ColumnSpacing=16,Margin=new Thickness(16,6)};row.Children.Add(labels);Grid.SetColumn(open,1);row.Children.Add(open);return row;
    }
    private Control ToolboxActions(JObject options,string kind,string policyId,string stepId,string prefix)
    {
        var body=new StackPanel{Spacing=5};var buttons=new WrapPanel();var message=Note("在工具箱选好目标、卡组或技能后保存供面板取回；取回只形成当前步骤草稿，最后仍需保存配置。");
        var send=new Button{Name=prefix+"ToolboxExport",Content="带本配置到工具箱",Margin=new Thickness(0,0,8,0)};
        var take=new Button{Name=prefix+"ToolboxImport",Content="取回工具箱草稿"};
        string? contextId=null;JObject? captured=null;long capturedEpoch=0;
        send.Click+=async(_,_)=>{
            if(!TryCommitPending()){message.Text="请先修正当前输入；未带出旧值。";return;}send.IsEnabled=false;
            var snapshot=(JObject)options.DeepClone();var at=toolboxEditEpoch;
            try {
                toolboxClient??=new();var id=await toolboxClient.ImportAsync(UmaKnowledgeToolsContext.Run(snapshot,kind,policyId,stepId));
                if(at!=toolboxEditEpoch||!JToken.DeepEquals(snapshot,options)){message.Text="配置在等待期间已修改，请重新带入；当前输入保留。";return;}
                contextId=id;captured=snapshot;capturedEpoch=at;await toolboxClient.OpenAsync(id);message.Text="本配置已带入本机工具箱；未提供的等级、技能和卡位保持未知。";
            } catch(Exception error){message.Text="带入未完成："+error.Message;}finally{send.IsEnabled=true;}
        };
        take.Click+=async(_,_)=>{
            if(contextId==null||captured==null){message.Text="请先带本配置到工具箱，再保存供面板取回。";return;}
            if(capturedEpoch!=toolboxEditEpoch||!JToken.DeepEquals(captured,options)){message.Text="带入后本配置已有修改；请重新带入后再取回，当前输入保留。";return;}
            take.IsEnabled=false;var requestId=contextId;
            try {
                toolboxClient??=new();var draft=await toolboxClient.DraftAsync(requestId);
                if(contextId!=requestId||capturedEpoch!=toolboxEditEpoch||!JToken.DeepEquals(captured,options)){message.Text="等待期间配置已变化，未采用旧草稿。";return;}
                if(draft==null){message.Text="工具箱尚未保存本次草稿；请先保存供面板取回。";return;}
                var catalog=EventSkillPicker.LoadCatalog(AppContext.BaseDirectory,out _);
                var next=UmaKnowledgeToolsContext.PatchRun(options,draft,catalog.Select(c=>(c.Id,c.Name,c.DbId)),out var notice);
                if(JToken.DeepEquals(next,options)){message.Text="没有需要采用的配置改动。"+notice;return;}
                options.RemoveAll();foreach(var property in next.Properties())options.Add(property.Name,property.Value.DeepClone());
                Emit();BuildEditors();
                Children.Add(Note("已取回当前步骤的目标／卡组／技能名单草稿，请检查后保存配置。"+notice));
            } catch(Exception error){message.Text="草稿未采用："+error.Message;}finally{take.IsEnabled=true;}
        };
        buttons.Children.Add(send);buttons.Children.Add(take);body.Children.Add(buttons);body.Children.Add(message);return body;
    }
    private Control AttributeGoals(JObject options,JObject settings,string kind,string prefix,bool compactCards=false)
    {
        var editor=new UmaAttributeGoalEditor(()=>options["targets"] as JObject,()=>options["larc_policy"] as JObject,(targets,weights)=> {
            var policyForWeights=weights.Count>0?PolicyForAttributeEdit(options):null;
            if(targets.Count>0) {
                var values=Object(options,"targets");
                // The workflow target contract requires all five entries. Materialize
                // the displayed defaults only on an explicit target edit.
                foreach(var pair in new[]{("speed",1550),("stamina",1200),("power",1200),("guts",1200),("wisdom",1200)})
                    if(values[pair.Item1]==null||values[pair.Item1]!.Type==JTokenType.Null)values[pair.Item1]=pair.Item2;
                foreach(var property in targets.Properties())values[property.Name]=property.Value.Type==JTokenType.Null?(property.Name=="speed"?1550:1200):property.Value.DeepClone();
            }
            if(weights.Count>0) {
                var values=Object(policyForWeights!,"attribute_weights");
                foreach(var property in weights.Properties())if(property.Value.Type==JTokenType.Null)values.Remove(property.Name);else values[property.Name]=property.Value.DeepClone();
            }
            Emit();
        },value=>{var policy=PolicyForAttributeEdit(options);if(value==null)policy.Remove("after_target_credit");else policy["after_target_credit"]=value.DeepClone();Emit();},prefix,()=>AttributePriorityNotice(options,settings,kind),compactCards);
        editor.PendingChanged+=()=>{if(!building)PendingChanged?.Invoke();};attributeEditors.Add(editor);return editor;
    }
    private JObject PolicyForAttributeEdit(JObject options)
    {
        if(options["larc_policy"] is JObject existing)return existing;
        if(options.Property("larc_policy")==null)
            throw new InvalidOperationException("当前步骤沿用原局或前一步的策略。请先在更多运行设置中明确基础策略版本，再应用属性偏好；输入已保留，目标仍可单独调整。");
        if(options["larc_policy"]!.Type!=JTokenType.Null)
            throw new InvalidOperationException("原策略格式已保留。请先在更多运行设置中明确策略版本，再应用属性偏好。");
        if(newPolicyDefaults==null)
            throw new InvalidOperationException("尚未读取后台的新建策略默认值，属性偏好未写入。请刷新面板或明确策略版本；输入已保留。");
        var value=(JObject)newPolicyDefaults.DeepClone();options["larc_policy"]=value;return value;
    }
    private static bool Enabled(JToken? value,bool fallback=false)=>value?.Type==JTokenType.Boolean?(bool)value:fallback;
    private static string? AttributePriorityNotice(JObject options,JObject settings,string kind)
    {
        var policy=options["larc_policy"] as JObject;
        if(UmaDecisionPreferences.UsesBFamilyPreferences((string?)policy?["revision"]))
            return "首年由统一估值 B 比较行动；数值、羁绊、充电和在场额外分各作用一次。高级细调、公式和分段策略已保存，供后续继承阶段及旧策略使用。";
        if(options.Property("larc_policy")==null)return "本步骤沿用原局或前一步策略；目标可单独调整。应用属性偏好前请在上方策略版本中明确选择。";
        if(policy!=null&&(policy["variant"]==null||string.Equals((string?)policy["variant"],"alpha",StringComparison.OrdinalIgnoreCase)))
            return "当前为旧 alpha 分支，基础属性偏好在该分支不生效；可在上方策略版本中主动切换。";
        var preferences=(kind=="cut"?settings:options)["decision_preferences"] as JObject;
        var workflow=preferences?["workflow"] as JObject;
        if(workflow!=null&&Enabled(workflow["enabled"])) {
            if(workflow["stages"] is not JArray&&workflow["editor_graph"] is JObject graph)try{workflow=UmaWeightGraphModel.Compile(graph);}catch(Exception){return null;}
            var overridden=(workflow["stages"] as JArray??new()).OfType<JObject>().Where(stage=>Enabled(stage["enabled"],true))
                .SelectMany(stage=>(stage["rules"] as JArray??new()).OfType<JObject>()).Any(rule=> {
                    if(!Enabled(rule["enabled"],true)||rule["effects"] is not JObject effects)return false;
                    return effects["attribute_weights"] is JObject weights&&weights.Count>0||effects["after_target_credit"] is JToken credit&&credit.Type!=JTokenType.Null;
                });
            return overridden?"已启用分段属性设置：匹配时逐项覆盖基础偏好／共用达标折扣，未覆盖项沿用基础值。":null;
        }
        return (string?)(preferences?["tuning"] as JObject)?["mode"]=="formula"?"已启用自写评分：最终选择受公式影响，本表仍保存基础属性偏好。":null;
    }
    private Control StrategyRevision(JObject options,string kind,string prefix,bool splitLayout=false)
    {
        var revisions=UmaPacerView.StrategyRevisions;
        var originalPolicy=options["larc_policy"]?.DeepClone();
        var hadOriginalPolicy=options.Property("larc_policy")!=null;
        var originalStrategy=options["strategy"]?.DeepClone();
        var saved=DisplayStrategyPolicy(originalPolicy);
        var revision=saved?["revision"]?.Type==JTokenType.String?(string?)saved["revision"]:null;
        var selected=(string?)saved?["variant"]=="baseline"?Array.IndexOf(revisions,revision):-1;
        var picker=new ComboBox{Name=prefix+"StrategyRevision",HorizontalAlignment=HorizontalAlignment.Stretch,
            HorizontalContentAlignment=HorizontalAlignment.Stretch,
            ItemTemplate=new FuncDataTemplate<string>((item,_)=>new TextBlock{Text=item,TextWrapping=TextWrapping.Wrap}),
            ItemsSource=UmaPacerView.StrategyItems("沿用原设置（不修改版本）"),
            SelectedIndex=selected+1};
        var note=Note(StrategyRevisionNote(revision));note.Name=prefix+"StrategyRevisionNotice";
        var guardText=Note("");guardText.Name=prefix+"VersionSwitchGuardText";
        var purifyOnSwitch=new Button{Name=prefix+"PurifyOnSwitch",Content="✦ 一并恢复官方纯净（推荐）",FontWeight=FontWeight.SemiBold,Height=30,Padding=new Thickness(10,4),Margin=new Thickness(0,0,8,4)};
        var keepCustomOnSwitch=new Button{Name=prefix+"KeepCustomOnSwitch",Content="保留我的自定义覆盖",Height=30,Padding=new Thickness(10,4),Margin=new Thickness(0,0,0,4)};
        var guard=new Border{
            Name=prefix+"VersionSwitchGuard",
            IsVisible=false,
            BorderThickness=new Thickness(1),
            BorderBrush=new SolidColorBrush(Color.FromRgb(217,119,6)),
            Background=new SolidColorBrush(Color.FromArgb(24,245,158,11)),
            CornerRadius=new CornerRadius(6),
            Padding=new Thickness(10),
            Margin=new Thickness(0,4,0,0),
            Child=new StackPanel{Spacing=6,Children={guardText,new WrapPanel{Orientation=Orientation.Horizontal,Children={purifyOnSwitch,keepCustomOnSwitch}}}}
        };
        string? switchedFrom=null;
        string? switchedTo=null;
        purifyOnSwitch.Click+=(_,_)=> {
            PacerPurity.PurifyStep(options,kind,PurityLayer.DecisionPreferences|PurityLayer.L1Attribute);
            guard.IsVisible=false;
            note.Text=$"已切至 {switchedTo ?? (string?)options["larc_policy"]?["revision"] ?? "所选版本"}，并清除先前步骤覆盖，恢复官方纯净；请点顶栏“保存配置”。";
            RevisionSwitchPurified?.Invoke(kind);
            Emit();
        };
        keepCustomOnSwitch.Click+=(_,_)=> {
            guard.IsVisible=false;
            var crossFamily=!PacerPurity.SameFamily(switchedFrom,switchedTo);
            note.Text=crossFamily
                ?$"已保留先前的自定义覆盖并跨代切至 {switchedTo}（纯净度徽章将标红提醒）。"
                :$"已切至 {switchedTo}，并保留当前步骤的自定义覆盖。";
        };
        var previousSelection=picker.SelectedIndex;var adjustingPicker=false;
        picker.SelectionChanged+=(_,_)=> {
            if(adjustingPicker||picker.SelectedIndex<0)return;
            if(picker.SelectedIndex==0) {
                var current=options["larc_policy"] as JObject;
                var hasPreferences=current?["attribute_weights"] is JObject currentWeights&&currentWeights.Count>0||current?.Property("after_target_credit")!=null;
                if(!hadOriginalPolicy&&hasPreferences||originalPolicy?.Type==JTokenType.Null&&hasPreferences&&newPolicyDefaults==null) {
                    adjustingPicker=true;picker.SelectedIndex=previousSelection;adjustingPicker=false;
                    note.Text=!hadOriginalPolicy?"原配置是继承策略，本步新增偏好无法随继承表达。当前显式版本和偏好已保留；先恢复本步偏好及共用折扣默认后可回到继承，也可直接选择明确旧版回退。":"尚未读取原新建策略的真实默认值，当前版本和新偏好已保留。请刷新默认值后再恢复原设置。";
                    return;
                }
                var restored=originalPolicy is JObject prior?(JObject)prior.DeepClone():originalPolicy?.Type==JTokenType.Null&&hasPreferences?(JObject)newPolicyDefaults!.DeepClone():new JObject();
                foreach(var key in new[]{"attribute_weights","after_target_credit"}) {
                    var before=(originalPolicy as JObject)?[key];var latest=current?[key];
                    if(JToken.DeepEquals(before,latest))continue;
                    if(!hadOriginalPolicy&&latest is JObject empty&&empty.Count==0)continue;
                    if(current?.Property(key)!=null)restored[key]=latest!.DeepClone();else restored.Remove(key);
                }
                if(restored.Count>0||originalPolicy is JObject)options["larc_policy"]=restored;
                else if(originalPolicy==null)options.Remove("larc_policy");else options["larc_policy"]=originalPolicy.DeepClone();
                if(originalStrategy==null)options.Remove("strategy");else options["strategy"]=originalStrategy.DeepClone();
                previousSelection=0;
                if (originalPolicy?.Type == JTokenType.Null && selected >= 0)
                {
                    adjustingPicker=true;
                    picker.SelectedIndex=previousSelection=selected+1;
                    adjustingPicker=false;
                }
                guard.IsVisible=false;
                note.Text=StrategyRevisionNote(revision);Emit();return;
            }
            var chosen=revisions[picker.SelectedIndex-1];
            var policy=options["larc_policy"] as JObject;
            var oldRev=(string?)policy?["revision"];
            policy=PacerPurity.AdoptRevision(policy==null?new JObject():(JObject)policy.DeepClone(),oldRev,chosen);
            PacerPurity.StripLegacyMeiDefault(options,kind,oldRev,chosen);
            // Choosing a registered unified revision explicitly adopts its required
            // features for future runs, while retaining targets and user weights.
            foreach(var feature in new[]{"strategy_refinement","training_balance_v2","overseas_purchase_v2","overseas_purchase_v3","strategy_v4"})policy[feature]=true;
            options["strategy"]="baseline";options["larc_policy"]=policy;
            if(options["failure_limit"]?.ToString()?.Trim() is "25" or "28") {
                var nextLimit=UmaPacerView.DefaultFailureLimitForRevision(chosen);
                options["failure_limit"]=nextLimit;
                foreach(var tb in Descendants(this).OfType<TextBox>().Where(b=>b.Name==prefix+"failure_limit"))
                    tb.Text=nextLimit;
            }
            previousSelection=picker.SelectedIndex;note.Text=StrategyRevisionNote(chosen);
            var report=PacerPurity.EvaluateStep("",kind,kind,options);
            if(oldRev!=chosen&&report.EffectiveCount>0) {
                switchedFrom=oldRev;switchedTo=chosen;
                var crossFamily=!PacerPurity.SameFamily(oldRev,chosen);
                var previewItems=string.Join("、",report.Items.Where(i=>i.Effective).Take(4).Select(i=>$"{i.Label}={i.ActualText}"));
                guardText.Text=crossFamily
                    ?$"⚠ 检测到原先策略「{oldRev ?? "继承/未知"}」遗留了 {report.EffectiveCount} 项自定义覆盖（{previewItems}）。跨代切换至「{chosen}」时若继续保留，将与新版平衡冲突："
                    :$"检测到当前步骤带有 {report.EffectiveCount} 项自定义覆盖（{previewItems}）。切换至「{chosen}」时是否一并清除并恢复官方纯净？";
                guard.IsVisible=true;
                if(crossFamily)RevisionSwitchedWithOverrides?.Invoke(kind,oldRev,chosen);
            }
            else {
                guard.IsVisible=false;
            }
            Emit();
        };
        if(!splitLayout)
            return new StackPanel{Spacing=5,Children={Field("基础策略版本（下次新建生效）",picker),note,guard}};

        var noteBox=new Border{
            CornerRadius=new CornerRadius(8),
            BorderThickness=new Thickness(1),
            Padding=new Thickness(12,9),
            Background=new SolidColorBrush(Color.FromArgb(14,100,116,139)),
            Child=note
        };
        noteBox.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
        var leftPicker=Field("基础策略版本（下次新建生效）",picker);
        var cols=new Grid{ColumnDefinitions=new("310,*"),ColumnSpacing=14,RowSpacing=8};
        cols.Children.Add(leftPicker);
        Grid.SetColumn(noteBox,1);cols.Children.Add(noteBox);
        bool? lastColsNarrow=null;
        cols.SizeChanged+=(_,_)=>{
            var w=cols.Bounds.Width;
            if(w<=0)return;
            var narrow=PacerLayoutGuard.NextNarrow(w,640,lastColsNarrow,24);
            if(lastColsNarrow==narrow)return;
            lastColsNarrow=narrow;
            cols.ColumnDefinitions=new(narrow?"*":"310,*");
            cols.RowDefinitions=new(narrow?"Auto,Auto":"Auto");
            Grid.SetColumn(noteBox,narrow?0:1);Grid.SetRow(noteBox,narrow?1:0);
        };
        return new StackPanel{Spacing=8,Children={cols,guard}};
    }
    private static string StrategyRevisionNote(string? revision)
    {
        string body;
        if (string.IsNullOrEmpty(revision))
            body = "沿用原配置语义；主动选择版本后才修改这一步的基础策略。";
        else if (StrategyPackCatalog.TryGetKnownNotice(revision, out var known))
            body = known;
        else
            body = $"当前保存策略 {revision}，原设置保留；主动选择下列版本后才替换。";
        return body + " 只作用于本执行步骤和当前配置；正在运行的局及自定义权重、画布开关不变。";
    }
    private Control Character(JObject settings,string kind,string prefix,bool allowCurrent=true)
    {
        if(characters.Length==0)return Text(settings,"character_id","角色编号（沿用已保存选择）",prefix);
        var picker=new ComboBox{Name=prefix+"character_id",MinWidth=220,HorizontalAlignment=HorizontalAlignment.Stretch};
        var choices=allowCurrent ? new[]{new UmaTraineeChoice("current","","沿用游戏当前角色","↻","接管当前育成时保留角色；新开局时请选择具体角色和衣装。")}.Concat(characters).ToArray() : characters;
        UmaTraineePicker.Configure(picker,choices);
        var id=(string?)settings["character_id"];picker.SelectedItem=choices.FirstOrDefault(c=>c.CardId==id);
        var nameKey=kind=="cut"?"validation_character_name":"character_name";
        var name=new TextBox{Name=prefix+nameKey,Text=(string?)settings[nameKey],Watermark="请输入当前角色的确切中文名"};
        var nameField=Field("手动角色名称",name);nameField.IsVisible=(picker.SelectedItem as UmaTraineeChoice)?.Manual==true;
        var isCycleWithoutCut=kind=="career"&&!allowCurrent;
        bool NeedsConcrete(string? cid)=>!allowCurrent&&(string.IsNullOrWhiteSpace(cid)||cid=="current"||(isCycleWithoutCut&&cid=="validation"));
        var concreteHint=new TextBlock{
            Name=prefix+"ConcreteCharacterHint",
            Text="当前循环/筛局/成品模式不能沿用当前角色，请从上方下拉框选择具体马娘与衣装。",
            Foreground=new SolidColorBrush(Color.Parse("#D97706")),
            FontSize=12,
            TextWrapping=TextWrapping.Wrap,
            IsVisible=NeedsConcrete(id)
        };
        picker.SelectionChanged+=(_,_)=> {
            if(picker.SelectedItem is not UmaTraineeChoice c)return;
            settings["character_id"]=c.CardId;
            if(kind=="cut")settings["validation_mode"]=!c.CutPreset;
            settings[nameKey]=c.Manual ? name.Text??"" : c.CharacterName;
            if(!c.Manual)name.Text=c.CharacterName;
            nameField.IsVisible=c.Manual;
            concreteHint.IsVisible=NeedsConcrete(c.CardId);
            Emit();
        };
        name.TextChanged+=(_,_)=> {if((picker.SelectedItem as UmaTraineeChoice)?.Manual!=true||JToken.DeepEquals(settings[nameKey],new JValue(name.Text??"")))return;settings[nameKey]=name.Text??"";Emit();};
        return new StackPanel{Spacing=5,Children={Field("育成角色",picker),nameField,concreteHint}};
    }

    private void BuildRunEditors(StackPanel body,JObject options,JObject settings,string kind,string prefix,bool aceCycle=false)
    {
        var cut=kind=="cut";var run=cut?settings:options;
        if(section is null or "ai") {
        var ai=new UmaAiOptions{Name=prefix+"AiOptions"};
        ai.Load(run["ai_options"] as JObject);
        var aiProjection=ai.Read();
        ai.Changed+=()=> {var next=ai.Read();WriteProjection(run,"ai_options",aiProjection,next);aiProjection=next;Emit();};
        if(bridge!=null) {
            ai.RefreshStatus=()=>bridge(new JObject{["action"]="ai_schema"});
            ai.ConfigureConnection=connection=>bridge((JObject)connection.DeepClone());
            var queried=false;ai.Loaded+=async(_,_)=> {if(queried)return;queried=true;try{ai.SetProvider((await ai.RefreshStatus())["provider"] as JObject);}catch(Exception){}};
        }
        SetNamedScope(ai,"决策方式随当前命名配置保存；AI 接口由本机全部配置共用，密钥不写入配置库。",text=>text.Contains("三组配置共用")||text.Contains("模式随配置1～3"));
        body.Children.Add(Section(prefix+"Ai","当前配置 · AI 决策",ai));
        }

        if(section is null or "continuation")body.Children.Add(ContinuationSettings(run,cut||aceCycle,prefix,ordinary:false,aceCycle:aceCycle));

        if(section is null or "mid_skills") {
        if(!cut) {
            body.Children.Add(Check(options,"terminal_skills","结束前学习技能",prefix));
            var arcPolicy=new UmaArcSkillPolicyEditor{Name=prefix+"ArcSkillPolicy"};
            arcPolicy.Load(options["arc_policy"]);
            arcPolicy.Changed+=()=> {
                options["arc_policy"]=arcPolicy.ReadDraft();
                Emit();
            };
            handoffCommits.Add(()=> {
                options["arc_policy"]=arcPolicy.Read();
                return true;
            });
            body.Children.Add(Section(prefix+"ArcCmSkillPolicy","凯旋门与终盘 · CM 大赛目标自动选技",arcPolicy));
        }
        var catalog=EventSkillPicker.LoadCatalog(AppContext.BaseDirectory,out var catalogError);
        var midSkills=new MidSkillLearningEditor(catalog,catalogError){Name=prefix+"MidSkillLearning"};
        midSkills.SetPresetSaveAvailable(false);midSkills.Load(options,cut,1);
        SetNamedScope(midSkills,"当前编辑：本命名配置的中途学技能。请使用上方“保存配置”；续跑沿用原局设置。",text=>text.StartsWith("当前编辑：",StringComparison.Ordinal));
        var skillProjection=midSkills.ReadDraft();var allowlist=midSkills.SkillValues;
        midSkills.Changed+=()=> {
            var next=midSkills.ReadDraft();var nextAllowlist=midSkills.SkillValues;
            WriteProjection(options,"mid_skill_learning",skillProjection,next);skillProjection=next;
            if(!JToken.DeepEquals(allowlist,nextAllowlist)){options["early_skill_allowlist"]=nextAllowlist.DeepClone();allowlist=(JArray)nextAllowlist.DeepClone();}
            Emit();
        };
        body.Children.Add(Section(prefix+"MidSkills","育成中学习 · 名单、时间与预算",midSkills));
        }

        if(section is "custom_races") {
        var raceEditor=new UmaCustomRaceCalendarEditor(options,kind,prefix,Emit){Name=prefix+"RaceCalendar"};
        body.Children.Add(raceEditor);
        }
    }

    private Control ContinuationSettings(JObject run,bool allowRestart,string prefix,bool ordinary=false,bool aceCycle=false)
    {
        var continuation=new UmaContinuationOptions{Name=prefix+"Continuation"};
        // SetMode can reset failure choices, so apply it before loading the original draft.
        continuation.SetMode(allowRestart,aceCycle);continuation.Load(run["continuation"] as JObject);
        var continuationProjection=continuation.ReadDraft();
        void SaveContinuation() {
            var next=continuation.ReadDraft();
            if(JToken.DeepEquals(continuationProjection,next))return;
            WriteProjection(run,"continuation",continuationProjection,next);continuationProjection=next;Emit();
        }
        continuation.TemplateApplied+=tf=>{
            SaveContinuation();
            if(applyScreeningTrainingFailure!=null) {
                applyScreeningTrainingFailure(tf);
            } else if(aceCycle&&tf!=null) {
                var aceNode=(document["run_graph"]?["nodes"] as JArray)?.OfType<JObject>().FirstOrDefault(n=>n["ace_cycle"] is JObject)?["ace_cycle"] as JObject;
                if(aceNode!=null) {
                    aceNode["restart_after_training_failure"]=(bool?)tf["enabled"]==true;
                    aceNode["training_failure_ignore_wisdom"]=(bool?)tf["ignore_wisdom"]!=false;
                    aceNode["training_failure_stop_at_senior_expedition"]=(bool?)tf["stop_at_senior_expedition"]!=false;
                    Emit();
                }
            }
        };
        ObserveInputs(continuation,SaveContinuation);
        return ordinary?CommonGroup(prefix+"ContinuationSection",allowRestart?(aceCycle?"多节点杀马线与条件筛选":"刷切筛选与续养"):"多节点条件监控",
            ()=>continuation.EnabledForRun?"已启用":"未启用",continuation)
            :Section(prefix+"ContinuationSection","当前配置 · 多节点条件筛选",continuation);
    }

    private Expander Section(string name,string label,Control content)=>SettingsExpander(name,label,content,section!=null&&section!="ordinary");
    private static IEnumerable<Control> Descendants(Control root)
    {
        var pending=new Stack<Control>();var seen=new HashSet<Control>();pending.Push(root);
        while(pending.Count>0) {
            var control=pending.Pop();if(!seen.Add(control))continue;yield return control;
            foreach(var logicalChild in control.GetLogicalChildren().OfType<Control>())pending.Push(logicalChild);
            if(control is Panel panel)foreach(var panelChild in panel.Children)pending.Push(panelChild);
            if(control is Decorator decorator&&decorator.Child is Control decorated)pending.Push(decorated);
            if(control is ContentControl owner&&owner.Content is Control content)pending.Push(content);
        }
    }
    private static void SetNamedScope(Control root,string replacement,Func<string,bool> matches)
    {foreach(var note in Descendants(root).OfType<TextBlock>().Where(n=>matches(n.Text??"")))note.Text=replacement;}
    private static void ObserveInputs(Control root,Action changed)
    {
        var seen=new HashSet<Control>();
        void Attach() {
            foreach(var control in Descendants(root)) {
                if(!seen.Add(control))continue;
                if(control is TextBox text)text.TextChanged+=(_,_)=>changed();
                if(control is ComboBox choice)choice.SelectionChanged+=(_,_)=>changed();
                if(control is CheckBox check)check.IsCheckedChanged+=(_,_)=>changed();
                if(control is NumericUpDown number)number.ValueChanged+=(_,_)=>changed();
                if(control is Button button)button.Click+=(_,_)=> {Attach();changed();};
            }
        }
        Attach();
    }
    private static void WriteProjection(JObject owner,string key,JObject previous,JObject current)
    {
        if(JToken.DeepEquals(previous,current))return;
        if(owner[key] is not JObject target) {owner[key]=current.DeepClone();return;}
        ApplyProjectionChanges(target,previous,current);
    }
    private static void ApplyProjectionChanges(JObject target,JObject previous,JObject current)
    {
        foreach(var property in current.Properties()) {
            var before=previous[property.Name];if(JToken.DeepEquals(before,property.Value))continue;
            if(property.Value is JObject value&&before is JObject prior&&target[property.Name] is JObject original)ApplyProjectionChanges(original,prior,value);
            else if(property.Value is JArray values&&before is JArray priorValues&&target[property.Name] is JArray originals)target[property.Name]=ProjectArray(originals,priorValues,values);
            else target[property.Name]=property.Value.DeepClone();
        }
    }
    private static JArray ProjectArray(JArray originals,JArray previous,JArray current)
    {
        var available=Enumerable.Range(0,previous.Count).ToList();var mapping=new Dictionary<int,int>();
        for(var i=0;i<current.Count;i++) {
            var match=available.FirstOrDefault(n=>JToken.DeepEquals(previous[n],current[i]),-1);
            if(match>=0){mapping[i]=match;available.Remove(match);}
        }
        var remaining=Enumerable.Range(0,current.Count).Where(i=>!mapping.ContainsKey(i)).ToArray();
        if(remaining.Length==available.Count)for(var n=0;n<remaining.Length;n++)mapping[remaining[n]]=available[n];
        var result=new JArray();
        for(var i=0;i<current.Count;i++) {
            if(mapping.TryGetValue(i,out var old)&&old<originals.Count) {
                var value=originals[old].DeepClone();
                if(value is JObject target&&previous[old] is JObject before&&current[i] is JObject next)ApplyProjectionChanges(target,before,next);
                else if(!JToken.DeepEquals(previous[old],current[i]))value=current[i].DeepClone();
                result.Add(value);
            } else result.Add(current[i].DeepClone());
        }
        return result;
    }
    private Control Borrow(JObject settings,string prefix)
    {
        if(supports.Length==0)return Text(settings,"borrow_card_id","借卡编号（0沿用当前）",prefix);
        var picker=new ComboBox{Name=prefix+"borrow_card_id",MinWidth=220,HorizontalAlignment=HorizontalAlignment.Stretch};UmaTraineePicker.Configure(picker,supports);
        var id=(int?)settings["borrow_card_id"]??0;picker.SelectedItem=supports.FirstOrDefault(c=>c.CardId==id);
        picker.SelectionChanged+=(_,_)=> {if(picker.SelectedItem is UmaSupportChoice c){settings["borrow_card_id"]=c.CardId;Emit();}};
        return Field("指定借卡",picker);
    }
    private Control Text(JObject target,string key,string label,string prefix)
    {
        var field=new TextBox{Name=prefix+key,Text=target[key]?.ToString(),Width=120,MinWidth=120,Watermark="沿用保存值"};
        field.LostFocus+=(_,_)=> {if(field.Text==target[key]?.ToString())return;target[key]=field.Text??"";Emit();};return Field(label,field);
    }
    private Control Pick(JObject target,string key,string label,string[] values,string prefix)
    {
        var field=new ComboBox{Name=prefix+key,ItemsSource=values,SelectedIndex=Math.Clamp((int?)target[key]??0,0,values.Length-1),MinWidth=170};
        field.SelectionChanged+=(_,_)=> {if(field.SelectedIndex>=0){target[key]=field.SelectedIndex;Emit();}};return Field(label,field);
    }
    private Control Check(JObject target,string key,string label,string prefix)
    {
        var field=new CheckBox{Name=prefix+key,Content=label,IsChecked=(bool?)target[key]==true};field.IsCheckedChanged+=(_,_)=> {target[key]=field.IsChecked==true;Emit();};return field;
    }
    private static Control Field(string label,Control field)=>new StackPanel{Spacing=5,Margin=new Thickness(0,0,12,8),Children={Note(label),field}};
}
