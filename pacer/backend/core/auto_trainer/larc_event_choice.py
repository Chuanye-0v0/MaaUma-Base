"""Local event preferences using U3 references and existing U2 value functions.

Reference effects guide a choice; they never become observed run rewards.
"""
from copy import deepcopy
from dataclasses import replace
import math

import cv2

from core.event_knowledge_lookup import (
    load_reviewed_events, normalize_text, personal_candidate_reference, _title_match,
)
from core.event_text_match import owner_matches, visible_event_options
from core.auto_trainer.event_options import detect_event_option_layout
from core.auto_trainer.larc_training_value import gain_value
from core.auto_trainer.larc_recovery import recovery_value
from core.auto_trainer.larc_phase import phase, PHASE_WEIGHTS
from core.auto_trainer.larc_mood import MOODS, mood_recovery_value
from core.auto_trainer.state import TRAINING_TYPES, TrainingTurnState
from core.main_menu_state import _analyze_vitality, _classify_local_template

STAT_MAP = {"speed": "speed", "stamina": "stamina", "power": "power",
            "guts": "guts", "wit": "wisdom", "skill_points": "skill_pt"}


def _compact(text):
    return "".join(normalize_text(text).split())


def match_visible_event(text, count, *, trainee_id=None, context=None):
    """Shared title evidence plus complete ordered options; no extra OCR."""
    from core.event_titles import title_key
    from core.event_knowledge_lookup import _titles
    from core.auto_trainer.larc_event_catalog import event_catalog,_scope_matches
    from core.auto_trainer.larc_event_matching import title_resolution
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = [];eligible=[]
    for event in load_reviewed_events():
        if (event["event_kind"] not in {"choice", "outgoing"}
                or len(event["options"]) != count or not owner_matches(event, trainee_id)):
            continue
        row=event_catalog().get('reviewed:'+event['event_id'])
        if context is not None and row and not _scope_matches(row,context):continue
        eligible.append(event)
        keys={title_key(title) for title in _titles(event)}
        titles=[i for i,line in enumerate(lines) if title_key(line) in keys]
        if len(titles)!=1:continue
        visible = visible_event_options(lines, event, title_index=titles[0])
        if visible:
            title = lines[titles[0]]
            matches.append((event, tuple(_compact(v) for v in [title, *visible])))
    if matches:return matches[0] if len(matches) == 1 else None
    resolved=title_resolution(text,count,context or {'trainee_id':trainee_id})
    if resolved['status']!='matched':return None
    for hit in resolved['matches']:
        if hit['row']['source']!='reviewed' or hit['title_match']['mode']!='one_edit':continue
        event=next((event for event in eligible if 'reviewed:'+event['event_id']==hit['row']['key']),None)
        if event is None:continue
        visible=visible_event_options(lines,event,title_index=hit['line_index'])
        if visible:
            matches.append((event,tuple(_compact(value) for value in [hit['title_match']['observed_title'],*visible])))
    return matches[0] if len(matches)==1 else None


def _with_title_evidence(selection,resolved):
    from core.auto_trainer.larc_event_matching import title_diagnostic
    selection['title_resolution']=title_diagnostic(resolved)
    identity=selection.get('event_id')
    hit=next((hit for hit in resolved.get('matches',[]) if hit['row']['key'] in {identity,'reviewed:'+str(identity)}),None)
    if hit is not None:selection['title_match']=hit['title_match']
    return selection


def _amount(effect, bonuses):
    value = effect.get("value")
    base = effect.get("base_value")
    kind = effect["kind"]
    # Capacity is a fixed game-point increase, not a support-event stat bonus.
    bonus = None if kind in {"max_energy", "max_vitality"} else bonuses.get("recovery" if kind == "energy" else "event")
    if type(base) in (int, float) and type(bonus) in (int, float):
        return math.floor(base * (1 + bonus / 100)), "profile_scaled_reference"
    if type(value) in (int, float):
        return value, "reference"
    if isinstance(value, dict) and all(type(value.get(k)) in (int, float) for k in ("min", "max")):
        return (value["min"] + value["max"]) / 2, "reference_range_midpoint"
    return None, "unknown"


def _event_energy(effects, state, context, bonuses, *, conservative=False):
    """Cap net reference recovery in game points; capacity itself is not a heal."""
    maximum = context.get("max_vitality")
    known_maximum = type(maximum) in (int, float) and math.isfinite(maximum) and 100 <= maximum <= 120
    maximum = maximum if known_maximum else 110.
    points = increase = 0.
    known = True
    for effect in effects:
        if effect["kind"] not in {"energy", "max_energy", "max_vitality"}:
            continue
        amount, basis = _amount(effect, bonuses)
        if conservative and basis == "reference_range_midpoint":
            amount = effect["value"]["min"]
        if amount is None or not math.isfinite(amount):
            known = False
        elif effect["kind"] == "energy":
            points += amount
        else:
            increase += amount
    vitality = state.vitality
    body_known = type(vitality) in (int, float) and math.isfinite(vitality) and 0 <= vitality <= 100
    current = maximum * vitality / 100 if body_known else None
    after_maximum = min(120., max(100., maximum + increase))
    usable = min(after_maximum, max(0., current + points)) - current if body_known and known else None
    return {"energy_reference_points": points if known else None, "energy_reference_known": known,
            "usable_energy_points": usable,
            "overflow_energy_points": max(0., points - usable) if usable is not None and points > 0 else None,
            "energy_deficit_points": maximum - current if body_known else None,
            "maximum_energy_points": maximum, "maximum_after_reference_points": after_maximum,
            "max_energy_increase_points": after_maximum - maximum,
            "capacity_basis": "profile" if known_maximum else "100_120_midpoint_estimate",
            "energy_units": "rewards_and_usable_recovery_game_points; vitality_bar_percent"}


def _recovery_safe(effects, unknown_rewards=()):
    """Unknown positive bond/hint value is disclosed; hazards cannot be ignored."""
    if unknown_rewards:
        return False
    for effect in effects:
        kind = effect["kind"]
        value = effect.get("value", effect.get("base_value"))
        if kind in {*STAT_MAP, "energy", "max_energy", "max_vitality", "mood", "bond", "skill_hint"}:
            if isinstance(value, dict):
                value = value.get("min")
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return False
        elif kind not in {"outing_unlock", "chain_end"}:
            return False
    return True


def _guaranteed_recovery_choice(rows, *, goal=None):
    """Keep the legacy heal override only when unified L'Arc scoring is off."""
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if unified_larc_enabled(goal):
        return None
    eligible = [row for row in rows if row.get("recovery_safe") is True
                and (row.get("energy_deficit_points") or 0) > 0
                and (row.get("guaranteed_usable_energy_points") or 0) > 0]
    return max(eligible, key=lambda row: (row["guaranteed_usable_energy_points"],
                row.get("priority", 0), row["score"], -row["index"])) if eligible else None


def _positive_unknown_dominance(rows, goal):
    """Prove only a same-owner positive bond difference cannot reverse a win.

    No unknown reward receives a numeric price. Both reference amount forms
    must preserve the ordering, since a known profile may scale base_value.
    Other unknown rewards, owners and hazards retain the original fallback.
    """
    from core.auto_trainer.larc_strategy_params import unified_larc_enabled
    if not unified_larc_enabled(goal) or len(rows) < 2:
        return None
    best = max(rows, key=lambda row: (row.get('priority', 0), row['score'], -row['index']))
    owners, amounts = set(), {}
    allowed = {'kind', 'op', 'value', 'base_value', 'source_code', 'owner_character_id', 'owner_id'}
    for row in rows:
        effects = row.get('unknown_effects', [])
        # unknown_effects is concatenated across conditional branches. Two
        # entries are not evidence that both rewards occur on the same path.
        if len(effects) != 1 or row['score'] > best['score']:
            return None
        totals = {'value': 0., 'base_value': 0.}
        for effect in effects:
            owner = effect.get('owner_id')
            value = effect.get('value', effect.get('base_value'))
            base = effect.get('base_value', value)
            if (effect.get('kind') != 'bond' or effect.get('op') not in {None, 'add'}
                    or not owner or set(effect) - allowed
                    or any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in (value, base))):
                return None
            owners.add((str(owner), str(effect.get('owner_character_id') or '')))
            totals['value'] += value
            totals['base_value'] += base
        amounts[row['index']] = totals
    if len(owners) != 1 or any(amounts[best['index']][key] < values[key]
            for values in amounts.values() for key in ('value', 'base_value')):
        return None
    return {'basis': 'positive_unknown_dominance', 'winner_index': best['index'],
            'unknown_effect_kind': 'bond', 'owner_id': next(iter(owners))[0],
            'reference_amounts_by_option': amounts, 'numeric_value_assigned': False,
            'meaning': 'higher_known_score_has_no_less_positive_same_owner_bond; unknown_value_not_zeroed'}


def _branch_recovery(scenarios):
    amounts = [row["guaranteed_usable_energy_points"] for row in scenarios]
    return {"guaranteed_usable_energy_points": min(amounts) if all(v is not None for v in amounts) else None,
            "recovery_safe": all(row["recovery_safe"] for row in scenarios)}


def _event_recovery_score(state, goal, usable_points, max_points, *, conserve_energy=True):
    """Value zero-turn event energy changes using the baseline recovery model and optional strategy extension."""
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled

    delta_pct = usable_points * 100.0 / max_points
    base = recovery_value(state, delta_pct, include_held_ss=False)
    if usable_points <= 0 or not unified_larc_enabled(goal):
        return base
    params = params_for_goal(goal)
    return active_provider().event_recovery_score(
        params,
        state,
        goal,
        usable_points,
        max_points,
        base,
        conserve_energy,
    )


def _effects_value(effects, state, goal, context, mood, bonuses):
    gains = {key: 0 for key in STAT_MAP.values()}
    unknown, components = [], {}
    unpriced = []
    energy = _event_energy(effects, state, context, bonuses)
    guaranteed = _event_energy(effects, state, context, bonuses, conservative=True)
    if energy["usable_energy_points"] is not None:
        prefs = context.get("event_preferences") or getattr(goal, "event_preferences", None) or {}
        conserve = prefs.get("conserve_energy", True)
        components["energy"] = _event_recovery_score(
            state, goal, energy["usable_energy_points"], energy["maximum_energy_points"],
            conserve_energy=True if conserve is None else bool(conserve),
        )
    priority = 0
    for effect in effects:
        kind = effect["kind"]
        value, basis = _amount(effect, bonuses)
        if kind in STAT_MAP and value is not None:
            gains[STAT_MAP[kind]] += value
        elif kind == "energy" and value is not None and energy["usable_energy_points"] is not None:
            pass  # Net energy was capped and valued once above.
        elif kind in {"max_energy", "max_vitality"} and value is not None:
            unpriced.append({**effect, "value_basis": "known_capacity_effect; future_capacity_value_unpriced"})
        elif kind == "mood" and value is not None and mood is not None:
            components["mood"] = components.get("mood", 0) + mood_recovery_value(mood, value)["score"]
        elif kind == "bond" and value is not None:
            bond = context.get("bonds", {}).get(str(effect.get("owner_id")))
            if type(bond) in (int, float):
                usable = max(0, min(80, bond + value)) - min(80, bond)
                components["bond"] = components.get("bond", 0) + usable / 7 * PHASE_WEIGHTS[phase(state)]["effective_bond"]
            else:
                unknown.append(effect)
        elif kind == "outing_unlock":
            if context.get("outing_unlocked") is not True:
                priority += 1
        elif kind in {"outing_unavailable", "chain_end"}:
            priority -= 1
        elif kind == "skill_hint":
            names = [effect.get(k) for k in ("skill_id", "name_zh_cn", "name_ja")]
            preference = next((context.get("skill_hint_values", {}).get(name) for name in names
                               if name in context.get("skill_hint_values", {})), None)
            if type(preference) in (int, float):
                components["target_skill_hint"] = components.get("target_skill_hint", 0) + preference
            else:
                unknown.append(effect)
        else:
            unknown.append(effect)
    # Reuse U2's numeric needs; gains are reference estimates, not receipts.
    positive = {k: round(max(0, v)) for k, v in gains.items()}
    negative = {k: round(max(0, -v)) for k, v in gains.items()}
    # This editor tunes action comparisons. Event choices keep their separate
    # explicit preferences and chain policy, without partially scaling rewards.
    if 'decision_preferences' in (goal.larc_policy or {}):
        from core.auto_trainer.config import replace_goal
        goal = replace_goal(goal, larc_policy={k: v for k, v in goal.larc_policy.items() if k != 'decision_preferences'})
    from core.auto_trainer.larc_strategy_params import params_for_goal, unified_larc_enabled
    negative_state = state
    if unified_larc_enabled(goal) and params_for_goal(goal).restore_high_stat_value and any(negative.get(key, 0) for key in TRAINING_TYPES):
        # Price a loss over its actual segment, from the reduced reference
        # state back to the current state. This is score-only; the observed
        # state and execution rewards remain untouched.
        negative = {
            key: min(value, max(0, state.current_stats[key]))
                 if key in TRAINING_TYPES and key in state.current_stats else value
            for key, value in negative.items()}
        negative_state = replace(state, current_stats={
            key: max(0, value - negative[key])
                 if key in TRAINING_TYPES and negative.get(key, 0) else value
            for key, value in state.current_stats.items()})
    components["numeric"] = (gain_value(state, positive, goal, project=False)["score"]
                             - gain_value(negative_state, negative, goal, project=False)["score"])
    return {"score": round(sum(components.values()), 4), "priority": priority,
            "components": components, "unknown_effects": unknown, "unpriced_effects": unpriced,
            **energy, "guaranteed_usable_energy_points": guaranteed["usable_energy_points"],
            "recovery_safe": _recovery_safe(effects)}


def _tutorial_selection(image, text, count):
    """System navigation precedes reward-library matching; no extra OCR."""
    from core.auto_trainer.safe_advance import SafeAdvancePlanner

    lines = {line.strip() for line in text.splitlines()}
    if not {"新手教程", "凯旋门计划"}.issubset(lines):
        return None
    options, index = None, None
    if count == 2 and "不听取说明" in lines:
        if SafeAdvancePlanner._grand_masters_tutorial_offer_evidence(image)["candidate"]:
            options, index = ("听取说明", "不听取说明"), 2
    elif count == 5:
        topics = ("了解养成目标", "了解群星赛", "了解海外适应性", "了解海外远征", "不需要了")
        if "不需要了" in lines and SafeAdvancePlanner._grand_masters_tutorial_topic_menu_evidence(image).get("present"):
            options, index = topics, 5
    elif count == 2 and {"是的", "不是"}.issubset(lines):
        options, index = ("是的", "不是"), 1
    if options is None:
        return None
    return {"status": "selected", "reason": "larc_tutorial_navigation", "choice_index": index,
            "expected_event": ("凯旋门计划", *options), "effect_basis": "system_navigation",
            "candidates": []}


def select_event_option(image, text, context, goal):
    selection=_select_event_option(image,text,context,goal)
    if selection.get('reason')=='user_event_choice':
        source,label='user_override','用户指定选项'
    elif selection.get('event_family')=='additional_training':
        source,label='common_event_family','通用事件规则'
    elif selection.get('status')=='selected':
        source,label='event_rule','事件规则'
    else:
        source,label='default_fallback','默认回退'
    selection.update(selection_source=source,selection_source_label=label)
    return selection


def _select_event_option(image, text, context, goal):
    result = {"status": "fallback", "reason": "event_not_matched", "choice_index": 1,
              "expected_event": None, "effect_basis": "U3_reference_not_observed", "candidates": []}
    if image is None:
        result["reason"] = "image_unavailable"
        return result
    count = detect_event_option_layout(image,visible_text=text)["option_count"]
    from core.auto_trainer.larc_event_matching import catalog_title_match
    try:
        resolved=catalog_title_match(text,count,context)
    except (OSError,ValueError,KeyError,TypeError) as error:
        resolved={'status':'unavailable','matches':[],'error':f'{type(error).__name__}:{error}'}
    context={**context,'_event_title_resolution':(text,count,resolved)}
    from core.auto_trainer.larc_event_catalog import manual_event_choice
    preferences=context.get('event_preferences') or goal.event_preferences
    manual=manual_event_choice(text,count,context,preferences)
    if manual is not None:return manual
    from core.auto_trainer.cut_training_failure import training_failure_choice
    failure=training_failure_choice(text,count,context)
    if failure is not None:return failure
    tutorial = _tutorial_selection(image, text, count)
    if tutorial is not None:
        return tutorial
    from core.auto_trainer.larc_scenario_events import scenario_selection,acupuncture_title
    if count==5 and acupuncture_title([_compact(line) for line in text.splitlines()]):
        from core.auto_trainer.larc_acupuncture_policy import configured_policy,observe_date
        if configured_policy(goal.event_preferences)['mode']=='rules':
            state,_,basis=_current_event_state(image,text,context)
            context={**context,'acupuncture_observation':{**observe_date(image,text),
                'vitality':state.vitality,'vitality_basis':basis}}
    scenario = scenario_selection(text, count, context, goal)
    if scenario is not None:
        return scenario
    if context.get('event_library_enabled') is True:
        from core.auto_trainer.larc_meal_event import resolve_meal,choose_meal
        meal=resolve_meal(text,count,context)
        if meal is not None:
            rules=preferences.get('manual_choices') or {}
            if meal.get('family_only') and isinstance(rules,dict) and any(
                    type(rules.get(hit['row']['key'])) is int and 1<=rules[hit['row']['key']]<=count
                    for hit in meal['resolution']['matches']):
                return {**result,'reason':'meal_manual_scope_unverified'}
            state,_,basis=_current_event_state(image,text,context,matched_meal=True)
            return {**choose_meal(meal,state,context),'vitality_basis':basis}
    chain_context = context.get("state", {}).get("scenario_state", {}).get("larc_support_chain_context")
    if isinstance(chain_context, dict):
        try:
            from core.support_card_knowledge import default_library
            library = default_library()
            cards = [r["card_id"] for r in chain_context.get("deck", []) if r.get("identity_status") == "known"]
            found = {}
            for line in text.splitlines():
                reference = library.resolve_event({"title": line.strip(), "option_count": count}, deck_card_ids=cards)
                if reference["status"] == "matched":
                    event = reference["events"][0]
                    found[event["event_id"]] = event
            if len(found) == 1:
                event = next(iter(found.values()))
                reference = library.option_consequences(event, targets=chain_context.get("target_skill_ids", []),
                    learned=chain_context.get("learned_skill_ids", []))
                reference["run_session_id"] = chain_context["run_session_id"]
                reference["through_action_turn"] = chain_context.get("through_turn")
                reference["usable_for_action"] = False
                result["support_chain_reference"] = reference
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["support_chain_reference"] = {"status": "unavailable", "reason": str(exc)}
    matched = match_visible_event(text, count, trainee_id=context.get("trainee_id"),context=context)
    if matched is None:
        if context.get('event_library_enabled') is True:
            from core.auto_trainer.larc_event_library import (resolve_library_event,score_library_event,
                unsupported_character_reference)
            try:
                linked=resolve_library_event(text,count,context)
                if linked is not None:
                    state,mood,basis=_current_event_state(image,text,context)
                    selected=score_library_event(linked,state,goal,context,mood)
                    if selected is not None:
                        selected.update(vitality=state.vitality,vitality_basis=basis)
                        return _with_title_evidence(selected,resolved)
                else:
                    diagnostic=unsupported_character_reference(text,count,context)
                    if diagnostic is not None:
                        result.update(reason='event_effect_adapter_unavailable',catalog_reference=diagnostic)
            except (OSError,ValueError,KeyError,TypeError) as error:
                result['library_error']=f'{type(error).__name__}:{error}'
        try:
            reference = personal_candidate_reference(text, count)
        except (OSError, ValueError, KeyError, TypeError) as error:
            reference = {"status": "unavailable", "reason": type(error).__name__,
                         "candidates": [], "usable_for_action": False}
        if reference["status"] not in {"unknown", "unavailable"}:
            result["library_reference"] = reference
        if result['reason']=='event_not_matched' and resolved['status'] in {'ambiguous','scope_unverified','option_count_mismatch'}:
            result['reason']='event_title_'+resolved['status']
        return _with_title_evidence(result,resolved)
    event, expected = matched
    result.update(event_id=event["event_id"], expected_event=expected, event_policy_version="larc_events_v5")
    _with_title_evidence(result,resolved)
    state,mood,basis=_current_event_state(image,text,context)
    result['vitality_basis']=basis
    from core.auto_trainer.larc_mei_unlock_policy import select_mei_unlock
    mei=select_mei_unlock(event,expected,state,context,context.get('event_preferences') or goal.event_preferences)
    if mei is not None:
        mei['vitality_basis']=basis
        return _with_title_evidence(mei,resolved)
    bonuses = context.get("bonuses_by_owner", {}).get(str(event["owner"]["owner_id"]), {})
    for option in sorted(event["options"], key=lambda o: o["index"]):
        base = option["effects"]
        branches = option["conditional_branches"]
        if not base and not branches:
            result["reason"] = "incomplete_effects"
            return result
        scenarios = [_effects_value(base + branch["effects"], state, goal, context, mood, bonuses)
                     for branch in branches] if branches else [_effects_value(base, state, goal, context, mood, bonuses)]
        # Unknown probabilities are not silently made 50/50: compare the worst known branch.
        value = min(scenarios, key=lambda x: (x["priority"], x["score"]))
        row = {**value, "index": option["index"], "branch_basis": "worst_known_branch" if branches else "unconditional",
               "unknown_effects": [e for s in scenarios for e in s["unknown_effects"]],
               **_branch_recovery(scenarios)}
        result["candidates"].append(row)
    rows = result["candidates"]
    recovery = _guaranteed_recovery_choice(rows, goal=goal)
    if recovery is not None:
        result.update(status="selected", reason="usable_recovery_saves_future_turn",
                      choice_index=recovery["index"], vitality=state.vitality,
                      recovery_priority_basis="real_body_deficit; safe_recovery_in_every_reference_branch; manual_choices_first")
        return result
    if not state.current_stats:
        result["reason"] = "current_needs_unavailable"
        return result
    best_priority = max(row["priority"] for row in rows)
    eligible = [row for row in rows if row["priority"] == best_priority]
    # Common unknown rewards cancel; differing unknown effects cannot be valued as zero.
    unknown_comparison = None
    if len(eligible) > 1 and any(row["unknown_effects"] != eligible[0]["unknown_effects"] for row in eligible[1:]):
        unknown_comparison = _positive_unknown_dominance(eligible, goal)
        if unknown_comparison is None:
            result["reason"] = "unpriced_effect_difference"
            return result
    best = max(eligible, key=lambda row: (row["score"], -row["index"]))
    result.update(status="selected", reason="positive_unknown_dominance" if unknown_comparison else "current_needs_reference_score", choice_index=best["index"],
                  vitality=state.vitality, stats_basis=context.get("state_basis", "last_observed_menu"),
                  capacity_basis="profile" if context.get("max_vitality") else "100_120_midpoint_estimate")
    if unknown_comparison is not None:
        result['unknown_effect_comparison'] = unknown_comparison
    return result


def _current_event_state(image,text,context,*,matched_meal=False):
    raw=deepcopy(context.get('state',{}));raw['vitality']=None
    observed_vitality=None
    canonical=cv2.resize(image,(720,1280));basis='current_event_bar_unreadable'
    mood_field=_classify_local_template('mood',canonical)
    mood=MOODS.get(mood_field.get('value')) if not mood_field.get('abstained',True) else None
    has_vitality_label='体力' in text
    if has_vitality_label or mood is not None:
        energy=_analyze_vitality(canonical)
        if not energy['abstained']:
            observed_vitality=energy['value']['percent']
            basis=('current_event_bar' if has_vitality_label
                else 'current_event_bar_meal_mood_hud' if matched_meal
                else 'current_event_bar_mood_hud')
    state=TrainingTurnState.from_dict(raw)
    # Preserve the observed percent for strict user-configured event thresholds.
    # TrainingTurnState's general deserializer otherwise rounds it to an int.
    state=replace(state,vitality=observed_vitality)
    return state,mood,basis
