using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public enum PurityState
{
    Pure = 0,
    EquivalentPure = 1,
    Custom = 2,
    Stale = 3,
    Unknown = 4
}

[Flags]
public enum PurityLayer
{
    None = 0,
    L0Revision = 1,
    L1Attribute = 2,
    L2Base = 4,
    L3Tuning = 8,
    L4Workflow = 16,
    DecisionPreferences = L2Base | L3Tuning | L4Workflow,
    All = L1Attribute | L2Base | L3Tuning | L4Workflow
}

public sealed record PurityItem(
    PurityLayer Layer,
    string Path,
    string Label,
    string ActualText,
    string DefaultText,
    bool Effective
);

public sealed record PurityReport(
    string StepId,
    string StepLabel,
    string Kind,
    string? Revision,
    PurityState State,
    IReadOnlyList<PurityItem> Items,
    string? StaleReason,
    ProvenanceEntry? Provenance
)
{
    public bool IsPure => State is PurityState.Pure or PurityState.EquivalentPure;
    public int EffectiveCount => Items.Count(i => i.Effective);
    public bool HasLayer(PurityLayer layer) => Items.Any(i => i.Layer == layer && i.Effective);
    public string Emoji => State switch
    {
        PurityState.Stale => "🔴",
        PurityState.Custom => "🟠",
        PurityState.Unknown => "⚪",
        _ => "🟢"
    };
}

/// <summary>Pure functions classifying L0..L4 scoring overrides and purifying profile drafts.</summary>
public static class PacerPurity
{
    public static string FamilyOf(string? revision) => StrategyPackCatalog.FamilyOf(revision);

    public static bool SameFamily(string? leftRev, string? rightRev) =>
        !string.IsNullOrWhiteSpace(leftRev)
        && !string.IsNullOrWhiteSpace(rightRev)
        && FamilyOf(leftRev) == FamilyOf(rightRev);

    internal static JObject? ResolveOptionsNode(JObject? policy, string kind)
    {
        if (policy == null) return null;
        return kind == "cut" ? policy["cut_options"] as JObject : policy["career_options"] as JObject;
    }

    internal static JObject? ReadPolicy(JObject? options, string kind) =>
        options?["larc_policy"] as JObject
        ?? (kind == "cut" ? options?["cut"]?["larc_policy"] as JObject : null);

    internal static JObject? ReadDecisionPreferences(JObject? options, string kind)
    {
        var policy = ReadPolicy(options, kind);
        return (kind == "cut" ? options?["cut"]?["decision_preferences"] as JObject : null)
            ?? options?["decision_preferences"] as JObject
            ?? policy?["decision_preferences"] as JObject;
    }

    private static string DisplayFactorName(string key) => key switch
    {
        "speed" => "速度偏好",
        "stamina" => "耐力偏好",
        "power" => "力量偏好",
        "guts" => "根性偏好",
        "wisdom" => "智力偏好",
        "numeric" => "数值收益",
        "bond" => "普通支援羁绊",
        "charge" => "充电进度",
        "gold_ship_bonus" => "黄金船额外分",
        "mei_bonus" => "黄帽额外分",
        _ => key
    };

    private static double DefaultAttributeWeight(string key, string? revision) =>
        StrategyPackCatalog.DefaultWeight(key, revision);

    public static PurityReport EvaluateStep(
        string stepId,
        string stepLabel,
        string kind,
        JObject? policyOptions,
        ProvenanceEntry? provenance = null)
    {
        var items = new List<PurityItem>();
        var larcToken = policyOptions?["larc_policy"] ?? (kind == "cut" ? policyOptions?["cut"]?["larc_policy"] : null);
        var larcPolicy = larcToken as JObject;
        var prefs = ReadDecisionPreferences(policyOptions, kind);

        string? rev = null;
        var unknown = false;
        if (policyOptions == null || larcToken == null)
        {
            unknown = true;
        }
        else if (larcToken.Type == JTokenType.Null)
        {
            // Newly created blank workflow document (`larc_policy: null`) runs factory defaults cleanly.
            rev = null;
        }
        else if (larcPolicy == null || !string.Equals((string?)larcPolicy["variant"], "baseline", StringComparison.OrdinalIgnoreCase))
        {
            unknown = true;
        }
        else
        {
            rev = larcPolicy["revision"]?.Type == JTokenType.String ? (string?)larcPolicy["revision"] : null;
            if (string.IsNullOrEmpty(rev) || Array.IndexOf(UmaPacerView.StrategyRevisions, rev) < 0)
            {
                items.Add(new PurityItem(
                    PurityLayer.L0Revision,
                    "larc_policy.revision",
                    "历史/未登记策略版本",
                    rev ?? "空",
                    UmaPacerView.DefaultStrategyRevision,
                    Effective: true));
            }
        }

        // L1: Attribute weights & after_target_credit
        if (larcPolicy?["attribute_weights"] is JObject weights && weights.Count > 0 && !UmaPacerView.IsDefaultWeightsForRevision(weights, rev))
        {
            foreach (var prop in weights.Properties())
            {
                if (prop.Value is not JValue val || val.Type is not (JTokenType.Float or JTokenType.Integer)) continue;
                var defW = DefaultAttributeWeight(prop.Name, rev);
                if (Math.Abs(val.Value<double>() - defW) > 1e-4)
                {
                    items.Add(new PurityItem(
                        PurityLayer.L1Attribute,
                        "larc_policy.attribute_weights." + prop.Name,
                        DisplayFactorName(prop.Name),
                        val.Value<double>().ToString("0.###", CultureInfo.InvariantCulture),
                        defW.ToString("0.###", CultureInfo.InvariantCulture),
                        Effective: true));
                }
            }
        }

        if (larcPolicy?["after_target_credit"] is JValue creditVal && creditVal.Type is JTokenType.Float or JTokenType.Integer)
        {
            var defC = UmaPacerView.DefaultAfterTargetCreditForRevision(rev);
            if (Math.Abs(creditVal.Value<double>() - defC) > 1e-5)
            {
                items.Add(new PurityItem(
                    PurityLayer.L1Attribute,
                    "larc_policy.after_target_credit",
                    "达标后保留比例",
                    creditVal.Value<double>().ToString("0.###", CultureInfo.InvariantCulture),
                    defC.ToString("0.###", CultureInfo.InvariantCulture),
                    Effective: true));
            }
        }

        // L2: Base multipliers & flat bonuses
        var meiDefault = UmaDecisionPreferences.UsesBFamilyPreferences(rev) ? 0d : 5d;
        foreach (var (key, defVal) in new[]
        {
            ("numeric", 1d),
            ("bond", 1d),
            ("charge", 1d),
            ("gold_ship_bonus", 0d),
            ("mei_bonus", meiDefault)
        })
        {
            if (prefs?[key] is JValue v && v.Type is JTokenType.Float or JTokenType.Integer && Math.Abs(v.Value<double>() - defVal) > 1e-5)
            {
                items.Add(new PurityItem(
                    PurityLayer.L2Base,
                    "decision_preferences." + key,
                    DisplayFactorName(key),
                    v.Value<double>().ToString("0.###", CultureInfo.InvariantCulture),
                    defVal.ToString("0.###", CultureInfo.InvariantCulture),
                    Effective: true));
            }
        }

        // L3: Detailed tuning & custom formula
        if (prefs?["tuning"] is JObject tuning)
        {
            var mode = (string?)tuning["mode"];
            if (mode == "detailed")
            {
                var nonDefault = (tuning["factors"] as JObject)?.Properties()
                    .Where(p => p.Value is JValue fv && fv.Type is JTokenType.Float or JTokenType.Integer && Math.Abs(fv.Value<double>() - 1d) > 1e-5)
                    .ToArray() ?? Array.Empty<JProperty>();
                if (nonDefault.Length == 0)
                {
                    items.Add(new PurityItem(
                        PurityLayer.L3Tuning,
                        "decision_preferences.tuning",
                        "空细调壳（不影响得分）",
                        "detailed (0 项)",
                        "default",
                        Effective: false));
                }
                else
                {
                    foreach (var f in nonDefault)
                    {
                        items.Add(new PurityItem(
                            PurityLayer.L3Tuning,
                            "decision_preferences.tuning.factors." + f.Name,
                            "细调 · " + f.Name,
                            f.Value.Value<double>().ToString("0.###", CultureInfo.InvariantCulture),
                            "1",
                            Effective: true));
                    }
                }
            }
            else if (mode == "formula")
            {
                var formula = ((string?)tuning["formula"] ?? "").Trim();
                var isDefaultFormula = string.IsNullOrEmpty(formula) || string.Equals(formula, "base_score", StringComparison.Ordinal);
                items.Add(new PurityItem(
                    PurityLayer.L3Tuning,
                    "decision_preferences.tuning.formula",
                    isDefaultFormula ? "空公式壳（不影响得分）" : "自写评分公式",
                    isDefaultFormula ? "base_score" : formula,
                    "base_score",
                    Effective: !isDefaultFormula));
            }
        }

        // L4: Stage workflow
        if (prefs?["workflow"] is JObject wf && wf["enabled"]?.Type == JTokenType.Boolean && (bool)wf["enabled"]!)
        {
            var stageCount = (wf["stages"] as JArray)?.Count ?? 0;
            items.Add(new PurityItem(
                PurityLayer.L4Workflow,
                "decision_preferences.workflow.enabled",
                "分年阶段表",
                $"启用 ({stageCount} 段)",
                "关闭",
                Effective: true));
        }

        string? staleReason = null;
        if (items.Any(i => i.Layer == PurityLayer.L0Revision))
        {
            staleReason = $"策略版本「{rev ?? "未指定"}」未在当前策略目录中注册，建议切至 {UmaPacerView.DefaultStrategyRevision}。";
        }
        else if (provenance != null && !string.IsNullOrWhiteSpace(rev) && !string.IsNullOrWhiteSpace(provenance.BaselineRevision) && !SameFamily(provenance.BaselineRevision, rev) && items.Any(i => i.Effective))
        {
            staleReason = $"当前覆盖源自旧基线（{provenance.BaselineRevision}），与当前策略 {rev} 跨族叠加。";
        }
        else if (provenance?.CarriedAcrossSwitch == true && items.Any(i => i.Effective))
        {
            staleReason = "切换策略版本时保留了先前版本的自定义覆盖，可能出现评分叠加异常。";
        }

        PurityState state;
        if (staleReason != null)
            state = PurityState.Stale;
        else if (unknown && !items.Any(i => i.Effective))
            state = PurityState.Unknown;
        else if (items.Any(i => i.Effective))
            state = PurityState.Custom;
        else if (items.Count > 0)
            state = PurityState.EquivalentPure;
        else
            state = PurityState.Pure;

        return new PurityReport(stepId, stepLabel, kind, rev, state, items, staleReason, provenance);
    }

    public static IReadOnlyList<PurityReport> EvaluateDocument(
        JObject? document,
        Func<string, ProvenanceEntry?>? provenanceLookup = null)
    {
        if (document == null) return Array.Empty<PurityReport>();
        var nodes = (document["run_graph"]?["nodes"] as JArray ?? new()).OfType<JObject>()
            .Where(n => (string?)n["kind"] is "career" or "cut")
            .ToArray();
        var policies = document["policies"] as JObject;
        var reports = new List<PurityReport>(nodes.Length);
        foreach (var node in nodes)
        {
            var stepId = (string?)node["id"] ?? "";
            var kind = (string)node["kind"]!;
            var label = (string?)node["label"] ?? (kind == "cut" ? "前期刷切" : "养完育成");
            var policyId = (string?)node["policy"] ?? "default";
            var policy = policies?[policyId] as JObject;
            var options = ResolveOptionsNode(policy, kind);
            reports.Add(EvaluateStep(stepId, label, kind, options, provenanceLookup?.Invoke(stepId)));
        }
        return reports;
    }

    public static (PurityState State, string BadgeText, string DetailText) SummarizeDocument(IReadOnlyList<PurityReport> reports)
    {
        if (reports.Count == 0)
            return (PurityState.Unknown, "⚪ 未配置策略", "当前流程没有培养步骤。");

        var aggregate = reports.Any(r => r.State == PurityState.Stale) ? PurityState.Stale
            : reports.Any(r => r.State == PurityState.Custom) ? PurityState.Custom
            : reports.Any(r => r.State == PurityState.Unknown) ? PurityState.Unknown
            : reports.Any(r => r.State == PurityState.EquivalentPure) ? PurityState.EquivalentPure
            : PurityState.Pure;

        var revText = string.Join(" / ", reports.Select(r => string.IsNullOrWhiteSpace(r.Revision) ? "默认" : r.Revision).Distinct());
        var totalEff = reports.Sum(r => r.EffectiveCount);
        var badge = aggregate switch
        {
            PurityState.Stale => $"🔴 旧版/跨代覆盖 ({totalEff} 项)",
            PurityState.Custom => $"🟠 已自定义 {totalEff} 项",
            PurityState.Unknown => "⚪ 历史/继承策略",
            PurityState.EquivalentPure => $"🟢 官方纯净 · {revText} ⓘ",
            _ => $"🟢 官方纯净 · {revText}"
        };

        var lines = new List<string>();
        foreach (var r in reports)
        {
            var line = $"{r.Emoji} {r.StepLabel}（策略：{r.Revision ?? "默认"}）";
            if (r.StaleReason != null) line += "\n  ⚠ " + r.StaleReason;
            else if (r.State == PurityState.Custom) line += $" — {r.EffectiveCount} 项覆盖：" + string.Join("、", r.Items.Where(i => i.Effective).Take(5).Select(i => $"{i.Label}={i.ActualText}"));
            else if (r.State == PurityState.EquivalentPure) line += " — 存在空细调/空公式壳（未改变分数）";
            else if (r.State == PurityState.Unknown) line += " — 沿用原局或旧分支设置";
            else line += " — 100% 官方纯净预设";
            lines.Add(line);
        }
        return (aggregate, badge, string.Join("\n", lines));
    }

    public static void PurifyStep(JObject? policyOptions, string kind, PurityLayer layers)
    {
        if (policyOptions == null) return;
        var larcPolicy = ReadPolicy(policyOptions, kind);
        var settings = kind == "cut" ? policyOptions["cut"] as JObject : null;

        if ((layers & PurityLayer.L1Attribute) != 0 && larcPolicy != null)
        {
            larcPolicy.Remove("attribute_weights");
            larcPolicy.Remove("after_target_credit");
        }

        void CleanPrefsObject(JObject? prefs)
        {
            if (prefs == null) return;
            if ((layers & PurityLayer.L2Base) != 0)
            {
                prefs.Remove("numeric");
                prefs.Remove("bond");
                prefs.Remove("charge");
                prefs.Remove("gold_ship_bonus");
                prefs.Remove("mei_bonus");
            }
            if ((layers & PurityLayer.L3Tuning) != 0)
            {
                prefs.Remove("tuning");
            }
            if ((layers & PurityLayer.L4Workflow) != 0 && prefs["workflow"] is JObject wf)
            {
                wf["enabled"] = false;
                if (wf["editor_graph"] is JObject graph)
                    graph["enabled"] = false;
            }
        }

        void CleanOwner(JObject? owner)
        {
            if (owner == null || owner["decision_preferences"] is not JObject prefs) return;
            CleanPrefsObject(prefs);
            if (prefs.Count == 0)
                owner.Remove("decision_preferences");
        }

        CleanOwner(settings);
        CleanOwner(policyOptions);
        CleanOwner(larcPolicy);
    }

    public static void PurifyDocument(JObject document, PurityLayer layers, string? onlyStepId = null)
    {
        var nodes = (document["run_graph"]?["nodes"] as JArray ?? new()).OfType<JObject>()
            .Where(n => (string?)n["kind"] is "career" or "cut")
            .Where(n => onlyStepId == null || (string?)n["id"] == onlyStepId);
        var policies = document["policies"] as JObject;
        if (policies == null) return;
        foreach (var node in nodes)
        {
            var kind = (string)node["kind"]!;
            var policyId = (string?)node["policy"] ?? "default";
            if (policies[policyId] is not JObject policy) continue;
            PurifyStep(ResolveOptionsNode(policy, kind), kind, layers);
        }
    }

    public static void StripLegacyMeiDefault(JObject? options, string kind, string? oldRev, string newRev)
    {
        if (options == null
            || UmaDecisionPreferences.UsesBFamilyPreferences(oldRev)
            || !UmaDecisionPreferences.UsesBFamilyPreferences(newRev))
            return;
        foreach (var owner in new[]
        {
            options,
            options["larc_policy"] as JObject,
            kind == "cut" ? options["cut"] as JObject : null
        })
        {
            if (owner?["decision_preferences"] is not JObject prefs) continue;
            if (prefs["mei_bonus"] is JValue mv
                && mv.Type is JTokenType.Float or JTokenType.Integer
                && Math.Abs(mv.Value<double>() - 5d) < 1e-6)
            {
                prefs.Remove("mei_bonus");
                if (prefs.Count == 0)
                    owner.Remove("decision_preferences");
            }
        }
    }

    /// <summary>Shared L1 default cleanup when switching baseline revisions.</summary>
    public static JObject AdoptRevision(JObject policy, string? oldRev, string newRev)
    {
        if (oldRev != newRev)
        {
            if (policy["after_target_credit"] is JValue creditVal
                && creditVal.Type is JTokenType.Float or JTokenType.Integer
                && Math.Abs(creditVal.Value<double>() - UmaPacerView.DefaultAfterTargetCreditForRevision(oldRev)) < 1e-6)
            {
                policy.Remove("after_target_credit");
            }
            if (policy["attribute_weights"] is JObject weights && UmaPacerView.IsDefaultWeightsForRevision(weights, oldRev))
            {
                policy.Remove("attribute_weights");
            }
            if (!UmaDecisionPreferences.UsesBFamilyPreferences(oldRev)
                && UmaDecisionPreferences.UsesBFamilyPreferences(newRev)
                && policy["decision_preferences"] is JObject prefs
                && prefs["mei_bonus"] is JValue mv
                && mv.Type is JTokenType.Float or JTokenType.Integer
                && Math.Abs(mv.Value<double>() - 5d) < 1e-6)
            {
                prefs.Remove("mei_bonus");
                if (prefs.Count == 0)
                    policy.Remove("decision_preferences");
            }
        }
        policy["variant"] = "baseline";
        policy["revision"] = newRev;
        return policy;
    }
}
