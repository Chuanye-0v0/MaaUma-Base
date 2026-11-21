using System;
using System.Diagnostics;

namespace MFAAvalonia.Helper;

/// <summary>Short-lived presentation between authoritative runtime snapshots.</summary>
internal sealed class MaaUmaPacerDisplayClock
{
    public const double FreshnessLimitSeconds = 10;
    private readonly Func<double> monotonic;
    private double anchor;
    private double? heldDelta;
    private double? active;
    private double? paused;
    private double? elapsed;
    private string phase = "";
    private bool unavailable;
    private (string? Scope, string? State, double? Active, double? Paused, double? Elapsed)? observed;

    public MaaUmaPacerDisplayClock(Func<double>? monotonic = null)
    {
        this.monotonic = monotonic ?? (() => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency);
    }

    public void Observe(string? scope, string? state, double? activeSeconds,
        double? pausedSeconds, double? elapsedSeconds)
    {
        var value = (scope, state, Valid(activeSeconds), Valid(pausedSeconds), Valid(elapsedSeconds));
        // A repeated cached response is not a newer timing measurement.
        if (observed.HasValue && observed.Value == value) return;
        observed = value;
        anchor = monotonic();
        heldDelta = null;
        unavailable = false;
        active = value.Item3;
        paused = value.Item4;
        elapsed = value.Item5;
        phase = !string.IsNullOrEmpty(scope) && active.HasValue && paused.HasValue && elapsed.HasValue
            ? state ?? "" : "";
    }

    public void Freeze()
    {
        heldDelta ??= Delta();
        unavailable = true;
    }

    private double Delta() => Math.Min(FreshnessLimitSeconds,
        Math.Max(0, monotonic() - anchor));

    public Reading Read()
    {
        var running = phase is "starting" or "running" or "pause_requested" or "resume_requested" or "stop_requested";
        var waiting = phase == "paused";
        var delta = heldDelta ?? Delta();
        var stale = (running || waiting) && (unavailable || monotonic() - anchor >= FreshnessLimitSeconds);
        return new Reading(active + (running ? delta : 0), paused + (waiting ? delta : 0),
            elapsed + (running || waiting ? delta : 0), stale);
    }

    private static double? Valid(double? value) => value.HasValue && double.IsFinite(value.Value)
        && value.Value < TimeSpan.MaxValue.TotalSeconds ? Math.Max(0, value.Value) : null;

    internal readonly record struct Reading(double? ActiveSeconds, double? PausedSeconds,
        double? ElapsedSeconds, bool WaitingForSnapshot);
}
