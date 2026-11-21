using System;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public sealed record ProvenanceEntry(
    string Source,
    string? SchemeId,
    string? Title,
    int? SchemeRevision,
    string? BaselineRevision,
    string? AppVersion,
    string AppliedAt,
    bool CarriedAcrossSwitch
);

/// <summary>UI-only provenance sidecar storing weight origin and strategy baseline outside library JSON.</summary>
public sealed class PacerUiProvenanceStore
{
    private readonly string filePath;

    public PacerUiProvenanceStore(string pacerRoot)
    {
        filePath = Path.Combine(Path.GetFullPath(pacerRoot), "ui_state", "weight_provenance.json");
    }

    private JObject ReadRoot()
    {
        try
        {
            if (!File.Exists(filePath)) return new JObject();
            var text = File.ReadAllText(filePath);
            return string.IsNullOrWhiteSpace(text) ? new JObject() : JObject.Parse(text);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException)
        {
            return new JObject();
        }
    }

    private void WriteRoot(JObject root)
    {
        try
        {
            var dir = Path.GetDirectoryName(filePath);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            var tmp = filePath + ".tmp";
            File.WriteAllText(tmp, root.ToString(Formatting.Indented));
            File.Move(tmp, filePath, overwrite: true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Non-critical UI provenance sidecar must never block profile operations.
        }
    }

    private static string Key(string modeId, string profileId, string stepId) =>
        $"{modeId}/{profileId}/{stepId}";

    public ProvenanceEntry? Read(string? modeId, string? profileId, string? stepId)
    {
        if (string.IsNullOrWhiteSpace(modeId) || string.IsNullOrWhiteSpace(profileId) || string.IsNullOrWhiteSpace(stepId))
            return null;
        var root = ReadRoot();
        if (root["entries"] is not JObject entries || entries[Key(modeId, profileId, stepId)] is not JObject row)
            return null;
        return new ProvenanceEntry(
            Source: (string?)row["source"] ?? "unknown",
            SchemeId: (string?)row["scheme_id"],
            Title: (string?)row["title"],
            SchemeRevision: (int?)row["scheme_revision"],
            BaselineRevision: (string?)row["baseline_revision"],
            AppVersion: (string?)row["app_version"],
            AppliedAt: (string?)row["applied_at"] ?? "",
            CarriedAcrossSwitch: (bool?)row["carried_across_switch"] == true
        );
    }

    public void Record(string modeId, string profileId, string stepId, ProvenanceEntry entry)
    {
        if (string.IsNullOrWhiteSpace(modeId) || string.IsNullOrWhiteSpace(profileId) || string.IsNullOrWhiteSpace(stepId))
            return;
        var root = ReadRoot();
        var entries = root["entries"] as JObject ?? (JObject)(root["entries"] = new JObject());
        entries[Key(modeId, profileId, stepId)] = new JObject
        {
            ["source"] = entry.Source,
            ["scheme_id"] = entry.SchemeId,
            ["title"] = entry.Title,
            ["scheme_revision"] = entry.SchemeRevision,
            ["baseline_revision"] = entry.BaselineRevision,
            ["app_version"] = entry.AppVersion,
            ["applied_at"] = entry.AppliedAt,
            ["carried_across_switch"] = entry.CarriedAcrossSwitch
        };
        WriteRoot(root);
    }

    public void MarkCarriedAcrossSwitch(string? modeId, string? profileId, string? stepId, string? previousRevision)
    {
        if (string.IsNullOrWhiteSpace(modeId) || string.IsNullOrWhiteSpace(profileId) || string.IsNullOrWhiteSpace(stepId))
            return;
        var existing = Read(modeId, profileId, stepId);
        var updated = new ProvenanceEntry(
            Source: existing?.Source ?? "local_edit",
            SchemeId: existing?.SchemeId,
            Title: existing?.Title,
            SchemeRevision: existing?.SchemeRevision,
            BaselineRevision: existing?.BaselineRevision ?? previousRevision,
            AppVersion: existing?.AppVersion,
            AppliedAt: DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm"),
            CarriedAcrossSwitch: true
        );
        Record(modeId, profileId, stepId, updated);
    }

    public void Clear(string? modeId, string? profileId, string? stepId = null)
    {
        if (string.IsNullOrWhiteSpace(modeId) || string.IsNullOrWhiteSpace(profileId)) return;
        var root = ReadRoot();
        if (root["entries"] is not JObject entries) return;
        if (!string.IsNullOrWhiteSpace(stepId))
        {
            entries.Remove(Key(modeId, profileId, stepId));
        }
        else
        {
            var prefix = $"{modeId}/{profileId}/";
            foreach (var prop in entries.Properties())
            {
                if (prop.Name.StartsWith(prefix, StringComparison.Ordinal))
                    entries[prop.Name] = null;
            }
            var toRemove = new System.Collections.Generic.List<string>();
            foreach (var prop in entries.Properties())
                if (prop.Value.Type == JTokenType.Null) toRemove.Add(prop.Name);
            foreach (var name in toRemove) entries.Remove(name);
        }
        WriteRoot(root);
    }

    public static (string Label, string? Family, bool Inferred) InferBaseline(JObject? scheme)
    {
        if (scheme == null) return ("未知基线", null, true);
        var explicitRev = (string?)scheme["baseline_revision"]
            ?? (string?)scheme["profile"]?["baseline_revision"];
        if (!string.IsNullOrWhiteSpace(explicitRev))
            return (explicitRev, PacerPurity.FamilyOf(explicitRev), false);
        var appVer = ((string?)scheme["app_version"] ?? "").Trim();
        if (TryParseVer(appVer, out _))
        {
            return ("作者版本 " + appVer, null, true);
        }
        return ("基线未知（发布时未附带版本信息）", null, true);
    }

    private static bool TryParseVer(string text, out Version version)
    {
        version = new Version(0, 0);
        var cleaned = text.TrimStart('v', 'V');
        var dash = cleaned.IndexOf('-');
        if (dash > 0) cleaned = cleaned[..dash];
        return Version.TryParse(cleaned, out version!);
    }
}
