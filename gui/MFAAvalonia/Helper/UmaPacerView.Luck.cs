using System;
using System.Collections.Generic;
using System.IO;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    private const double LuckThermometerRange = 30.0;
    private const double LuckThermometerHalfHeight = 64.0;
    private const int LuckMascotDipSize = 48;

    private static readonly IBrush WarmScoreBrush = new SolidColorBrush(Color.Parse("#F59E0B"));
    private static readonly IBrush HotScoreBrush = new SolidColorBrush(Color.Parse("#EF4444"));
    private static readonly IBrush ColdScoreBrush = new SolidColorBrush(Color.Parse("#38BDF8"));
    private static readonly IBrush DeepColdScoreBrush = new SolidColorBrush(Color.Parse("#2563EB"));
    private static readonly IBrush NeutralScoreBrush = new SolidColorBrush(Color.Parse("#94A3B8"));
    private static readonly IBrush TubeTrackBrush = new SolidColorBrush(Color.Parse("#26888888"));
    private static readonly IBrush TubeBorderBrush = new SolidColorBrush(Color.Parse("#4D888888"));
    private static readonly IBrush ZeroTickBrush = new SolidColorBrush(Color.Parse("#D9FFFFFF"));
    private static readonly IBrush MinorTickBrush = new SolidColorBrush(Color.Parse("#59FFFFFF"));
    private static readonly IBrush WarmBadgeBgBrush = new SolidColorBrush(Color.Parse("#1AF59E0B"));
    private static readonly IBrush WarmBadgeBorderBrush = new SolidColorBrush(Color.Parse("#40F59E0B"));
    private static readonly IBrush ColdBadgeBgBrush = new SolidColorBrush(Color.Parse("#1A38BDF8"));
    private static readonly IBrush ColdBadgeBorderBrush = new SolidColorBrush(Color.Parse("#4038BDF8"));
    private static readonly IBrush StateBadgeBgBrush = new SolidColorBrush(Color.Parse("#22888888"));

    private static readonly IBrush WarmMercuryBrush = new LinearGradientBrush
    {
        StartPoint = new RelativePoint(0.5, 1.0, RelativeUnit.Relative),
        EndPoint = new RelativePoint(0.5, 0.0, RelativeUnit.Relative),
        GradientStops =
        {
            new GradientStop(Color.Parse("#F59E0B"), 0.0),
            new GradientStop(Color.Parse("#EF4444"), 1.0)
        }
    };

    private static readonly IBrush ColdMercuryBrush = new LinearGradientBrush
    {
        StartPoint = new RelativePoint(0.5, 0.0, RelativeUnit.Relative),
        EndPoint = new RelativePoint(0.5, 1.0, RelativeUnit.Relative),
        GradientStops =
        {
            new GradientStop(Color.Parse("#38BDF8"), 0.0),
            new GradientStop(Color.Parse("#2563EB"), 1.0)
        }
    };

    private readonly WrapPanel realtimeLuckSummary = new() { Name = "PacerRealtimeLuckSummary", IsVisible = false };
    private readonly TextBlock realtimeLuckScore = new()
    {
        Name = "PacerRealtimeLuckScore", FontSize = 13, FontWeight = FontWeight.SemiBold,
        Margin = new Thickness(0, 0, 12, 0)
    };
    private readonly TextBlock realtimeLuckDailySummary = new()
    {
        Name = "PacerRealtimeLuckDailySummary", FontSize = 12, Opacity = 0.86,
        Margin = new Thickness(0, 0, 12, 0)
    };
    private readonly TextBlock realtimeLuckJudgment = new()
    {
        Name = "PacerRealtimeLuckJudgment", FontSize = 12, IsVisible = false
    };
    private readonly TextBlock realtimeLuckTitle = new()
    {
        Name = "PacerRealtimeLuckTitle", Text = "本局胡度", FontSize = 18, FontWeight = FontWeight.SemiBold,
        VerticalAlignment = VerticalAlignment.Center
    };
    private readonly TextBlock realtimeLuckStateTag = new()
    {
        Name = "PacerRealtimeLuckStateTag", FontSize = 11, FontWeight = FontWeight.Medium,
        VerticalAlignment = VerticalAlignment.Center, Opacity = 0.85
    };
    private readonly Border realtimeLuckStateBadge = new()
    {
        Name = "PacerRealtimeLuckStateBadge", CornerRadius = new CornerRadius(4), Padding = new Thickness(6, 2),
        Margin = new Thickness(8, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center,
        Background = StateBadgeBgBrush, IsVisible = false
    };
    private readonly TextBlock realtimeLuckCardScore = new()
    {
        Name = "PacerRealtimeLuckCardScore", FontSize = 28, FontWeight = FontWeight.Bold,
        VerticalAlignment = VerticalAlignment.Center
    };
    private readonly TextBlock realtimeLuckRecentTag = new()
    {
        Name = "PacerRealtimeLuckRecentTag", FontSize = 11, FontWeight = FontWeight.Medium,
        VerticalAlignment = VerticalAlignment.Center, Opacity = 0.85
    };
    private readonly Border realtimeLuckRecentBadge = new()
    {
        CornerRadius = new CornerRadius(4), Padding = new Thickness(6, 2),
        Margin = new Thickness(8, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center,
        Background = new SolidColorBrush(Color.Parse("#1F888888")), IsVisible = false
    };
    private readonly TextBlock realtimeLuckCardCoverage = new()
    {
        Name = "PacerRealtimeLuckCardCoverage", FontSize = 12, Opacity = 0.82,
        TextWrapping = TextWrapping.Wrap
    };
    private readonly TextBlock realtimeLuckCardJudgment = new()
    {
        Name = "PacerRealtimeLuckCardJudgment", FontSize = 12, TextWrapping = TextWrapping.Wrap, IsVisible = false
    };
    private readonly TextBlock realtimeLuckDailyBestText = new()
    {
        Name = "PacerDailyBestText", Text = "--", FontSize = 12.5, FontWeight = FontWeight.SemiBold,
        Foreground = NeutralScoreBrush, TextTrimming = TextTrimming.CharacterEllipsis
    };
    private readonly TextBlock realtimeLuckDailyWorstText = new()
    {
        Name = "PacerDailyWorstText", Text = "--", FontSize = 12.5, FontWeight = FontWeight.SemiBold,
        Foreground = NeutralScoreBrush, TextTrimming = TextTrimming.CharacterEllipsis
    };
    private readonly Image realtimeLuckMascot = new()
    {
        Name = "PacerRealtimeLuckMascot", Width = LuckMascotDipSize, Height = LuckMascotDipSize,
        Stretch = Stretch.Uniform, UseLayoutRounding = true,
        VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(8, -2, 0, -2), IsVisible = false
    };
    private readonly Border realtimeLuckWarmBar = new()
    {
        Name = "PacerRealtimeLuckWarmBar", Width = 8, Height = 0,
        CornerRadius = new CornerRadius(4, 4, 0, 0),
        HorizontalAlignment = HorizontalAlignment.Center,
        VerticalAlignment = VerticalAlignment.Bottom,
        Background = WarmMercuryBrush
    };
    private readonly Border realtimeLuckColdBar = new()
    {
        Name = "PacerRealtimeLuckColdBar", Width = 8, Height = 0,
        CornerRadius = new CornerRadius(0, 0, 4, 4),
        HorizontalAlignment = HorizontalAlignment.Center,
        VerticalAlignment = VerticalAlignment.Top,
        Background = ColdMercuryBrush
    };
    private readonly Border realtimeLuckBulb = new()
    {
        Name = "PacerRealtimeLuckBulb", Width = 16, Height = 16,
        CornerRadius = new CornerRadius(8),
        Margin = new Thickness(0, -3, 0, 0),
        HorizontalAlignment = HorizontalAlignment.Center,
        Background = NeutralScoreBrush,
        BorderBrush = TubeBorderBrush,
        BorderThickness = new Thickness(1)
    };
    private readonly Dictionary<string, Bitmap?> realtimeLuckIcons = new();
    private readonly Dictionary<string, Bitmap?> realtimeLuckCrispIcons = new();
    private Control? realtimeLuckCard;
    private TopLevel? realtimeLuckWatchedTopLevel;
    private bool realtimeLuckHasScore = true;
    private string? realtimeLuckMood = "steady";
    private string? realtimeLuckRenderedMood;
    private int realtimeLuckRenderedPixels;
    private JObject? cachedRecordedLuck;
    private JObject? cachedDailyBest;
    private JObject? cachedDailyWorst;

    private Control BuildVerticalThermometer()
    {
        var chamber = new Grid
        {
            RowDefinitions = new("*,*"),
            Width = 10,
            Height = LuckThermometerHalfHeight * 2
        };
        Grid.SetRow(realtimeLuckWarmBar, 0);
        Grid.SetRow(realtimeLuckColdBar, 1);
        chamber.Children.Add(realtimeLuckWarmBar);
        chamber.Children.Add(realtimeLuckColdBar);

        var plusHalfTick = new Border
        {
            Width = 8, Height = 1,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            Background = MinorTickBrush,
            ZIndex = 2
        };
        Grid.SetRow(plusHalfTick, 0);
        chamber.Children.Add(plusHalfTick);

        var minusHalfTick = new Border
        {
            Width = 8, Height = 1,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            Background = MinorTickBrush,
            ZIndex = 2
        };
        Grid.SetRow(minusHalfTick, 1);
        chamber.Children.Add(minusHalfTick);

        var zeroLine = new Border
        {
            Width = 12, Height = 2,
            HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Center,
            Background = ZeroTickBrush,
            ZIndex = 3
        };
        Grid.SetRowSpan(zeroLine, 2);
        chamber.Children.Add(zeroLine);

        var tube = new Border
        {
            Width = 12,
            Height = LuckThermometerHalfHeight * 2 + 2,
            CornerRadius = new CornerRadius(6),
            Background = TubeTrackBrush,
            BorderBrush = TubeBorderBrush,
            BorderThickness = new Thickness(1),
            Child = chamber
        };

        var tubeWithBulb = new StackPanel
        {
            Orientation = Orientation.Vertical,
            HorizontalAlignment = HorizontalAlignment.Center,
            Children = { tube, realtimeLuckBulb }
        };

        var scaleColumn = new Grid
        {
            RowDefinitions = new("Auto,*,Auto,*,Auto"),
            Height = LuckThermometerHalfHeight * 2 + 2,
            VerticalAlignment = VerticalAlignment.Top,
            Margin = new Thickness(0, 0, 5, 0)
        };
        var topTick = new TextBlock { Text = "+30", FontSize = 10, Opacity = 0.62, HorizontalAlignment = HorizontalAlignment.Right };
        var upperMidTick = new TextBlock { Text = "+15", FontSize = 9.5, Opacity = 0.48, VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Right };
        var zeroTick = new TextBlock { Text = "0", FontSize = 10, Opacity = 0.8, VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Right };
        var lowerMidTick = new TextBlock { Text = "-15", FontSize = 9.5, Opacity = 0.48, VerticalAlignment = VerticalAlignment.Center, HorizontalAlignment = HorizontalAlignment.Right };
        var bottomTick = new TextBlock { Text = "-30", FontSize = 10, Opacity = 0.62, VerticalAlignment = VerticalAlignment.Bottom, HorizontalAlignment = HorizontalAlignment.Right };
        Grid.SetRow(topTick, 0);
        Grid.SetRow(upperMidTick, 1);
        Grid.SetRow(zeroTick, 2);
        Grid.SetRow(lowerMidTick, 3);
        Grid.SetRow(bottomTick, 4);
        scaleColumn.Children.Add(topTick);
        scaleColumn.Children.Add(upperMidTick);
        scaleColumn.Children.Add(zeroTick);
        scaleColumn.Children.Add(lowerMidTick);
        scaleColumn.Children.Add(bottomTick);

        var thermometer = new StackPanel
        {
            Name = "PacerRealtimeLuckThermometer",
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center,
            Children = { scaleColumn, tubeWithBulb }
        };
        ToolTip.SetTip(thermometer, "竖向胡度温度计：0.0 为本局配卡理论期望均线，向上暖色代表发牌高于期望（欧），向下冷色代表低于期望（坐牢）。");
        return thermometer;
    }

    private static Border BuildDailyExtremumBadge(string name, string caption, TextBlock valueBlock, IBrush bg, IBrush border, string tip)
    {
        var inner = new StackPanel
        {
            Spacing = 2,
            Children =
            {
                new TextBlock { Text = caption, FontSize = 10.5, Opacity = 0.78 },
                valueBlock
            }
        };
        var badge = new Border
        {
            Name = name,
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(8, 5),
            Background = bg,
            BorderBrush = border,
            BorderThickness = new Thickness(1),
            Child = inner
        };
        ToolTip.SetTip(badge, tip);
        return badge;
    }

    private void AttachMascotScalingWatcher()
    {
        var top = TopLevel.GetTopLevel(realtimeLuckMascot);
        if (!ReferenceEquals(top, realtimeLuckWatchedTopLevel))
        {
            if (realtimeLuckWatchedTopLevel is not null)
                realtimeLuckWatchedTopLevel.ScalingChanged -= OnMascotTopLevelScalingChanged;
            realtimeLuckWatchedTopLevel = top;
            if (top is not null)
                top.ScalingChanged += OnMascotTopLevelScalingChanged;
        }
        RenderRealtimeLuckMascot(realtimeLuckCard?.IsVisible == true);
    }

    private void OnMascotTopLevelScalingChanged(object? sender, EventArgs e)
    {
        RenderRealtimeLuckMascot(realtimeLuckCard?.IsVisible == true);
    }

    private Control BuildRealtimeLuckCard()
    {
        RenderOptions.SetBitmapInterpolationMode(realtimeLuckMascot, BitmapInterpolationMode.HighQuality);
        realtimeLuckMascot.AttachedToVisualTree += (_, _) => AttachMascotScalingWatcher();
        ToolTip.SetTip(realtimeLuckCardScore, "相对本局配卡发牌理论期望的纯 RNG 胡度（0.0 为理论期望均线，剥离残血惩罚与配卡满破贫富差距）。");
        ToolTip.SetTip(realtimeLuckMascot, "奇宝 · 根据本局相对理论期望的正负胡度切换表情");
        realtimeLuckRecentBadge.Child = realtimeLuckRecentTag;
        realtimeLuckStateBadge.Child = realtimeLuckStateTag;

        var titleRow = new WrapPanel
        {
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center,
            Children = { realtimeLuckTitle, realtimeLuckStateBadge }
        };
        var headerGrid = new Grid { ColumnDefinitions = new("*,Auto") };
        Grid.SetColumn(titleRow, 0);
        Grid.SetColumn(realtimeLuckMascot, 1);
        headerGrid.Children.Add(titleRow);
        headerGrid.Children.Add(realtimeLuckMascot);

        var scoreRow = new WrapPanel
        {
            Orientation = Orientation.Horizontal,
            VerticalAlignment = VerticalAlignment.Center,
            Children = { realtimeLuckCardScore, realtimeLuckRecentBadge }
        };

        var dailyGrid = new Grid
        {
            Name = "PacerRealtimeLuckDailyGrid",
            ColumnDefinitions = new("*,*"),
            ColumnSpacing = 8,
            Margin = new Thickness(0, 4, 0, 0)
        };
        var bestBadge = BuildDailyExtremumBadge(
            "PacerDailyBestBadge", "今日最胡", realtimeLuckDailyBestText,
            WarmBadgeBgBrush, WarmBadgeBorderBrush, "今日（自然日）已记录对局的最高胡度极值。");
        var worstBadge = BuildDailyExtremumBadge(
            "PacerDailyWorstBadge", "今日最拉", realtimeLuckDailyWorstText,
            ColdBadgeBgBrush, ColdBadgeBorderBrush, "今日（自然日）已记录对局的最低胡度极值。");
        Grid.SetColumn(bestBadge, 0);
        Grid.SetColumn(worstBadge, 1);
        dailyGrid.Children.Add(bestBadge);
        dailyGrid.Children.Add(worstBadge);

        var rightStack = new StackPanel
        {
            Spacing = 6,
            VerticalAlignment = VerticalAlignment.Center,
            Children = { headerGrid, scoreRow, realtimeLuckCardCoverage, realtimeLuckCardJudgment, dailyGrid }
        };

        var layout = new Grid
        {
            ColumnDefinitions = new("Auto,*"),
            ColumnSpacing = 12,
            VerticalAlignment = VerticalAlignment.Center
        };
        var thermometer = BuildVerticalThermometer();
        Grid.SetColumn(thermometer, 0);
        Grid.SetColumn(rightStack, 1);
        layout.Children.Add(thermometer);
        layout.Children.Add(rightStack);

        ApplyNeutralLuckDefaults(isLive: false);
        return layout;
    }

    private Control BuildRealtimeLuckSummary()
    {
        ToolTip.SetTip(realtimeLuckScore, "相对本局配卡发牌理论期望的正负胡度（0.0 为理论期望均线）。");
        realtimeLuckSummary.Children.Add(realtimeLuckScore);
        realtimeLuckSummary.Children.Add(realtimeLuckDailySummary);
        realtimeLuckSummary.Children.Add(realtimeLuckJudgment);
        ApplyNeutralLuckDefaults(isLive: false);
        return realtimeLuckSummary;
    }

    private void UpdateThermometerVisuals(double scoreValue)
    {
        var clamped = Math.Clamp(scoreValue, -LuckThermometerRange, LuckThermometerRange);
        if (clamped >= 0)
        {
            realtimeLuckWarmBar.Height = Math.Round((clamped / LuckThermometerRange) * LuckThermometerHalfHeight, 1);
            realtimeLuckColdBar.Height = 0;
        }
        else
        {
            realtimeLuckWarmBar.Height = 0;
            realtimeLuckColdBar.Height = Math.Round((-clamped / LuckThermometerRange) * LuckThermometerHalfHeight, 1);
        }

        if (scoreValue >= 5.0)
        {
            realtimeLuckCardScore.Foreground = scoreValue >= 15.0 ? HotScoreBrush : WarmScoreBrush;
            realtimeLuckBulb.Background = scoreValue >= 15.0 ? HotScoreBrush : WarmScoreBrush;
        }
        else if (scoreValue <= -5.0)
        {
            realtimeLuckCardScore.Foreground = scoreValue <= -15.0 ? DeepColdScoreBrush : ColdScoreBrush;
            realtimeLuckBulb.Background = scoreValue <= -15.0 ? DeepColdScoreBrush : ColdScoreBrush;
        }
        else
        {
            realtimeLuckCardScore.Foreground = scoreValue > 1.5 ? WarmScoreBrush : (scoreValue < -1.5 ? ColdScoreBrush : NeutralScoreBrush);
            realtimeLuckBulb.Background = NeutralScoreBrush;
        }
    }

    private void RenderDailyExtrema(JObject? bestObj, JObject? worstObj)
    {
        var bestScore = LuckNumber(bestObj?["score"]);
        var bestDates = (int?)bestObj?["scored_dates"] ?? 0;
        if (bestScore.HasValue && bestDates > 0)
        {
            realtimeLuckDailyBestText.Text = $"{bestScore.Value:+0.0;-0.0;0.0} · {bestDates}回合";
            realtimeLuckDailyBestText.Foreground = bestScore.Value >= 15.0 ? HotScoreBrush
                : (bestScore.Value > 0.0 ? WarmScoreBrush : NeutralScoreBrush);
        }
        else
        {
            realtimeLuckDailyBestText.Text = "--";
            realtimeLuckDailyBestText.Foreground = NeutralScoreBrush;
        }

        var worstScore = LuckNumber(worstObj?["score"]);
        var worstDates = (int?)worstObj?["scored_dates"] ?? 0;
        if (worstScore.HasValue && worstDates > 0)
        {
            realtimeLuckDailyWorstText.Text = $"{worstScore.Value:+0.0;-0.0;0.0} · {worstDates}回合";
            realtimeLuckDailyWorstText.Foreground = worstScore.Value <= -15.0 ? DeepColdScoreBrush
                : (worstScore.Value < 0.0 ? ColdScoreBrush : NeutralScoreBrush);
        }
        else
        {
            realtimeLuckDailyWorstText.Text = "--";
            realtimeLuckDailyWorstText.Foreground = NeutralScoreBrush;
        }

        var shortBest = bestScore.HasValue && bestDates > 0 ? $"{bestScore.Value:+0.0;-0.0;0.0}" : "--";
        var shortWorst = worstScore.HasValue && worstDates > 0 ? $"{worstScore.Value:+0.0;-0.0;0.0}" : "--";
        realtimeLuckDailySummary.Text = $"今日最胡 {shortBest} · 今日最拉 {shortWorst}";
        realtimeLuckDailySummary.IsVisible = true;
    }

    private void ApplyNeutralLuckDefaults(bool isLive)
    {
        realtimeLuckTitle.Text = "本局胡度";
        realtimeLuckStateTag.Text = "";
        realtimeLuckStateBadge.IsVisible = false;
        realtimeLuckCardScore.Text = "0.0";
        UpdateThermometerVisuals(0.0);
        realtimeLuckRecentTag.Text = "";
        realtimeLuckRecentBadge.IsVisible = false;
        realtimeLuckCardCoverage.Text = isLive ? "开局准备中 · 0.0 为理论期望均线" : "待开局 · 0.0 为理论期望均线";
        realtimeLuckScore.Text = isLive ? "本局胡度 0.0 · 开局准备中（0.0 为理论期望均线）" : "本局胡度 0.0 · 待开局（0.0 为理论期望均线）";
        realtimeLuckJudgment.Text = realtimeLuckCardJudgment.Text = "";
        realtimeLuckJudgment.IsVisible = realtimeLuckCardJudgment.IsVisible = false;
        realtimeLuckMood = isLive ? "warming_up" : "steady";
    }

    private void RenderRealtimeLuck(JObject? snapshot)
    {
        var state = (string?)snapshot?["control"]?["state"];
        var isLive = Active.Contains(state ?? "idle");
        var luck = snapshot?["realtime_luck"] as JObject;

        if (luck is not null)
        {
            if (luck.Property("daily_best") is not null)
                cachedDailyBest = luck["daily_best"] as JObject;
            if (luck.Property("daily_worst") is not null)
                cachedDailyWorst = luck["daily_worst"] as JObject;
        }
        RenderDailyExtrema(cachedDailyBest, cachedDailyWorst);

        var status = (string?)luck?["status"];
        var dates = (int?)luck?["scored_dates"] ?? 0;
        var score = LuckNumber(luck?["score"]);
        var hasFreshRecorded = status == "recorded" && dates > 0 && score.HasValue;

        if (hasFreshRecorded && luck is not null)
        {
            cachedRecordedLuck = (JObject)luck.DeepClone();
        }

        JObject? displayLuck = null;
        var displayScope = (string?)luck?["display_scope"];
        if (hasFreshRecorded)
        {
            displayLuck = luck;
        }
        else if (!isLive && cachedRecordedLuck is not null)
        {
            displayLuck = cachedRecordedLuck;
            displayScope = ((bool?)cachedRecordedLuck["career_complete"] == true) ? "finished" : "last_run";
        }

        if (displayLuck is null)
        {
            ApplyNeutralLuckDefaults(isLive);
            SetRealtimeLuckVisible(true);
            RenderRealtimeLuckMascot(realtimeLuckCard?.IsVisible == true);
            return;
        }

        var scoreVal = LuckNumber(displayLuck["score"]) ?? 0.0;
        var displayDates = (int?)displayLuck["scored_dates"] ?? 0;
        var formattedScore = scoreVal.ToString("+0.0;-0.0;0.0");
        var recentDelta = LuckNumber(displayLuck["recent_delta"]);
        var isHistorical = !isLive || displayScope is "finished" or "last_run";

        if (isHistorical)
        {
            realtimeLuckTitle.Text = "上局胡度";
            var complete = displayScope == "finished" || (bool?)displayLuck["career_complete"] == true;
            realtimeLuckStateTag.Text = complete ? "完赛定格" : "本局定格";
            realtimeLuckStateBadge.IsVisible = true;
        }
        else
        {
            realtimeLuckTitle.Text = "本局胡度";
            realtimeLuckStateTag.Text = "";
            realtimeLuckStateBadge.IsVisible = false;
        }

        realtimeLuckCardScore.Text = formattedScore;
        UpdateThermometerVisuals(scoreVal);

        if (recentDelta.HasValue)
        {
            realtimeLuckRecentTag.Text = $"近5手 {recentDelta.Value:+0.0;-0.0;0.0}";
            realtimeLuckRecentTag.Foreground = recentDelta.Value >= 2.0 ? WarmScoreBrush
                : (recentDelta.Value <= -2.0 ? ColdScoreBrush : NeutralScoreBrush);
            realtimeLuckRecentBadge.IsVisible = true;
        }
        else
        {
            realtimeLuckRecentTag.Text = "";
            realtimeLuckRecentBadge.IsVisible = false;
        }

        var customCoverage = (string?)displayLuck["coverage_label"];
        realtimeLuckCardCoverage.Text = !string.IsNullOrWhiteSpace(customCoverage)
            ? customCoverage!
            : $"较理论期望 · {displayDates} 回合";

        var prefixTitle = isHistorical ? "上局胡度" : "本局胡度";
        realtimeLuckScore.Text = $"{prefixTitle} {formattedScore} · 较理论期望 · {displayDates} 回合";

        var judgment = displayLuck["training_luck"] as JObject;
        var confirmed = (int?)judgment?["scored_trainings"] ?? 0;
        var recorded = (int?)judgment?["recorded_trainings"] ?? 0;
        var trainingScore = LuckNumber(judgment?["score"]);
        realtimeLuckJudgment.IsVisible = confirmed > 0 && confirmed <= recorded && trainingScore.HasValue;
        realtimeLuckJudgment.Text = realtimeLuckJudgment.IsVisible
            ? $"训练判定运 {trainingScore:+0.##;-0.##;0} 分 · 已确认 {confirmed}/{recorded}"
            : "";
        realtimeLuckCardJudgment.Text = realtimeLuckJudgment.Text;
        realtimeLuckCardJudgment.IsVisible = realtimeLuckJudgment.IsVisible;

        var mood = (string?)displayLuck["mood"];
        realtimeLuckMood = mood is "warming_up" or "rough" or "steady" or "lucky" ? mood : "steady";
        SetRealtimeLuckVisible(true);
        RenderRealtimeLuckMascot(realtimeLuckCard?.IsVisible == true);
    }

    private void SetRealtimeLuckVisible(bool visible)
    {
        if (realtimeLuckHasScore == visible) return;
        realtimeLuckHasScore = visible;
        if (realtimeLuckCard is not null) Reflow();
        else realtimeLuckSummary.IsVisible = visible;
    }

    private void RenderRealtimeLuckMascot(bool showSideLuck)
    {
        var key = showSideLuck ? realtimeLuckMood : null;
        var scaling = TopLevel.GetTopLevel(realtimeLuckMascot)?.RenderScaling ?? IconHelper.GetSystemScale();
        var targetPx = Math.Clamp((int)Math.Round(LuckMascotDipSize * scaling), 32, 192);
        if (realtimeLuckRenderedMood == key && realtimeLuckRenderedPixels == targetPx) return;
        realtimeLuckRenderedMood = key;
        realtimeLuckRenderedPixels = targetPx;

        Bitmap? crisp = null;
        if (key is not null)
        {
            var cacheKey = $"{key}:{targetPx}";
            if (!realtimeLuckCrispIcons.TryGetValue(cacheKey, out crisp))
            {
                if (!realtimeLuckIcons.TryGetValue(key, out var raw))
                {
                    var path = Path.Combine(AppContext.BaseDirectory, "resource", "pacer", "luck-mascot", key + ".png");
                    try
                    {
                        if (File.Exists(path))
                        {
                            using var fs = File.OpenRead(path);
                            raw = UmaIconRendering.LoadBestBitmap(fs);
                        }
                    }
                    catch (Exception) { /* The readout remains useful without this local image. */ }
                    realtimeLuckIcons[key] = raw;
                }
                if (raw is not null)
                {
                    crisp = UmaIconRendering.ScaleCrisp(raw, targetPx, 0.56);
                }
                realtimeLuckCrispIcons[cacheKey] = crisp;
            }
        }
        realtimeLuckMascot.Source = crisp;
        realtimeLuckMascot.Tag = crisp is null ? null : key;
        realtimeLuckMascot.IsVisible = crisp is not null;
    }

    private static double? LuckNumber(JToken? value)
    {
        if (value?.Type is not (JTokenType.Integer or JTokenType.Float)) return null;
        var number = value.Value<double>();
        return double.IsFinite(number) ? number : null;
    }
}
