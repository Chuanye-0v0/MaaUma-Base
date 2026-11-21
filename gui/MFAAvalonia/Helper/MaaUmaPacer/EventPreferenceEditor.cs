using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Layout;
using Avalonia.Media;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;

namespace UmaCutPanel;

public sealed class EventPreferenceEditor:Expander
{
    private sealed record EventRow(JObject Data)
    {
        public string Key=>(string)Data["key"]!;
        public override string ToString()=>(string?)Data["label"] ?? Key;
    }
    private sealed record OptionRow(int Index,string Label) {public override string ToString()=>Label;}
    private readonly StackPanel sharedStatus=new() {Name="EventPreferencesSaveArea",Spacing=7};
    private readonly TextBlock saveState=new() {Name="EventPreferencesSaveState",TextWrapping=TextWrapping.Wrap,FontSize=12};
    private StackPanel choicesBody=null!,automaticBody=null!;
    private Expander automaticSection=null!;
    private bool sectionsSeparated;
    private Action? reloadBoundAction;
    private Action<bool,string?>? setMeiContextAction;
    private Func<JObject?>? readProfilePrefs;
    private Action<JObject>? writeProfileDraft;
    private Func<string>? getProfileLabel;
    private Func<Task>? saveProfileCallback;
    private Func<(string ScopeLabel, int ManualCount, string WithSummary)>? readSummaryFunc;
    public event Action? PreferencesChanged;
    public string PacerRoot { get; }
    public (string ScopeLabel, int ManualCount, string WithSummary) ReadPreflightSummary() =>
        readSummaryFunc?.Invoke() ?? ("全局事件库", 0, "自动选择");

    /// <summary>Detach the existing controls once; their draft and shared save closure remain intact.</summary>
    public (Control Choices,Control Automatic,Control Actions) SeparateSections()
    {
        if(sectionsSeparated)throw new InvalidOperationException("事件设置分区已经挂载。");
        sectionsSeparated=true;
        choicesBody.Children.Remove(sharedStatus);
        choicesBody.Children.Remove(automaticSection);
        automaticSection.Content=null;
        Content=null;
        return (choicesBody,automaticBody,sharedStatus);
    }

    public EventPreferenceEditor(string root,IUmaPacerBridge bridge,Action<string> message,bool collapseAfterSave=true)
    {
        PacerRoot=Path.GetFullPath(root);
        Margin=new Thickness(12,0,12,8);
        var outerMessage=message;
        message=text=>{saveState.Text=text;outerMessage(text);};
        saveState.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));
        var path=Path.Combine(PacerRoot,"portable_config.json");
        JObject? ReadPortablePreferences()
        {
            try { return File.Exists(path) ? JObject.Parse(File.ReadAllText(path))["event_preferences"] as JObject : null; }
            catch { return null; }
        }
        var saved=ReadPortablePreferences();
        var rules=(JObject?)saved?["manual_choices"]?.DeepClone() ?? new JObject();
        var knownEvents=new Dictionary<string,JObject>(StringComparer.Ordinal);
        var localCardNames=new Dictionary<string,string>(StringComparer.Ordinal);
        try {
            var catalogParent=Path.GetDirectoryName(PacerRoot) ?? PacerRoot;
            var supportCards=UmaSupportPicker.Load(catalogParent);
            if(supportCards.Count<=1)supportCards=UmaSupportPicker.Load(PacerRoot);
            foreach(var card in supportCards)
                if(card.CardId>0 && !string.IsNullOrWhiteSpace(card.Label))
                    localCardNames[card.CardId.ToString(System.Globalization.CultureInfo.InvariantCulture)]=card.Label;
        } catch { }
        var inFlightKeys=new HashSet<string>(StringComparer.Ordinal);
        var unresolvableKeys=new HashSet<string>(StringComparer.Ordinal);
        var activeOverridesHeader=new TextBlock {Name="EventActiveOverridesHeader",Text="当前已锁定的手动事件 (0 项)",FontSize=14,FontWeight=FontWeight.SemiBold};
        var activeOverridesList=new StackPanel {Name="EventActiveOverridesList",Spacing=4};
        var activeOverridesPanel=new Border {
            Name="EventActiveOverridesPanel",
            BorderThickness=new Thickness(1),
            CornerRadius=new CornerRadius(8),
            Padding=new Thickness(10,8),
            Child=new StackPanel {Spacing=6,Children={activeOverridesHeader,activeOverridesList}}
        };
        activeOverridesPanel.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
        var query=new TextBox {Name="EventSearch",Watermark="搜索事件、角色或支援卡，例如：根城 第二段"};
        var search=new Button {Name="SearchEvents",Content="搜索",Margin=new Thickness(6,0,0,0)};
        var configured=new CheckBox {Name="ConfiguredEventsOnly",Content="只看已指定",Margin=new Thickness(8,0,0,0)};
        var results=new ListBox {Name="EventSearchResults",MaxHeight=200,IsVisible=false};
        var counter=new TextBlock {Text="输入关键词后搜索，只列出有选项的事件。",TextWrapping=TextWrapping.Wrap};
        var details=new TextBlock {Name="EventChoiceDetails",Text="选中事件后查看各项参考收益。",TextWrapping=TextWrapping.Wrap};
        var selectedOption=new ComboBox {Name="EventPreferredOption",IsEnabled=false,IsVisible=false,HorizontalAlignment=HorizontalAlignment.Stretch};
        var queryRow=new DockPanel();DockPanel.SetDock(search,Dock.Right);DockPanel.SetDock(configured,Dock.Right);
        queryRow.Children.Add(search);queryRow.Children.Add(configured);queryRow.Children.Add(query);
        var save=new Button {Name="SaveEventPreferences",Content="立即验证并保存事件设置"};
        var clear=new Button {Name="ClearEventOverrides",Content="清空事件指定"};
        var skillCatalog=EventSkillPicker.LoadCatalog(root,out var skillCatalogError);
        var targets=new EventSkillPicker("EventTargetSkills",skillCatalog);
        var ignored=new EventSkillPicker("EventIgnoredSkills",skillCatalog);
        var conserve=new CheckBox {Name="EventConserveEnergy",Content="事件里优先节约额外体力",IsChecked=(bool?)saved?["conserve_energy"] ?? true};
        var preserve=new CheckBox {Name="EventPreserveTargets",Content="优先保留目标技能路线",IsChecked=(bool?)saved?["prioritize_target_routes"] ?? true};
        var preset=new Button {Name="EventMilePreset",Content="填入本次英里LOH取向"};
        var meiPolicy=saved?["mei_unlock_policy"] as JObject;
        var meiGamble=new CheckBox {Name="MeiUnlockGamble",Content="刷切专用：黄帽解锁低体力时选2，确认失败后重开",
            IsChecked=(string?)meiPolicy?["mode"]=="gamble_when_low"};
        var meiThreshold=new NumericUpDown {Name="MeiUnlockThreshold",Minimum=0,Maximum=100,Increment=5,
            FormatString="0.#",Value=(decimal?)meiPolicy?["below_vitality_percent"] ?? 50,Width=120,MinHeight=34,
            IsEnabled=meiGamble.IsChecked==true};
        var meiThresholdRow=new StackPanel {Orientation=Orientation.Horizontal,Spacing=6};
        meiThresholdRow.Children.Add(new TextBlock {Text="体力低于",VerticalAlignment=VerticalAlignment.Center});
        meiThresholdRow.Children.Add(meiThreshold);
        meiThresholdRow.Children.Add(new TextBlock {Text="% 时冒险（默认稳妥选1）",VerticalAlignment=VerticalAlignment.Center});
        var meiScopeNote=new TextBlock {Name="MeiUnlockScopeNote",FontSize=11.5,TextWrapping=TextWrapping.Wrap,IsVisible=false};
        meiScopeNote.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));

        // Inline With priority list (no modal needed for routine reordering)
        var withChars=new[]{"神鹰","里见光钻","佐岳鸣","天狼星象征","曼城茶座","中山庆典","黄金船"};
        var withPriorityList=new List<string>();
        void LoadWithList(JObject? src)
        {
            withPriorityList.Clear();
            if(src?["with_priority"] is JArray arr)
                foreach(var v in arr)
                    if(v.Type==JTokenType.String&&!string.IsNullOrWhiteSpace((string?)v)&&!withPriorityList.Contains(((string)v!).Trim()))
                        withPriorityList.Add(((string)v!).Trim());
            else if(src?["with_target"] is JValue lv&&!string.IsNullOrWhiteSpace((string?)lv))
                withPriorityList.Add(((string)lv!).Trim());
        }
        LoadWithList(saved);
        var withListPanel=new StackPanel {Name="InlineWithPriorityList",Spacing=4};
        var withClearBtn=new Button {Name="InlineWithClear",Content="恢复自动",Height=28,Padding=new Thickness(10,0),FontSize=12};
        var withAddCombo=new ComboBox {Name="InlineWithAddCombo",Height=32,MinHeight=0,MinWidth=130};
        var withCustomInput=new TextBox {Name="InlineWithCustomName",Watermark="其他角色名",Width=110,Height=32,MinHeight=0};
        var withAddBtn=new Button {Name="InlineWithAddBtn",Content="+ 添加",Height=32,Padding=new Thickness(10,0)};
        var withStatus=new TextBlock {Name="WithPrioritySummary",FontSize=12,TextWrapping=TextWrapping.Wrap};
        withStatus.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));

        // Inline Acupuncture mode selector + optional rules window trigger
        var inlineAcupuncture=AcupuncturePolicyWindow.PolicyFromPreferences(saved);
        var needleCombo=new ComboBox {
            Name="InlineAcupunctureMode",
            ItemsSource=new[]{
                "默认拒绝（第5项 · 稳妥推荐）",
                "固定选 1 · 成为强大的优俊少女",
                "固定选 2 · 赢下比赛",
                "固定选 3 · 健康有活力",
                "固定选 4 · 提升魅力",
                "按条件规则选择（下方按钮配置规则）"
            },
            HorizontalAlignment=HorizontalAlignment.Stretch,
            Height=34,MinHeight=0
        };
        int NeedleComboIndex(JObject pol)
        {
            var m=(string?)pol["mode"];
            if(m=="rules")return 5;
            if(m=="fixed")return Math.Clamp((int?)pol["fixed_choice"]??5,1,5) switch {1=>1,2=>2,3=>3,4=>4,_=>0};
            return 0;
        }
        needleCombo.SelectedIndex=NeedleComboIndex(inlineAcupuncture);
        var needle=new Button {Name="AcupunctureSettings",Content="条件规则明细…",Height=30,Padding=new Thickness(10,0),FontSize=12};
        var needleStatus=new TextBlock {Name="AcupunctureSummary",Text="针灸："+AcupuncturePolicyWindow.Summary(saved),FontSize=12,TextWrapping=TextWrapping.Wrap};
        needleStatus.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));

        var withHeaderGrid=new Grid {ColumnDefinitions=new("*,Auto"),ColumnSpacing=8};
        withHeaderGrid.Children.Add(new StackPanel {Spacing=2,Children={
            new TextBlock {Text="凯旋门 With 角色优先级（直接调序 · 免弹窗）",FontSize=14,FontWeight=FontWeight.SemiBold},
            withStatus
        }});
        Grid.SetColumn(withClearBtn,1);withHeaderGrid.Children.Add(withClearBtn);
        var withAddRow=new WrapPanel {Orientation=Orientation.Horizontal};
        withAddCombo.Margin=new Thickness(0,0,6,4);withCustomInput.Margin=new Thickness(0,0,6,4);withAddBtn.Margin=new Thickness(0,0,0,4);
        withAddRow.Children.Add(withAddCombo);withAddRow.Children.Add(withCustomInput);withAddRow.Children.Add(withAddBtn);
        var withCard=new Border {
            Name="EventInlineWithCard",
            CornerRadius=new CornerRadius(10),
            BorderThickness=new Thickness(1),
            Padding=new Thickness(12),
            Child=new StackPanel {Spacing=8,Children={withHeaderGrid,withListPanel,withAddRow}}
        };
        withCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));

        var needleHeadRow=new DockPanel();
        DockPanel.SetDock(needle,Dock.Right);needleHeadRow.Children.Add(needle);
        needleHeadRow.Children.Add(new TextBlock {Text="庸医扎针 & 事件自动护栏",FontSize=14,FontWeight=FontWeight.SemiBold,VerticalAlignment=VerticalAlignment.Center});
        var guardCard=new Border {
            Name="EventInlineGuardsCard",
            CornerRadius=new CornerRadius(10),
            BorderThickness=new Thickness(1),
            Padding=new Thickness(12),
            Child=new StackPanel {Spacing=8,Children={needleHeadRow,needleCombo,needleStatus,conserve,preserve,meiGamble,meiThresholdRow,meiScopeNote}}
        };
        guardCard.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));

        var inlineRulesGrid=new Grid {Name="EventInlineRulesGrid",ColumnDefinitions=new("*,*"),ColumnSpacing=12,RowSpacing=12};
        inlineRulesGrid.Children.Add(withCard);
        Grid.SetColumn(guardCard,1);inlineRulesGrid.Children.Add(guardCard);
        bool? lastInlineNarrow=null;
        inlineRulesGrid.SizeChanged+=(_,_)=>{
            var w=inlineRulesGrid.Bounds.Width;
            if(w<=0)return;
            var narrow=PacerLayoutGuard.NextNarrow(w,720,lastInlineNarrow,24);
            if(lastInlineNarrow==narrow)return;
            lastInlineNarrow=narrow;
            inlineRulesGrid.ColumnDefinitions=new(narrow?"*":"*,*");
            inlineRulesGrid.RowDefinitions=new(narrow?"Auto,Auto":"Auto");
            Grid.SetColumn(guardCard,narrow?0:1);Grid.SetRow(guardCard,narrow?1:0);
        };

        targets.SetValues(saved?["target_skills"]);ignored.SetValues(saved?["ignored_skills"]);
        var columns=new Grid {Name="EventAutomaticSkillColumns",ColumnDefinitions=new("*,*"),ColumnSpacing=12,RowSpacing=10};
        var left=new StackPanel {Spacing=4};left.Children.Add(new TextBlock {Text="优先取得的技能（#1 → #N 严格顺位）",FontWeight=FontWeight.SemiBold});left.Children.Add(targets);
        var right=new StackPanel {Spacing=4};right.Children.Add(new TextBlock {Text="本次不需要的技能",FontWeight=FontWeight.SemiBold});right.Children.Add(ignored);Grid.SetColumn(right,1);
        columns.Children.Add(left);columns.Children.Add(right);
        bool? lastColsNarrow=null;
        columns.SizeChanged+=(_,_)=> {
            var w=columns.Bounds.Width;
            if(w<=0)return;
            var narrow=PacerLayoutGuard.NextNarrow(w,650,lastColsNarrow,24);
            if(lastColsNarrow==narrow)return;
            lastColsNarrow=narrow;
            columns.ColumnDefinitions=new(narrow?"*":"*,*");columns.RowDefinitions=new(narrow?"Auto,Auto":"Auto");
            Grid.SetColumn(right,narrow?0:1);Grid.SetRow(right,narrow?1:0);
        };
        automaticBody=new StackPanel {Name="EventAutomaticBody",Spacing=10};automaticBody.Children.Add(columns);
        if(skillCatalogError!=null)automaticBody.Children.Add(new TextBlock {Text=skillCatalogError,TextWrapping=TextWrapping.Wrap});
        automaticBody.Children.Add(preset);
        automaticSection=new Expander {Name="EventAutomaticPreferences",Header="未指定事件时的自动取舍（可选）",Content=automaticBody};
        var buttons=new WrapPanel {Orientation=Orientation.Horizontal};save.Margin=new Thickness(0,0,8,4);clear.Margin=new Thickness(0,0,8,4);buttons.Children.Add(save);buttons.Children.Add(clear);
        sharedStatus.Children.Add(buttons);sharedStatus.Children.Add(saveState);
        var body=choicesBody=new StackPanel {Name="EventChoicesBody",Spacing=12};
        body.Children.Add(inlineRulesGrid);
        body.Children.Add(activeOverridesPanel);
        body.Children.Add(queryRow);body.Children.Add(counter);body.Children.Add(results);
        body.Children.Add(new ScrollViewer {Content=details,MaxHeight=200});body.Children.Add(selectedOption);
        body.Children.Add(new TextBlock {Text="你的事件指定优先于自动策略。选“自动选择”或点上方“恢复自动”可取消该事件的指定。",TextWrapping=TextWrapping.Wrap});
        body.Children.Add(sharedStatus);body.Children.Add(automaticSection);Content=body;
        var updating=false;var searchRevision=0;var dirty=false;var meiSupported=true;
        JObject CaptureForm()=>new() {
            ["target_skills"]=targets.Values,["ignored_skills"]=ignored.Values,
            ["conserve_energy"]=conserve.IsChecked==true,["prioritize_target_routes"]=preserve.IsChecked==true,
            ["manual_choices"]=rules.DeepClone(),["mei_gamble"]=meiGamble.IsChecked==true,
            ["mei_threshold"]=meiThreshold.Value ?? 50,
            ["with_priority"]=new JArray(withPriorityList),
            ["acupuncture_policy"]=inlineAcupuncture.DeepClone()};
        var savedForm=CaptureForm();
        JObject BuildCurrentPayload()
        {
            var preferences=(JObject?)saved?.DeepClone() ?? new JObject();
            preferences["target_skills"]=targets.Values;
            preferences["ignored_skills"]=ignored.Values;
            preferences["conserve_energy"]=conserve.IsChecked==true;
            preferences["prioritize_target_routes"]=preserve.IsChecked==true;
            preferences["manual_choices"]=rules.DeepClone();
            preferences["with_priority"]=new JArray(withPriorityList);
            preferences.Remove("with_target");
            preferences["acupuncture_policy"]=inlineAcupuncture.DeepClone();
            preferences.Remove("acupuncture_choice");
            preferences["mei_unlock_policy"]=new JObject {
                ["mode"]=meiGamble.IsChecked==true ? "gamble_when_low" : "safe",
                ["below_vitality_percent"]=meiThreshold.Value ?? 50
            };
            return preferences;
        }
        readSummaryFunc=()=> {
            var scopeLbl=getProfileLabel!=null ? $"配置「{getProfileLabel()}」独立覆盖" : "全局共用事件库";
            var withStr=withPriorityList.Count==0 ? "自动选择" : string.Join(" → ", withPriorityList);
            return (scopeLbl, rules.Count, withStr);
        };
        async Task ResolveMissingEventTitlesAsync(string[] missingKeys)
        {
            var resolvedOk=false;
            try {
                var resp=await bridge.Request(new JObject {["action"]="search_events",["query"]="",["limit"]=Math.Max(40,missingKeys.Length),["keys"]=new JArray(missingKeys)});
                if(resp["events"] is JArray evArr)
                {
                    resolvedOk=true;
                    var returned=new HashSet<string>(StringComparer.Ordinal);
                    foreach(var row in evArr.OfType<JObject>())
                    {
                        if((string?)row["key"] is string k && !string.IsNullOrEmpty(k))
                        {
                            knownEvents[k]=(JObject)row.DeepClone();
                            returned.Add(k);
                            unresolvableKeys.Remove(k);
                        }
                    }
                    foreach(var reqKey in missingKeys)
                        if(!returned.Contains(reqKey))unresolvableKeys.Add(reqKey);
                }
            } catch {
                /* Transient/startup errors leave keys retryable once the host/bridge is ready. */
            } finally {
                foreach(var reqKey in missingKeys)inFlightKeys.Remove(reqKey);
                if(resolvedOk)
                {
                    void Redraw()
                    {
                        try { RefreshActiveOverrides(); ShowEvent(); } catch { }
                    }
                    if(Avalonia.Threading.Dispatcher.UIThread.CheckAccess())Redraw();
                    else Avalonia.Threading.Dispatcher.UIThread.Post(Redraw);
                }
            }
        }
        string DescribeUnresolvedKey(string rawKey, bool exhausted)
        {
            var parts=rawKey.Split(':');
            if(parts.Length>=2 && parts[0]=="support")
            {
                var cardId=parts[1];
                var cardLabel=localCardNames.TryGetValue(cardId,out var foundCard) ? foundCard : $"支援卡 #{cardId}";
                var storyId=parts.Length>=4 && parts[2]=="story" ? parts[3] : rawKey;
                return exhausted ? $"{cardLabel} · 支援卡事件（编号 {storyId}）" : $"{cardLabel} · 支援卡剧情事件（正在读取事件标题…）";
            }
            return exhausted ? $"已指定事件（编号 {rawKey}）" : "已指定事件（正在读取事件标题…）";
        }
        void RefreshActiveOverrides()
        {
            activeOverridesHeader.Text=$"当前已锁定的手动事件 ({rules.Count} 项)";
            activeOverridesList.Children.Clear();
            var props=rules.Properties().ToArray();
            if(props.Length==0)
            {
                activeOverridesList.Children.Add(new TextBlock {Text="当前未锁定任何手动事件（全部按右侧“自动策略”智能选择）。",FontSize=12,Opacity=0.75,TextWrapping=TextWrapping.Wrap});
                return;
            }
            var missing=new List<string>();
            foreach(var prop in props)
            {
                var optionIdx=(int?)prop.Value ?? 0;
                JObject? metaCopy=null;
                string displayLabel;
                string optionLabel=$"固定选第 {optionIdx} 项";
                string? effectsLine=null;
                string rowTip=prop.Name;
                if(knownEvents.TryGetValue(prop.Name,out var meta))
                {
                    metaCopy=meta;
                    var stageNum=(int?)meta["stage"];
                    var stageText=stageNum switch {1=>"第一段",2=>"第二段",3=>"第三段",4=>"第四段",5=>"第五段",_=>null};
                    var stageSuffix=string.IsNullOrEmpty(stageText)?"":$"（{stageText}）";
                    var evTitle=(string?)meta["title"] ?? (string?)meta["label"] ?? prop.Name;
                    var ownerText=(string?)meta["owner"];
                    displayLabel=string.IsNullOrWhiteSpace(ownerText) ? $"{evTitle}{stageSuffix}" : $"{ownerText} · {evTitle}{stageSuffix}";
                    var opts=((JArray?)meta["options"] ?? new()).OfType<JObject>().ToArray();
                    var opt=opts.FirstOrDefault(o=>(int?)o["index"]==optionIdx);
                    if(opt!=null && (string?)opt["position"] is string pos)
                        optionLabel=$"固定选{pos}（第{optionIdx}项）";
                    var eff=(string?)opt?["effects"];
                    var optText=(string?)opt?["text"];
                    var combinedEff=!string.IsNullOrWhiteSpace(optText)&&!string.IsNullOrWhiteSpace(eff)
                        ? $"{optText} · {eff}" : (!string.IsNullOrWhiteSpace(eff) ? eff : optText);
                    if(!string.IsNullOrWhiteSpace(combinedEff))
                        effectsLine=combinedEff.Length>90 ? combinedEff[..90]+"…" : combinedEff;
                    if(opts.Length>0)
                        rowTip=$"{displayLabel}\n键名：{prop.Name}\n"+string.Join("\n",opts.Select(o=>$"选{o["position"] ?? o["index"]?.ToString()}（第{o["index"]}项）：{o["effects"]}"));
                }
                else
                {
                    var exhausted=unresolvableKeys.Contains(prop.Name);
                    displayLabel=DescribeUnresolvedKey(prop.Name,exhausted);
                    rowTip=$"{displayLabel}\n原始规则键：{prop.Name}";
                    if(!exhausted && inFlightKeys.Add(prop.Name))
                        missing.Add(prop.Name);
                }
                var keyCopy=prop.Name;
                var rowPanel=new DockPanel {Margin=new Thickness(0,2,0,2)};
                var btnStrip=new StackPanel {Orientation=Orientation.Horizontal,Spacing=6,Margin=new Thickness(8,0,0,0),VerticalAlignment=VerticalAlignment.Center};
                if(metaCopy!=null)
                {
                    var capturedMeta=metaCopy;
                    var capturedLabel=displayLabel;
                    var inspectBtn=new Button {Content="查看/改选",Padding=new Thickness(8,3),FontSize=12};
                    inspectBtn.Click+=(_,_)=>{
                        results.ItemsSource=new[]{new EventRow(capturedMeta)};
                        results.IsVisible=true;
                        results.SelectedIndex=0;
                        counter.Text=$"已定位「{capturedLabel}」，可在下方查看完整效果并切换固定选项。";
                    };
                    btnStrip.Children.Add(inspectBtn);
                }
                var removeBtn=new Button {Content="恢复自动",Padding=new Thickness(8,3),FontSize=12};
                removeBtn.Click+=(_,_)=>{
                    rules.Remove(keyCopy);
                    RefreshActiveOverrides();
                    ShowEvent();
                    Changed();
                };
                btnStrip.Children.Add(removeBtn);
                DockPanel.SetDock(btnStrip,Dock.Right);
                rowPanel.Children.Add(btnStrip);

                var titleBadge=new Border {
                    CornerRadius=new CornerRadius(6),
                    BorderThickness=new Thickness(1),
                    Padding=new Thickness(6,1),
                    Margin=new Thickness(6,0,0,0),
                    VerticalAlignment=VerticalAlignment.Center,
                    Background=new SolidColorBrush(Color.FromArgb(24,59,130,246)),
                    BorderBrush=new SolidColorBrush(Color.FromRgb(147,197,253)),
                    Child=new TextBlock {
                        Text=optionLabel,
                        FontSize=11,
                        FontWeight=FontWeight.SemiBold,
                        Foreground=new SolidColorBrush(Color.Parse("#1D4ED8"))
                    }
                };
                var headLine=new WrapPanel {Orientation=Orientation.Horizontal,VerticalAlignment=VerticalAlignment.Center};
                headLine.Children.Add(new TextBlock {
                    Text=displayLabel,
                    FontSize=12.5,
                    FontWeight=FontWeight.SemiBold,
                    TextWrapping=TextWrapping.Wrap,
                    VerticalAlignment=VerticalAlignment.Center
                });
                headLine.Children.Add(titleBadge);
                var leftStack=new StackPanel {Spacing=2,VerticalAlignment=VerticalAlignment.Center};
                leftStack.Children.Add(headLine);
                if(!string.IsNullOrWhiteSpace(effectsLine))
                {
                    var effBlock=new TextBlock {
                        Text="参考收益："+effectsLine,
                        FontSize=11,
                        TextWrapping=TextWrapping.Wrap
                    };
                    effBlock.Bind(TextBlock.ForegroundProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiLowText"));
                    leftStack.Children.Add(effBlock);
                }
                ToolTip.SetTip(rowPanel,rowTip);
                rowPanel.Children.Add(leftStack);
                activeOverridesList.Children.Add(rowPanel);
            }
            if(missing.Count>0)
                _=ResolveMissingEventTitlesAsync(missing.ToArray());
        }
        void RefreshWithList()
        {
            withListPanel.Children.Clear();
            withClearBtn.IsEnabled=withPriorityList.Count>0;
            withStatus.Text=withPriorityList.Count==0
                ? "当前：自动选择（按卡组与金技适配评分）"
                : "当前顺位："+string.Join(" → ",withPriorityList);
            if(withPriorityList.Count==0)
            {
                var emptyWith=new Border {
                    CornerRadius=new CornerRadius(6),
                    BorderThickness=new Thickness(1),
                    Padding=new Thickness(10,7),
                    Child=new TextBlock {Text="当前为「自动选择」，可在下方添加偏好 With 角色。",FontSize=12,Opacity=0.75,TextWrapping=TextWrapping.Wrap}
                };
                emptyWith.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
                withListPanel.Children.Add(emptyWith);
            }
            else
            {
                for(var i=0;i<withPriorityList.Count;i++)
                {
                    var idx=i;
                    var charName=withPriorityList[i];
                    var rowDock=new DockPanel {Margin=new Thickness(0,1)};
                    var upBtn=new Button {Content="↑",Width=26,Height=24,Padding=new Thickness(0),FontSize=11,IsEnabled=idx>0,Margin=new Thickness(4,0,0,0)};
                    var dnBtn=new Button {Content="↓",Width=26,Height=24,Padding=new Thickness(0),FontSize=11,IsEnabled=idx<withPriorityList.Count-1,Margin=new Thickness(4,0,0,0)};
                    var delBtn=new Button {Content="✕",Width=26,Height=24,Padding=new Thickness(0),FontSize=11,Margin=new Thickness(4,0,0,0)};
                    upBtn.Click+=(_,_)=>{
                        if(idx<=0)return;
                        (withPriorityList[idx-1],withPriorityList[idx])=(withPriorityList[idx],withPriorityList[idx-1]);
                        RefreshWithList();Changed();
                    };
                    dnBtn.Click+=(_,_)=>{
                        if(idx>=withPriorityList.Count-1)return;
                        (withPriorityList[idx],withPriorityList[idx+1])=(withPriorityList[idx+1],withPriorityList[idx]);
                        RefreshWithList();Changed();
                    };
                    delBtn.Click+=(_,_)=>{
                        withPriorityList.RemoveAt(idx);
                        RefreshWithList();Changed();
                    };
                    var btnStrip=new StackPanel {Orientation=Orientation.Horizontal,Children={upBtn,dnBtn,delBtn}};
                    DockPanel.SetDock(btnStrip,Dock.Right);
                    rowDock.Children.Add(btnStrip);
                    rowDock.Children.Add(new TextBlock {
                        Text=$"#{idx+1}  {charName}",
                        FontSize=12,
                        FontWeight=FontWeight.SemiBold,
                        VerticalAlignment=VerticalAlignment.Center
                    });
                    var itemBox=new Border {
                        CornerRadius=new CornerRadius(6),
                        BorderThickness=new Thickness(1),
                        Padding=new Thickness(8,4),
                        Child=rowDock
                    };
                    itemBox.Bind(Border.BorderBrushProperty,new Avalonia.Markup.Xaml.MarkupExtensions.DynamicResourceExtension("SukiControlBorderBrush"));
                    withListPanel.Children.Add(itemBox);
                }
            }
            var remaining=withChars.Where(c=>!withPriorityList.Contains(c)).ToArray();
            withAddCombo.ItemsSource=remaining.Length>0?remaining:new[]{"均已添加"};
            withAddCombo.SelectedIndex=0;
            withAddCombo.IsEnabled=remaining.Length>0;
        }
        withAddBtn.Click+=(_,_)=>{
            var custom=withCustomInput.Text?.Trim();
            var name=!string.IsNullOrEmpty(custom)?custom:(withAddCombo.SelectedItem as string);
            if(string.IsNullOrWhiteSpace(name)||name=="均已添加")return;
            if(withPriorityList.Contains(name))return;
            withPriorityList.Add(name);
            withCustomInput.Text="";
            RefreshWithList();Changed();
        };
        withClearBtn.Click+=(_,_)=>{
            if(withPriorityList.Count==0)return;
            withPriorityList.Clear();
            RefreshWithList();Changed();
        };
        needleCombo.SelectionChanged+=(_,_)=>{
            if(updating||needleCombo.SelectedIndex<0)return;
            var sel=needleCombo.SelectedIndex;
            if(sel==0){inlineAcupuncture["mode"]="decline";inlineAcupuncture["fixed_choice"]=5;}
            else if(sel is >=1 and <=4){inlineAcupuncture["mode"]="fixed";inlineAcupuncture["fixed_choice"]=sel;}
            else inlineAcupuncture["mode"]="rules";
            needleStatus.Text="针灸："+AcupuncturePolicyWindow.Summary(new JObject {["acupuncture_policy"]=inlineAcupuncture});
            Changed();
        };
        needle.Click+=async(_,_)=> {
            if(TopLevel.GetTopLevel(choicesBody) is not Window parent)return;
            needle.IsEnabled=false;
            try {
                var seed=BuildCurrentPayload();
                var window=new AcupuncturePolicyWindow(seed,async policy=> {
                    var checkedValue=await bridge.Request(new JObject {["action"]="validate_event_preferences",["preferences"]=new JObject {["acupuncture_policy"]=policy}});
                    var normalized=checkedValue["preferences"]?["acupuncture_policy"] as JObject??throw new InvalidOperationException("后台没有确认针灸设置，尚未保存。");
                    inlineAcupuncture=(JObject)normalized.DeepClone();
                    updating=true;
                    needleCombo.SelectedIndex=NeedleComboIndex(inlineAcupuncture);
                    needleStatus.Text="针灸："+AcupuncturePolicyWindow.Summary(new JObject {["acupuncture_policy"]=inlineAcupuncture});
                    updating=false;
                    if(writeProfileDraft!=null)writeProfileDraft(BuildCurrentPayload());
                    else PersistPortableFallback(BuildCurrentPayload());
                    message(writeProfileDraft!=null?"针灸条件规则已写入当前配置草稿；请点顶栏“保存配置”提交。":"针灸设置已保存，对后续针灸事件生效。");
                    PreferencesChanged?.Invoke();
                });
                await window.ShowDialog<bool>(parent);
            } finally {needle.IsEnabled=true;}
        };
        void HeaderText() {
            var scopeTag=getProfileLabel!=null ? $"当前配置「{getProfileLabel()}」事件设置" : "共用事件设置";
            Header=$"事件选项设置 · 已指定 {rules.Count} 项"+(dirty ? " · 未保存" : "");
            saveState.Text=$"{scopeTag} · 已指定 {rules.Count} 项"+(dirty?" · 有未保存修改":" · 当前没有未保存修改");
        }
        void Changed()
        {
            if(updating)return;
            RefreshActiveOverrides();
            var wasDirty=dirty;dirty=!JToken.DeepEquals(CaptureForm(),savedForm);HeaderText();
            if(writeProfileDraft!=null)writeProfileDraft(BuildCurrentPayload());
            if(dirty!=wasDirty)message(dirty
                ? (writeProfileDraft!=null ? "已写入当前配置草稿；点击顶栏“保存配置”或下方按钮立即提交。" : "事件设置已修改，点击“保存事件设置”后对后续事件生效。")
                : "当前事件设置没有未保存的修改。");
            PreferencesChanged?.Invoke();
        }
        void ShowEvent()
        {
            updating=true;
            try {
                if(results.SelectedItem is not EventRow row){selectedOption.IsEnabled=selectedOption.IsVisible=false;return;}
                var options=((JArray?)row.Data["options"] ?? new()).OfType<JObject>().ToArray();
                details.Text=$"{row.Data["title"]}\n{row.Data["owner"]}\n{row.Data["scope_label"]}\n参考收益：\n"+
                    string.Join("\n\n",options.Select(option=>$"{option["position"]}：{option["text"]}\n{option["effects"]}"));
                var choices=new[] {new OptionRow(0,"自动选择")}.Concat(options.Select(option=>new OptionRow((int)option["index"]!,"选"+option["position"]))).ToArray();
                selectedOption.IsVisible=true;selectedOption.ItemsSource=choices;
                selectedOption.SelectedItem=choices.FirstOrDefault(option=>option.Index==((int?)rules[row.Key] ?? 0)) ?? choices[0];
                selectedOption.IsEnabled=(bool?)row.Data["selectable"]!=false;
                if(!selectedOption.IsEnabled)details.Text+="\n此条角色身份尚未关联，仅展示参考。";
            } finally {updating=false;}
        }
        async Task Search()
        {
            var revision=++searchRevision;
            try {
                var response=await bridge.Request(new JObject {["action"]="search_events",["query"]=query.Text ?? "",["limit"]=40,
                    ["keys"]=configured.IsChecked==true ? new JArray(rules.Properties().Select(p=>p.Name)) : null});
                if(revision!=searchRevision)return;
                var rows=((JArray?)response["events"] ?? new()).OfType<JObject>().Select(row=>{
                    if((string?)row["key"] is string k && !string.IsNullOrEmpty(k))
                        knownEvents[k]=(JObject)row.DeepClone();
                    return new EventRow(row);
                }).ToArray();
                results.ItemsSource=rows;results.IsVisible=rows.Length>0;results.SelectedIndex=rows.Length>0 ? 0 : -1;
                counter.Text=$"找到 {response["total"] ?? 0} 项，显示 {rows.Length} 项。可用角色、卡名或事件名缩小范围。";
                if(rows.Length==0){details.Text="没有匹配事件。";selectedOption.IsEnabled=selectedOption.IsVisible=false;}
                RefreshActiveOverrides();
            } catch(Exception error){if(revision==searchRevision)counter.Text=error.Message;}
        }
        search.Click+=async(_,_)=>await Search();
        query.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;await Search();}};
        configured.IsCheckedChanged+=async(_,_)=>await Search();
        results.SelectionChanged+=(_,_)=>ShowEvent();
        selectedOption.SelectionChanged+=(_,_)=> {
            if(updating || results.SelectedItem is not EventRow row || selectedOption.SelectedItem is not OptionRow option)return;
            if(option.Index==0)rules.Remove(row.Key);else rules[row.Key]=option.Index;
            Changed();
        };
        targets.Changed+=()=>Changed();ignored.Changed+=()=>Changed();
        conserve.IsCheckedChanged+=(_,_)=>Changed();preserve.IsCheckedChanged+=(_,_)=>Changed();
        meiGamble.IsCheckedChanged+=(_,_)=>{meiThreshold.IsEnabled=meiSupported&&meiGamble.IsChecked==true;Changed();};meiThreshold.ValueChanged+=(_,_)=>Changed();
        clear.Click+=(_,_)=>{rules.RemoveAll();RefreshActiveOverrides();ShowEvent();Changed();message("事件指定已清空，点击保存生效。");};
        preset.Click+=(_,_)=>{targets.SetValues(new JArray("斗志高昂"));ignored.SetValues(new JArray("777","准备突围"));conserve.IsChecked=true;preserve.IsChecked=true;Changed();message("已填入自动取舍；点击保存生效，事件指定仍然优先。");};
        save.Click+=async(_,_)=> {
            save.IsEnabled=false;body.IsEnabled=automaticBody.IsEnabled=buttons.IsEnabled=false;
            try {
                var preferences=BuildCurrentPayload();
                var response=await bridge.Request(new JObject {["action"]="validate_event_preferences",["preferences"]=preferences});
                preferences=(JObject)response["preferences"]!;
                PersistPortableFallback(preferences);
                saved=(JObject)preferences.DeepClone();
                updating=true;
                LoadWithList(saved);
                RefreshWithList();
                inlineAcupuncture=AcupuncturePolicyWindow.PolicyFromPreferences(saved);
                needleCombo.SelectedIndex=NeedleComboIndex(inlineAcupuncture);
                needleStatus.Text="针灸："+AcupuncturePolicyWindow.Summary(saved);
                targets.SetValues(preferences["target_skills"]);ignored.SetValues(preferences["ignored_skills"]);
                savedForm=CaptureForm();updating=false;dirty=false;HeaderText();RefreshActiveOverrides();
                if(writeProfileDraft!=null)writeProfileDraft(preferences);
                if(saveProfileCallback!=null)await saveProfileCallback();
                var activeTargets=targets.DisplayText.Replace("\n","、");
                message($"事件设置已保存：{rules.Count} 项指定；目标技能：{(activeTargets.Length==0 ? "未指定" : activeTargets)}。对后续事件生效。");if(collapseAfterSave)IsExpanded=false;
                PreferencesChanged?.Invoke();
            } catch(Exception error){message(error.Message);}
            finally {updating=false;body.IsEnabled=automaticBody.IsEnabled=buttons.IsEnabled=true;save.IsEnabled=true;}
        };
        reloadBoundAction=()=> {
            var fromProfile=readProfilePrefs?.Invoke();
            var source=fromProfile is { Count: > 0 } ? (JObject)fromProfile.DeepClone() : (ReadPortablePreferences() ?? new JObject());
            updating=true;
            try {
                saved=(JObject)source.DeepClone();
                rules.RemoveAll();
                if(source["manual_choices"] is JObject mc)
                    foreach(var p in mc.Properties()) rules[p.Name]=p.Value.DeepClone();
                conserve.IsChecked=(bool?)source["conserve_energy"] ?? true;
                preserve.IsChecked=(bool?)source["prioritize_target_routes"] ?? true;
                var mp=source["mei_unlock_policy"] as JObject;
                meiGamble.IsChecked=(string?)mp?["mode"]=="gamble_when_low";
                meiThreshold.Value=(decimal?)mp?["below_vitality_percent"] ?? 50;
                meiThreshold.IsEnabled=meiSupported&&meiGamble.IsChecked==true;
                LoadWithList(source);
                RefreshWithList();
                inlineAcupuncture=AcupuncturePolicyWindow.PolicyFromPreferences(source);
                needleCombo.SelectedIndex=NeedleComboIndex(inlineAcupuncture);
                needleStatus.Text="针灸："+AcupuncturePolicyWindow.Summary(saved);
                targets.SetValues(source["target_skills"]);
                ignored.SetValues(source["ignored_skills"]);
                savedForm=CaptureForm();
                dirty=false;
                ShowEvent();
                RefreshActiveOverrides();
                HeaderText();
            } finally { updating=false; }
            PreferencesChanged?.Invoke();
        };
        RefreshWithList();
        RefreshActiveOverrides();
        HeaderText();
        choicesBody.Loaded+=(_,_)=>{
            if(rules.Properties().Any(p=>!knownEvents.ContainsKey(p.Name)&&!unresolvableKeys.Contains(p.Name)))
                RefreshActiveOverrides();
        };
        setMeiContextAction=(supported,reason)=>{
            meiSupported=supported;
            meiGamble.IsEnabled=supported;
            meiThreshold.IsEnabled=supported&&meiGamble.IsChecked==true;
            meiScopeNote.Text=reason ?? "";
            meiScopeNote.IsVisible=!string.IsNullOrWhiteSpace(reason);
        };
    }

    public void SetMeiGambleContext(bool supported,string? reason=null) => setMeiContextAction?.Invoke(supported,reason);

    public void PersistPortableFallback(JObject? preferences)
    {
        if(preferences==null)return;
        try {
            var configPath=Path.Combine(PacerRoot,"portable_config.json");
            var config=File.Exists(configPath) ? JObject.Parse(File.ReadAllText(configPath)) : new JObject();
            config["event_preferences"]=preferences.DeepClone();
            var temporary=configPath+".events.tmp";
            File.WriteAllText(temporary,config.ToString());
            File.Move(temporary,configPath,true);
        } catch { }
    }

    public void BindWorkflowProfile(Func<JObject?> readFromProfile,Action<JObject> writeToDraft,Func<string> profileLabel,Func<Task>? saveProfileAsync=null)
    {
        readProfilePrefs=readFromProfile;
        writeProfileDraft=writeToDraft;
        getProfileLabel=profileLabel;
        saveProfileCallback=saveProfileAsync;
        reloadBoundAction?.Invoke();
    }

    public void ReloadFromBoundProfile() => reloadBoundAction?.Invoke();
}
