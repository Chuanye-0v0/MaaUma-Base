using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace MFAAvalonia.Helper;

/// <summary>Copies only advisory data; run settings and identity remain owned by their existing editors.</summary>
public static class UmaKnowledgeToolsContext
{
    private static readonly string[] Stats={"speed","stamina","power","guts","wisdom"};
    private static readonly int[] GoalDefaults={1550,1200,1200,1200,1200};
    private static readonly string[] NodeKeys={"parent_a","gp_a1","gp_a2","parent_b","gp_b1","gp_b2"};
    private static readonly string[] NodeFields={"entry_id","trained_chara_id","character_id","character_name","variant_id","variant_name","outfit_id","outfit_name","status","availability","ownership","owner_account_id","borrowable_by","source_server","identity_status","red_factors","stats","red_target","ancestry_bound_to_entry"};
    private static JObject Fields(JObject value,IEnumerable<string> keys)
    {var copy=new JObject();foreach(var key in keys)if(value.Property(key)!=null)copy[key]=value[key]!.DeepClone();return copy;}
    private static JObject Node(JObject row)
    {
        var copy=Fields(row,NodeFields);
        if(row["source"] is JObject source)copy["source"]=Fields(source,new[]{"kind","status","id","revision","server"});
        if(row["self"] is JObject self)copy["self"]=Node(self);
        if(row["parents"] is JArray parents)copy["parents"]=new JArray(parents.Select(p=>p is JObject obj?(JToken)Node(obj):JValue.CreateNull()));
        var raceFields=new[]{"race_id","id","event_key","name","raw_name","grade","status","confirmed","venue","surface","distance_m"};
        foreach(var key in new[]{"wins","g1_wins","planned_wins","planned_races"})if(row[key] is JArray races)copy[key]=new JArray(races.Select(r=>r is JObject race?(JToken)Fields(race,raceFields):r.DeepClone()));
        if(row["race_records"] is JObject records){var values=Fields(records,new[]{"entry_id","coverage"});if(records["wins"] is JArray wins)values["wins"]=new JArray(wins.OfType<JObject>().Select(r=>Fields(r,raceFields)));copy["race_records"]=values;}
        if(row["factors"] is JArray factors)copy["factors"]=new JArray(factors.OfType<JObject>().Select(f=>Fields(f,new[]{"kind","type","factor_key","name","stars","status","level","skill_id","name_status"})));
        return copy;
    }
    public static JObject Planning(JObject state,string? accountId,string label,JObject requirements,JObject? member,JObject? route=null)
    {
        var rows=(state["entries"] as JArray??new()).OfType<JObject>().Where(row=>accountId==null
            ||(string?)row["owner_account_id"]==accountId||(row["borrowable_by"] as JArray??new()).Any(v=>(string?)v==accountId));
        var entries=new JArray();foreach(var row in rows) {
            var copy=Node(row);
            if(accountId==null){copy["status"]="unknown";copy["availability"]="reference";copy["reference_only"]=true;}
            else {var prior=(string?)row["status"];var known=(string?)row["availability"];
                copy["availability"]=prior is "unknown" or "planned"?prior:known is "unknown" or "planned"?known:
                    (string?)row["owner_account_id"]==accountId?"owned":"borrowable";}
            entries.Add(copy);
        }
        var context=new JObject{["schema_version"]="uma.toolbox.context.v1",["server"]="cn",
            ["account"]=accountId==null?null:new JObject{["id"]=accountId,["label"]=label},
            ["library"]=new JObject{["library_id"]=state["library_id"]?.DeepClone(),["revision"]=state["library_revision"]?.DeepClone(),["entries"]=entries},
            ["requirements"]=requirements.DeepClone(),["source_revisions"]=new JObject{["library_revision"]=state["library_revision"]?.DeepClone(),["inventory_revision"]=state["inventory_revision"]?.DeepClone()}};
        if(member!=null){context["target_character_id"]=member["character_id"]?.DeepClone();
            if(member["target_stats"] is JObject target)context["targets"]=Fields(target,Stats);
            if(member["core_skills"] is JArray skills)context["skill_targets"]=new JArray(skills.OfType<JObject>().Select(s=>Fields(s,new[]{"skill_id","id","db_id","name","raw_name","priority"})));}
        return context;
    }
    public static JObject Run(JObject options,string kind,string policyId,string stepId)
    {
        var value=new JObject{["schema_version"]="uma.toolbox.context.v1",["server"]="cn",["source_revisions"]=new JObject{["mode"]=kind,["policy_id"]=policyId,["step_id"]=stepId,["strategy_revision"]=(options["larc_policy"] as JObject)?["revision"]?.DeepClone()}};
        if(options["targets"] is JObject targets)value["targets"]=Fields(targets,Stats);
        if(options["support_profile"] is JObject support){var copy=Fields(support,new[]{"schema_version"});if(support["source"] is JObject source)copy["source"]=Fields(source,new[]{"kind","status","id","revision"});if(support["cards"] is JArray cards)copy["cards"]=new JArray(cards.OfType<JObject>().Select(c=>Fields(c,new[]{"slot","type","card_id","level","limit_breaks"})));value["support_profile"]=copy;}
        var allowlist=options["early_skill_allowlist"]??(options["mid_skill_learning"] as JObject)?["allowlist"];
        if(allowlist is JArray skills)value["skill_targets"]=skills.DeepClone();
        return value;
    }
    public static JObject PatchRun(JObject options,JObject draft,IEnumerable<(string Id,string Name,string DbId)> catalog,out string notice)
    {
        var next=(JObject)options.DeepClone();var notes=new List<string>();
        if(draft["targets"] is JObject targets&&targets.Count>0) {
            var changes=new JObject();foreach(var key in Stats)if(targets.Property(key)!=null) {
                if(targets[key]?.Type!=JTokenType.Integer||(int)targets[key]! is <0 or >3000)throw new InvalidOperationException("工具箱属性目标需为0—3000整数；当前草稿保留。");
                changes[key]=targets[key]!.DeepClone();}
            if(changes.Count>0){var output=next["targets"] as JObject;if(output==null){output=new JObject();for(var i=0;i<Stats.Length;i++)output[Stats[i]]=GoalDefaults[i];next["targets"]=output;}
                foreach(var property in changes.Properties())output[property.Name]=property.Value.DeepClone();}
        }
        if(draft["support_profile"] is JObject support) {
            var sourceKind=(string?)(support["source"] as JObject)?["kind"];
            if(sourceKind is not ("user_declared" or "toolbox_recommendation"))notes.Add("工具箱卡组来源尚非本次手动声明或推荐草稿，未替换卡组。");
            else if(support["cards"] is JArray cards) {
                var output=new JArray();var used=new HashSet<int>();foreach(var card in cards.OfType<JObject>()) {
                    if(card["slot"]?.Type!=JTokenType.Integer||(int)card["slot"]! is <0 or >5||!used.Add((int)card["slot"]!))throw new InvalidOperationException("工具箱卡位编号不完整或重复，未采用卡组。");
                    var type=(string?)card["type"];if(type!=null&&!new[]{"speed","stamina","power","guts","wisdom","friend","group"}.Contains(type))throw new InvalidOperationException("工具箱卡片类型尚未映射，当前卡组保留。");
                    foreach(var field in new[]{("level",1,50),("limit_breaks",0,4)})if(card[field.Item1]!=null&&card[field.Item1]!.Type!=JTokenType.Null&&(card[field.Item1]!.Type!=JTokenType.Integer||(int)card[field.Item1]!<field.Item2||(int)card[field.Item1]!>field.Item3))throw new InvalidOperationException("工具箱等级或突破格式无效，原卡组保留。");
                    output.Add(Fields(card,new[]{"slot","type","card_id","level","limit_breaks"}));
                }
                var profile=new JObject{["schema_version"]="uma_run_support_profile.v1",["source"]=new JObject{["kind"]=sourceKind},["cards"]=output};
                if((options["support_profile"] as JObject)?["reference_snapshot"] is JToken reference)profile["reference_snapshot"]=reference.DeepClone();
                else if(support["reference_snapshot"] is JObject incomingReference)profile["reference_snapshot"]=incomingReference.DeepClone();next["support_profile"]=profile;
                if(sourceKind=="toolbox_recommendation")notes.Add("推荐卡组作参考，核对游戏后用作本次填写；不自动换卡。");
            }
        }
        if(draft["skill_targets"] is JArray selected) {
            var all=catalog.ToArray();var resolved=new JArray();var unknown=new List<string>();
            foreach(var value in selected) {
                var row=value as JObject;var id=row==null?(string?)value:(string?)(row["skill_id"]??row["id"]);var name=(string?)(row?["name"]??row?["raw_name"]);
                var byId=all.Where(x=>x.Id==id||x.DbId.Length>0&&x.DbId==id).ToArray();
                var databaseId=row?["db_id"]?.Type is JTokenType.String or JTokenType.Integer?row["db_id"]!.ToString():null;
                var matches=byId;
                if(!string.IsNullOrWhiteSpace(databaseId)) {
                    matches=all.Where(x=>x.DbId==databaseId).ToArray();
                    if(matches.Length==1&&byId.Length==1&&matches[0].Id!=byId[0].Id){unknown.Add((name??id??databaseId)+"（编号冲突）");continue;}
                    if(matches.Length!=1){unknown.Add((name??id??databaseId)+"（数据库编号待核对）");continue;}
                } else {
                    if(matches.Length!=1&&name!=null)matches=all.Where(x=>x.Name==name).ToArray();
                    if(matches.Length!=1&&row==null)matches=all.Where(x=>x.Name==id).ToArray();
                }
                if(matches.Length==1){if(!resolved.Any(x=>(string?)x==matches[0].Id))resolved.Add(matches[0].Id);}else unknown.Add(name??id??"未命名技能");
            }
            var anyResolved=resolved.Count>0;
            if(unknown.Count>0){notes.Add("未映射技能已保留在工具箱："+string.Join("、",unknown)+"；当前旧名单保留，仅加入已唯一映射的技能。");
                var prior=(next["early_skill_allowlist"] as JArray??new()).DeepClone() as JArray??new();foreach(var id in resolved)if(!prior.Any(x=>JToken.DeepEquals(x,id)))prior.Add(id.DeepClone());resolved=prior;}
            if(unknown.Count==0||anyResolved) {
                next["early_skill_allowlist"]=resolved;
                if(next["mid_skill_learning"] is JObject mid&&mid.Property("allowlist")!=null)mid["allowlist"]=resolved.DeepClone();
            }
        }
        notice=string.Join("\n",notes);return next;
    }
    public static JObject RouteDraft(JObject current,JObject lineage,JArray entries,string? accountId,out string notice)
    {
        if(lineage["nodes"] is not JObject nodes)throw new InvalidOperationException("工具箱没有可取回的系谱节点。");
        var next=(JObject)current.DeepClone();var rows=(JArray)(next["nodes"] as JArray??new()).DeepClone();while(rows.Count<6)rows.Add(JValue.CreateNull());
        var unknown=0;for(var i=0;i<NodeKeys.Length;i++)if(nodes[NodeKeys[i]] is JObject node) {
            var entryId=(string?)node["entry_id"];var entry=entries.OfType<JObject>().FirstOrDefault(e=>(string?)e["entry_id"]==entryId);
            var usable=accountId!=null&&entry!=null&&(string?)entry["status"] is not ("unknown" or "planned")&&(string?)entry["availability"] is not ("unknown" or "planned")
                &&((string?)entry["owner_account_id"]==accountId||(entry["borrowable_by"] as JArray??new()).Any(x=>(string?)x==accountId));
            if(usable&&(string?)node["status"] is "owned" or "borrowable")rows[i]=new JObject{["kind"]="entry",["entry_id"]=entryId};
            else if(int.TryParse(node["character_id"]?.ToString(),out var id)&&id>0){rows[i]=new JObject{["kind"]="planned",["character_id"]=id,["variant_id"]=node["variant_id"]?.DeepClone(),["red_target"]=node["red_target"]?.DeepClone()};unknown++;}
            else unknown++;
        }
        next["nodes"]=rows;notice=unknown>0?"未关联可用成品的节点仅作为待培养参考；未知节点保留原草稿，实际胜场没有改写。":"已带回当前成员系谱草稿，实际胜场由原成品库核对。";return next;
    }
}
