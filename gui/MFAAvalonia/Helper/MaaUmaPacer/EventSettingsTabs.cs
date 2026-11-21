using System;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Documents;
using Avalonia.Controls.Primitives;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.LogicalTree;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using SukiUI.Controls;

namespace UmaCutPanel;

/// <summary>Unified configuration workbench tabs for ④ 赛程 / 技能 / 事件.</summary>
public sealed class EventSettingsTabs : UserControl
{
    private readonly TextBlock scope = new() { Name = "EventSettingsScope", FontSize = 13, TextWrapping = TextWrapping.Wrap };
    private readonly Control sharedSave;
    private readonly Button? clearChoices;
    private readonly bool hasMidSkills;
    private readonly bool hasCustomRaces;
    public TabControl Tabs { get; }

    public EventSettingsTabs(EventPreferenceEditor preferences, Control? midSkills = null)
        : this(preferences, midSkills, null) { }

    public EventSettingsTabs(EventPreferenceEditor preferences, Control? midSkills, Control? customRaces)
    {
        Name = "PacerEventSettingsTabs";
        this.Bind(TextElement.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        var sections = preferences.SeparateSections();
        // The tab replaces the old expander shell. Keep the runtime owner's
        // enabled state without attaching the editor to two visual parents.
        var midEditor = midSkills;
        if (midSkills is Expander section && section.Content is Control editor)
        {
            section.Content = null;
            editor.Bind(IsEnabledProperty, section.GetObservable(IsEnabledProperty));
            midEditor = editor;
        }
        hasMidSkills = midEditor != null;
        hasCustomRaces = customRaces != null;
        sharedSave = sections.Actions;
        clearChoices = sharedSave.GetLogicalDescendants().OfType<Button>().FirstOrDefault(button => button.Name == "ClearEventOverrides");
        sharedSave.Margin = new Thickness(0, 12, 0, 0);

        var choicesCard = new Border
        {
            Padding = new Thickness(14),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Child = new StackPanel
            {
                Spacing = 10,
                Children =
                {
                    new TextBlock { Text = "手动事件指定与高频规则 · With / 针灸 / 黄帽", FontSize = 15, FontWeight = FontWeight.SemiBold },
                    sections.Choices
                }
            }
        };
        choicesCard.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));

        var autoCard = new Border
        {
            Name = "EventAutomaticScroll",
            Padding = new Thickness(14),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(10),
            Child = new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = "未指定事件 · 自动技能偏好（#1 → #N 顺位优先）", FontSize = 15, FontWeight = FontWeight.SemiBold },
                    sections.Automatic
                }
            }
        };
        autoCard.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));

        var pages = new System.Collections.Generic.List<TabItem>();
        if (hasMidSkills)
        {
            pages.Add(Page("⚡ 事件偏好与手动指定", choicesCard, "EventChoicesScroll"));
            var skillStack = new StackPanel
            {
                Spacing = 14,
                Children = { autoCard, midEditor! }
            };
            pages.Add(Page("🎯 自动取舍与技能路线", skillStack, "EventMidSkillsScroll"));
        }
        else
        {
            var singleStack = new StackPanel { Spacing = 12, Children = { choicesCard, autoCard } };
            pages.Add(Page("⚡ 事件偏好与手动指定", singleStack, "EventChoicesScroll"));
        }
        if (hasCustomRaces)
        {
            pages.Add(Page("🏁 自定义赛程与跑法", customRaces!, "EventCustomRacesScroll"));
        }

        Tabs = new TabControl
        {
            Name = "PacerEventSettingsSections",
            SelectedIndex = 0,
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch,
            ItemsPanel = new FuncTemplate<Panel?>(() => new WrapPanel()),
            ItemsSource = pages
        };
        var grid = new Grid { RowDefinitions = new("Auto,*,Auto"), RowSpacing = 10 };
        grid.Children.Add(scope);
        Grid.SetRow(Tabs, 1);
        grid.Children.Add(Tabs);
        Grid.SetRow(sharedSave, 2);
        grid.Children.Add(sharedSave);
        Content = new GlassCard
        {
            Content = grid,
            Margin = new Thickness(18),
            Padding = new Thickness(20),
            MaxWidth = 1220,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Stretch,
            IsAnimated = false,
            IsOpaque = true,
            CornerRadius = new CornerRadius(14)
        };
        Tabs.SelectionChanged += (_, _) => UpdateScope();
        UpdateScope();
    }

    private static TabItem Page(string title, Control editor, string name) => new()
    {
        Header = title,
        Content = new ScrollViewer
        {
            Name = name,
            Content = editor,
            Margin = new Thickness(0, 8, 0, 0),
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Top
        }
    };

    private void UpdateScope()
    {
        var idx = Tabs.SelectedIndex;
        var isRacesTab = hasCustomRaces && idx == Tabs.ItemCount - 1;
        var isSkillsTab = hasMidSkills && idx == 1;
        sharedSave.IsVisible = !isRacesTab && !isSkillsTab;
        if (clearChoices != null) clearChoices.IsVisible = idx == 0;
        scope.Text = isRacesTab
            ? "自定义赛程与跑法按当前命名配置独立保存，切换策略版本或恢复官方纯净均不会改动赛程。"
            : isSkillsTab
                ? "自动技能顺位、CM 大赛选技与中途学技能均仅对当前运行配置生效，点顶栏「保存配置」提交。"
                : "当前配置的事件偏好、With 与针灸独立随配置保存；修改即时写入草稿，可直接点顶栏「保存配置」。";
        scope.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
    }

    public void ShowEvents() { if (Tabs.ItemCount > 0) Tabs.SelectedIndex = 0; }
    public void ShowMidSkills() { if (hasMidSkills && Tabs.ItemCount > 1) Tabs.SelectedIndex = 1; }
    public void ShowRaces() { if (hasCustomRaces && Tabs.ItemCount > 0) Tabs.SelectedIndex = Tabs.ItemCount - 1; }
}
