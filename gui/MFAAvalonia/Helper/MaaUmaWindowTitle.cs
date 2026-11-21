using System;
using System.IO;
using Avalonia.Controls;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Display installed component and base identities without querying a service.</summary>
public static class MaaUmaWindowTitle
{
    private static JObject? ReadObject(string path)
    {
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete);
            if (stream.Length > 4 * 1024 * 1024) return null;
            using var reader = new StreamReader(stream);
            return JObject.Parse(reader.ReadToEnd());
        }
        catch { return null; }
    }

    private static string? Text(JObject? value, string key) =>
        value?[key] is JValue { Type: JTokenType.String } item ? item.Value<string>() : null;

    private static string? VersionText(JObject? value, string key)
    {
        var text = Text(value, key)?.Trim();
        return text is { Length: <= 40 } && Version.TryParse(text, out _) ? text : null;
    }

    public static string Resolve(string packageRoot, string? productTitle = null)
    {
        var product = string.IsNullOrWhiteSpace(productTitle) ? "MaaUma Pacer版" : productTitle.Trim();
        var suffix = product.LastIndexOf(" v", StringComparison.OrdinalIgnoreCase);
        if (suffix > 0 && Version.TryParse(product[(suffix + 2)..], out _))
            product = product[..suffix];

        var manifest = ReadObject(Path.Combine(packageRoot, "public-package-manifest.json"));
        var modules = ReadObject(Path.Combine(packageRoot, "config", "maauma-modules.json"));
        var edition = ReadObject(Path.Combine(packageRoot, "maauma-edition.json"));
        var components = modules?["components"] as JObject;
        var pacer = components?["pacer"] as JObject;
        var baseVersion = VersionText(manifest, "version") ?? VersionText(modules, "base_version")
            ?? VersionText(edition, "base_version");
        var pacerVersion = VersionText(pacer, "version");
        if (pacerVersion is null && Text(edition, "edition") == "enhanced"
            && VersionText(edition, "base_version") is not null)
            pacerVersion = VersionText(edition, "version");

        var name = product.Contains("Pacer", StringComparison.OrdinalIgnoreCase)
            ? product : product + " · Pacer";
        return name + (pacerVersion is not null ? " v" + pacerVersion : " · 组件版本待确认")
            + (baseVersion is not null ? " · 基础 v" + baseVersion : " · 基础版本待确认");
    }

    public static void Apply(Window window, string packageRoot)
    {
        var text = Resolve(packageRoot, window.Title);
        window.Title = text;
        if (window.FindControl<TextBlock>("MaaUmaProductTitle") is { } label)
            label.Text = text;
    }
}
