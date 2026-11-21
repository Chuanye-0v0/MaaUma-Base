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
    public const string CurrentVersion = "0.5.9";
    private const bool EnhancedBuild = true;
    private static readonly string[] CurrentNotes = {
        "修复领奖页右侧“下一步”被识别区域截掉而反复等待的问题。",
        "日活不再归档过程图或原生识别失败图；保留当前未完成事务必需的临时工作图，完成后清理。",
        "新增可明确选择的1.8.2综合现金规划试用策略，继承1.8.1；输入覆盖不足时沿用同一候选集合的原估值。",
        "保留1.8.1回退，默认仍为1.7.16.1；旧配置和当前育成不自动迁移。",
        "策略估值不保证实战收益；未宣称所有口述卡死或完整实机现金模型均已验证。",
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

    // Announcement edition detection begin.
    private static bool HasEnhancedComponent(string root)
    {
        if (!EnhancedBuild) return false;
        var modules = ReadObject(Path.Combine(root, "config", "maauma-modules.json"), 4 * 1024 * 1024);
        return modules?["components"] is JObject components && components["pacer"] is JObject &&
            Directory.Exists(Path.Combine(root, "pacer", "backend"));
    }
    // Announcement edition detection end.

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
        // Announcement enhanced presentation begin.
        if (HasEnhancedComponent(root)) {
            var enhancedNotes = ReadReleaseNotes(root, version, "enhanced");
            if (enhancedNotes.Length > 0) body.Children.Add(new TextBlock { Name = "MaaUmaAnnouncementEnhancedNotes",
                Text = string.Join("\n\n", enhancedNotes), TextWrapping = TextWrapping.Wrap });
        }
        // Announcement enhanced presentation end.
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
