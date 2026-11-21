using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Media;

namespace MFAAvalonia.Helper;

/// <summary>Compact top-bar purity pill opening the slide-over drawer (with flyout fallback) and 1-step undo.</summary>
public sealed class PacerPurityBadge : StackPanel
{
    private readonly Button badge = new()
    {
        Name = "PacerPurityBadge",
        Content = "🟢 检查策略纯净度…",
        Height = 32,
        Padding = new Thickness(12, 0),
        CornerRadius = new CornerRadius(16),
        BorderThickness = new Thickness(1),
        FontSize = 12,
        FontWeight = FontWeight.SemiBold,
        VerticalContentAlignment = VerticalAlignment.Center
    };
    private readonly Button quickUndo = new()
    {
        Name = "PacerPurityQuickUndo",
        Content = "↶ 撤销恢复纯净",
        Height = 30,
        Padding = new Thickness(10, 0),
        FontSize = 12,
        IsVisible = false,
        VerticalContentAlignment = VerticalAlignment.Center
    };
    private readonly TextBlock flyoutDetails = new()
    {
        Name = "PacerPurityDetails",
        TextWrapping = TextWrapping.Wrap,
        FontSize = 12,
        MaxWidth = 440
    };
    private readonly Button restoreButton = new()
    {
        Name = "PacerPurityRestore",
        Content = "✦ 一键恢复官方纯净",
        Height = 32,
        Padding = new Thickness(12, 4),
        FontWeight = FontWeight.SemiBold
    };
    private readonly Button openWeightsButton = new()
    {
        Name = "PacerPurityOpenWeights",
        Content = "前往高级设置 ›",
        Height = 32,
        Padding = new Thickness(10, 4)
    };
    private readonly Flyout flyout = new();

    public PurityState CurrentState { get; private set; } = PurityState.Unknown;
    public IReadOnlyList<PurityReport> CurrentReports { get; private set; } = Array.Empty<PurityReport>();

    public event Action? RestoreRequested;
    public event Action? UndoRequested;
    public event Action? DetailsRequested;
    public event Action? DrawerRequested;

    public PacerPurityBadge()
    {
        Orientation = Orientation.Horizontal;
        Spacing = 6;
        VerticalAlignment = VerticalAlignment.Center;

        var buttons = new WrapPanel { Orientation = Orientation.Horizontal };
        restoreButton.Margin = new Thickness(0, 0, 8, 4);
        buttons.Children.Add(restoreButton);
        buttons.Children.Add(openWeightsButton);

        var body = new StackPanel
        {
            Spacing = 10,
            Children =
            {
                new TextBlock { Text = "当前策略纯净度与覆盖详情", FontSize = 14, FontWeight = FontWeight.SemiBold },
                flyoutDetails,
                buttons,
                new TextBlock { Text = "提示：一键恢复仅修改草稿，需点「保存配置」生效；保存前可随时撤销。", FontSize = 11, Opacity = 0.72, TextWrapping = TextWrapping.Wrap, MaxWidth = 440 }
            }
        };
        flyout.Content = body;
        FlyoutBase.SetAttachedFlyout(badge, flyout);

        badge.Click += (_, _) =>
        {
            if (DrawerRequested != null)
                DrawerRequested.Invoke();
            else
                FlyoutBase.ShowAttachedFlyout(badge);
        };
        restoreButton.Click += (_, _) =>
        {
            flyout.Hide();
            RestoreRequested?.Invoke();
        };
        quickUndo.Click += (_, _) => UndoRequested?.Invoke();
        openWeightsButton.Click += (_, _) =>
        {
            flyout.Hide();
            DetailsRequested?.Invoke();
        };

        Children.Add(badge);
        Children.Add(quickUndo);
    }

    public void Update(IReadOnlyList<PurityReport> reports, bool canUndo)
    {
        CurrentReports = reports;
        var (state, text, details) = PacerPurity.SummarizeDocument(reports);
        CurrentState = state;
        badge.Content = text + " ▾";
        flyoutDetails.Text = details;
        ToolTip.SetTip(badge, details + "\n（点击展开右侧策略覆盖明细抽屉）");
        restoreButton.IsVisible = state is PurityState.Custom or PurityState.Stale or PurityState.EquivalentPure;
        quickUndo.IsVisible = canUndo;

        if (state == PurityState.Stale)
        {
            badge.Background = new SolidColorBrush(Color.Parse("#FEE2E2"));
            badge.BorderBrush = new SolidColorBrush(Color.Parse("#FCA5A5"));
            badge.Foreground = new SolidColorBrush(Color.Parse("#991B1B"));
        }
        else if (state == PurityState.Custom)
        {
            badge.Background = new SolidColorBrush(Color.Parse("#FFEDD5"));
            badge.BorderBrush = new SolidColorBrush(Color.Parse("#FDBA74"));
            badge.Foreground = new SolidColorBrush(Color.Parse("#9A3412"));
        }
        else if (state == PurityState.Unknown)
        {
            badge.Background = new SolidColorBrush(Color.Parse("#F1F5F9"));
            badge.BorderBrush = new SolidColorBrush(Color.Parse("#CBD5E1"));
            badge.Foreground = new SolidColorBrush(Color.Parse("#475569"));
        }
        else
        {
            badge.Background = new SolidColorBrush(Color.Parse("#DCFCE7"));
            badge.BorderBrush = new SolidColorBrush(Color.Parse("#86EFAC"));
            badge.Foreground = new SolidColorBrush(Color.Parse("#166534"));
        }
    }
}
