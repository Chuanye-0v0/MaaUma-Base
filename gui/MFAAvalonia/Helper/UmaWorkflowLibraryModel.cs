using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Lossless library drafts. Flow topology belongs to the mode; values belong to a configuration.</summary>
public static class UmaWorkflowLibraryModel
{
    public static JObject Resolve(JObject mode, JObject profile)
    {
        var document = (JObject)((JObject?)mode["document"] ?? new JObject()).DeepClone();
        var legacy = profile["document"] as JObject ?? new JObject();
        Merge(document, new JObject(legacy.Properties().Where(p => p.Name is not ("run_graph" or "policies"))
            .Select(p => new JProperty(p.Name, p.Value.DeepClone()))));
        var parameters = profile["node_parameters"] == null ? new JObject() : profile["node_parameters"] as JObject
            ?? throw new InvalidOperationException("节点参数应按节点标识保存为对象。");
        var legacyNodes = new Dictionary<string, JObject>();
        foreach(var node in Nodes(legacy))if((string?)node["id"] is string id)legacyNodes[id] = node;
        foreach(var node in Nodes(document)) {
            var id = (string?)node["id"] ?? "";
            if(legacyNodes.TryGetValue(id, out var oldNode))Overlay(node, Parameters(oldNode));
            if(parameters[id] is not JToken values)continue;
            if(values is not JObject overrides || overrides.Properties().Any(p => Structural(p.Name)))
                throw new InvalidOperationException("配置只能覆盖节点参数，不能改变节点标识、类型或策略引用。");
            Overlay(node, overrides);
        }
        var policies = document["policies"] as JObject ?? throw new InvalidOperationException("工作流需要policies参数对象。");
        var legacyPolicies = legacy["policies"] == null ? new JObject() : legacy["policies"] as JObject
            ?? throw new InvalidOperationException("工作流需要policies参数对象。");
        // A policy ID is a complete snapshot. Recursive merging could revive
        // fields intentionally removed from a configuration, including budgets.
        Overlay(policies, legacyPolicies);
        Overlay(policies, profile["policies"] as JObject ?? throw new InvalidOperationException("命名配置需要policies参数快照。"));
        return document;
    }

    private static IEnumerable<JObject> Nodes(JObject document) =>
        (document["run_graph"]?["nodes"] as JArray ?? new()).OfType<JObject>();
    private static bool Structural(string name) => name is "id" or "kind" or "policy";
    private static JObject Parameters(JObject node) => new(node.Properties().Where(p => !Structural(p.Name))
        .Select(p => new JProperty(p.Name, p.Value.DeepClone())));
    private static void Overlay(JObject target, JObject values)
    { foreach(var value in values.Properties())target[value.Name] = value.Value.DeepClone(); }
    private static JObject EmptyPolicy() => new() { ["career_options"] = new JObject(), ["cut_options"] = new JObject() };

    public static void Merge(JObject target, JObject changes)
    {
        foreach(var property in changes.Properties()) {
            if(property.Value is JObject obj && target[property.Name] is JObject existing)Merge(existing, obj);
            else target[property.Name] = property.Value.DeepClone();
        }
    }

    public static void Capture(JObject mode, JObject profile, JObject draft)
    {
        profile["policies"] = draft["policies"]?.DeepClone() ?? new JObject();
        var extension = profile["document"] as JObject ?? new JObject();
        foreach(var property in draft.Properties().Where(p => p.Name is not ("run_graph" or "policies")))
            extension[property.Name] = property.Value.DeepClone();
        if(extension.Count > 0)profile["document"] = extension;
        var parameters = profile["node_parameters"] as JObject ?? new JObject();
        foreach(var node in Nodes(draft)) {
            var id = (string?)node["id"]; if(id == null)continue;
            parameters[id] = Parameters(node);
        }
        profile["node_parameters"] = parameters;
    }

    /// <summary>
    /// Apply shared topology without committing another configuration's draft.
    /// Saved policies map from saved values; returned drafts map from their own
    /// values. New policy IDs never inherit the editing configuration's budget.
    /// </summary>
    public static IReadOnlyDictionary<string, JObject> RebaseMode(JObject mode, JObject newDocument,
        IReadOnlyDictionary<string, JObject> drafts)
    {
        var original = (JObject)mode.DeepClone();
        var updated = (JObject)mode.DeepClone();
        var document = (JObject)((JObject?)original["document"] ?? new JObject()).DeepClone();
        var graph = (JObject?)(document["run_graph"]?.DeepClone()) ?? new JObject();
        Merge(graph, newDocument["run_graph"] as JObject ?? throw new InvalidOperationException("模式需要执行图。"));
        document["run_graph"] = graph;
        // Only shared graph layout is promoted from the current document.
        // Unknown profile extensions stay in that profile's saved/draft copy.
        if(newDocument["editor"] is JObject layout) {
            var editor = document["editor"] as JObject ?? new JObject();
            foreach(var key in new[] { "view", "positions" })
                if(layout[key] is JToken value)editor[key] = value.DeepClone();
            document["editor"] = editor;
        }
        var defaults = (JObject?)document["policies"] ?? new JObject();
        foreach(var policy in (newDocument["policies"] as JObject ?? new JObject()).Properties())
            if(defaults[policy.Name] == null)defaults[policy.Name] = EmptyPolicy();
        foreach(var node in Nodes(document).Where(UsesPolicy))
            if(defaults[PolicyId(node)] == null)defaults[PolicyId(node)] = EmptyPolicy();
        document["policies"] = defaults;
        updated["document"] = document;

        var modeId = (string?)mode["id"] ?? "";
        foreach(var profile in (updated["profiles"] as JArray ?? new()).OfType<JObject>()) {
            var rebased = RebaseDocument(Resolve(original, profile), document);
            Capture(updated, profile, rebased);
            ArchiveRemovedParameters(profile, document);
        }
        var result = new Dictionary<string, JObject>();
        foreach(var draft in drafts)
            result[draft.Key] = draft.Key.StartsWith(modeId + "/", StringComparison.Ordinal)
                ? RebaseDocument(draft.Value, document) : (JObject)draft.Value.DeepClone();

        // Build everything first so an ambiguous policy mapping leaves the
        // caller's mode and all drafts intact.
        mode.RemoveAll();
        Overlay(mode, updated);
        return result;
    }

    private static bool UsesPolicy(JObject node) => node["policy"] != null || (string?)node["kind"] is "career" or "cut" or "condition";
    private static string PolicyId(JObject node) => (string?)node["policy"] ?? "default";
    private static JObject RebaseDocument(JObject source, JObject modeDocument)
    {
        var document = (JObject)source.DeepClone();
        var graph = (JObject)(modeDocument["run_graph"]?.DeepClone() ?? new JObject());
        if(source["run_graph"] is JObject oldGraph)
            Overlay(graph, new JObject(oldGraph.Properties().Where(p => p.Name is not ("entry" or "nodes" or "edges"))
                .Select(p => new JProperty(p.Name, p.Value.DeepClone()))));
        var sourceNodes = Nodes(source).ToArray();
        var archive = document["archived_node_parameters"] as JObject ?? new JObject();
        var newIds = (graph["nodes"] as JArray ?? new()).OfType<JObject>().Select(n => (string?)n["id"]).ToHashSet();
        foreach(var oldNode in sourceNodes.Where(n => !newIds.Contains((string?)n["id"])))
            if((string?)oldNode["id"] is string oldId)archive[oldId] = Parameters(oldNode);
        if(archive.Count > 0)document["archived_node_parameters"] = archive;
        document["run_graph"] = graph;
        foreach(var node in Nodes(document)) {
            var same = sourceNodes.FirstOrDefault(n => (string?)n["id"] == (string?)node["id"] && (string?)n["kind"] == (string?)node["kind"]);
            if(same != null)Overlay(node, Parameters(same));
        }
        var policies = (JObject?)(source["policies"]?.DeepClone()) ?? new JObject();
        foreach(var group in Nodes(document).Where(UsesPolicy).GroupBy(PolicyId)) {
            if(policies[group.Key] is JObject)continue;
            var mapped = EmptyPolicy();
            var candidates = group.Select(node => {
                var kind = (string?)node["kind"];
                var previous = sourceNodes.FirstOrDefault(n => (string?)n["id"] == (string?)node["id"] && (string?)n["kind"] == kind)
                    ?? sourceNodes.FirstOrDefault(n => (string?)n["kind"] == kind);
                return (kind, policy: previous == null ? null : source["policies"]?[PolicyId(previous)] as JObject);
            }).Where(pair => pair.policy != null).ToArray();
            if(candidates.Length > 0) {
                mapped = (JObject)candidates[0].policy!.DeepClone();
                foreach(var kind in candidates.GroupBy(pair => pair.kind)) {
                    if(kind.Skip(1).Any(pair => !JToken.DeepEquals(pair.policy, kind.First().policy)))
                        throw new InvalidOperationException("多个执行块合并到同一新策略时原配置参数不同；请先为这些执行块保留独立策略。");
                    var field = kind.Key == "cut" ? "cut_options" : kind.Key == "career" ? "career_options" : null;
                    if(field != null)mapped[field] = kind.First().policy![field]?.DeepClone() ?? new JObject();
                }
            }
            policies[group.Key] = mapped;
        }
        // Preserve unreferenced mode policy IDs without importing their values.
        foreach(var policy in (modeDocument["policies"] as JObject ?? new()).Properties())
            if(policies[policy.Name] == null)policies[policy.Name] = EmptyPolicy();
        document["policies"] = policies;
        return document;
    }

    private static void ArchiveRemovedParameters(JObject profile, JObject document)
    {
        if(profile["node_parameters"] is not JObject parameters)return;
        var ids = Nodes(document).Select(node => (string?)node["id"]).ToHashSet();
        var archive = profile["archived_node_parameters"] as JObject ?? new JObject();
        foreach(var item in parameters.Properties().Where(p => !ids.Contains(p.Name)).ToArray()) {
            archive[item.Name] = item.Value.DeepClone();
            item.Remove();
        }
        if(archive.Count > 0)profile["archived_node_parameters"] = archive;
    }

    public static bool SameTopology(JObject first, JObject second)
    {
        JObject Shape(JObject doc) => new() {
            ["entry"] = doc["run_graph"]?["entry"]?.DeepClone(),
            ["edges"] = doc["run_graph"]?["edges"]?.DeepClone(),
            ["nodes"] = new JArray((doc["run_graph"]?["nodes"] as JArray ?? new()).OfType<JObject>()
                .Select(node => new JObject { ["id"] = node["id"]?.DeepClone(), ["kind"] = node["kind"]?.DeepClone(), ["policy"] = node["policy"]?.DeepClone() }))
        };
        return JToken.DeepEquals(Shape(first), Shape(second));
    }

    public static JObject CopyProfile(JObject profile, string name)
    {
        var copy = (JObject)profile.DeepClone(); copy["id"] = "profile_" + Guid.NewGuid().ToString("N"); copy["name"] = name;
        return copy;
    }

    public static JObject CopyMode(JObject mode, JObject profile, JObject draft, string name)
    {
        var copy = (JObject)mode.DeepClone(); copy["id"] = "workflow_" + Guid.NewGuid().ToString("N"); copy["name"] = name;
        copy["document"] = draft.DeepClone(); copy["document"]!["label"] = name;
        var configuration = CopyProfile(profile, (string?)profile["name"] ?? "常用配置");
        Capture(copy, configuration, draft); copy["profiles"] = new JArray(configuration); copy["selected_profile_id"] = configuration["id"]!.DeepClone();
        return copy;
    }

    public static JObject CreateBlankProfile(JObject mode, string name, JObject? newPolicyDefaultsByKind)
    {
        var document = (JObject)((JObject?)mode["document"] ?? new JObject()).DeepClone();
        var policies = (JObject?)(document["policies"]?.DeepClone()) ?? new JObject();
        foreach (var node in Nodes(document).Where(n => (string?)n["kind"] is "career" or "cut"))
        {
            var policyId = (string?)node["policy"] ?? "default";
            var kind = (string)node["kind"]!;
            var policy = policies[policyId] as JObject ?? (JObject)(policies[policyId] = EmptyPolicy());
            var optionKey = kind == "cut" ? "cut_options" : "career_options";
            var optObj = new JObject();
            if (newPolicyDefaultsByKind?[kind] is JObject defPolicy)
            {
                optObj["strategy"] = "baseline";
                optObj["larc_policy"] = defPolicy.DeepClone();
            }
            else
            {
                optObj["larc_policy"] = JValue.CreateNull();
            }
            if (kind == "cut") optObj["cut"] = new JObject();
            policy[optionKey] = optObj;
        }
        return new JObject
        {
            ["id"] = "profile_" + Guid.NewGuid().ToString("N"),
            ["name"] = name,
            ["policies"] = policies,
            ["node_parameters"] = new JObject()
        };
    }

    public static bool RemoveProfile(JObject mode, string profileId, out string? nextSelectedId)
    {
        nextSelectedId = null;
        if (mode["profiles"] is not JArray profiles || profiles.Count <= 1)
            return false;
        var list = profiles.OfType<JObject>().ToList();
        var idx = list.FindIndex(p => string.Equals((string?)p["id"], profileId, StringComparison.Ordinal));
        if (idx < 0) return false;
        list.RemoveAt(idx);
        var nextIdx = Math.Min(idx, list.Count - 1);
        nextSelectedId = (string?)list[nextIdx]["id"];
        profiles.Clear();
        foreach (var item in list) profiles.Add(item);
        if (!string.IsNullOrEmpty(nextSelectedId))
            mode["selected_profile_id"] = nextSelectedId;
        return true;
    }

    // ==================== V2 Build-First Model (Option A: build x mode) ====================

    public const string SchemaV2 = "uma.career_workflow_library.v2";

    public static readonly (string Id, string Name)[] BuiltinModes =
    {
        ("direct_career", "自动育成"),
        ("cut_preserve", "刷切并保留"),
        ("cut_then_career", "出切先停，确认后同局养完"),
        ("ss_sieve_cycle", "SS筛局循环"),
        ("ace_career_cycle", "大赛成品马连开循环"),
    };

    private static readonly string[] SetupKeys =
    {
        "character_id",
        "character_name",
        "validation_character_name",
        "support_deck",
        "borrow_card_id",
        "borrow_min_level",
        "borrow_min_break",
    };

    private static readonly string[] RecipeTopKeys =
    {
        "support_profile",
        "strategy",
        "larc_policy",
        "targets",
        "failure_limit",
        "max_actions",
        "race_retry",
        "running_style",
        "race_running_styles",
        "custom_races",
        "terminal_skills",
        "arc_policy",
        "mid_skill_learning",
        "early_skill_allowlist",
        "mei_breaks",
        "with_ramonu",
        "retain_training",
        "decision_preferences",
        "event_preferences",
        "ai_options",
    };

    private static readonly HashSet<string> CareerPhaseOnlyKeys = new(StringComparer.Ordinal)
    {
        "custom_races",
        "terminal_skills",
        "arc_policy",
        "mid_skill_learning",
        "early_skill_allowlist",
        "mei_breaks",
        "with_ramonu",
    };

    private static readonly HashSet<string> CareerModeOnlyKeys = new(StringComparer.Ordinal)
    {
        "setup",
        "manual_handoff",
        "manual_pause_dates",
        "continuation",
    };

    private static readonly HashSet<string> CutInnerSharedKeys = new(SetupKeys, StringComparer.Ordinal)
    {
        "decision_preferences",
        "event_preferences",
        "ai_options",
    };

    private static readonly HashSet<string> DocReservedKeys = new(StringComparer.Ordinal)
    {
        "schema_version",
        "id",
        "label",
        "template_id",
        "run_graph",
        "policies",
        "editor",
        "career_batch",
    };

    public static bool IsV2(JObject? library) =>
        string.Equals((string?)library?["schema_version"], SchemaV2, StringComparison.Ordinal);

    public static bool IsBuiltinMode(string? modeId) =>
        BuiltinModes.Any(m => string.Equals(m.Id, modeId, StringComparison.Ordinal));

    public static string BuiltinModeName(string? modeId) =>
        BuiltinModes.FirstOrDefault(m => string.Equals(m.Id, modeId, StringComparison.Ordinal)).Name ?? modeId ?? "育成模式";

    public static JObject DefaultModeSettings() => new()
    {
        ["direct_career"] = new JObject
        {
            ["career_batch"] = new JObject { ["max_careers"] = 1 },
        },
        ["cut_preserve"] = new JObject
        {
            ["cut_params"] = new JObject { ["max_attempts"] = 20, ["max_tp_items"] = 0, ["max_carrots"] = 0 },
        },
        ["cut_then_career"] = new JObject
        {
            ["cut_params"] = new JObject { ["max_attempts"] = 20, ["max_tp_items"] = 0, ["max_carrots"] = 0 },
        },
        ["ss_sieve_cycle"] = new JObject
        {
            ["ss_sieve"] = new JObject { ["pre_debut"] = false, ["year1_min"] = 4, ["allow_ready_five"] = false },
            ["cycle_budget"] = new JObject { ["max_attempts"] = 20, ["max_tp_items"] = 0, ["max_carrots"] = 0 },
        },
        ["ace_career_cycle"] = new JObject
        {
            ["ace_cycle"] = new JObject
            {
                ["target_graduates"] = 1,
                ["graduation"] = new JObject
                {
                    ["enabled"] = false,
                    ["min_stats"] = new JObject(),
                    ["min_stat_sum"] = JValue.CreateNull(),
                    ["distances"] = new JObject(),
                },
                ["ss_sieve"] = JValue.CreateNull(),
            },
            ["cycle_budget"] = new JObject { ["max_attempts"] = 10, ["max_tp_items"] = 10, ["max_carrots"] = 0 },
        },
    };

    public static JObject DefaultBuiltinTemplate(string modeId)
    {
        JObject Make(string id, string label, JArray nodes, JArray edges, string initialPhase) => new()
        {
            ["schema_version"] = "uma.career_workflow.v1",
            ["id"] = id,
            ["label"] = label,
            ["template_id"] = id,
            ["run_graph"] = new JObject
            {
                ["entry"] = "start",
                ["nodes"] = nodes,
                ["edges"] = edges,
            },
            ["policies"] = new JObject
            {
                ["default"] = new JObject
                {
                    ["career_options"] = initialPhase == "career" ? new JObject { ["larc_policy"] = JValue.CreateNull() } : new JObject(),
                    ["cut_options"] = initialPhase == "cut" ? new JObject { ["larc_policy"] = JValue.CreateNull() } : new JObject(),
                },
            },
            ["editor"] = new JObject { ["view"] = "standard", ["positions"] = new JObject() },
        };

        return modeId switch
        {
            "cut_preserve" => Make(
                "cut_preserve",
                "刷切并保留",
                new JArray
                {
                    new JObject { ["id"] = "start", ["kind"] = "entry" },
                    new JObject { ["id"] = "screen", ["kind"] = "cut", ["policy"] = "default", ["check_at"] = "cut_checkpoint" },
                    new JObject { ["id"] = "retry", ["kind"] = "restart" },
                    new JObject { ["id"] = "wait", ["kind"] = "hold" },
                },
                new JArray
                {
                    new JObject { ["from"] = "start", ["outcome"] = "next", ["to"] = "screen" },
                    new JObject { ["from"] = "screen", ["outcome"] = "not_matched", ["to"] = "retry" },
                    new JObject { ["from"] = "screen", ["outcome"] = "unknown", ["to"] = "wait" },
                    new JObject { ["from"] = "retry", ["outcome"] = "next", ["to"] = "screen" },
                    new JObject { ["from"] = "screen", ["outcome"] = "matched", ["to"] = "wait" },
                },
                "cut"),
            "cut_then_career" => Make(
                "cut_then_career",
                "出切先停，确认后同局养完",
                new JArray
                {
                    new JObject { ["id"] = "start", ["kind"] = "entry" },
                    new JObject { ["id"] = "screen", ["kind"] = "cut", ["policy"] = "default", ["check_at"] = "cut_checkpoint" },
                    new JObject { ["id"] = "retry", ["kind"] = "restart" },
                    new JObject { ["id"] = "wait", ["kind"] = "hold" },
                    new JObject { ["id"] = "same", ["kind"] = "continue_current" },
                    new JObject { ["id"] = "grow", ["kind"] = "career", ["policy"] = "default" },
                    new JObject { ["id"] = "done", ["kind"] = "finish" },
                },
                new JArray
                {
                    new JObject { ["from"] = "start", ["outcome"] = "next", ["to"] = "screen" },
                    new JObject { ["from"] = "screen", ["outcome"] = "not_matched", ["to"] = "retry" },
                    new JObject { ["from"] = "screen", ["outcome"] = "unknown", ["to"] = "wait" },
                    new JObject { ["from"] = "retry", ["outcome"] = "next", ["to"] = "screen" },
                    new JObject { ["from"] = "screen", ["outcome"] = "matched", ["to"] = "same" },
                    new JObject { ["from"] = "same", ["outcome"] = "next", ["to"] = "grow" },
                    new JObject { ["from"] = "grow", ["outcome"] = "completed", ["to"] = "done" },
                },
                "cut"),
            "ss_sieve_cycle" => Make(
                "ss_sieve_cycle",
                "SS筛局循环",
                new JArray
                {
                    new JObject { ["id"] = "start", ["kind"] = "entry" },
                    new JObject
                    {
                        ["id"] = "grow",
                        ["kind"] = "career",
                        ["policy"] = "default",
                        ["ss_sieve"] = new JObject { ["pre_debut"] = false, ["year1_min"] = 4, ["allow_ready_five"] = false },
                    },
                    new JObject { ["id"] = "done", ["kind"] = "finish" },
                },
                new JArray
                {
                    new JObject { ["from"] = "start", ["outcome"] = "next", ["to"] = "grow" },
                    new JObject { ["from"] = "grow", ["outcome"] = "completed", ["to"] = "done" },
                },
                "career"),
            "ace_career_cycle" => Make(
                "ace_career_cycle",
                "大赛成品马连开循环",
                new JArray
                {
                    new JObject { ["id"] = "start", ["kind"] = "entry" },
                    new JObject
                    {
                        ["id"] = "grow",
                        ["kind"] = "career",
                        ["policy"] = "default",
                        ["ace_cycle"] = DefaultModeSettings()["ace_career_cycle"]!["ace_cycle"]!.DeepClone(),
                    },
                    new JObject { ["id"] = "done", ["kind"] = "finish" },
                },
                new JArray
                {
                    new JObject { ["from"] = "start", ["outcome"] = "next", ["to"] = "grow" },
                    new JObject { ["from"] = "grow", ["outcome"] = "completed", ["to"] = "done" },
                },
                "career"),
            _ => Make(
                "direct_career",
                "自动育成",
                new JArray
                {
                    new JObject { ["id"] = "start", ["kind"] = "entry" },
                    new JObject { ["id"] = "grow", ["kind"] = "career", ["policy"] = "default" },
                    new JObject { ["id"] = "done", ["kind"] = "finish" },
                },
                new JArray
                {
                    new JObject { ["from"] = "start", ["outcome"] = "next", ["to"] = "grow" },
                    new JObject { ["from"] = "grow", ["outcome"] = "completed", ["to"] = "done" },
                },
                "career"),
        };
    }

    private static JToken? StripPhaseHorizon(JToken? larcPolicy, string phase)
    {
        if (larcPolicy is not JObject obj) return larcPolicy?.DeepClone();
        var cleaned = (JObject)obj.DeepClone();
        var defHorizon = phase == "cut" ? "junior_cutoff_discard" : "full_career";
        if (string.Equals((string?)cleaned["ss_planning_horizon"], defHorizon, StringComparison.Ordinal))
            cleaned.Remove("ss_planning_horizon");
        return cleaned;
    }

    private static JObject CleanCycleBudget(JObject? rawCut, JObject defaultsBudget)
    {
        var budget = new JObject();
        if (rawCut != null)
        {
            foreach (var key in new[] { "max_attempts", "max_tp_items", "max_carrots", "refill_operation_limit" })
            {
                if (rawCut.Property(key) == null) continue;
                var val = rawCut[key]!;
                if (key == "max_carrots" && val.Type == JTokenType.Null)
                    budget[key] = 0;
                else
                    budget[key] = val.DeepClone();
            }
        }
        foreach (var prop in defaultsBudget.Properties())
            if (budget.Property(prop.Name) == null)
                budget[prop.Name] = prop.Value.DeepClone();
        return budget;
    }

    private static JObject CompileCareerOptionsV2(JObject recipe, JObject setting, JObject? overrides = null)
    {
        var options = new JObject();
        if (recipe["setup"] is JObject setup)
            options["setup"] = setup.DeepClone();
        foreach (var key in RecipeTopKeys)
        {
            if (recipe.Property(key) == null) continue;
            options[key] = key == "larc_policy"
                ? (StripPhaseHorizon(recipe[key], "career") ?? JValue.CreateNull())
                : recipe[key]!.DeepClone();
        }
        if (recipe["extra_options"] is JObject extra)
        {
            foreach (var p in extra.Properties())
                if (options.Property(p.Name) == null && !CareerModeOnlyKeys.Contains(p.Name))
                    options[p.Name] = p.Value.DeepClone();
        }
        if (setting.Property("manual_handoff") != null)
            options["manual_handoff"] = setting["manual_handoff"]!.DeepClone();
        if (setting.Property("manual_pause_dates") != null)
            options["manual_pause_dates"] = setting["manual_pause_dates"]!.DeepClone();
        if (setting.Property("continuation") != null)
            options["continuation"] = setting["continuation"]!.DeepClone();
        if (overrides != null)
        {
            foreach (var p in overrides.Properties())
                options[p.Name] = p.Name == "larc_policy"
                    ? (StripPhaseHorizon(p.Value, "career") ?? JValue.CreateNull())
                    : p.Value.DeepClone();
        }
        if (options.Property("larc_policy") == null)
            options["larc_policy"] = JValue.CreateNull();
        return options;
    }

    private static JObject CompileCutOptionsV2(JObject recipe, JObject setting)
    {
        var options = new JObject();
        foreach (var key in RecipeTopKeys)
        {
            if (key is "decision_preferences" or "event_preferences" or "ai_options") continue;
            if (CareerPhaseOnlyKeys.Contains(key)) continue;
            if (recipe.Property(key) == null) continue;
            options[key] = key == "larc_policy"
                ? (StripPhaseHorizon(recipe[key], "cut") ?? JValue.CreateNull())
                : recipe[key]!.DeepClone();
        }
        if (recipe["extra_options"] is JObject extra)
        {
            foreach (var p in extra.Properties())
                if (options.Property(p.Name) == null && !CareerPhaseOnlyKeys.Contains(p.Name) && !CareerModeOnlyKeys.Contains(p.Name) && p.Name != "cut")
                    options[p.Name] = p.Value.DeepClone();
        }
        if (options.Property("larc_policy") == null)
            options["larc_policy"] = JValue.CreateNull();

        var cutInner = new JObject();
        if (recipe["setup"] is JObject setup)
        {
            foreach (var k in SetupKeys)
                if (setup.Property(k) != null)
                    cutInner[k] = setup[k]!.DeepClone();
        }
        if (setting["cut_params"] is JObject cutParams)
        {
            foreach (var p in cutParams.Properties())
                if (!CutInnerSharedKeys.Contains(p.Name) && p.Name != "continuation")
                    cutInner[p.Name] = p.Value.DeepClone();
        }
        foreach (var shared in new[] { "decision_preferences", "event_preferences", "ai_options" })
        {
            if (recipe[shared] is { Type: not JTokenType.Null } tok)
                cutInner[shared] = tok.DeepClone();
        }
        if (setting["continuation"] is { Type: not JTokenType.Null } cont)
            cutInner["continuation"] = cont.DeepClone();
        options["cut"] = cutInner;
        return options;
    }

    public static JObject CompileV2(JObject build, string modeId, JObject? builtinTemplates = null)
    {
        if (!IsBuiltinMode(modeId))
            throw new InvalidOperationException("未知内置育成模式：" + modeId);
        var tpl = (builtinTemplates?[modeId] as JObject)?.DeepClone() as JObject ?? DefaultBuiltinTemplate(modeId);
        var recipe = build["recipe"] as JObject ?? new JObject();
        var defAll = DefaultModeSettings();
        var setting = ((build["mode_settings"]?[modeId] as JObject) ?? (defAll[modeId] as JObject))?.DeepClone() as JObject ?? new JObject();

        if (setting["doc_extensions"] is JObject ext)
        {
            foreach (var p in ext.Properties())
                if (!DocReservedKeys.Contains(p.Name))
                    tpl[p.Name] = p.Value.DeepClone();
        }

        var nodes = (tpl["run_graph"]?["nodes"] as JArray ?? new JArray()).OfType<JObject>().ToArray();
        var policies = tpl["policies"] as JObject ?? (JObject)(tpl["policies"] = new JObject());
        var defPolicy = policies["default"] as JObject ?? (JObject)(policies["default"] = EmptyPolicy());

        if (modeId == "direct_career")
        {
            defPolicy["career_options"] = CompileCareerOptionsV2(recipe, setting);
            defPolicy["cut_options"] = new JObject();
            if (setting["career_batch"] is JObject batch)
            {
                var maxC = (int?)batch["max_careers"] ?? 1;
                if (maxC != 1 || batch.Count > 1)
                    tpl["career_batch"] = batch.DeepClone();
            }
        }
        else if (modeId == "cut_preserve")
        {
            defPolicy["cut_options"] = CompileCutOptionsV2(recipe, setting);
            defPolicy["career_options"] = new JObject();
            var screen = nodes.FirstOrDefault(n => (string?)n["id"] == "screen") ?? nodes.FirstOrDefault(n => (string?)n["kind"] == "cut");
            if (screen != null)
            {
                if (setting.Property("screening") != null) screen["screening"] = setting["screening"]!.DeepClone();
                if (setting.Property("check_at") != null) screen["check_at"] = setting["check_at"]!.DeepClone();
            }
        }
        else if (modeId == "cut_then_career")
        {
            defPolicy["cut_options"] = CompileCutOptionsV2(recipe, setting);
            defPolicy["career_options"] = CompileCareerOptionsV2(recipe, setting, setting["grow_overrides"] as JObject);
            var screen = nodes.FirstOrDefault(n => (string?)n["id"] == "screen") ?? nodes.FirstOrDefault(n => (string?)n["kind"] == "cut");
            if (screen != null)
            {
                if (setting.Property("screening") != null) screen["screening"] = setting["screening"]!.DeepClone();
                if (setting.Property("check_at") != null) screen["check_at"] = setting["check_at"]!.DeepClone();
            }
        }
        else if (modeId == "ss_sieve_cycle")
        {
            defPolicy["career_options"] = CompileCareerOptionsV2(recipe, setting);
            var budget = CleanCycleBudget(setting["cycle_budget"] as JObject, (JObject)defAll["ss_sieve_cycle"]!["cycle_budget"]!);
            defPolicy["cut_options"] = new JObject { ["cut"] = budget };
            var grow = nodes.FirstOrDefault(n => (string?)n["id"] == "grow") ?? nodes.FirstOrDefault(n => n.Property("ss_sieve") != null);
            if (grow != null)
                grow["ss_sieve"] = (setting["ss_sieve"] as JObject ?? (JObject)defAll["ss_sieve_cycle"]!["ss_sieve"]!).DeepClone();
        }
        else if (modeId == "ace_career_cycle")
        {
            defPolicy["career_options"] = CompileCareerOptionsV2(recipe, setting);
            var budget = CleanCycleBudget(setting["cycle_budget"] as JObject, (JObject)defAll["ace_career_cycle"]!["cycle_budget"]!);
            defPolicy["cut_options"] = new JObject { ["cut"] = budget };
            var grow = nodes.FirstOrDefault(n => (string?)n["id"] == "grow") ?? nodes.FirstOrDefault(n => n.Property("ace_cycle") != null);
            if (grow != null)
                grow["ace_cycle"] = (setting["ace_cycle"] as JObject ?? (JObject)defAll["ace_career_cycle"]!["ace_cycle"]!).DeepClone();
        }

        return tpl;
    }

    private static void CaptureRecipeFromCareerOptions(JObject careerOpts, JObject recipe, string? onlyPhase = null)
    {
        if (onlyPhase == null)
        {
            if (careerOpts["setup"] is JObject setup)
                recipe["setup"] = setup.DeepClone();
            foreach (var key in RecipeTopKeys)
            {
                if (careerOpts.Property(key) == null)
                    recipe.Remove(key);
                else
                    recipe[key] = key == "larc_policy"
                        ? (StripPhaseHorizon(careerOpts[key], "career") ?? JValue.CreateNull())
                        : careerOpts[key]!.DeepClone();
            }
            var known = new HashSet<string>(RecipeTopKeys, StringComparer.Ordinal);
            foreach (var k in CareerModeOnlyKeys) known.Add(k);
            var extras = new JObject();
            foreach (var p in careerOpts.Properties())
                if (!known.Contains(p.Name))
                    extras[p.Name] = p.Value.DeepClone();
            if (extras.Count > 0) recipe["extra_options"] = extras;
            else recipe.Remove("extra_options");
        }
        else if (onlyPhase == "career_only")
        {
            // Used when editing grow node in cut_then_career (e.g. custom_races, mid_skills)
            foreach (var key in RecipeTopKeys)
            {
                if (!CareerPhaseOnlyKeys.Contains(key) && key is not ("running_style" or "race_running_styles"))
                    continue;
                if (careerOpts.Property(key) == null) recipe.Remove(key);
                else recipe[key] = careerOpts[key]!.DeepClone();
            }
        }
    }

    private static void CaptureRecipeFromCutOptions(JObject cutOpts, JObject recipe)
    {
        var cutInner = cutOpts["cut"] as JObject ?? new JObject();
        var setup = new JObject();
        foreach (var k in SetupKeys)
            if (cutInner.Property(k) != null)
                setup[k] = cutInner[k]!.DeepClone();
        recipe["setup"] = setup;

        foreach (var key in RecipeTopKeys)
        {
            if (key is "decision_preferences" or "event_preferences" or "ai_options")
            {
                var tok = cutOpts[key] ?? cutInner[key];
                if (tok == null) recipe.Remove(key);
                else recipe[key] = tok.DeepClone();
                continue;
            }
            if (CareerPhaseOnlyKeys.Contains(key))
                continue; // Never drop career-only keys (custom_races, arc_policy, mid_skill_learning, etc.) when editing a cut node.
            if (cutOpts.Property(key) == null) recipe.Remove(key);
            else recipe[key] = key == "larc_policy"
                ? (StripPhaseHorizon(cutOpts[key], "cut") ?? JValue.CreateNull())
                : cutOpts[key]!.DeepClone();
        }
    }

    public static void CaptureV2(JObject build, string modeId, JObject editedDoc, string? editedKind = null)
    {
        if (!IsBuiltinMode(modeId))
            throw new InvalidOperationException("未知内置育成模式：" + modeId);
        var recipe = build["recipe"] as JObject ?? (JObject)(build["recipe"] = new JObject { ["setup"] = new JObject() });
        var ms = build["mode_settings"] as JObject ?? (JObject)(build["mode_settings"] = DefaultModeSettings());
        var defAll = DefaultModeSettings();
        var setting = ms[modeId] as JObject ?? (JObject)(ms[modeId] = defAll[modeId]!.DeepClone());

        var defPolicy = editedDoc["policies"]?["default"] as JObject ?? new JObject();
        var careerOpts = defPolicy["career_options"] as JObject ?? new JObject();
        var cutOpts = defPolicy["cut_options"] as JObject ?? new JObject();
        var cutInner = cutOpts["cut"] as JObject ?? new JObject();
        var nodes = (editedDoc["run_graph"]?["nodes"] as JArray ?? new JArray()).OfType<JObject>().ToArray();

        if (modeId == "direct_career")
        {
            CaptureRecipeFromCareerOptions(careerOpts, recipe);
            if (editedDoc["career_batch"] is JObject batch)
                setting["career_batch"] = batch.DeepClone();
            else
                setting["career_batch"] = new JObject { ["max_careers"] = 1 };
            if (careerOpts.Property("manual_handoff") != null) setting["manual_handoff"] = careerOpts["manual_handoff"]!.DeepClone();
            else setting.Remove("manual_handoff");
            if (careerOpts.Property("manual_pause_dates") != null) setting["manual_pause_dates"] = careerOpts["manual_pause_dates"]!.DeepClone();
            else setting.Remove("manual_pause_dates");
            if (careerOpts.Property("continuation") != null) setting["continuation"] = careerOpts["continuation"]!.DeepClone();
            else setting.Remove("continuation");
        }
        else if (modeId == "ss_sieve_cycle")
        {
            CaptureRecipeFromCareerOptions(careerOpts, recipe);
            var grow = nodes.FirstOrDefault(n => (string?)n["id"] == "grow") ?? nodes.FirstOrDefault(n => n.Property("ss_sieve") != null);
            if (grow?["ss_sieve"] is JObject ss) setting["ss_sieve"] = ss.DeepClone();
            setting["cycle_budget"] = CleanCycleBudget(cutInner, (JObject)defAll["ss_sieve_cycle"]!["cycle_budget"]!);
            if (careerOpts.Property("manual_handoff") != null) setting["manual_handoff"] = careerOpts["manual_handoff"]!.DeepClone();
            else setting.Remove("manual_handoff");
            if (careerOpts.Property("manual_pause_dates") != null) setting["manual_pause_dates"] = careerOpts["manual_pause_dates"]!.DeepClone();
            else setting.Remove("manual_pause_dates");
            if (careerOpts.Property("continuation") != null) setting["continuation"] = careerOpts["continuation"]!.DeepClone();
            else setting.Remove("continuation");
        }
        else if (modeId == "ace_career_cycle")
        {
            CaptureRecipeFromCareerOptions(careerOpts, recipe);
            var grow = nodes.FirstOrDefault(n => (string?)n["id"] == "grow") ?? nodes.FirstOrDefault(n => n.Property("ace_cycle") != null);
            if (grow?["ace_cycle"] is JObject ace) setting["ace_cycle"] = ace.DeepClone();
            setting["cycle_budget"] = CleanCycleBudget(cutInner, (JObject)defAll["ace_career_cycle"]!["cycle_budget"]!);
            if (careerOpts.Property("manual_handoff") != null) setting["manual_handoff"] = careerOpts["manual_handoff"]!.DeepClone();
            else setting.Remove("manual_handoff");
            if (careerOpts.Property("manual_pause_dates") != null) setting["manual_pause_dates"] = careerOpts["manual_pause_dates"]!.DeepClone();
            else setting.Remove("manual_pause_dates");
            if (careerOpts.Property("continuation") != null) setting["continuation"] = careerOpts["continuation"]!.DeepClone();
            else setting.Remove("continuation");
        }
        else if (modeId == "cut_preserve")
        {
            CaptureRecipeFromCutOptions(cutOpts, recipe);
            var cutParams = new JObject();
            foreach (var p in cutInner.Properties())
                if (!CutInnerSharedKeys.Contains(p.Name) && p.Name != "continuation")
                    cutParams[p.Name] = p.Value.DeepClone();
            setting["cut_params"] = cutParams;
            if (cutInner.Property("continuation") != null) setting["continuation"] = cutInner["continuation"]!.DeepClone();
            else setting.Remove("continuation");
            var screen = nodes.FirstOrDefault(n => (string?)n["id"] == "screen") ?? nodes.FirstOrDefault(n => (string?)n["kind"] == "cut");
            if (screen?.Property("screening") != null) setting["screening"] = screen["screening"]!.DeepClone();
            else setting.Remove("screening");
            if (screen?["check_at"] is JToken cat && (string?)cat != "cut_checkpoint") setting["check_at"] = cat.DeepClone();
            else setting.Remove("check_at");
        }
        else if (modeId == "cut_then_career")
        {
            if (editedKind == "career")
            {
                CaptureRecipeFromCareerOptions(careerOpts, recipe, "career_only");
                if (careerOpts.Property("manual_handoff") != null) setting["manual_handoff"] = careerOpts["manual_handoff"]!.DeepClone();
                else setting.Remove("manual_handoff");
                if (careerOpts.Property("manual_pause_dates") != null) setting["manual_pause_dates"] = careerOpts["manual_pause_dates"]!.DeepClone();
                else setting.Remove("manual_pause_dates");
            }
            else
            {
                CaptureRecipeFromCutOptions(cutOpts, recipe);
                CaptureRecipeFromCareerOptions(careerOpts, recipe, "career_only");
                var cutParams = new JObject();
                foreach (var p in cutInner.Properties())
                    if (!CutInnerSharedKeys.Contains(p.Name) && p.Name != "continuation")
                        cutParams[p.Name] = p.Value.DeepClone();
                setting["cut_params"] = cutParams;
                if (cutInner.Property("continuation") != null) setting["continuation"] = cutInner["continuation"]!.DeepClone();
                else setting.Remove("continuation");
                if (careerOpts.Property("manual_handoff") != null) setting["manual_handoff"] = careerOpts["manual_handoff"]!.DeepClone();
                else setting.Remove("manual_handoff");
                if (careerOpts.Property("manual_pause_dates") != null) setting["manual_pause_dates"] = careerOpts["manual_pause_dates"]!.DeepClone();
                else setting.Remove("manual_pause_dates");
                var screen = nodes.FirstOrDefault(n => (string?)n["id"] == "screen") ?? nodes.FirstOrDefault(n => (string?)n["kind"] == "cut");
                if (screen?.Property("screening") != null) setting["screening"] = screen["screening"]!.DeepClone();
                else setting.Remove("screening");
                if (screen?["check_at"] is JToken cat && (string?)cat != "cut_checkpoint") setting["check_at"] = cat.DeepClone();
                else setting.Remove("check_at");
            }
        }
    }

    public static JObject CreateBlankBuild(string name, JObject? newPolicyDefaultsByKind)
    {
        var defPolicy = newPolicyDefaultsByKind?["career"] as JObject ?? newPolicyDefaultsByKind?["cut"] as JObject;
        var recipe = new JObject
        {
            ["setup"] = new JObject(),
            ["larc_policy"] = defPolicy != null ? defPolicy.DeepClone() : JValue.CreateNull(),
        };
        if (defPolicy != null)
            recipe["strategy"] = "baseline";
        return new JObject
        {
            ["id"] = "build_" + Guid.NewGuid().ToString("N")[..12],
            ["name"] = name,
            ["recipe"] = recipe,
            ["mode_settings"] = DefaultModeSettings(),
        };
    }

    public static JObject CopyBuild(JObject sourceBuild, string name, bool purify)
    {
        var copy = (JObject)sourceBuild.DeepClone();
        copy["id"] = "build_" + Guid.NewGuid().ToString("N")[..12];
        copy["name"] = name;
        if (purify)
        {
            if (copy["recipe"] is JObject recipe)
            {
                if (recipe["larc_policy"] is JObject lp)
                {
                    lp.Remove("attribute_weights");
                    lp.Remove("after_target_credit");
                    lp.Remove("decision_preferences");
                }
                recipe.Remove("decision_preferences");
            }
            if (copy["mode_settings"] is JObject ms && ms["cut_then_career"]?["grow_overrides"] is JObject go)
            {
                if (go["larc_policy"] is JObject glp)
                {
                    glp.Remove("attribute_weights");
                    glp.Remove("after_target_credit");
                    glp.Remove("decision_preferences");
                }
                go.Remove("decision_preferences");
            }
        }
        return copy;
    }

    public static bool RemoveBuild(JObject library, string buildId, out string? nextSelectedId)
    {
        nextSelectedId = null;
        if (library["builds"] is not JArray builds || builds.Count <= 1)
            return false;
        var list = builds.OfType<JObject>().ToList();
        var idx = list.FindIndex(b => string.Equals((string?)b["id"], buildId, StringComparison.Ordinal));
        if (idx < 0) return false;
        list.RemoveAt(idx);
        var nextIdx = Math.Min(idx, list.Count - 1);
        nextSelectedId = (string?)list[nextIdx]["id"];
        builds.Clear();
        foreach (var item in list) builds.Add(item);
        if (!string.IsNullOrEmpty(nextSelectedId))
            library["selected_build_id"] = nextSelectedId;
        return true;
    }
}
