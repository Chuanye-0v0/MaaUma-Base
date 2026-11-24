using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UmaCutPanel;

namespace MFAAvalonia.Helper;

/// <summary>Edits skill_timing.arc_policy for CM auto-compilation and custom tiered skill ladders.</summary>
public sealed class UmaArcSkillPolicyEditor : StackPanel
{
    private sealed record PresetItem(string? Id, string Label, string? DistanceType);
    private sealed record ChoiceItem(string? Key, string Label);
    private sealed record SkillOption(string SkillId, string Name, string Type, string Rarity, int BaseCost, string DbId)
    {
        public string DisplayLabel =>
            string.IsNullOrEmpty(Rarity) && BaseCost <= 0
                ? Name
                : $"{Name}  [{Type}/{Rarity} · {BaseCost}Pt]";
    }

    private sealed record LadderItemRow(int Index, string Entry, string Label)
    {
        public override string ToString() => $"{Index + 1}. {Label}";
    }

    private sealed record BucketDescriptor(int Id, string Title, bool IsTier, int TierIndex);

    private static readonly PresetItem[] TrackPresets =
    {
        new(null, "自动识别 / 按距离近似", null),
        new("longchamp_2400_turf", "隆尚 2400m · 草地（凯旋门 / 中距离）", "middle"),
        new("tokyo_2400_turf", "东京 2400m · 草地（日本德比 / 橡树）", "middle"),
        new("tokyo_1600_turf", "东京 1600m · 草地（安田 / 维多利亚英里）", "mile"),
        new("nakayama_2000_turf", "中山 2000m · 草地（皋月赏 / 紫苑）", "middle"),
        new("nakayama_2500_turf", "中山 2500m · 草地（有马纪念 / 长距离）", "long"),
        new("hanshin_2200_turf", "阪神 2200m · 草地（宝冢纪念）", "middle"),
        new("kyoto_3000_turf", "京都 3000m · 草地（菊花赏）", "long"),
        new("kyoto_3200_turf", "京都 3200m · 草地（天皇赏春）", "long"),
        new("chukyo_1200_turf", "中京 1200m · 草地（高松宫 / 短距离）", "short"),
        new("ooi_2000_dirt", "大井 2000m · 泥地（帝王赏 / 东京大赏典）", "middle"),
        new("kawasaki_2100_dirt_loh", "川崎 2100m · 泥地（LoH 12人 / 冬季）", "middle"),
    };

    private static readonly ChoiceItem[] PolicyModes =
    {
        new("auto_cm", "CM 大赛自动编译（auto_cm）"),
        new("tiered_ladder", "自定义技能梯队（tiered_ladder）"),
    };

    private static readonly ChoiceItem[] StyleChoices =
    {
        new(null, "不指定跑法（通用）"),
        new("leader", "领跑 (逃马 / leader)"),
        new("front", "跟前 (先行 / front)"),
        new("betweener", "居中 (差马 / betweener)"),
        new("chaser", "后追 (追马 / chaser)"),
    };

    private static readonly ChoiceItem[] Distances =
    {
        new(null, "不指定 / 自动"),
        new("short", "短距离 (<1600m)"),
        new("mile", "英里 (1600~1800m)"),
        new("middle", "中距离 (2000~2400m)"),
        new("long", "长距离 (>2400m)"),
    };

    private static readonly ChoiceItem[] WithinTierOrders =
    {
        new("discount_cost_first", "同档优先高折扣 / 低花费"),
        new("strict_order", "严格按表内顺序"),
    };

    private static readonly ChoiceItem[] UnlistedPolicies =
    {
        new("skip", "表外技能不买（skip）"),
        new("safe_cm_fallback", "表外按 CM 估值兜底（safe_cm_fallback）"),
    };

    private static readonly ChoiceItem[] ClassicStageModes =
    {
        new("stat_rescue", "属性落后时高折扣救急（stat_rescue）"),
        new("skip", "经典年凯旋门前不买（skip）"),
        new("allowlist", "仅按经典年白名单（allowlist）"),
    };

    private static readonly ChoiceItem[] SeniorStageModes =
    {
        new("t0_only", "只买 T0 并保留余点（t0_only）"),
        new("skip", "资深年凯旋门前不买（skip）"),
        new("full", "全梯队开放购买（full）"),
    };

    private static readonly ChoiceItem[] EvolutionBranches =
    {
        new("original", "原版进化（有啥进化啥 / original）"),
        new("scenario", "剧本进化（scenario）"),
        new("skip", "不进化（skip）"),
    };

    private static readonly string[] PresetDerivedTargetKeys =
    {
        "track_id", "distance_m", "surface", "rotation", "ground_condition", "season", "field_size",
    };

    private static readonly (string Id, string Label)[] DefaultTierDefs =
    {
        ("T0", "最优先"),
        ("T1", "次优先"),
        ("T2", "可选"),
        ("T3", "优先级 4"),
    };

    private static readonly BucketDescriptor[] Buckets =
    {
        new(0, "T0 最优先", true, 0),
        new(1, "T1 次优先", true, 1),
        new(2, "T2 可选", true, 2),
        new(3, "T3 优先级 4", true, 3),
        new(4, "禁买清单 (avoid)", false, -1),
        new(5, "终盘缺耐补回顺位", false, -1),
        new(6, "凑绿主池", false, -1),
        new(7, "凑绿垫底", false, -1),
    };

    private readonly CheckBox enabled = new() { Name = "ArcPolicyEnabled", Content = "启用凯旋门赛前与终盘目标选技" };
    private readonly ComboBox modePicker = new()
    {
        Name = "ArcPolicyMode",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = PolicyModes.Select(m => m.Label).ToArray(),
        SelectedIndex = 0,
    };
    private readonly CheckBox autoCm = new()
    {
        Name = "ArcPolicyAutoCm",
        Content = "启用 CM 大赛智能编译与终局清空余点（Ver3.4 几何窗口 + 回体门控 + 地固联动）",
        IsChecked = true,
    };
    private readonly ComboBox presetPicker = new()
    {
        Name = "ArcPolicyPresetId",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = TrackPresets.Select(p => p.Label).ToArray(),
        SelectedIndex = 0,
    };
    private readonly ComboBox stylePicker = new()
    {
        Name = "ArcPolicyStyle",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = StyleChoices.Select(s => s.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 175,
    };
    private readonly ComboBox distancePicker = new()
    {
        Name = "ArcPolicyDistanceType",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = Distances.Select(d => d.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 165,
    };
    private readonly NumericUpDown midReserve = new()
    {
        Name = "ArcPolicyMidReservePt",
        Minimum = 0,
        Maximum = 2000,
        Increment = 20,
        FormatString = "0",
        Value = 360,
        MinWidth = 140,
    };
    private readonly NumericUpDown minMidDiscount = new()
    {
        Name = "ArcPolicyMinMidDiscount",
        Minimum = 0,
        Maximum = 60,
        Increment = 5,
        FormatString = "0",
        Value = 20,
        MinWidth = 140,
    };

    private readonly StackPanel details = new() { Spacing = 8 };
    private readonly StackPanel autoCmSection = new() { Spacing = 8 };
    private readonly StackPanel ladderPanel = new() { Spacing = 10, IsVisible = false };

    // Strategy pack preset loader
    private readonly ComboBox packPresetPicker = new()
    {
        Name = "ArcLadderPackPreset",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        MinWidth = 240,
    };
    private readonly Button loadPackPresetBtn = new()
    {
        Name = "ArcLadderLoadPreset",
        Content = "载入预设到下方编辑",
        Padding = new Thickness(12, 5),
    };
    private readonly TextBlock packPresetSummary = Note("");
    private readonly StackPanel packPresetBar = new() { Spacing = 6 };
    private readonly TextBlock noPackNotice = Note("未检测到策略包技能梯队预设；你仍可在下方直接搜索添加技能，或通过高级 JSON 导入导出完整梯队配置。");

    // Multi-bucket skill editor
    private readonly WrapPanel bucketTabs = new() { Orientation = Orientation.Horizontal };
    private readonly Button[] bucketTabButtons = new Button[Buckets.Length];
    private readonly TextBox skillSearch = new()
    {
        Name = "ArcLadderSkillSearch",
        Watermark = "搜索技能名、简称/黑话或技能编号（或直接点“按输入名添加”）",
    };
    private readonly ListBox skillResults = new() { Name = "ArcLadderSkillResults", MaxHeight = 150, IsVisible = false };
    private readonly TextBlock skillSearchStatus = Note("");
    private readonly ListBox bucketItemsList = new() { Name = "ArcLadderBucketItems", MinHeight = 96, MaxHeight = 190 };
    private readonly Button addSelectedBtn = ActionBtn("ArcLadderAddSelected", "添加到当前组");
    private readonly Button addTypedBtn = ActionBtn("ArcLadderAddTyped", "按输入名添加");
    private readonly Button moveUpBtn = ActionBtn("ArcLadderMoveUp", "上移");
    private readonly Button moveDownBtn = ActionBtn("ArcLadderMoveDown", "下移");
    private readonly Button promoteTierBtn = ActionBtn("ArcLadderPromoteTier", "升一档");
    private readonly Button demoteTierBtn = ActionBtn("ArcLadderDemoteTier", "降一档");
    private readonly Button removeItemBtn = ActionBtn("ArcLadderRemoveItem", "移除");
    private readonly Button clearBucketBtn = ActionBtn("ArcLadderClearBucket", "清空本组");

    // Ladder rule controls
    private readonly ComboBox withinOrderPicker = new()
    {
        Name = "ArcLadderWithinOrder",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = WithinTierOrders.Select(o => o.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 200,
    };
    private readonly ComboBox unlistedPolicyPicker = new()
    {
        Name = "ArcLadderUnlistedPolicy",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = UnlistedPolicies.Select(o => o.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 220,
    };
    private readonly CheckBox allowLowerFallback = new()
    {
        Name = "ArcLadderAllowLowerFallback",
        Content = "高顺位金技能未到/买不起时，下位白自动降一档兜底",
        IsChecked = true,
    };

    private readonly CheckBox gwEnabled = new() { Name = "ArcLadderGwEnabled", Content = "启用地固 / 磐石开局前置门控", IsChecked = true };
    private readonly NumericUpDown gwMinSources = IntBox("ArcLadderGwMinSources", 1, 6, 1, 3);
    private readonly CheckBox gwReliablePair = new()
    {
        Name = "ArcLadderGwReliablePair",
        Content = "支持「3 稳定绿」与「2 稳定绿 + 2 主动开局」自动择优打包",
    };

    private readonly CheckBox staBoostEnabled = new() { Name = "ArcLadderStaBoostEnabled", Content = "终盘缺耐时自动将下方「缺耐补回顺位」技能提档至 T0" };
    private readonly NumericUpDown staBoostMin = IntBox("ArcLadderStaBoostMin", 0, 3000, 50, 1150);
    private readonly CheckBox staBoostHpGate = new() { Name = "ArcLadderStaBoostHpGate", Content = "启用 HP 耗尽测算门控（use_hp_gate）", IsChecked = true };

    private readonly CheckBox greenQuotaEnabled = new() { Name = "ArcLadderGreenQuotaEnabled", Content = "启用凑绿硬配额（从「凑绿主池」凑满数量，不足再启用「凑绿垫底」）" };
    private readonly NumericUpDown greenQuotaMinCount = IntBox("ArcLadderGreenQuotaMin", 1, 12, 1, 6);

    // Stage & evolution controls
    private readonly ComboBox classicModePicker = new()
    {
        Name = "ArcLadderClassicMode",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = ClassicStageModes.Select(o => o.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 220,
    };
    private readonly NumericUpDown classicReservePt = IntBox("ArcLadderClassicReserve", 0, 2000, 20, 360);
    private readonly NumericUpDown classicMinDiscount = IntBox("ArcLadderClassicDiscount", 0, 60, 5, 20);
    private readonly CheckBox classicAllowRecovery = new()
    {
        Name = "ArcLadderClassicAllowRecovery",
        Content = "经典年凯旋门前允许买回复技能（关闭可避免提前买耐）",
        IsChecked = true,
    };

    private readonly ComboBox seniorModePicker = new()
    {
        Name = "ArcLadderSeniorMode",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = SeniorStageModes.Select(o => o.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 220,
    };
    private readonly NumericUpDown seniorMaxSkills = IntBox("ArcLadderSeniorMaxSkills", 1, 10, 1, 5);
    private readonly NumericUpDown seniorReservePt = IntBox("ArcLadderSeniorReserve", 0, 2000, 20, 400);
    private readonly CheckBox seniorAllowRecovery = new()
    {
        Name = "ArcLadderSeniorAllowRecovery",
        Content = "资深年凯旋门前允许买回复技能",
        IsChecked = true,
    };

    private readonly CheckBox evoEnabled = new() { Name = "ArcLadderEvoEnabled", Content = "启用结算自动技能进化", IsChecked = true };
    private readonly ComboBox evoBranchPicker = new()
    {
        Name = "ArcLadderEvoBranch",
        HorizontalAlignment = HorizontalAlignment.Stretch,
        ItemsSource = EvolutionBranches.Select(o => o.Label).ToArray(),
        SelectedIndex = 0,
        MinWidth = 220,
    };

    // Advanced JSON & preserved lossless structures
    private readonly TextBlock preservedRulesSummary = Note("");
    private readonly TextBox jsonBox = new()
    {
        Name = "ArcLadderJsonBox",
        AcceptsReturn = true,
        TextWrapping = TextWrapping.Wrap,
        MinHeight = 140,
        MaxHeight = 280,
        FontFamily = new FontFamily("Consolas, Courier New, monospace"),
        FontSize = 12,
    };
    private readonly Button refreshJsonBtn = ActionBtn("ArcLadderJsonRefresh", "从上方表单刷新 JSON");
    private readonly Button applyJsonBtn = ActionBtn("ArcLadderJsonApply", "应用 JSON 到表单");
    private readonly TextBlock jsonStatus = Note("");

    private readonly TextBlock status = Note("");

    private IReadOnlyList<SkillOption> skillCatalog = Array.Empty<SkillOption>();
    private Dictionary<string, SkillOption> skillByKey = new(StringComparer.Ordinal);
    private HashSet<string> duplicateSkillNames = new(StringComparer.Ordinal);
    private IReadOnlyList<SkillLadderPresetEntry> packPresets = Array.Empty<SkillLadderPresetEntry>();

    private readonly List<List<string>> tierLists = new() { new(), new(), new(), new() };
    private readonly List<string> avoidList = new();
    private readonly List<string> staminaPromoList = new();
    private readonly List<string> greenPrimaryList = new();
    private readonly List<string> greenLastResortList = new();
    private readonly List<JObject> preservedExtraTiers = new();
    private readonly JObject[] preservedTierMeta = new JObject[4];
    private JArray preservedComboPromotions = new();
    private JArray preservedAccelGroupCaps = new();
    private JObject preservedEvoOverrides = new();
    private bool preservedClassicPreferWhite = true;
    private int preservedClassicMaxSkills = 2;
    private int preservedClassicSpeedThreshold = 900;
    private int preservedClassicStaminaThreshold = 600;
    private JArray preservedClassicAllowedTiers = new("T0", "T1");
    private JArray preservedSeniorAllowedTiers = new("T0");

    private int activeBucketId;
    private bool loading;
    private JObject loaded = new();
    private string? loadedPresetId;

    public event Action? Changed;

    public string Summary
    {
        get
        {
            if (enabled.IsChecked != true)
                return "CM 目标选技：未启用";
            var preset = SelectedPreset;
            var track = preset.Id == null ? SelectedDistance.Label.Split(' ')[0] : preset.Label.Split('·')[0].Trim();
            var style = SelectedStyle.Key == null ? "通用跑法" : SelectedStyle.Label.Split(' ')[0];
            if (IsTieredLadderMode)
            {
                return $"自定义技能梯队：{track} · {style}（T0 {tierLists[0].Count} / T1 {tierLists[1].Count} / T2 {tierLists[2].Count} / T3 {tierLists[3].Count}）";
            }
            if (autoCm.IsChecked != true)
                return "目标选技：仅用手动经典年白名单";
            return $"CM 智能选技：{track} · {style}";
        }
    }

    private bool IsTieredLadderMode => modePicker.SelectedIndex == 1;

    private PresetItem SelectedPreset =>
        presetPicker.SelectedIndex >= 0 && presetPicker.SelectedIndex < TrackPresets.Length
            ? TrackPresets[presetPicker.SelectedIndex]
            : TrackPresets[0];

    private ChoiceItem SelectedStyle =>
        stylePicker.SelectedIndex >= 0 && stylePicker.SelectedIndex < StyleChoices.Length
            ? StyleChoices[stylePicker.SelectedIndex]
            : StyleChoices[0];

    private ChoiceItem SelectedDistance =>
        distancePicker.SelectedIndex >= 0 && distancePicker.SelectedIndex < Distances.Length
            ? Distances[distancePicker.SelectedIndex]
            : Distances[0];

    public UmaArcSkillPolicyEditor()
    {
        Name = "PacerArcSkillPolicy";
        Spacing = 8;
        for (int i = 0; i < preservedTierMeta.Length; i++)
            preservedTierMeta[i] = new JObject();

        LoadSkillCatalog();
        RefreshPackPresetCatalog();

        Children.Add(enabled);
        Children.Add(Note("针对目标大赛赛道编译 T0/T1/T2 优先级、Ver3.4 2101m+ 耐力回体阈值及地固前置绑定；终局自动清空安全余点。"));

        details.Children.Add(Field("选技模式", modePicker));
        details.Children.Add(Field("目标 CM 赛道预设", presetPicker));
        var row = new WrapPanel();
        row.Children.Add(Field("目标跑法", stylePicker));
        row.Children.Add(Field("近似距离（未选预设时使用）", distancePicker));
        details.Children.Add(row);

        autoCmSection.Children.Add(autoCm);
        var budgets = new WrapPanel();
        budgets.Children.Add(Field("经典年赛前保留 Pt（默认 360）", midReserve));
        budgets.Children.Add(Field("经典年最低折扣（% · 默认 20）", minMidDiscount));
        autoCmSection.Children.Add(budgets);
        details.Children.Add(autoCmSection);

        BuildLadderPanel();
        details.Children.Add(ladderPanel);

        Children.Add(details);
        Children.Add(status);

        WireEvents();
        RefreshBucketTabs();
        RenderBucketItems(-1);
        FilterSearch();
        RefreshUiState();
    }

    private void BuildLadderPanel()
    {
        // 1. Strategy pack preset loader
        var presetRow = new WrapPanel { Orientation = Orientation.Horizontal };
        presetRow.Children.Add(packPresetPicker);
        loadPackPresetBtn.Margin = new Thickness(8, 0, 0, 0);
        presetRow.Children.Add(loadPackPresetBtn);
        packPresetBar.Children.Add(Note("策略包推荐梯队（载入后可直接在下方删改技能、调档或微调规则）"));
        packPresetBar.Children.Add(presetRow);
        packPresetBar.Children.Add(packPresetSummary);
        ladderPanel.Children.Add(packPresetBar);
        ladderPanel.Children.Add(noPackNotice);

        // 2. Multi-bucket skill editor
        for (int i = 0; i < Buckets.Length; i++)
        {
            var bucketId = i;
            var btn = new Button
            {
                Name = $"ArcLadderBucket_{bucketId}",
                Padding = new Thickness(9, 4),
                Margin = new Thickness(0, 0, 6, 6),
                Content = Buckets[i].Title,
            };
            btn.Click += (_, _) => SelectBucket(bucketId);
            bucketTabButtons[i] = btn;
            bucketTabs.Children.Add(btn);
        }

        skillResults.ItemTemplate = new FuncDataTemplate<SkillOption>((opt, _) =>
            new TextBlock { Text = opt?.DisplayLabel ?? "", TextWrapping = TextWrapping.Wrap });

        var searchActions = new WrapPanel { Orientation = Orientation.Horizontal };
        searchActions.Children.Add(addSelectedBtn);
        searchActions.Children.Add(addTypedBtn);

        var listActions = new WrapPanel { Orientation = Orientation.Horizontal };
        foreach (var b in new[] { moveUpBtn, moveDownBtn, promoteTierBtn, demoteTierBtn, removeItemBtn, clearBucketBtn })
            listActions.Children.Add(b);

        var editorBox = new StackPanel
        {
            Spacing = 6,
            Children =
            {
                Note("技能分组编排（点击切换 T0~T3、禁买、补耐或凑绿组）"),
                bucketTabs,
                skillSearch,
                skillResults,
                skillSearchStatus,
                searchActions,
                bucketItemsList,
                listActions,
            },
        };
        ladderPanel.Children.Add(editorBox);

        // 3. Ladder compilation rules
        var orderWrap = new WrapPanel();
        orderWrap.Children.Add(Field("同档内买法排序", withinOrderPicker));
        orderWrap.Children.Add(Field("未上榜技能处理", unlistedPolicyPicker));
        var ruleChecks = new StackPanel { Spacing = 6, Children = { orderWrap, allowLowerFallback } };

        var gwRow = new WrapPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center };
        gwRow.Children.Add(Field("最少开局触发源（min_sources）", gwMinSources));
        var gwPanel = new StackPanel { Spacing = 4, Children = { gwEnabled, gwRow, gwReliablePair } };

        var staRow = new WrapPanel { Orientation = Orientation.Horizontal };
        staRow.Children.Add(Field("最低目标耐力门槛（min_stamina）", staBoostMin));
        var staPanel = new StackPanel { Spacing = 4, Children = { staBoostEnabled, staRow, staBoostHpGate } };

        var greenRow = new WrapPanel { Orientation = Orientation.Horizontal };
        greenRow.Children.Add(Field("最少凑满绿技数量（min_count）", greenQuotaMinCount));
        var greenPanel = new StackPanel { Spacing = 4, Children = { greenQuotaEnabled, greenRow } };

        ladderPanel.Children.Add(new Expander
        {
            Name = "ArcLadderRulesExpander",
            Header = "梯队通用规则 · 磐石打包 / 缺耐补回 / 凑绿配额",
            Content = new StackPanel { Spacing = 10, Children = { ruleChecks, gwPanel, staPanel, greenPanel } },
        });

        // 4. Stage & evolution policy
        var classicWrap = new WrapPanel();
        classicWrap.Children.Add(Field("经典年买技模式", classicModePicker));
        classicWrap.Children.Add(Field("经典年保留 Pt", classicReservePt));
        classicWrap.Children.Add(Field("经典年最低折扣 %", classicMinDiscount));

        var seniorWrap = new WrapPanel();
        seniorWrap.Children.Add(Field("资深年凯旋门前模式", seniorModePicker));
        seniorWrap.Children.Add(Field("资深年最多买几个", seniorMaxSkills));
        seniorWrap.Children.Add(Field("资深年保留 Pt", seniorReservePt));

        var evoWrap = new WrapPanel();
        evoWrap.Children.Add(Field("进化分支偏好", evoBranchPicker));

        ladderPanel.Children.Add(new Expander
        {
            Name = "ArcLadderStageEvoExpander",
            Header = "凯旋门阶段买法与结算进化策略",
            Content = new StackPanel
            {
                Spacing = 10,
                Children =
                {
                    classicWrap,
                    classicAllowRecovery,
                    seniorWrap,
                    seniorAllowRecovery,
                    evoEnabled,
                    evoWrap,
                },
            },
        });

        // 5. Preserved combo summary & advanced JSON import/export
        ladderPanel.Children.Add(preservedRulesSummary);
        var jsonBtnRow = new WrapPanel { Orientation = Orientation.Horizontal };
        jsonBtnRow.Children.Add(refreshJsonBtn);
        jsonBtnRow.Children.Add(applyJsonBtn);
        ladderPanel.Children.Add(new Expander
        {
            Name = "ArcLadderJsonExpander",
            Header = "高级 JSON 导入 / 导出（可直接粘贴或微调完整规则，含联动提档）",
            Content = new StackPanel
            {
                Spacing = 6,
                Children = { jsonBtnRow, jsonStatus, jsonBox },
            },
        });
    }

    private void WireEvents()
    {
        enabled.IsCheckedChanged += (_, _) => Notify();
        modePicker.SelectionChanged += (_, _) => Notify();
        autoCm.IsCheckedChanged += (_, _) => Notify();
        presetPicker.SelectionChanged += (_, _) =>
        {
            if (!loading) SyncPresetDistanceState();
            Notify();
        };
        stylePicker.SelectionChanged += (_, _) => Notify();
        distancePicker.SelectionChanged += (_, _) => Notify();
        midReserve.ValueChanged += (_, _) => Notify();
        minMidDiscount.ValueChanged += (_, _) => Notify();

        packPresetPicker.SelectionChanged += (_, _) => UpdatePackPresetSummary();
        loadPackPresetBtn.Click += (_, _) => LoadSelectedPackPreset();

        skillSearch.TextChanged += (_, _) => FilterSearch();
        skillResults.SelectionChanged += (_, _) => UpdateListButtons();
        skillResults.DoubleTapped += (_, _) => AddSelectedSkill();
        bucketItemsList.SelectionChanged += (_, _) => UpdateListButtons();
        addSelectedBtn.Click += (_, _) => AddSelectedSkill();
        addTypedBtn.Click += (_, _) => AddTypedSkill();
        moveUpBtn.Click += (_, _) => MoveSelectedItem(-1);
        moveDownBtn.Click += (_, _) => MoveSelectedItem(1);
        promoteTierBtn.Click += (_, _) => ShiftSelectedTier(-1);
        demoteTierBtn.Click += (_, _) => ShiftSelectedTier(1);
        removeItemBtn.Click += (_, _) => RemoveSelectedItem();
        clearBucketBtn.Click += (_, _) => ClearCurrentBucket();

        withinOrderPicker.SelectionChanged += (_, _) => Notify();
        unlistedPolicyPicker.SelectionChanged += (_, _) => Notify();
        allowLowerFallback.IsCheckedChanged += (_, _) => Notify();
        gwEnabled.IsCheckedChanged += (_, _) => Notify();
        gwMinSources.ValueChanged += (_, _) => Notify();
        gwReliablePair.IsCheckedChanged += (_, _) => Notify();
        staBoostEnabled.IsCheckedChanged += (_, _) => Notify();
        staBoostMin.ValueChanged += (_, _) => Notify();
        staBoostHpGate.IsCheckedChanged += (_, _) => Notify();
        greenQuotaEnabled.IsCheckedChanged += (_, _) => Notify();
        greenQuotaMinCount.ValueChanged += (_, _) => Notify();

        classicModePicker.SelectionChanged += (_, _) => Notify();
        classicReservePt.ValueChanged += (_, _) => Notify();
        classicMinDiscount.ValueChanged += (_, _) => Notify();
        classicAllowRecovery.IsCheckedChanged += (_, _) => Notify();
        seniorModePicker.SelectionChanged += (_, _) => Notify();
        seniorMaxSkills.ValueChanged += (_, _) => Notify();
        seniorReservePt.ValueChanged += (_, _) => Notify();
        seniorAllowRecovery.IsCheckedChanged += (_, _) => Notify();
        evoEnabled.IsCheckedChanged += (_, _) => Notify();
        evoBranchPicker.SelectionChanged += (_, _) => Notify();

        refreshJsonBtn.Click += (_, _) => RefreshJsonEditorText();
        applyJsonBtn.Click += (_, _) => ApplyJsonEditorText();
    }

    private void RefreshPackPresetCatalog()
    {
        packPresets = StrategyPackCatalog.SkillLadderPresets;
        packPresetBar.IsVisible = packPresets.Count > 0;
        noPackNotice.IsVisible = packPresets.Count == 0;
        if (packPresets.Count > 0)
        {
            packPresetPicker.ItemsSource = packPresets.Select(p => p.Name).ToArray();
            if (packPresetPicker.SelectedIndex < 0 || packPresetPicker.SelectedIndex >= packPresets.Count)
                packPresetPicker.SelectedIndex = 0;
        }
        else
        {
            packPresetPicker.ItemsSource = Array.Empty<string>();
        }
        UpdatePackPresetSummary();
    }

    private void UpdatePackPresetSummary()
    {
        var idx = packPresetPicker.SelectedIndex;
        if (idx >= 0 && idx < packPresets.Count)
            packPresetSummary.Text = packPresets[idx].Summary;
        else
            packPresetSummary.Text = "";
    }

    private void LoadSelectedPackPreset()
    {
        var idx = packPresetPicker.SelectedIndex;
        if (idx < 0 || idx >= packPresets.Count) return;
        try
        {
            var presetObj = JObject.Parse(packPresets[idx].PolicyJson);
            presetObj["enabled"] = true;
            presetObj["mode"] = "tiered_ladder";
            presetObj.Remove("ladder_preset_id");
            Load(presetObj);
            jsonStatus.Text = $"已载入策略包预设「{packPresets[idx].Name}」，可在下方任意微调。";
            Notify();
        }
        catch (Exception ex)
        {
            jsonStatus.Text = "预设解析失败：" + ex.Message;
        }
    }

    private void LoadSkillCatalog()
    {
        var baseDir = AppContext.BaseDirectory;
        var appRoot = Path.GetFullPath(baseDir);
        if (string.Equals(Path.GetFileName(Path.TrimEndingDirectorySeparator(appRoot)), "pacer", StringComparison.OrdinalIgnoreCase))
            appRoot = Path.GetDirectoryName(Path.TrimEndingDirectorySeparator(appRoot)) ?? appRoot;

        foreach (var candidate in new[]
        {
            Path.Combine(appRoot, "pacer", "backend", "data", "skills", "cn_larc_end", "cn_skill_planner_catalog.json"),
            Path.Combine(appRoot, "backend", "data", "skills", "cn_larc_end", "cn_skill_planner_catalog.json"),
            Path.Combine(appRoot, "backend", "data", "skills", "cn-20260916", "cn_skill_planner_catalog.json"),
        })
        {
            if (!File.Exists(candidate)) continue;
            try
            {
                var doc = JObject.Parse(File.ReadAllText(candidate));
                if (doc["skills"] is JArray arr)
                {
                    var rows = new List<SkillOption>();
                    var seenIds = new HashSet<string>(StringComparer.Ordinal);
                    foreach (var item in arr.OfType<JObject>())
                    {
                        var sid = ((string?)item["skill_id"] ?? (string?)item["id"] ?? "").Trim();
                        var name = ((string?)item["name_zh_cn"] ?? (string?)item["name"] ?? "").Trim();
                        var rarity = ((string?)item["rarity"] ?? "").Trim();
                        var baseCost = (int?)item["base_cost"] ?? 0;
                        if (string.IsNullOrEmpty(sid) || string.IsNullOrEmpty(name)) continue;
                        if (string.Equals(rarity, "unique", StringComparison.OrdinalIgnoreCase) && baseCost == 0) continue;
                        if (!seenIds.Add(sid)) continue;
                        var dbId = item["external_truth"]?["db_id"]?.ToString() ?? item["knowledge"]?["db_id"]?.ToString() ?? "";
                        rows.Add(new SkillOption(sid, name, ((string?)item["type"] ?? "").Trim(), rarity, baseCost, dbId));
                    }
                    if (rows.Count > 0)
                    {
                        ApplySkillCatalog(rows);
                        return;
                    }
                }
            }
            catch
            {
                // Fall back to next catalog source.
            }
        }

        var fallback = EventSkillPicker.LoadCatalog(baseDir, out _);
        if (fallback.Count > 0)
        {
            ApplySkillCatalog(fallback.Select(c => new SkillOption(c.Id, c.Name, "", "", 0, c.DbId)).ToList());
        }
    }

    private void ApplySkillCatalog(List<SkillOption> rows)
    {
        skillCatalog = rows;
        var map = new Dictionary<string, SkillOption>(StringComparer.Ordinal);
        foreach (var opt in rows)
        {
            map.TryAdd(opt.SkillId, opt);
            map.TryAdd(opt.Name, opt);
        }
        skillByKey = map;
        duplicateSkillNames = rows
            .GroupBy(r => r.Name, StringComparer.Ordinal)
            .Where(g => g.Select(r => r.SkillId).Distinct(StringComparer.Ordinal).Skip(1).Any())
            .Select(g => g.Key)
            .ToHashSet(StringComparer.Ordinal);
    }

    private string EntryForOption(SkillOption opt) =>
        duplicateSkillNames.Contains(opt.Name) ? opt.SkillId : opt.Name;

    private List<string> BucketList(int bucketId) => bucketId switch
    {
        >= 0 and <= 3 => tierLists[bucketId],
        4 => avoidList,
        5 => staminaPromoList,
        6 => greenPrimaryList,
        7 => greenLastResortList,
        _ => tierLists[0],
    };

    private string FormatSkillEntry(string entry)
    {
        if (skillByKey.TryGetValue(entry, out var opt))
            return string.IsNullOrEmpty(opt.Rarity) ? opt.Name : $"{opt.Name} ({opt.Rarity})";
        return entry;
    }

    private void SelectBucket(int bucketId)
    {
        activeBucketId = Math.Clamp(bucketId, 0, Buckets.Length - 1);
        RefreshBucketTabs();
        RenderBucketItems(-1);
    }

    private void RefreshBucketTabs()
    {
        for (int i = 0; i < Buckets.Length; i++)
        {
            var count = BucketList(i).Count;
            var btn = bucketTabButtons[i];
            btn.Content = $"{Buckets[i].Title} ({count})";
            btn.FontWeight = i == activeBucketId ? FontWeight.Bold : FontWeight.Normal;
        }
    }

    private void RenderBucketItems(int selectIndex)
    {
        var list = BucketList(activeBucketId);
        bucketItemsList.ItemsSource = list.Select((e, i) => new LadderItemRow(i, e, FormatSkillEntry(e))).ToArray();
        bucketItemsList.SelectedIndex = selectIndex >= 0 && selectIndex < list.Count ? selectIndex : -1;
        UpdateListButtons();
    }

    private void FilterSearch()
    {
        var query = (skillSearch.Text ?? "").Trim();
        addTypedBtn.IsEnabled = query.Length > 0;
        if (query.Length == 0)
        {
            skillResults.ItemsSource = Array.Empty<SkillOption>();
            skillResults.IsVisible = false;
            skillSearchStatus.Text = skillCatalog.Count == 0
                ? "未在本地目录找到技能数据库；仍可输入准确技能名后点击“按输入名添加”。"
                : $"已加载 {skillCatalog.Count} 项技能；支持按技能名、编号或简称搜索。";
        }
        else
        {
            var aliasTargets = EventSkillPicker.Aliases
                .Where(kv => kv.Key.Contains(query, StringComparison.OrdinalIgnoreCase))
                .Select(kv => kv.Value)
                .ToHashSet(StringComparer.OrdinalIgnoreCase);
            var matches = skillCatalog.Where(opt =>
                (opt.Name + " " + opt.SkillId + " " + opt.DbId + " " + opt.Type)
                .Contains(query, StringComparison.OrdinalIgnoreCase)
                || aliasTargets.Contains(opt.Name)).ToArray();
            skillResults.ItemsSource = matches.Take(40).ToArray();
            skillResults.IsVisible = matches.Length > 0;
            skillResults.SelectedIndex = matches.Length > 0 ? 0 : -1;
            skillSearchStatus.Text = matches.Length == 0
                ? "词库未匹配到该关键词，可直接点“按输入名添加”。"
                : $"找到 {matches.Length} 项（显示前 {Math.Min(40, matches.Length)} 项），双击可直接加入当前组。";
        }
        UpdateListButtons();
    }

    private void UpdateListButtons()
    {
        var list = BucketList(activeBucketId);
        var idx = bucketItemsList.SelectedIndex;
        var hasSel = idx >= 0 && idx < list.Count;
        var isTier = Buckets[activeBucketId].IsTier;
        addSelectedBtn.IsEnabled = skillResults.SelectedItem is SkillOption;
        addTypedBtn.IsEnabled = !string.IsNullOrWhiteSpace(skillSearch.Text);
        moveUpBtn.IsEnabled = hasSel && idx > 0;
        moveDownBtn.IsEnabled = hasSel && idx < list.Count - 1;
        promoteTierBtn.IsEnabled = hasSel && isTier && activeBucketId > 0;
        demoteTierBtn.IsEnabled = hasSel && isTier && activeBucketId < 3;
        removeItemBtn.IsEnabled = hasSel;
        clearBucketBtn.IsEnabled = list.Count > 0;
    }

    private void AddEntryToCurrentBucket(string rawEntry)
    {
        var clean = rawEntry.Trim();
        if (string.IsNullOrEmpty(clean)) return;
        var list = BucketList(activeBucketId);
        var existingIdx = list.FindIndex(x => string.Equals(x, clean, StringComparison.Ordinal));
        if (existingIdx >= 0)
        {
            RenderBucketItems(existingIdx);
            return;
        }
        if (Buckets[activeBucketId].IsTier)
        {
            // Keep tier placement unique across T0..T3 so UI reflects backend resolution cleanly.
            for (int t = 0; t < 4; t++)
            {
                if (t != activeBucketId)
                    tierLists[t].RemoveAll(x => string.Equals(x, clean, StringComparison.Ordinal));
            }
        }
        list.Add(clean);
        RefreshBucketTabs();
        RenderBucketItems(list.Count - 1);
        Notify();
    }

    private void AddSelectedSkill()
    {
        if (skillResults.SelectedItem is not SkillOption opt) return;
        AddEntryToCurrentBucket(EntryForOption(opt));
    }

    private void AddTypedSkill()
    {
        var text = (skillSearch.Text ?? "").Trim();
        if (string.IsNullOrEmpty(text)) return;
        AddEntryToCurrentBucket(text);
    }

    private void MoveSelectedItem(int delta)
    {
        var list = BucketList(activeBucketId);
        var idx = bucketItemsList.SelectedIndex;
        var next = idx + delta;
        if (idx < 0 || next < 0 || next >= list.Count) return;
        (list[idx], list[next]) = (list[next], list[idx]);
        RenderBucketItems(next);
        Notify();
    }

    private void ShiftSelectedTier(int tierDelta)
    {
        if (!Buckets[activeBucketId].IsTier) return;
        var list = BucketList(activeBucketId);
        var idx = bucketItemsList.SelectedIndex;
        var targetBucket = activeBucketId + tierDelta;
        if (idx < 0 || idx >= list.Count || targetBucket < 0 || targetBucket > 3) return;
        var entry = list[idx];
        list.RemoveAt(idx);
        var dst = tierLists[targetBucket];
        if (!dst.Any(x => string.Equals(x, entry, StringComparison.Ordinal)))
            dst.Add(entry);
        RefreshBucketTabs();
        RenderBucketItems(Math.Min(idx, list.Count - 1));
        Notify();
    }

    private void RemoveSelectedItem()
    {
        var list = BucketList(activeBucketId);
        var idx = bucketItemsList.SelectedIndex;
        if (idx < 0 || idx >= list.Count) return;
        list.RemoveAt(idx);
        RefreshBucketTabs();
        RenderBucketItems(Math.Min(idx, list.Count - 1));
        Notify();
    }

    private void ClearCurrentBucket()
    {
        var list = BucketList(activeBucketId);
        if (list.Count == 0) return;
        list.Clear();
        RefreshBucketTabs();
        RenderBucketItems(-1);
        Notify();
    }

    private void RefreshPreservedSummary()
    {
        var parts = new List<string>();
        if (preservedComboPromotions.Count > 0) parts.Add($"联动提档 combo_promotions：{preservedComboPromotions.Count} 条");
        if (preservedAccelGroupCaps.Count > 0) parts.Add($"加速组上限 accel_group_caps：{preservedAccelGroupCaps.Count} 条");
        if (preservedExtraTiers.Count > 0) parts.Add($"额外档位：{preservedExtraTiers.Count} 组");
        if (preservedEvoOverrides.Count > 0) parts.Add($"进化单技覆写：{preservedEvoOverrides.Count} 项");
        preservedRulesSummary.Text = parts.Count == 0
            ? "附加高级规则（combo_promotions 等）：无；如需配置联动规则可展开下方高级 JSON。"
            : "已无损保留高级规则：" + string.Join(" · ", parts);
    }

    private void RefreshJsonEditorText()
    {
        jsonBox.Text = ReadCore(validate: false).ToString(Formatting.Indented);
        jsonStatus.Text = "已同步当前表单配置到 JSON。";
    }

    private void ApplyJsonEditorText()
    {
        var raw = jsonBox.Text ?? "";
        if (string.IsNullOrWhiteSpace(raw))
        {
            jsonStatus.Text = "JSON 为空，未应用。";
            return;
        }
        try
        {
            var parsed = JObject.Parse(raw);
            Load(parsed);
            jsonStatus.Text = "已将 JSON 规则应用到表单。";
            Notify();
        }
        catch (Exception ex)
        {
            jsonStatus.Text = "JSON 解析失败：" + ex.Message;
        }
    }

    private static TextBlock Note(string text) => new() { Text = text, FontSize = 12, TextWrapping = TextWrapping.Wrap };

    private static Button ActionBtn(string name, string label) => new()
    {
        Name = name,
        Content = label,
        Padding = new Thickness(10, 4),
        Margin = new Thickness(0, 0, 5, 4),
    };

    private static NumericUpDown IntBox(string name, decimal min, decimal max, decimal inc, decimal val) => new()
    {
        Name = name,
        Minimum = min,
        Maximum = max,
        Increment = inc,
        FormatString = "0",
        Value = val,
        MinWidth = 135,
    };

    private static Control Field(string label, Control field) =>
        new StackPanel { Spacing = 4, Margin = new Thickness(0, 0, 12, 6), Children = { Note(label), field } };

    private static int FindChoiceIndex(ChoiceItem[] choices, string? key, int fallback = 0)
    {
        var idx = Array.FindIndex(choices, c => string.Equals(c.Key, key, StringComparison.Ordinal));
        return idx >= 0 ? idx : fallback;
    }

    private static void ReadStringArray(JToken? token, List<string> target)
    {
        target.Clear();
        if (token is not JArray arr) return;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in arr)
        {
            if (item.Type != JTokenType.String) continue;
            var s = ((string?)item ?? "").Trim();
            if (!string.IsNullOrEmpty(s) && seen.Add(s))
                target.Add(s);
        }
    }

    private void SyncPresetDistanceState()
    {
        var preset = SelectedPreset;
        if (preset.Id != null)
        {
            var idx = Array.FindIndex(Distances, d => d.Key == preset.DistanceType);
            if (idx >= 0) distancePicker.SelectedIndex = idx;
        }
        var targetEnabled = enabled.IsChecked == true && (IsTieredLadderMode || autoCm.IsChecked == true);
        distancePicker.IsEnabled = targetEnabled && preset.Id == null;
    }

    private void RefreshUiState()
    {
        var on = enabled.IsChecked == true;
        var tiered = IsTieredLadderMode;
        var cmOn = autoCm.IsChecked == true;

        details.IsEnabled = on;
        modePicker.IsEnabled = on;
        autoCmSection.IsVisible = !tiered;
        ladderPanel.IsVisible = tiered;
        ladderPanel.IsEnabled = on && tiered;

        presetPicker.IsEnabled = on && (tiered || cmOn);
        stylePicker.IsEnabled = on && (tiered || cmOn);
        midReserve.IsEnabled = on;
        minMidDiscount.IsEnabled = on;
        SyncPresetDistanceState();

        gwMinSources.IsEnabled = gwEnabled.IsChecked == true;
        gwReliablePair.IsEnabled = gwEnabled.IsChecked == true;
        staBoostMin.IsEnabled = staBoostEnabled.IsChecked == true;
        staBoostHpGate.IsEnabled = staBoostEnabled.IsChecked == true;
        greenQuotaMinCount.IsEnabled = greenQuotaEnabled.IsChecked == true;
        evoBranchPicker.IsEnabled = evoEnabled.IsChecked == true;

        RefreshPreservedSummary();
        status.Text = Summary;
    }

    private void Notify()
    {
        if (loading) return;
        RefreshUiState();
        Changed?.Invoke();
    }

    public void Load(JToken? value)
    {
        loading = true;
        try
        {
            RefreshPackPresetCatalog();
            var obj = value as JObject;
            loaded = obj == null ? new JObject() : (JObject)obj.DeepClone();
            enabled.IsChecked = (bool?)obj?["enabled"] == true;
            autoCm.IsChecked = (bool?)obj?["auto_cm"] != false;

            var isLadder = string.Equals((string?)obj?["mode"], "tiered_ladder", StringComparison.Ordinal)
                || obj?["ladder"] is JObject
                || !string.IsNullOrWhiteSpace((string?)obj?["ladder_preset_id"]);
            modePicker.SelectedIndex = isLadder ? 1 : 0;

            loadedPresetId = (string?)obj?["preset_id"];
            var presetIdx = Array.FindIndex(TrackPresets, p => string.Equals(p.Id, loadedPresetId, StringComparison.Ordinal));
            presetPicker.SelectedIndex = presetIdx >= 0 ? presetIdx : 0;

            var target = obj?["final_target"] as JObject;
            var style = (string?)target?["style"];
            stylePicker.SelectedIndex = FindChoiceIndex(StyleChoices, style, 0);

            var dist = (string?)target?["distance_type"];
            if (SelectedPreset.Id != null && SelectedPreset.DistanceType != null)
                dist = SelectedPreset.DistanceType;
            distancePicker.SelectedIndex = FindChoiceIndex(Distances, dist, 0);

            midReserve.Value = Math.Clamp((int?)obj?["mid_reserve_pt"] ?? 360, 0, 2000);
            minMidDiscount.Value = Math.Clamp((int?)obj?["min_mid_discount_percent"] ?? 20, 0, 60);

            LoadLadderFields(obj);
            RefreshBucketTabs();
            RenderBucketItems(-1);
            RefreshUiState();
            jsonBox.Text = ReadCore(validate: false).ToString(Formatting.Indented);
        }
        finally
        {
            loading = false;
        }
    }

    private void LoadLadderFields(JObject? obj)
    {
        foreach (var list in tierLists) list.Clear();
        avoidList.Clear();
        staminaPromoList.Clear();
        greenPrimaryList.Clear();
        greenLastResortList.Clear();
        preservedExtraTiers.Clear();
        for (int i = 0; i < 4; i++)
            preservedTierMeta[i] = new JObject { ["id"] = DefaultTierDefs[i].Id, ["label"] = DefaultTierDefs[i].Label };

        var ladder = obj?["ladder"] as JObject;
        if (ladder != null)
        {
            if (ladder["tiers"] is JArray rawTiers)
            {
                for (int i = 0; i < rawTiers.Count; i++)
                {
                    if (rawTiers[i] is not JObject tObj) continue;
                    if (i < 4)
                    {
                        var meta = new JObject();
                        meta["id"] = !string.IsNullOrWhiteSpace((string?)tObj["id"]) ? (string?)tObj["id"] : DefaultTierDefs[i].Id;
                        meta["label"] = !string.IsNullOrWhiteSpace((string?)tObj["label"]) ? (string?)tObj["label"] : DefaultTierDefs[i].Label;
                        if (tObj["within_tier_order"] != null) meta["within_tier_order"] = tObj["within_tier_order"]!.DeepClone();
                        if (tObj["max_count"] != null) meta["max_count"] = tObj["max_count"]!.DeepClone();
                        preservedTierMeta[i] = meta;
                        ReadStringArray(tObj["skills"], tierLists[i]);
                    }
                    else
                    {
                        preservedExtraTiers.Add((JObject)tObj.DeepClone());
                    }
                }
            }
            ReadStringArray(ladder["avoid"], avoidList);
            withinOrderPicker.SelectedIndex = FindChoiceIndex(WithinTierOrders, (string?)ladder["within_tier_order"], 0);
            unlistedPolicyPicker.SelectedIndex = FindChoiceIndex(UnlistedPolicies, (string?)ladder["unlisted_policy"], 0);
            allowLowerFallback.IsChecked = (bool?)ladder["allow_lower_fallback"] != false;

            var gw = ladder["groundwork_gate"] as JObject;
            gwEnabled.IsChecked = (bool?)gw?["enabled"] != false;
            gwMinSources.Value = Math.Clamp((int?)gw?["min_sources"] ?? 3, 1, 6);
            gwReliablePair.IsChecked = (bool?)gw?["reliable_active_pair"] == true;

            var sta = ladder["stamina_gap_boost"] as JObject;
            staBoostEnabled.IsChecked = (bool?)sta?["enabled"] == true;
            staBoostMin.Value = Math.Clamp((int?)sta?["min_stamina"] ?? 1150, 0, 3000);
            staBoostHpGate.IsChecked = (bool?)sta?["use_hp_gate"] != false;
            ReadStringArray(sta?["promote_skills"], staminaPromoList);

            var gq = ladder["green_skill_quota"] as JObject;
            greenQuotaEnabled.IsChecked = (bool?)gq?["enabled"] == true;
            greenQuotaMinCount.Value = Math.Clamp((int?)gq?["min_count"] ?? 6, 1, 12);
            ReadStringArray(gq?["skills"], greenPrimaryList);
            ReadStringArray(gq?["last_resort_skills"], greenLastResortList);

            preservedComboPromotions = ladder["combo_promotions"] is JArray cp ? (JArray)cp.DeepClone() : new JArray();
            preservedAccelGroupCaps = ladder["accel_group_caps"] is JArray ac ? (JArray)ac.DeepClone() : new JArray();
        }
        else
        {
            withinOrderPicker.SelectedIndex = 0;
            unlistedPolicyPicker.SelectedIndex = 0;
            allowLowerFallback.IsChecked = true;
            gwEnabled.IsChecked = true;
            gwMinSources.Value = 3;
            gwReliablePair.IsChecked = false;
            staBoostEnabled.IsChecked = false;
            staBoostMin.Value = 1150;
            staBoostHpGate.IsChecked = true;
            greenQuotaEnabled.IsChecked = false;
            greenQuotaMinCount.Value = 6;
            preservedComboPromotions = new JArray();
            preservedAccelGroupCaps = new JArray();
        }

        var stage = obj?["stage_policy"] as JObject;
        var classic = stage?["classic_arc"] as JObject;
        classicModePicker.SelectedIndex = FindChoiceIndex(ClassicStageModes, (string?)classic?["mode"], 0);
        classicReservePt.Value = Math.Clamp((int?)classic?["reserve_pt"] ?? 360, 0, 2000);
        classicMinDiscount.Value = Math.Clamp((int?)classic?["min_discount_percent"] ?? 20, 0, 60);
        classicAllowRecovery.IsChecked = (bool?)classic?["allow_recovery"] != false;
        preservedClassicPreferWhite = (bool?)classic?["prefer_white"] != false;
        preservedClassicMaxSkills = Math.Clamp((int?)classic?["max_skills"] ?? 2, 1, 5);
        preservedClassicSpeedThreshold = Math.Clamp((int?)classic?["speed_threshold"] ?? 900, 0, 3000);
        preservedClassicStaminaThreshold = Math.Clamp((int?)classic?["stamina_threshold"] ?? 600, 0, 3000);
        preservedClassicAllowedTiers = classic?["allowed_tiers"] is JArray cat ? (JArray)cat.DeepClone() : new JArray("T0", "T1");

        var senior = stage?["senior_arc"] as JObject;
        seniorModePicker.SelectedIndex = FindChoiceIndex(SeniorStageModes, (string?)senior?["mode"], 0);
        seniorMaxSkills.Value = Math.Clamp((int?)senior?["max_skills"] ?? 5, 1, 10);
        seniorReservePt.Value = Math.Clamp((int?)senior?["reserve_pt"] ?? 400, 0, 2000);
        seniorAllowRecovery.IsChecked = (bool?)senior?["allow_recovery"] != false;
        preservedSeniorAllowedTiers = senior?["allowed_tiers"] is JArray sat ? (JArray)sat.DeepClone() : new JArray("T0");

        var evo = obj?["evolution_policy"] as JObject;
        evoEnabled.IsChecked = (bool?)evo?["enabled"] != false;
        evoBranchPicker.SelectedIndex = FindChoiceIndex(EvolutionBranches, (string?)evo?["default_branch"], 0);
        preservedEvoOverrides = evo?["overrides"] is JObject ov ? (JObject)ov.DeepClone() : new JObject();
    }

    public JObject ReadDraft() => ReadCore(validate: false);

    public JObject Read() => ReadCore(validate: true);

    private static int ReadInt(NumericUpDown control, int min, int max, int fallback, bool validate, string err)
    {
        var d = control.Value ?? fallback;
        if (validate && (d != decimal.Truncate(d) || d < min || d > max))
            throw new FormatException(err);
        return Math.Clamp((int)decimal.Truncate(d), min, max);
    }

    private JObject ReadCore(bool validate)
    {
        var reserveInt = ReadInt(midReserve, 0, 2000, 360, validate, "经典年保留 Pt 请填 0～2000 的整数。");
        var discountInt = ReadInt(minMidDiscount, 0, 60, 20, validate, "经典年最低折扣请填 0～60 的整数。");

        var result = new JObject
        {
            ["enabled"] = enabled.IsChecked == true,
            ["auto_cm"] = IsTieredLadderMode ? false : autoCm.IsChecked == true,
        };

        var preset = SelectedPreset;
        result["preset_id"] = preset.Id != null ? preset.Id : JValue.CreateNull();

        var existingTarget = loaded["final_target"] as JObject;
        var target = preset.Id != null && string.Equals(preset.Id, loadedPresetId, StringComparison.Ordinal) && existingTarget != null
            ? (JObject)existingTarget.DeepClone()
            : new JObject();
        if (!string.Equals(preset.Id, loadedPresetId, StringComparison.Ordinal))
        {
            foreach (var key in PresetDerivedTargetKeys)
                target.Remove(key);
        }

        var styleKey = SelectedStyle.Key;
        if (styleKey != null) target["style"] = styleKey;
        else target.Remove("style");

        if (preset.Id != null)
        {
            // Keep preset authoritative and avoid any distance_m vs distance_type conflict.
            target.Remove("distance_type");
        }
        else
        {
            var distKey = SelectedDistance.Key;
            if (distKey != null) target["distance_type"] = distKey;
            else target.Remove("distance_type");
            foreach (var key in PresetDerivedTargetKeys)
                if (existingTarget == null || loadedPresetId != null) target.Remove(key);
        }

        result["final_target"] = target;
        result["classic_allowlist"] = loaded["classic_allowlist"] is JArray list
            ? (JArray)list.DeepClone()
            : new JArray();
        result["mid_reserve_pt"] = reserveInt;
        result["min_mid_discount_percent"] = discountInt;

        if (!IsTieredLadderMode)
            return result;

        result["mode"] = "tiered_ladder";

        var gwSourcesInt = ReadInt(gwMinSources, 1, 6, 3, validate, "磐石/地固最少触发源请填 1～6 的整数。");
        var staMinInt = ReadInt(staBoostMin, 0, 3000, 1150, validate, "最低耐力门槛请填 0～3000 的整数。");
        var greenMinInt = ReadInt(greenQuotaMinCount, 1, 12, 6, validate, "绿技凑满数量请填 1～12 的整数。");
        var classicResInt = ReadInt(classicReservePt, 0, 2000, 360, validate, "经典年阶段保留 Pt 请填 0～2000 的整数。");
        var classicDiscInt = ReadInt(classicMinDiscount, 0, 60, 20, validate, "经典年阶段最低折扣请填 0～60 的整数。");
        var seniorMaxInt = ReadInt(seniorMaxSkills, 1, 10, 5, validate, "资深年最多买技能数请填 1～10 的整数。");
        var seniorResInt = ReadInt(seniorReservePt, 0, 2000, 400, validate, "资深年阶段保留 Pt 请填 0～2000 的整数。");

        var tiersArr = new JArray();
        for (int i = 0; i < 4; i++)
        {
            var meta = preservedTierMeta[i];
            var tierObj = new JObject
            {
                ["id"] = (string?)meta["id"] ?? DefaultTierDefs[i].Id,
                ["label"] = (string?)meta["label"] ?? DefaultTierDefs[i].Label,
                ["skills"] = new JArray(tierLists[i]),
            };
            if (meta["within_tier_order"] != null && meta["within_tier_order"]!.Type != JTokenType.Null)
                tierObj["within_tier_order"] = meta["within_tier_order"]!.DeepClone();
            if (meta["max_count"] != null && meta["max_count"]!.Type != JTokenType.Null)
                tierObj["max_count"] = meta["max_count"]!.DeepClone();
            tiersArr.Add(tierObj);
        }
        foreach (var extra in preservedExtraTiers)
            tiersArr.Add((JObject)extra.DeepClone());

        var withinKey = WithinTierOrders[Math.Clamp(withinOrderPicker.SelectedIndex, 0, WithinTierOrders.Length - 1)].Key!;
        var unlistedKey = UnlistedPolicies[Math.Clamp(unlistedPolicyPicker.SelectedIndex, 0, UnlistedPolicies.Length - 1)].Key!;

        result["ladder"] = new JObject
        {
            ["tiers"] = tiersArr,
            ["avoid"] = new JArray(avoidList),
            ["unlisted_policy"] = unlistedKey,
            ["within_tier_order"] = withinKey,
            ["allow_lower_fallback"] = allowLowerFallback.IsChecked == true,
            ["groundwork_gate"] = new JObject
            {
                ["enabled"] = gwEnabled.IsChecked == true,
                ["min_sources"] = gwSourcesInt,
                ["reliable_active_pair"] = gwReliablePair.IsChecked == true,
            },
            ["stamina_gap_boost"] = new JObject
            {
                ["enabled"] = staBoostEnabled.IsChecked == true,
                ["min_stamina"] = staMinInt,
                ["use_hp_gate"] = staBoostHpGate.IsChecked == true,
                ["promote_skills"] = new JArray(staminaPromoList),
            },
            ["green_skill_quota"] = new JObject
            {
                ["enabled"] = greenQuotaEnabled.IsChecked == true,
                ["min_count"] = greenMinInt,
                ["skills"] = new JArray(greenPrimaryList),
                ["last_resort_skills"] = new JArray(greenLastResortList),
            },
            ["accel_group_caps"] = (JArray)preservedAccelGroupCaps.DeepClone(),
            ["combo_promotions"] = (JArray)preservedComboPromotions.DeepClone(),
        };

        var classicModeKey = ClassicStageModes[Math.Clamp(classicModePicker.SelectedIndex, 0, ClassicStageModes.Length - 1)].Key!;
        var seniorModeKey = SeniorStageModes[Math.Clamp(seniorModePicker.SelectedIndex, 0, SeniorStageModes.Length - 1)].Key!;
        result["stage_policy"] = new JObject
        {
            ["classic_arc"] = new JObject
            {
                ["mode"] = classicModeKey,
                ["speed_threshold"] = preservedClassicSpeedThreshold,
                ["stamina_threshold"] = preservedClassicStaminaThreshold,
                ["max_skills"] = preservedClassicMaxSkills,
                ["allowed_tiers"] = (JArray)preservedClassicAllowedTiers.DeepClone(),
                ["min_discount_percent"] = classicDiscInt,
                ["reserve_pt"] = classicResInt,
                ["prefer_white"] = preservedClassicPreferWhite,
                ["allow_recovery"] = classicAllowRecovery.IsChecked == true,
            },
            ["senior_arc"] = new JObject
            {
                ["mode"] = seniorModeKey,
                ["allowed_tiers"] = (JArray)preservedSeniorAllowedTiers.DeepClone(),
                ["max_skills"] = seniorMaxInt,
                ["reserve_pt"] = seniorResInt,
                ["allow_recovery"] = seniorAllowRecovery.IsChecked == true,
            },
            ["terminal"] = new JObject { ["mode"] = "full_ladder" },
        };

        var evoBranchKey = EvolutionBranches[Math.Clamp(evoBranchPicker.SelectedIndex, 0, EvolutionBranches.Length - 1)].Key!;
        result["evolution_policy"] = new JObject
        {
            ["enabled"] = evoEnabled.IsChecked == true,
            ["default_branch"] = evoBranchKey,
            ["overrides"] = (JObject)preservedEvoOverrides.DeepClone(),
        };

        return result;
    }
}
