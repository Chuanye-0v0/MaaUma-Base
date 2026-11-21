using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

public record GraphPort(string Key, string Label, string Type);
public record WeightGroup(string Key, string Label, string[] Components);

/// <summary>Editor-only typed graph compiled to the existing per-turn calendar rules.</summary>
public static class UmaWeightGraphModel
{
    public const string SchemaVersion = "uma.weight_graph.v1";
    private const string WorkflowVersion = "uma.timeline_workflow.v1";
    private static readonly string[] Fields = { "vitality", "speed", "stamina", "power", "guts", "wisdom", "skill_pt", "calendar_year", "calendar_month", "calendar_turn", "remaining_turns" };
    private static readonly string[] Training = { "speed", "stamina", "power", "guts", "wisdom" };
    private static readonly string[] AdaptationPhases = { "guts_expedition", "stamina_expedition", "power_expedition", "speed_expedition", "french_three", "expedition_skill_pt", "courage_three", "willpower_three" };
    private static readonly string[] Actions = { "train", "rest", "outing", "ss" };
    private static readonly string[] Components = {
        "stats", "dynamic_attribute_gap", "skill_pt", "first_arc_speed_preparation", "high_stat_target_scarcity",
        "effective_bond", "training_hint_bond", "friend_startup", "friend_coverage_preparation", "mei_initial_contact_preference",
        "net_charge", "newly_full", "ss_preparation", "cycle_reward_preparation", "charming_charge_progress",
        "future_recovery_cycle_progress", "mei_expected_charge", "full_group_preference", "battery_release_opportunity", "first_charming_preference",
        "vitality", "recovery", "known_recovery", "rule_recovery", "energy_continuity", "recovery_timing_preference",
        "opening_preparation_preference", "healing_charge_tie_preference", "failure_penalty", "failure_expected_loss",
        "mood_recovery", "outing_chain_progress", "outing_completion_increment", "outing_skill_hint_option",
        "conditional_fan_route", "expectation_training_progress", "adaptation_early_use", "phase_strong_training_opportunity",
        "supporter_deadline_preference", "baseline_support", "user_gold_ship_bonus", "user_mei_bonus", "risk_aversion_preference"
    };
    public static readonly WeightGroup[] WeightGroups = {
        new("score_attributes", "属性与技能点", new[] { "stats", "dynamic_attribute_gap", "skill_pt", "first_arc_speed_preparation", "high_stat_target_scarcity" }),
        new("score_bond", "羁绊启动", new[] { "effective_bond", "training_hint_bond", "friend_startup", "friend_coverage_preparation", "mei_initial_contact_preference", "user_gold_ship_bonus", "user_mei_bonus" }),
        new("score_charge", "充电与 SS", new[] { "net_charge", "newly_full", "ss_preparation", "cycle_reward_preparation", "charming_charge_progress", "future_recovery_cycle_progress", "mei_expected_charge", "full_group_preference", "battery_release_opportunity", "first_charming_preference" }),
        new("score_recovery", "体力与心情", new[] { "vitality", "recovery", "known_recovery", "rule_recovery", "energy_continuity", "recovery_timing_preference", "opening_preparation_preference", "healing_charge_tie_preference", "mood_recovery", "outing_chain_progress", "outing_completion_increment", "outing_skill_hint_option" }),
        new("score_stage", "阶段目标", new[] { "conditional_fan_route", "expectation_training_progress", "adaptation_early_use", "phase_strong_training_opportunity", "supporter_deadline_preference", "baseline_support" }),
        new("score_risk", "风险偏好", new[] { "failure_penalty", "failure_expected_loss", "risk_aversion_preference" }),
    };
    private static readonly Dictionary<string, string> Labels = new()
    {
        ["start"] = "每回合开始", ["stage"] = "育成阶段", ["condition"] = "体力低于40",
        ["priority"] = "优先行动", ["weights"] = "训练权重", ["rule"] = "安排输出",
        ["attributes"] = "属性收益权重", ["adaptation"] = "适应性购买安排",
        ["score_attributes"] = "属性与技能点",
        ["score_bond"] = "羁绊启动",
        ["score_charge"] = "充电与 SS",
        ["score_recovery"] = "体力与心情",
        ["score_stage"] = "阶段目标",
        ["score_risk"] = "风险偏好"
    };

    public static GraphPort[] InputPorts(string type) => type switch
    {
        "stage" => new[] { new GraphPort("flow", "阶段顺序", "stage_flow") },
        "weights" => new[] { new GraphPort("base", "基础配置", "weights") }.Concat(WeightGroups.Select(g => new GraphPort(g.Key, g.Label, g.Key))).ToArray(),
        "rule" => new[] { new GraphPort("flow", "安排顺序", "rule_flow"), new GraphPort("condition", "条件", "condition"),
            new GraphPort("priority", "优先行动", "priority"), new GraphPort("weights", "训练权重", "weights"),
            new GraphPort("attributes", "属性收益", "attributes"), new GraphPort("adaptation", "适应性", "adaptation") },
        _ => Array.Empty<GraphPort>()
    };
    public static GraphPort[] OutputPorts(string type) => type switch
    {
        "start" => new[] { new GraphPort("next", "下一阶段", "stage_flow") },
        "stage" => new[] { new GraphPort("next", "下一阶段", "stage_flow"), new GraphPort("rules", "阶段内安排", "rule_flow") },
        "rule" => new[] { new GraphPort("next", "下一安排", "rule_flow") },
        "condition" or "priority" or "weights" or "attributes" or "adaptation" => new[] { new GraphPort("value", "设置", type) },
        _ when WeightGroups.Any(g => g.Key == type) => new[] { new GraphPort("value", "设置", type) },
        _ => Array.Empty<GraphPort>()
    };

    public static JObject NewNode(string type, double x, double y)
    {
        if (!Labels.ContainsKey(type)) throw Error("未知节点类型：" + type);
        var data = type switch
        {
            "stage" => new JObject { ["enabled"] = true, ["window"] = DefaultWindow() },
            "condition" => new JObject { ["when"] = new JObject { ["all"] = new JArray(new JObject { ["field"] = "vitality", ["op"] = "lt", ["value"] = 40 }) } },
            "priority" => new JObject { ["preferred_actions"] = new JArray("train", "ss", "rest", "outing") },
            "weights" => new JObject { ["training_multipliers"] = new JObject { ["speed"] = 1.2 }, ["component_factors"] = new JObject() },
            "attributes" => new JObject { ["attribute_weights"] = new JObject() },
            "adaptation" => new JObject { ["mode"] = "auto", ["priorities"] = new JArray(), ["reserve_points"] = 0 },
            "rule" => new JObject { ["enabled"] = true },
            _ when WeightGroups.Any(g => g.Key == type) => new JObject { ["component_factors"] = new JObject() },
            _ => new JObject()
        };
        return new JObject { ["id"] = type + "_" + Guid.NewGuid().ToString("N"), ["type"] = type,
            ["label"] = Labels[type], ["x"] = x, ["y"] = y, ["data"] = data };
    }
    public static JObject NewEdge(string from, string port, string to, string input) => new()
    {
        ["id"] = "edge_" + Guid.NewGuid().ToString("N"), ["from"] = from, ["port"] = port, ["to"] = to, ["input"] = input
    };
    public static JObject Empty()
    {
        var start = NewNode("start", 40, 180); start["id"] = "start";
        return new JObject { ["schema_version"] = SchemaVersion, ["enabled"] = false, ["entry"] = "start",
            ["nodes"] = new JArray(start), ["edges"] = new JArray() };
    }
    public static JObject Example()
    {
        var graph = Empty(); graph["enabled"] = true;
        var nodes = (JArray)graph["nodes"]!; var edges = (JArray)graph["edges"]!;
        JObject Add(string type, string id, string label, double x, double y)
        { var node = NewNode(type, x, y); node["id"] = id; node["label"] = label; nodes.Add(node); return node; }
        Add("stage", "stage1", "整个育成：每回合检查", 320, 180);
        Add("condition", "low", "体力低于40", 650, 70);
        var rest = Add("priority", "rest", "低体力先恢复", 650, 330);
        rest["data"]!["preferred_actions"] = new JArray("rest", "outing", "train", "ss");
        Add("rule", "rule1", "体力低先恢复", 1000, 80);
        Add("rule", "rule2", "其余回合偏重速度", 1360, 80);
        var train = Add("priority", "train", "优先训练", 1000, 380);
        train["data"]!["preferred_actions"] = new JArray("train", "ss", "rest", "outing");
        Add("weights", "weights", "速度×1.2", 1360, 380);
        void Edge(string from, string port, string to, string input) => edges.Add(NewEdge(from, port, to, input));
        Edge("start", "next", "stage1", "flow"); Edge("stage1", "rules", "rule1", "flow");
        Edge("low", "value", "rule1", "condition"); Edge("rest", "value", "rule1", "priority");
        Edge("rule1", "next", "rule2", "flow"); Edge("train", "value", "rule2", "priority");
        Edge("weights", "value", "rule2", "weights");
        return graph;
    }

    /// <summary>Editable neutral calendar; empty effects retain the current strategy.</summary>
    public static JObject DefaultCalendar()
    {
        var graph = Empty(); var nodes = (JArray)graph["nodes"]!; var edges = (JArray)graph["edges"]!;
        string previous = "start";
        for (var year = 1; year <= 3; year++)
        {
            var x = 320 + (year - 1) * 1450; var stageId = "year" + year;
            var stage = NewNode("stage", x, 160); stage["id"] = stageId;
            stage["label"] = new[] { "第一年 · 初级（含出道前）", "第二年 · 经典", "第三年 · 高级" }[year - 1];
            stage["data"]!["window"] = new JObject { ["phase"] = "year", ["year"] = year, ["from"] = null, ["to"] = null };
            nodes.Add(stage); edges.Add(NewEdge(previous, "next", stageId, "flow")); previous = stageId;
            var previousRule = stageId; var previousPort = "rules";
            for (var index = 0; index < (year == 1 ? 1 : 2); index++)
            {
                var overseas = year > 1 && index == 0; var ruleId = stageId + (overseas ? "_overseas" : "_base");
                var rx = x + 350 + index * 580; var rule = NewNode("rule", rx, 100); rule["id"] = ruleId;
                rule["label"] = overseas ? "7—9月 · 远征安排" : "其余时间 · 沿用策略";
                nodes.Add(rule); edges.Add(NewEdge(previousRule, previousPort, ruleId, "flow")); previousRule = ruleId; previousPort = "next";
                if (overseas)
                {
                    var condition = NewNode("condition", rx, 340); condition["id"] = ruleId + "_when";
                    condition["label"] = "当前月份为7—9月";
                    condition["data"] = new JObject { ["when"] = new JObject { ["all"] = new JArray(
                        new JObject { ["field"] = "calendar_month", ["op"] = "gte", ["value"] = 7 },
                        new JObject { ["field"] = "calendar_month", ["op"] = "lte", ["value"] = 9 }) } };
                    nodes.Add(condition); edges.Add(NewEdge((string)condition["id"]!, "value", ruleId, "condition"));
                }
                foreach (var kind in new[] { "weights", "attributes", "adaptation" })
                {
                    var setting = NewNode(kind, rx + (kind == "weights" ? 0 : kind == "attributes" ? 70 : 140), 540 + (kind == "weights" ? 0 : kind == "attributes" ? 200 : 400));
                    setting["id"] = ruleId + "_" + kind;
                    if (kind == "weights") setting["data"] = new JObject { ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() };
                    nodes.Add(setting); edges.Add(NewEdge((string)setting["id"]!, "value", ruleId, kind));
                }
            }
        }
        Compile(graph); return graph;
    }


    /// <summary>Neutral three-year calendar with one shared base and six editable groups per segment.</summary>
    public static JObject GroupedCalendar()
    {
        var graph = DefaultCalendar(); var nodes = (JArray)graph["nodes"]!; var edges = (JArray)graph["edges"]!;
        var byId = nodes.OfType<JObject>().ToDictionary(n => (string)n["id"]!);
        var baseWeights = NewNode("weights", -420, 160); baseWeights["id"] = "base_weights"; baseWeights["label"] = "全年基础配置";
        baseWeights["data"] = new JObject { ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() }; nodes.Add(baseWeights);
        void Groups(JObject target, string prefix, double x, double y, double rowStep = 210)
        {
            for (var index = 0; index < WeightGroups.Length; index++)
            {
                var group = WeightGroups[index]; var node = NewNode(group.Key, x + index % 2 * 280, y + index / 2 * rowStep);
                node["id"] = prefix + "_" + group.Key; node["label"] = group.Label; nodes.Add(node);
                edges.Add(NewEdge((string)node["id"]!, "value", (string)target["id"]!, group.Key));
            }
        }
        Groups(baseWeights, "base", -1000, 390);
        void Position(string id, double x, double y) { byId[id]["x"] = x; byId[id]["y"] = y; }
        for (var year = 1; year <= 3; year++)
        {
            var stageX = 320 + (year - 1) * 3200; var stageId = "year" + year;
            Position(stageId, stageX, 160);
            for (var index = 0; index < (year == 1 ? 1 : 2); index++)
            {
                var overseas = year > 1 && index == 0; var ruleId = stageId + (overseas ? "_overseas" : "_base");
                var rx = stageX + 350 + index * 1400; Position(ruleId, rx, 100);
                var id = ruleId + "_weights"; var weights = byId[id]; weights["label"] = "本段权重汇总";
                Position(id, rx + 300, 350);
                Position(ruleId + "_attributes", rx + 600, 350);
                Position(ruleId + "_adaptation", rx + 600, 600);
                if (overseas) Position(ruleId + "_when", rx + 600, 100);
                edges.Add(NewEdge("base_weights", "value", id, "base"));
                Groups(weights, id, rx - 260, 350, 160);
            }
        }
        Compile(graph); return graph;
    }

    /// <summary>Split before the selected half-month; clone all rule settings for independent edits.</summary>
    public static JObject SplitStage(JObject graph, string stageId, int year, int month, string half)
    {
        var result = Normalize(graph); CompileChecked(result, out _);
        var nodes = (JArray)result["nodes"]!; var edges = (JArray)result["edges"]!;
        var originalMaxY = nodes.OfType<JObject>().Max(n => (double)n["y"]!);
        var byId = nodes.OfType<JObject>().ToDictionary(n => (string)n["id"]!);
        if (!byId.TryGetValue(stageId, out var stage) || (string?)stage["type"] != "stage") throw Error("请选择需要切分的育成阶段。");
        var cut = Date(new JObject { ["year"] = year, ["month"] = month, ["half"] = half }, stageId)!;
        var cutDay = Ordinal(cut); var window = (JObject)stage["data"]!["window"]!; var phase = (string?)window["phase"];
        if (phase is not ("year" or "dated")) throw Error("请先将阶段设为全年或日期范围，再切分时间。");
        var from = window["from"] as JObject; var to = window["to"] as JObject;
        var stageYear = phase == "year" ? (int)window["year"]! : year;
        var lower = from is null ? phase == "year" ? (stageYear - 1) * 24 : 0 : Ordinal(from);
        var upper = to is null ? phase == "year" ? stageYear * 24 - 1 : 71 : Ordinal(to);
        var preDebutLeft = phase == "year" && stageYear == 1 && from is null && cutDay == 0;
        if (phase == "year" && year != stageYear || cutDay > upper || cutDay < lower || cutDay == lower && !preDebutLeft)
            throw Error("切分点必须位于阶段内，且左右均保留时间。");
        JObject At(int day) => new() { ["year"] = day / 24 + 1, ["month"] = day % 24 / 2 + 1, ["half"] = day % 2 == 0 ? "first" : "second" };
        var right = (JObject)stage.DeepClone(); right["id"] = "stage_" + Guid.NewGuid().ToString("N");
        right["label"] = Text(stage["label"], "阶段名称").Length > 85 ? "切分后的时间段" : Text(stage["label"], "阶段名称") + " · 后段";
        right["x"] = (double)stage["x"]! + 480; right["y"] = (double)stage["y"]! + 200;
        ((JObject)right["data"]!).Remove("workflow_id");
        var rightWindow = (JObject)window.DeepClone(); rightWindow["from"] = cut; right["data"]!["window"] = rightWindow;
        if (preDebutLeft) stage["data"]!["window"] = new JObject { ["phase"] = "pre_debut", ["from"] = null, ["to"] = null };
        else window["to"] = At(cutDay - 1);
        var oldEdges = edges.OfType<JObject>().ToArray(); var next = oldEdges.FirstOrDefault(e => (string?)e["from"] == stageId && (string?)e["port"] == "next");
        var rightId = (string)right["id"]!; nodes.Add(right);
        var copiedNodes = new List<JObject> { right };
        if (next is not null) next["from"] = rightId;
        edges.Add(NewEdge(stageId, "next", rightId, "flow"));
        var clones = new Dictionary<string, string>();
        string CloneNode(string id)
        {
            if (clones.TryGetValue(id, out var known)) return known;
            var copy = (JObject)byId[id].DeepClone(); var newId = (string)copy["type"]! + "_" + Guid.NewGuid().ToString("N");
            copy["id"] = newId; copy["x"] = (double)copy["x"]! + 480; copy["y"] = (double)copy["y"]! + 200;
            ((JObject)copy["data"]!).Remove("workflow_id"); nodes.Add(copy); copiedNodes.Add(copy); clones[id] = newId;
            foreach (var input in oldEdges.Where(e => (string?)copy["type"] == "weights" && (string?)e["to"] == id && (string?)e["input"] != "flow"))
            {
                var sourceId = (string)input["from"]!;
                var dependencyId = (string?)input["input"] == "base" ? sourceId : CloneNode(sourceId);
                edges.Add(NewEdge(dependencyId, (string)input["port"]!, newId, (string)input["input"]!));
            }
            return newId;
        }
        var firstRule = oldEdges.FirstOrDefault(e => (string?)e["from"] == stageId && (string?)e["port"] == "rules");
        string? rule = (string?)firstRule?["to"]; var previous = rightId; var previousPort = "rules";
        while (rule is not null)
        {
            var copyRule = CloneNode(rule); edges.Add(NewEdge(previous, previousPort, copyRule, "flow")); previous = copyRule; previousPort = "next";
            foreach (var input in oldEdges.Where(e => (string?)e["to"] == rule && (string?)e["input"] != "flow"))
                edges.Add(NewEdge(CloneNode((string)input["from"]!), (string)input["port"]!, copyRule, (string)input["input"]!));
            rule = (string?)oldEdges.FirstOrDefault(e => (string?)e["from"] == rule && (string?)e["port"] == "next")?["to"];
        }
        var shiftX = (double)stage["x"]! - (double)right["x"]!;
        var shiftY = originalMaxY + 400 - copiedNodes.Min(n => (double)n["y"]!);
        foreach (var copied in copiedNodes)
        {
            copied["x"] = (double)copied["x"]! + shiftX;
            copied["y"] = (double)copied["y"]! + shiftY;
        }
        var normalized = Normalize(result); CompileChecked(normalized, out _); return normalized;
    }

    public static JObject FromWorkflow(JObject? workflow)
    {
        if (workflow is null) return Empty();
        if (workflow.Property("editor_graph") is not null)
        {
            if (workflow["editor_graph"] is not JObject existing) throw Error("节点画布格式不正确。");
            var graph = Normalize(existing); CompileChecked(graph, out _); return graph;
        }
        var result = Empty(); result["enabled"] = Flag(workflow["enabled"] ?? new JValue(false), "工作流启停");
        if (workflow["stages"] is not JArray stages) throw Error("工作流需要阶段列表。");
        var nodes = (JArray)result["nodes"]!; var edges = (JArray)result["edges"]!;
        string previousStage = "start"; var stageIndex = 0;
        foreach (var token in stages)
        {
            if (token is not JObject source) throw Error("工作流阶段格式不正确。");
            var stage = NewNode("stage", 320 + stageIndex * 400, 180);
            var stageId = (string)stage["id"]!;
            stage["label"] = Text(source["label"], "阶段名称");
            stage["data"] = new JObject { ["workflow_id"] = source["id"]?.DeepClone(),
                ["enabled"] = source["enabled"]?.DeepClone() ?? true, ["window"] = source["window"]?.DeepClone() ?? DefaultWindow() };
            nodes.Add(stage); edges.Add(NewEdge(previousStage, "next", stageId, "flow")); previousStage = stageId;
            if (source["rules"] is not JArray rules) throw Error("阶段需要安排列表。");
            string previousRule = stageId; string previousPort = "rules"; var ruleIndex = 0;
            foreach (var item in rules)
            {
                if (item is not JObject original) throw Error("工作流安排格式不正确。");
                var rule = NewNode("rule", 620 + stageIndex * 400 + ruleIndex * 340, 100 + ruleIndex * 400);
                var ruleId = (string)rule["id"]!; rule["label"] = Text(original["label"], "安排名称");
                rule["data"] = new JObject { ["workflow_id"] = original["id"]?.DeepClone(), ["enabled"] = original["enabled"]?.DeepClone() ?? true };
                nodes.Add(rule); edges.Add(NewEdge(previousRule, previousPort, ruleId, "flow")); previousRule = ruleId; previousPort = "next";
                var condition = NewNode("condition", (double)rule["x"]! - 50, (double)rule["y"]! + 160);
                condition["label"] = "安排条件"; condition["data"] = new JObject { ["when"] = original["when"]?.DeepClone() ?? new JObject { ["all"] = new JArray() } };
                nodes.Add(condition); edges.Add(NewEdge((string)condition["id"]!, "value", ruleId, "condition"));
                var effects = original["effects"] as JObject ?? new JObject();
                var priority = NewNode("priority", (double)rule["x"]! + 50, (double)rule["y"]! + 270);
                priority["data"] = new JObject { ["preferred_actions"] = effects["preferred_actions"]?.DeepClone() ?? new JArray() };
                nodes.Add(priority); edges.Add(NewEdge((string)priority["id"]!, "value", ruleId, "priority"));
                var weights = NewNode("weights", (double)rule["x"]! + 150, (double)rule["y"]! + 380);
                weights["data"] = new JObject { ["training_multipliers"] = effects["training_multipliers"]?.DeepClone() ?? new JObject(), ["component_factors"] = effects["component_factors"]?.DeepClone() ?? new JObject() };
                nodes.Add(weights); edges.Add(NewEdge((string)weights["id"]!, "value", ruleId, "weights"));
                foreach (var setting in new[] { "attributes", "adaptation" })
                {
                    if (setting == "attributes" && effects.Property("attribute_weights") is null && effects.Property("after_target_credit") is null || setting == "adaptation" && effects.Property("adaptation") is null) continue;
                    var extra = NewNode(setting, (double)rule["x"]! + 250, (double)rule["y"]! + (setting == "attributes" ? 490 : 600));
                    extra["data"] = setting == "adaptation" ? effects["adaptation"]!.DeepClone() : new JObject(effects.Properties().Where(p => p.Name is "attribute_weights" or "after_target_credit").Select(p => new JProperty(p.Name, p.Value.DeepClone())));
                    nodes.Add(extra); edges.Add(NewEdge((string)extra["id"]!, "value", ruleId, setting));
                }
                ruleIndex++;
            }
            stageIndex++;
        }
        var normalized = Normalize(result); CompileChecked(normalized, out _); return normalized;
    }

    public static JObject Compile(JObject graph, bool retainGraph = true)
    {
        var checkedGraph = Normalize(graph); var workflow = CompileChecked(checkedGraph, out _);
        if (retainGraph) workflow["editor_graph"] = checkedGraph;
        return workflow;
    }
    public static string[] Warnings(JObject graph)
    {
        var checkedGraph = Normalize(graph); CompileChecked(checkedGraph, out var used);
        return ((JArray)checkedGraph["nodes"]!).OfType<JObject>().Where(node => !used.Contains((string)node["id"]!))
            .Select(node => $"节点 {node["id"]}（{node["label"]}）尚未连接到有效安排，仅保留草稿。").ToArray();
    }

    private static JObject Normalize(JObject graph)
    {
        Keys(graph, "节点画布", "schema_version", "enabled", "entry", "nodes", "edges");
        if ((string?)graph["schema_version"] != SchemaVersion) throw Error("节点画布版本不正确。");
        var enabled = Flag(graph["enabled"], "画布启停");
        if (graph["nodes"] is not JArray rawNodes || graph["edges"] is not JArray rawEdges) throw Error("节点画布需要节点和连线列表。");
        var nodes = new JArray(); var byId = new Dictionary<string, JObject>();
        foreach (var token in rawNodes)
        {
            if (token is not JObject raw) throw Error("画布节点格式不正确。");
            Keys(raw, "画布节点", "id", "type", "label", "x", "y", "data");
            var id = Text(raw["id"], "节点编号", true); Text(raw["type"], "节点类型"); var type = (string)raw["type"]!;
            if (!Labels.ContainsKey(type) || byId.ContainsKey(id)) throw Error("节点 " + id + "：类型未知或编号重复。");
            var x = Number(raw["x"], "节点 " + id + " 横坐标"); var y = Number(raw["y"], "节点 " + id + " 纵坐标");
            var node = new JObject { ["id"] = id, ["type"] = type, ["label"] = Text(raw["label"] ?? new JValue(Labels[type]), "节点 " + id + " 名称"),
                ["x"] = x, ["y"] = y, ["data"] = Data(type, raw["data"] ?? new JObject(), id) };
            byId.Add(id, node); nodes.Add(node);
        }
        var entry = Text(graph["entry"], "起点编号", true);
        if (!byId.TryGetValue(entry, out var start) || (string?)start["type"] != "start") throw Error("画布起点必须指向一个每回合开始节点。");
        var edges = new JArray(); var ids = new HashSet<string>(); var inputs = new HashSet<(string, string)>(); var outputs = new HashSet<(string, string)>();
        var adjacency = byId.Keys.ToDictionary(id => id, _ => new List<string>());
        foreach (var token in rawEdges)
        {
            if (token is not JObject raw) throw Error("画布连线格式不正确。");
            Keys(raw, "画布连线", "id", "from", "port", "to", "input");
            var edge = new JObject(); foreach (var key in new[] { "id", "from", "port", "to", "input" }) edge[key] = Text(raw[key], "连线 " + key, true);
            var id = (string)edge["id"]!; var from = (string)edge["from"]!; var to = (string)edge["to"]!;
            var port = (string)edge["port"]!; var input = (string)edge["input"]!;
            if (!ids.Add(id)) throw Error("连线编号重复：" + id);
            if (!byId.ContainsKey(from) || !byId.ContainsKey(to)) throw Error("连线 " + id + "：找不到对应节点。");
            var outPort = OutputPorts((string)byId[from]["type"]!).FirstOrDefault(p => p.Key == port);
            var inPort = InputPorts((string)byId[to]["type"]!).FirstOrDefault(p => p.Key == input);
            if (outPort is null || inPort is null || outPort.Type != inPort.Type) throw Error("连线 " + id + "：节点 " + from + " 与 " + to + " 的端口类型不匹配。");
            if (!inputs.Add((to, input)) || outPort.Type.EndsWith("_flow", StringComparison.Ordinal) && !outputs.Add((from, port)))
                throw Error("连线 " + id + "：节点输入或顺序输出只能连接一次。");
            adjacency[from].Add(to);
            edges.Add(edge);
        }
        var marks = new Dictionary<string, int>();
        void Visit(string id)
        {
            marks.TryGetValue(id, out var mark); if (mark == 1) throw Error("节点 " + id + "：阶段、安排或基础权重连线不能形成循环。");
            if (mark == 2) return; marks[id] = 1; foreach (var to in adjacency[id]) Visit(to); marks[id] = 2;
        }
        foreach (var id in byId.Keys) Visit(id);
        return new JObject { ["schema_version"] = SchemaVersion, ["enabled"] = enabled, ["entry"] = entry, ["nodes"] = nodes, ["edges"] = edges };
    }

    /// <summary>Effective relative maps for a weights node; base and connected group overrides use the same compiler resolver.</summary>
    public static JObject ResolveWeights(JObject graph, string id)
    {
        var normalized = Normalize(graph);
        var nodes = ((JArray)normalized["nodes"]!).OfType<JObject>().ToDictionary(n => (string)n["id"]!);
        if (!nodes.TryGetValue(id, out var node) || (string?)node["type"] != "weights") throw Error("请选择权重汇总节点。");
        var inputs = ((JArray)normalized["edges"]!).OfType<JObject>().ToDictionary(e => ((string)e["to"]!, (string)e["input"]!), e => (string)e["from"]!);
        return ResolveWeightsChecked(nodes, inputs, id, new HashSet<string>(), new Dictionary<string, JObject>());
    }

    private static JObject ResolveWeightsChecked(Dictionary<string, JObject> nodes, Dictionary<(string, string), string> inputs,
        string id, HashSet<string> used, Dictionary<string, JObject> cache)
    {
        used.Add(id);
        if (cache.TryGetValue(id, out var known)) return (JObject)known.DeepClone();
        var source = (JObject)nodes[id]["data"]!;
        var result = inputs.TryGetValue((id, "base"), out var baseId) ? ResolveWeightsChecked(nodes, inputs, baseId, used, cache)
            : new JObject { ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() };
        foreach (var map in new[] { "training_multipliers", "component_factors" })
            foreach (var property in ((JObject)source[map]!).Properties()) result[map]![property.Name] = property.Value.DeepClone();
        foreach (var group in WeightGroups)
        {
            if (!inputs.TryGetValue((id, group.Key), out var groupId)) continue;
            used.Add(groupId);
            foreach (var property in ((JObject)nodes[groupId]["data"]!["component_factors"]!).Properties())
                result["component_factors"]![property.Name] = property.Value.DeepClone();
        }
        cache[id] = (JObject)result.DeepClone();
        return result;
    }

    private static JObject CompileChecked(JObject graph, out HashSet<string> used)
    {
        var nodes = ((JArray)graph["nodes"]!).OfType<JObject>().ToDictionary(n => (string)n["id"]!);
        var edges = ((JArray)graph["edges"]!).OfType<JObject>().ToArray();
        var next = edges.GroupBy(e => ((string)e["from"]!, (string)e["port"]!)).ToDictionary(g => g.Key, g => (string)g.First()["to"]!);
        var inputs = edges.ToDictionary(e => ((string)e["to"]!, (string)e["input"]!), e => (string)e["from"]!);
        used = new HashSet<string> { (string)graph["entry"]! }; var weightCache = new Dictionary<string, JObject>();
        var stages = new JArray(); var stageIds = new HashSet<string>();
        string? stageId = next.GetValueOrDefault(((string)graph["entry"]!, "next"));
        while (stageId is not null)
        {
            var stage = nodes[stageId]; var data = (JObject)stage["data"]!; used.Add(stageId);
            if (stages.Count == 16) throw Error("节点 " + stageId + "：工作流最多16个阶段。");
            var outputId = (string?)data["workflow_id"] ?? stageId;
            if (!stageIds.Add(outputId)) throw Error("节点 " + stageId + "：阶段执行编号重复。");
            var rules = new JArray(); var ruleIds = new HashSet<string>(); string? ruleId = next.GetValueOrDefault((stageId, "rules"));
            while (ruleId is not null)
            {
                var node = nodes[ruleId]; var ruleData = (JObject)node["data"]!; used.Add(ruleId);
                if (rules.Count == 16) throw Error("节点 " + ruleId + "：每个阶段最多16条安排。");
                var outputRuleId = (string?)ruleData["workflow_id"] ?? ruleId;
                if (!ruleIds.Add(outputRuleId)) throw Error("节点 " + ruleId + "：同一阶段的安排执行编号重复。");
                JToken when = new JObject { ["all"] = new JArray() }; var effects = DefaultEffects();
                foreach (var port in new[] { "condition", "priority", "weights", "attributes", "adaptation" })
                {
                    if (!inputs.TryGetValue((ruleId, port), out var sourceId)) continue;
                    used.Add(sourceId); var source = port == "weights" ? ResolveWeightsChecked(nodes, inputs, sourceId, used, weightCache) : (JObject)nodes[sourceId]["data"]!;
                    if (port == "condition") when = source["when"]!.DeepClone();
                    else if (port == "adaptation") effects["adaptation"] = source.DeepClone();
                    else foreach (var property in source.Properties()) effects[property.Name] = property.Value.DeepClone();
                }
                rules.Add(new JObject { ["id"] = outputRuleId, ["label"] = node["label"]!.DeepClone(), ["enabled"] = ruleData["enabled"]!.DeepClone(), ["when"] = when, ["effects"] = effects });
                ruleId = next.GetValueOrDefault((ruleId, "next"));
            }
            stages.Add(new JObject { ["id"] = outputId, ["label"] = stage["label"]!.DeepClone(), ["enabled"] = data["enabled"]!.DeepClone(), ["window"] = data["window"]!.DeepClone(), ["rules"] = rules });
            stageId = next.GetValueOrDefault((stageId, "next"));
        }
        return new JObject { ["schema_version"] = WorkflowVersion, ["enabled"] = graph["enabled"]!.DeepClone(), ["evaluation"] = "calendar_recheck", ["stages"] = stages };
    }

    private static JObject Data(string type, JToken token, string id)
    {
        if (token is not JObject value) throw Error("节点 " + id + " 参数需要对象。");
        var group = WeightGroups.FirstOrDefault(g => g.Key == type);
        var keys = group is not null ? new[] { "component_factors" } : type switch { "stage" => new[] { "enabled", "window", "workflow_id" }, "rule" => new[] { "enabled", "workflow_id" },
            "condition" => new[] { "when" }, "priority" => new[] { "preferred_actions" }, "weights" => new[] { "training_multipliers", "component_factors" },
            "attributes" => new[] { "attribute_weights", "after_target_credit" }, "adaptation" => new[] { "mode", "priorities", "reserve_points" }, _ => Array.Empty<string>() };
        Keys(value, "节点 " + id + " 参数", keys); var result = new JObject();
        if (type is "stage" or "rule") result["enabled"] = Flag(value["enabled"] ?? new JValue(true), "节点 " + id + " 启停");
        if (type == "stage") result["window"] = Window(value["window"] ?? DefaultWindow(), id);
        if (type == "condition") result["when"] = When(value["when"] ?? new JObject { ["all"] = new JArray() }, id);
        if (type == "priority")
        {
            if ((value["preferred_actions"] ?? new JArray()) is not JArray actions || actions.Any(a => a.Type != JTokenType.String || !Actions.Contains((string)a!)) || actions.Select(a => (string)a!).Distinct().Count() != actions.Count)
                throw Error("节点 " + id + "：优先行动须为不重复的训练、休息、外出、SS。");
            result["preferred_actions"] = actions.DeepClone();
        }
        if (type == "weights")
        {
            result["training_multipliers"] = Factors(value["training_multipliers"] ?? new JObject(), Training, id);
            result["component_factors"] = Factors(value["component_factors"] ?? new JObject(), Components, id);
        }
        if (group is not null) result["component_factors"] = Factors(value["component_factors"] ?? new JObject(), group.Components, id);
        if (type == "attributes")
        {
            if (value.Property("attribute_weights") is not null)
            {
                var factors = Factors(value["attribute_weights"]!, Training, id);
                if (factors.Properties().Any(p => (double)p.Value <= 0)) throw Error("节点 " + id + "：属性收益倍率应大于0且不超过10。");
                result["attribute_weights"] = factors;
            }
            if (value.Property("after_target_credit") is not null)
            {
                var amount = Number(value["after_target_credit"], "达标后收益折扣");
                if (amount <= 0 || amount > 1) throw Error("节点 " + id + "：达标后收益折扣应大于0且不超过1。");
                result["after_target_credit"] = amount;
            }
        }
        if (type == "adaptation")
        {
            var mode = value.Property("mode") is null ? "auto" : value["mode"]?.Type == JTokenType.String ? (string)value["mode"]! : "";
            if (!new[] { "auto", "prefer", "defer" }.Contains(mode)) throw Error("节点 " + id + "：适应性安排请选择自动、优先或暂缓。");
            if ((value["priorities"] ?? new JArray()) is not JArray priorities || priorities.Any(p => p.Type != JTokenType.String || !AdaptationPhases.Contains((string)p!)) || priorities.Select(p => (string)p!).Distinct().Count() != priorities.Count)
                throw Error("节点 " + id + "：适应性优先项应为现有项目的不重复顺序。");
            var reserve = value.Property("reserve_points") is null ? new JValue(0) : value["reserve_points"]!;
            if (reserve.Type != JTokenType.Integer || System.Numerics.BigInteger.Parse(reserve.ToString()) < 0) throw Error("节点 " + id + "：保留Pt应为非负整数。");
            result["mode"] = mode; result["priorities"] = priorities.DeepClone(); result["reserve_points"] = reserve.DeepClone();
        }
        if (value.Property("workflow_id") is not null) result["workflow_id"] = Text(value["workflow_id"], "节点 " + id + " 原编号", true);
        return result;
    }
    private static JObject Window(JToken token, string id)
    {
        if (token is not JObject value) throw Error("节点 " + id + "：阶段时间格式不正确。");
        Keys(value, "节点 " + id + " 时间", "phase", "year", "from", "to");
        var phase = value.Property("phase") is null ? "all" : value["phase"]?.Type == JTokenType.String ? (string)value["phase"]! : "";
        if (!new[] { "all", "pre_debut", "dated", "year" }.Contains(phase)) throw Error("节点 " + id + "：阶段类型不正确。");
        var from = Date(value["from"], id); var to = Date(value["to"], id);
        if (phase is not ("dated" or "year") && (from is not null || to is not null)) throw Error("节点 " + id + "：只有日期阶段可设置边界。");
        if (from is not null && to is not null && Ordinal(from) > Ordinal(to)) throw Error("节点 " + id + "：结束日期早于开始日期。");
        var result = new JObject { ["phase"] = phase, ["from"] = from, ["to"] = to };
        if (phase == "year")
        {
            if (value["year"]?.Type != JTokenType.Integer || Number(value["year"], "年份") < 1 || Number(value["year"], "年份") > 3) throw Error("节点 " + id + "：全年阶段请选择第一年、第二年或第三年。");
            var year = (int)value["year"]!;
            if (from is not null && (int)from["year"]! != year || to is not null && (int)to["year"]! != year) throw Error("节点 " + id + "：全年阶段边界必须在所选年份。");
            result["year"] = year;
        }
        else if (value.Property("year") is not null) throw Error("节点 " + id + "：只有全年阶段可以设置年份。");
        return result;
    }
    private static JObject? Date(JToken? token, string id)
    {
        if (token is null || token.Type == JTokenType.Null) return null;
        if (token is not JObject value) throw Error("节点 " + id + "：日期格式不正确。");
        Keys(value, "节点 " + id + " 日期", "year", "month", "half");
        if (value.Count != 3 || value["year"]?.Type != JTokenType.Integer || value["month"]?.Type != JTokenType.Integer) throw Error("节点 " + id + "：日期需要年份月份上下旬。");
        var year = Number(value["year"], "年份"); var month = Number(value["month"], "月份"); var half = (string?)value["half"];
        if (year < 1 || year > 3 || month < 1 || month > 12 || half is not ("first" or "second")) throw Error("节点 " + id + "：日期范围不正确。");
        return (JObject)value.DeepClone();
    }
    private static int Ordinal(JObject value) => ((int)value["year"]! - 1) * 24 + ((int)value["month"]! - 1) * 2 + ((string?)value["half"] == "second" ? 1 : 0);
    private static JObject When(JToken token, string id)
    {
        if (token is not JObject value || value.Count != 1 || value.Properties().First().Name is not ("all" or "any") || value.Properties().First().Value is not JArray conditions)
            throw Error("节点 " + id + "：条件请选择全部满足或任一满足。");
        if (conditions.Count > 12) throw Error("节点 " + id + "：每条安排最多12个条件。");
        var checkedConditions = new JArray();
        foreach (var item in conditions)
        {
            if (item is not JObject condition) throw Error("节点 " + id + "：条件格式不正确。");
            Keys(condition, "节点 " + id + " 条件", "field", "op", "value");
            var field = condition["field"]?.Type == JTokenType.String ? (string)condition["field"]! : "";
            var op = condition["op"]?.Type == JTokenType.String ? (string)condition["op"]! : "";
            var number = Number(condition["value"], "节点 " + id + " 条件数值");
            if (condition.Count != 3 || !Fields.Contains(field) || !new[] { "lt", "lte", "eq", "gte", "gt" }.Contains(op) || number < 0 || number > 100000 || field == "vitality" && number > 100
                || new[] { "calendar_year", "calendar_month", "calendar_turn", "remaining_turns" }.Contains(field) && condition["value"]!.Type != JTokenType.Integer
                || field == "calendar_year" && (number < 1 || number > 3) || field == "calendar_month" && (number < 1 || number > 12) || field == "calendar_turn" && (number < 1 || number > 72))
                throw Error("节点 " + id + "：条件字段、比较符或数值不正确。");
            checkedConditions.Add(new JObject { ["field"] = field, ["op"] = op, ["value"] = condition["value"]!.DeepClone() });
        }
        return new JObject { [value.Properties().First().Name] = checkedConditions };
    }
    private static JObject Factors(JToken token, string[] allowed, string id)
    {
        if (token is not JObject value) throw Error("节点 " + id + "：倍率需要对象。");
        Keys(value, "节点 " + id + " 倍率", allowed); var result = new JObject();
        foreach (var property in value.Properties()) { var number = Number(property.Value, "节点 " + id + " 倍率"); if (number < 0 || number > 10) throw Error("节点 " + id + "：倍率应在0～10之间。"); result[property.Name] = number; }
        return result;
    }
    private static JObject DefaultWindow() => new() { ["phase"] = "all", ["from"] = null, ["to"] = null };
    private static JObject DefaultEffects() => new() { ["preferred_actions"] = new JArray(), ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() };
    private static void Keys(JObject value, string label, params string[] allowed)
    { if (value.Properties().Any(p => !allowed.Contains(p.Name))) throw Error(label + "包含未知字段。"); }
    private static string Text(JToken? token, string label, bool identifier = false)
    {
        if (token?.Type != JTokenType.String) throw Error(label + "格式不正确。"); var value = (string)token!;
        if (string.IsNullOrWhiteSpace(value) || value.EnumerateRunes().Count() > (identifier ? 64 : 100) || value.Any(c => c < 32) || identifier && !Regex.IsMatch(value, @"\A[A-Za-z0-9_.-]+\z")) throw Error(label + "格式不正确。");
        return value.Trim();
    }
    private static bool Flag(JToken? token, string label) => token?.Type == JTokenType.Boolean ? (bool)token : throw Error(label + "必须为启用或关闭。");
    private static double Number(JToken? token, string label)
    { if (token?.Type is not (JTokenType.Integer or JTokenType.Float)) throw Error(label + "必须是有限数字。"); var value = (double)token; return double.IsFinite(value) ? value : throw Error(label + "必须是有限数字。"); }
    private static ArgumentException Error(string message) => new(message);
}
