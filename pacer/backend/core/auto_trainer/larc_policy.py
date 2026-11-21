"""Unified L'Arc policy with explicit, unchanged legacy branch compatibility."""
from __future__ import annotations
import json, math
from pathlib import Path
from copy import deepcopy
from core.auto_trainer.larc_baseline import (
    UNIFIED_VARIANT, UNIFIED_VERSION, UNIFIED_FEATURES,
    normalize_larc_policy, upgrade_larc_policy,
)
from core.auto_trainer.larc_strategy_params import AUTO_ROUTED_REVISIONS, params_for_goal, unified_larc_enabled
from core.auto_trainer.larc_strategy_versions import (
    strategy_revision, policy_version_for_revision, validate_policy_revision, _revision_version_key,
)

BASELINE = "larc_local_v1_18_6"
BASELINE_PREVIOUS = "larc_local_v1_18_5"
EXPERIMENTAL = "larc_resource_v1_19_8"
EXPERIMENTAL_OVERSEAS_ONLY = "larc_resource_v1_19_7"
EXPERIMENTAL_PREVIOUS = "larc_resource_v1_19_6"
REFINED_BASELINE = "larc_local_v1_18_9"
REFINED_EXPERIMENTAL = "larc_resource_v1_19_11"
# V2 includes first Mei contact as well as opening and healing preferences.
BALANCED_BASELINE = "larc_local_v1_18_21"
BALANCED_EXPERIMENTAL = "larc_resource_v1_19_23"
PURCHASE_V3_EXPERIMENTAL = "larc_resource_v1_20_1_preview"
STRATEGY_V4_EXPERIMENTAL = "larc_resource_v1_20_2_preview"

VARIANT_BRANCHES = {"alpha": "alpha", "baseline_v118": "alpha",
                    "beta": "beta", "resource_v119": "beta", "baseline": "baseline"}


def policy_branch(goal):
    variant = getattr(goal, "larc_policy", {}).get("variant", "alpha")
    if variant not in VARIANT_BRANCHES:
        raise ValueError("larc_policy_variant_unknown")
    return VARIANT_BRANCHES[variant] if getattr(goal, "scenario", None) == "larc" else "alpha"


def enabled(goal):
    return policy_branch(goal) in {"beta", UNIFIED_VARIANT}

def overseas_purchase_v2_enabled(goal):
    return enabled(goal) and getattr(goal, 'larc_policy', {}).get('overseas_purchase_v2', True)


def overseas_purchase_v3_enabled(goal):
    return overseas_purchase_v2_enabled(goal) and getattr(goal, 'larc_policy', {}).get(
        'overseas_purchase_v3', policy_branch(goal) == UNIFIED_VARIANT) is True

def strategy_v4_enabled(goal):
    return (getattr(goal, "scenario", None) == "larc"
        and getattr(goal, "larc_policy", {}).get("strategy_v4", policy_branch(goal) == UNIFIED_VARIANT) is True)


def policy_version(goal):
    if policy_branch(goal) == UNIFIED_VARIANT:
        return policy_version_for_revision(strategy_revision(goal))
    if strategy_v4_enabled(goal):
        return STRATEGY_V4_EXPERIMENTAL if enabled(goal) else "larc_local_v1_20_2_preview"
    if overseas_purchase_v3_enabled(goal):
        return PURCHASE_V3_EXPERIMENTAL
    if training_balance_v2_enabled(goal):
        return BALANCED_EXPERIMENTAL if enabled(goal) else BALANCED_BASELINE
    if strategy_refinement_enabled(goal):
        return REFINED_EXPERIMENTAL if enabled(goal) else REFINED_BASELINE
    if enabled(goal):
        return EXPERIMENTAL if opening_charge_enabled(goal) else EXPERIMENTAL_OVERSEAS_ONLY if overseas_purchase_v2_enabled(goal) else EXPERIMENTAL_PREVIOUS
    return BASELINE if opening_charge_enabled(goal) else BASELINE_PREVIOUS


def strategy_refinement_enabled(goal):
    # Saved goals without this flag retain their original decision behavior.
    return (getattr(goal, 'scenario', None) == 'larc'
        and getattr(goal, 'larc_policy', {}).get('strategy_refinement', policy_branch(goal) == UNIFIED_VARIANT) is True)


def training_balance_v2_enabled(goal):
    """Saved R8 goals without an explicit flag retain their training scores."""
    return (getattr(goal, 'scenario', None) == 'larc'
        and getattr(goal, 'larc_policy', {}).get('training_balance_v2', policy_branch(goal) == UNIFIED_VARIANT) is True)


def opening_charge_enabled(goal):
    default=not (enabled(goal) and not overseas_purchase_v2_enabled(goal))
    return getattr(goal,'larc_policy',{}).get('opening_charge_window',default)

def validate_policy(value):
    if not isinstance(value, dict):raise ValueError("larc_policy_must_be_object")
    if value.get("variant", "alpha") not in VARIANT_BRANCHES:raise ValueError("larc_policy_variant_unknown")
    validate_policy_revision(value)
    if value.get('variant') == UNIFIED_VARIANT:
        for key, required in UNIFIED_FEATURES.items():
            if key in value and value[key] is not required:
                raise ValueError('larc_unified_explicit_upgrade_required:' + key)
    if value.get("purpose", "custom") not in {"custom","career_validation","competitive"}:raise ValueError("larc_policy_purpose_unknown")
    allowed={"variant","revision","purpose","attribute_weights","after_target_credit","skill_future_credit","adaptation_early_credit","ss_planning_horizon","overseas_purchase_v2","overseas_purchase_v3","opening_charge_window","opening_charge_prior_units","strategy_refinement","training_balance_v2","decision_preferences","strategy_v4","tail263_scenario","year1_cycle_opportunity_enabled","strategy22_params","stamina_contest","distance_m","race_distance_m"}
    if set(value)-allowed:raise ValueError("larc_policy_unknown_fields")
    if 'strategy22_params' in value and not isinstance(value['strategy22_params'], dict):
        raise ValueError('larc_policy_strategy22_params_must_be_object')
    if 'stamina_contest' in value and type(value['stamina_contest']) is not bool:
        raise ValueError('larc_policy_stamina_contest_must_be_bool')
    for dist_key in ('distance_m', 'race_distance_m'):
        if dist_key in value and (type(value[dist_key]) not in {int, float} or not math.isfinite(value[dist_key]) or value[dist_key] <= 0):
            raise ValueError('larc_policy_distance_m_invalid')
    if 'year1_cycle_opportunity_enabled' in value and type(value['year1_cycle_opportunity_enabled']) is not bool:
        raise ValueError('larc_policy_year1_cycle_opportunity_enabled_must_be_bool')
    if 'decision_preferences' in value:
        from core.auto_trainer.larc_user_preferences import normalize
        normalize(value['decision_preferences'])
    if 'overseas_purchase_v2' in value and type(value['overseas_purchase_v2']) is not bool:
        raise ValueError('larc_policy_overseas_purchase_v2_must_be_bool')
    if 'overseas_purchase_v3' in value and type(value['overseas_purchase_v3']) is not bool:
        raise ValueError('larc_policy_overseas_purchase_v3_must_be_bool')
    if 'strategy_v4' in value and type(value['strategy_v4']) is not bool:
        raise ValueError('larc_policy_strategy_v4_must_be_bool')
    if 'opening_charge_window' in value and type(value['opening_charge_window']) is not bool:
        raise ValueError('larc_policy_opening_charge_window_must_be_bool')
    if 'strategy_refinement' in value and type(value['strategy_refinement']) is not bool:
        raise ValueError('larc_policy_strategy_refinement_must_be_bool')
    if 'training_balance_v2' in value and type(value['training_balance_v2']) is not bool:
        raise ValueError('larc_policy_training_balance_v2_must_be_bool')
    units=value.get('opening_charge_prior_units',3.)
    if type(units) not in {int,float} or not math.isfinite(units) or not 0<=units<=10:
        raise ValueError('larc_policy_opening_charge_prior_units')
    if value.get('ss_planning_horizon','full_career') not in {'full_career','junior_cutoff_discard'}:
        raise ValueError('larc_policy_ss_planning_horizon_unknown')
    weights=value.get("attribute_weights",{})
    if not isinstance(weights,dict) or set(weights)-{"speed","stamina","power","guts","wisdom"}:raise ValueError("larc_policy_attribute_weights")
    for amount in weights.values():
        if type(amount) not in {int,float} or not math.isfinite(amount) or not 0 < amount <= 10:raise ValueError("larc_policy_attribute_weight_range")
    for key in ("after_target_credit","skill_future_credit","adaptation_early_credit"):
        if key in value and (type(value[key]) not in {int,float} or not math.isfinite(value[key]) or not 0 < value[key] <= 1):raise ValueError("larc_policy_credit_range:"+key)

def attribute_options(goal):
    v4 = {"high_stat_scarcity": False} if strategy_v4_enabled(goal) else {}
    if not enabled(goal):return v4
    p=goal.larc_policy
    if unified_larc_enabled(goal):
        params = params_for_goal(goal)
        v4 = {**v4, "high_stat_scarcity": params.high_stat_scarcity, "restore_high_stat_value": params.restore_high_stat_value}
        rev = p.get("revision")
        if isinstance(rev, str) and (_revision_version_key(rev) >= (2, 3) or rev in AUTO_ROUTED_REVISIONS):
            weights = {**params.attribute_weights_dict(), **p.get("attribute_weights", {})}
            return {"attribute_weights": weights, "after_target_credit": p.get("after_target_credit", params.default_after_target_credit), **v4}
        return {"attribute_weights": p.get("attribute_weights", {}), "after_target_credit": p.get("after_target_credit", params.default_after_target_credit), **v4}
    return {"attribute_weights":p.get("attribute_weights",{}),"after_target_credit":p.get("after_target_credit",.5), **v4}

def _bound(packet,state):
    source=packet.get("source",{}) if isinstance(packet,dict) else {}
    runs={o.raw_data.get("capture",{}).get("run_session_id") for o in state.training_options}
    return (packet.get("status")=="accepted" and len(runs)==1 and None not in runs
        and source.get("run_session_id") in runs and type(source.get("observed_turn")) is int
        and type(source.get("valid_until_turn")) is int and type(state.turn) is int
        and source["observed_turn"]<=state.turn<=source["valid_until_turn"] and bool(source.get("path")))

def skill_budget(state):
    packet=state.scenario_state.get("larc_skill_opportunities",{})
    if not isinstance(packet,dict) or not _bound(packet,state) or packet.get("schema_version")!="larc_skill_opportunities.v1" or packet.get("complete") is not True:
        return {"status":"unknown","reason":"missing_incomplete_stale_or_cross_run_skill_opportunities"}
    rows=packet.get("opportunities")
    if not isinstance(rows,list):return {"status":"unknown","reason":"malformed_skill_opportunities"}
    learned=set(packet.get("learned_skill_ids",[]));groups={};seen={}
    learned_groups={}
    for row in rows:
        if isinstance(row,dict) and (row.get("skill_id") in learned or row.get("learned") is True):
            learned.add(row.get("skill_id"))
            learned_groups.setdefault(row.get("exclusive_group"),set()).add(row.get("skill_id"))
    for row in rows:
        if not isinstance(row,dict):return {"status":"unknown","reason":"malformed_skill_opportunity"}
        sid,group,cost=row.get("skill_id"),row.get("exclusive_group"),row.get("cost")
        utility=row.get("utility",1.)
        if not sid or not isinstance(group,str) or not group or type(cost) is not int or cost<=0 or type(utility) not in {int,float} or not math.isfinite(utility) or utility<=0:
            return {"status":"unknown","reason":"skill_cost_relation_or_utility_unknown"}
        if sid in seen and seen[sid]!=row:return {"status":"unknown","reason":"conflicting_skill_duplicates"}
        seen[sid]=row
        if sid in learned or row.get("learned") is True:continue
        relations={name:row.get(name,[]) for name in ("lower_skill_ids","upper_skill_ids","mutually_exclusive_skill_ids")}
        if any(not isinstance(values,list) or any(not isinstance(v,str) for v in values) for values in relations.values()):
            return {"status":"unknown","reason":"skill_relation_direction_unknown"}
        upper=set(relations["upper_skill_ids"]);lower=set(relations["lower_skill_ids"]);exclusive=set(relations["mutually_exclusive_skill_ids"])
        if upper&learned or exclusive&learned:continue
        if learned_groups.get(group,set())-(upper|lower|exclusive):
            return {"status":"unknown","reason":"learned_same_family_relation_unknown"}
        # Costs must already represent the payable incremental price. Never
        # add lower+upper, mutual alternatives, or already learned nodes twice.
        if row.get("incremental_cost_verified") is not True:return {"status":"unknown","reason":"skill_incremental_cost_unknown"}
        previous=groups.get(group)
        if previous is None or (utility,-cost,str(sid))>(previous["utility"],-previous["cost"],str(previous["skill_id"])):
            groups[group]={"skill_id":sid,"cost":cost,"utility":utility}
    return {"status":"accepted","useful_budget":sum(r["cost"] for r in groups.values()),
        "groups":list(groups.values()),"source":packet["source"],"basis":"one useful incremental opportunity per exclusive family; not a global optimizer"}

def skill_marginal(state,goal,amount):
    result={"factor":1.,"status":"baseline","known_budget":None}
    if not enabled(goal):return result
    if goal.larc_policy.get("skill_future_credit",1.) == 1.:
        return dict(result,status="disabled",reason="skill_saturation_discount_opt_in_only")
    budget=skill_budget(state)
    if budget["status"]!="accepted":return dict(result,status="unknown",reason=budget["reason"])
    owned=state.current_stats.get("skill_pt")
    if type(owned) is not int or owned<0:return dict(result,status="unknown",reason="skill_balance_unknown")
    future=goal.larc_policy.get("skill_future_credit",1.)
    shortage=max(0.,budget["useful_budget"]-owned)
    fraction=min(1.,shortage/amount) if amount>0 else float(shortage>0)
    return {"factor":future+(1-future)*fraction,"status":"accepted","known_budget":budget["useful_budget"],
        "owned_points":owned,"remaining_known_demand":shortage,"future_unseen_credit":future,
        "basis":budget["basis"],"source":budget["source"]}

def future_skill_factor(state,goal):
    # Only the current action and one observed held SS may claim known demand.
    # Unspecified repeated future layouts use the unseen-opportunity floor,
    # a conservative bound rather than repeatedly spending one finite budget.
    if enabled(goal) and goal.larc_policy.get("skill_future_credit",1.) < 1. and skill_budget(state)["status"]=="accepted":
        return goal.larc_policy.get("skill_future_credit",1.)
    return 1.


def import_d1_inventory(inventory,*,source,learned_skill_ids=()):
    """Read an existing D1 packet, without invoking its optimizer or executor.

    Strict abstentions/unsafe/unscored rows make complete demand unknown.
    The packet is validated as data, never reused as execution authority.
    Opportunity validity is bounded by observed_turn/valid_until_turn.
    """
    from core.skill_batch_purchase import validate_skill_inventory_packet, _candidate_is_locally_safe, _related_skill_ids
    valid,reason=validate_skill_inventory_packet(inventory)
    empty={"status":"unknown","reason":reason,"source":source}
    if not valid or inventory.get("abstentions"):return dict(empty,reason="d1_inventory_incomplete_or_invalid:"+reason)
    binding=inventory.get("capture_binding",{})
    expected=source.get("turn_id")
    if not expected or binding.get("turn_id")!=expected:return dict(empty,reason="d1_turn_binding_mismatch")
    rows=inventory.get("candidates",[])
    newly_learned=set(learned_skill_ids)-set(inventory.get("owned_skill_ids",[]))
    if any(_related_skill_ids(r)&newly_learned for r in rows if r.get("skill_id") not in newly_learned):
        return dict(empty,reason="post_purchase_upgrade_cost_unknown")
    if any(not _candidate_is_locally_safe(r) or r.get("cost_basis")!="observed_final" for r in rows):return dict(empty,reason="d1_unvalued_or_unsafe_candidates")
    ids={r["skill_id"] for r in rows};edges={sid:{sid} for sid in ids}
    for row in rows:
        relations=row.get("relations",{})
        others=_related_skill_ids(row)|set(relations.get("exclusive",[]))|set(relations.get("mutually_exclusive",[]))
        for other in others&ids:edges[row["skill_id"]].add(other);edges[other].add(row["skill_id"])
    labels={}
    for sid in sorted(ids):
        if sid in labels:continue
        component=set();pending=[sid]
        while pending:
            node=pending.pop()
            if node in component:continue
            component.add(node);pending.extend(edges[node]-component)
        for node in component:labels[node]=min(component)
    return {"schema_version":"larc_skill_opportunities.v1","status":"accepted","complete":True,
        "source":source,"learned_skill_ids":sorted(set(inventory.get("owned_skill_ids",[]))|set(learned_skill_ids)),
        "opportunities":[{"skill_id":r["skill_id"],"exclusive_group":labels[r["skill_id"]],
            "cost":r["display_cost"],"utility":r["score"],"incremental_cost_verified":True,
            "lower_skill_ids":r.get("relations",{}).get("lower",[]),
            "upper_skill_ids":r.get("relations",{}).get("upper",[]),
            "mutually_exclusive_skill_ids":sorted(set(r.get("relations",{}).get("exclusive",[]))|set(r.get("relations",{}).get("mutually_exclusive",[])))} for r in rows],
        "basis":"existing D1 inventory packet costs, score and relations; read-only import"}

def load_skill_context(session_dir,run_id,turn):
    """Optional normalized cache or pointer to an existing D1 inventory."""
    root=Path(session_dir);cache=root/"larc_skill_opportunities.json";ref=root/"larc_skill_inventory_ref.json"
    try:
        if cache.is_file():packet=json.loads(cache.read_text(encoding="utf-8-sig"))
        elif ref.is_file():
            pointer=json.loads(ref.read_text(encoding="utf-8-sig"));source=pointer.get("source",{})
            if source.get("run_session_id")!=run_id or not (type(source.get("observed_turn")) is int and source["observed_turn"]<=turn<=source.get("valid_until_turn",-1)):return {}
            inventory_path=Path(pointer["inventory_path"]).resolve()
            if not inventory_path.is_relative_to(root.parent.resolve()):return {}
            worker_path=Path(pointer["worker_report_path"]).resolve()
            if not worker_path.is_relative_to(root.parent.resolve()):return {}
            worker=json.loads(worker_path.read_text(encoding="utf-8-sig"))
            if worker.get("run_session_id")!=run_id or worker.get("turn_index")!=source["observed_turn"] or worker.get("ok") is not True or worker.get("stage")!="complete":return {}
            packet=import_d1_inventory(json.loads(inventory_path.read_text(encoding="utf-8-sig")),source=dict(source,path=str(inventory_path)),
                learned_skill_ids=[r["skill_id"] for r in worker.get("selected_skills",[]) if r.get("skill_id")])
        else:return {}
        source=packet.get("source",{})
        if source.get("run_session_id")!=run_id or type(source.get("observed_turn")) is not int or type(source.get("valid_until_turn")) is not int or not source["observed_turn"]<=turn<=source["valid_until_turn"]:return {}
        return {"larc_skill_opportunities":packet}
    except (OSError,ValueError,KeyError,TypeError):return {}

def ss_adaptation_income(state,preview):
    from core.auto_trainer.larc_charge_value import _bound as bound
    source=preview.get("source") or preview.get("participants_observation",{}).get("source",{})
    run=source.get("run_session_id")
    if not run or not bound(source,state,run) or not all(o.raw_data.get("capture",{}).get("run_session_id")==run for o in state.training_options):return {"points":0,"status":"unknown"}
    observed=preview.get("adaptation_points_if_all_win")
    people=preview.get("participants_observation",{}).get("participants",[])
    count=preview.get("ready_count")
    if not (preview.get("status")=="accepted" and preview.get("stage")=="preview" and type(count) is int and len(people)==count and all(p.get("win_guaranteed") is True for p in people)):
        return {"points":0,"status":"unknown"}
    if type(observed) is int and observed>=0:return {"points":observed,"status":"observed_preview","source":source}
    if preview.get("kind")=="SS" and count in {4,5}:
        return {"points":count*10,"status":"rule_estimate","basis":"user CN ordinary SS teaching:10 per win; no extra reward or SSS assumption"}
    return {"points":0,"status":"unknown","reason":"SSS_or_special_income_not_observed"}

def adaptation_timing_value(state,goal,preview,continuity):
    result={"score":0.,"basis":"only earlier future training benefit; same income in timely held route; cash not mutated"}
    if not enabled(goal):return dict(result,status="baseline")
    from core.auto_trainer.larc_recovery import period_value,estimated_training_cost
    from core.auto_trainer.larc_recovery_routes import recovery_window
    from core.auto_trainer.larc_training_value import _future_numeric_values
    from core.auto_trainer.larc_expedition_forecast import _context,expedition_scenarios
    period=period_value(state)
    if period.get("year_index") in {2,3} and period.get("month") in {7,8,9}:return dict(result,status="unavailable_overseas")
    income=ss_adaptation_income(state,preview);context=_context(state);window=recovery_window(state)
    if income["points"]<=0 or not context.get("balance_current") or context.get("through_turn")!=state.turn or type(context.get("points")) is not int or not window:
        return dict(result,status="unknown_or_no_window",income=income)
    reference=_future_numeric_values(state,goal);cost,_=estimated_training_cost(state)
    before=_income_forecast(state,goal,reference,window,cost,context)
    future=deepcopy(state);future.scenario_state["larc_expedition_context"]=dict(context,points=context["points"]+income["points"])
    after=_income_forecast(future,goal,reference,window,cost,future.scenario_state["larc_expedition_context"])
    if not before.get("active") or not after.get("active"):return dict(result,status="no_purchase_forecast",income=income)
    # Immediate SS can fund upgrades only AFTER this action. A held SS has
    # the identical eventual income; credit only trainings before its cash
    # could arrive, never the entire upgrade value twice.
    held=continuity.get("future_route_before",[]);route=continuity.get("future_route_after",[])
    held_at=next((i+1 for i,a in enumerate(held) if a.startswith("held_ss")),window["steps"])
    changes=[]
    for i,(old,new) in enumerate(zip(before["future_numeric_by_period"],after["future_numeric_by_period"])):
        value=max(0.,new["value"]-old["value"])
        eligible=i<held_at and i<len(route) and route[i] in {"train","wisdom"}
        changes.append({"period":old["period"],"earlier_training":eligible,"forecast_delta":round(value,4)})
    credit=goal.larc_policy.get("adaptation_early_credit",.5)
    score=sum(x["forecast_delta"]*.95**i for i,x in enumerate(changes) if x["earlier_training"])*credit
    return dict(result,score=round(score,4),status="conditional_forecast",income=income,
        balance_before=context["points"],balance_after_ss_estimate=context["points"]+income["points"],
        held_income_delay_actions=held_at,credit=credit,changes=changes)


def _income_forecast(state,goal,reference,window,cost,context):
    from core.auto_trainer.larc_expedition_forecast import expedition_scenarios,LEVEL_TWO_COSTS
    from core.auto_trainer.larc_training_forecast import training_scenarios
    from core.auto_trainer.larc_recovery import period_value
    original=expedition_scenarios(state,goal,reference,window,cost)
    cases=original.get("scenarios",[]) if original.get("active") else [{"name":"committed","levels":context["levels"],"weight":1.,"cost":0}]
    # Existing shared effects: expedition_skill_pt(slot5,200) adds10Pt;
    # willpower_three(slot7,300) improves eligible friendship training.
    # Alternatives each spend ONE balance, never simultaneous free upgrades.
    levels=context["levels"]
    reserve=sum(c for level,c in zip(levels,LEVEL_TWO_COSTS) if level==1)
    alternatives=[{"name":c["name"],"levels":c["levels"],"cost":c["cost"]} for c in cases if c["name"]!="committed"]
    if period_value(state).get("year_index") in {2,3}:
        for name,slot,price in (("expedition_skill_pt",5,200),("willpower_three",7,300)):
            if levels[slot]==2 and context["points"]-reserve>=price:
                target=list(levels);target[slot]=3
                if not any(c["levels"]==target for c in alternatives):
                    alternatives.append({"name":"conditional:"+name,"levels":target,"cost":price})
    committed={"name":"committed","levels":levels,"cost":0,"weight":1.}
    options=[committed]+[dict(c,weight=1.) for c in alternatives]
    forecasts=[training_scenarios(state,goal,reference,window,cost,cases=[c]) for c in options]
    def total(report):
        return sum(r["value"]*.95**i for i,r in enumerate(report.get("future_numeric_by_period",[])))
    index=max(range(len(options)),key=lambda i:(total(forecasts[i]),-options[i]["cost"]))
    # Purchases are chosen, not randomly drawn. Pick ONE affordable fixed
    # plan for this whole finite window; retain no-purchase as an alternative.
    # Half credit discounts uncertain forecast effects rather than averaging
    # unrelated purchase plans or granting a different free upgrade each day.
    if index==0:report=forecasts[0]
    else:report=training_scenarios(state,goal,reference,window,cost,cases=[dict(committed,weight=.5),dict(options[index],weight=.5)])
    report["selected_purchase_plan"]={"name":options[index]["name"],"cost":options[index]["cost"],"levels":options[index]["levels"]}
    report["affordable_plan_count"]=len(options)
    return report



def cache_worker_skill_opportunities(run_root,worker,inventory_path=None):
    """Natural, non-executing hook after an already authorized D1 batch.

    Four-turn opportunity TTL is a conservative planning prior. Every later
    completed purchase overwrites/invalidate this cache, even when unknown.
    """
    root=Path(run_root).resolve();run=root.name
    if worker.get("run_session_id")!=run or worker.get("ok") is not True or worker.get("stage")!="complete":return {"status":"unknown","reason":"worker_not_completed"}
    turn=worker.get("turn_index")
    if type(turn) is not int:return {"status":"unknown","reason":"worker_turn_unknown"}
    prepared=worker.get("preference_preparation") or worker.get("preparation") or worker.get("learning_result",{}).get("preparation",{})
    path=Path(inventory_path or prepared.get("inventory_path") or Path(worker.get("output_dir",""))/"transaction/prepare/skill_inventory.packet.json").resolve()
    source={"run_session_id":run,"observed_turn":turn,"valid_until_turn":turn+4,"path":str(path)}
    packet={"status":"unknown","source":source,"reason":"D1_complete_inventory_unavailable"}
    try:
        if not path.is_relative_to(root):raise ValueError("inventory_outside_run")
        inventory=json.loads(path.read_text(encoding="utf-8-sig"));source["turn_id"]=inventory.get("capture_binding",{}).get("turn_id")
        if not source["turn_id"] or not source["turn_id"].startswith(run+"_turn"+str(turn)+"_"):raise ValueError("inventory_turn_binding_mismatch")
        learned=[row["skill_id"] for row in worker.get("selected_skills",[]) if isinstance(row,dict) and row.get("skill_id")]
        before=worker.get("before",{}).get("current_skill_points");after=worker.get("after",{}).get("current_skill_points",worker.get("remaining_skill_points"))
        if type(before) is int and type(after) is int and after<before and not learned:raise ValueError("post_purchase_skill_ids_unknown")
        packet=import_d1_inventory(inventory,source=source,learned_skill_ids=learned)
    except (OSError,ValueError,KeyError,TypeError) as exc:packet["reason"]=str(exc)
    session=root/"session";session.mkdir(exist_ok=True)
    destination=session/"larc_skill_opportunities.json";temporary=destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding="utf-8");temporary.replace(destination)
    return {"status":packet["status"],"reason":packet.get("reason"),"path":str(destination)}
