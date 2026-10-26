using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using SukiUI.Controls;

namespace MFAAvalonia.Helper;

/// <summary>Local release notes. Reading or dismissing them never invokes a task or service.</summary>
public static class MaaUmaUpdateAnnouncement
{
    public const string CurrentVersion = "0.4.41";
    private const bool EnhancedBuild = false;
    private static readonly string[] CurrentNotes = {
        "修复官方DeepSeek的团队及系谱生成，减少输出截断导致的失败。",
        "完整方案继续核验；未完整生成时保留当前草稿。"
    };
    private const string SeenFile = "maauma-update-announcements.json";
    private static readonly Dictionary<string, Window> OpenWindows = new(StringComparer.OrdinalIgnoreCase);
    private static readonly ConditionalWeakTable<Window, object> AttachedOwners = new();

    private static JObject? ReadObject(string path, long maximumBytes = 131072)
    {
        try {
            var file = new FileInfo(path);
            if (!file.Exists || file.Length > maximumBytes) return null;
            return JObject.Parse(File.ReadAllText(path));
        }
        catch { return null; }
    }

    private static string Scalar(JObject? value, string key) =>
        value?[key] is JValue { Type: JTokenType.String } text ? text.Value<string>() ?? "" : "";

    public static string RuntimeVersion(string packageRoot)
    {
        var manifest = ReadObject(Path.Combine(packageRoot, "public-package-manifest.json"), 4 * 1024 * 1024);
        var version = Scalar(manifest, "version");
        return Version.TryParse(version, out _) ? version : CurrentVersion;
    }

    private static string[] ReadReleaseNotes(string root, string version, string edition)
    {
        var metadata = ReadObject(Path.Combine(root, "maauma-edition.json"));
        if (metadata?["release_notes"] is not JObject notes || Scalar(notes, "version") != version ||
            notes[edition] is not JArray entries) return version == CurrentVersion ? CurrentNotes : Array.Empty<string>();
        return entries.Where(value => value.Type == JTokenType.String).Select(value => value.Value<string>() ?? "")
            .Where(value => !string.IsNullOrWhiteSpace(value)).Take(12).ToArray();
    }

    public static bool WasSeen(string packageRoot)
    {
        var state = ReadObject(Path.Combine(packageRoot, "config", SeenFile));
        var key = SeenKey(packageRoot);
        return state?["seen_versions"] is JArray versions && versions.Any(value =>
            value.Type == JTokenType.String && value.Value<string>() == key);
    }

    /// <summary>Recognizes the transaction written by existing 0.4.17 installers.</summary>
    public static bool IsSuccessfulUpgrade(string packageRoot)
    {
        try {
            var root = Path.GetFullPath(packageRoot);
            var state = ReadObject(Path.Combine(root, "updates", "maauma-suite-state.json"));
            if (Scalar(state, "phase") != "installed" || Scalar(state, "purpose") == "updater_only") return false;
            var manifest = ReadObject(Path.Combine(root, "public-package-manifest.json"), 4 * 1024 * 1024);
            var version = Scalar(manifest, "version");
            if (!Version.TryParse(version, out _) || Scalar(state, "base_version") != version) return false;
            // The installed state is the durable transaction result. Its temporary stage may have been cleaned.
            return true;
        }
        catch { return false; }
    }

    public static bool ShouldShowAutomatically(string packageRoot) =>
        IsSuccessfulUpgrade(packageRoot) && !WasSeen(packageRoot);

    private static bool HasEnhancedComponent(string root) => false;

    private static string SeenKey(string root) => RuntimeVersion(root) + (HasEnhancedComponent(root) ? ":enhanced" : ":standard");

    private static void RememberSeen(string root, string seenKey)
    {
        try {
            var directory = Path.Combine(root, "config");
            Directory.CreateDirectory(directory);
            var path = Path.Combine(directory, SeenFile);
            var prior = ReadObject(path);
            var versions = prior?["seen_versions"] is JArray rows
                ? rows.Where(value => value.Type == JTokenType.String).Select(value => value.Value<string>()!)
                    .Where(value => value.Length <= 80).ToHashSet(StringComparer.Ordinal)
                : new HashSet<string>(StringComparer.Ordinal);
            versions.Add(seenKey);
            var state = new JObject { ["schema_version"] = "maauma.announcement_seen.v1", ["seen_versions"] = new JArray(versions.Order()) };
            var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try {
                File.WriteAllText(temporary, state.ToString(Formatting.Indented));
                File.Move(temporary, path, true);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }
        catch { /* A preferences write cannot interrupt running tasks or application shutdown. */ }
    }

    public static Window CreateWindow(string packageRoot, bool afterUpdate = false)
    {
        var root = Path.GetFullPath(packageRoot);
        var version = RuntimeVersion(root);
        var seenKey = SeenKey(root);
        var body = new StackPanel { Margin = new Thickness(26), Spacing = 16 };
        body.Children.Add(new TextBlock { Name = "MaaUmaAnnouncementHeading", Text = $"MaaUma {version} 更新公告",
            FontSize = 24, FontWeight = FontWeight.Bold, TextWrapping = TextWrapping.Wrap });
        body.Children.Add(new TextBlock { Name = "MaaUmaAnnouncementContext", Text = afterUpdate
            ? $"已更新至 {version}。这次更新带来了以下变化：" : "本版本更新内容", TextWrapping = TextWrapping.Wrap });
        var standardNotes = ReadReleaseNotes(root, version, "standard");
        body.Children.Add(new TextBlock { Name = "MaaUmaAnnouncementStandardNotes", Text = standardNotes.Length > 0
            ? string.Join("\n\n", standardNotes) : afterUpdate ? $"已更新至 {version}，具体变更见发布说明。" : $"本版本为 {version}，具体变更见发布说明。",
            TextWrapping = TextWrapping.Wrap });
        body.Children.Add(new TextBlock { Text = "关闭后不再自动提示本版本；可在“系统设置 → 更新 / 反馈 → 更新公告”回看。",
            FontSize = 13, TextWrapping = TextWrapping.Wrap });
        var close = new Button { Name = "MaaUmaAnnouncementClose", Content = "知道了", HorizontalAlignment = HorizontalAlignment.Right };
        body.Children.Add(close);
        var window = new SukiWindow { Name = "MaaUmaUpdateAnnouncement", Title = $"MaaUma {version} 更新公告",
            Width = 690, Height = 430, MinWidth = 480, MinHeight = 320,
            WindowStartupLocation = WindowStartupLocation.CenterOwner, Content = new ScrollViewer { Content = body } };
        var displayed = false;
        window.Opened += (_, _) => displayed = true;
        window.Closed += (_, _) => { if (displayed) RememberSeen(root, seenKey); OpenWindows.Remove(root); };
        close.Click += (_, _) => window.Close();
        return window;
    }

    public static Window ShowCurrent(Window? owner = null, string? packageRoot = null, bool afterUpdate = false)
    {
        var root = Path.GetFullPath(packageRoot ?? AppContext.BaseDirectory);
        if (OpenWindows.TryGetValue(root, out var existing)) { existing.Activate(); return existing; }
        var window = CreateWindow(root, afterUpdate);
        OpenWindows[root] = window;
        try { if (owner?.IsVisible == true) window.Show(owner); else window.Show(); }
        catch { OpenWindows.Remove(root); throw; }
        return window;
    }

    public static void Attach(Window owner, string? packageRoot = null)
    {
        if (AttachedOwners.TryGetValue(owner, out _)) return;
        AttachedOwners.Add(owner, new object());
        var root = Path.GetFullPath(packageRoot ?? AppContext.BaseDirectory);
        Dispatcher.UIThread.Post(() => {
            try { if (owner.IsVisible && ShouldShowAutomatically(root)) ShowCurrent(owner, root, afterUpdate: true); }
            catch { /* Announcement failure must not prevent normal startup. */ }
        }, DispatcherPriority.Loaded);
    }
}
