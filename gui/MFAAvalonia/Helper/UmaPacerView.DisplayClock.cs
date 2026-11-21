using System;
using Newtonsoft.Json.Linq;
using UmaCutPanel;

namespace MFAAvalonia.Helper;

public sealed partial class UmaPacerView
{
    private readonly MaaUmaPacerDisplayClock displayClock = new();
    private JObject? displayClockSnapshot;
    private bool displayClockVisible;

    private async void RuntimeTimerTick(object? sender, EventArgs args)
    {
        RefreshRuntimeClock();
        await Refresh();
    }

    private void ObserveRuntimeClock(UmaPacerContext current, JObject? runtime)
    {
        displayClockVisible = runtime != null;
        // Form redraws must not renew the age of an old backend snapshot.
        if (!ReferenceEquals(displayClockSnapshot, snapshot))
        {
            displayClockSnapshot = snapshot;
            var scope = string.IsNullOrEmpty(runRoot) ? null
                : runRoot + "|" + ((string?)snapshot?["mode"] ?? Mode)
                    + "|" + ((string?)snapshot?["endpoint"] ?? current.Endpoint);
            displayClock.Observe(scope, (string?)runtime?["state"],
                ClockSeconds(runtime?["active_seconds"]), ClockSeconds(runtime?["paused_seconds"]),
                ClockSeconds(runtime?["elapsed_seconds"]));
        }
        RefreshRuntimeClock();
    }

    private void RefreshRuntimeClock()
    {
        var value = displayClock.Read();
        timing.Text = displayClockVisible
            ? $"本次运行 {ClockDuration(value.ActiveSeconds)}   暂停 {ClockDuration(value.PausedSeconds)}   墙钟 {ClockDuration(value.ElapsedSeconds)}"
                + (value.WaitingForSnapshot ? " · 等待后台计时更新" : "")
            : "本次运行 —   暂停 —   墙钟 —";
    }

    private void FreezeRuntimeClock()
    {
        displayClock.Freeze();
        RefreshRuntimeClock();
    }

    private static double? ClockSeconds(JToken? value) => value?.Type is JTokenType.Integer or JTokenType.Float
        ? value.Value<double>() : null;

    private static string ClockDuration(double? value) => value.HasValue && double.IsFinite(value.Value)
        && value.Value < TimeSpan.MaxValue.TotalSeconds
            ? TimeSpan.FromSeconds(Math.Max(0, value.Value)).ToString(@"hh\:mm\:ss") : "—";
}
