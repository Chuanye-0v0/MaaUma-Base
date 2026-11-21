using System;
using System.Collections.Generic;
using System.Linq;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;

namespace MFAAvalonia.Helper;

/// <summary>Right slide-over drawer displaying L0..L4 purity breakdown, diff table, and 1-click official restore.</summary>
public sealed class PacerPurityDrawer : Grid
{
    private readonly Border backdrop = new()
    {
        Name = "PacerPurityDrawerBackdrop",
        Background = new SolidColorBrush(Color.FromArgb(115, 0, 0, 0)),
        HorizontalAlignment = HorizontalAlignment.Stretch,
        VerticalAlignment = VerticalAlignment.Stretch
    };
    private readonly Border panel = new()
    {
        Name = "PacerPurityDrawerPanel",
        Width = 490,
        MaxWidth = 540,
        HorizontalAlignment = HorizontalAlignment.Right,
        VerticalAlignment = VerticalAlignment.Stretch,
        BorderThickness = new Thickness(1, 0, 0, 0),
        Padding = new Thickness(22)
    };
    private readonly TextBlock subtitleText = new()
    {
        Name = "PacerPurityDrawerSubtitle",
        FontSize = 12,
        TextWrapping = TextWrapping.Wrap
    };
    private readonly Border summaryBanner = new()
    {
        CornerRadius = new CornerRadius(10),
        BorderThickness = new Thickness(1),
        Padding = new Thickness(12, 10)
    };
    private readonly TextBlock summaryTitle = new() { FontSize = 14, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock summaryDesc = new() { FontSize = 12, TextWrapping = TextWrapping.Wrap };
    private readonly StackPanel itemsPanel = new() { Name = "PacerPurityDrawerItems", Spacing = 8 };
    private readonly Button editWeightsBtn = new()
    {
        Name = "PacerPurityDrawerEditWeights",
        Content = "在「高级设置 → 评分权重」中编辑",
        Height = 34,
        Padding = new Thickness(12, 0),
        VerticalContentAlignment = VerticalAlignment.Center
    };
    private readonly Button purifyBtn = new()
    {
        Name = "PacerPurityDrawerPurify",
        Content = "✓ 已是官方纯净",
        Height = 34,
        Padding = new Thickness(14, 0),
        FontWeight = FontWeight.SemiBold,
        VerticalContentAlignment = VerticalAlignment.Center
    };

    public event Action? EditWeightsRequested;
    public event Action? PurifyRequested;
    public int DisplayedItemCount { get; private set; }

    public PacerPurityDrawer()
    {
        Name = "PacerPurityDrawer";
        IsVisible = false;
        HorizontalAlignment = HorizontalAlignment.Stretch;
        VerticalAlignment = VerticalAlignment.Stretch;

        panel.Bind(Border.BackgroundProperty, new DynamicResourceExtension("SukiStrongBackground"));
        panel.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
        panel.Bind(Avalonia.Controls.Documents.TextElement.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        subtitleText.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        backdrop.PointerPressed += (_, _) => Close();
        panel.PointerPressed += (_, e) => e.Handled = true;

        var closeBtn = new Button
        {
            Name = "PacerPurityDrawerClose",
            Content = "关闭",
            Height = 30,
            Padding = new Thickness(12, 0),
            VerticalAlignment = VerticalAlignment.Top,
            VerticalContentAlignment = VerticalAlignment.Center
        };
        closeBtn.Click += (_, _) => Close();

        var headerLeft = new StackPanel
        {
            Spacing = 4,
            Children =
            {
                new TextBlock { Text = "当前生效策略与覆盖明细", FontSize = 17, FontWeight = FontWeight.SemiBold },
                subtitleText
            }
        };
        var header = new Grid
        {
            ColumnDefinitions = new("*,Auto"),
            ColumnSpacing = 12,
            Margin = new Thickness(0, 0, 0, 12)
        };
        header.Children.Add(headerLeft);
        Grid.SetColumn(closeBtn, 1);
        header.Children.Add(closeBtn);

        summaryBanner.Child = new StackPanel { Spacing = 4, Children = { summaryTitle, summaryDesc } };

        var bodyStack = new StackPanel
        {
            Spacing = 12,
            Children =
            {
                summaryBanner,
                new TextBlock
                {
                    Text = "分层覆盖检查清单（合并读取 options 层与 larc_policy 回退层）",
                    FontSize = 13,
                    FontWeight = FontWeight.SemiBold,
                    TextWrapping = TextWrapping.Wrap
                },
                itemsPanel
            }
        };
        var bodyScroll = new ScrollViewer
        {
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            Content = bodyStack
        };

        var footer = new Grid
        {
            ColumnDefinitions = new("*,Auto"),
            ColumnSpacing = 10,
            Margin = new Thickness(0, 14, 0, 0)
        };
        editWeightsBtn.HorizontalAlignment = HorizontalAlignment.Left;
        footer.Children.Add(editWeightsBtn);
        Grid.SetColumn(purifyBtn, 1);
        footer.Children.Add(purifyBtn);

        editWeightsBtn.Click += (_, _) =>
        {
            Close();
            EditWeightsRequested?.Invoke();
        };
        purifyBtn.Click += (_, _) =>
        {
            Close();
            PurifyRequested?.Invoke();
        };

        var shell = new Grid { RowDefinitions = new("Auto,*,Auto") };
        shell.Children.Add(header);
        Grid.SetRow(bodyScroll, 1);
        shell.Children.Add(bodyScroll);
        Grid.SetRow(footer, 2);
        shell.Children.Add(footer);
        panel.Child = shell;

        Children.Add(backdrop);
        Children.Add(panel);
    }

    public void Open(string contextSubtitle, IReadOnlyList<PurityReport> reports)
    {
        subtitleText.Text = contextSubtitle;
        var (agg, _, _) = PacerPurity.SummarizeDocument(reports);
        var revs = string.Join(" / ", reports.Select(r => string.IsNullOrWhiteSpace(r.Revision) ? "默认" : r.Revision).Distinct());
        var totalEff = reports.Sum(r => r.EffectiveCount);
        var isPure = agg is PurityState.Pure or PurityState.EquivalentPure;

        if (agg == PurityState.Stale)
        {
            summaryBanner.BorderBrush = new SolidColorBrush(Color.FromRgb(220, 38, 38));
            summaryBanner.Background = new SolidColorBrush(Color.FromArgb(26, 239, 68, 68));
            summaryTitle.Foreground = new SolidColorBrush(Color.Parse("#DC2626"));
            summaryTitle.Text = $"🔴 检测到跨代 / 历史残留覆盖（{totalEff} 项）";
            var staleLine = reports.Select(r => r.StaleReason).FirstOrDefault(s => !string.IsNullOrWhiteSpace(s));
            summaryDesc.Text = (staleLine ?? "旧版调参或跨代残留会在 2.8.x 第二年远征起叠乘干扰判断。") + " 建议点击下方「✦ 恢复官方策略」消除干扰。";
        }
        else if (agg == PurityState.Custom)
        {
            summaryBanner.BorderBrush = new SolidColorBrush(Color.FromRgb(217, 119, 6));
            summaryBanner.Background = new SolidColorBrush(Color.FromArgb(26, 245, 158, 11));
            summaryTitle.Foreground = new SolidColorBrush(Color.Parse("#D97706"));
            summaryTitle.Text = $"🟠 当前策略含 {totalEff} 项自定义覆盖（基准：{revs}）";
            summaryDesc.Text = "仅下表列出的自定义层生效，其余参数跟随官方 preset；恢复官方策略不会改动五维培养目标、马娘卡组或事件配置。";
        }
        else if (agg == PurityState.Unknown)
        {
            summaryBanner.BorderBrush = new SolidColorBrush(Color.FromRgb(100, 116, 139));
            summaryBanner.Background = new SolidColorBrush(Color.FromArgb(22, 100, 116, 139));
            summaryTitle.Foreground = new SolidColorBrush(Color.Parse("#475569"));
            summaryTitle.Text = "⚪ 历史 / 继承策略状态";
            summaryDesc.Text = "当前步骤未锁定具体的 baseline revision，将沿用原配置默认值。";
        }
        else
        {
            summaryBanner.BorderBrush = new SolidColorBrush(Color.FromRgb(22, 163, 74));
            summaryBanner.Background = new SolidColorBrush(Color.FromArgb(24, 34, 197, 94));
            summaryTitle.Foreground = new SolidColorBrush(Color.Parse("#16A34A"));
            summaryTitle.Text = agg == PurityState.EquivalentPure
                ? $"🟢 等效官方纯净（基准：{revs}，含未改分数的空壳）"
                : $"🟢 100% 官方纯净策略（基准：{revs}）";
            summaryDesc.Text = "L1 五维偏好、L2 基础倍率、L3 细调/公式、L4 分年策略全部为官方出厂设定（调整五维培养目标不破坏纯净度）。";
        }

        itemsPanel.Children.Clear();
        var count = 0;
        foreach (var report in reports)
        {
            if (reports.Count > 1)
            {
                var stepHead = new TextBlock
                {
                    Text = $"{report.Emoji} 步骤：{report.StepLabel}（策略：{report.Revision ?? "继承"}）",
                    FontSize = 12,
                    FontWeight = FontWeight.SemiBold,
                    Margin = new Thickness(0, 4, 0, 0)
                };
                itemsPanel.Children.Add(stepHead);
            }
            if (report.Items.Count == 0) continue;
            foreach (var item in report.Items)
            {
                count++;
                itemsPanel.Children.Add(BuildItemRow(item));
            }
        }
        DisplayedItemCount = count;

        if (count == 0)
        {
            var emptyBox = new Border
            {
                CornerRadius = new CornerRadius(8),
                BorderThickness = new Thickness(1),
                Padding = new Thickness(12, 10),
                Child = new TextBlock
                {
                    Text = "✓ 无任何覆盖或残留壳，L0–L4 全量纯净。",
                    FontSize = 12,
                    TextWrapping = TextWrapping.Wrap
                }
            };
            emptyBox.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
            itemsPanel.Children.Add(emptyBox);
        }

        var canPurify = !isPure || agg == PurityState.EquivalentPure;
        purifyBtn.IsEnabled = canPurify;
        if (canPurify)
        {
            var revSingle = reports.Select(r => r.Revision).FirstOrDefault(r => !string.IsNullOrWhiteSpace(r));
            purifyBtn.Content = agg == PurityState.EquivalentPure
                ? "✦ 清理空壳恢复纯正默认"
                : (string.IsNullOrEmpty(revSingle) ? "✦ 恢复官方策略" : $"✦ 恢复 {revSingle} 官方策略");
            if (!purifyBtn.Classes.Contains("Accent"))
                purifyBtn.Classes.Add("Accent");
        }
        else
        {
            purifyBtn.Content = "✓ 已是官方纯净";
            purifyBtn.Classes.Remove("Accent");
        }

        IsVisible = true;
    }

    public void Close() => IsVisible = false;

    private static Control BuildItemRow(PurityItem item)
    {
        var layerTag = item.Layer switch
        {
            PurityLayer.L0Revision => "L0",
            PurityLayer.L1Attribute => "L1",
            PurityLayer.L2Base => "L2",
            PurityLayer.L3Tuning => "L3",
            PurityLayer.L4Workflow => "L4",
            _ => "--"
        };
        var layerBadge = new Border
        {
            CornerRadius = new CornerRadius(5),
            Padding = new Thickness(6, 2),
            Background = new SolidColorBrush(Color.FromArgb(28, 100, 116, 139)),
            VerticalAlignment = VerticalAlignment.Center,
            Child = new TextBlock
            {
                Text = layerTag,
                FontSize = 11,
                FontFamily = new FontFamily("Cascadia Mono, Consolas"),
                FontWeight = FontWeight.SemiBold
            }
        };
        var titleRow = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            Children =
            {
                layerBadge,
                new TextBlock { Text = item.Label, FontSize = 12, FontWeight = FontWeight.SemiBold, TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center }
            }
        };
        var pathText = new TextBlock
        {
            Text = item.Path,
            FontSize = 11,
            FontFamily = new FontFamily("Cascadia Mono, Consolas"),
            TextWrapping = TextWrapping.Wrap
        };
        pathText.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        var left = new StackPanel { Spacing = 3, Children = { titleRow, pathText } };

        var actualText = new TextBlock
        {
            Text = item.ActualText,
            FontSize = 12,
            FontFamily = new FontFamily("Cascadia Mono, Consolas"),
            FontWeight = FontWeight.SemiBold,
            HorizontalAlignment = HorizontalAlignment.Right,
            Foreground = new SolidColorBrush(item.Effective ? Color.Parse("#D97706") : Color.Parse("#64748B"))
        };
        var defText = new TextBlock
        {
            Text = "默认 " + item.DefaultText,
            FontSize = 11,
            FontFamily = new FontFamily("Cascadia Mono, Consolas"),
            HorizontalAlignment = HorizontalAlignment.Right
        };
        defText.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        var right = new StackPanel
        {
            Spacing = 2,
            HorizontalAlignment = HorizontalAlignment.Right,
            VerticalAlignment = VerticalAlignment.Center,
            Children = { actualText, defText }
        };

        var grid = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 12 };
        grid.Children.Add(left);
        Grid.SetColumn(right, 1);
        grid.Children.Add(right);

        var row = new Border
        {
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(10, 8),
            Child = grid
        };
        row.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
        return row;
    }
}
