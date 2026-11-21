"""Soft consecutive-race preference using calendar planning options, not entry proofs."""

from core.auto_trainer.race_calendar import RaceCalendar
from core.auto_trainer.race_plan import RacePlan


def plan_count_race_continuation(*, calendar: RaceCalendar, race_plan: RacePlan,
    current_race_id: str, current_turn: int, deadline_turn: int,
    required_upper_bound: int, previous_action_kind: str) -> dict:
    """The caller must verify history and current eligibility before executing a race.

    Future dates remain potential options: aptitude, health and outcomes are not
    inferred. A shortfall means risk to covering the progress upper bound, never
    proof that the game's actual remaining requirement makes this race mandatory.
    """
    if previous_action_kind not in {"race", "non_race"}:
        raise ValueError("count_policy_previous_action_unverified")
    if (not isinstance(calendar, RaceCalendar) or not isinstance(race_plan, RacePlan)
        or any(type(value) is not int for value in (current_turn, deadline_turn, required_upper_bound))
        or current_turn < 1 or deadline_turn < current_turn or required_upper_bound < 1):
        raise ValueError("count_policy_context_invalid")
    current = calendar.get(current_race_id)
    if (current is None or current.career_time is None
        or current.career_time.global_turn != current_turn or current.schedule_status != "verified"):
        raise ValueError("count_policy_current_race_unverified")
    mandatory = []
    for race_id in {row.race_id for row in race_plan.directives}:
        directive = race_plan.resolve(race_id, hard_goal=False, scenario_mandatory=False)
        if not directive.executable or directive.directive != "must_run" or race_id == current_race_id:
            continue
        entry = calendar.get(race_id)
        if entry is None or entry.career_time is None or entry.career_time.global_turn == current_turn:
            raise ValueError("count_goal_other_race_obligation_pending")
        if current_turn < entry.career_time.global_turn <= deadline_turn:
            mandatory.append(entry)
    future = []
    undated = []
    for entry in calendar.entries:
        if entry.grade != current.grade:
            continue
        if entry.career_time is None:
            undated.append(entry.race_id)
            continue
        turn = entry.career_time.global_turn
        if not current_turn < turn <= deadline_turn or entry.schedule_status == "unavailable":
            continue
        if any((fixed.career_time.global_turn == turn and fixed.grade != current.grade)
               or abs(fixed.career_time.global_turn - turn) == 1 for fixed in mandatory):
            continue
        directive = race_plan.resolve(entry.race_id, hard_goal=False, scenario_mandatory=False)
        if directive.executable and directive.directive == "avoid_run":
            continue
        future.append({"race_id": entry.race_id, "calendar_turn": turn,
            "schedule_status": entry.schedule_status, "eligibility_status": "unknown"})
    future.sort(key=lambda row: (row["calendar_turn"], row["race_id"]))
    # Earliest-first gives the largest subset with a non-race turn between dates.
    # Skipping current_turn also breaks the run from current_turn - 1.
    spaced = []
    for turn in sorted({row["calendar_turn"] for row in future if row["schedule_status"] == "verified"}):
        if not spaced or turn > spaced[-1] + 1:
            spaced.append(turn)
    current_directive = race_plan.resolve(current_race_id, hard_goal=False, scenario_mandatory=False)
    action = "enter_race"
    if current_directive.executable and current_directive.directive == "must_run":
        reason = "explicit_current_must_run"
    elif current_directive.executable and current_directive.directive == "avoid_run":
        action, reason = "defer_to_local_policy", "explicit_current_avoid_run"
    elif previous_action_kind == "non_race":
        reason = "previous_action_not_race"
    elif len(spaced) >= required_upper_bound + race_plan.reserve_opportunities:
        action, reason = "defer_to_local_policy", "avoid_consecutive_with_calendar_options"
    elif len(spaced) >= required_upper_bound:
        reason = "count_goal_buffer_risk"
    else:
        reason = "count_goal_calendar_shortfall"
    return {"action": action, "reason": reason, "current_race_id": current_race_id,
        "current_turn": current_turn, "deadline_turn": deadline_turn,
        "previous_action_kind": previous_action_kind, "required_upper_bound": required_upper_bound,
        "reserve_opportunities": race_plan.reserve_opportunities,
        "future_calendar_options": future, "spaced_future_turns": spaced,
        "spaced_turns_basis": "verified_dates_only_eligibility_unknown",
        "undated_same_grade_race_ids": sorted(undated),
        "future_eligibility_status": "unknown", "calendar_exhaustive": False}
