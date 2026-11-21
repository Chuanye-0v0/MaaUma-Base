using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using SukiUI.Controls;

namespace UmaCutPanel;

// Grouped 99/1 desktop IA around Suki controls + right-hand purity slide-over drawer.
internal sealed class PacerCenterLayout : Grid
{
    public SukiSideMenu Navigation { get; }
    public IReadOnlyList<SukiSideMenuItem> Pages { get; }

    public PacerCenterLayout(Control connection, Control status, UmaPacerView runtime,
        EventPreferenceEditor preferences, Control review, Control updates, Control? exchange,
        Control? parentLibrary = null, bool showConnectionBar = true)
    {
        Name = "PacerCenterRoot";
        this.Bind(BackgroundProperty, new DynamicResourceExtension("SukiStrongBackground"));
        ColumnDefinitions = new("Auto,*");

        var settings = runtime.SeparateSettingsPages();
        runtime.BindEventPreferencesEditor(preferences);
        if (exchange is ScoringExchangeView scoringExchange)
            runtime.BindExchangeView(scoringExchange);

        var libView = runtime.WorkflowLibraryView;

        // Top utility + SelectorBar + Toast + PageContent
        var workspace = new Grid { RowDefinitions = new("Auto,Auto,Auto,*") };

        var connectionExpander = new Expander
        {
            Name = "PacerConnection",
            Header = "模拟器连接与来源",
            IsExpanded = false,
            IsVisible = showConnectionBar,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            Content = new StackPanel { Children = { connection, status } }
        };
        var connectionCard = new GlassCard
        {
            Margin = new Thickness(16, 10, 16, 0),
            Padding = new Thickness(10, 2),
            IsAnimated = false,
            IsOpaque = true,
            CornerRadius = new CornerRadius(10),
            IsVisible = showConnectionBar,
            Content = connectionExpander
        };
        workspace.Children.Add(connectionCard);

        var selectorBar = new Border
        {
            Name = "PacerSelectorWrapper",
            Padding = new Thickness(16, 10, 16, 0),
            Child = settings.Presets
        };
        Grid.SetRow(selectorBar, 1);
        workspace.Children.Add(selectorBar);

        // Inline toast banner with 1-step Purify Undo
        var toastText = new TextBlock { Name = "PacerToastText", FontSize = 12, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
        var toastUndoBtn = new Button { Name = "PacerToastUndo", Content = "↶ 撤销", Height = 26, MinHeight = 0, Padding = new Thickness(10, 0), FontSize = 12, FontWeight = FontWeight.SemiBold, Margin = new Thickness(8, 0, 0, 0) };
        var toastCloseBtn = new Button { Name = "PacerToastClose", Content = "知道了", Height = 26, MinHeight = 0, Padding = new Thickness(10, 0), FontSize = 12, Margin = new Thickness(6, 0, 0, 0) };
        var toastActions = new StackPanel { Orientation = Orientation.Horizontal, Children = { toastUndoBtn, toastCloseBtn } };
        var toastGrid = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 10 };
        toastGrid.Children.Add(toastText);
        Grid.SetColumn(toastActions, 1);
        toastGrid.Children.Add(toastActions);
        var toastBanner = new Border
        {
            Name = "PacerToastBanner",
            IsVisible = false,
            Margin = new Thickness(16, 8, 16, 0),
            Padding = new Thickness(12, 8),
            CornerRadius = new CornerRadius(10),
            BorderThickness = new Thickness(1),
            Child = toastGrid
        };
        Grid.SetRow(toastBanner, 2);
        workspace.Children.Add(toastBanner);

        void ShowToast(string message, bool canUndo, string kind)
        {
            toastText.Text = message;
            toastUndoBtn.IsVisible = canUndo;
            if (kind == "ok")
            {
                toastBanner.BorderBrush = new SolidColorBrush(Color.FromRgb(22, 163, 74));
                toastBanner.Background = new SolidColorBrush(Color.FromArgb(24, 34, 197, 94));
            }
            else if (kind == "warn")
            {
                toastBanner.BorderBrush = new SolidColorBrush(Color.FromRgb(217, 119, 6));
                toastBanner.Background = new SolidColorBrush(Color.FromArgb(24, 245, 158, 11));
            }
            else
            {
                toastBanner.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
                toastBanner.Background = new SolidColorBrush(Color.FromArgb(16, 100, 116, 139));
            }
            toastBanner.IsVisible = true;
        }
        toastUndoBtn.Click += (_, _) => { toastBanner.IsVisible = false; libView?.UndoPurifyDraft(); };
        toastCloseBtn.Click += (_, _) => toastBanner.IsVisible = false;
        if (libView != null) libView.ToastRequested += ShowToast;

        var content = new ContentControl
        {
            Name = "PacerCenterContent",
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
            ClipToBounds = true
        };
        Grid.SetRow(content, 3);
        workspace.Children.Add(content);

        // Build unified Configuration Hub: (1) 马娘与卡组 | (2) 策略与五维目标 | (3) 连开与毕业 | (4) 赛程/技能/事件
        preferences.IsExpanded = true;
        var eventEditors = new EventSettingsTabs(preferences, settings.MidSkills, runtime.WorkflowCustomRacesPage);
        var configDeckPage = runtime.WorkflowDeckPage is { } deckPg
            ? Settings(deckPg, "PacerDeckScroll")
            : new Border();
        var configStrategyPage = runtime.WorkflowStrategyTargetsPage is { } stratPg
            ? Settings(stratPg, "PacerStrategyTargetsScroll")
            : new Border();
        var configCyclePage = runtime.WorkflowCyclePage is { } cycPg
            ? Settings(cycPg, "PacerCycleScroll")
            : new Border();
        var (configHub, selectConfigTab) = BuildSubHub(
            "PacerConfigurationHub",
            "PacerConfigTabBar",
            ("PacerConfigTabDeck", "① 马娘与卡组", configDeckPage, null, null),
            ("PacerConfigTabStrategy", "② 策略与五维目标", configStrategyPage, null, null),
            ("PacerConfigTabCycle", "③ 育成模式与条件", configCyclePage, null, null),
            ("PacerConfigTabRacesEvents", "④ 赛程 / 技能 / 事件", eventEditors, null, null)
        );

        // Build Advanced Settings Hub (1% 极客与炼丹): 评分权重与基础倍率 | 分年阶段策略 | 流程编排 | AI 决策 | 权重交换站
        var workflowSettings = Settings(settings.Weights);
        var aiSettings = Settings(settings.AI);
        Control? exchangeCard = exchange == null
            ? null
            : new GlassCard
            {
                Content = exchange,
                Margin = new Thickness(18),
                Padding = new Thickness(8),
                MaxWidth = 1120,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                VerticalAlignment = VerticalAlignment.Stretch,
                IsAnimated = false,
                IsOpaque = true,
                CornerRadius = new CornerRadius(14)
            };
        var strategyTabs = new List<(string Name, string Label, Control Page, Control? Visibility, Action? OnActivate)>
        {
            ("PacerStrategyTabWeights", "评分权重与基础倍率", workflowSettings, settings.Weights, () => runtime.SetCustomWeightsPage(true)),
            ("PacerStrategyTabFlow", "分年阶段策略", workflowSettings, settings.Weights, () =>
            {
                runtime.SetCustomWeightsPage(false);
                runtime.WorkflowLibraryView?.OpenStrategyTimelineTab();
            }),
            ("PacerStrategyTabTopology", "流程编排", workflowSettings, settings.Weights, () =>
            {
                runtime.SetCustomWeightsPage(false);
                runtime.WorkflowLibraryView?.OpenAdvancedFlowTab();
            }),
            ("PacerStrategyTabAi", "AI 决策", aiSettings, settings.AI, null),
        };
        if (exchangeCard != null)
            strategyTabs.Add(("PacerStrategyTabExchange", "⇄ 权重交换站", exchangeCard, null, null));
        var (strategyHub, selectStrategyTab) = BuildSubHub(
            "PacerStrategyHub",
            "PacerStrategyTabBar",
            strategyTabs.ToArray()
        );

        // Compatibility Navigation & Pages collection preserved for host bindings/tests
        var pages = new List<SukiSideMenuItem>();
        SukiSideMenuItem MakeCompatItem(string title, string icon, Control page)
        {
            var item = new SukiSideMenuItem
            {
                Header = title,
                Icon = new TextBlock { Text = icon, FontSize = 18 },
                PageContent = new Border(),
                IsContentMovable = false
            };
            pages.Add(item);
            return item;
        }
        var overviewItem = MakeCompatItem("运行总览", "▶", runtime);
        var configItem = MakeCompatItem("育成配置", "☷", configHub);
        var reviewItem = MakeCompatItem("复盘与反馈", "▤", review);
        var parentLibraryItem = parentLibrary != null ? MakeCompatItem("种马库", "▦", parentLibrary) : null;
        var strategyItem = MakeCompatItem("高级设置", "◇", strategyHub);
        Pages = pages;
        Navigation = new SukiSideMenu
        {
            Name = "PacerCenterNavigation",
            ItemsSource = pages,
            SelectedItem = overviewItem,
            IsContentVisible = false,
            IsVisible = false
        };
        var pageContents = new Dictionary<SukiSideMenuItem, Control>
        {
            [overviewItem] = runtime,
            [configItem] = configHub,
            [reviewItem] = review,
            [strategyItem] = strategyHub
        };
        if (parentLibraryItem != null) pageContents[parentLibraryItem] = parentLibrary!;

        // Custom Grouped Sidebar matching prototype layout
        var sidebarExpanded = true;
        var advMiniBadgeBorder = new Border
        {
            Name = "SidebarAdvMiniBadge",
            IsVisible = false,
            CornerRadius = new CornerRadius(5),
            Padding = new Thickness(6, 2),
            VerticalAlignment = VerticalAlignment.Center
        };
        var advMiniBadgeText = new TextBlock { FontSize = 11, FontWeight = FontWeight.SemiBold };
        advMiniBadgeBorder.Child = advMiniBadgeText;

        var navButtons = new Dictionary<SukiSideMenuItem, (Button Btn, TextBlock Label)>();
        Button MakeNavButton(SukiSideMenuItem item, string icon, string label, Border? badge = null)
        {
            var iconTxt = new TextBlock { Text = icon, FontSize = 15, Width = 22, TextAlignment = TextAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            var labelTxt = new TextBlock { Text = label, FontSize = 13, VerticalAlignment = VerticalAlignment.Center };
            var row = new Grid { ColumnDefinitions = new("Auto,*,Auto"), ColumnSpacing = 8 };
            row.Children.Add(iconTxt);
            Grid.SetColumn(labelTxt, 1); row.Children.Add(labelTxt);
            if (badge != null) { Grid.SetColumn(badge, 2); row.Children.Add(badge); }
            var btn = new Button
            {
                Name = "Nav_" + label,
                Content = row,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Height = 36,
                Padding = new Thickness(10, 0),
                CornerRadius = new CornerRadius(8),
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0)
            };
            ToolTip.SetTip(btn, label);
            navButtons[item] = (btn, labelTxt);
            return btn;
        }

        var btnOverview = MakeNavButton(overviewItem, "▶", "运行总览");
        var btnConfig = MakeNavButton(configItem, "☷", "育成配置");
        var btnReview = MakeNavButton(reviewItem, "▤", "复盘与反馈");
        Button? btnParentLib = parentLibraryItem != null ? MakeNavButton(parentLibraryItem, "▦", "种马库") : null;
        var btnAdvanced = MakeNavButton(strategyItem, "◇", "高级设置", advMiniBadgeBorder);

        var commonHeader = new TextBlock { Text = "常用（99% 日常路径）", FontSize = 11, FontWeight = FontWeight.SemiBold, Margin = new Thickness(8, 2, 0, 4) };
        commonHeader.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var advHeader = new TextBlock { Text = "进阶（1% 炼丹与编排）", FontSize = 11, FontWeight = FontWeight.SemiBold, Margin = new Thickness(8, 4, 0, 4) };
        advHeader.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var divider = new Border { Height = 1, Margin = new Thickness(4, 6) };
        divider.Bind(Border.BackgroundProperty, new DynamicResourceExtension("SukiControlBorderBrush"));

        var headerTitle = new TextBlock { Text = "MaaUma", FontSize = 21, FontWeight = FontWeight.Bold };
        var headerSub = new TextBlock { Text = "育成中心", FontSize = 12 };
        headerSub.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var collapseBtn = new Button
        {
            Name = "PacerSidebarToggle",
            Content = "◀",
            Width = 26,
            Height = 26,
            MinHeight = 0,
            Padding = new Thickness(0),
            FontSize = 11,
            VerticalAlignment = VerticalAlignment.Top,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            VerticalContentAlignment = VerticalAlignment.Center
        };
        var titleBlock = new StackPanel { Spacing = 2, Children = { headerTitle, headerSub } };
        var topHead = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 4, Margin = new Thickness(6, 4, 2, 10) };
        topHead.Children.Add(titleBlock);
        Grid.SetColumn(collapseBtn, 1);
        topHead.Children.Add(collapseBtn);

        var navStack = new StackPanel { Spacing = 4 };
        navStack.Children.Add(topHead);
        navStack.Children.Add(commonHeader);
        navStack.Children.Add(btnOverview);
        navStack.Children.Add(btnConfig);
        navStack.Children.Add(btnReview);
        if (btnParentLib != null) navStack.Children.Add(btnParentLib);
        navStack.Children.Add(divider);
        navStack.Children.Add(advHeader);
        navStack.Children.Add(btnAdvanced);

        var sidebarEquipment = new TextBlock { Name = "SidebarEquipmentSummary", FontSize = 11, TextWrapping = TextWrapping.Wrap, MaxLines = 3 };
        sidebarEquipment.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var sidebarRunBtn = new Button
        {
            Name = "SidebarRunBtn",
            Content = "▶ 开始运行",
            Height = 36,
            Padding = new Thickness(8, 0),
            FontSize = 12.5,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            VerticalContentAlignment = VerticalAlignment.Center,
            CornerRadius = new CornerRadius(8),
            FontWeight = FontWeight.SemiBold,
            IsEnabled = false
        };
        var gettingStartedBtn = new Button
        {
            Name = "PacerGettingStartedTopBtn",
            Content = "? 新手指南",
            Height = 30,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            HorizontalContentAlignment = HorizontalAlignment.Center,
            VerticalContentAlignment = VerticalAlignment.Center,
            CornerRadius = new CornerRadius(8),
            FontSize = 12
        };

        var footerSep = new Border { Height = 1, Margin = new Thickness(0, 6, 0, 8) };
        footerSep.Bind(Border.BackgroundProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
        var footerStack = new StackPanel
        {
            Spacing = 8,
            Children = { footerSep, sidebarEquipment, sidebarRunBtn, gettingStartedBtn }
        };

        var sidebarGrid = new Grid { RowDefinitions = new("*,Auto") };
        var navScroll = new ScrollViewer
        {
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            Content = navStack
        };
        sidebarGrid.Children.Add(navScroll);
        Grid.SetRow(footerStack, 1);
        sidebarGrid.Children.Add(footerStack);

        var sidebarContainer = new Border
        {
            Name = "PacerGroupedSidebar",
            Width = 218,
            BorderThickness = new Thickness(0, 0, 1, 0),
            Padding = new Thickness(10, 14, 10, 12),
            Child = sidebarGrid
        };
        sidebarContainer.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
        Children.Add(sidebarContainer);

        void ApplySidebarCollapsed(bool expanded)
        {
            sidebarExpanded = expanded;
            sidebarContainer.Width = expanded ? 218 : 60;
            Navigation.IsMenuExpanded = expanded;
            titleBlock.IsVisible = expanded;
            commonHeader.IsVisible = expanded;
            advHeader.IsVisible = expanded;
            sidebarEquipment.IsVisible = expanded;
            advMiniBadgeBorder.IsVisible = expanded && advMiniBadgeText.Text?.Length > 0;
            collapseBtn.Content = expanded ? "◀" : "▶";
            sidebarRunBtn.Content = expanded ? (sidebarRunBtn.Tag as string ?? "▶ 开始运行") : "▶";
            gettingStartedBtn.Content = expanded ? "? 新手指南" : "?";
            foreach (var pair in navButtons.Values)
                pair.Label.IsVisible = expanded;
        }
        collapseBtn.Click += (_, _) => ApplySidebarCollapsed(!sidebarExpanded);

        var showingGettingStarted = false;
        MaaUmaPacerGettingStartedView? gettingStartedView = null;

        void UpdateSidebarVisuals()
        {
            var sel = Navigation.SelectedItem as SukiSideMenuItem;
            foreach (var (item, pair) in navButtons)
            {
                var active = !showingGettingStarted && ReferenceEquals(item, sel);
                if (active)
                {
                    if (!pair.Btn.Classes.Contains("Accent")) pair.Btn.Classes.Add("Accent");
                    pair.Label.FontWeight = FontWeight.SemiBold;
                    pair.Btn.ClearValue(Button.BackgroundProperty);
                }
                else
                {
                    pair.Btn.Classes.Remove("Accent");
                    pair.Label.FontWeight = FontWeight.Normal;
                    pair.Btn.Background = Brushes.Transparent;
                }
            }
        }

        void UpdateSidebarSummary()
        {
            if (libView == null) return;
            var ov = libView.ReadOverviewSummary();
            sidebarEquipment.Text = $"{ov.ModeProfile}\n{ov.EquipmentBatch}";
            if (ov.State == PurityState.Stale)
            {
                advMiniBadgeBorder.Background = new SolidColorBrush(Color.Parse("#FEE2E2"));
                advMiniBadgeText.Foreground = new SolidColorBrush(Color.Parse("#DC2626"));
                advMiniBadgeText.Text = "残留";
                advMiniBadgeBorder.IsVisible = sidebarExpanded;
            }
            else if (ov.State == PurityState.Custom)
            {
                advMiniBadgeBorder.Background = new SolidColorBrush(Color.Parse("#FFEDD5"));
                advMiniBadgeText.Foreground = new SolidColorBrush(Color.Parse("#D97706"));
                advMiniBadgeText.Text = "定制";
                advMiniBadgeBorder.IsVisible = sidebarExpanded;
            }
            else
            {
                advMiniBadgeText.Text = "";
                advMiniBadgeBorder.IsVisible = false;
            }
        }
        if (libView != null)
        {
            libView.SelectionChanged += UpdateSidebarSummary;
            libView.PurityReportsChanged += _ => UpdateSidebarSummary();
            UpdateSidebarSummary();
        }

        runtime.PrimaryRunStateChanged += (caption, canStart, _) =>
        {
            var label = "▶ " + caption;
            sidebarRunBtn.Tag = label;
            sidebarRunBtn.Content = sidebarExpanded ? label : "▶";
            sidebarRunBtn.IsEnabled = canStart;
            if (canStart) { if (!sidebarRunBtn.Classes.Contains("Accent")) sidebarRunBtn.Classes.Add("Accent"); }
            else sidebarRunBtn.Classes.Remove("Accent");
        };

        void UpdateTopBarState()
        {
            if (showingGettingStarted)
            {
                selectorBar.IsVisible = false;
                if (!gettingStartedBtn.Classes.Contains("Accent"))
                    gettingStartedBtn.Classes.Add("Accent");
                gettingStartedBtn.FontWeight = FontWeight.SemiBold;
                return;
            }
            gettingStartedBtn.Classes.Remove("Accent");
            gettingStartedBtn.FontWeight = FontWeight.Normal;
            var sel = Navigation.SelectedItem;
            selectorBar.IsVisible = !ReferenceEquals(sel, reviewItem) && (parentLibraryItem == null || !ReferenceEquals(sel, parentLibraryItem));
        }

        void Select()
        {
            if (Navigation.SelectedItem is not SukiSideMenuItem selected) return;
            showingGettingStarted = false;
            if (ReferenceEquals(selected, overviewItem)) runtime.RefreshOverviewSettings();
            if (ReferenceEquals(selected, strategyItem)) selectStrategyTab(-1);
            content.Content = pageContents[selected];
            UpdateTopBarState();
            UpdateSidebarVisuals();
        }

        void OpenGettingStarted()
        {
            if (showingGettingStarted)
            {
                Select();
                return;
            }
            gettingStartedView ??= new MaaUmaPacerGettingStartedView(() =>
            {
                showingGettingStarted = false;
                Navigation.SelectedItem = overviewItem;
                Select();
            });
            showingGettingStarted = true;
            content.Content = gettingStartedView;
            UpdateTopBarState();
            UpdateSidebarVisuals();
        }

        sidebarRunBtn.Click += async (_, _) =>
        {
            if (!ReferenceEquals(Navigation.SelectedItem, overviewItem) || showingGettingStarted)
            {
                Navigation.SelectedItem = overviewItem;
                Select();
            }
            await runtime.TriggerSidebarPrimaryRunAsync();
        };
        gettingStartedBtn.Click += (_, _) => OpenGettingStarted();

        foreach (var (item, pair) in navButtons)
        {
            var captured = item;
            pair.Btn.Click += (_, _) =>
            {
                showingGettingStarted = false;
                Navigation.SelectedItem = captured;
                Select();
            };
        }

        runtime.OverviewSettingsRequested += () => { Navigation.SelectedItem = overviewItem; Select(); };
        runtime.GettingStartedRequested += OpenGettingStarted;
        runtime.WorkflowConfigurationRequested += () => { Navigation.SelectedItem = configItem; selectConfigTab(0); Select(); };
        runtime.ConfigStrategyRequested += () => { Navigation.SelectedItem = configItem; selectConfigTab(1); Select(); };
        runtime.RaceScheduleSettingsRequested += () =>
        {
            Navigation.SelectedItem = configItem;
            selectConfigTab(3);
            eventEditors.ShowRaces();
            Select();
        };
        runtime.ConfigEventsRequested += () =>
        {
            Navigation.SelectedItem = configItem;
            selectConfigTab(3);
            eventEditors.ShowEvents();
            Select();
        };
        runtime.MidSkillSettingsRequested += () =>
        {
            Navigation.SelectedItem = configItem;
            selectConfigTab(3);
            eventEditors.ShowMidSkills();
            Select();
        };
        runtime.CustomWeightsRequested += () => { Navigation.SelectedItem = strategyItem; selectStrategyTab(0); Select(); };
        if (libView != null)
            libView.CustomWeightsRequested += () => { Navigation.SelectedItem = strategyItem; selectStrategyTab(0); Select(); };

        // Slide-over purity breakdown drawer
        var purityDrawer = new PacerPurityDrawer();
        Grid.SetColumnSpan(purityDrawer, 2);
        purityDrawer.EditWeightsRequested += () => { Navigation.SelectedItem = strategyItem; selectStrategyTab(0); Select(); };
        purityDrawer.PurifyRequested += () => libView?.PurifyCurrentDraft(PurityLayer.All, recordUndo: true);
        if (libView != null)
            libView.PurityDrawerRequested += (sub, reps) => purityDrawer.Open(sub, reps);

        Navigation.PropertyChanged += (_, args) => { if (args.Property == SukiSideMenu.SelectedItemProperty) Select(); };
        Select();

        var workspaceHost = new ContentControl
        {
            Name = "PacerWorkspaceHost",
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
            ClipToBounds = true,
            Content = workspace
        };
        var compactScroller = new ScrollViewer
        {
            Name = "PacerWorkspaceScroller",
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalContentAlignment = HorizontalAlignment.Stretch
        };
        var compact = false;
        var updatingScroll = false;
        void UpdateWorkspaceScrolling()
        {
            if (updatingScroll || Bounds.Height <= 0) return;
            var topH = (connectionCard.IsVisible ? connectionCard.DesiredSize.Height : 0) + (selectorBar.IsVisible ? selectorBar.DesiredSize.Height : 0);
            var avail = Bounds.Height - topH;
            var needsScroll = PacerLayoutGuard.NeedsCompactHeight(avail, compact, 410, 470);
            if (needsScroll == compact) return;
            updatingScroll = true;
            try
            {
                compact = needsScroll;
                workspaceHost.Content = null;
                if (compact)
                {
                    compactScroller.Content = workspace;
                    workspaceHost.Content = compactScroller;
                }
                else
                {
                    compactScroller.Content = null;
                    workspaceHost.Content = workspace;
                }
            }
            finally { updatingScroll = false; }
        }
        Grid.SetColumn(workspaceHost, 1);
        Children.Add(Navigation);
        Children.Add(workspaceHost);
        Children.Add(purityDrawer);

        Navigation.PropertyChanged += (_, args) =>
        {
            if (args.Property == SukiSideMenu.SelectedItemProperty)
                compactScroller.Offset = default;
        };
        connectionCard.SizeChanged += (_, _) => Dispatcher.UIThread.Post(UpdateWorkspaceScrolling, DispatcherPriority.Loaded);
        selectorBar.SizeChanged += (_, _) => Dispatcher.UIThread.Post(UpdateWorkspaceScrolling, DispatcherPriority.Loaded);
        Loaded += (_, _) => runtime.SetSettingsHostVisible(true);
        Unloaded += (_, _) => runtime.SetSettingsHostVisible(false);
        bool? lastWide = null;
        SizeChanged += (_, _) =>
        {
            if (Bounds.Width > 0)
            {
                var narrowSidebar = PacerLayoutGuard.NextNarrow(Bounds.Width, 1020, lastWide.HasValue ? !lastWide.Value : null, 24);
                var wide = !narrowSidebar;
                if (lastWide != wide)
                {
                    lastWide = wide;
                    ApplySidebarCollapsed(wide);
                }
            }
            Dispatcher.UIThread.Post(UpdateWorkspaceScrolling, DispatcherPriority.Loaded);
        };
    }

    private static (Grid Hub, Action<int> SelectTab) BuildSubHub(
        string hubName,
        string barName,
        params (string Name, string Label, Control Page, Control? Visibility, Action? OnActivate)[] specs)
    {
        var hub = new Grid { Name = hubName, RowDefinitions = new("Auto,*"), RowSpacing = 0 };
        var pills = new WrapPanel { Orientation = Orientation.Horizontal };
        var barCard = new GlassCard
        {
            Name = barName,
            Margin = new Thickness(18, 10, 18, 0),
            Padding = new Thickness(8, 6),
            MaxWidth = 1120,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            IsAnimated = false,
            IsOpaque = true,
            CornerRadius = new CornerRadius(12),
            Content = pills
        };
        hub.Children.Add(barCard);

        var bodyHost = new ContentControl
        {
            Name = hubName + "Content",
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch
        };
        Grid.SetRow(bodyHost, 1);
        hub.Children.Add(bodyHost);

        var buttons = new List<Button>();
        var current = 0;

        void Activate(int index)
        {
            if (specs.Length == 0) return;
            if (index < 0 || index >= specs.Length) index = current;
            current = index;
            for (var i = 0; i < buttons.Count; i++)
            {
                var btn = buttons[i];
                var active = i == index;
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
            var spec = specs[index];
            spec.OnActivate?.Invoke();
            if (!ReferenceEquals(bodyHost.Content, spec.Page))
            {
                bodyHost.Content = null;
                bodyHost.Content = spec.Page;
            }
        }

        for (var i = 0; i < specs.Length; i++)
        {
            var idx = i;
            var spec = specs[i];
            var btn = new Button
            {
                Name = spec.Name,
                Content = spec.Label,
                Height = 32,
                Padding = new Thickness(14, 0),
                Margin = new Thickness(0, 0, 6, 0),
                CornerRadius = new CornerRadius(8),
                VerticalContentAlignment = VerticalAlignment.Center
            };
            if (spec.Visibility != null)
            {
                btn.Bind(IsVisibleProperty, spec.Visibility.GetObservable(IsVisibleProperty));
                spec.Visibility.PropertyChanged += (_, args) =>
                {
                    if (args.Property == IsVisibleProperty && !spec.Visibility.IsVisible && current == idx)
                    {
                        var fallback = Enumerable.Range(0, buttons.Count).FirstOrDefault(k => buttons[k].IsVisible, -1);
                        if (fallback >= 0) Activate(fallback);
                    }
                };
            }
            btn.Click += (_, _) => Activate(idx);
            buttons.Add(btn);
            pills.Children.Add(btn);
        }

        Activate(0);
        return (hub, Activate);
    }

    private static Control Settings(Control editor, string? name = null)
    {
        var card = new GlassCard
        {
            Content = editor,
            Margin = new Thickness(18),
            Padding = new Thickness(24),
            MaxWidth = 1080,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Top,
            IsAnimated = false,
            IsOpaque = true,
            CornerRadius = new CornerRadius(14)
        };
        var scroll = new ScrollViewer
        {
            Name = name,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            Content = card
        };
        bool? lastNarrow = null;
        scroll.SizeChanged += (_, args) =>
        {
            if (args.NewSize.Width <= 0) return;
            var narrow = PacerLayoutGuard.NextNarrow(args.NewSize.Width, 620, lastNarrow, 24);
            if (lastNarrow == narrow) return;
            lastNarrow = narrow;
            card.Margin = new Thickness(narrow ? 8 : 18);
            card.Padding = new Thickness(narrow ? 12 : 24);
        };
        return scroll;
    }
}
