"""Structured, plan-bound race-result observations with no device actions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from core.auto_trainer.race_calendar import RaceCalendar


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GRADES = frozenset({"G1", "G2", "G3", "OPEN"})


class RaceResultObservationError(ValueError):
    """Stable validation error for result evidence."""


@dataclass(frozen=True)
class RaceResultObservation:
    run_session_id: str
    observed_turn: int
    race_id: str
    grade: str
    rank: int | None
    source_frame_sha256: str
    schedule_packet_sha256: str
    race_route_session_id: str
    confidence: float
    abstain_reasons: tuple[str, ...]
    observation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for value, reason in (
            (self.run_session_id, "run_session_id_invalid"),
            (self.race_id, "race_id_invalid"),
            (self.race_route_session_id, "race_route_session_id_invalid"),
        ):
            _require_text(value, reason)
        if type(self.observed_turn) is not int or self.observed_turn <= 0:
            raise RaceResultObservationError("observed_turn_invalid")
        if self.grade not in _GRADES:
            raise RaceResultObservationError("grade_invalid")
        if self.rank is not None and (
            type(self.rank) is not int or self.rank <= 0
        ):
            raise RaceResultObservationError("rank_invalid")
        _require_sha(
            self.source_frame_sha256,
            "source_frame_sha256_invalid",
        )
        _require_sha(
            self.schedule_packet_sha256,
            "schedule_packet_sha256_invalid",
        )
        if (
            type(self.confidence) not in {int, float}
            or isinstance(self.confidence, bool)
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise RaceResultObservationError("confidence_invalid")
        if (
            type(self.abstain_reasons) is not tuple
            or any(
                type(reason) is not str or not reason
                for reason in self.abstain_reasons
            )
            or len(set(self.abstain_reasons)) != len(self.abstain_reasons)
        ):
            raise RaceResultObservationError("abstain_reasons_invalid")
        if self.rank is None and not self.abstain_reasons:
            raise RaceResultObservationError("rank_unknown_without_abstain")
        object.__setattr__(
            self,
            "observation_sha256",
            _canonical_sha(
                {
                    "run_session_id": self.run_session_id,
                    "observed_turn": self.observed_turn,
                    "race_id": self.race_id,
                    "grade": self.grade,
                    "rank": self.rank,
                    "source_frame_sha256": self.source_frame_sha256,
                    "schedule_packet_sha256": self.schedule_packet_sha256,
                    "race_route_session_id": self.race_route_session_id,
                    "confidence": float(self.confidence),
                    "abstain_reasons": list(self.abstain_reasons),
                }
            ),
        )


def build_race_result_observation(
    image_path: str | Path,
    *,
    source_frame_sha256: str,
    run_session_id: str,
    observed_turn: int,
    scheduled_turn: int,
    planned_race_ids: tuple[str, ...],
    detected_race_id: str,
    expected_grade: str,
    detected_grade: str,
    rank: int | None,
    schedule_packet_sha256: str,
    race_route_session_id: str,
    confidence: float,
) -> RaceResultObservation:
    source = Path(image_path)
    _require_sha(source_frame_sha256, "source_frame_sha256_invalid")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise RaceResultObservationError("source_frame_unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != source_frame_sha256:
        raise RaceResultObservationError("source_frame_sha256_mismatch")
    if type(scheduled_turn) is not int or scheduled_turn <= 0:
        raise RaceResultObservationError("scheduled_turn_invalid")
    if (
        type(planned_race_ids) is not tuple
        or not planned_race_ids
        or any(type(item) is not str or not item for item in planned_race_ids)
        or len(set(planned_race_ids)) != len(planned_race_ids)
    ):
        raise RaceResultObservationError("planned_race_ids_invalid")
    _require_text(detected_race_id, "race_id_invalid")
    if expected_grade not in _GRADES or detected_grade not in _GRADES:
        raise RaceResultObservationError("grade_invalid")

    reasons: list[str] = []
    if observed_turn < scheduled_turn:
        reasons.append("race_result_stale")
    if detected_race_id not in planned_race_ids:
        reasons.append("race_id_not_planned")
    if detected_grade != expected_grade:
        reasons.append("race_grade_mismatch")
    if rank is None:
        reasons.append("race_rank_unknown")
    if (
        type(confidence) not in {int, float}
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise RaceResultObservationError("confidence_invalid")
    if float(confidence) < 0.90:
        reasons.append("race_result_low_confidence")
    return RaceResultObservation(
        run_session_id=run_session_id,
        observed_turn=observed_turn,
        race_id=detected_race_id,
        grade=detected_grade,
        rank=rank,
        source_frame_sha256=source_frame_sha256,
        schedule_packet_sha256=schedule_packet_sha256,
        race_route_session_id=race_route_session_id,
        confidence=float(confidence),
        abstain_reasons=tuple(reasons),
    )


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_text(value: object, reason: str) -> None:
    if type(value) is not str or not value:
        raise RaceResultObservationError(reason)


def _require_sha(value: object, reason: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise RaceResultObservationError(reason)


@dataclass(frozen=True)
class CountRaceResultReceipt:
    """An offline receipt; it grants no race-entry or result-continue authority."""

    observation: RaceResultObservation
    schedule_path: str
    schedule_file_sha256: str
    schedule: dict[str, object]
    race_report_path: str
    race_report_sha256: str
    confirmation_step: int
    result_step: int
    source_path: str
    receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _count_dict(value: object, reason: str) -> dict:
    if type(value) is not dict:
        raise RaceResultObservationError(reason)
    return value


def _count_file(path: str | Path, expected_sha: str | None = None) -> tuple[Path, bytes, str]:
    try:
        source = Path(path).resolve(strict=True)
        raw = source.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise RaceResultObservationError("count_source_unreadable") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if not raw or expected_sha is not None and actual != expected_sha:
        raise RaceResultObservationError("count_source_sha_mismatch")
    return source, raw, actual


def _count_json(path: str | Path) -> tuple[Path, dict, str]:
    source, raw, digest = _count_file(path)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RaceResultObservationError("count_source_json_invalid") from exc
    return source, _count_dict(value, "count_source_json_invalid"), digest


def _count_result_layout_matches(path: Path) -> bool:
    # The complete rank badge is on the ranking list, not the later rewards page.
    # Reuse the existing owner geometry; the separate rank reader binds the badge.
    import cv2
    from core.auto_trainer.race_route import RaceRoutePlanner

    image = cv2.imread(str(path))
    return image is not None and RaceRoutePlanner._looks_like_ranking_list_result(image)


def build_count_race_result_receipt(
    image_path: str | Path, *, source_frame_sha256: str,
    schedule_path: str | Path, race_report_path: str | Path, calendar: RaceCalendar,
) -> CountRaceResultReceipt:
    """Bind existing local result tokens to one persisted count reservation.

    Eligibility parsing and actual child-session creation belong to the caller.
    Neither nonempty evidence nor this receipt authenticates aptitude by itself.
    Missing structured result tokens fail closed; this function never runs OCR.
    """
    _require_sha(source_frame_sha256, "source_frame_sha256_invalid")
    image, _, image_sha = _count_file(image_path, source_frame_sha256)
    schedule_file, schedule, schedule_sha = _count_json(schedule_path)
    report_file, report, report_sha = _count_json(race_report_path)
    reservation = _count_dict(schedule.get("count_reservation"), "count_reservation_required")
    decision = _count_dict(schedule.get("decision"), "count_schedule_decision_required")
    if (schedule.get("schema_version") != "race_schedule_decision.v1"
        or reservation.get("schema_version") != "count_race_reservation.v1"):
        raise RaceResultObservationError("count_reservation_schema_invalid")
    for outer, inner in (("run_session_id", "run_session_id"), ("turn_index", "observed_runner_turn"),
                         ("source_frame_sha256", "source_frame_sha256"),
                         ("ledger_revision", "ledger_revision"), ("ledger_view_sha256", "ledger_view_sha256")):
        if outer not in schedule or schedule[outer] != reservation.get(inner):
            raise RaceResultObservationError("count_reservation_outer_mismatch")
    for key in ("run_session_id", "goal_instance_id", "race_id"):
        _require_text(reservation.get(key), "count_reservation_identity_invalid")
    for key in ("observed_runner_turn", "calendar_turn", "deadline_turn", "ledger_revision"):
        if type(reservation.get(key)) is not int or reservation[key] <= 0:
            raise RaceResultObservationError("count_reservation_turn_invalid")
    _require_sha(reservation.get("ledger_view_sha256"), "count_reservation_ledger_sha_invalid")
    _require_sha(reservation.get("source_frame_sha256"), "count_reservation_source_sha_required")
    _count_file(reservation.get("source_path"), reservation.get("source_frame_sha256"))
    race_id, goal_id = reservation["race_id"], reservation["goal_instance_id"]
    entry = calendar.get(race_id) if isinstance(calendar, RaceCalendar) else None
    if (entry is None or entry.schedule_status != "verified" or entry.career_time is None
        or entry.career_time.global_turn != reservation["calendar_turn"]
        or entry.grade != reservation.get("grade")
        or reservation["calendar_turn"] > reservation["deadline_turn"]):
        raise RaceResultObservationError("count_reservation_calendar_mismatch")
    eligibility = _count_dict(reservation.get("eligibility"), "count_eligibility_required")
    if (any(eligibility.get(key) != reservation[key] for key in ("run_session_id", "calendar_turn", "race_id"))
        or any(eligibility.get(key) is not True for key in (
            "aptitude_ok", "condition_ok", "consecutive_race_ok", "entry_conditions_met"))
        or not _count_dict(eligibility.get("evidence"), "count_eligibility_evidence_required")):
        raise RaceResultObservationError("count_eligibility_unverified")
    _require_sha(eligibility.get("source_frame_sha256"), "count_eligibility_source_sha_required")
    _count_file(eligibility.get("source_path"), eligibility.get("source_frame_sha256"))
    candidates = decision.get("candidates")
    if type(candidates) is not list or len(candidates) != 1:
        raise RaceResultObservationError("count_reservation_single_candidate_required")
    candidate = _count_dict(candidates[0], "count_reservation_candidate_invalid")
    expected_candidate = {"action_id": f"enter_race:{race_id}", "action_kind": "enter_race",
        "race_id": race_id, "priority": "none", "source_goal_ids": [goal_id],
        "reasons": ["count_goal_single_race_reservation"]}
    input_sha = _canonical_sha(reservation)
    expected_candidate["evidence_sha256"] = _canonical_sha({"input_sha256": input_sha, **expected_candidate})
    expected_decision = {"status": "count_race_reserved", "candidates": [expected_candidate],
        "required_remaining": {}, "verified_opportunity_race_ids": {}}
    expected_decision["packet_sha256"] = _canonical_sha({"input_sha256": input_sha, **expected_decision})
    if candidate != expected_candidate or decision != expected_decision:
        raise RaceResultObservationError("count_schedule_packet_mismatch")
    packet_sha = decision["packet_sha256"]
    if (report.get("run_session_id") != reservation["run_session_id"]
        or report.get("observed_runner_turn") != reservation["observed_runner_turn"]
        or report.get("planned_race_ids") != [race_id]
        or report.get("schedule_packet_sha256") != packet_sha):
        raise RaceResultObservationError("count_child_binding_mismatch")
    _require_text(report.get("race_route_session_id"), "count_child_session_required")
    steps = report.get("step_reports")
    if type(steps) is not list:
        raise RaceResultObservationError("count_child_steps_required")
    entries, confirmations, results = [], [], []
    previous_step = 0
    for step in steps:
        step = _count_dict(step, "count_child_step_invalid")
        number = step.get("step")
        if type(number) is not int or number <= previous_step:
            raise RaceResultObservationError("count_child_step_invalid")
        previous_step = number
        value = _count_dict(step.get("result"), "count_child_step_invalid")
        plan = _count_dict(value.get("decision"), "count_child_step_invalid")
        evidence = _count_dict(plan.get("evidence"), "count_child_step_invalid")
        if plan.get("target_race_verification") is not None:
            entries.append(step)
        if plan.get("consume_target_race_verification") is True:
            confirmations.append(step)
        try:
            result_path = Path(evidence.get("image")).resolve()
        except (OSError, TypeError, ValueError):
            result_path = None
        if result_path == image:
            results.append(step)
    if (len(entries) != 1 or len(confirmations) != 1 or len(results) != 1
        or not entries[0]["step"] < confirmations[0]["step"] < results[0]["step"]):
        raise RaceResultObservationError("count_confirmation_or_result_not_unique")
    entry_result = entries[0]["result"]
    confirmation, result = confirmations[0]["result"], results[0]["result"]
    confirm_plan = confirmation["decision"]
    verification = _count_dict(confirm_plan["evidence"].get("prior_target_race_verification"), "count_confirmation_proof_required")
    entry_plan = entry_result["decision"]
    if (entry_result.get("performed") is not True or entry_result.get("tap_performed") is not True
        or entry_plan.get("action") != "tap" or entry_plan.get("reason") != "enter_selected_target_race"
        or entry_plan.get("route_state") != "race_list"
        or _count_dict(entry_plan.get("tap"), "count_entry_action_invalid").get("coordinate_name") != "race_list.primary_entry"
        or entry_plan.get("target_race_verification") != verification):
        raise RaceResultObservationError("count_entry_confirmation_mismatch")
    if (confirmation.get("performed") is not True or confirmation.get("tap_performed") is not True
        or confirm_plan.get("action") != "tap" or confirm_plan.get("reason") != "confirm_race_entry"
        or _count_dict(confirm_plan.get("tap"), "count_confirmation_action_invalid").get("coordinate_name") != "race_confirm.primary"
        or any(verification.get(key) != expected for key, expected in (
            ("race_id", race_id), ("grade", entry.grade), ("schedule_packet_sha256", packet_sha)))):
        raise RaceResultObservationError("count_confirmation_invalid")
    for value, action_id in ((entry_result, "race_list.primary_entry"), (confirmation, "race_confirm.primary"), (result, None)):
        frame = _count_dict(value.get("frame_safety"), "count_frame_required")
        gate = _count_dict(value.get("safety_verdict"), "count_gate_required")
        _require_sha(frame.get("source_image_sha256"), "count_frame_sha_required")
        _, _, digest = _count_file(value["decision"]["evidence"].get("image"), frame.get("source_image_sha256"))
        if gate.get("allowed") is not True or gate.get("source_image_sha256") != digest or gate.get("action_id") != action_id:
            raise RaceResultObservationError("count_gate_binding_mismatch")
    result_plan = result["decision"]
    if (result.get("performed") is not False or result_plan.get("action") != "stop"
        or result_plan.get("reason") != "race_result:continue_not_allowed" or result_plan.get("tap") is not None
        or not _count_result_layout_matches(image)):
        raise RaceResultObservationError("count_result_layout_unverified")
    from core.auto_trainer.race_result_rank_reader import (
        RaceResultRankReaderError, validate_race_result_rank_evidence,
    )
    result_evidence = _count_dict(result.get("race_result"), "count_result_rank_evidence_required")
    try:
        rank_evidence = validate_race_result_rank_evidence(result_evidence.get("rank_evidence"))
    except RaceResultRankReaderError as exc:
        raise RaceResultObservationError("count_result_rank_evidence_invalid") from exc
    if (rank_evidence.source_frame_sha256 != image_sha or rank_evidence.status != "accepted"
        or rank_evidence.rank is None):
        raise RaceResultObservationError("count_result_rank_source_invalid")
    observation = build_race_result_observation(image, source_frame_sha256=image_sha,
        run_session_id=reservation["run_session_id"], observed_turn=report["observed_runner_turn"],
        scheduled_turn=reservation["observed_runner_turn"], planned_race_ids=(race_id,),
        detected_race_id=verification["race_id"], expected_grade=entry.grade, detected_grade=verification["grade"],
        rank=rank_evidence.rank, schedule_packet_sha256=packet_sha,
        race_route_session_id=report["race_route_session_id"], confidence=rank_evidence.confidence)
    # The accepted reader evidence above has already checked each role's floor;
    # a lower localization score remains truthful, not a failed PP rank reading.
    if observation.abstain_reasons == ("race_result_low_confidence",):
        observation = replace(observation, abstain_reasons=())
    material = dict(observation=observation, schedule_path=str(schedule_file), schedule_file_sha256=schedule_sha,
        schedule=schedule, race_report_path=str(report_file), race_report_sha256=report_sha,
        confirmation_step=confirmations[0]["step"], result_step=results[0]["step"], source_path=str(image))
    return CountRaceResultReceipt(**material, receipt_sha256=_canonical_sha({**material, "observation": asdict(observation)}))


def validate_count_race_result_receipt(receipt: CountRaceResultReceipt, *, calendar: RaceCalendar) -> None:
    if not isinstance(receipt, CountRaceResultReceipt):
        raise RaceResultObservationError("count_result_receipt_required")
    rebuilt = build_count_race_result_receipt(receipt.source_path,
        source_frame_sha256=receipt.observation.source_frame_sha256, schedule_path=receipt.schedule_path,
        race_report_path=receipt.race_report_path, calendar=calendar)
    if rebuilt.to_dict() != receipt.to_dict():
        raise RaceResultObservationError("count_result_receipt_mismatch")
