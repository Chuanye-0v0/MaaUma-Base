using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Input;
using Avalonia.Input.Platform;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed record UmaCustomRaceEntry(
    string RaceId,
    string NameZhCn,
    string ShortNameZhCn,
    string PrimaryAliasZh,
    string DisplayNameZh,
    string[] Aliases,
    string Grade,
    string Surface,
    string SurfaceZh,
    int DistanceM,
    string DistanceCategory,
    string DistanceCategoryZh,
    string VenueZh,
    int FanRequirement,
    int YearIndex,
    string YearZh,
    int Month,
    string Half,
    string DateLabelZh,
    int LarcOrdinal,
    bool LarcSelectable,
    string? LarcConflictReason,
    string SearchHaystack,
    string Direction = "",
    string DirectionZh = "");

public sealed record UmaRaceSchedulePreset(
    string PresetId,
    string TitleZh,
    string TagZh,
    string DescriptionZh,
    string RecommendedAptitudeZh,
    string[] RaceIds);

public sealed record UmaSpecialRaceTarget(
    string Id,
    string LabelZh,
    string ShortZh,
    string HintZh);

public sealed class UmaCustomRaceCalendarEditor : StackPanel
{
    public const int MaxCustomRaces = 24;

    public static readonly (string Id, string Label, string ShortLabel)[] RunningStyles =
    {
        ("keep", "保持游戏当前（默认）", "保持当前"),
        ("leader", "领跑（逃）", "领跑（逃）"),
        ("front", "跟前（先）", "跟前（先）"),
        ("middle", "居中（差）", "居中（差）"),
        ("back", "后追（追）", "后追（追）"),
    };

    public static readonly (string Id, string Label, string CompactLabel)[] PerRaceRunningStyles =
    {
        ("inherit", "跟随默认跑法", "默认"),
        ("leader", "领跑（逃）", "逃"),
        ("front", "跟前（先）", "先"),
        ("middle", "居中（差）", "差"),
        ("back", "后追（追）", "追"),
        ("keep", "保持游戏当前", "不改"),
    };

    private sealed record LarcFixedGoal(
        string Title,
        string Subtitle,
        string SpecialKey,
        string AccentStartHex,
        string AccentEndHex);

    private static readonly Dictionary<int, LarcFixedGoal> LarcFixedGoals = new()
    {
        [33] = new("全国德比", "东京 草地 2400米（中距离）· 左", "scenario.derby", "#991B1B", "#DC2626"),
        [40] = new("尼尔锦标", "隆尚 草地 2400米（中距离）· 右", "scenario.overseas", "#6B21A8", "#9333EA"),
        [42] = new("凯旋门奖", "隆尚 草地 2400米（中距离）· 右", "scenario.overseas", "#92400E", "#D97706"),
        [59] = new("宝塚纪念", "阪神 草地 2200米（中距离）· 右", "scenario.takarazuka", "#9F1239", "#E11D48"),
        [64] = new("福伊锦标", "隆尚 草地 2400米（中距离）· 右", "scenario.overseas", "#6B21A8", "#9333EA"),
        [66] = new("凯旋门奖", "隆尚 草地 2400米（最终决战）· 右", "scenario.overseas", "#92400E", "#D97706"),
    };

    private static readonly HashSet<int> OverseasLockedOrdinals = new()
    {
        36, 37, 38, 39, 41,
        60, 61, 62, 63, 65,
    };

    private static readonly string[] JuniorPreDebutLabels =
    {
        "4月 上半月", "4月 上半月", "4月 下半月", "4月 下半月",
        "5月 上半月", "5月 上半月", "5月 下半月", "5月 下半月",
        "6月 上半月", "6月 上半月", "6月 下半月", "6月 下半月",
    };

    private static readonly object CatalogLock = new();
    private static UmaCustomRaceEntry[]? cachedEntries;
    private static Dictionary<string, UmaCustomRaceEntry>? cachedById;
    private static UmaRaceSchedulePreset[]? cachedPresets;
    private static UmaSpecialRaceTarget[]? cachedSpecialTargets;

    // Palette
    private static readonly IBrush HeaderGrad = HorizontalGrad("#4D7C0F", "#65A30D");
    private static readonly IBrush UmaGreenBrush = new SolidColorBrush(Color.Parse("#84CC16"));
    private static readonly IBrush UmaGreenDeepBrush = new SolidColorBrush(Color.Parse("#4D7C0F"));
    private static readonly IBrush UmaGreenBrightBrush = new SolidColorBrush(Color.Parse("#A3E635"));
    private static readonly IBrush ActiveRingBrush = new SolidColorBrush(Color.Parse("#84CC16"));
    private static readonly IBrush CardSurfaceBg = new SolidColorBrush(Color.FromArgb(24, 148, 163, 184));
    private static readonly IBrush CardSurfaceBorder = new SolidColorBrush(Color.FromArgb(62, 148, 163, 184));
    private static readonly IBrush CalendarCardBorder = new SolidColorBrush(Color.FromArgb(145, 101, 163, 13));
    private static readonly IBrush SlotEmptyBg = new SolidColorBrush(Color.FromArgb(34, 255, 255, 255));
    private static readonly IBrush SlotMutedBg = new SolidColorBrush(Color.FromArgb(18, 100, 116, 139));
    private static readonly IBrush SlotOverseasBg = new SolidColorBrush(Color.FromArgb(36, 79, 70, 229));
    private static readonly IBrush SlotBorder = new SolidColorBrush(Color.FromArgb(72, 148, 163, 184));
    private static readonly IBrush SelectedCardBg = new SolidColorBrush(Color.FromArgb(40, 37, 99, 235));
    private static readonly IBrush SelectedCardBorder = new SolidColorBrush(Color.FromArgb(165, 59, 130, 246));
    private static readonly IBrush PresetPanelBg = new SolidColorBrush(Color.FromArgb(24, 56, 189, 248));
    private static readonly IBrush PresetPanelBorder = new SolidColorBrush(Color.FromArgb(85, 56, 189, 248));
    private static readonly IBrush PillTrackBg = new SolidColorBrush(Color.FromArgb(36, 15, 23, 42));
    private static readonly IBrush ConflictWarningBrush = new SolidColorBrush(Color.Parse("#F59E0B"));
    private static readonly IBrush StreakOkBrush = new SolidColorBrush(Color.Parse("#4ADE80"));
    private static readonly IBrush StreakWarnBrush = new SolidColorBrush(Color.Parse("#FBBF24"));
    private static readonly IBrush TagBorder = new SolidColorBrush(Color.FromArgb(95, 217, 119, 6));
    private static readonly IBrush TagBg = new SolidColorBrush(Color.FromArgb(26, 245, 158, 11));
    private static readonly IBrush StarTint = new SolidColorBrush(Color.Parse("#FBBF24"));
    private static readonly IBrush G1HintBg = new SolidColorBrush(Color.FromArgb(170, 30, 64, 175));
    private static readonly IBrush G1HintFg = new SolidColorBrush(Color.Parse("#DBEAFE"));
    private static readonly IBrush GoalTagBg = new SolidColorBrush(Color.Parse("#DC2626"));
    private static readonly IBrush StyleTagBg = new SolidColorBrush(Color.Parse("#D97706"));
    private static readonly IBrush StreakBadgeBg = new SolidColorBrush(Color.Parse("#EF4444"));
    private static readonly IBrush SlotRemoveBg = new SolidColorBrush(Color.FromArgb(175, 15, 23, 42));

    // Grade Banner & Pill Colors (In-Game Screenshot 4 fidelity)
    private static readonly IBrush G1PillBrush = new SolidColorBrush(Color.Parse("#2563EB"));
    private static readonly IBrush G2PillBrush = new SolidColorBrush(Color.Parse("#EC4899"));
    private static readonly IBrush G3PillBrush = new SolidColorBrush(Color.Parse("#16A34A"));
    private static readonly IBrush G1BannerBrush = HorizontalGrad("#1E40AF", "#3B82F6");
    private static readonly IBrush G2BannerBrush = HorizontalGrad("#4F46E5", "#818CF8");
    private static readonly IBrush G3BannerBrush = HorizontalGrad("#B45309", "#D97706");

    private readonly JObject options;
    private readonly string kind;
    private readonly string prefix;
    private readonly Action onChanged;
    private readonly UmaCustomRaceEntry[] catalog;
    private readonly Dictionary<string, UmaCustomRaceEntry> byId;
    private readonly Dictionary<int, UmaCustomRaceEntry[]> byOrdinal;
    private readonly int[] navigableOrdinals;
    private readonly UmaRaceSchedulePreset[] presets;
    private readonly UmaSpecialRaceTarget[] specialTargets;
    private readonly List<string> selectedRaceIds = new();
    private readonly Dictionary<string, string> raceRunningStyles = new(StringComparer.Ordinal);
    private readonly Dictionary<string, ComboBox> specialCombos = new(StringComparer.Ordinal);

    private int activeYear = 2;      // 1=初级, 2=经典级, 3=高级
    private int activeOrdinal = 30;  // 经典级 4月上半月
    private int rightMode;           // 0=参赛设定, 1=我的参赛计划, 2=全局搜赛

    private readonly ComboBox styleCombo;
    private readonly ComboBox? presetCombo;
    private readonly TextBlock? presetDescriptionText;
    private readonly TextBlock? selectedCountBadge;
    private readonly TextBlock? streakBadge;
    private readonly Button? clearAllButton;
    private readonly Button? myScheduleBtn;
    private readonly Button[] yearTabButtons = new Button[3];
    private readonly UniformGrid? calGrid;

    private readonly Button? rightTabTurnBtn;
    private readonly Button? rightTabSelectedBtn;
    private readonly Button? rightTabSearchBtn;
    private readonly ContentControl? rightBodyHost;

    // View 1: 参赛设定
    private readonly StackPanel? turnViewPanel;
    private readonly Button? prevYearTurnBtn;
    private readonly Button? prevTurnBtn;
    private readonly Button? nextTurnBtn;
    private readonly Button? nextYearTurnBtn;
    private readonly TextBlock? activeTurnTitle;
    private readonly TextBlock? activeTurnSubText;
    private readonly CheckBox? showTurnConflictsCheck;
    private readonly StackPanel? activeTurnListPanel;

    // View 2: 我的参赛计划
    private readonly StackPanel? myPlanViewPanel;
    private readonly StackPanel? myPlanListPanel;

    // View 3: 全局搜赛
    private readonly StackPanel? searchViewPanel;
    private readonly TextBox? searchBox;
    private readonly ComboBox? yearFilter;
    private readonly ComboBox? gradeFilter;
    private readonly ComboBox? surfaceFilter;
    private readonly ComboBox? distanceFilter;
    private readonly CheckBox? showConflictsCheck;
    private readonly TextBlock? filterSummaryText;
    private readonly StackPanel? raceListPanel;

    private readonly TextBlock statusNote;
    private bool suppressEvents;

    public UmaCustomRaceCalendarEditor(JObject options, string kind, string prefix, Action onChanged)
    {
        this.options = options;
        this.kind = kind;
        this.prefix = prefix;
        this.onChanged = onChanged;
        Name = prefix + "CustomRaceCalendarEditor";
        Spacing = 6;

        catalog = LoadCatalog(AppContext.BaseDirectory);
        byId = catalog.ToDictionary(r => r.RaceId, StringComparer.Ordinal);
        byOrdinal = catalog
            .GroupBy(r => r.LarcOrdinal)
            .ToDictionary(
                g => g.Key,
                g => g.OrderBy(r => GradeRank(r.Grade))
                      .ThenByDescending(r => r.FanRequirement)
                      .ThenBy(r => r.NameZhCn, StringComparer.Ordinal)
                      .ToArray());
        navigableOrdinals = Enumerable.Range(12, 55)
            .Where(o => LarcFixedGoals.ContainsKey(o) || (byOrdinal.TryGetValue(o, out var list) && list.Any(r => r.LarcSelectable)))
            .ToArray();
        presets = LoadPresets(AppContext.BaseDirectory);
        specialTargets = LoadSpecialTargets(AppContext.BaseDirectory);

        LoadStateFromOptions();
        PickInitialActiveLocation();

        // 1. Compact Top Running-Style Bar (Default Style + Scenario Key Races on 1 compact strip)
        var styleCard = new Border
        {
            Background = CardSurfaceBg,
            BorderBrush = CardSurfaceBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(10, 5),
        };
        var styleWrap = new WrapPanel { VerticalAlignment = VerticalAlignment.Center };

        var defaultBlock = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            Margin = new Thickness(0, 1, 12, 1),
            VerticalAlignment = VerticalAlignment.Center,
        };
        defaultBlock.Children.Add(new TextBlock
        {
            Text = "默认跑法",
            FontSize = 12.5,
            FontWeight = FontWeight.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
        });

        var currentStyle = NormalizeRunningStyle((string?)options["running_style"]) ?? "keep";
        var initialStyleIdx = Math.Max(0, Array.FindIndex(RunningStyles, s => s.Id == currentStyle));
        styleCombo = new ComboBox
        {
            Name = prefix + "RunningStyleCombo",
            ItemsSource = RunningStyles.Select(s => s.Label).ToArray(),
            SelectedIndex = initialStyleIdx,
            MinWidth = 148,
            Height = 28,
            MinHeight = 0,
            Padding = new Thickness(8, 2),
            FontSize = 11.5,
            VerticalAlignment = VerticalAlignment.Center,
        };
        styleCombo.SelectionChanged += (_, _) =>
        {
            if (suppressEvents || styleCombo.SelectedIndex < 0 || styleCombo.SelectedIndex >= RunningStyles.Length)
                return;
            var picked = RunningStyles[styleCombo.SelectedIndex].Id;
            if (picked == "keep" && options["running_style"] == null)
                return;
            options["running_style"] = picked;
            SetStatus("默认比赛跑法已设为「" + RunningStyles[styleCombo.SelectedIndex].Label + "」，请保存配置。");
            this.onChanged();
        };
        defaultBlock.Children.Add(styleCombo);
        styleWrap.Children.Add(defaultBlock);

        var visibleSpecials = kind == "cut"
            ? specialTargets.Where(t => t.Id == "scenario.debut").ToArray()
            : specialTargets;
        if (visibleSpecials.Length > 0)
        {
            var specialsWrap = new WrapPanel { VerticalAlignment = VerticalAlignment.Center };
            specialsWrap.Children.Add(new TextBlock
            {
                Text = kind == "cut" ? "特定比赛：" : "关键战：",
                FontSize = 12,
                FontWeight = FontWeight.SemiBold,
                Opacity = 0.85,
                Margin = new Thickness(0, 1, 6, 1),
                VerticalAlignment = VerticalAlignment.Center,
            });
            foreach (var sp in visibleSpecials)
            {
                var curOverride = raceRunningStyles.TryGetValue(sp.Id, out var ov) ? ov : "inherit";
                var idx = Math.Max(0, Array.FindIndex(PerRaceRunningStyles, p => p.Id == curOverride));
                var spCombo = new ComboBox
                {
                    Name = prefix + "SpecialStyle_" + SanitizeId(sp.Id),
                    Tag = sp.Id,
                    ItemsSource = PerRaceRunningStyles.Select(ShortStyleChoiceLabel).ToArray(),
                    SelectedIndex = idx,
                    MinWidth = 86,
                    Height = 28,
                    MinHeight = 0,
                    Padding = new Thickness(7, 2),
                    FontSize = 11.5,
                    VerticalAlignment = VerticalAlignment.Center,
                };
                var capturedTarget = sp;
                spCombo.SelectionChanged += (_, _) =>
                {
                    if (suppressEvents || spCombo.SelectedIndex < 0 || spCombo.SelectedIndex >= PerRaceRunningStyles.Length)
                        return;
                    var pickedId = PerRaceRunningStyles[spCombo.SelectedIndex].Id;
                    SetPerRaceRunningStyle(capturedTarget.Id, pickedId, capturedTarget.ShortZh);
                };
                specialCombos[sp.Id] = spCombo;
                var spItem = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Spacing = 4,
                    Margin = new Thickness(0, 1, 8, 1),
                    VerticalAlignment = VerticalAlignment.Center,
                    Children =
                    {
                        new TextBlock { Text = sp.ShortZh, FontSize = 11.5, Opacity = 0.82, VerticalAlignment = VerticalAlignment.Center },
                        spCombo,
                    },
                };
                ToolTip.SetTip(spItem, sp.LabelZh + "：" + sp.HintZh);
                specialsWrap.Children.Add(spItem);
            }
            styleWrap.Children.Add(specialsWrap);
        }

        styleCard.Child = styleWrap;
        Children.Add(styleCard);

        statusNote = Note("");
        statusNote.Name = prefix + "RaceEditorStatus";
        statusNote.FontSize = 11.5;
        statusNote.Opacity = 0.86;
        statusNote.IsVisible = false;

        if (kind != "career")
        {
            Children.Add(Note("提示：自定义重赏加赛与内置攻略赛程请在「自动育成」步骤中配置。"));
            Children.Add(statusNote);
            return;
        }

        // 2. Compact Community Schedule Presets Bar
        if (presets.Length > 0)
        {
            var presetCard = new Border
            {
                Background = PresetPanelBg,
                BorderBrush = PresetPanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 5),
            };
            var presetStack = new StackPanel { Spacing = 3 };
            var presetRow = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto,Auto"), ColumnSpacing = 8 };
            presetRow.Children.Add(new TextBlock
            {
                Text = "攻略赛程：",
                FontSize = 12,
                FontWeight = FontWeight.SemiBold,
                VerticalAlignment = VerticalAlignment.Center,
            });
            presetCombo = new ComboBox
            {
                Name = prefix + "PresetScheduleCombo",
                ItemsSource = presets.Select(p => $"[{p.TagZh}] {p.TitleZh}").ToArray(),
                SelectedIndex = 1 < presets.Length ? 1 : 0,
                Height = 28,
                MinHeight = 0,
                Padding = new Thickness(8, 2),
                FontSize = 12,
                HorizontalAlignment = HorizontalAlignment.Stretch,
            };
            Grid.SetColumn(presetCombo, 1);
            presetRow.Children.Add(presetCombo);

            var applyPresetBtn = new Button
            {
                Name = prefix + "ApplyPresetScheduleBtn",
                Content = "一键套用此赛程",
                Height = 28,
                MinHeight = 0,
                Padding = new Thickness(12, 2),
                FontSize = 12,
                FontWeight = FontWeight.SemiBold,
            };
            Grid.SetColumn(applyPresetBtn, 2);
            presetRow.Children.Add(applyPresetBtn);

            var pastePresetBtn = new Button
            {
                Name = prefix + "PasteSchedulePresetBtn",
                Content = "📋 粘贴种马赛程预设",
                Height = 28,
                MinHeight = 0,
                Padding = new Thickness(10, 2),
                FontSize = 12,
            };
            ToolTip.SetTip(pastePresetBtn, "从种马规划器点击“复制 Pacer 参赛预设”后，点击此处直接导入参赛场次与默认跑法");
            Grid.SetColumn(pastePresetBtn, 3);
            presetRow.Children.Add(pastePresetBtn);
            presetStack.Children.Add(presetRow);

            presetDescriptionText = Note("");
            presetDescriptionText.Name = prefix + "PresetScheduleDesc";
            presetDescriptionText.FontSize = 11;
            presetDescriptionText.Opacity = 0.85;
            presetStack.Children.Add(presetDescriptionText);

            void RefreshPresetDesc()
            {
                if (presetCombo.SelectedIndex < 0 || presetCombo.SelectedIndex >= presets.Length) return;
                var p = presets[presetCombo.SelectedIndex];
                presetDescriptionText.Text = $"适用：{p.RecommendedAptitudeZh}  ｜  {p.DescriptionZh}";
            }
            RefreshPresetDesc();
            presetCombo.SelectionChanged += (_, _) => RefreshPresetDesc();
            applyPresetBtn.Click += (_, _) =>
            {
                if (presetCombo.SelectedIndex < 0 || presetCombo.SelectedIndex >= presets.Length) return;
                ApplySchedulePreset(presets[presetCombo.SelectedIndex]);
            };
            pastePresetBtn.Click += async (_, _) => await PasteSchedulePresetAsync();

            presetCard.Child = presetStack;
            Children.Add(presetCard);
        }

        // 3. Responsive 2-Pane Split View: Left 参赛计划 (4x6 Grid) | Right 参赛设定 + 我的参赛计划 + 全局搜赛
        var splitGrid = new Grid
        {
            Name = prefix + "CalendarSplitGrid",
            ColumnDefinitions = new ColumnDefinitions("1.14*,1*"),
            ColumnSpacing = 12,
            RowSpacing = 12,
        };

        // --- LEFT PANE: 参赛计划 Card ---
        var leftCard = new Border
        {
            BorderBrush = CalendarCardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Background = CardSurfaceBg,
            VerticalAlignment = VerticalAlignment.Top,
        };
        var leftRoot = new Grid { RowDefinitions = new RowDefinitions("Auto,Auto,Auto,Auto") };

        // Left Header Bar (Green Banner)
        var leftHeader = new Border
        {
            Background = HeaderGrad,
            CornerRadius = new CornerRadius(9, 9, 0, 0),
            Padding = new Thickness(11, 5),
        };
        var leftHeaderGrid = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto") };
        leftHeaderGrid.Children.Add(new TextBlock
        {
            Text = "参赛计划",
            FontSize = 14,
            FontWeight = FontWeight.Bold,
            Foreground = Brushes.White,
            VerticalAlignment = VerticalAlignment.Center,
        });
        selectedCountBadge = new TextBlock
        {
            Name = prefix + "SelectedCountBadge",
            FontSize = 12,
            FontWeight = FontWeight.SemiBold,
            Foreground = Brushes.White,
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(selectedCountBadge, 1);
        leftHeaderGrid.Children.Add(selectedCountBadge);
        leftHeader.Child = leftHeaderGrid;
        leftRoot.Children.Add(leftHeader);

        // 3-Year Segmented Tab Bar
        var tabsTrack = new Border
        {
            Name = prefix + "CalYearTabs",
            Background = PillTrackBg,
            BorderBrush = CardSurfaceBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(15),
            Padding = new Thickness(2),
            Margin = new Thickness(8, 5, 8, 3),
        };
        var tabsGrid = new UniformGrid { Columns = 3, Rows = 1 };
        for (var y = 1; y <= 3; y++)
        {
            var capturedYear = y;
            var btn = new Button
            {
                Name = prefix + "CalYearTab" + y,
                Content = YearTabName(y),
                Height = 26,
                MinHeight = 0,
                Padding = new Thickness(6, 0),
                CornerRadius = new CornerRadius(13),
                BorderThickness = new Thickness(0),
                FontSize = 12,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Center,
                VerticalContentAlignment = VerticalAlignment.Center,
            };
            btn.Click += (_, _) =>
            {
                activeYear = capturedYear;
                EnsureActiveOrdinalMatchesYear();
                RefreshAll();
            };
            yearTabButtons[y - 1] = btn;
            tabsGrid.Children.Add(btn);
        }
        tabsTrack.Child = tabsGrid;
        Grid.SetRow(tabsTrack, 1);
        leftRoot.Children.Add(tabsTrack);

        // 4x6 Half-Month Calendar Grid
        calGrid = new UniformGrid
        {
            Name = prefix + "CalGrid",
            Columns = 4,
            Rows = 6,
            Margin = new Thickness(7, 1, 7, 2),
        };
        Grid.SetRow(calGrid, 2);
        leftRoot.Children.Add(calGrid);

        // Bottom Footer (重置 | Streak | 我的参赛计划)
        var leftFooter = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"),
            ColumnSpacing = 8,
            Margin = new Thickness(10, 3, 10, 6),
        };
        clearAllButton = new Button
        {
            Name = prefix + "ClearAllRaces",
            Content = "重置",
            Height = 28,
            MinHeight = 0,
            MinWidth = 62,
            Padding = new Thickness(10, 2),
            CornerRadius = new CornerRadius(14),
            FontSize = 11.5,
            HorizontalContentAlignment = HorizontalAlignment.Center,
        };
        clearAllButton.Click += (_, _) =>
        {
            if (selectedRaceIds.Count == 0) return;
            foreach (var oldId in selectedRaceIds.ToArray())
                raceRunningStyles.Remove(oldId);
            selectedRaceIds.Clear();
            WriteStateToOptions();
            SetStatus("已清空全部自定义加赛，请保存配置。");
            RefreshAll();
            this.onChanged();
        };
        leftFooter.Children.Add(clearAllButton);

        streakBadge = new TextBlock
        {
            FontSize = 11.5,
            FontWeight = FontWeight.Medium,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(streakBadge, 1);
        leftFooter.Children.Add(streakBadge);

        myScheduleBtn = new Button
        {
            Name = prefix + "MyScheduleBtn",
            Content = "我的参赛计划 (0)",
            Height = 28,
            MinHeight = 0,
            Padding = new Thickness(11, 2),
            CornerRadius = new CornerRadius(14),
            Background = UmaGreenDeepBrush,
            Foreground = Brushes.White,
            FontSize = 11.5,
            FontWeight = FontWeight.SemiBold,
        };
        myScheduleBtn.Click += (_, _) =>
        {
            rightMode = 1;
            RefreshRightPane();
        };
        Grid.SetColumn(myScheduleBtn, 2);
        leftFooter.Children.Add(myScheduleBtn);

        Grid.SetRow(leftFooter, 3);
        leftRoot.Children.Add(leftFooter);
        leftCard.Child = leftRoot;
        splitGrid.Children.Add(leftCard);

        // --- RIGHT PANE: 参赛设定 Card ---
        var rightCard = new Border
        {
            BorderBrush = CardSurfaceBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Background = CardSurfaceBg,
            VerticalAlignment = VerticalAlignment.Top,
        };
        var rightRoot = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };

        // Right Header Tabs
        var rightHeader = new Border
        {
            Background = PillTrackBg,
            BorderBrush = CardSurfaceBorder,
            BorderThickness = new Thickness(0, 0, 0, 1),
            CornerRadius = new CornerRadius(9, 9, 0, 0),
            Padding = new Thickness(8, 6),
        };
        var rightTabRow = new UniformGrid { Columns = 3, Rows = 1 };
        rightTabTurnBtn = RightModeTabButton(prefix + "RightTabTurn", "参赛设定", 0);
        rightTabSelectedBtn = RightModeTabButton(prefix + "RightTabSelected", "我的参赛计划 (0)", 1);
        rightTabSearchBtn = RightModeTabButton(prefix + "RightTabSearch", "全局搜赛", 2);
        rightTabRow.Children.Add(rightTabTurnBtn);
        rightTabRow.Children.Add(rightTabSelectedBtn);
        rightTabRow.Children.Add(rightTabSearchBtn);
        rightHeader.Child = rightTabRow;
        rightRoot.Children.Add(rightHeader);

        rightBodyHost = new ContentControl
        {
            Padding = new Thickness(10, 8, 10, 10),
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
        };
        Grid.SetRow(rightBodyHost, 1);
        rightRoot.Children.Add(rightBodyHost);
        rightCard.Child = rightRoot;

        // Build View 1: 参赛设定
        turnViewPanel = new StackPanel { Spacing = 8 };
        var turnNavGrid = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,Auto,*,Auto,Auto"),
            ColumnSpacing = 6,
            VerticalAlignment = VerticalAlignment.Center,
        };
        prevYearTurnBtn = NavButton(prefix + "PrevYearTurnBtn", "«", "跳至上一年份");
        prevTurnBtn = NavButton(prefix + "PrevTurnBtn", "‹", "上一回合");
        nextTurnBtn = NavButton(prefix + "NextTurnBtn", "›", "下一回合");
        nextYearTurnBtn = NavButton(prefix + "NextYearTurnBtn", "»", "跳至下一年份");

        prevYearTurnBtn.Click += (_, _) => StepYear(-1);
        prevTurnBtn.Click += (_, _) => StepTurn(-1);
        nextTurnBtn.Click += (_, _) => StepTurn(1);
        nextYearTurnBtn.Click += (_, _) => StepYear(1);

        var titleStack = new StackPanel
        {
            Spacing = 1,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        };
        activeTurnTitle = new TextBlock
        {
            Name = prefix + "ActiveTurnTitle",
            FontSize = 15.5,
            FontWeight = FontWeight.Bold,
            Foreground = UmaGreenBrush,
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        activeTurnSubText = new TextBlock
        {
            FontSize = 11,
            Opacity = 0.78,
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        titleStack.Children.Add(activeTurnTitle);
        titleStack.Children.Add(activeTurnSubText);

        turnNavGrid.Children.Add(prevYearTurnBtn);
        Grid.SetColumn(prevTurnBtn, 1);
        turnNavGrid.Children.Add(prevTurnBtn);
        Grid.SetColumn(titleStack, 2);
        turnNavGrid.Children.Add(titleStack);
        Grid.SetColumn(nextTurnBtn, 3);
        turnNavGrid.Children.Add(nextTurnBtn);
        Grid.SetColumn(nextYearTurnBtn, 4);
        turnNavGrid.Children.Add(nextYearTurnBtn);
        turnViewPanel.Children.Add(turnNavGrid);

        showTurnConflictsCheck = new CheckBox
        {
            Name = prefix + "ShowTurnConflictsCheck",
            Content = "显示本回合不可选冲突赛事",
            IsChecked = false,
            FontSize = 11.5,
            Opacity = 0.85,
            IsVisible = false,
        };
        showTurnConflictsCheck.IsCheckedChanged += (_, _) => { if (!suppressEvents && rightMode == 0) RefreshTurnView(); };
        turnViewPanel.Children.Add(showTurnConflictsCheck);

        activeTurnListPanel = new StackPanel { Name = prefix + "ActiveTurnListPanel", Spacing = 5 };
        var turnScroll = new ScrollViewer
        {
            Content = activeTurnListPanel,
            MaxHeight = 290,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        turnViewPanel.Children.Add(turnScroll);

        // Build View 2: 我的参赛计划
        myPlanViewPanel = new StackPanel { Spacing = 8 };
        myPlanListPanel = new StackPanel { Name = prefix + "MyPlanListPanel", Spacing = 5 };
        var myPlanScroll = new ScrollViewer
        {
            Content = myPlanListPanel,
            MaxHeight = 335,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        myPlanViewPanel.Children.Add(myPlanScroll);

        // Build View 3: 全局搜赛
        searchViewPanel = new StackPanel { Spacing = 6 };
        var searchRow = new Grid { ColumnDefinitions = new ColumnDefinitions("*,Auto"), ColumnSpacing = 6 };
        searchBox = new TextBox
        {
            Name = prefix + "RaceSearchBox",
            Watermark = "搜索赛事简称或马场（如：金鯱、有马、安田、东京）...",
            Height = 30,
            MinHeight = 0,
            FontSize = 12,
            VerticalContentAlignment = VerticalAlignment.Center,
        };
        searchRow.Children.Add(searchBox);
        var resetFiltersButton = new Button
        {
            Name = prefix + "ResetFiltersButton",
            Content = "重置",
            Height = 30,
            MinHeight = 0,
            Padding = new Thickness(10, 2),
            FontSize = 11.5,
        };
        Grid.SetColumn(resetFiltersButton, 1);
        searchRow.Children.Add(resetFiltersButton);
        searchViewPanel.Children.Add(searchRow);

        var filterWrap = new WrapPanel { Margin = new Thickness(0) };
        yearFilter = FilterCombo(prefix + "YearFilter", new[] { "全部年份", "初级", "经典级", "高级" }, 92);
        gradeFilter = FilterCombo(prefix + "GradeFilter", new[] { "全部级别", "G1", "G2", "G3" }, 88);
        surfaceFilter = FilterCombo(prefix + "SurfaceFilter", new[] { "全部场地", "草地", "泥地" }, 88);
        distanceFilter = FilterCombo(prefix + "DistanceFilter", new[] { "全部距离", "短距离", "英里", "中距离", "长距离" }, 96);
        showConflictsCheck = new CheckBox
        {
            Name = prefix + "ShowConflictsCheck",
            Content = "含剧本冲突赛事",
            IsChecked = false,
            FontSize = 11.5,
            Margin = new Thickness(2, 0, 0, 4),
            VerticalAlignment = VerticalAlignment.Center,
        };
        filterWrap.Children.Add(yearFilter);
        filterWrap.Children.Add(gradeFilter);
        filterWrap.Children.Add(surfaceFilter);
        filterWrap.Children.Add(distanceFilter);
        filterWrap.Children.Add(showConflictsCheck);
        searchViewPanel.Children.Add(filterWrap);

        searchBox.TextChanged += (_, _) => { if (!suppressEvents && rightMode == 2) RefreshRaceList(); };
        yearFilter.SelectionChanged += (_, _) => { if (!suppressEvents && rightMode == 2) RefreshRaceList(); };
        gradeFilter.SelectionChanged += (_, _) => { if (!suppressEvents && rightMode == 2) RefreshRaceList(); };
        surfaceFilter.SelectionChanged += (_, _) => { if (!suppressEvents && rightMode == 2) RefreshRaceList(); };
        distanceFilter.SelectionChanged += (_, _) => { if (!suppressEvents && rightMode == 2) RefreshRaceList(); };
        showConflictsCheck.IsCheckedChanged += (_, _) => { if (!suppressEvents && rightMode == 2) RefreshRaceList(); };
        resetFiltersButton.Click += (_, _) =>
        {
            suppressEvents = true;
            searchBox.Text = "";
            yearFilter.SelectedIndex = 0;
            gradeFilter.SelectedIndex = 0;
            surfaceFilter.SelectedIndex = 0;
            distanceFilter.SelectedIndex = 0;
            showConflictsCheck.IsChecked = false;
            suppressEvents = false;
            if (rightMode == 2) RefreshRaceList();
        };

        filterSummaryText = Note("");
        filterSummaryText.Name = prefix + "FilterResultSummary";
        filterSummaryText.FontSize = 11.5;
        filterSummaryText.Opacity = 0.8;
        searchViewPanel.Children.Add(filterSummaryText);

        raceListPanel = new StackPanel { Name = prefix + "RaceListPanel", Spacing = 5 };
        var listScroll = new ScrollViewer
        {
            Name = prefix + "RaceListScroll",
            Content = raceListPanel,
            MaxHeight = 220,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        searchViewPanel.Children.Add(listScroll);

        Grid.SetColumn(rightCard, 1);
        splitGrid.Children.Add(rightCard);

        // Responsive collapse for narrow panes
        bool? stacked = null;
        splitGrid.SizeChanged += (_, args) =>
        {
            if (args.NewSize.Width <= 0) return;
            var wantStacked = PacerLayoutGuard.NextNarrow(args.NewSize.Width, 760, stacked, 24);
            if (wantStacked == stacked) return;
            stacked = wantStacked;
            if (wantStacked)
            {
                splitGrid.ColumnDefinitions = new ColumnDefinitions("*");
                splitGrid.RowDefinitions = new RowDefinitions("Auto,Auto");
                Grid.SetColumn(leftCard, 0);
                Grid.SetRow(leftCard, 0);
                Grid.SetColumn(rightCard, 0);
                Grid.SetRow(rightCard, 1);
            }
            else
            {
                splitGrid.ColumnDefinitions = new ColumnDefinitions("1.14*,1*");
                splitGrid.RowDefinitions = new RowDefinitions("Auto");
                Grid.SetColumn(leftCard, 0);
                Grid.SetRow(leftCard, 0);
                Grid.SetColumn(rightCard, 1);
                Grid.SetRow(rightCard, 0);
            }
        };

        Children.Add(splitGrid);
        Children.Add(Note("提示：同一回合最多排 1 场加赛；国服凯旋门比赛 0 体力消耗，若未解锁或粉丝不足会自动返回继续训练。"));
        Children.Add(statusNote);

        RefreshAll();
    }

    public IReadOnlyList<string> SelectedRaceIds => selectedRaceIds;

    public static UmaCustomRaceEntry[] LoadCatalog(string? baseDirectory = null)
    {
        EnsureLoaded(baseDirectory);
        return cachedEntries!;
    }

    public static UmaRaceSchedulePreset[] LoadPresets(string? baseDirectory = null)
    {
        EnsureLoaded(baseDirectory);
        return cachedPresets!;
    }

    public static UmaSpecialRaceTarget[] LoadSpecialTargets(string? baseDirectory = null)
    {
        EnsureLoaded(baseDirectory);
        return cachedSpecialTargets!;
    }

    private static void EnsureLoaded(string? baseDirectory)
    {
        lock (CatalogLock)
        {
            if (cachedEntries != null && cachedPresets != null && cachedSpecialTargets != null)
                return;

            string? json = null;
            if (!string.IsNullOrWhiteSpace(baseDirectory))
            {
                var candidates = new[]
                {
                    Path.Combine(baseDirectory, "pacer", "backend", "core", "auto_trainer", "assets", "cn_larc_custom_races_v1.json"),
                    Path.Combine(baseDirectory, "backend", "core", "auto_trainer", "assets", "cn_larc_custom_races_v1.json"),
                    Path.Combine(baseDirectory, "resource", "cn_larc_custom_races_v1.json"),
                };
                foreach (var candidate in candidates)
                {
                    if (File.Exists(candidate))
                    {
                        json = File.ReadAllText(candidate, Encoding.UTF8);
                        break;
                    }
                }
            }

            if (json == null)
            {
                var asm = typeof(UmaCustomRaceCalendarEditor).Assembly;
                var resName = asm.GetManifestResourceNames()
                    .FirstOrDefault(n => n.EndsWith("cn_larc_custom_races_v1.json", StringComparison.OrdinalIgnoreCase));
                if (resName != null)
                {
                    using var stream = asm.GetManifestResourceStream(resName);
                    if (stream != null)
                    {
                        using var reader = new StreamReader(stream, Encoding.UTF8);
                        json = reader.ReadToEnd();
                    }
                }
            }

            if (string.IsNullOrWhiteSpace(json))
            {
                cachedEntries = Array.Empty<UmaCustomRaceEntry>();
                cachedById = new Dictionary<string, UmaCustomRaceEntry>(StringComparer.Ordinal);
                cachedPresets = Array.Empty<UmaRaceSchedulePreset>();
                cachedSpecialTargets = Array.Empty<UmaSpecialRaceTarget>();
                return;
            }

            var root = JObject.Parse(json);

            var spList = new List<UmaSpecialRaceTarget>();
            foreach (var sp in (root["special_race_targets"] as JArray ?? new JArray()).OfType<JObject>())
            {
                var id = (string?)sp["id"] ?? "";
                if (string.IsNullOrWhiteSpace(id)) continue;
                spList.Add(new UmaSpecialRaceTarget(
                    id,
                    (string?)sp["label_zh"] ?? id,
                    (string?)sp["short_zh"] ?? id,
                    (string?)sp["hint_zh"] ?? ""));
            }
            cachedSpecialTargets = spList.ToArray();

            var prList = new List<UmaRaceSchedulePreset>();
            foreach (var pr in (root["schedule_presets"] as JArray ?? new JArray()).OfType<JObject>())
            {
                var pid = (string?)pr["preset_id"] ?? "";
                if (string.IsNullOrWhiteSpace(pid)) continue;
                var rids = (pr["race_ids"] as JArray)?.Select(x => x.ToString()).Where(x => x.Length > 0).ToArray() ?? Array.Empty<string>();
                prList.Add(new UmaRaceSchedulePreset(
                    pid,
                    (string?)pr["title_zh"] ?? pid,
                    (string?)pr["tag_zh"] ?? "预设",
                    (string?)pr["description_zh"] ?? "",
                    (string?)pr["recommended_aptitude_zh"] ?? "",
                    rids));
            }
            cachedPresets = prList.ToArray();

            var rows = (root["races"] as JArray ?? new JArray()).OfType<JObject>();
            var parsed = new List<UmaCustomRaceEntry>();
            foreach (var r in rows)
            {
                var raceId = (string?)r["race_id"] ?? "";
                if (string.IsNullOrWhiteSpace(raceId)) continue;
                var nameZhCn = (string?)r["name_zh_cn"] ?? "";
                var shortZhCn = (string?)r["short_name_zh_cn"] ?? nameZhCn;
                var primaryAlias = (string?)r["primary_alias_zh"] ?? shortZhCn;
                var displayZh = (string?)r["display_name_zh"] ?? nameZhCn;
                var aliases = (r["aliases"] as JArray)?.Select(a => a.ToString()).Where(s => s.Length > 0).ToArray() ?? Array.Empty<string>();
                var grade = (string?)r["grade"] ?? "G3";
                var surface = (string?)r["surface"] ?? "turf";
                var surfaceZh = (string?)r["surface_zh"] ?? "草地";
                var distM = (int?)r["distance_m"] ?? 1600;
                var distCat = (string?)r["distance_category"] ?? "mile";
                var distCatZh = (string?)r["distance_category_zh"] ?? "英里";
                var venueZh = (string?)r["venue_zh"] ?? "";
                var fans = (int?)r["fan_requirement"] ?? 0;
                var yearIdx = (int?)r["year_index"] ?? 1;
                var yearZh = (string?)r["year_zh"] ?? "初级年";
                var month = (int?)r["month"] ?? 1;
                var half = (string?)r["half"] ?? "first";
                var dateLabel = (string?)r["date_label_zh"] ?? "";
                var ordinal = (int?)r["larc_ordinal"] ?? 0;
                var selectable = (bool?)r["larc_selectable"] == true;
                var conflict = (string?)r["larc_conflict_reason"];
                var direction = ((string?)r["direction"] ?? "").Trim().ToLowerInvariant();
                var directionZh = direction switch
                {
                    "right" => "顺",
                    "left" => "逆",
                    "straight" => "直",
                    _ => "",
                };

                var sb = new StringBuilder();
                sb.Append(displayZh).Append(' ')
                  .Append(nameZhCn).Append(' ')
                  .Append(shortZhCn).Append(' ')
                  .Append(primaryAlias).Append(' ')
                  .Append(venueZh).Append(' ')
                  .Append(dateLabel).Append(' ')
                  .Append(grade).Append(' ')
                  .Append(surfaceZh).Append(' ')
                  .Append(distCatZh).Append(' ')
                  .Append(directionZh).Append(' ')
                  .Append(distM);
                foreach (var alias in aliases)
                    sb.Append(' ').Append(alias);

                parsed.Add(new UmaCustomRaceEntry(
                    raceId, nameZhCn, shortZhCn, primaryAlias, displayZh, aliases,
                    grade, surface, surfaceZh, distM, distCat, distCatZh,
                    venueZh, fans, yearIdx, yearZh, month, half, dateLabel,
                    ordinal, selectable, conflict, sb.ToString().ToLowerInvariant(),
                    direction, directionZh));
            }

            cachedEntries = parsed
                .OrderBy(e => e.LarcOrdinal)
                .ThenBy(e => GradeRank(e.Grade))
                .ThenByDescending(e => e.FanRequirement)
                .ThenBy(e => e.NameZhCn, StringComparer.Ordinal)
                .ToArray();
            cachedById = cachedEntries.ToDictionary(e => e.RaceId, StringComparer.Ordinal);
        }
    }

    public static string FormatSummary(JObject options, string kind)
    {
        EnsureLoaded(AppContext.BaseDirectory);
        var styleId = NormalizeRunningStyle((string?)options["running_style"]) ?? "keep";
        var styleShort = RunningStyles.FirstOrDefault(s => s.Id == styleId).ShortLabel ?? "保持当前";
        var overridesCount = (options["race_running_styles"] as JObject)?.Properties()
            .Count(p => NormalizeRunningStyle(p.Value?.ToString()) != null) ?? 0;
        var overrideTag = overridesCount > 0 ? $"（含 {overridesCount} 项单场跑法）" : "";

        if (kind != "career")
            return "默认跑法：" + styleShort + overrideTag + " · 加赛在自动育成设置";

        var lookup = cachedById ?? new Dictionary<string, UmaCustomRaceEntry>(StringComparer.Ordinal);
        var ids = ReadNormalizedRaceIds(options, lookup);
        if (ids.Count == 0)
            return "默认跑法：" + styleShort + overrideTag + " · 未安排额外加赛";

        var preview = ids
            .Select(id => lookup.TryGetValue(id, out var e) ? ShortChipTitle(e) : id)
            .Take(2)
            .ToArray();
        var suffix = ids.Count > 2 ? "等" : "";
        return "默认：" + styleShort + overrideTag + " · 已排 " + ids.Count + " 场（" + string.Join("、", preview) + suffix + "）";
    }

    public static string? NormalizeRunningStyle(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;
        return raw.Trim().ToLowerInvariant() switch
        {
            "inherit" or "auto" => null,
            "default" or "keep" => "keep",
            "leader" => "leader",
            "front" => "front",
            "middle" or "betweener" => "middle",
            "back" or "chaser" => "back",
            _ => null,
        };
    }

    private static List<string> ReadNormalizedRaceIds(JObject options, Dictionary<string, UmaCustomRaceEntry> lookup)
    {
        var result = new List<UmaCustomRaceEntry>();
        var seenTurns = new HashSet<int>();
        if (options["custom_races"] is JArray arr)
        {
            foreach (var item in arr)
            {
                var id = item.Type == JTokenType.Object ? (string?)item["race_id"] : item.ToString();
                if (string.IsNullOrWhiteSpace(id)) continue;
                if (!lookup.TryGetValue(id.Trim(), out var entry) || !entry.LarcSelectable) continue;
                if (!seenTurns.Add(entry.LarcOrdinal)) continue;
                result.Add(entry);
                if (result.Count >= MaxCustomRaces) break;
            }
        }
        return result.OrderBy(e => e.LarcOrdinal).Select(e => e.RaceId).ToList();
    }

    private void LoadStateFromOptions()
    {
        selectedRaceIds.Clear();
        selectedRaceIds.AddRange(ReadNormalizedRaceIds(options, byId));

        raceRunningStyles.Clear();
        if (options["race_running_styles"] is JObject styleMap)
        {
            foreach (var prop in styleMap.Properties())
            {
                var norm = NormalizeRunningStyle(prop.Value?.ToString());
                if (norm != null)
                    raceRunningStyles[prop.Name] = norm;
            }
        }
    }

    private void PickInitialActiveLocation()
    {
        activeYear = 2;
        activeOrdinal = 30; // 经典级 4月上半月
        if (selectedRaceIds.Count == 0) return;
        var classicFirst = selectedRaceIds
            .Select(id => byId.TryGetValue(id, out var e) ? e : null)
            .FirstOrDefault(e => e != null && e.YearIndex == 2);
        if (classicFirst != null)
        {
            activeYear = 2;
            activeOrdinal = classicFirst.LarcOrdinal;
            return;
        }
        if (byId.TryGetValue(selectedRaceIds[0], out var anyFirst))
        {
            activeYear = anyFirst.YearIndex;
            activeOrdinal = anyFirst.LarcOrdinal;
        }
    }

    private void EnsureActiveOrdinalMatchesYear()
    {
        if (YearFromOrdinal(activeOrdinal) == activeYear) return;
        var scheduledInYear = selectedRaceIds
            .Select(id => byId.TryGetValue(id, out var e) ? e : null)
            .FirstOrDefault(e => e != null && e.YearIndex == activeYear);
        if (scheduledInYear != null)
        {
            activeOrdinal = scheduledInYear.LarcOrdinal;
            return;
        }
        activeOrdinal = activeYear switch
        {
            1 => 22, // 初级 12月上半月
            2 => 30, // 经典级 4月上半月
            _ => 52, // 高级 3月上半月
        };
    }

    private void WriteStateToOptions()
    {
        if (kind == "career")
        {
            var ordered = selectedRaceIds
                .Where(id => byId.ContainsKey(id))
                .OrderBy(id => byId[id].LarcOrdinal)
                .ToList();
            selectedRaceIds.Clear();
            selectedRaceIds.AddRange(ordered);
            var arr = new JArray();
            foreach (var id in selectedRaceIds)
                arr.Add(id);
            options["custom_races"] = arr;
        }

        var activeCustomSet = new HashSet<string>(selectedRaceIds, StringComparer.Ordinal);
        foreach (var key in raceRunningStyles.Keys.ToArray())
        {
            if (!key.StartsWith("scenario.", StringComparison.Ordinal) && kind == "career" && !activeCustomSet.Contains(key))
                raceRunningStyles.Remove(key);
        }

        if (raceRunningStyles.Count == 0)
        {
            options.Remove("race_running_styles");
        }
        else
        {
            var map = new JObject();
            foreach (var kv in raceRunningStyles.OrderBy(k => k.Key, StringComparer.Ordinal))
                map[kv.Key] = kv.Value;
            options["race_running_styles"] = map;
        }
    }

    private void SyncSpecialStyleCombos()
    {
        suppressEvents = true;
        try
        {
            foreach (var kv in specialCombos)
            {
                var cur = raceRunningStyles.TryGetValue(kv.Key, out var ov) ? ov : "inherit";
                var idx = Math.Max(0, Array.FindIndex(PerRaceRunningStyles, p => p.Id == cur));
                if (kv.Value.SelectedIndex != idx)
                    kv.Value.SelectedIndex = idx;
            }
        }
        finally
        {
            suppressEvents = false;
        }
    }

    private void SetPerRaceRunningStyle(string raceKey, string pickedStyleId, string displayTitle)
    {
        var norm = NormalizeRunningStyle(pickedStyleId);
        if (norm == null)
            raceRunningStyles.Remove(raceKey);
        else
            raceRunningStyles[raceKey] = norm;

        WriteStateToOptions();
        SyncSpecialStyleCombos();
        var styleText = PerRaceRunningStyles.FirstOrDefault(p => p.Id == (norm ?? "inherit")).Label ?? "跟随默认跑法";
        SetStatus($"「{displayTitle}」跑法已设为：{styleText}（请保存配置）。");
        RefreshCalendar();
        RefreshRightPane();
        onChanged();
    }

    private void ApplySchedulePreset(UmaRaceSchedulePreset preset)
    {
        selectedRaceIds.Clear();
        foreach (var rid in preset.RaceIds)
        {
            if (byId.TryGetValue(rid, out var entry) && entry.LarcSelectable && selectedRaceIds.Count < MaxCustomRaces)
                selectedRaceIds.Add(rid);
        }
        WriteStateToOptions();
        PickInitialActiveLocation();
        SetStatus($"已套用攻略赛程「{preset.TitleZh}」（共 {selectedRaceIds.Count} 场），可在右侧网格或清单中继续微调。");
        RefreshAll();
        onChanged();
    }

    private async Task PasteSchedulePresetAsync()
    {
        var clipboard = TopLevel.GetTopLevel(this)?.Clipboard;
        if (clipboard == null)
        {
            SetStatus("当前窗口剪贴板不可用，请检查权限后重试。");
            return;
        }
        string? raw;
        try
        {
            raw = await clipboard.TryGetTextAsync();
        }
        catch
        {
            SetStatus("读取剪贴板失败，请重新复制赛程 JSON 后再试。");
            return;
        }
        if (string.IsNullOrWhiteSpace(raw))
        {
            SetStatus("剪贴板为空，请先在种马规划器中点击“复制 Pacer 参赛预设”。");
            return;
        }
        JObject parsed;
        try
        {
            parsed = JObject.Parse(raw);
        }
        catch
        {
            SetStatus("剪贴板内容不是有效的 JSON 赛程预设。");
            return;
        }
        var preset = parsed["pacer_preset"] as JObject ?? parsed;
        if (preset["custom_races"] is not JArray raceArr)
        {
            SetStatus("剪贴板 JSON 缺少 custom_races 赛程列表。");
            return;
        }

        var nextIds = new List<string>();
        var seenTurns = new HashSet<int>();
        var skipped = 0;
        foreach (var tok in raceArr)
        {
            var rid = tok.Type == JTokenType.Object ? (string?)tok["race_id"] : tok.ToString();
            rid = rid?.Trim() ?? "";
            if (rid.Length == 0) continue;
            if (byId.TryGetValue(rid, out var entry) && entry.LarcSelectable && seenTurns.Add(entry.LarcOrdinal) && nextIds.Count < MaxCustomRaces)
                nextIds.Add(entry.RaceId);
            else
                skipped++;
        }

        selectedRaceIds.Clear();
        selectedRaceIds.AddRange(nextIds);

        if (NormalizeRunningStyle((string?)preset["running_style"]) is string normStyle)
        {
            var styleIdx = Array.FindIndex(RunningStyles, s => s.Id == normStyle);
            if (styleIdx >= 0)
            {
                suppressEvents = true;
                try
                {
                    styleCombo.SelectedIndex = styleIdx;
                    options["running_style"] = normStyle;
                }
                finally
                {
                    suppressEvents = false;
                }
            }
        }

        if (preset["race_running_styles"] is JObject styleMap)
        {
            foreach (var prop in styleMap.Properties())
            {
                var norm = NormalizeRunningStyle(prop.Value?.ToString());
                if (norm == null) raceRunningStyles.Remove(prop.Name);
                else raceRunningStyles[prop.Name] = norm;
            }
            SyncSpecialStyleCombos();
        }

        WriteStateToOptions();
        PickInitialActiveLocation();
        var titleText = ((string?)preset["title"])?.Trim();
        var titlePrefix = string.IsNullOrEmpty(titleText) ? "" : $"「{titleText}」";
        var skipNote = skipped > 0 ? $"（已略过 {skipped} 场剧本冲突或超出上限项）" : "";
        SetStatus($"已从剪贴板导入种马赛程{titlePrefix}：共 {selectedRaceIds.Count} 场重赏{skipNote}，请保存配置。");
        RefreshAll();
        onChanged();
    }

    private void ToggleOrReplaceRace(UmaCustomRaceEntry entry, bool jumpCalendar = true)
    {
        if (!entry.LarcSelectable)
            return;

        if (selectedRaceIds.Contains(entry.RaceId))
        {
            selectedRaceIds.Remove(entry.RaceId);
            raceRunningStyles.Remove(entry.RaceId);
            WriteStateToOptions();
            SetStatus("已取消加赛：" + entry.DateLabelZh + " · " + entry.ShortNameZhCn + "（请保存配置）。");
            if (jumpCalendar)
            {
                activeYear = entry.YearIndex;
                activeOrdinal = entry.LarcOrdinal;
            }
            RefreshAll();
            onChanged();
            return;
        }

        var existingSameTurnId = selectedRaceIds.FirstOrDefault(id => byId.TryGetValue(id, out var other) && other.LarcOrdinal == entry.LarcOrdinal);
        if (existingSameTurnId != null)
        {
            var prevName = byId[existingSameTurnId].ShortNameZhCn;
            selectedRaceIds.Remove(existingSameTurnId);
            raceRunningStyles.Remove(existingSameTurnId);
            selectedRaceIds.Add(entry.RaceId);
            WriteStateToOptions();
            SetStatus(entry.DateLabelZh + " 已由「" + prevName + "」替换为「" + entry.ShortNameZhCn + "」（请保存配置）。");
            if (jumpCalendar)
            {
                activeYear = entry.YearIndex;
                activeOrdinal = entry.LarcOrdinal;
            }
            RefreshAll();
            onChanged();
            return;
        }

        if (selectedRaceIds.Count >= MaxCustomRaces)
        {
            SetStatus("单局最多可安排 " + MaxCustomRaces + " 场自定义加赛，请先移除部分场次。");
            return;
        }

        selectedRaceIds.Add(entry.RaceId);
        WriteStateToOptions();
        SetStatus("已排入加赛：" + entry.DateLabelZh + " · " + entry.ShortNameZhCn + "（请保存配置）。");
        if (jumpCalendar)
        {
            activeYear = entry.YearIndex;
            activeOrdinal = entry.LarcOrdinal;
        }
        RefreshAll();
        onChanged();
    }

    private void JumpToTurn(int ordinal)
    {
        activeYear = YearFromOrdinal(ordinal);
        activeOrdinal = ordinal;
        rightMode = 0;
        RefreshAll();
    }

    private void StepTurn(int delta)
    {
        if (navigableOrdinals.Length == 0) return;
        var idx = Array.IndexOf(navigableOrdinals, activeOrdinal);
        if (idx < 0)
        {
            idx = delta >= 0
                ? Array.FindIndex(navigableOrdinals, o => o > activeOrdinal)
                : Array.FindLastIndex(navigableOrdinals, o => o < activeOrdinal);
            if (idx < 0) idx = delta >= 0 ? 0 : navigableOrdinals.Length - 1;
        }
        else
        {
            idx = Math.Clamp(idx + delta, 0, navigableOrdinals.Length - 1);
        }
        JumpToTurn(navigableOrdinals[idx]);
    }

    private void StepYear(int delta)
    {
        var targetYear = Math.Clamp(activeYear + delta, 1, 3);
        if (targetYear == activeYear)
        {
            targetYear = delta < 0 ? 1 : 3;
        }
        activeYear = targetYear;
        activeOrdinal = targetYear switch
        {
            1 => 13, // 初级 7月下半月（首场可加赛重赏）
            2 => 24, // 经典级 1月上半月
            _ => 48, // 高级 1月上半月
        };
        rightMode = 0;
        RefreshAll();
    }

    private void RefreshAll()
    {
        RefreshCalendar();
        RefreshRightPane();
    }

    private HashSet<int> ComputeThreeRaceStreakOrdinals()
    {
        var occupied = new HashSet<int>(LarcFixedGoals.Keys);
        foreach (var id in selectedRaceIds)
        {
            if (byId.TryGetValue(id, out var e))
                occupied.Add(e.LarcOrdinal);
        }
        var streak = new HashSet<int>();
        foreach (var o in occupied)
        {
            if ((occupied.Contains(o - 2) && occupied.Contains(o - 1))
                || (occupied.Contains(o - 1) && occupied.Contains(o + 1))
                || (occupied.Contains(o + 1) && occupied.Contains(o + 2)))
            {
                streak.Add(o);
            }
        }
        return streak;
    }

    private void RefreshCalendar()
    {
        if (calGrid == null) return;

        var selectedByTurn = new Dictionary<int, UmaCustomRaceEntry>();
        var yearCounts = new int[4];
        foreach (var id in selectedRaceIds)
        {
            if (byId.TryGetValue(id, out var entry))
            {
                selectedByTurn[entry.LarcOrdinal] = entry;
                if (entry.YearIndex is >= 1 and <= 3)
                    yearCounts[entry.YearIndex]++;
            }
        }
        var streakOrdinals = ComputeThreeRaceStreakOrdinals();
        var hasCustomStreak = selectedByTurn.Keys.Any(o => streakOrdinals.Contains(o));

        if (selectedCountBadge != null)
            selectedCountBadge.Text = $"已排 {selectedRaceIds.Count} / {MaxCustomRaces} 场";
        if (clearAllButton != null)
            clearAllButton.IsEnabled = selectedRaceIds.Count > 0;
        if (myScheduleBtn != null)
            myScheduleBtn.Content = $"我的参赛计划 ({selectedRaceIds.Count})";
        if (streakBadge != null)
        {
            if (selectedRaceIds.Count == 0)
            {
                streakBadge.Text = "点选绿色 + 格排入加赛";
                streakBadge.ClearValue(TextBlock.ForegroundProperty);
                streakBadge.Opacity = 0.72;
            }
            else if (hasCustomStreak)
            {
                streakBadge.Text = "⚠ 含三连战（注意疲劳）";
                streakBadge.Foreground = StreakWarnBrush;
                streakBadge.Opacity = 0.96;
            }
            else
            {
                streakBadge.Text = "✓ 零三连战冲突";
                streakBadge.Foreground = StreakOkBrush;
                streakBadge.Opacity = 0.94;
            }
        }

        for (var y = 1; y <= 3; y++)
        {
            var btn = yearTabButtons[y - 1];
            var active = y == activeYear;
            var countTag = yearCounts[y] > 0 ? $" ({yearCounts[y]})" : "";
            btn.Content = YearTabName(y) + countTag;
            if (active)
            {
                btn.Background = UmaGreenDeepBrush;
                btn.Foreground = Brushes.White;
                btn.FontWeight = FontWeight.Bold;
                btn.Opacity = 1.0;
            }
            else
            {
                btn.Background = Brushes.Transparent;
                btn.ClearValue(Button.ForegroundProperty);
                btn.FontWeight = FontWeight.Medium;
                btn.Opacity = 0.85;
            }
        }

        calGrid.Children.Clear();
        for (var cell = 0; cell < 24; cell++)
        {
            if (activeYear == 1 && cell < 12)
            {
                calGrid.Children.Add(BuildPreDebutCell(JuniorPreDebutLabels[cell]));
                continue;
            }
            if (activeYear == 3 && cell >= 19)
            {
                calGrid.Children.Add(new Border { Margin = new Thickness(3, 1.5), IsHitTestVisible = false });
                continue;
            }

            var ordinal = activeYear switch
            {
                1 => cell,          // 12..23
                2 => 24 + cell,     // 24..47
                _ => 48 + cell,     // 48..66
            };
            calGrid.Children.Add(BuildCalendarCell(ordinal, selectedByTurn, streakOrdinals));
        }
    }

    private Control BuildPreDebutCell(string dateLabel)
    {
        var box = new Border
        {
            Height = 33,
            CornerRadius = new CornerRadius(5),
            Background = SlotMutedBg,
            BorderBrush = CardSurfaceBorder,
            BorderThickness = new Thickness(1),
            Opacity = 0.42,
            Child = new TextBlock
            {
                Text = "出道前",
                FontSize = 9.5,
                Opacity = 0.65,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            },
        };
        var lbl = new TextBlock
        {
            Text = dateLabel,
            FontSize = 10,
            Opacity = 0.56,
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        return new StackPanel
        {
            Margin = new Thickness(3, 1.5),
            Spacing = 1,
            IsHitTestVisible = false,
            Children = { box, lbl },
        };
    }

    private Control BuildCalendarCell(
        int ordinal,
        Dictionary<int, UmaCustomRaceEntry> selectedByTurn,
        HashSet<int> streakOrdinals)
    {
        var month = (ordinal % 24) / 2 + 1;
        var isFirst = ordinal % 2 == 0;
        var dateLabel = month + "月 " + (isFirst ? "上半月" : "下半月");
        var isActive = ordinal == activeOrdinal && rightMode == 0;

        selectedByTurn.TryGetValue(ordinal, out var scheduled);
        LarcFixedGoals.TryGetValue(ordinal, out var fixedGoal);
        var isOverseas = OverseasLockedOrdinals.Contains(ordinal);
        byOrdinal.TryGetValue(ordinal, out var turnRaces);
        var selectableCount = turnRaces?.Count(r => r.LarcSelectable) ?? 0;
        var hasG1 = turnRaces?.Any(r => r.LarcSelectable && r.Grade == "G1") == true;
        var isStreak = scheduled != null && streakOrdinals.Contains(ordinal);

        IBrush boxBg;
        IBrush defaultBorder = SlotBorder;
        string? tooltip;
        Control boxInner;

        if (fixedGoal != null)
        {
            boxBg = HorizontalGrad(fixedGoal.AccentStartHex, fixedGoal.AccentEndHex);
            defaultBorder = new SolidColorBrush(Color.Parse(fixedGoal.AccentEndHex));
            tooltip = $"[剧本目标] {fixedGoal.Title}（{YearTabName(YearFromOrdinal(ordinal))} {dateLabel}）· {fixedGoal.Subtitle}\n点击可在右侧查看或单独设置关键战跑法";

            var hasSpStyle = raceRunningStyles.TryGetValue(fixedGoal.SpecialKey, out var spOv);
            var spShort = hasSpStyle && spOv != null
                ? PerRaceRunningStyles.FirstOrDefault(p => p.Id == spOv).CompactLabel
                : null;

            var innerGrid = new Grid { RowDefinitions = new RowDefinitions("Auto,*") };
            var topRow = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Spacing = 3,
                Children =
                {
                    new Border
                    {
                        Background = GoalTagBg,
                        CornerRadius = new CornerRadius(3),
                        Padding = new Thickness(4, 0),
                        Child = new TextBlock { Text = "目标", FontSize = 9, FontWeight = FontWeight.Bold, Foreground = Brushes.White },
                    }
                },
            };
            if (!string.IsNullOrEmpty(spShort))
            {
                topRow.Children.Add(new Border
                {
                    Background = StyleTagBg,
                    CornerRadius = new CornerRadius(3),
                    Padding = new Thickness(4, 0),
                    Child = new TextBlock { Text = spShort, FontSize = 9, FontWeight = FontWeight.Bold, Foreground = Brushes.White },
                });
            }
            innerGrid.Children.Add(topRow);

            var title = new TextBlock
            {
                Text = fixedGoal.Title,
                FontSize = 11.5,
                FontWeight = FontWeight.Bold,
                Foreground = Brushes.White,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            Grid.SetRow(title, 1);
            innerGrid.Children.Add(title);
            boxInner = innerGrid;
        }
        else if (scheduled != null)
        {
            boxBg = scheduled.Grade switch
            {
                "G1" => G1BannerBrush,
                "G2" => G2BannerBrush,
                _ => G3BannerBrush,
            };
            defaultBorder = GradePillBrush(scheduled.Grade);
            tooltip = $"已排：{scheduled.DisplayNameZh}（{scheduled.DateLabelZh} · {scheduled.SurfaceZh}{scheduled.DistanceM}m · {scheduled.VenueZh}）\n点击在右侧切换同回合其他赛事或设置单场跑法";

            var hasOv = raceRunningStyles.TryGetValue(scheduled.RaceId, out var ov);
            var ovShort = hasOv && ov != null
                ? PerRaceRunningStyles.FirstOrDefault(p => p.Id == ov).CompactLabel
                : null;

            var innerGrid = new Grid { RowDefinitions = new RowDefinitions("Auto,*"), Margin = new Thickness(0, 0, 14, 0) };
            var topRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 3 };
            topRow.Children.Add(new Border
            {
                Background = new SolidColorBrush(Color.FromArgb(170, 15, 23, 42)),
                CornerRadius = new CornerRadius(3),
                Padding = new Thickness(4, 0),
                Child = new TextBlock
                {
                    Text = DisplayGrade(scheduled.Grade),
                    FontSize = 9,
                    FontWeight = FontWeight.Bold,
                    Foreground = Brushes.White,
                },
            });
            if (!string.IsNullOrEmpty(ovShort))
            {
                topRow.Children.Add(new Border
                {
                    Background = StyleTagBg,
                    CornerRadius = new CornerRadius(3),
                    Padding = new Thickness(4, 0),
                    Child = new TextBlock { Text = ovShort, FontSize = 9, FontWeight = FontWeight.Bold, Foreground = Brushes.White },
                });
            }
            if (isStreak)
            {
                topRow.Children.Add(new Border
                {
                    Background = StreakBadgeBg,
                    CornerRadius = new CornerRadius(3),
                    Padding = new Thickness(3, 0),
                    Child = new TextBlock { Text = "连", FontSize = 9, FontWeight = FontWeight.Bold, Foreground = Brushes.White },
                });
            }
            innerGrid.Children.Add(topRow);

            var title = new TextBlock
            {
                Text = CompactCalendarTitle(scheduled),
                FontSize = 11,
                FontWeight = FontWeight.Bold,
                Foreground = Brushes.White,
                HorizontalAlignment = HorizontalAlignment.Left,
                VerticalAlignment = VerticalAlignment.Center,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            Grid.SetRow(title, 1);
            innerGrid.Children.Add(title);
            boxInner = innerGrid;
        }
        else if (isOverseas)
        {
            boxBg = SlotOverseasBg;
            tooltip = $"海外远征期间（{dateLabel}）不可安排国内加赛";
            boxInner = new TextBlock
            {
                Text = "✈ 远征",
                FontSize = 10.5,
                Opacity = 0.72,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
        }
        else if (selectableCount == 0)
        {
            boxBg = SlotMutedBg;
            tooltip = $"{dateLabel} 无重赏（G1/G2/G3）赛事";
            boxInner = new TextBlock
            {
                Text = "无重赏",
                FontSize = 10,
                Opacity = 0.45,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
        }
        else
        {
            boxBg = SlotEmptyBg;
            tooltip = $"{YearTabName(YearFromOrdinal(ordinal))} {dateLabel}：共 {selectableCount} 场重赏可选，点击前往右侧「参赛设定」选赛";
            var emptyGrid = new Grid();
            emptyGrid.Children.Add(new TextBlock
            {
                Text = "＋",
                FontSize = 18,
                FontWeight = FontWeight.Bold,
                Foreground = UmaGreenBrush,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            });
            if (hasG1)
            {
                emptyGrid.Children.Add(new Border
                {
                    Background = G1HintBg,
                    CornerRadius = new CornerRadius(3),
                    Padding = new Thickness(3, 0),
                    HorizontalAlignment = HorizontalAlignment.Right,
                    VerticalAlignment = VerticalAlignment.Top,
                    Margin = new Thickness(0, 1, 1, 0),
                    Child = new TextBlock
                    {
                        Text = "G1",
                        FontSize = 8.5,
                        FontWeight = FontWeight.Bold,
                        Foreground = G1HintFg,
                    },
                });
            }
            boxInner = emptyGrid;
        }

        var slotBtn = new Button
        {
            Name = prefix + "CalSlot_" + ordinal,
            Margin = new Thickness(0),
            Padding = new Thickness(4, 2),
            Background = Brushes.Transparent,
            BorderThickness = new Thickness(0),
            CornerRadius = new CornerRadius(5),
            HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
            Content = boxInner,
        };
        slotBtn.Click += (_, _) => JumpToTurn(ordinal);

        var cellHost = new Grid();
        cellHost.Children.Add(slotBtn);

        if (scheduled != null)
        {
            var capturedRace = scheduled;
            var removeBtn = new Button
            {
                Name = prefix + "SlotRemove_" + ordinal,
                Content = "✕",
                Width = 16,
                Height = 16,
                MinWidth = 0,
                MinHeight = 0,
                Padding = new Thickness(0),
                CornerRadius = new CornerRadius(8),
                Background = SlotRemoveBg,
                Foreground = Brushes.White,
                BorderThickness = new Thickness(0),
                FontSize = 9.5,
                HorizontalAlignment = HorizontalAlignment.Right,
                VerticalAlignment = VerticalAlignment.Top,
                Margin = new Thickness(0, 2, 2, 0),
                HorizontalContentAlignment = HorizontalAlignment.Center,
                VerticalContentAlignment = VerticalAlignment.Center,
            };
            ToolTip.SetTip(removeBtn, "取消 " + scheduled.ShortNameZhCn);
            removeBtn.Click += (_, _) => ToggleOrReplaceRace(capturedRace, jumpCalendar: false);
            cellHost.Children.Add(removeBtn);
        }

        var slotBorder = new Border
        {
            Height = 33,
            CornerRadius = new CornerRadius(5),
            Background = boxBg,
            BorderBrush = isActive ? ActiveRingBrush : defaultBorder,
            BorderThickness = isActive ? new Thickness(2) : new Thickness(1),
            Child = cellHost,
        };
        ToolTip.SetTip(slotBorder, tooltip);

        var dateText = new TextBlock
        {
            Text = dateLabel,
            FontSize = 10,
            FontWeight = isActive ? FontWeight.Bold : FontWeight.Normal,
            Opacity = isActive ? 1.0 : 0.78,
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        if (isActive)
            dateText.Foreground = UmaGreenBrightBrush;

        return new StackPanel
        {
            Margin = new Thickness(3, 1.5),
            Spacing = 1,
            Children = { slotBorder, dateText },
        };
    }

    private void RefreshRightPane()
    {
        if (rightBodyHost == null) return;

        if (rightTabSelectedBtn != null)
            rightTabSelectedBtn.Content = $"我的参赛计划 ({selectedRaceIds.Count})";

        var tabs = new[]
        {
            (Btn: rightTabTurnBtn, Mode: 0),
            (Btn: rightTabSelectedBtn, Mode: 1),
            (Btn: rightTabSearchBtn, Mode: 2),
        };
        foreach (var (btn, modeIdx) in tabs)
        {
            if (btn == null) continue;
            var active = rightMode == modeIdx;
            if (active)
            {
                if (!btn.Classes.Contains("Accent")) btn.Classes.Add("Accent");
                btn.FontWeight = FontWeight.SemiBold;
            }
            else
            {
                btn.Classes.Remove("Accent");
                btn.FontWeight = FontWeight.Normal;
            }
        }

        if (rightMode == 0)
        {
            RefreshTurnView();
            if (!ReferenceEquals(rightBodyHost.Content, turnViewPanel))
                rightBodyHost.Content = turnViewPanel;
        }
        else if (rightMode == 1)
        {
            RefreshMyPlanView();
            if (!ReferenceEquals(rightBodyHost.Content, myPlanViewPanel))
                rightBodyHost.Content = myPlanViewPanel;
        }
        else
        {
            RefreshRaceList();
            if (!ReferenceEquals(rightBodyHost.Content, searchViewPanel))
                rightBodyHost.Content = searchViewPanel;
        }
    }

    private void RefreshTurnView()
    {
        if (activeTurnTitle == null || activeTurnSubText == null || activeTurnListPanel == null || showTurnConflictsCheck == null)
            return;

        var y = YearFromOrdinal(activeOrdinal);
        var m = (activeOrdinal % 24) / 2 + 1;
        var halfZh = activeOrdinal % 2 == 0 ? "上半月" : "下半月";
        activeTurnTitle.Text = $"{YearTabName(y)} {m}月{halfZh}";

        var selectedSet = new HashSet<string>(selectedRaceIds, StringComparer.Ordinal);
        var selectedByTurn = new Dictionary<int, UmaCustomRaceEntry>();
        foreach (var id in selectedRaceIds)
        {
            if (byId.TryGetValue(id, out var sel))
                selectedByTurn[sel.LarcOrdinal] = sel;
        }

        activeTurnListPanel.Children.Clear();
        byOrdinal.TryGetValue(activeOrdinal, out var rawTurnRaces);
        var turnRaces = rawTurnRaces ?? Array.Empty<UmaCustomRaceEntry>();
        var selectableRaces = turnRaces.Where(r => r.LarcSelectable).ToArray();
        var conflictRaces = turnRaces.Where(r => !r.LarcSelectable).ToArray();

        if (LarcFixedGoals.TryGetValue(activeOrdinal, out var goalInfo))
        {
            activeTurnSubText.Text = "剧本固定目标回合（无需额外加赛）";
            showTurnConflictsCheck.IsVisible = conflictRaces.Length > 0;
            activeTurnListPanel.Children.Add(BuildGoalTurnCard(goalInfo));

            if (showTurnConflictsCheck.IsChecked == true)
            {
                foreach (var cr in conflictRaces)
                    activeTurnListPanel.Children.Add(BuildInGameRaceCard(cr, selectedSet, selectedByTurn, jumpCalendarOnPick: false));
            }
            return;
        }

        if (OverseasLockedOrdinals.Contains(activeOrdinal))
        {
            activeTurnSubText.Text = "海外远征期间（7～9月）不可参加国内加赛";
            showTurnConflictsCheck.IsVisible = conflictRaces.Length > 0;
            activeTurnListPanel.Children.Add(BuildTurnNoticeCard(
                "✈ 海外远征中",
                "经典级 / 高级 7月～9月正在法国远征，无法返回日本国内加赛。请点击上方 ‹ › 箭头或左侧高亮日历格选择其他回合。"));
            if (showTurnConflictsCheck.IsChecked == true)
            {
                foreach (var cr in conflictRaces)
                    activeTurnListPanel.Children.Add(BuildInGameRaceCard(cr, selectedSet, selectedByTurn, jumpCalendarOnPick: false));
            }
            return;
        }

        showTurnConflictsCheck.IsVisible = false;
        if (selectableRaces.Length == 0)
        {
            activeTurnSubText.Text = "本回合无 G1 / G2 / G3 重赏赛事";
            activeTurnListPanel.Children.Add(BuildTurnNoticeCard(
                "无重赏加赛",
                "该半月在游戏内仅包含 OP / Pre-OP 赛事，重赏加赛表未收录。请点击上方 ‹ › 箭头浏览相邻有重赏的回合。"));
            return;
        }

        selectedByTurn.TryGetValue(activeOrdinal, out var currentPick);
        activeTurnSubText.Text = currentPick != null
            ? $"共 {selectableRaces.Length} 场可选 · 本回合已排「{currentPick.ShortNameZhCn}」"
            : $"共 {selectableRaces.Length} 场可选重赏（同回合最多排 1 场）";

        foreach (var entry in selectableRaces)
            activeTurnListPanel.Children.Add(BuildInGameRaceCard(entry, selectedSet, selectedByTurn, jumpCalendarOnPick: false));
    }

    private Control BuildGoalTurnCard(LarcFixedGoal goal)
    {
        var curOv = raceRunningStyles.TryGetValue(goal.SpecialKey, out var ov) ? ov : "inherit";
        var ovIdx = Math.Max(0, Array.FindIndex(PerRaceRunningStyles, p => p.Id == curOv));
        var goalCombo = new ComboBox
        {
            Name = prefix + "GoalTurnStyleCombo",
            ItemsSource = PerRaceRunningStyles.Select(p => "跑法·" + p.Label).ToArray(),
            SelectedIndex = ovIdx,
            MinWidth = 172,
            Height = 30,
            MinHeight = 0,
            FontSize = 12,
        };
        goalCombo.SelectionChanged += (_, _) =>
        {
            if (suppressEvents || goalCombo.SelectedIndex < 0 || goalCombo.SelectedIndex >= PerRaceRunningStyles.Length)
                return;
            var picked = PerRaceRunningStyles[goalCombo.SelectedIndex].Id;
            SetPerRaceRunningStyle(goal.SpecialKey, picked, goal.Title);
        };

        var banner = new Border
        {
            Width = 90,
            MinHeight = 50,
            CornerRadius = new CornerRadius(6),
            Background = HorizontalGrad(goal.AccentStartHex, goal.AccentEndHex),
            Padding = new Thickness(6, 4),
            Child = new TextBlock
            {
                Text = goal.Title,
                FontSize = 12.5,
                FontWeight = FontWeight.Bold,
                Foreground = Brushes.White,
                TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            },
        };

        var info = new StackPanel
        {
            Spacing = 4,
            VerticalAlignment = VerticalAlignment.Center,
            Children =
            {
                new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Spacing = 6,
                    Children =
                    {
                        new Border
                        {
                            Background = GoalTagBg,
                            CornerRadius = new CornerRadius(9),
                            Padding = new Thickness(7, 1),
                            Child = new TextBlock { Text = "剧本目标", FontSize = 10, FontWeight = FontWeight.Bold, Foreground = Brushes.White },
                        },
                        new TextBlock { Text = goal.Subtitle, FontSize = 12, FontWeight = FontWeight.SemiBold },
                    },
                },
                new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Spacing = 8,
                    VerticalAlignment = VerticalAlignment.Center,
                    Children =
                    {
                        new TextBlock { Text = "本战单独跑法：", FontSize = 11.5, Opacity = 0.82, VerticalAlignment = VerticalAlignment.Center },
                        goalCombo,
                    },
                },
            },
        };

        var grid = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*"), ColumnSpacing = 12 };
        grid.Children.Add(banner);
        Grid.SetColumn(info, 1);
        grid.Children.Add(info);

        return new Border
        {
            Background = SelectedCardBg,
            BorderBrush = SelectedCardBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(10, 9),
            Child = grid,
        };
    }

    private Control BuildTurnNoticeCard(string title, string detail) => new Border
    {
        Background = CardSurfaceBg,
        BorderBrush = CardSurfaceBorder,
        BorderThickness = new Thickness(1),
        CornerRadius = new CornerRadius(8),
        Padding = new Thickness(14, 14),
        Child = new StackPanel
        {
            Spacing = 6,
            Children =
            {
                new TextBlock { Text = title, FontSize = 13.5, FontWeight = FontWeight.SemiBold },
                new TextBlock { Text = detail, FontSize = 12, Opacity = 0.8, TextWrapping = TextWrapping.Wrap },
            },
        },
    };

    private void RefreshMyPlanView()
    {
        if (myPlanListPanel == null) return;
        myPlanListPanel.Children.Clear();

        if (selectedRaceIds.Count == 0)
        {
            myPlanListPanel.Children.Add(BuildTurnNoticeCard(
                "暂未安排额外加赛",
                "可点击左上角「攻略赛程 -> 一键套用此赛程」直接抄作业，或在左侧 4×6 参赛计划网格点选任意绿色 ＋ 回合添加比赛。"));
            return;
        }

        var streakOrdinals = ComputeThreeRaceStreakOrdinals();
        foreach (var id in selectedRaceIds)
        {
            if (!byId.TryGetValue(id, out var entry)) continue;
            var hasOverride = raceRunningStyles.TryGetValue(id, out var curOverride);
            var curStyleId = hasOverride && curOverride != null ? curOverride : "inherit";
            var styleIdx = Math.Max(0, Array.FindIndex(PerRaceRunningStyles, p => p.Id == curStyleId));
            var isStreak = streakOrdinals.Contains(entry.LarcOrdinal);

            var rowGrid = new Grid { ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto,Auto"), ColumnSpacing = 8 };

            var badge = GradeBadge(entry.Grade, compact: true);
            badge.VerticalAlignment = VerticalAlignment.Center;
            rowGrid.Children.Add(badge);

            var titleStack = new StackPanel { Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
            var jumpBtn = new Button
            {
                Content = $"{entry.DateLabelZh} · {entry.ShortNameZhCn}",
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                Padding = new Thickness(0),
                MinHeight = 0,
                Height = 20,
                FontSize = 12.5,
                FontWeight = FontWeight.SemiBold,
                HorizontalAlignment = HorizontalAlignment.Left,
            };
            var capturedJump = entry;
            jumpBtn.Click += (_, _) => JumpToTurn(capturedJump.LarcOrdinal);
            titleStack.Children.Add(jumpBtn);

            var dirTag = string.IsNullOrEmpty(entry.DirectionZh) ? "" : " " + entry.DirectionZh;
            var metaLine = $"{entry.VenueZh} {entry.SurfaceZh}{entry.DistanceM}米（{entry.DistanceCategoryZh}）{dirTag}";
            if (isStreak) metaLine += "  ·  ⚠ 三连战";
            var metaTb = new TextBlock
            {
                Text = metaLine,
                FontSize = 11,
                Opacity = isStreak ? 0.96 : 0.78,
                TextTrimming = TextTrimming.CharacterEllipsis,
            };
            if (isStreak)
                metaTb.Foreground = StreakWarnBrush;
            titleStack.Children.Add(metaTb);
            Grid.SetColumn(titleStack, 1);
            rowGrid.Children.Add(titleStack);

            var chipStyleCombo = new ComboBox
            {
                Name = prefix + "ChipStyle_" + SanitizeId(entry.RaceId),
                Tag = entry.RaceId,
                ItemsSource = PerRaceRunningStyles.Select(p => "跑法·" + p.CompactLabel).ToArray(),
                SelectedIndex = styleIdx,
                MinWidth = 90,
                Height = 28,
                MinHeight = 0,
                Padding = new Thickness(6, 2),
                FontSize = 11.5,
                VerticalAlignment = VerticalAlignment.Center,
            };
            var captured = entry;
            chipStyleCombo.SelectionChanged += (_, _) =>
            {
                if (suppressEvents || chipStyleCombo.SelectedIndex < 0 || chipStyleCombo.SelectedIndex >= PerRaceRunningStyles.Length)
                    return;
                var picked = PerRaceRunningStyles[chipStyleCombo.SelectedIndex].Id;
                SetPerRaceRunningStyle(captured.RaceId, picked, captured.ShortNameZhCn);
            };
            Grid.SetColumn(chipStyleCombo, 2);
            rowGrid.Children.Add(chipStyleCombo);

            var removeBtn = new Button
            {
                Name = prefix + "RemoveChip_" + SanitizeId(entry.RaceId),
                Tag = entry.RaceId,
                Content = "✕",
                Padding = new Thickness(7, 2),
                MinHeight = 0,
                Height = 28,
                FontSize = 11.5,
                VerticalAlignment = VerticalAlignment.Center,
            };
            removeBtn.Click += (_, _) => ToggleOrReplaceRace(captured, jumpCalendar: false);
            ToolTip.SetTip(removeBtn, "移除 " + entry.DisplayNameZh);
            Grid.SetColumn(removeBtn, 3);
            rowGrid.Children.Add(removeBtn);

            myPlanListPanel.Children.Add(new Border
            {
                Background = CardSurfaceBg,
                BorderBrush = CardSurfaceBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(7),
                Padding = new Thickness(9, 6),
                Child = rowGrid,
            });
        }
    }

    private void RefreshRaceList()
    {
        if (raceListPanel == null || filterSummaryText == null)
            return;

        var query = (searchBox?.Text ?? "").Trim().ToLowerInvariant();
        var yearIdx = yearFilter?.SelectedIndex ?? 0;
        var gradeIdx = gradeFilter?.SelectedIndex ?? 0;
        var surfaceIdx = surfaceFilter?.SelectedIndex ?? 0;
        var distIdx = distanceFilter?.SelectedIndex ?? 0;
        var includeConflicts = showConflictsCheck?.IsChecked == true;

        var selectedSet = new HashSet<string>(selectedRaceIds, StringComparer.Ordinal);
        var selectedByTurn = new Dictionary<int, UmaCustomRaceEntry>();
        foreach (var id in selectedRaceIds)
        {
            if (byId.TryGetValue(id, out var sel))
                selectedByTurn[sel.LarcOrdinal] = sel;
        }

        var matches = catalog.Where(e =>
        {
            if (!includeConflicts && !e.LarcSelectable) return false;
            if (yearIdx is >= 1 and <= 3 && e.YearIndex != yearIdx) return false;
            if (gradeIdx == 1 && e.Grade != "G1") return false;
            if (gradeIdx == 2 && e.Grade != "G2") return false;
            if (gradeIdx == 3 && e.Grade != "G3") return false;
            if (surfaceIdx == 1 && e.Surface != "turf") return false;
            if (surfaceIdx == 2 && e.Surface != "dirt") return false;
            if (distIdx == 1 && e.DistanceCategory != "sprint") return false;
            if (distIdx == 2 && e.DistanceCategory != "mile") return false;
            if (distIdx == 3 && e.DistanceCategory != "medium") return false;
            if (distIdx == 4 && e.DistanceCategory != "long") return false;
            if (query.Length > 0)
            {
                foreach (var token in query.Split(' ', StringSplitOptions.RemoveEmptyEntries))
                {
                    if (!e.SearchHaystack.Contains(token, StringComparison.Ordinal))
                        return false;
                }
            }
            return true;
        }).ToArray();

        var selectableMatchCount = matches.Count(m => m.LarcSelectable);
        filterSummaryText.Text = includeConflicts
            ? $"共 {matches.Length} 场（其中 {selectableMatchCount} 场可在凯旋门加赛）"
            : $"共 {matches.Length} 场可选重赏（点击「+ 排入」同步定位左侧日历）";

        raceListPanel.Children.Clear();
        if (matches.Length == 0)
        {
            var empty = Note("没有找到符合筛选条件的赛事，可清空关键词或调整筛选条件。");
            empty.Margin = new Thickness(6, 10);
            raceListPanel.Children.Add(empty);
            return;
        }

        foreach (var entry in matches)
            raceListPanel.Children.Add(BuildInGameRaceCard(entry, selectedSet, selectedByTurn, jumpCalendarOnPick: true, showDateLine: true));
    }

    private Control BuildInGameRaceCard(
        UmaCustomRaceEntry entry,
        HashSet<string> selectedSet,
        Dictionary<int, UmaCustomRaceEntry> selectedByTurn,
        bool jumpCalendarOnPick,
        bool showDateLine = false)
    {
        var isSelected = selectedSet.Contains(entry.RaceId);
        selectedByTurn.TryGetValue(entry.LarcOrdinal, out var sameTurnOther);
        var hasSameTurnOther = !isSelected && sameTurnOther != null;

        var rowGrid = new Grid
        {
            ColumnDefinitions = new ColumnDefinitions("Auto,*,Auto"),
            ColumnSpacing = 9,
        };

        // 1. Left Race Banner (matching Screenshot 4: Grade gradient card + crisp race name)
        var bannerBg = entry.Grade switch
        {
            "G1" => G1BannerBrush,
            "G2" => G2BannerBrush,
            _ => G3BannerBrush,
        };
        var leftBanner = new Border
        {
            Width = 86,
            MinHeight = 48,
            CornerRadius = new CornerRadius(6),
            Background = bannerBg,
            Opacity = entry.LarcSelectable ? 1.0 : 0.55,
            Padding = new Thickness(5, 4),
            VerticalAlignment = VerticalAlignment.Center,
            Child = new TextBlock
            {
                Text = entry.ShortNameZhCn,
                FontSize = entry.ShortNameZhCn.Length >= 8 ? 10.5 : 11.5,
                FontWeight = FontWeight.Bold,
                Foreground = Brushes.White,
                TextWrapping = TextWrapping.Wrap,
                TextAlignment = TextAlignment.Center,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            },
        };
        rowGrid.Children.Add(leftBanner);

        // 2. Middle Info (Grade Pill + Track/Dist/Direction + Fans + Surface/Dist Pills)
        var infoStack = new StackPanel { Spacing = 4, VerticalAlignment = VerticalAlignment.Center };

        var topLine = new WrapPanel { VerticalAlignment = VerticalAlignment.Center };
        var gradePill = GradeBadge(entry.Grade, compact: true);
        gradePill.Margin = new Thickness(0, 0, 6, 1);
        gradePill.VerticalAlignment = VerticalAlignment.Center;
        topLine.Children.Add(gradePill);

        var dirSuffix = string.IsNullOrEmpty(entry.DirectionZh) ? "" : " " + entry.DirectionZh;
        var trackText = $"{entry.VenueZh} {entry.SurfaceZh} {entry.DistanceM}米（{entry.DistanceCategoryZh}）{dirSuffix}";
        topLine.Children.Add(new TextBlock
        {
            Text = trackText,
            FontSize = 11.5,
            FontWeight = FontWeight.SemiBold,
            Opacity = entry.LarcSelectable ? 0.96 : 0.62,
            VerticalAlignment = VerticalAlignment.Center,
        });
        infoStack.Children.Add(topLine);

        var bottomRow = new WrapPanel { VerticalAlignment = VerticalAlignment.Center };
        if (showDateLine)
        {
            bottomRow.Children.Add(new TextBlock
            {
                Text = entry.DateLabelZh + "  ·  ",
                FontSize = 11,
                FontWeight = FontWeight.SemiBold,
                Foreground = UmaGreenBrush,
                VerticalAlignment = VerticalAlignment.Center,
            });
        }
        bottomRow.Children.Add(new TextBlock
        {
            Text = $"粉丝 ≥ {entry.FanRequirement:N0} 人",
            FontSize = 11,
            Opacity = 0.82,
            Margin = new Thickness(0, 0, 8, 0),
            VerticalAlignment = VerticalAlignment.Center,
        });
        bottomRow.Children.Add(MiniAptitudeTag(entry.SurfaceZh, entry.Surface == "turf"));
        bottomRow.Children.Add(MiniAptitudeTag(entry.DistanceCategoryZh, true));
        infoStack.Children.Add(bottomRow);

        if (!entry.LarcSelectable && !string.IsNullOrWhiteSpace(entry.LarcConflictReason))
        {
            infoStack.Children.Add(new TextBlock
            {
                Text = "不可选：" + entry.LarcConflictReason,
                FontSize = 10.5,
                Foreground = ConflictWarningBrush,
                TextWrapping = TextWrapping.Wrap,
            });
        }
        else if (hasSameTurnOther && sameTurnOther != null)
        {
            infoStack.Children.Add(new TextBlock
            {
                Text = $"同回合已排：{sameTurnOther.ShortNameZhCn}（点击可换选）",
                FontSize = 10.5,
                Opacity = 0.78,
                TextWrapping = TextWrapping.Wrap,
            });
        }

        Grid.SetColumn(infoStack, 1);
        rowGrid.Children.Add(infoStack);

        // 3. Right Action Button + Optional Per-Race Style Selector
        var actionCol = new StackPanel
        {
            Spacing = 4,
            VerticalAlignment = VerticalAlignment.Center,
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        var actionBtn = new Button
        {
            Name = prefix + "RaceToggle_" + SanitizeId(entry.RaceId),
            Tag = entry.RaceId,
            MinWidth = 78,
            Height = 28,
            MinHeight = 0,
            Padding = new Thickness(8, 2),
            CornerRadius = new CornerRadius(6),
            FontSize = 11.5,
            FontWeight = isSelected ? FontWeight.SemiBold : FontWeight.Normal,
            VerticalAlignment = VerticalAlignment.Center,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            IsEnabled = entry.LarcSelectable,
            Content = !entry.LarcSelectable
                ? "剧本冲突"
                : isSelected
                    ? "✓ 已排参赛"
                    : hasSameTurnOther
                        ? "⇄ 换选本场"
                        : "＋ 排入",
        };
        if (isSelected)
        {
            actionBtn.Background = UmaGreenDeepBrush;
            actionBtn.Foreground = Brushes.White;
        }
        var captured = entry;
        actionBtn.Click += (_, _) => ToggleOrReplaceRace(captured, jumpCalendar: jumpCalendarOnPick);
        actionCol.Children.Add(actionBtn);

        if (isSelected)
        {
            var hasOverride = raceRunningStyles.TryGetValue(entry.RaceId, out var curOv);
            var curStyleId = hasOverride && curOv != null ? curOv : "inherit";
            var styleIdx = Math.Max(0, Array.FindIndex(PerRaceRunningStyles, p => p.Id == curStyleId));
            var turnStyleCombo = new ComboBox
            {
                Name = prefix + "TurnRaceStyle_" + SanitizeId(entry.RaceId),
                Tag = entry.RaceId,
                ItemsSource = PerRaceRunningStyles.Select(p => "跑法·" + p.CompactLabel).ToArray(),
                SelectedIndex = styleIdx,
                MinWidth = 86,
                Height = 29,
                MinHeight = 0,
                Padding = new Thickness(6, 2),
                FontSize = 11.5,
            };
            turnStyleCombo.SelectionChanged += (_, _) =>
            {
                if (suppressEvents || turnStyleCombo.SelectedIndex < 0 || turnStyleCombo.SelectedIndex >= PerRaceRunningStyles.Length)
                    return;
                var picked = PerRaceRunningStyles[turnStyleCombo.SelectedIndex].Id;
                SetPerRaceRunningStyle(captured.RaceId, picked, captured.ShortNameZhCn);
            };
            ToolTip.SetTip(turnStyleCombo, "单独设置本场跑法");
            actionCol.Children.Add(turnStyleCombo);
        }

        Grid.SetColumn(actionCol, 2);
        rowGrid.Children.Add(actionCol);

        return new Border
        {
            Background = !entry.LarcSelectable ? SlotMutedBg : isSelected ? SelectedCardBg : CardSurfaceBg,
            BorderBrush = isSelected ? SelectedCardBorder : CardSurfaceBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(8, 6),
            Child = rowGrid,
        };
    }

    private Button RightModeTabButton(string name, string label, int targetMode)
    {
        var btn = new Button
        {
            Name = name,
            Content = label,
            Height = 28,
            MinHeight = 0,
            Padding = new Thickness(6, 0),
            Margin = new Thickness(2, 0),
            CornerRadius = new CornerRadius(7),
            FontSize = 11.5,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            VerticalContentAlignment = VerticalAlignment.Center,
        };
        btn.Click += (_, _) =>
        {
            rightMode = targetMode;
            RefreshCalendar();
            RefreshRightPane();
        };
        return btn;
    }

    private static Button NavButton(string name, string symbol, string tip)
    {
        var btn = new Button
        {
            Name = name,
            Content = symbol,
            Width = 28,
            Height = 28,
            MinWidth = 0,
            MinHeight = 0,
            Padding = new Thickness(0),
            CornerRadius = new CornerRadius(14),
            FontSize = 13,
            FontWeight = FontWeight.Bold,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            VerticalContentAlignment = VerticalAlignment.Center,
        };
        ToolTip.SetTip(btn, tip);
        return btn;
    }

    private static ComboBox FilterCombo(string name, string[] items, double width) => new()
    {
        Name = name,
        ItemsSource = items,
        SelectedIndex = 0,
        MinWidth = width,
        Height = 28,
        MinHeight = 0,
        FontSize = 11.5,
        Margin = new Thickness(0, 0, 6, 4),
    };

    private static Border GradeBadge(string grade, bool compact) => new()
    {
        Background = GradePillBrush(grade),
        CornerRadius = new CornerRadius(9),
        MinWidth = compact ? 30 : 36,
        Padding = compact ? new Thickness(5, 1) : new Thickness(7, 2),
        Child = new TextBlock
        {
            Text = DisplayGrade(grade),
            FontSize = compact ? 10 : 11,
            FontWeight = FontWeight.Bold,
            Foreground = Brushes.White,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
        },
    };

    private static Control MiniAptitudeTag(string label, bool showStar)
    {
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 2, VerticalAlignment = VerticalAlignment.Center };
        if (showStar)
            row.Children.Add(new TextBlock { Text = "★", FontSize = 9.5, Foreground = StarTint, VerticalAlignment = VerticalAlignment.Center });
        row.Children.Add(new TextBlock { Text = label, FontSize = 10, Opacity = 0.88, VerticalAlignment = VerticalAlignment.Center });
        return new Border
        {
            Background = TagBg,
            BorderBrush = TagBorder,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(5, 1),
            Margin = new Thickness(0, 0, 4, 0),
            Child = row,
        };
    }

    private static IBrush GradePillBrush(string grade) => grade switch
    {
        "G1" => G1PillBrush,
        "G2" => G2PillBrush,
        _ => G3PillBrush,
    };

    private static string DisplayGrade(string grade) => grade switch
    {
        "G1" => "GI",
        "G2" => "GII",
        "G3" => "GIII",
        _ => grade,
    };

    private static string CompactCalendarTitle(UmaCustomRaceEntry entry)
    {
        var raw = !string.IsNullOrWhiteSpace(entry.ShortNameZhCn) ? entry.ShortNameZhCn : entry.NameZhCn;
        if (raw.EndsWith("锦标赛", StringComparison.Ordinal) && raw.Length > 7)
            return raw[..^3] + "S";
        return raw;
    }

    private static string ShortChipTitle(UmaCustomRaceEntry entry)
    {
        if (!string.IsNullOrWhiteSpace(entry.PrimaryAliasZh) && entry.PrimaryAliasZh.Length <= 8)
            return entry.PrimaryAliasZh;
        return !string.IsNullOrWhiteSpace(entry.ShortNameZhCn) ? entry.ShortNameZhCn : entry.NameZhCn;
    }

    private static string ShortStyleChoiceLabel((string Id, string Label, string CompactLabel) choice) =>
        choice.Id == "inherit" ? "默认" : choice.Label;

    private static string YearTabName(int yearIndex) => yearIndex switch
    {
        1 => "初级",
        2 => "经典级",
        _ => "高级",
    };

    private static int YearFromOrdinal(int ordinal) =>
        ordinal < 24 ? 1 : ordinal < 48 ? 2 : 3;

    private static int GradeRank(string grade) => grade switch
    {
        "G1" => 0,
        "G2" => 1,
        _ => 2,
    };

    private static LinearGradientBrush HorizontalGrad(string startHex, string endHex) => new()
    {
        StartPoint = new RelativePoint(0, 0, RelativeUnit.Relative),
        EndPoint = new RelativePoint(1, 0.2, RelativeUnit.Relative),
        GradientStops = new GradientStops
        {
            new GradientStop(Color.Parse(startHex), 0),
            new GradientStop(Color.Parse(endHex), 1),
        },
    };

    private void SetStatus(string text)
    {
        statusNote.Text = text;
        statusNote.IsVisible = !string.IsNullOrWhiteSpace(text);
    }

    private static string SanitizeId(string raceId) => raceId.Replace('.', '_').Replace('-', '_');

    private static TextBlock Note(string text) => new()
    {
        Text = text,
        TextWrapping = TextWrapping.Wrap,
        FontSize = 12,
    };
}
