using System;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>Compact 2x2 preflight summary and quick navigation card displayed on the Overview page.</summary>
public sealed class PacerOverviewPreflightCard : GlassCard
{
    private readonly TextBlock modeProfileText = new() { Name = "PreflightModeProfile", FontSize = 14, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock equipmentBatchText = new() { Name = "PreflightEquipmentBatch", FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock purityText = new() { Name = "PreflightPurityText", FontSize = 14, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock targetsText = new() { Name = "PreflightTargetsText", FontSize = 14, FontWeight = FontWeight.SemiBold, FontFamily = new FontFamily("Cascadia Mono, Consolas"), TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock eventsHeadlineText = new() { Name = "PreflightEventsHeadline", FontSize = 13, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock withSummaryText = new() { Name = "PreflightWithSummary", FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly Grid tiles = new() { ColumnDefinitions = new("*,*"), RowDefinitions = new("Auto,Auto"), ColumnSpacing = 12, RowSpacing = 12 };

    public event Action? OpenStrategyRequested;
    public event Action? OpenEventsRequested;

    public PacerOverviewPreflightCard()
    {
        Name = "PacerOverviewPreflightCard";
        Padding = new Thickness(18);
        CornerRadius = new CornerRadius(14);
        IsAnimated = false;
        IsOpaque = true;

        equipmentBatchText.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        withSummaryText.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        var tile1 = Tile("模式 / 运行配置", modeProfileText, equipmentBatchText);

        var goStrategy = LinkButton("PreflightGoStrategy", "前往调整策略或恢复纯净 ›", () => OpenStrategyRequested?.Invoke());
        var tile2 = Tile("策略与纯净度", purityText, goStrategy);

        var targetNote = new TextBlock { Text = "调整五维目标不计入定制权重覆盖", FontSize = 12, TextWrapping = TextWrapping.Wrap };
        targetNote.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var tile3 = Tile("五维培养目标", targetsText, targetNote);

        var goEvents = LinkButton("PreflightGoEvents", "前往查看/定制事件偏好 ›", () => OpenEventsRequested?.Invoke());
        var tile4 = Tile("事件与赛程就绪摘要", eventsHeadlineText, withSummaryText, goEvents);

        tiles.Children.Add(tile1);
        Grid.SetColumn(tile2, 1); tiles.Children.Add(tile2);
        Grid.SetRow(tile3, 1); tiles.Children.Add(tile3);
        Grid.SetColumn(tile4, 1); Grid.SetRow(tile4, 1); tiles.Children.Add(tile4);

        var stack = new StackPanel
        {
            Spacing = 12,
            Children =
            {
                new TextBlock { Text = "即将开跑摘要与快捷巡检", FontSize = 17, FontWeight = FontWeight.SemiBold },
                tiles
            }
        };
        Content = stack;

        bool? lastNarrow = null;
        SizeChanged += (_, args) =>
        {
            if (args.NewSize.Width <= 0) return;
            var narrow = PacerLayoutGuard.NextNarrow(args.NewSize.Width, 520, lastNarrow, 24);
            if (lastNarrow == narrow) return;
            lastNarrow = narrow;
            tiles.ColumnDefinitions = new(narrow ? "*" : "*,*");
            tiles.RowDefinitions = new(narrow ? "Auto,Auto,Auto,Auto" : "Auto,Auto");
            Grid.SetColumn(tile2, narrow ? 0 : 1); Grid.SetRow(tile2, narrow ? 1 : 0);
            Grid.SetColumn(tile3, 0); Grid.SetRow(tile3, narrow ? 2 : 1);
            Grid.SetColumn(tile4, narrow ? 0 : 1); Grid.SetRow(tile4, narrow ? 3 : 1);
        };
    }

    public void Update(
        string modeProfile,
        string equipmentBatch,
        PurityState state,
        string puritySummary,
        string targets,
        string eventsHeadline,
        string withSummary)
    {
        modeProfileText.Text = string.IsNullOrWhiteSpace(modeProfile) ? "未读取模式与配置" : modeProfile;
        equipmentBatchText.Text = string.IsNullOrWhiteSpace(equipmentBatch) ? "沿用当前角色 · 沿用当前卡组" : equipmentBatch;
        purityText.Text = string.IsNullOrWhiteSpace(puritySummary) ? "⚪ 策略待读取" : puritySummary;
        purityText.Foreground = state switch
        {
            PurityState.Stale => new SolidColorBrush(Color.Parse("#DC2626")),
            PurityState.Custom => new SolidColorBrush(Color.Parse("#D97706")),
            PurityState.Unknown => new SolidColorBrush(Color.Parse("#64748B")),
            _ => new SolidColorBrush(Color.Parse("#16A34A"))
        };
        targetsText.Text = string.IsNullOrWhiteSpace(targets) ? "1550 / 1200 / 1200 / 1000 / 1200" : targets;
        eventsHeadlineText.Text = string.IsNullOrWhiteSpace(eventsHeadline) ? "事件偏好 · 已就绪" : eventsHeadline;
        withSummaryText.Text = string.IsNullOrWhiteSpace(withSummary) ? "With 优先：自动选择" : withSummary;
    }

    private static Control Tile(string caption, params Control[] children)
    {
        var cap = new TextBlock { Text = caption, FontSize = 12 };
        cap.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));
        var stack = new StackPanel { Spacing = 4 };
        stack.Children.Add(cap);
        foreach (var child in children)
            stack.Children.Add(child);
        var border = new Border
        {
            CornerRadius = new CornerRadius(10),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12, 10),
            Child = stack
        };
        border.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
        return border;
    }

    private static Button LinkButton(string name, string text, Action click)
    {
        var btn = new Button
        {
            Name = name,
            Content = text,
            FontSize = 12,
            Padding = new Thickness(8, 3),
            Margin = new Thickness(0, 2, 0, 0),
            HorizontalAlignment = HorizontalAlignment.Left
        };
        btn.Click += (_, _) => click();
        return btn;
    }
}
