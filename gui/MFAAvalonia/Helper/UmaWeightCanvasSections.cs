using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Navigation and visibility only. Never normalizes or changes the stored graph.</summary>
public static class UmaWeightCanvasSections
{
    public record Stage(string Id,string Label,string Window,int? Year);
    public record Arrangement(string Id,string Label,string Condition,int Priority);

    private static IEnumerable<JObject> Nodes(JObject graph) => (graph["nodes"] as JArray ?? new()).OfType<JObject>();
    private static IEnumerable<JObject> Edges(JObject graph) => (graph["edges"] as JArray ?? new()).OfType<JObject>();
    private static JObject? Node(JObject graph,string? id) => Nodes(graph).FirstOrDefault(n=>(string?)n["id"]==id);
    private static string? Next(JObject graph,string id,string port) => (string?)Edges(graph).FirstOrDefault(e=>(string?)e["from"]==id&&(string?)e["port"]==port)?["to"];

    public static Stage[] Stages(JObject graph)
    {
        var result=new List<Stage>();var seen=new HashSet<string>();var id=Next(graph,(string?)graph["entry"]??"","next");
        void Add(JObject node){var window=node["data"]?["window"];result.Add(new Stage((string)node["id"]!,(string?)node["label"]??"未命名时间段",WindowText(window),Year(window)));}
        while(id!=null&&seen.Add(id)&&Node(graph,id) is JObject stage&&(string?)stage["type"]=="stage") {Add(stage);id=Next(graph,id,"next");}
        foreach(var extra in Nodes(graph).Where(n=>(string?)n["type"]=="stage"&&!seen.Contains((string)n["id"]!)))Add(extra);
        return result.ToArray();
    }

    public static Arrangement[] Arrangements(JObject graph,string stageId)
    {
        var result=new List<Arrangement>();var seen=new HashSet<string>();var id=Next(graph,stageId,"rules");
        while(id!=null&&seen.Add(id)&&Node(graph,id) is JObject rule&&(string?)rule["type"]=="rule") {
            var edge=Edges(graph).FirstOrDefault(e=>(string?)e["to"]==id&&(string?)e["input"]=="condition");
            result.Add(new Arrangement(id,(string?)rule["label"]??"未命名安排",ConditionText(Node(graph,(string?)edge?["from"])?["data"]?["when"]),result.Count+1));
            id=Next(graph,id,"next");
        }
        return result.ToArray();
    }

    public static HashSet<string> Dependencies(JObject graph,string root,bool includeBase=true)
    {
        var ids=new HashSet<string>();
        void Include(string id) {
            if(Node(graph,id)==null||!ids.Add(id))return;
            foreach(var edge in Edges(graph).Where(e=>(string?)e["to"]==id&&(string?)e["input"]!="flow"&&(includeBase||(string?)e["input"]!="base")))
                if((string?)edge["from"] is string source)Include(source);
        }
        Include(root);return ids;
    }

    public static HashSet<string> VisibleNodes(JObject graph,string stageId,string? arrangementId)
    {
        var ids=new HashSet<string>();
        if(arrangementId!=null&&Arrangements(graph,stageId).Any(r=>r.Id==arrangementId))ids.UnionWith(Dependencies(graph,arrangementId,false));
        else if(Node(graph,stageId)!=null)ids.Add(stageId);
        return ids;
    }

    public static string? FindArrangement(JObject graph,string stageId,string nodeId) => Arrangements(graph,stageId)
        .FirstOrDefault(r=>r.Id==nodeId||Dependencies(graph,r.Id).Contains(nodeId))?.Id;

    private static int? Year(JToken? window)
    {
        if(window is not JObject)return null;
        if((string?)window?["phase"]=="pre_debut")return 1;
        if(window?["year"]?.Type==JTokenType.Integer)return (int)window["year"]!;
        var from=(window?["from"] as JObject)?["year"];var to=(window?["to"] as JObject)?["year"];
        if(from?.Type==JTokenType.Integer&&to?.Type==JTokenType.Integer)return (int)from==(int)to?(int)from:null;
        return from?.Type==JTokenType.Integer?(int)from:to?.Type==JTokenType.Integer?(int)to:null;
    }

    private static string WindowText(JToken? window)
    {
        if(window is not JObject value)return "未限定日期";
        string Date(JToken? d)=>d is JObject?$"第{d["year"]}年{d["month"]}月{((string?)d["half"]=="second"?"下":"上")}":"";
        var phase=(string?)value["phase"] switch{"year"=>$"第{value["year"]}年","pre_debut"=>"出道前","dated"=>"自定日期",_=>"未限定日期"};
        return phase+(value["from"] is JObject?" · "+Date(value["from"])+"起":"")+(value["to"] is JObject?" 至"+Date(value["to"]):"");
    }

    private static string ConditionText(JToken? when)
    {
        if(when==null)return "无附加条件（按优先级）";
        if(when is not JObject value||value.Count!=1||value.Properties().First().Name is not ("all" or "any")||value.Properties().First().Value is not JArray clauses)return "高级条件（保留原文）";
        var any=value.Property("any")!=null;if(clauses.Count==0)return any?"任一满足（空条件）":"无附加条件（按优先级）";
        var fields=new Dictionary<string,string>{{"vitality","体力"},{"speed","速度"},{"stamina","耐力"},{"power","力量"},{"guts","毅力"},{"wisdom","智力"},{"skill_pt","技能Pt"},{"calendar_year","年份"},{"calendar_month","月份"},{"calendar_turn","日历回合"},{"remaining_turns","剩余回合"}};
        var ops=new Dictionary<string,string>{{"lt","小于"},{"lte","不高于"},{"eq","等于"},{"gte","不低于"},{"gt","大于"}};var result=new List<string>();
        foreach(var token in clauses){if(token is not JObject item||!fields.TryGetValue((string?)item["field"]??"",out var field)||!ops.TryGetValue((string?)item["op"]??"",out var op)||item["value"]==null)return "高级条件（保留原文）";result.Add(field+op+item["value"]);}
        return (any?"任一满足：":"全部满足：")+string.Join("；",result);
    }
}
