using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>A read-only sheet projection; edits clone and patch only one rule's training map.</summary>
public static class UmaStrategySheetModel
{
    public static readonly string[] TrainingKeys = { "speed", "stamina", "power", "guts", "wisdom" };
    public sealed class Row
    {
        public string StageId { get; init; } = "";
        public string RuleId { get; init; } = "";
        public string StageLabel { get; init; } = "";
        public string Label { get; init; } = "";
        public string Window { get; init; } = "";
        public string ConditionSummary { get; init; } = "";
        public bool? StageEnabled { get; init; }
        public bool? RuleEnabled { get; init; }
        public int Priority { get; init; }
        public JObject Effective { get; init; } = new();
        public string[] Changed { get; init; } = Array.Empty<string>();
        public bool CanEdit { get; init; }
        public string Notice { get; init; } = "";
    }

    public static Row[] Read(JObject graph)
    {
        var nodes = (graph["nodes"] as JArray ?? new JArray()).OfType<JObject>()
            .Where(n => n["id"]?.Type == JTokenType.String).GroupBy(n => (string)n["id"]!)
            .ToDictionary(g => g.Key, g => g.First());
        var edges = (graph["edges"] as JArray ?? new JArray()).OfType<JObject>().ToArray();
        JObject? Node(string? id) => id != null && nodes.TryGetValue(id, out var found) ? found : null;
        string? Next(string id, string port) => (string?)edges.FirstOrDefault(e => (string?)e["from"] == id && (string?)e["port"] == port)?["to"];
        JObject? Input(string id, string port) => Node((string?)edges.FirstOrDefault(e => (string?)e["to"] == id && (string?)e["input"] == port)?["from"]);
        string? graphIssue = null;
        try { UmaWeightGraphModel.Compile(graph); }
        catch (Exception error) { graphIssue = error.Message; }
        var result = new List<Row>(); var stages = new HashSet<string>();
        var stageId = Next((string?)graph["entry"] ?? "", "next");
        while (stageId != null && stages.Add(stageId) && Node(stageId) is JObject stage)
        {
            var seen = new HashSet<string>(); var ruleId = Next(stageId, "rules"); var priority = 0;
            while (ruleId != null && seen.Add(ruleId) && Node(ruleId) is JObject rule)
            {
                priority++; var weights = Input(ruleId, "weights"); var effective = new JObject();
                var issue = graphIssue;
                if (issue == null)
                {
                    var map = weights == null ? new JObject() : UmaWeightGraphModel.ResolveWeights(graph, (string)weights["id"]!)["training_multipliers"] as JObject ?? new JObject();
                    foreach (var key in TrainingKeys) effective[key] = map[key]?.DeepClone() ?? new JValue(1m);
                }
                var shared = weights != null && edges.Count(e => (string?)e["from"] == (string?)weights["id"]) > 1;
                var changed = ((weights?["data"]?["training_multipliers"] as JObject)?.Properties() ?? Enumerable.Empty<JProperty>()).Select(p => p.Name).ToArray();
                var label = (string?)rule["label"] ?? "条件安排";
                if (label == "其余时间 · 沿用策略") label = priority == 1 ? "全年安排" : "其余时间";
                result.Add(new Row {
                    StageId = stageId, RuleId = ruleId, StageLabel = (string?)stage["label"] ?? stageId,
                    Label = label, Window = WindowText(stage["data"]?["window"]), Priority = priority,
                    ConditionSummary = DescribeCondition(Input(ruleId,"condition")?["data"]?["when"]),
                    StageEnabled = EnabledFlag(stage["data"]?["enabled"]), RuleEnabled = EnabledFlag(rule["data"]?["enabled"]),
                    Effective = effective, Changed = changed, CanEdit = issue == null && !shared,
                    Notice = issue != null ? "高级图完整保留" : shared ? "共用权重 · 在高级编辑" : changed.Length > 0 ? $"本段覆盖 {changed.Length} 项" : "沿用基础倍率",
                });
                ruleId = Next(ruleId, "next");
            }
            stageId = Next(stageId, "next");
        }
        return result.ToArray();
    }

    private static bool? EnabledFlag(JToken? value) => value == null ? true : value.Type == JTokenType.Boolean ? (bool)value : null;
    private static string DescribeCondition(JToken? when)
    {
        if(when==null)return "每回合：无附加条件（按优先级）";
        if(when is not JObject value||value.Count!=1||value.Properties().First().Name is not ("all" or "any")||value.Properties().First().Value is not JArray clauses)
            return "高级条件（原文保留）";
        var any=value.Property("any")!=null;
        if(clauses.Count==0)return any?"每回合：任一满足（空条件）":"每回合：无附加条件（按优先级）";
        var fields=new Dictionary<string,string>{{"vitality","体力"},{"speed","速度"},{"stamina","耐力"},{"power","力量"},{"guts","毅力"},{"wisdom","智力"},{"skill_pt","技能Pt"},{"calendar_year","育成年份"},{"calendar_month","月份"},{"calendar_turn","日历回合"},{"remaining_turns","距目标剩余回合"}};
        var operators=new Dictionary<string,string>{{"lt","小于"},{"lte","不高于"},{"eq","等于"},{"gte","不低于"},{"gt","大于"}};
        var parts=new List<string>();
        foreach(var token in clauses) {
            if(token is not JObject clause||!fields.TryGetValue((string?)clause["field"]??"",out var field)||!operators.TryGetValue((string?)clause["op"]??"",out var op)||clause["value"]==null)
                return "高级条件（原文保留）";
            parts.Add(field+op+clause["value"]);
        }
        return "每回合 · "+(any?"任一满足：":"全部满足：")+string.Join("；",parts);
    }

    public static JObject PatchTraining(JObject graph, string ruleId, JObject values, bool restore = false)
    {
        var row = Read(graph).FirstOrDefault(r => r.RuleId == ruleId) ?? throw new ArgumentException("该阶段安排已变化，请重新读取。");
        if (!row.CanEdit) throw new InvalidOperationException(row.Notice);
        foreach (var p in values.Properties())
            if (!TrainingKeys.Contains(p.Name) || p.Value.Type is not (JTokenType.Float or JTokenType.Integer) || (double)p.Value < 0 || (double)p.Value > 10 || !double.IsFinite((double)p.Value))
                throw new ArgumentException("训练偏好需要 0—10 的有效倍率。");
        var result = (JObject)graph.DeepClone();
        if (!restore && values.Count == 0) return result;
        var nodes = (JArray)result["nodes"]!; var edges = (JArray)result["edges"]!;
        var edge = edges.OfType<JObject>().FirstOrDefault(e => (string?)e["to"] == ruleId && (string?)e["input"] == "weights");
        var weights = edge == null ? null : nodes.OfType<JObject>().First(n => (string?)n["id"] == (string?)edge["from"]);
        if (weights == null)
        {
            if (restore) return result;
            weights = UmaWeightGraphModel.NewNode("weights", 800, 500);
            weights["data"] = new JObject { ["training_multipliers"] = new JObject(), ["component_factors"] = new JObject() };
            nodes.Add(weights); edges.Add(UmaWeightGraphModel.NewEdge((string)weights["id"]!, "value", ruleId, "weights"));
        }
        var data = (JObject)weights["data"]!;
        var map = data["training_multipliers"] as JObject;
        if (map == null) { map = new JObject(); data["training_multipliers"] = map; }
        foreach (var key in TrainingKeys)
            if (restore) map.Remove(key); else if (values[key] != null) map[key] = values[key]!.DeepClone();
        return result;
    }

    public static string WindowText(JToken? value)
    {
        if (value is not JObject window) return "全部回合";
        string Date(JToken? date) => date is JObject d ? $"第{d["year"]}年{d["month"]}月{((string?)d["half"] == "second" ? "下" : "上")}" : "";
        var label = (string?)window["phase"] switch { "year" => $"第{window["year"]}年", "pre_debut" => "出道前", "dated" => "指定日期", _ => "全部回合" };
        return label + (window["from"] is JObject ? " · " + Date(window["from"]) + "起" : "") + (window["to"] is JObject ? " 至" + Date(window["to"]) : "");
    }
}
