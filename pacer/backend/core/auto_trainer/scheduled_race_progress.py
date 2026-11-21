"""Fail-closed accounting for plan-bound scheduled race confirmations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ScheduledRaceProgressError(ValueError):
    """Stable validation error for scheduled-race progress evidence."""


@dataclass(frozen=True)
class ScheduledRaceProgress:
    entry_consumed: bool
    race_id: str | None
    confirmation_step: int | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "uma.scheduled_race_progress.v1",
            "entry_consumed": self.entry_consumed,
            "race_id": self.race_id,
            "confirmation_step": self.confirmation_step,
            "reason": self.reason,
        }


def inspect_scheduled_race_progress(
    race_report: dict[str, Any],
    *,
    planned_race_ids: tuple[str, ...],
    schedule_packet_sha256: str,
) -> ScheduledRaceProgress:
    if not isinstance(race_report, dict):
        raise ScheduledRaceProgressError("race_report_invalid")
    if (
        type(planned_race_ids) is not tuple
        or not planned_race_ids
        or any(type(race_id) is not str or not race_id for race_id in planned_race_ids)
        or len(set(planned_race_ids)) != len(planned_race_ids)
    ):
        raise ScheduledRaceProgressError("planned_race_ids_invalid")
    if (
        type(schedule_packet_sha256) is not str
        or _SHA256_RE.fullmatch(schedule_packet_sha256) is None
    ):
        raise ScheduledRaceProgressError("schedule_packet_sha256_invalid")

    steps = race_report.get("step_reports")
    if not isinstance(steps, list):
        return ScheduledRaceProgress(
            entry_consumed=False,
            race_id=None,
            confirmation_step=None,
            reason="scheduled_race_confirmation_missing",
        )

    valid: list[tuple[str, int]] = []
    saw_unperformed = False
    saw_mismatch = False
    for step_record in steps:
        if not isinstance(step_record, dict):
            continue
        step = step_record.get("step")
        result = step_record.get("result")
        if not isinstance(result, dict):
            continue
        decision = result.get("decision")
        if (
            not isinstance(decision, dict)
            or decision.get("consume_target_race_verification") is not True
        ):
            continue
        safety = result.get("safety_verdict")
        if (
            result.get("performed") is not True
            or not isinstance(safety, dict)
            or safety.get("allowed") is not True
        ):
            saw_unperformed = True
            continue
        evidence = decision.get("evidence")
        verification = (
            evidence.get("prior_target_race_verification")
            if isinstance(evidence, dict)
            else None
        )
        race_id = verification.get("race_id") if isinstance(verification, dict) else None
        if (
            decision.get("action") != "tap"
            or decision.get("reason") != "confirm_race_entry"
            or type(step) is not int
            or step <= 0
            or not isinstance(verification, dict)
            or race_id not in planned_race_ids
            or verification.get("grade") != "G1"
            or verification.get("schedule_packet_sha256")
            != schedule_packet_sha256
        ):
            saw_mismatch = True
            continue
        valid.append((race_id, step))

    if saw_mismatch:
        reason = "scheduled_race_confirmation_binding_mismatch"
    elif len(valid) > 1:
        reason = "scheduled_race_confirmation_ambiguous"
    elif len(valid) == 1:
        race_id, step = valid[0]
        return ScheduledRaceProgress(
            entry_consumed=True,
            race_id=race_id,
            confirmation_step=step,
            reason="scheduled_race_entry_consumed",
        )
    elif saw_unperformed:
        reason = "scheduled_race_confirmation_not_performed"
    else:
        reason = "scheduled_race_confirmation_missing"
    return ScheduledRaceProgress(
        entry_consumed=False,
        race_id=None,
        confirmation_step=None,
        reason=reason,
    )
