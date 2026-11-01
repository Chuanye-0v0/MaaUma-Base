using System;
using System.Collections.Generic;
using System.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Daily queues use temporary working frames, never MaaFW archives.</summary>
public static class MaaUmaDailyScreenshotPolicy
{
    public static bool AppliesTo(IEnumerable<string?>? taskEntries) =>
        taskEntries?.Any(entry => entry?.StartsWith("Uma", StringComparison.Ordinal) == true) == true;

    // Call after DebugMode as well: some SDK builds may restore its save flags.
    // Callbacks keep the policy testable without starting a native tasker.
    public static void Apply(bool isMaaUmaDaily, Action<bool> saveDraw, Action<bool> saveOnError)
    {
        if (!isMaaUmaDaily) return;
        saveDraw(false);
        saveOnError(false);
    }
}
