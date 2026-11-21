"""Compile an observed skill inventory for a Champions Meeting target.

Timing windows describe where a skill can start and whether its effect can
reach the entry to the last third. The presets use coarse course geometry
and a nine-runner CM field. Rankings are heuristics, independent of OCR or
purchase execution.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import product
import math


_COURSE_KEYS = ("track_id", "distance_m", "distance_type", "surface", "rotation")
_REPRESENTATIVE_DISTANCE = {"short": 1200, "mile": 1600, "middle": 2400, "long": 3000}
_STATIC = frozenset((*_COURSE_KEYS, "course_distance", "running_style", "ground_type",
                    "ground_condition", "season", "weather", "is_basis_distance", "always"))
_STYLE_RANKS = {"leader": (1, 2, 3), "front": (2, 3, 4, 5, 6),
                "betweener": (4, 5, 6, 7, 8), "chaser": (5, 6, 7, 8, 9)}
_STYLE_HP = {"leader": .95, "front": .89, "betweener": 1., "chaser": .995}
_STYLE_SPEED = {"leader": (1., .98, .962), "front": (.978, .991, .975),
                "betweener": (.938, .998, .994), "chaser": (.931, 1., 1.)}
_STAT_KEYS = ("speed", "stamina", "power", "guts", "wisdom")
_MAX_CANDIDATES = 48
CM_PROFILE_SUFFIX = "-arc-target-cm"
# The preference schema already carries a priority on soft rules. These
# markers bind a start package for the CM optimizer and leave hard priorities
# available for ordering genuinely required targets.
CM_GROUNDWORK_LINK = -400
CM_START_SOURCE_LINK = -410


def _preset(track_id, distance_m, distance_type, rotation, season, entry, offsets, surface="turf"):
    return dict(track_id=track_id, distance_m=distance_m, distance_type=distance_type,
                surface=surface, rotation=rotation, ground_condition=1, season=season,
                entry_windows=entry, entry_offsets_m=offsets)


# Approximate offsets from the last-third boundary, in order: third corner,
# final corner, second half of final corner, final straight.
CM_TRACK_PRESETS = {
    "longchamp_2400_turf": _preset(10201, 2400, "middle", 1, 3,
        ("final_corner_start",), (-210, -18, 106, 267)),
    "tokyo_2400_turf": _preset(10006, 2400, "middle", 2, 1,
        ("final_corner_start",), (-245, -32, 117, 274)),
    "tokyo_1600_turf": _preset(10006, 1600, "mile", 2, 1,
        ("final_corner_tail", "final_straight"), (-285, -153, -42, 8)),
    "nakayama_2000_turf": _preset(10005, 2000, "middle", 1, 1,
        ("backstretch", "third_corner"), (16, 173, 255, 357)),
    "nakayama_2500_turf": _preset(10005, 2500, "long", 1, 4,
        ("backstretch", "third_corner"), (48, 300, 420, 523)),
    "hanshin_2200_turf": _preset(10009, 2200, "middle", 1, 2,
        ("third_corner", "final_corner_start"), (-85, 45, 172, 377)),
    "kyoto_3000_turf": _preset(10008, 3000, "long", 1, 3,
        ("backstretch", "third_corner"), (33, 305, 440, 596)),
    "kyoto_3200_turf": _preset(10008, 3200, "long", 1, 1,
        ("backstretch", "third_corner"), (32, 411, 530, 663)),
    "chukyo_1200_turf": _preset(10007, 1200, "short", 2, 1,
        ("final_corner_tail", "final_straight"), (-330, -202, -105, -13)),
    "ooi_2000_dirt": _preset(10101, 2000, "middle", 1, 4,
        ("third_corner",), (-36, 100, 202, 281), surface="dirt"),
}
_GENERIC_OFFSETS = {"short": (-260, -155, -80, 8), "mile": (-280, -155, -38, 18),
                    "middle": (-220, -25, 110, 260), "long": (40, 230, 360, 510)}


def distance_type_for(distance_m):
    return "short" if distance_m < 1600 else "mile" if distance_m <= 1800 else "middle" if distance_m <= 2400 else "long"


def resolve_cm_target(final_target, preset_id=None):
    """Fill static facts from a preset and identify the geometry that still fits."""
    target = {key: value for key, value in final_target.items()
              if value not in (None, "", "any", "unknown")}
    distance = target.get("distance_m")
    if type(distance) is int and distance > 0:
        target.setdefault("distance_type", distance_type_for(distance))
    if preset_id in CM_TRACK_PRESETS:
        defaults = {key: value for key, value in CM_TRACK_PRESETS[preset_id].items()
                    if key not in {"entry_windows", "entry_offsets_m"}}
        if "distance_type" in target and target["distance_type"] != defaults["distance_type"]:
            defaults.pop("distance_m")
        target = {**defaults, **target}

    def matches(preset):
        surface = {"草地": "turf", "泥地": "dirt"}.get(target.get("surface"), target.get("surface"))
        return all(preset[key] == (surface if key == "surface" else target[key])
                   for key in _COURSE_KEYS if key in target)

    # A distance type by itself cannot identify a specific meet.
    options = [key for key, preset in CM_TRACK_PRESETS.items() if matches(preset)]
    if preset_id in options:
        matched = preset_id
    elif len(options) == 1 and ("distance_m" in target or "track_id" in target):
        matched = options[0]
    else:
        matched = None
    if matched:
        preset = CM_TRACK_PRESETS[matched]
        for key in (*_COURSE_KEYS, "ground_condition", "season"):
            target.setdefault(key, preset[key])
    return target, matched


def merge_current_stats(*snapshots):
    """Overlay known numeric panel values in order of freshness."""
    return {key: snapshot[key] for snapshot in snapshots if isinstance(snapshot, Mapping)
            for key in _STAT_KEYS if type(snapshot.get(key)) in {int, float}
            and 0 < snapshot[key] <= 3000}


def _scaled_stat(value):
    return min(value, 1200) + max(0, value - 1200) / 2


def stamina_recovery_gate(target, current_stats=None, *, owned_recovery_hp=0.):
    """Estimate full-spurt HP and the 1.04x conservation margin from Ver3.4."""
    stats = merge_current_stats(current_stats)
    distance = target.get("distance_m") or _REPRESENTATIVE_DISTANCE.get(target.get("distance_type"))
    style = target.get("style")
    stamina = stats.get("stamina")
    gate = {"status": "unknown", "distance_m": distance, "stamina": stamina,
            "guts": stats.get("guts"), "conservation_multiplier": 1.04,
            "stamina_duel_distance_eligible": distance is not None and distance >= 2101,
            "stamina_duel_panel_eligible": None, "initial_hp": None,
            "consumption_hp": None, "conservation_deficit_hp": None,
            "owned_recovery_hp": round(owned_recovery_hp, 1)}
    if stamina is not None:
        gate["stamina_duel_panel_eligible"] = gate["stamina_duel_distance_eligible"] and _scaled_stat(stamina) > 1200
    if stamina is None or distance is None:
        return gate
    base = max(1., 20 - (distance - 2000) / 1000)
    guts = _scaled_stat(stats.get("guts", 900))
    speed_bonus = math.sqrt(500 * _scaled_stat(stats.get("speed", 1100))) * .002
    early, middle, late = _STYLE_SPEED.get(style, _STYLE_SPEED["front"])
    spurt = (base * late + speed_bonus + .01 * base) * 1.05 + speed_bonus + (450 * guts)**.597 * .0001
    late_guts_cost = 1 + 200 / math.sqrt(600 * guts)
    phases = ((distance / 6, base * early, 1.), (distance / 2, base * middle, 1.),
              (max(0., distance / 3 - 60), spurt, late_guts_cost))
    ground = target.get("ground_condition")
    ground_factor = 1.01 if ground == 3 and target.get("surface") in {"dirt", "泥地"} else 1.02 if ground in {3, 4} else 1.
    spent = sum(metres / v * 20 * (v - base + 12)**2 / 144 * factor for metres, v, factor in phases) * ground_factor
    hp = distance + .8 * _scaled_stat(stamina) * _STYLE_HP.get(style, .89)
    gap = spent * 1.04 - hp - owned_recovery_hp
    status = "deficit" if gap > .015 * hp else "surplus" if gap <= -.035 * hp else "adequate"
    return {**gate, "status": status, "initial_hp": round(hp, 1), "consumption_hp": round(spent, 1),
            "conservation_deficit_hp": round(gap, 1)}


@dataclass(frozen=True)
class _Course:
    distance: int
    known_distance: bool
    preset_id: str | None
    sections: tuple

    @property
    def entry(self):
        return 2 * self.distance / 3

    @property
    def entry_window(self):
        # Useful acceleration after the boundary is roughly the first 2–3 s.
        return max(40., min(70., .03 * self.distance))


def _course(target, preset_id):
    distance_type = target.get("distance_type", "middle")
    known = "distance_m" in target or "distance_type" in target
    distance = target.get("distance_m") or _REPRESENTATIVE_DISTANCE[distance_type]
    offsets = (CM_TRACK_PRESETS[preset_id]["entry_offsets_m"] if preset_id
               else _GENERIC_OFFSETS[distance_type])
    entry = distance * 2 / 3
    third, final, tail, straight = [max(0., min(distance, entry + offset)) for offset in offsets]
    first, second, backstretch = (distance * ratio if distance_type in {"middle", "long"} else 0.
                                  for ratio in (.12, .22, .32))
    backstretch = min(backstretch, third)
    # On one-turn layouts the preceding straight is the backstretch.
    geometry = ((0., first, 0, 0, 0, 0), (first, second, 1, 0, 0, 0),
                (second, backstretch, 2, 0, 0, 0), (backstretch, third, 0, 0, 0, 2),
                (third, final, 3, 0, 0, 0), (final, tail, 4, 1, 0, 0),
                (tail, straight, 4, 1, 1, 0), (straight, distance, 0, 1, 1, 1))
    sections = tuple((lo, hi, {"corner": corner, "is_finalcorner": final_corner,
                    "is_finalcorner_laterhalf": later_half, "is_last_straight": int(front == 1),
                    "is_last_straight_onetime": int(front == 1), "straight_front_type": front})
                    for lo, hi, corner, final_corner, later_half, front in geometry if hi > lo)
    return _Course(distance, known, preset_id, sections)


def _alternatives(ast):
    """Return conjunctive paths, preserving independent OR alternatives."""
    if "variable" in ast:
        return [(ast,)]
    if "any" in ast:
        return [path for child in ast["any"] for path in _alternatives(child)]
    if "all" in ast:
        return [sum(paths, ()) for paths in product(*map(_alternatives, ast["all"]))]
    return [()] if ast.get("constant", True) else []


def _static_status(atoms, inputs, evaluate):
    return evaluate({"all": [term for term in atoms if term["variable"] in _STATIC]}, inputs)


def _rank_quality(atoms, style, evaluate):
    terms = [term for term in atoms if term["variable"] in {"order", "order_rate"}]
    if not terms:
        return 1.
    # Percentage boundaries are converted to rank boundaries in a CM field.
    terms = [dict(term, variable="order", value=max(1, int(term["value"] * 9 / 100 + .5)))
             if term["variable"] == "order_rate" else term for term in terms]
    ranks = _STYLE_RANKS.get(style, tuple(range(1, 10)))
    matched = sum(evaluate({"all": terms}, {"order": rank}) is True for rank in ranks)
    if matched == 0:
        return 0.
    return .45 + .55 * matched / len(ranks) if style in _STYLE_RANKS else .45 + .25 * matched / 9


_PHASE_EVENTS = {"phase": (0., 1.), "phase_random": (0., 1.),
                 "phase_firsthalf_random": (0., .5), "phase_laterhalf_random": (.5, 1.),
                 "phase_firstquarter_random": (0., .25), "phase_corner_random": (0., 1.)}
_GEOMETRY_EVENTS = {"corner", "is_finalcorner", "is_finalcorner_laterhalf", "is_last_straight",
                    "is_last_straight_onetime", "straight_front_type"}
_CURVE_RANDOM = {"all_corner_random", "corner_random", "is_finalcorner_random",
                 "phase_corner_random"}
_NON_TIMING = _STATIC | {"order", "order_rate", "is_lastspurt", "lastspurt",
                         "StartDash", "start_dash", "activate_count_start"}


def _position_window(variable, op, value, distance):
    if variable == "remain_distance":
        threshold = distance - value
        op = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "==": "==", "!=": "!="}[op]
    else:
        threshold = distance * value / 100
    # Keep strict comparisons on their own side of phase/segment boundaries.
    before, after = threshold - .001, threshold + .001
    return {"<": [(0., before)], "<=": [(0., threshold)], ">": [(after, distance)],
            ">=": [(threshold, distance)], "==": [(threshold, threshold)],
            "!=": [(0., before), (after, distance)]}[op]


def _trigger_windows(terms, course, evaluate):
    distance = course.distance
    phases = (0., distance / 6, course.entry, 5 * distance / 6, float(distance))
    windows = [(0., float(distance))]
    for term in terms:
        variable, op, value = term["variable"], term["operator"], term["value"]
        if variable in _PHASE_EVENTS:
            first, last = _PHASE_EVENTS[variable]
            allowed = [(lo + (hi - lo)*first, lo + (hi - lo)*last - .001)
                       for phase, (lo, hi) in enumerate(zip(phases, phases[1:]))
                       if evaluate(term, {variable: phase}) is True]
            if variable == "phase_corner_random":
                allowed = _intersect(allowed, [(a, b) for a, b, data in course.sections if data["corner"]])
        elif variable in {"is_lastspurt", "lastspurt"}:
            allowed = [(course.entry, distance)] if evaluate(term, {variable: 1}) else [(0., course.entry - .001)]
        elif variable in {"distance_rate", "remain_distance", "distance_rate_after_random"}:
            allowed = _position_window(variable, ">=" if variable.endswith("_random") else op, value, distance)
        elif variable in _GEOMETRY_EVENTS:
            allowed = [(lo, hi - .001) for lo, hi, data in course.sections if evaluate(term, data)]
        elif variable in {"corner_random", "all_corner_random", "is_finalcorner_random", "straight_random"}:
            allowed = [(lo, hi - .001) for lo, hi, data in course.sections
                       if (data["corner"] == value if variable == "corner_random"
                           else data["corner"] == 4 if variable == "is_finalcorner_random"
                           else bool(data["corner"]) == (variable == "all_corner_random"))]
        elif variable in {"accumulatetime", "time"} and op in {">", ">=", "<", "<="}:
            allowed = _position_window("distance_rate", op, value * 18 * 100 / distance, distance)
        elif variable in {"StartDash", "start_dash"} and op == "==" and value == 1:
            allowed = [(0., 6.)]
        else:
            continue
        windows = _intersect(windows, allowed)
        if not windows:
            break
    return windows


def _intersect(left, right):
    return sorted((max(lo, a), min(hi, b)) for lo, hi in left for a, b in right
                  if max(lo, a) <= min(hi, b))


@dataclass(frozen=True)
class _Rating:
    tier: int
    utility: int
    reason: str
    priority: int = 0
    start_source: bool = False
    start_requirement: int = 0
    recovery_rate: float = 0.


_NO_VALUE = _Rating(3, 0, "no_useful_self_effect")
_ACCEL = ("acceleration", "accel", "加速度")
_SPEED = ("target_speed", "speed", "instant_speed", "current_speed_with_natural_decel")
_PASSIVE = {"passive_speed": 1., "passive_power": 1.15, "passive_stamina": .8,
            "passive_guts": .6, "passive_wisdom": .6, "all_stats": 2.5}


def _self_effects(branch, master):
    effects = branch.get("effects", master.get("effects", []))
    return {kind: sum(e["value"] for e in effects if e.get("kind", e.get("type")) == kind
                      and e.get("target_type", 1) == 1 and type(e.get("value")) in {float, int})
            for kind in {e.get("kind", e.get("type")) for e in effects}}


def _preservation_reliability(windows, course, random, quality):
    """Estimate whether recovery is early enough to prevent conservation."""
    cutoff = course.distance * 10 / 24
    first = windows[0][0]
    if first >= course.entry:
        return 0.
    if not random:
        return quality if first < cutoff else .62 * quality
    # Broad random timing can heal after the first conservation check.
    length = sum(hi - lo for lo, hi in windows)
    early = sum(max(0., min(hi, cutoff) - lo) for lo, hi in windows)
    useful = sum(max(0., min(hi, course.entry) - lo) for lo, hi in windows)
    return quality * (.55 * early + .45 * useful) / max(1., length)


def _acceleration_rating(terms, windows, course, duration, accel, rank, reliability):
    if not windows or accel <= 0:
        return _NO_VALUE
    names = {term["variable"] for term in terms}
    required = max((int(term["value"]) + int(term["operator"] == ">") for term in terms
                    if term["variable"] == "activate_count_start" and term["operator"] in {">=", ">", "=="}), default=0)
    if required:
        return _Rating(3, 0, "start_acceleration_needs_sources", start_requirement=required)
    random = any("random" in name for name in names)
    first = windows[0][0]
    if first <= 8 and not random and reliability >= .7:
        return _Rating(0, round(161 + 115 * accel), "start_acceleration", 280, True)
    if not course.known_distance:
        return _Rating(2, 18, "unknown_acceleration_timing")
    entry = course.entry
    effect_metres = duration * course.distance / 1000 * 20
    if random:
        good = sum(max(0., min(hi, entry + course.entry_window) - max(lo, entry - effect_metres * .45))
                   for lo, hi in windows)
        width = max(1., sum(hi - lo for lo, hi in windows))
        quality = good / width * rank * reliability
        if quality >= .38:
            return _Rating(1, round(109 + 95 * accel * quality), "narrow_random_entry_acceleration")
        if quality >= .17:
            return _Rating(2, round(8 + 80 * accel * quality), "random_entry_overlap")
        return _Rating(3, 0, "random_acceleration_misses_entry")
    if first < entry - effect_metres * .70 or first > entry + course.entry_window:
        return _Rating(3, 0, "acceleration_outside_entry")
    overlap = min(1., (first + effect_metres - entry) / max(1., effect_metres))
    lateness = 1 - .60 * max(0., first - entry) / course.entry_window
    quality = min(overlap, lateness) * rank * reliability
    if quality >= .55 and accel >= .10 and (course.preset_id or not names & _GEOMETRY_EVENTS):
        return _Rating(0, round(155 + 175 * accel * quality), "last_third_entry_acceleration",
                       round(300 + 100 * accel * quality))
    if quality >= .26:
        return _Rating(1, round(107 + 142 * accel * quality), "conditional_entry_acceleration")
    return _Rating(2, round(12 + 60 * accel * quality), "weak_entry_acceleration")


def _rate_branch(master, branch, terms, preconditions, target, course, gate, evaluate):
    effects = _self_effects(branch, master)
    rank = _rank_quality(terms, target.get("style"), evaluate)
    if rank == 0.:
        return _Rating(3, 0, "order_window_mismatches_style")
    windows = _trigger_windows(terms, course, evaluate)
    if not windows:
        return _Rating(3, 0, "no_trigger_window_on_course")
    names = {term["variable"] for term in terms}
    dynamic = names - _NON_TIMING - _GEOMETRY_EVENTS - _PHASE_EVENTS.keys() - {
        "distance_rate", "distance_rate_after_random", "remain_distance", "accumulatetime", "time",
        "all_corner_random", "corner_random", "is_finalcorner_random", "straight_random", "temptation_count",
    }
    reliability = (.72 if dynamic else 1.) * (.85 if any(
        term["variable"] not in _STATIC for term in preconditions) else 1.)
    random = any("random" in name for name in names)
    duration = (branch.get("duration") or {}).get("base_seconds") or 0.
    first = windows[0][0]
    entry = course.entry
    accel = sum(effects.get(kind, 0.) for kind in _ACCEL)
    if effects.get("start_dash") or effects.get("StartDash"):
        return _Rating(0, 200, "start_dash", 280, True)
    ratings = [_acceleration_rating(terms, windows, course, duration, accel, rank, reliability)]

    passive = sum(max(0., effects.get(kind, 0.)) * weight for kind, weight in _PASSIVE.items())
    if passive:
        certain = names <= _STATIC and all(t["variable"] in _STATIC for t in preconditions)
        speed_or_power = effects.get("passive_speed", 0.) > 0 or effects.get("passive_power", 0.) > 0
        stamina_needed = effects.get("passive_stamina", 0.) > 0 and (
            gate["status"] in {"deficit", "adequate"} or gate["stamina_duel_distance_eligible"])
        core = certain and (speed_or_power or stamina_needed)
        ratings.append(_Rating(1 if core else 2, round(59 + passive if core else 6 + .37 * passive),
                               "matched_green" if certain else "conditional_green", start_source=certain))
    price_at_start = effects.get("start_delay_multiplier", 1.)
    if 0 < price_at_start < 1 or effects.get("start_delay", 0.) < 0:
        ratings.append(_Rating(1, 102 if price_at_start < .6 else 79, "gate_start",
                               start_source=names <= _STATIC))

    heal = effects.get("stamina_recovery", 0.)
    if heal > 0:
        early = _preservation_reliability(windows, course, random, rank * reliability)
        if early:
            urgency = gate["status"]
            if urgency == "deficit" and early >= .52:
                ratings.append(_Rating(0 if heal >= .04 else 1, round(113 + 1400 * heal * early),
                                       "early_recovery_for_stamina_gap", 380 if heal >= .04 else 0,
                                       recovery_rate=heal * early))
            elif urgency in {"deficit", "adequate", "unknown"} and course.distance >= 2000:
                ratings.append(_Rating(1 if heal >= .04 and early >= .5 else 2,
                    round(75 + 640 * heal * early) if heal >= .04 and early >= .5 else round(10 + 340 * heal * early),
                    "early_recovery_insurance", recovery_rate=heal * early))
            else:
                ratings.append(_Rating(2, round(3 + 260 * heal * early), "low_recovery_need", recovery_rate=heal * early))
        else:
            ratings.append(_Rating(3, 0, "recovery_after_last_third_entry"))

    speed = sum(max(0., effects.get(kind, 0.)) for kind in _SPEED)
    if speed > 0:
        middle = any(lo < entry and hi >= course.distance / 6 for lo, hi in windows)
        carry_effect = any(effects.get(kind, 0.) > 0 for kind in {"speed", "target_speed", "current_speed_with_natural_decel"})
        carry = (carry_effect and .60 * course.distance <= first < entry
                 and first + duration * course.distance / 1000 * 20 >= entry)
        double = ("all_corner_random" in names and (branch.get("cooldown_seconds") or 500) <= 30
                  and windows[-1][1] - first >= .60 * course.distance)
        product_value = speed * duration * rank * reliability
        core = (carry or middle and duration >= 2.4 or double) and product_value >= .22
        utility = (round(80 + 54 * product_value + 22 * carry + 21 * double) if core
                   else round(7 + min(54., 53 * product_value)))
        ratings.append(_Rating(1 if core else 2, utility,
            "speed_carry_into_last_third" if carry and core else "repeatable_corner_speed" if double and core
            else "midrace_speed" if core else "situational_speed",
            start_source=first <= 8 and names <= _STATIC | {"phase"} and not preconditions))
    # Keep a compound skill's useful speed even when its acceleration is late.
    return min(ratings, key=lambda item: (item.tier, -item.utility, -item.start_source, item.reason == _NO_VALUE.reason))


def _rating(master, target, course, gate, inputs, evaluate):
    ratings, unknown, viable = [], False, False
    for branch in master.get("activation_branches", []):
        conditions = _alternatives(branch.get("condition_ast", {}))
        preconditions = _alternatives(branch.get("precondition_ast", {}))
        for condition, precondition in product(conditions, preconditions):
            match = _static_status(condition + precondition, inputs, evaluate)
            if match is not True:
                unknown |= match is None
                continue
            viable = True
            ratings.append(_rate_branch(master, branch, condition, precondition, target, course, gate, evaluate))
    if not viable:
        reason = "unknown_target_requirement" if unknown else "final_target_incompatible"
        return _Rating(3, 0, reason), reason
    # A later application on another path does not erase a viable early path.
    useful = min(ratings, key=lambda item: (item.tier, -item.utility, -item.start_source))
    need = max(item.start_requirement for item in ratings)
    return replace(useful, start_requirement=need, start_source=any(r.start_source for r in ratings),
                   recovery_rate=max(r.recovery_rate for r in ratings)), None


def _rule(skill_id, mode, priority=0, weight=0):
    return {"selector": {"kind": "skill_id", "value": skill_id}, "mode": mode,
            "priority": priority, "weight": weight}


def _native_utility(row):
    base = row.get("base_utility")
    if type(base) not in {float, int} or not math.isfinite(base):
        base = {"acceleration": 100, "speed": 75, "recovery": 60, "green": 55}.get(row.get("type"), 20)
    if row.get("type") == "speed" and row.get("conditions", {}).get("style") not in (None, ["any"]):
        base += 2
    return base


def _resolve_master(knowledge, row):
    identity = row.get("knowledge", {}).get("db_id") or row.get("external_truth", {}).get("db_id") or row["skill_id"]
    try:
        return knowledge.get(identity)
    except ValueError:
        return None


def _is_learnable(row, master):
    return row.get("base_cost") != 0 and master is not None and master.get("acquisition", {}).get("kind") == "pt_purchase"


def _manual_allowlist(entries, rows, masters, knowledge):
    allowed, resolution = set(), []
    for entry in entries:
        try:
            identity = knowledge.get(entry)["skill_id"]
        except ValueError:
            identity = None
        matches = ([entry] if entry in rows and _is_learnable(rows[entry], masters[entry]) else
                   [sid for sid, row in rows.items() if _is_learnable(row, masters[sid]) and
                    (row.get("name_zh_cn") == entry or masters[sid]["skill_id"] in {entry, identity})])
        if len(matches) == 1:
            allowed.add(matches[0])
        resolution.append({"entry": entry, "matches": matches,
                           "status": "resolved" if len(matches) == 1 else "ambiguous_or_missing"})
    return allowed, resolution


def _existing_rules(profile, row):
    keys = {("skill_id", row["skill_id"]), ("category", row.get("type"))}
    keys.update(("tag", tag) for tag in row.get("tags", []))
    return [rule for rule in profile.get("rules", [])
            if (rule["selector"]["kind"], rule["selector"]["value"]) in keys]


def _hard_modes(rules):
    hard = [r for r in rules if r["mode"] in {"must_buy_if_safe", "avoid"}]
    exact = [r for r in hard if r["selector"]["kind"] == "skill_id"]
    return {r["mode"] for r in exact or hard}


def _eligible_discount(item, base, minimum):
    cost = item.get("display_cost")
    return (item.get("cost_basis") == "observed_final" and type(cost) is int and cost > 0
            and type(base) is int and base > 0 and (base - cost) * 100 >= base * minimum)


def _select_start_bundle(ratings, masters, items, owned_sources, eligible, *, budget, batch_cap):
    """Choose distinct skill families that can supply the start trigger count."""
    family = lambda master: next(iter(master.get("cost", {}).get("chain", [])), master["skill_id"])
    owned_families = {family(master) for master in owned_sources}
    ranked_sources = sorted((sid for sid in eligible if ratings[sid].start_source
                             and family(masters[sid]) not in owned_families),
                            key=lambda sid: (items[sid]["display_cost"], -ratings[sid].utility, sid))
    by_family = {}
    for sid in ranked_sources:
        by_family.setdefault(family(masters[sid]), sid)
    available = list(by_family.values())
    groundwork = sorted((sid for sid in eligible if ratings[sid].start_requirement),
                        key=lambda sid: (-masters[sid].get("upgrade_cost", 0), sid))
    for sid in groundwork:
        need = ratings[sid].start_requirement
        missing = max(0, need - len(owned_families))
        support = available[:missing]
        if len(support) != missing or 1 + missing > batch_cap:
            continue
        if items[sid]["display_cost"] + sum(items[x]["display_cost"] for x in support) > budget:
            continue
        for source in support:
            ratings[source] = replace(ratings[source], tier=0, priority=410,
                                      utility=max(128, ratings[source].utility), reason="groundwork_start_source")
        ratings[sid] = _Rating(0, 215, "groundwork_with_start_sources", 400, start_requirement=need)
        return {sid: {"required": need, "owned_source_count": len(owned_families), "required_skill_ids": support}}
    return {}


def compile_cm_skill_profile(profile, inventory, catalog, *, final_target, preset_id=None,
                             current_stats=None, year=3, terminal=None, classic_allowlist=(),
                             auto_cm=True, mid_reserve_pt=360, min_mid_discount_percent=20,
                             current_race_profile=None, planner_policy=None, knowledge=None):
    """Return a strict preference dictionary and a captured-inventory eligibility report."""
    if knowledge is None:
        from core.skill_knowledge import SkillKnowledgeLibrary
        knowledge = SkillKnowledgeLibrary.load()
    if hasattr(knowledge, "arc_race_inputs"):
        race_inputs, evaluate = knowledge.arc_race_inputs, knowledge.arc_evaluate_condition
    else:
        from core.skill_knowledge import race_inputs
        from core.skill_conditions import evaluate_condition as evaluate
    if terminal is None:
        terminal = year >= 3
    classic = year == 2 and not terminal
    derived = classic and not classic_allowlist and auto_cm
    if auto_cm:
        target, geometry = resolve_cm_target(final_target, preset_id)
    else:
        target, geometry = dict(final_target), None
    inputs = race_inputs(target)
    if "course_distance" in inputs:
        inputs["is_basis_distance"] = int(inputs["course_distance"] % 400 == 0)
    course = _course(target, geometry)
    result = deepcopy(profile)
    planner_policy = planner_policy or {}
    if auto_cm:
        result["base_profile"] = "race"
        result["max_batch_size"] = min(result["max_batch_size"], 24)
        if terminal:
            result.update(filler_policy="spend_all_safe", reserve_skill_pt=0)
        elif classic:
            result["filler_policy"] = "safe_useful_only"
    if derived:
        result["reserve_skill_pt"] = max(result["reserve_skill_pt"], mid_reserve_pt)
    budget = max(0, inventory.get("current_skill_points", 0)
                 - max(result["reserve_skill_pt"], planner_policy.get("reserve_skill_pt", 0)))
    if type(planner_policy.get("trigger_budget")) is int:
        budget = min(budget, planner_policy["trigger_budget"])
    batch_cap = min(result["max_batch_size"], planner_policy.get("max_purchase_count") or 48)
    rows = {row["skill_id"]: row for row in catalog.get("skills", [])}
    masters = {sid: _resolve_master(knowledge, row) for sid, row in rows.items()}
    allowed, resolution = _manual_allowlist(classic_allowlist, rows, masters, knowledge)
    owned = {}
    for sid in inventory.get("owned_skill_ids", []):
        try:
            row = masters.get(sid) or knowledge.get(sid)
            owned[row["skill_id"]] = row
        except ValueError:
            continue
    gate = stamina_recovery_gate(target, current_stats)
    owned_ratings = {mid: _rating(master, target, course, gate, inputs, evaluate)[0] for mid, master in owned.items()}
    superseded = {lower for master in owned.values() for lower in master.get("relations", {}).get("supersedes", [])}
    owned_heal = math.fsum(r.recovery_rate for mid, r in owned_ratings.items() if mid not in superseded)
    if gate["initial_hp"] is not None and owned_heal:
        gate = stamina_recovery_gate(target, current_stats, owned_recovery_hp=owned_heal * gate["initial_hp"])
    current_target, current_geometry = (resolve_cm_target(current_race_profile) if derived and current_race_profile else (None, None))
    if current_target:
        current_inputs = race_inputs(current_target)
        if "course_distance" in current_inputs:
            current_inputs["is_basis_distance"] = int(current_inputs["course_distance"] % 400 == 0)
        current_course = _course(current_target, current_geometry)
        current_gate = stamina_recovery_gate(current_target, current_stats)

    items = {item["skill_id"]: item for item in inventory.get("candidates", [])}
    action_ids = set(inventory.get("allowed_action_ids", items))
    decisions, ratings, source_rules, eligible = {}, {}, {}, set()
    for sid, item in sorted(items.items()):
        detail = decisions[sid] = {"skill_id": sid, "reason": None}
        row, master = rows.get(sid, {}), masters.get(sid)
        if not _is_learnable(row, master):
            detail["reason"] = "not_pt_learnable"
            continue
        if sid not in action_ids:
            detail["reason"] = "unselectable_capture_row"
            continue
        rules = source_rules[sid] = _existing_rules(profile, row)
        if "avoid" in _hard_modes(rules):
            detail["reason"] = "user_preference_avoid"
            continue
        if classic and not derived and sid not in allowed:
            detail["reason"] = "not_in_classic_allowlist"
            continue
        if (type(item.get("display_cost")) is not int or item["display_cost"] <= 0
                or item.get("cost_basis") not in {"base", "observed_final"}):
            detail["reason"] = "unverified_skill_price"
            continue
        rating, exclusion = _rating(master, target, course, gate, inputs, evaluate)
        detail["target_status"] = ("conditional" if exclusion is None else
                                    "unknown" if exclusion == "unknown_target_requirement" else "incompatible")
        if exclusion:
            detail["reason"] = exclusion
            continue
        quote = knowledge.quote_cost(master["skill_id"], owned=tuple(owned))
        base, cost = quote.get("cost_pt"), item["display_cost"]
        discount = 100 * (base - cost) / base if type(base) is int and base > 0 else None
        detail.update(remaining_chain_base_pt=base, cost_quote=quote,
                      effective_discount_percent=discount,
                      observed_step_discount_percent=item.get("observed_discount_percent"))
        if base == 0:
            detail["reason"] = quote["cost_basis"]
            continue
        if auto_cm and item.get("cost_basis") != "observed_final" and len(quote.get("steps", [])) > 1:
            detail["reason"] = "unverified_chain_price"
            continue
        if derived and not _eligible_discount(item, base, min_mid_discount_percent):
            detail["reason"] = "mid_chain_discount_below_minimum_or_unverified"
            continue
        if classic and not derived and not (_eligible_discount(item, base, 0)
                and discount is not None and discount > 0):
            detail["reason"] = "positive_discount_unverified"
            continue
        if auto_cm and current_target:
            actual, actual_exclusion = _rating(master, current_target, current_course,
                                               current_gate, current_inputs, evaluate)
            detail["current_race_reason"] = actual_exclusion or actual.reason
            if actual_exclusion or actual.tier > 1 and not (actual.start_requirement or actual.start_source):
                detail["reason"] = "not_current_race_core"
                continue
        if auto_cm and rating.tier == 3 and not rating.start_requirement and not (classic and classic_allowlist):
            detail["reason"] = rating.reason
            continue
        if auto_cm and derived and rating.tier == 2 and not rating.start_source:
            detail["reason"] = "not_cm_core"
            continue
        ratings[sid] = rating
        eligible.add(sid)

    bundles = {}
    if auto_cm and target.get("style") == "leader":
        bundles = _select_start_bundle(ratings, masters, items,
            [owned[mid] for mid, rating in owned_ratings.items() if rating.start_source],
            eligible, budget=budget, batch_cap=batch_cap)

    # Upgrades replace the lower's effect. Only the extra gain matters when
    # the lower is owned, and double-circle offers belong in the filler pass
    # when the equivalent single-circle is still available.
    visible_master_ids = {masters[sid]["skill_id"]: sid for sid in eligible}
    for sid in sorted(eligible):
        rating, master = ratings[sid], masters[sid]
        if not auto_cm or rating.tier >= 3:
            continue
        lower = master.get("relations", {}).get("supersedes", [])
        if rating.reason != "groundwork_start_source" and master.get("name_zh_cn", "").endswith("◎") and any(
                item in owned or item in visible_master_ids for item in lower):
            gain = max(3, rating.utility - max((ratings[visible_master_ids[x]].utility
                       if x in visible_master_ids else owned_ratings[x].utility
                       for x in lower if x in visible_master_ids or x in owned), default=0))
            ratings[sid] = _Rating(2, min(gain, 38), "double_circle_marginal_gain", start_source=rating.start_source)
        elif rating.priority and any(x in visible_master_ids for x in lower):
            # The upper and lower cannot both be obligations for the same effect.
            for mid in lower:
                if mid in visible_master_ids and ratings[visible_master_ids[mid]].reason == rating.reason:
                    lower_id = visible_master_ids[mid]
                    ratings[lower_id] = replace(ratings[lower_id], priority=0)

    # Extra recovery is filler once selected early healing covers the projected gap.
    gap = max(0., gate["conservation_deficit_hp"] or 0.)
    recovery = sorted((sid for sid in eligible if ratings[sid].priority == 380),
                      key=lambda sid: (-ratings[sid].recovery_rate / items[sid]["display_cost"], sid))
    committed_recovery = set()
    for sid in recovery:
        lower = masters[sid].get("relations", {}).get("supersedes", [])
        if gap > 0 and items[sid]["display_cost"] <= budget and not set(lower) & committed_recovery:
            committed_recovery.add(masters[sid]["skill_id"])
            gap -= ratings[sid].recovery_rate * gate["initial_hp"]
        else:
            ratings[sid] = _Rating(2, 19, "additional_early_recovery")

    if auto_cm:
        for sid in sorted(eligible):
            rating = ratings[sid]
            if rating.start_requirement and not rating.priority and not (classic and classic_allowlist):
                decisions[sid]["reason"] = "insufficient_start_activation_sources"
            elif derived and rating.tier > 1:
                decisions[sid]["reason"] = "not_cm_core"
        eligible = {sid for sid in eligible if decisions[sid]["reason"] is None}
        # Prefer trusted affordable core choices and cheap useful alternatives
        # when a capture contains more candidates than the optimizer admits.
        ordered = sorted(eligible, key=lambda sid: (items[sid]["display_cost"] > budget,
            ratings[sid].tier, -ratings[sid].priority, -ratings[sid].utility / items[sid]["display_cost"], sid))
        kept = set(ordered[:_MAX_CANDIDATES])
        for sid in eligible - kept:
            decisions[sid]["reason"] = "optimizer_candidate_shortlist"
        eligible = kept
        for sid, bundle in bundles.items():
            if sid in eligible and not set(bundle["required_skill_ids"]) <= eligible:
                eligible.remove(sid)
                decisions[sid]["reason"] = "insufficient_start_activation_sources"

    # Only one generated rule of each mode can exist per exact selector.
    candidate_ids = set(rows) | set(items)
    result["rules"] = [r for r in result.get("rules", []) if not (
        r["selector"]["kind"] == "skill_id" and r["selector"]["value"] in candidate_ids
        and (auto_cm or r["selector"]["value"] not in eligible))]
    result["rules"].extend(_rule(sid, "avoid") for sid in sorted(candidate_ids - eligible))
    if auto_cm:
        for sid in sorted(eligible):
            rating = ratings[sid]
            base = _native_utility(rows[sid])
            exact = [r for r in source_rules[sid] if r["selector"]["kind"] == "skill_id"]
            prefer = [r for r in source_rules[sid] if r["mode"] == "prefer"]
            prefer = [r for r in exact if r["mode"] == "prefer"] or prefer
            adjustment = sum(r.get("weight", 0) for r in prefer)
            boost = 0 if rating.tier == 3 else rating.utility - base
            if classic and classic_allowlist:
                boost = max(0, boost)
            priority = max((r["priority"] for r in source_rules[sid] if r["mode"] == "must_buy_if_safe"), default=0)
            if rating.priority or "must_buy_if_safe" in _hard_modes(source_rules[sid]):
                result["rules"].append(_rule(sid, "must_buy_if_safe", max(priority, rating.priority)))
            link = {"groundwork_with_start_sources": CM_GROUNDWORK_LINK,
                    "groundwork_start_source": CM_START_SOURCE_LINK}.get(rating.reason, 0)
            result["rules"].append(_rule(sid, "prefer", priority=link, weight=boost + adjustment))
    for sid, rating in ratings.items():
        decisions[sid].update(tier=f"T{rating.tier}" if rating.tier < 3 else None,
                              cm_reason=rating.reason, cm_utility=rating.utility)
    result["profile_id"] += CM_PROFILE_SUFFIX if auto_cm else "-arc-target"
    return result, {"enabled": True, "auto_cm": auto_cm, "year": year, "terminal_build": terminal,
        "final_target": target, "preset_id": geometry, "current_stats": merge_current_stats(current_stats),
        "stamina_recovery_gate": gate, "eligible_skill_ids": sorted(eligible), "allowlist_resolution": resolution,
        "groundwork_bundles": {sid: data for sid, data in bundles.items() if sid in eligible},
        "reserve_skill_pt": result["reserve_skill_pt"], "skills": list(decisions.values())}
