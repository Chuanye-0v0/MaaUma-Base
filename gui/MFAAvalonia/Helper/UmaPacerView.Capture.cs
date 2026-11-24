using System;
using System.Linq;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    private static readonly IBrush CaptureFastBgBrush = new SolidColorBrush(Color.Parse("#1A22C55E"));
    private static readonly IBrush CaptureFastBorderBrush = new SolidColorBrush(Color.Parse("#4022C55E"));
    private static readonly IBrush CaptureFastDotBrush = new SolidColorBrush(Color.Parse("#16A34A"));
    private static readonly IBrush CaptureSlowBgBrush = new SolidColorBrush(Color.Parse("#1AF59E0B"));
    private static readonly IBrush CaptureSlowBorderBrush = new SolidColorBrush(Color.Parse("#40F59E0B"));
    private static readonly IBrush CaptureSlowDotBrush = new SolidColorBrush(Color.Parse("#F59E0B"));
    private static readonly IBrush CaptureNeutralBgBrush = new SolidColorBrush(Color.Parse("#14888888"));
    private static readonly IBrush CaptureNeutralBorderBrush = new SolidColorBrush(Color.Parse("#33888888"));

    private readonly Border captureStatusDot = new()
    {
        Width = 8,
        Height = 8,
        CornerRadius = new CornerRadius(4),
        VerticalAlignment = VerticalAlignment.Center,
        Background = NeutralScoreBrush,
    };

    private readonly TextBlock captureStatusText = new()
    {
        Name = "PacerCaptureStatusText",
        Text = "截图通道：尚未检测（可点击右侧测速，不启动育成）",
        FontSize = 12,
        TextWrapping = TextWrapping.Wrap,
        VerticalAlignment = VerticalAlignment.Center,
    };

    private readonly Button probeCaptureButton = new()
    {
        Name = "PacerProbeCapture",
        Content = "检测截图延迟",
        Height = 28,
        MinHeight = 28,
        Padding = new Thickness(10, 3),
        FontSize = 12,
        VerticalAlignment = VerticalAlignment.Center,
    };

    private Border? captureStatusBadge;
    private bool probeCaptureBusy;
    private bool preferManualProbeDisplay;
    private JObject? probeCaptureResult;
    private string? probeCaptureEndpoint;
    private string? probeCaptureError;

    private Control BuildCaptureLatencyBar()
    {
        probeCaptureButton.Click += async (_, _) => await ProbeCaptureAsync();
        var row = new Grid
        {
            ColumnDefinitions = new("Auto,*,Auto"),
            ColumnSpacing = 8,
        };
        row.Children.Add(captureStatusDot);
        Grid.SetColumn(captureStatusText, 1);
        row.Children.Add(captureStatusText);
        Grid.SetColumn(probeCaptureButton, 2);
        row.Children.Add(probeCaptureButton);

        captureStatusBadge = new Border
        {
            Name = "PacerCaptureStatusBadge",
            CornerRadius = new CornerRadius(8),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(10, 6),
            Background = CaptureNeutralBgBrush,
            BorderBrush = CaptureNeutralBorderBrush,
            Child = row,
        };
        return captureStatusBadge;
    }

    private async Task ProbeCaptureAsync()
    {
        if (probeCaptureBusy)
            return;
        var current = context();
        var state = (string?)snapshot?["control"]?["state"] ?? "idle";
        if (Active.Contains(state) || string.IsNullOrWhiteSpace(current.Endpoint))
            return;

        probeCaptureBusy = true;
        probeCaptureEndpoint = current.Endpoint;
        probeCaptureError = null;
        probeCaptureButton.Content = "检测中……";
        Render(current);

        try
        {
            probeCaptureResult = await bridge.Request(new JObject { ["action"] = "probe_device" });
            preferManualProbeDisplay = true;
        }
        catch (Exception ex)
        {
            probeCaptureResult = null;
            probeCaptureError = ex.Message;
            preferManualProbeDisplay = true;
        }
        finally
        {
            probeCaptureBusy = false;
            probeCaptureButton.Content = "检测截图延迟";
            Render(context());
        }
    }

    private void RenderCaptureLatency(UmaPacerContext current, JObject? snap)
    {
        var state = (string?)snap?["control"]?["state"] ?? "idle";
        var active = Active.Contains(state);
        if (probeCaptureEndpoint != null && probeCaptureEndpoint != current.Endpoint)
        {
            probeCaptureResult = null;
            probeCaptureError = null;
            probeCaptureEndpoint = null;
            preferManualProbeDisplay = false;
        }
        if (active)
            preferManualProbeDisplay = false;

        probeCaptureButton.IsEnabled = !active && !submitting && !presetBusy && !probeCaptureBusy
            && !string.IsNullOrWhiteSpace(current.Endpoint);

        var rc = snap?["runtime_capture"] as JObject;
        JObject? displayObj = null;
        var isLiveRuntime = false;

        if (probeCaptureBusy)
        {
            SetCaptureBadge(
                "正在检测模拟器截图通道与延迟……",
                null,
                CaptureNeutralBgBrush,
                CaptureNeutralBorderBrush,
                NeutralScoreBrush);
            return;
        }

        if (active && rc != null)
        {
            displayObj = rc;
            isLiveRuntime = true;
        }
        else if (preferManualProbeDisplay && (probeCaptureResult != null || !string.IsNullOrEmpty(probeCaptureError)))
        {
            displayObj = probeCaptureResult;
        }
        else if (rc != null)
        {
            displayObj = rc;
            isLiveRuntime = true;
        }
        else if (probeCaptureResult != null)
        {
            displayObj = probeCaptureResult;
        }

        if (displayObj != null)
        {
            var ok = (bool?)displayObj["ok"] ?? true;
            var fallback = (bool?)displayObj["fallback"] == true;
            var isSlow = (bool?)displayObj["is_slow"] == true;
            var msg = (string?)displayObj["message"] ?? (isLiveRuntime ? "最近训练截图：状态已记录" : "截图通道检测完成");
            var slowOrWarn = !ok || fallback || isSlow;

            string? tip = null;
            if (!isLiveRuntime)
            {
                var first = displayObj["first_frame_ms"];
                var samples = displayObj["capture_ms"] is JArray arr && arr.Count > 0
                    ? string.Join(", ", arr.Select(v => v?.ToString() + "ms"))
                    : null;
                var nativeFlag = displayObj["native_available"]?.ToString();
                var reasons = displayObj["fallback_reasons"] is JArray fr && fr.Count > 0
                    ? string.Join("; ", fr.Select(v => v?.ToString()))
                    : null;
                tip = (first != null ? $"首帧 {first}ms" : "")
                    + (samples != null ? $" | 采样 [{samples}]" : "")
                    + (!string.IsNullOrEmpty(nativeFlag) ? $" | native={nativeFlag}" : "")
                    + (!string.IsNullOrEmpty(reasons) ? $" | 回退原因：{reasons}" : "");
            }
            if (slowOrWarn)
            {
                const string advise = "检测到慢速或兼容截图通道（约 1 秒以上/页可能漏判充能闪烁帧）；建议检查 MuMu / 雷电原生高速截图通道或以管理员身份运行。";
                tip = string.IsNullOrWhiteSpace(tip) ? advise : tip + "\n" + advise;
            }

            SetCaptureBadge(
                msg,
                string.IsNullOrWhiteSpace(tip) ? null : tip,
                slowOrWarn ? CaptureSlowBgBrush : CaptureFastBgBrush,
                slowOrWarn ? CaptureSlowBorderBrush : CaptureFastBorderBrush,
                slowOrWarn ? CaptureSlowDotBrush : CaptureFastDotBrush);
            return;
        }

        if (!string.IsNullOrWhiteSpace(probeCaptureError))
        {
            SetCaptureBadge(
                "截图检测失败：" + probeCaptureError,
                null,
                CaptureSlowBgBrush,
                CaptureSlowBorderBrush,
                CaptureSlowDotBrush);
            return;
        }

        SetCaptureBadge(
            "截图通道：尚未检测（可点击右侧测速，不启动育成）",
            null,
            CaptureNeutralBgBrush,
            CaptureNeutralBorderBrush,
            NeutralScoreBrush);
    }

    private void SetCaptureBadge(string text, string? tip, IBrush bg, IBrush border, IBrush dot)
    {
        captureStatusText.Text = text;
        captureStatusDot.Background = dot;
        if (captureStatusBadge != null)
        {
            captureStatusBadge.Background = bg;
            captureStatusBadge.BorderBrush = border;
            ToolTip.SetTip(captureStatusBadge, tip);
        }
    }
}
