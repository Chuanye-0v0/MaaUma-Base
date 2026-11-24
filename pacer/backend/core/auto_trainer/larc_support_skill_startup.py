"""Shared support activation, pure and loadable inside the isolated D1 worker.

No ranking model, skill-page opening, account lookup or training gain injection.
"""
from copy import deepcopy
import math
import re

DEFAULTS={"max_cost":180,"min_discount":10.,"spend_fraction":.6,"pt_change_trigger":80,"min_check_gap":2,"uncertain_recheck_multiplier":2}
LEGACY_MODE="legacy_speed_startup"
GENERAL_MODE="allowlist_v1"
# Existing D1 preference contract: max_batch_size must be 1..48.
GENERAL_BATCH_LIMIT=48
MID_DEFAULTS={"mode":LEGACY_MODE,"enabled":False,"interval_enabled":True,"interval_turns":6,
    "dates":[],"min_discount":10.,"max_cost":180,"spend_percent":60.}


def normalize_mid_skill_learning(value=None, *, legacy_enabled=False):
    """Explicit per-slot settings; absent legacy intent is decided by caller."""
    result=deepcopy(MID_DEFAULTS)
    if value is None:
        result["enabled"]=legacy_enabled is True
        return result
    if not isinstance(value,dict) or set(value)-set(result):
        raise ValueError("mid_skill_learning_options_invalid")
    result.update(deepcopy(value))
    if type(result["mode"]) is not str or result["mode"] not in {LEGACY_MODE,GENERAL_MODE}:raise ValueError("mid_skill_learning_mode_invalid")
    for key in ("enabled","interval_enabled"):
        if type(result[key]) is not bool:raise ValueError("mid_skill_learning_boolean_required:"+key)
    for key,minimum in (("interval_turns",1),("max_cost",0)):
        if type(result[key]) is not int or result[key]<minimum:
            raise ValueError("mid_skill_learning_integer_invalid:"+key)
    for key in ("min_discount","spend_percent"):
        number=result[key]
        if type(number) not in {int,float} or not math.isfinite(number) or not 0<=number<=100:
            raise ValueError("mid_skill_learning_percent_invalid:"+key)
    if not isinstance(result["dates"],list):raise ValueError("mid_skill_learning_dates_invalid")
    dates={}
    _date_allowed={"year_index","month","half","min_discount","max_cost","spend_percent"}
    for value in result["dates"]:
        if not isinstance(value,dict) or set(value)-_date_allowed:
            raise ValueError("mid_skill_learning_date_invalid")
        date=game_date(value)
        if date is None:
            raise ValueError("mid_skill_learning_date_invalid")
        entry=dict(date)
        if "max_cost" in value and value["max_cost"] is not None:
            mc=value["max_cost"]
            if type(mc) is not int or mc<0:
                raise ValueError("mid_skill_learning_date_invalid")
            entry["max_cost"]=mc
        for pkey in ("min_discount","spend_percent"):
            if pkey in value and value[pkey] is not None:
                num=value[pkey]
                if type(num) not in {int,float} or not math.isfinite(num) or not 0<=num<=100:
                    raise ValueError("mid_skill_learning_date_invalid")
                entry[pkey]=num
        dates[date_key(entry)]=entry
    result["dates"]=sorted(dates.values(),key=lambda v:(v["year_index"],v["month"],v["half"]=="second"))
    return result


def new_mid_skill_learning(value):
    """Adopt general allowlist semantics for new runs unless caller explicitly chose a mode."""
    result=normalize_mid_skill_learning(value)
    if not (isinstance(value,dict) and value.get("mode") in {LEGACY_MODE,GENERAL_MODE}):
        result["mode"]=GENERAL_MODE
    return result


def learning_mode(timing):
    return normalize_mid_skill_learning(getattr(timing,"mid_skill_learning",None))["mode"]


def learning_completion(target,held):
    if target.get("mode")==GENERAL_MODE:
        return {"completed":False,"safe_remaining":None}
    required=target.get("required",5)
    return {"completed":held["confirmed_count"]>=required,
        "safe_remaining":max(0,required-held["possible_count"])}


def waiting_status(ledger,target,*,run_id,turn,points,timing,period=None):
    mode=learning_mode(timing)
    result={"mode":mode,"status":"waiting","reason":"waiting_trigger","message":"等待中途技能检查时间"}
    if not target.get("enabled"):
        reason=target.get("reason","mid_skill_learning_disabled")
        return {**result,"status":"skipped" if reason=="early_skill_allowlist_empty" else "disabled",
            "reason":reason,"message":"中途学习已启用，请先选择允许学习的技能" if reason=="early_skill_allowlist_empty" else "中途技能学习已关闭"}
    if ledger.get("status")=="pending_purchase":
        return {**result,"status":"pending","reason":"pending_purchase","message":"正在收尾已确认的技能学习"}
    if ledger.get("completed") is True and mode!=GENERAL_MODE:
        return {**result,"status":"complete","reason":"legacy_speed_target_complete","message":"旧版高峰启动目标已完成：已持有足够速度技能"}
    if type(points) is not int:
        return {**result,"reason":"skill_pt_unknown","message":"等待识别当前技能Pt"}
    if points<timing.min_pt:
        return {**result,"reason":"below_min_pt","message":f"技能Pt不足{timing.min_pt}，等待下一回合"}
    settings=normalize_mid_skill_learning(getattr(timing,"mid_skill_learning",None))
    last=ledger.get("last_checked_turn")
    if settings["interval_enabled"] and type(last) is int:
        result.update(reason="interval_wait",next_check_turn=last+settings["interval_turns"],
            message=f"等待第{last+settings['interval_turns']}回合检查中途技能")
    elif not settings["interval_enabled"] and not settings["dates"]:
        result.update(status="skipped",reason="no_triggers",message="尚未设置中途学习间隔或日期")
    elif not settings["interval_enabled"]:
        result.update(reason="date_wait",message="等待已设置的游戏日期检查中途技能")
    return result


def learning_status_snapshot(run_root):
    """Compact local status only; never capture or operate a device."""
    import json
    from pathlib import Path
    root=Path(run_root)
    try:
        ledger=json.loads((root/"session/larc_support_skill_startup.json").read_text(encoding="utf-8-sig"))
        if isinstance(ledger,dict) and ledger.get("run_session_id")==root.name and ledger.get("status")=="pending_purchase":
            return {"schema_version":"uma.mid_skill_learning_status.v1","run_session_id":root.name,
                "mode":ledger.get("target",{}).get("mode",LEGACY_MODE),"status":"pending",
                "reason":"pending_purchase","message":"等待已提交技能学习的确认回执","turn_index":None}
    except (OSError,ValueError,TypeError):pass
    try:
        data=json.loads((root/"session/mid_skill_learning_status.json").read_text(encoding="utf-8-sig"))
        if isinstance(data,dict) and data.get("run_session_id")==root.name:return data
    except (OSError,ValueError,TypeError):pass
    try:
        goal=json.loads((root/"goal.json").read_text(encoding="utf-8-sig"))
        timing=goal.get("skill_timing",{})
        settings=normalize_mid_skill_learning(timing.get("mid_skill_learning"))
        enabled=settings["enabled"] if timing.get("mid_skill_learning") is not None else bool(timing.get("early_skill_allowlist"))
        return {"schema_version":"uma.mid_skill_learning_status.v1","run_session_id":root.name,
            "mode":settings["mode"],"status":"waiting" if enabled else "disabled",
            "reason":"awaiting_main_menu" if enabled else "mid_skill_learning_disabled",
            "message":"等待主菜单检查中途技能" if enabled else "中途技能学习已关闭","turn_index":None}
    except (OSError,ValueError,TypeError):return None


def game_date(value):
    if not isinstance(value,dict):return None
    year,month,half=value.get("year_index"),value.get("month"),value.get("half")
    if (type(year) is not int or year not in (1,2,3)
            or type(month) is not int or not 1<=month<=12 or half not in ("first","second")):
        return None
    return {"year_index":year,"month":month,"half":half}


def date_key(value):
    return f'{value["year_index"]}-{value["month"]:02d}-{value["half"]}'


def execution_options(timing,*,period=None):
    options=deepcopy(getattr(timing,"early_options",{}))
    configured=getattr(timing,"mid_skill_learning",None)
    if configured is not None:
        settings=normalize_mid_skill_learning(configured)
        options.update(max_cost=settings["max_cost"],min_discount=settings["min_discount"],
            spend_fraction=settings["spend_percent"]/100)
        date=game_date(period)
        if date is not None:
            dkey=date_key(date)
            for entry in settings["dates"]:
                if date_key(entry)==dkey:
                    if "max_cost" in entry:options["max_cost"]=entry["max_cost"]
                    if "min_discount" in entry:options["min_discount"]=entry["min_discount"]
                    if "spend_percent" in entry:options["spend_fraction"]=entry["spend_percent"]/100
                    break
    return startup_options(options)

def startup_options(options):
    import math
    if not isinstance(options,dict) or set(options)-set(DEFAULTS):raise ValueError("skill_timing.early_options_unknown")
    settings={**DEFAULTS,**options}
    for key in ("max_cost","pt_change_trigger","min_check_gap","uncertain_recheck_multiplier"):
        value=settings[key]
        minimum=2 if key in {"min_check_gap","uncertain_recheck_multiplier"} else 0 if key=="max_cost" else 1
        if type(value) is not int or value<minimum:raise ValueError("skill_timing.early_options_invalid:"+key)
    for key,limit in (("min_discount",100),("spend_fraction",1)):
        value=settings[key]
        if type(value) not in {int,float} or not math.isfinite(value) or not 0<=value<=limit:raise ValueError("skill_timing.early_options_invalid:"+key)
    return settings


def startup_target(profile,run_id,timing):
    configured=getattr(timing,"mid_skill_learning",None)
    if configured is not None and not normalize_mid_skill_learning(configured)["enabled"]:
        return {"enabled":False,"reason":"mid_skill_learning_disabled"}
    if not getattr(timing,"early_skill_allowlist",[]):
        return {"enabled":False,"reason":"early_skill_allowlist_empty"}
    if configured is not None:
        mode=learning_mode(timing)
        return {"enabled":True,"required":len(timing.early_skill_allowlist) if mode==GENERAL_MODE else 5,"mode":mode,"basis":"explicit_mid_skill_learning"}
    explicit=getattr(timing,"early_required",{}).get("speed")
    if type(explicit) is int and explicit>0:
        return {"enabled":True,"required":explicit,"basis":"explicit_skill_timing.early_required.speed"}
    if not isinstance(profile,dict) or profile.get("schema_version")!="uma_run_support_profile.v1" or profile.get("run_session_id")!=run_id:
        return {"enabled":False,"reason":"run_support_profile_missing_or_mismatched"}
    for card in profile.get("cards",[]):
        rarity=card.get("rarity");kind=card.get("type") or card.get("card_type")
        if rarity not in {None,"SSR"} or kind not in {None,"wisdom","智力"}:continue
        if type(card.get("level")) is int and card["level"]<30:continue
        sid=str(card.get("card_id") or "")
        exact=re.search(r"(?:^|[:_/])30134$",sid) is not None
        title=card.get("title") or card.get("card_name") or card.get("name") or ""
        named=(card.get("character_id") in {"mejiro_ramonu","mejiro_ramone"} and rarity=="SSR" and kind in {"wisdom","智力"} and "燦爛" in title)
        if exact or named:return {"enabled":True,"required":5,"card_id":"30134","basis":"run_local_Ramonu_SSR_wisdom_unique"}
    return {"enabled":False,"reason":"no_matching_support_or_explicit_target"}

def speed_effect(row):
    if not isinstance(row,dict):return None
    if row.get("type")=="green":return False
    knowledge=row.get("knowledge")
    if not isinstance(knowledge,dict):knowledge={}
    all_effects=[]
    effects=knowledge.get("effects",row.get("effects"))
    if isinstance(effects,list):
        all_effects.extend(effects)
    branches=knowledge.get("activation_branches",row.get("activation_branches"))
    if isinstance(branches,list):
        for branch in branches:
            if isinstance(branch,dict) and isinstance(branch.get("effects"),list):
                all_effects.extend(branch["effects"])
    if all_effects:
        if any(
            isinstance(e,dict)
            and e.get("kind") in {"speed","instant_speed","current_speed","target_speed"}
            and not any(bad in str(e.get("type_raw","")) for bad in ("被动","减速","降低","目标速度降低"))
            and type(e.get("value")) in {int,float}
            and e["value"]>0
            for e in all_effects
        ):
            return True
        if any(not isinstance(e,dict) or e.get("kind") in {None,"unknown"} for e in all_effects):
            type_raw=str(knowledge.get("type_raw") or row.get("type_raw") or "")
            if "速度" in type_raw and not any(bad in type_raw for bad in ("被动","减速","降低","目标速度降低")):
                return True
            return None
        return False # acceleration/green/debuff/lateral-only are not speed
    if row.get("type")=="speed":return True
    type_raw=str(knowledge.get("type_raw") or row.get("type_raw") or "")
    if "速度" in type_raw and not any(bad in type_raw for bad in ("被动","减速","降低","目标速度降低")):
        return True
    if row.get("type") in {"acceleration","green","recovery","debuff"}:return False
    return None

def catalog_rows(catalog):
    return {r["skill_id"]:r for r in catalog.get("skills",[]) if isinstance(r,dict) and r.get("skill_id")}

def relations(row):
    r=row.get("relations",{})
    return set(r.get("lower",[]))|set(r.get("supersedes",[]))

def held_speed_skills(owned_ids,catalog,*,unknown_owned=0,native_known=False):
    rows=catalog_rows(catalog);owned=set(owned_ids);covered=set()
    for sid in owned:covered.update(relations(rows.get(sid,{})))
    active=owned-covered;confirmed=[];unknown=[]
    for sid in sorted(active):
        effect=speed_effect(rows.get(sid))
        if effect is True:confirmed.append(sid)
        elif effect is None:unknown.append(sid)
    # Missing a native observation must not be silently interpreted as zero.
    native_seen=native_known or any(rows.get(sid,{}).get("rarity")=="unique" and rows.get(sid,{}).get("base_cost")==0 for sid in owned)
    unresolved=max(0,unknown_owned)+len(unknown)+(0 if native_seen else 1)
    return {"confirmed_count":len(confirmed),"confirmed_skill_ids":confirmed,"owned_skill_ids":sorted(owned),
        "unknown_skill_ids":unknown,"unknown_count":unresolved,"native_observation_covered":bool(native_seen),
        "possible_count":len(confirmed)+unresolved,"basis":"observed_owned/verified_learning; no race-condition filter"}

def owned_parser_observation(report,inventory,catalog):
    rows=catalog_rows(catalog);known=set(inventory.get("owned_skill_ids",[]))
    owned_names={rows.get(sid,{}).get("name_zh_cn") for sid in known if rows.get(sid,{}).get("name_zh_cn")}
    manifests=[report.get("parser_manifest",{})]
    for page in report.get("parsed_pages",[]):
        if isinstance(page,dict):manifests.append(page.get("manifest",page.get("parser_manifest",page)))
    unknown=set();native_covered=False
    for manifest in manifests:
        for row in manifest.get("skills",[]):
            if row.get("acquired") is not True:continue
            evidence=row.get("source_evidence",[])
            # First acquired row on first skill page covers the native slot;
            # this is coverage only, never a claim about its speed effect.
            if any(e.get("page_index")==0 and e.get("row_index")==0 for e in evidence):native_covered=True
            if row.get("skill_id") in known or row.get("name") in owned_names:continue
            key=row.get("row_fingerprint") or row.get("name") or str(evidence)
            unknown.add(key)
    return {"unknown_owned":len(unknown),"native_observation_covered":native_covered}


def applicable(row,race):
    conditions=row.get("conditions")
    if not isinstance(conditions,dict):return False
    for key,field in (("distance","distance_type"),("style","style"),("ground_condition","ground_condition"),("season","season")):
        values=conditions.get(key)
        if values is not None and (not isinstance(values,list) or ("any" not in values and race.get(field) not in values)):return False
    multiple=conditions.get("distance_m_multiple_of")
    if multiple is not None and (type(multiple) is not int or multiple<=0 or type(race.get("distance_m")) is not int or race["distance_m"]%multiple):return False
    # Other unhandled constraints are not magically universal.
    if set(conditions)-{"distance","style","ground_condition","season","distance_m_multiple_of","phase_hint"}:return False
    return True

def resolve_allowlist(allowlist,catalog):
    """Exact existing U3/D1 catalog names or IDs; no custom aliases/fuzzy match."""
    rows=catalog_rows(catalog);allowed=set();diagnostics=[]
    for text in allowlist or []:
        if not isinstance(text,str):continue
        text=text.strip()
        if text in rows and pt_learnable(rows[text]):
            allowed.add(text);diagnostics.append({"entry":text,"skill_id":text,"status":"exact_id"});continue
        matches=[sid for sid,row in rows.items() if row.get("name_zh_cn")==text
            and pt_learnable(row)]
        if len(matches)==1:
            allowed.add(matches[0]);diagnostics.append({"entry":text,"skill_id":matches[0],"status":"exact_name"})
        else:diagnostics.append({"entry":text,"status":"ambiguous_name_use_id" if matches else "unknown_name_or_id"})
    return allowed,diagnostics


def pt_learnable(row):
    kind=row.get("knowledge",{}).get("skill_kind")
    return row.get("base_cost")!=0 and (kind is None or kind in {"purchasable","inherited_unique"})


def startup_preference(inventory,catalog,*,required,race_profile=None,extra_owned=(),unknown_owned=0,native_known=False,options=None,allowlist=None,mode=LEGACY_MODE):
    general=mode==GENERAL_MODE
    settings=startup_options(options or {});rows=catalog_rows(catalog)
    allowed,allowlist_status=resolve_allowlist(allowlist,catalog)
    owned=set(inventory.get("owned_skill_ids",[]))|set(extra_owned)
    held=held_speed_skills(owned,catalog,unknown_owned=unknown_owned,native_known=native_known)
    # Conservatively reserve possible already-owned speed skills. Unknown
    # does not imply completed, but must not cause five needless purchases.
    missing=len(allowed-owned) if general else max(0,required-held["possible_count"])
    points=inventory.get("current_skill_points",0)
    points=points if type(points) is int and points>=0 else 0
    budget=int(points*settings["spend_fraction"]);eligible=[]
    for item in inventory.get("candidates",[]):
        sid=item.get("skill_id");row=rows.get(sid,{})
        if sid not in allowed or not pt_learnable(row) or sid in owned or (not general and speed_effect(row) is not True) or row.get("requires_manual_verification") is True:continue
        if not general and relations(row)&set(held["confirmed_skill_ids"]):continue # replacing a speed lower gives no extra count
        if set(row.get("relations",{}).get("upper",[]))&owned:continue
        if set(row.get("relations",{}).get("exclusive",[]))&owned:continue
        cost=item.get("display_cost");discount=item.get("observed_discount_percent")
        flags=item.get("risk_flags",[])
        if type(cost) is not int or cost<=0 or cost>settings["max_cost"] or item.get("cost_basis")!="observed_final" or item.get("recognition_confidence",0)<.8 or any(str(f).startswith("negative_") for f in flags):continue
        if type(discount) not in {int,float}:
            base=row.get("base_cost")
            discount=max(0.,100*(1-cost/base)) if type(base) in {int,float} and base>0 and not relations(row) and (not general or cost<=base) else None
        if discount is None or not math.isfinite(discount) or (discount<0 if general else discount<=0) or discount<settings["min_discount"]:continue
        eligible.append({"skill_id":sid,"cost":cost,"discount":discount,"row":row})
    eligible.sort(key=lambda r:(r["cost"],-r["discount"],r["skill_id"]))
    selected=[];covered=set();spent=0
    for item in eligible:
        if len(selected)>=(min(GENERAL_BATCH_LIMIT,missing) if general else min(5,missing)):break
        if item["skill_id"] in covered or relations(item["row"])&set(selected):continue
        if spent+item["cost"]>budget:continue
        selected.append(item["skill_id"]);spent+=item["cost"]
        covered.update(relations(item["row"]));covered.update(item["row"].get("relations",{}).get("exclusive",[]))
    rules=[]
    for item in inventory.get("candidates",[]):
        sid=item.get("skill_id")
        if sid:rules.append({"selector":{"kind":"skill_id","value":sid},"mode":"must_buy_if_safe" if sid in selected else "avoid","priority":0,"weight":0.})
    profile={"schema_version":"uma.skill_preference_profile.v1","profile_id":"larc-support-speed-startup",
        "base_profile":"parent","reserve_skill_pt":points-spent,"filler_policy":"safe_useful_only",
        "max_batch_size":max(1,len(selected)),"rules":rules}
    return profile,{"held":held,"required":required,"safe_remaining":missing,"selected_skill_ids":selected,
        "spending_budget":spent,"options":settings,"allowlist_resolution":allowlist_status,"authorized_skill_ids":sorted(allowed),"mode":mode,"deferred_by_batch_limit":max(0,len(eligible)-len(selected)) if general and len(selected)>=GENERAL_BATCH_LIMIT else 0,"status":"partial_or_no_candidate" if general else "confirmed_complete" if held["confirmed_count"]>=required else "uncertain_owned" if not missing else "partial_or_no_candidate"}

def due_reasons(ledger,target,*,run_id,turn,points,timing,information_revision=None,period=None):
    if not target.get("enabled") or type(points) is not int or points<timing.min_pt:return []
    if ledger.get("run_session_id") not in {None,run_id}:return []
    if (ledger.get("completed") is True and target.get("mode")!=GENERAL_MODE) or ledger.get("status")=="pending_purchase":return []
    last=ledger.get("last_checked_turn")
    if last==turn:return []
    configured=getattr(timing,"mid_skill_learning",None)
    if configured is not None:
        settings=normalize_mid_skill_learning(configured)
        if not settings["enabled"]:return []
        reasons=[]
        date=game_date(period)
        checked=ledger.get("checked_dates",[])
        if date is not None and any(date_key(entry)==date_key(date) for entry in settings["dates"]) and date_key(date) not in checked:
            reasons.append("date:"+date_key(date))
        if settings["interval_enabled"] and (type(last) is not int or turn-last>=settings["interval_turns"]):
            reasons.append("interval")
        return reasons
    settings=startup_options(getattr(timing,"early_options",{}))
    if ledger.get("safe_remaining")==0 and ledger.get("completed") is not True:
        due=(information_revision is not None and information_revision!=ledger.get("information_revision")) or (type(last) is int and turn-last>=max(settings["min_check_gap"],timing.interval_turns*settings["uncertain_recheck_multiplier"]))
        return ["legacy_interval"] if due else []
    if type(last) is not int:return ["legacy_interval"]
    gap=turn-last
    due=gap>=max(settings["min_check_gap"],timing.interval_turns) or (gap>=settings["min_check_gap"] and (points-ledger.get("last_checked_pt",points)>=settings["pt_change_trigger"] or information_revision is not None and information_revision!=ledger.get("information_revision")))
    return ["legacy_interval"] if due else []


def check_due(ledger,target,*,run_id,turn,points,timing,information_revision=None,period=None):
    return bool(due_reasons(ledger,target,run_id=run_id,turn=turn,points=points,timing=timing,
        information_revision=information_revision,period=period))
