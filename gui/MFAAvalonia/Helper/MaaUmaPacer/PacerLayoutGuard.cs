using System;

namespace MFAAvalonia.Helper;

/// <summary>
/// Pure hysteresis and deadband helpers preventing SizeChanged / ScrollViewer
/// oscillation loops across responsive Pacer Center panels.
/// </summary>
public static class PacerLayoutGuard
{
    public static bool NextNarrow(double width, double threshold, bool? current, double hysteresis = 24.0)
    {
        if (width <= 0 || double.IsNaN(width) || double.IsInfinity(width))
            return current ?? false;
        var half = Math.Max(0, hysteresis * 0.5);
        if (!current.HasValue)
            return width < threshold;
        return current.Value
            ? width < threshold + half
            : width < threshold - half;
    }

    public static bool NeedsCompactHeight(double availableHeight, bool current, double enterBelow = 410.0, double exitAbove = 470.0)
    {
        if (double.IsNaN(availableHeight) || double.IsInfinity(availableHeight))
            return current;
        return current
            ? availableHeight < exitAbove
            : availableHeight < enterBelow;
    }

    public static bool ChangedBeyond(double next, double last, double tolerance = 2.0) =>
        last < 0 || Math.Abs(next - last) >= tolerance;
}
