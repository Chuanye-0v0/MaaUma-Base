using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Primitives;
using Avalonia.Controls.Templates;
using Avalonia.Layout;
using Avalonia.Markup.Xaml.MarkupExtensions;
using Avalonia.Media;

namespace MFAAvalonia.Helper;

/// <summary>Modal configuration lifecycle manager: switch, delete, rename, clone (purify optional), or create blank pure profile.</summary>
public sealed class PacerProfileManagerWindow : Window
{
    public sealed record ProfileItemInfo(
        string Id,
        string Name,
        bool IsCurrent,
        string EquipmentSummary,
        IReadOnlyList<PurityReport> Reports);

    private readonly Func<IReadOnlyList<ProfileItemInfo>> readProfiles;
    private readonly Func<string, Task> onSwitchProfile;
    private readonly Func<string, Task> onDeleteProfile;
    private readonly Func<string, Task> onCreateBlank;
    private readonly Func<string, Task> onRenameCurrent;
    private readonly Func<string, bool, Task> onSaveAsCopy;

    private readonly StackPanel listPanel = new() { Name = "ProfileManagerList", Spacing = 8 };
    private readonly TextBox blankNameBox = new() { Name = "ProfileManagerBlankName", Watermark = "例如：2.9 三速纯冲" };
    private readonly TextBox renameBox = new() { Name = "ProfileManagerRename", Watermark = "输入当前配置新名称" };
    private readonly TextBox copyNameBox = new() { Name = "ProfileManagerCopyName", Watermark = "留空自动生成副本名" };
    private readonly CheckBox copyPurifyCheck = new()
    {
        Name = "ProfileManagerCopyPurify",
        Content = "另存副本时恢复官方纯净权重（清除自定义倍率和高级评分）",
        IsChecked = false
    };
    private readonly TextBlock statusText = new()
    {
        Name = "ProfileManagerStatus",
        FontSize = 12,
        TextWrapping = TextWrapping.Wrap,
        Text = "「模式」决定局外流程拓扑；「配置」各自保存马娘、卡组、目标与策略权重。"
    };
    private readonly Button createBlankBtn;
    private readonly Button renameBtn;
    private readonly Button saveAsBtn;
    private readonly Button closeBtn;
    private bool busy;

    public PacerProfileManagerWindow(
        string modeName,
        Func<IReadOnlyList<ProfileItemInfo>> readProfiles,
        Func<string, Task> onSwitchProfile,
        Func<string, Task> onDeleteProfile,
        Func<string, Task> onCreateBlank,
        Func<string, Task> onRenameCurrent,
        Func<string, bool, Task> onSaveAsCopy)
    {
        this.readProfiles = readProfiles;
        this.onSwitchProfile = onSwitchProfile;
        this.onDeleteProfile = onDeleteProfile;
        this.onCreateBlank = onCreateBlank;
        this.onRenameCurrent = onRenameCurrent;
        this.onSaveAsCopy = onSaveAsCopy;

        Title = $"配置管理 · 模式「{modeName}」";
        Width = 780;
        Height = 660;
        MinWidth = 620;
        MinHeight = 500;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;

        this.Bind(TemplatedControl.BackgroundProperty, new DynamicResourceExtension("SukiStrongBackground"));
        this.Bind(TemplatedControl.ForegroundProperty, new DynamicResourceExtension("SukiText"));
        statusText.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        closeBtn = new Button
        {
            Name = "ProfileManagerClose",
            Content = "关闭",
            Height = 32,
            Padding = new Thickness(14, 0),
            VerticalContentAlignment = VerticalAlignment.Center
        };
        closeBtn.Click += (_, _) => Close();

        var sub = new TextBlock
        {
            Text = "切换、删除、重命名运行配置，或基于官方 2.9 模板新建纯净配置。",
            FontSize = 12,
            TextWrapping = TextWrapping.Wrap
        };
        sub.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        var header = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 12 };
        header.Children.Add(new StackPanel
        {
            Spacing = 3,
            Children =
            {
                new TextBlock { Text = $"配置管理 · 模式「{modeName}」", FontSize = 18, FontWeight = FontWeight.SemiBold },
                sub
            }
        });
        Grid.SetColumn(closeBtn, 1);
        header.Children.Add(closeBtn);

        var listScroll = new ScrollViewer
        {
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            Content = listPanel
        };
        var listCard = new Border
        {
            CornerRadius = new CornerRadius(12),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(12),
            Child = listScroll
        };
        listCard.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));

        createBlankBtn = new Button
        {
            Name = "ProfileManagerCreateBlank",
            Content = "创建并切换",
            Height = 34,
            Padding = new Thickness(14, 0),
            FontWeight = FontWeight.SemiBold,
            VerticalContentAlignment = VerticalAlignment.Center
        };
        createBlankBtn.Classes.Add("Accent");
        createBlankBtn.Click += async (_, _) => await RunAction(async () =>
        {
            await this.onCreateBlank(blankNameBox.Text?.Trim() ?? "");
            blankNameBox.Text = "";
            RefreshList();
            statusText.Text = "已新建空白纯净配置并切换为当前配置。";
        });

        var newDesc = new TextBlock
        {
            Text = "从官方 2.9 模板建立全纯净新配置（L1–L4 全量零自定义），不克隆当前草稿中的任何覆盖。",
            FontSize = 12,
            TextWrapping = TextWrapping.Wrap
        };
        newDesc.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

        var leftCard = new Border
        {
            CornerRadius = new CornerRadius(12),
            BorderThickness = new Thickness(1.5),
            BorderBrush = new SolidColorBrush(Color.FromRgb(59, 130, 246)),
            Background = new SolidColorBrush(Color.FromArgb(18, 59, 130, 246)),
            Padding = new Thickness(14),
            Child = new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = "+ 新建空白纯净配置（推荐）", FontSize = 14, FontWeight = FontWeight.SemiBold },
                    newDesc,
                    blankNameBox,
                    createBlankBtn
                }
            }
        };

        renameBtn = new Button
        {
            Name = "ProfileManagerRenameBtn",
            Content = "应用改名",
            Height = 32,
            Padding = new Thickness(12, 0),
            VerticalContentAlignment = VerticalAlignment.Center
        };
        renameBtn.Click += async (_, _) => await RunAction(async () =>
        {
            await this.onRenameCurrent(renameBox.Text?.Trim() ?? "");
            RefreshList();
            statusText.Text = "配置名称已更新。";
        });

        var renameRow = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 8 };
        renameRow.Children.Add(renameBox);
        Grid.SetColumn(renameBtn, 1);
        renameRow.Children.Add(renameBtn);

        saveAsBtn = new Button
        {
            Name = "ProfileManagerSaveAs",
            Content = "另存为新配置",
            Height = 32,
            Padding = new Thickness(12, 0),
            VerticalContentAlignment = VerticalAlignment.Center
        };
        saveAsBtn.Click += async (_, _) => await RunAction(async () =>
        {
            await this.onSaveAsCopy(copyNameBox.Text?.Trim() ?? "", copyPurifyCheck.IsChecked == true);
            copyNameBox.Text = "";
            RefreshList();
            statusText.Text = "已另存为独立新配置并切换。";
        });

        var copyRow = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 8 };
        copyRow.Children.Add(copyNameBox);
        Grid.SetColumn(saveAsBtn, 1);
        copyRow.Children.Add(saveAsBtn);

        var rightCard = new Border
        {
            CornerRadius = new CornerRadius(12),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(14),
            Child = new StackPanel
            {
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = "重命名当前配置 / 另存副本", FontSize = 14, FontWeight = FontWeight.SemiBold },
                    renameRow,
                    copyRow,
                    copyPurifyCheck
                }
            }
        };
        rightCard.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));

        var actionGrid = new Grid { ColumnDefinitions = new("*,*"), ColumnSpacing = 12 };
        actionGrid.Children.Add(leftCard);
        Grid.SetColumn(rightCard, 1);
        actionGrid.Children.Add(rightCard);

        var root = new Grid
        {
            Margin = new Thickness(22),
            RowDefinitions = new("Auto,*,Auto,Auto"),
            RowSpacing = 12
        };
        root.Children.Add(header);
        Grid.SetRow(listCard, 1);
        root.Children.Add(listCard);
        Grid.SetRow(actionGrid, 2);
        root.Children.Add(actionGrid);
        Grid.SetRow(statusText, 3);
        root.Children.Add(statusText);
        Content = root;

        RefreshList();
    }

    private async Task RunAction(Func<Task> work)
    {
        if (busy) return;
        busy = true;
        SetControlsEnabled(false);
        try { await work(); }
        catch (Exception ex) { statusText.Text = ex.Message; }
        finally
        {
            busy = false;
            SetControlsEnabled(true);
            RefreshList();
        }
    }

    private void SetControlsEnabled(bool enabled)
    {
        createBlankBtn.IsEnabled = enabled;
        renameBtn.IsEnabled = enabled;
        saveAsBtn.IsEnabled = enabled;
        blankNameBox.IsEnabled = enabled;
        renameBox.IsEnabled = enabled;
        copyNameBox.IsEnabled = enabled;
        copyPurifyCheck.IsEnabled = enabled;
    }

    public void RefreshList()
    {
        var items = readProfiles();
        var current = items.FirstOrDefault(i => i.IsCurrent);
        if (current != null)
        {
            renameBox.Text = current.Name;
            copyPurifyCheck.IsChecked = current.Reports.Any(r =>
                r.State == PurityState.Stale || r.HasLayer(PurityLayer.L3Tuning) || r.HasLayer(PurityLayer.L4Workflow));
        }

        listPanel.Children.Clear();
        foreach (var item in items)
        {
            var (state, badgeText, _) = PacerPurity.SummarizeDocument(item.Reports);
            var titleRow = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                Spacing = 8,
                Children =
                {
                    new TextBlock { Text = item.Name, FontSize = 14, FontWeight = FontWeight.SemiBold, VerticalAlignment = VerticalAlignment.Center }
                }
            };
            if (item.IsCurrent)
            {
                titleRow.Children.Add(new Border
                {
                    CornerRadius = new CornerRadius(4),
                    Padding = new Thickness(6, 2),
                    Background = new SolidColorBrush(Color.FromRgb(37, 99, 235)),
                    VerticalAlignment = VerticalAlignment.Center,
                    Child = new TextBlock { Text = "当前选中", FontSize = 11, FontWeight = FontWeight.SemiBold, Foreground = Brushes.White }
                });
            }
            var purityColor = state == PurityState.Stale ? "#DC2626"
                : state == PurityState.Custom ? "#D97706"
                : state == PurityState.Unknown ? "#64748B"
                : "#16A34A";
            titleRow.Children.Add(new TextBlock
            {
                Text = badgeText,
                FontSize = 12,
                FontWeight = FontWeight.SemiBold,
                Foreground = new SolidColorBrush(Color.Parse(purityColor)),
                VerticalAlignment = VerticalAlignment.Center
            });

            var subLine = new TextBlock { Text = item.EquipmentSummary, FontSize = 12, TextWrapping = TextWrapping.Wrap };
            subLine.Bind(TextBlock.ForegroundProperty, new DynamicResourceExtension("SukiLowText"));

            var left = new StackPanel { Spacing = 3, Children = { titleRow, subLine } };

            var right = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
            if (!item.IsCurrent)
            {
                var idCopy = item.Id;
                var switchBtn = new Button
                {
                    Content = "切换使用",
                    Height = 30,
                    Padding = new Thickness(10, 0),
                    IsEnabled = !busy,
                    VerticalContentAlignment = VerticalAlignment.Center
                };
                switchBtn.Click += async (_, _) => await RunAction(async () =>
                {
                    await onSwitchProfile(idCopy);
                    statusText.Text = $"已切换至配置「{item.Name}」。";
                });
                right.Children.Add(switchBtn);
            }

            var delId = item.Id;
            var delBtn = new Button
            {
                Content = "🗑 删除",
                Height = 30,
                Padding = new Thickness(10, 0),
                IsEnabled = !busy && items.Count > 1,
                VerticalContentAlignment = VerticalAlignment.Center
            };
            ToolTip.SetTip(delBtn, items.Count <= 1 ? "每个模式至少保留一份配置" : $"删除配置「{item.Name}」");
            delBtn.Click += async (_, _) => await RunAction(async () =>
            {
                await onDeleteProfile(delId);
                statusText.Text = $"已删除配置「{item.Name}」。";
            });
            right.Children.Add(delBtn);

            var rowGrid = new Grid { ColumnDefinitions = new("*,Auto"), ColumnSpacing = 12 };
            rowGrid.Children.Add(left);
            Grid.SetColumn(right, 1);
            rowGrid.Children.Add(right);

            var card = new Border
            {
                CornerRadius = new CornerRadius(8),
                BorderThickness = new Thickness(1),
                Padding = new Thickness(12, 10),
                Background = item.IsCurrent ? new SolidColorBrush(Color.FromArgb(20, 59, 130, 246)) : Brushes.Transparent,
                Child = rowGrid
            };
            card.Bind(Border.BorderBrushProperty, new DynamicResourceExtension("SukiControlBorderBrush"));
            listPanel.Children.Add(card);
        }
    }
}
