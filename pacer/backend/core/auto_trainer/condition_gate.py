"""Pure triggered-condition gate policy and evidence classification."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from core.auto_trainer.condition_tracker import ConditionQuickSnapshot, ConditionTracker


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EFFECT_KEYS = frozenset(
    {
        "schema_version",
        "resolution",
        "changes_condition_or_mood",
        "event_sha256",
        "choice_sha256",
        "result_sha256",
        "version_scope",
    }
)


class ConditionProbePolicy(str, Enum):
    ALWAYS = "always"
    TRIGGERED = "triggered"
    OFF = "off"


@dataclass(frozen=True)
class ConditionGateDecision:
    required: bool
    reasons: Sequence[str]


def build_condition_quick_snapshot(
    provider_report: Mapping[str, object],
    *,
    expected_turn: int,
    expected_source_sha256: str,
) -> ConditionQuickSnapshot:
    """Build a fail-closed quick snapshot from a verified provider report."""
    try:
        if not isinstance(provider_report, Mapping):
            raise ValueError
        if provider_report.get("schema_version") != "main_menu_state_context.v1":
            raise ValueError
        if provider_report.get("valid_for_training") is not True:
            raise ValueError
        source = provider_report.get("source")
        context = provider_report.get("state_context")
        if not isinstance(source, Mapping) or not isinstance(context, Mapping):
            raise ValueError
        source_sha256 = source.get("sha256")
        if not _is_sha256(source_sha256) or not _is_sha256(expected_source_sha256):
            raise ValueError
        turn = context.get("turn")
        turn_index = context.get("turn_index")
        if (
            not _is_nonnegative_int(turn)
            or not _is_nonnegative_int(expected_turn)
            or not _is_nonnegative_int(turn_index)
        ):
            raise ValueError
        vitality = context.get("vitality")
        if not _is_finite_number(vitality):
            raise ValueError
        scenario_state = context.get("scenario_state")
        if not isinstance(scenario_state, Mapping):
            raise ValueError
        mood = scenario_state.get("mood")
        if not isinstance(mood, str) or not mood:
            raise ValueError
        raw_condition = context.get("condition_snapshot")
        expected_keys = {"visible_condition_signature", "suspected_negative"}
        if not isinstance(raw_condition, Mapping) or not expected_keys.issubset(raw_condition):
            raise ValueError
        signature = raw_condition["visible_condition_signature"]
        suspected_negative = raw_condition["suspected_negative"]
        if signature is not None and (not isinstance(signature, str) or not signature):
            raise ValueError
        if not isinstance(suspected_negative, bool):
            raise ValueError
        snapshot = ConditionQuickSnapshot(
            source_frame_sha256=source_sha256,
            vitality=float(vitality),
            mood=mood,
            visible_condition_signature=signature,
            suspected_negative=suspected_negative,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("condition_quick_snapshot_invalid") from exc
    # The existing owner catches ValueError and reacquires one fresh source.
    # An adjacent turn or different frame is a refresh request, not a usable
    # condition snapshot. Larger turn mismatches keep the old invalid verdict.
    if max(abs(turn - expected_turn), abs(turn_index - expected_turn)) > 1:
        raise ValueError("condition_quick_snapshot_invalid")
    if source_sha256 != expected_source_sha256:
        raise ValueError("condition_quick_snapshot_invalid:source_refresh_required")
    if turn != expected_turn or turn_index != expected_turn:
        raise ValueError("condition_quick_snapshot_invalid:turn_refresh_required")
    return snapshot


def decide_condition_probe(
    policy: ConditionProbePolicy,
    tracker: ConditionTracker,
    global_action_turn: int,
    critical_race: bool,
) -> ConditionGateDecision:
    """Resolve policy without duplicating the tracker's triggered-rule ordering."""
    if not isinstance(policy, ConditionProbePolicy):
        raise ValueError("condition_probe_policy_invalid")
    if not isinstance(tracker, ConditionTracker):
        raise ValueError("condition_tracker_required")
    if policy is ConditionProbePolicy.ALWAYS:
        return ConditionGateDecision(True, ("policy_always",))
    if policy is ConditionProbePolicy.OFF:
        return ConditionGateDecision(False, ())
    plan = tracker.plan_probe(global_action_turn, critical_race=critical_race)
    return ConditionGateDecision(
        plan.required, tuple(reason.value for reason in plan.reasons)
    )


def apply_event_chain_risk(
    tracker: ConditionTracker, route_steps: Sequence[object]
) -> bool:
    """Dirty once unless every successful event has exact no-effect evidence."""
    if not isinstance(tracker, ConditionTracker):
        raise ValueError("condition_tracker_required")
    if isinstance(route_steps, (str, bytes)) or not isinstance(route_steps, Sequence):
        raise ValueError("condition_route_steps_invalid")
    events = [
        step
        for step in route_steps
        if isinstance(step, Mapping)
        and step.get("kind") == "event_choice"
    ]
    if not events:
        return False
    if all(
        step.get("success") is False
        or (
            step.get("success") is True
            and _is_exact_no_effect_envelope(step)
        )
        for step in events
    ):
        return False
    tracker.mark_dirty()
    return True


def _is_exact_no_effect_envelope(step: Mapping[str, object]) -> bool:
    value = step.get("condition_effect")
    if not isinstance(value, Mapping) or set(value) != _EFFECT_KEYS:
        return False
    if (
        value.get("schema_version") != "event_condition_effect.v1"
        or value.get("resolution") != "exact"
        or value.get("changes_condition_or_mood") is not False
        or value.get("version_scope") != "cn"
    ):
        return False
    for key in ("event_sha256", "choice_sha256", "result_sha256"):
        if not _is_sha256(step.get(key)) or value.get(key) != step.get(key):
            return False
    return True


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
