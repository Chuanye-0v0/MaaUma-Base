"""Skill-learning timing policy helpers.

The loop should visit the skill page at times chosen by the goal's
``skill_timing`` profile (e.g. terminal bulk-buy before the final race,
early required counts for support-card bonuses, fixed intervals, or
immediate purchase).  This module keeps the pure decision logic separate
from the ADB/live wiring so it can be unit-tested without an emulator.
"""

from __future__ import annotations

from typing import Any, Mapping

from core.auto_trainer.config import SkillTiming


def _field_value(state: Mapping[str, Any], key: str) -> Any:
    fields = state.get("fields")
    if not isinstance(fields, Mapping):
        return None
    field = fields.get(key)
    if not isinstance(field, Mapping):
        return None
    value = field.get("value")
    if isinstance(value, bool):
        return None
    return value


def remaining_turns_from_state(state: Mapping[str, Any]) -> int | None:
    value = _field_value(state, "remaining_turns")
    return value if isinstance(value, int) else None


def skill_pt_from_state(state: Mapping[str, Any]) -> int | None:
    value = _field_value(state, "skill_pt")
    return value if isinstance(value, int) else None


def turn_index_from_state(state: Mapping[str, Any]) -> int | None:
    value = _field_value(state, "turn_index")
    if value is not None:
        return value if isinstance(value, int) else None
    context_turn = state.get("turn_index")
    return context_turn if isinstance(context_turn, int) else None


def should_enter_terminal_skill_turn(
    state: Mapping[str, Any],
    timing: SkillTiming,
    *,
    total_turns: int = 72,
) -> bool:
    """Whether this turn should visit the skill page for the terminal bulk-buy.

    Only the ``terminal`` mode (and ``scenario_required_first`` with no
    outstanding early requirements, which behaves like terminal) triggers
    here; other modes are wired separately.
    """
    if timing.mode not in {"terminal", "scenario_required_first"}:
        return False
    skill_pt = skill_pt_from_state(state)
    if skill_pt is None:
        return False
    # The main-menu "remaining turns" panel is the current goal deadline, not
    # the run end.  For the terminal bulk-buy we must anchor to the runner's
    # monotonic turn index (total_turns - turn_index); the goal deadline is
    # only a fallback when the turn index is unreadable (e.g. race-day menu).
    turn_index = turn_index_from_state(state)
    if isinstance(turn_index, int):
        remaining = total_turns - turn_index
    else:
        remaining = remaining_turns_from_state(state)
    if remaining is None or remaining < 0:
        return False
    if remaining > timing.final_lead_turns:
        return False
    return skill_pt >= timing.min_pt
