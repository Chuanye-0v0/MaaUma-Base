"""Run-bound, append-only persistence for structured career goals."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.career_goal_perception import (
    CareerGoalObservation,
    career_goal_observation_to_dict,
    is_goal_achievement_title,
    match_main_menu_count_goal,
    match_main_menu_named_goal,
)
from core.auto_trainer.race_calendar import RaceCalendar, RaceCalendarError, career_global_turn


SCHEMA_VERSION = "career_goal_ledger.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ZERO_SHA = "0" * 64
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "run_session_id",
        "revision",
        "observed_turn",
        "goals",
        "race_results",
        "revisions",
        "current_view_sha256",
    }
)
_REVISION_KEYS = frozenset(
    {
        "revision",
        "previous_revision_sha256",
        "event_kind",
        "source_sha256",
        "view",
        "revision_sha256",
    }
)
_VIEW_KEYS = frozenset(
    {
        "run_session_id",
        "revision",
        "observed_turn",
        "goals",
        "race_results",
    }
)
_GOAL_KEYS = frozenset(
    {
        "goal_instance_id",
        "goal_kind",
        "grade",
        "required_rank",
        "required_count",
        "completed_count",
        "deadline_turn",
        "status",
        "source_packet_sha256",
        "evidence_refs",
    }
)
_RACE_RESULT_KEYS = frozenset(
    {
        "run_session_id",
        "observed_turn",
        "race_id",
        "grade",
        "rank",
        "source_frame_sha256",
        "schedule_packet_sha256",
        "race_route_session_id",
        "confidence",
        "abstain_reasons",
        "observation_sha256",
    }
)


class CareerGoalLedgerError(ValueError):
    """Raised when ledger evidence or persistence fails closed."""


@dataclass(frozen=True)
class CareerGoal:
    goal_instance_id: str
    goal_kind: str
    grade: str
    required_rank: int
    required_count: int
    completed_count: int
    deadline_turn: int | None
    status: str
    source_packet_sha256: str
    evidence_refs: tuple[str, ...]
    target_race_id: str | None = None
    remaining_turns: int | None = None
    candidate_race_ids: tuple[str, ...] = ()
    progress_basis: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = {
            "goal_instance_id": self.goal_instance_id,
            "goal_kind": self.goal_kind,
            "grade": self.grade,
            "required_rank": self.required_rank,
            "required_count": self.required_count,
            "completed_count": self.completed_count,
            "deadline_turn": self.deadline_turn,
            "status": self.status,
            "source_packet_sha256": self.source_packet_sha256,
            "evidence_refs": list(self.evidence_refs),
        }
        if self.goal_kind == "named_race_rank":
            payload["target_race_id"] = self.target_race_id
            payload["remaining_turns"] = self.remaining_turns
        if self.candidate_race_ids:
            payload["candidate_race_ids"] = list(self.candidate_race_ids)
        if self.progress_basis is not None:
            payload["progress_basis"] = self.progress_basis
            payload["remaining_turns"] = self.remaining_turns
        return payload


@dataclass(frozen=True)
class CareerGoalLedger:
    schema_version: str
    run_session_id: str
    revision: int
    observed_turn: int
    goals: tuple[CareerGoal, ...]
    race_results: tuple[Mapping[str, object], ...]
    revisions: tuple[Mapping[str, object], ...]
    current_view_sha256: str

    @classmethod
    def empty(cls, run_session_id: str) -> "CareerGoalLedger":
        _require_nonempty_string(run_session_id, "run_session_id_invalid")
        view = _view_dict(
            run_session_id=run_session_id,
            revision=0,
            observed_turn=0,
            goals=(),
            race_results=(),
        )
        return cls(
            schema_version=SCHEMA_VERSION,
            run_session_id=run_session_id,
            revision=0,
            observed_turn=0,
            goals=(),
            race_results=(),
            revisions=(),
            current_view_sha256=_view_head_sha(view, _ZERO_SHA),
        )

    def active_goals(self) -> tuple[CareerGoal, ...]:
        return tuple(goal for goal in self.goals if goal.status == "active")

    def pending_named_goals(self) -> tuple[CareerGoal, ...]:
        return tuple(goal for goal in self.active_goals()
                     if goal.goal_kind == "named_race_rank" and goal.target_race_id is None)

    def pending_count_goals(self) -> tuple[CareerGoal, ...]:
        return tuple(goal for goal in self.active_goals()
                     if goal.goal_kind == "graded_race_rank_count"
                     and goal.progress_basis == "verified_lower_bound"
                     and goal.deadline_turn is None)

    def required_remaining(self, goal_instance_id: str) -> int:
        for goal in self.goals:
            if goal.goal_instance_id == goal_instance_id:
                if goal.progress_basis == "verified_lower_bound":
                    raise CareerGoalLedgerError("count_goal_progress_lower_bound_only")
                return max(goal.required_count - goal.completed_count, 0)
        raise CareerGoalLedgerError("goal_instance_unknown")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "revision": self.revision,
            "observed_turn": self.observed_turn,
            "goals": [goal.to_dict() for goal in self.goals],
            "race_results": [dict(result) for result in self.race_results],
            "revisions": [_json_copy(revision) for revision in self.revisions],
            "current_view_sha256": self.current_view_sha256,
        }


@dataclass(frozen=True)
class LedgerMergeResult:
    ledger: CareerGoalLedger
    status: str
    reason: str
    progressed_goal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class NamedGoalCompletionReceipt:
    run_session_id: str
    observed_turn: int
    goal_instance_id: str
    target_race_id: str
    grade: str
    required_rank: int
    required_count: int
    candidate_race_ids: tuple[str, ...]
    source_path: str
    source_frame_sha256: str
    normalized_text: str
    normalized_text_sha256: str
    receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["candidate_race_ids"] = list(self.candidate_race_ids)
        return payload


def build_named_goal_completion_receipt(
    *, run_session_id: str, observed_turn: int, active_goal: CareerGoal,
    source_path: str | Path, source_frame_sha256: str, visible_text: str,
    allow_continue_variant: bool = False,
) -> NamedGoalCompletionReceipt:
    _validate_goal(active_goal)
    if (active_goal.goal_kind != "named_race_rank" or active_goal.status != "active"
        or active_goal.target_race_id is None):
        raise CareerGoalLedgerError("named_goal_completion_active_bound_goal_required")
    if type(visible_text) is not str:
        raise CareerGoalLedgerError("named_goal_completion_text_invalid")
    normalized = "\n".join(line.strip() for line in unicodedata.normalize("NFKC", visible_text).strip().splitlines())
    if normalized.splitlines()[-1:] == ["继续"] and allow_continue_variant is not True:
        raise CareerGoalLedgerError("named_goal_completion_continue_not_verified")
    receipt = NamedGoalCompletionReceipt(
        run_session_id=run_session_id, observed_turn=observed_turn,
        goal_instance_id=active_goal.goal_instance_id, target_race_id=active_goal.target_race_id,
        grade=active_goal.grade, required_rank=active_goal.required_rank,
        required_count=active_goal.required_count, candidate_race_ids=active_goal.candidate_race_ids,
        source_path=str(Path(source_path).resolve()),
        source_frame_sha256=source_frame_sha256, normalized_text=normalized,
        normalized_text_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(), receipt_sha256="",
    )
    material = receipt.to_dict()
    material.pop("receipt_sha256")
    receipt = replace(receipt, receipt_sha256=_canonical_sha(material))
    _validate_named_goal_completion_receipt(receipt)
    return receipt


def _validate_named_goal_completion_receipt(receipt: NamedGoalCompletionReceipt) -> None:
    if not isinstance(receipt, NamedGoalCompletionReceipt):
        raise CareerGoalLedgerError("named_goal_completion_receipt_required")
    for value in (receipt.run_session_id, receipt.goal_instance_id, receipt.target_race_id, receipt.source_path):
        _require_nonempty_string(value, "named_goal_completion_identity_invalid")
    _require_positive_int(receipt.observed_turn, "observed_turn_invalid")
    _validate_candidate_ids(receipt.candidate_race_ids)
    for value in (receipt.source_frame_sha256,
                  receipt.normalized_text_sha256, receipt.receipt_sha256):
        _require_sha(value, "named_goal_completion_sha_invalid")
    material = receipt.to_dict()
    material.pop("receipt_sha256")
    if _canonical_sha(material) != receipt.receipt_sha256:
        raise CareerGoalLedgerError("named_goal_completion_receipt_sha_mismatch")
    text = receipt.normalized_text
    if (type(text) is not str
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt.normalized_text_sha256):
        raise CareerGoalLedgerError("named_goal_completion_text_sha_mismatch")
    lines = text.splitlines()
    contract = match_main_menu_named_goal(lines[1]) if len(lines) == 3 else None
    if (len(lines) != 3 or not is_goal_achievement_title(lines[0]) or lines[2] not in {"完成!", "继续"} or not contract
        or receipt.grade != contract["grade"] or type(receipt.required_rank) is not int
        or receipt.required_rank != contract["required_rank"] or type(receipt.required_count) is not int
        or receipt.required_count != 1
        or receipt.candidate_race_ids != tuple(contract.get("candidate_race_ids", ()))
        or (receipt.target_race_id not in receipt.candidate_race_ids if receipt.candidate_race_ids
            else receipt.target_race_id != contract["target_race_id"])):
        raise CareerGoalLedgerError("named_goal_completion_contract_mismatch")
    try:
        raw = Path(receipt.source_path).read_bytes()
    except OSError as exc:
        raise CareerGoalLedgerError("named_goal_completion_source_missing") from exc
    if not raw or hashlib.sha256(raw).hexdigest() != receipt.source_frame_sha256:
        raise CareerGoalLedgerError("named_goal_completion_source_sha_mismatch")


@dataclass(frozen=True)
class CountGoalCompletionReceipt:
    run_session_id: str
    observed_turn: int
    goal_instance_id: str
    grade: str
    required_rank: int
    required_count: int
    source_path: str
    source_frame_sha256: str
    normalized_text: str
    normalized_text_sha256: str
    receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_count_goal_completion_receipt(
    *, run_session_id: str, observed_turn: int, active_goal: CareerGoal,
    source_path: str | Path, source_frame_sha256: str, visible_text: str,
    allow_continue_variant: bool = False,
) -> CountGoalCompletionReceipt:
    _validate_goal(active_goal)
    if (active_goal.goal_kind != "graded_race_rank_count" or active_goal.status != "active"
        or active_goal.progress_basis != "verified_lower_bound" or active_goal.deadline_turn is None
        or active_goal.completed_count < active_goal.required_count):
        raise CareerGoalLedgerError("count_goal_completion_verified_goal_required")
    if type(visible_text) is not str:
        raise CareerGoalLedgerError("count_goal_completion_text_invalid")
    normalized = "\n".join(line.strip() for line in unicodedata.normalize("NFKC", visible_text).strip().splitlines())
    if normalized.splitlines()[-1:] == ["继续"] and allow_continue_variant is not True:
        raise CareerGoalLedgerError("count_goal_completion_continue_not_verified")
    receipt = CountGoalCompletionReceipt(
        run_session_id=run_session_id, observed_turn=observed_turn,
        goal_instance_id=active_goal.goal_instance_id, grade=active_goal.grade,
        required_rank=active_goal.required_rank, required_count=active_goal.required_count,
        source_path=str(Path(source_path).resolve()), source_frame_sha256=source_frame_sha256,
        normalized_text=normalized, normalized_text_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        receipt_sha256="",
    )
    material = receipt.to_dict()
    material.pop("receipt_sha256")
    receipt = replace(receipt, receipt_sha256=_canonical_sha(material))
    _validate_count_goal_completion_receipt(receipt)
    return receipt


def _validate_count_goal_completion_receipt(receipt: CountGoalCompletionReceipt) -> None:
    if not isinstance(receipt, CountGoalCompletionReceipt):
        raise CareerGoalLedgerError("count_goal_completion_receipt_required")
    for value in (receipt.run_session_id, receipt.goal_instance_id, receipt.source_path):
        _require_nonempty_string(value, "count_goal_completion_identity_invalid")
    _require_positive_int(receipt.observed_turn, "observed_turn_invalid")
    for value in (receipt.source_frame_sha256, receipt.normalized_text_sha256, receipt.receipt_sha256):
        _require_sha(value, "count_goal_completion_sha_invalid")
    material = receipt.to_dict()
    material.pop("receipt_sha256")
    if _canonical_sha(material) != receipt.receipt_sha256:
        raise CareerGoalLedgerError("count_goal_completion_receipt_sha_mismatch")
    text = receipt.normalized_text
    if type(text) is not str or hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt.normalized_text_sha256:
        raise CareerGoalLedgerError("count_goal_completion_text_sha_mismatch")
    lines = text.splitlines()
    contract = match_main_menu_count_goal(lines[1]) if len(lines) == 3 else None
    if (len(lines) != 3 or not is_goal_achievement_title(lines[0]) or lines[2] not in {"完成!", "继续"}
        or not contract or receipt.grade != contract["grade"]
        or type(receipt.required_rank) is not int or receipt.required_rank != contract["required_rank"]
        or type(receipt.required_count) is not int or receipt.required_count != contract["required_count"]):
        raise CareerGoalLedgerError("count_goal_completion_contract_mismatch")
    try:
        raw = Path(receipt.source_path).read_bytes()
    except OSError as exc:
        raise CareerGoalLedgerError("count_goal_completion_source_missing") from exc
    if not raw or hashlib.sha256(raw).hexdigest() != receipt.source_frame_sha256:
        raise CareerGoalLedgerError("count_goal_completion_source_sha_mismatch")


@dataclass(frozen=True)
class CountGoalMainMenuCompletionReceipt:
    run_session_id: str
    observed_turn: int
    ledger_revision: int
    ledger_view_sha256: str
    goal_instance_id: str
    grade: str
    required_rank: int
    required_count: int
    deadline_turn: int
    anchor: dict[str, object]
    source_path: str
    source_frame_sha256: str
    receipt_sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _validate_count_main_menu_predecessor(goal: Mapping[str, object], revision: Mapping[str, object],
                                        prior_view: Mapping[str, object]) -> None:
    error = "count_goal_main_menu_qualifying_result_required"
    prior = next((item for item in prior_view["goals"]
                  if item["goal_instance_id"] == goal["goal_instance_id"]), None)
    view = revision["view"]
    results, prior_results = view["race_results"], prior_view["race_results"]
    if (revision["event_kind"] != "count_race_result" or prior is None
        or goal["goal_kind"] != "graded_race_rank_count" or goal["status"] != "active"
        or goal.get("progress_basis") != "verified_lower_bound" or goal["deadline_turn"] is None
        or goal["completed_count"] not in {goal["required_count"] - 1, goal["required_count"]}
        or prior["status"] != "active" or prior["completed_count"] != goal["completed_count"] - 1
        or any(prior.get(key) != goal.get(key) for key in (
            "goal_kind", "grade", "required_rank", "required_count", "deadline_turn", "progress_basis"))
        or revision["source_sha256"] not in goal["evidence_refs"]
        or revision["source_sha256"] in prior["evidence_refs"]
        or len(results) != len(prior_results) + 1 or results[:-1] != prior_results):
        raise CareerGoalLedgerError(error)
    result = results[-1]
    if (result["grade"] != goal["grade"] or result["rank"] > goal["required_rank"]
        or result["abstain_reasons"] or result["observed_turn"] != view["observed_turn"]
        or any(item["race_id"] == result["race_id"]
               or item["schedule_packet_sha256"] == result["schedule_packet_sha256"] for item in prior_results)):
        raise CareerGoalLedgerError(error)


def _count_main_menu_completion_goal(ledger: CareerGoalLedger) -> CareerGoal:
    active = ledger.active_goals()
    if len(active) != 1 or len(ledger.revisions) < 2:
        raise CareerGoalLedgerError("count_goal_main_menu_active_goal_required")
    goal = active[0]
    _validate_count_main_menu_predecessor(goal.to_dict(), ledger.revisions[-1], ledger.revisions[-2]["view"])
    return goal


def build_count_goal_main_menu_completion_receipt(
    *, ledger: CareerGoalLedger, anchor: Mapping[str, object], run_session_id: str,
    observed_turn: int, source_path: str | Path, source_frame_sha256: str,
) -> CountGoalMainMenuCompletionReceipt:
    """Bind a main-menu achieved marker to the latest qualifying count result."""
    CareerGoalLedgerStore().verify_hash_chain(ledger)
    if ledger.run_session_id != run_session_id:
        raise CareerGoalLedgerError("run_session_mismatch")
    _require_positive_int(observed_turn, "observed_turn_invalid")
    if observed_turn < ledger.observed_turn:
        raise CareerGoalLedgerError("observed_turn_regression")
    if observed_turn != ledger.observed_turn + 1:
        raise CareerGoalLedgerError("count_goal_main_menu_next_turn_required")
    goal = _count_main_menu_completion_goal(ledger)
    if not isinstance(anchor, Mapping):
        raise CareerGoalLedgerError("count_goal_main_menu_anchor_invalid")
    try:
        source = str(Path(source_path).resolve())
    except (OSError, TypeError, ValueError) as exc:
        raise CareerGoalLedgerError("count_goal_main_menu_source_missing") from exc
    receipt = CountGoalMainMenuCompletionReceipt(
        run_session_id=run_session_id, observed_turn=observed_turn, ledger_revision=ledger.revision,
        ledger_view_sha256=ledger.current_view_sha256, goal_instance_id=goal.goal_instance_id,
        grade=goal.grade, required_rank=goal.required_rank, required_count=goal.required_count,
        deadline_turn=goal.deadline_turn, anchor=_json_copy(anchor),
        source_path=source, source_frame_sha256=source_frame_sha256, receipt_sha256="")
    material = receipt.to_dict()
    material.pop("receipt_sha256")
    receipt = replace(receipt, receipt_sha256=_canonical_sha(material))
    _validate_count_goal_main_menu_completion_receipt(receipt)
    return receipt


def _validate_count_goal_main_menu_completion_receipt(receipt: CountGoalMainMenuCompletionReceipt) -> None:
    if not isinstance(receipt, CountGoalMainMenuCompletionReceipt):
        raise CareerGoalLedgerError("count_goal_main_menu_receipt_required")
    for value in (receipt.run_session_id, receipt.goal_instance_id, receipt.source_path):
        _require_nonempty_string(value, "count_goal_main_menu_identity_invalid")
    for value in (receipt.observed_turn, receipt.ledger_revision, receipt.required_rank,
                  receipt.required_count, receipt.deadline_turn):
        _require_positive_int(value, "count_goal_main_menu_contract_invalid")
    for value in (receipt.ledger_view_sha256, receipt.source_frame_sha256, receipt.receipt_sha256):
        _require_sha(value, "count_goal_main_menu_sha_invalid")
    material = receipt.to_dict()
    material.pop("receipt_sha256")
    if _canonical_sha(material) != receipt.receipt_sha256:
        raise CareerGoalLedgerError("count_goal_main_menu_receipt_sha_mismatch")
    anchor = receipt.anchor
    if (not isinstance(anchor, Mapping) or anchor.get("status") != "verified"
        or anchor.get("basis") != "same_frame_main_menu_header"
        or anchor.get("source_frame_sha256") != receipt.source_frame_sha256
        or "target_race_id" in anchor or "candidate_race_ids" in anchor):
        raise CareerGoalLedgerError("count_goal_main_menu_anchor_invalid")
    evidence, marker = anchor.get("evidence"), anchor.get("goal_achieved_marker")
    if (not isinstance(evidence, Mapping) or not isinstance(marker, Mapping)
        or marker.get("status") != "verified"
        or marker.get("verified") is not True or marker.get("present") is not True
        or marker.get("source_frame_sha256") != receipt.source_frame_sha256
        or type(marker.get("text")) is not str
        or re.sub(r"\s+", "", unicodedata.normalize("NFKC", marker["text"])) != "目标达成!"
        or type(marker.get("confidence")) not in (int, float) or not .95 <= marker["confidence"] <= 1
        or type(evidence.get("goal_confidence")) not in (int, float)
        or not .95 <= evidence["goal_confidence"] <= 1):
        raise CareerGoalLedgerError("count_goal_main_menu_marker_unverified")
    roi, bbox, color = marker.get("roi"), marker.get("bbox"), marker.get("color_evidence")
    if (not isinstance(roi, (list, tuple)) or len(roi) != 4 or any(type(v) is not int for v in roi)
        or list(roi) != [496, 104, 609, 132]
        or not isinstance(bbox, (list, tuple)) or len(bbox) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox)
        or not (roi[0] <= bbox[0] < bbox[2] <= roi[2] and roi[1] <= bbox[1] < bbox[3] <= roi[3])):
        raise CareerGoalLedgerError("count_goal_main_menu_marker_bbox_invalid")
    if (not isinstance(color, Mapping)
        or set(color) != {"hsv_lower", "hsv_upper", "orange_pixels", "orange_ratio"}
        or color.get("hsv_lower") != [5, 120, 140] or color.get("hsv_upper") != [30, 255, 255]
        or type(color.get("orange_pixels")) is not int or color["orange_pixels"] < 100
        or type(color.get("orange_ratio")) not in (int, float) or not .05 <= color["orange_ratio"] <= .50
        or abs(color["orange_ratio"] - color["orange_pixels"] / ((roi[2] - roi[0]) * (roi[3] - roi[1]))) > 1e-9):
        raise CareerGoalLedgerError("count_goal_main_menu_marker_color_invalid")
    contract = match_main_menu_count_goal(evidence.get("goal_text"))
    if (not contract or contract != {"goal_kind": "graded_race_rank_count", "grade": receipt.grade,
        "required_rank": receipt.required_rank, "required_count": receipt.required_count}
        or any(anchor.get(key) != value for key, value in contract.items())
        or any(type(anchor.get(key)) is not int for key in ("required_rank", "required_count", "deadline_turn"))
        or anchor["deadline_turn"] != receipt.deadline_turn):
        raise CareerGoalLedgerError("count_goal_main_menu_contract_mismatch")
    time, remaining = anchor.get("career_time"), anchor.get("remaining_turns")
    if not isinstance(time, Mapping):
        raise CareerGoalLedgerError("count_goal_main_menu_calendar_invalid")
    try:
        calendar_turn = career_global_turn(stage=time.get("stage"), year_index=time.get("year_index"),
            month=time.get("month"), half=time.get("half"))
    except (RaceCalendarError, TypeError) as exc:
        raise CareerGoalLedgerError("count_goal_main_menu_calendar_invalid") from exc
    if (type(time.get("global_turn")) is not int or time["global_turn"] != calendar_turn
        or type(remaining) is not int or not 0 <= remaining <= 99
        or calendar_turn + remaining != receipt.deadline_turn):
        raise CareerGoalLedgerError("count_goal_main_menu_calendar_invalid")
    try:
        raw = Path(receipt.source_path).read_bytes()
    except OSError as exc:
        raise CareerGoalLedgerError("count_goal_main_menu_source_missing") from exc
    if not raw or hashlib.sha256(raw).hexdigest() != receipt.source_frame_sha256:
        raise CareerGoalLedgerError("count_goal_main_menu_source_sha_mismatch")


class CareerGoalLedgerStore:
    def load(
        self,
        path: Path,
        *,
        expected_run_session_id: str,
    ) -> CareerGoalLedger:
        _require_nonempty_string(
            expected_run_session_id,
            "run_session_id_invalid",
        )
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise CareerGoalLedgerError("ledger_read_failed") from exc
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_members,
                parse_constant=_reject_nonstandard_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CareerGoalLedgerError("ledger_json_invalid") from exc
        ledger = _ledger_from_payload(payload)
        if ledger.run_session_id != expected_run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        self.verify_hash_chain(ledger)
        return ledger

    def merge_observation(
        self,
        ledger: CareerGoalLedger,
        observation: CareerGoalObservation,
    ) -> LedgerMergeResult:
        self.verify_hash_chain(ledger)
        _validate_observation(observation)
        if observation.abstain_reasons or not observation.is_content_complete:
            return LedgerMergeResult(
                ledger=ledger,
                status="no_change",
                reason="goal_observation_abstained",
            )
        if observation.observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")

        assert observation.goal_kind is not None
        assert observation.grade is not None
        assert observation.required_rank is not None
        assert observation.required_count is not None
        assert observation.completed_count is not None
        lower_bound = observation.progress_basis == "verified_lower_bound"
        if observation.goal_kind != "named_race_rank" and not lower_bound:
            assert observation.cutoff_turn is not None
        if lower_bound:
            active = ledger.active_goals()
            if active and (len(active) != 1 or (
                active[0].progress_basis != observation.progress_basis
                or active[0].goal_kind != observation.goal_kind
                or active[0].grade != observation.grade
                or active[0].required_rank != observation.required_rank
                or active[0].required_count != observation.required_count
            )):
                raise CareerGoalLedgerError("goal_observation_conflict")
            goal_instance_id = active[0].goal_instance_id if active else _count_goal_instance_id(
                ledger.run_session_id, grade=observation.grade,
                required_rank=observation.required_rank, required_count=observation.required_count,
                activation_revision=ledger.revision + 1,
            )
        else:
            goal_instance_id = _goal_instance_id(
                ledger.run_session_id,
                goal_kind=observation.goal_kind,
                grade=observation.grade,
                deadline_turn=observation.cutoff_turn,
                target_race_id=observation.target_race_id,
                candidate_race_ids=observation.candidate_race_ids,
            )
            prior = next((goal for goal in ledger.goals if goal.goal_instance_id == goal_instance_id), None)
            if (observation.goal_kind == "named_race_rank" and observation.candidate_race_ids
                and prior is not None and prior.status == "completed"
                and prior.required_rank != observation.required_rank):
                # A later highlighted rank contract may reuse the same race
                # candidates. Keep the completed contract's legacy ID intact.
                digest = _canonical_sha({"prior_goal_instance_id": goal_instance_id,
                    "required_rank": observation.required_rank, "required_count": observation.required_count})
                goal_instance_id = f"goal-{digest[:16]}"
                if any(goal.goal_instance_id != goal_instance_id for goal in ledger.active_goals()):
                    raise CareerGoalLedgerError("goal_observation_conflict")
        goals = list(ledger.goals)
        existing_index = next(
            (
                index
                for index, goal in enumerate(goals)
                if goal.goal_instance_id == goal_instance_id
            ),
            None,
        )
        if existing_index is None:
            same_contract = next(
                (
                    goal
                    for goal in goals
                    if goal.goal_kind == observation.goal_kind
                    and goal.grade == observation.grade
                    and goal.required_rank == observation.required_rank
                    and goal.required_count == observation.required_count
                    and goal.target_race_id == observation.target_race_id
                    and goal.candidate_race_ids == observation.candidate_race_ids
                ),
                None,
            )
            if same_contract is not None:
                # Without a static goal ID, a deadline change can be either a
                # new sequential goal or OCR drift.  Treating it as a second
                # target would over-schedule races, so wait for stronger
                # identity evidence instead.
                raise CareerGoalLedgerError("goal_observation_conflict")
            completed_count = observation.completed_count
            goal = CareerGoal(
                goal_instance_id=goal_instance_id,
                goal_kind=observation.goal_kind,
                grade=observation.grade,
                required_rank=observation.required_rank,
                required_count=observation.required_count,
                completed_count=completed_count,
                deadline_turn=observation.cutoff_turn,
                status=(
                    "completed"
                    if not lower_bound and completed_count >= observation.required_count
                    else "active"
                ),
                source_packet_sha256=observation.observation_sha256,
                evidence_refs=(
                    observation.source_frame_sha256,
                    observation.observation_sha256,
                ),
                target_race_id=observation.target_race_id,
                candidate_race_ids=observation.candidate_race_ids,
                remaining_turns=(
                    observation.remaining_turns
                    if observation.goal_kind == "named_race_rank" or lower_bound
                    else None
                ),
                progress_basis=observation.progress_basis,
            )
            _validate_goal(goal)
            goals.append(goal)
        else:
            existing = goals[existing_index]
            if (
                existing.goal_kind != observation.goal_kind
                or existing.grade != observation.grade
                or existing.required_rank != observation.required_rank
                or existing.required_count != observation.required_count
                or (not lower_bound and existing.deadline_turn != observation.cutoff_turn)
                or existing.candidate_race_ids != observation.candidate_race_ids
                or (existing.target_race_id != observation.target_race_id
                    and not (observation.candidate_race_ids and observation.target_race_id is None))
            ):
                raise CareerGoalLedgerError("goal_observation_conflict")
            relative_new_evidence = (
                (observation.goal_kind == "named_race_rank" or lower_bound)
                and observation.observation_sha256 not in existing.evidence_refs
            )
            if (
                observation.completed_count <= existing.completed_count
                and not relative_new_evidence
            ):
                return LedgerMergeResult(
                    ledger=ledger,
                    status="no_change",
                    reason="goal_observation_not_stronger",
                )
            completed_count = max(observation.completed_count, existing.completed_count)
            goals[existing_index] = replace(
                existing,
                completed_count=completed_count,
                status=(
                    "completed"
                    if not lower_bound and completed_count >= existing.required_count
                    else "active"
                ),
                source_packet_sha256=observation.observation_sha256,
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            *existing.evidence_refs,
                            observation.source_frame_sha256,
                            observation.observation_sha256,
                        )
                    )
                ),
                remaining_turns=(
                    observation.remaining_turns
                    if observation.goal_kind == "named_race_rank" or lower_bound
                    else None
                ),
            )

        updated = _append_revision(
            ledger,
            observed_turn=observation.observed_turn,
            goals=tuple(sorted(goals, key=lambda item: item.goal_instance_id)),
            race_results=ledger.race_results,
            event_kind="goal_observation",
            source_sha256=observation.observation_sha256,
        )
        return LedgerMergeResult(
            ledger=updated,
            status="updated",
            reason="goal_observation_merged",
        )

    def _validated_main_menu_occurrence(
        self, ledger: CareerGoalLedger, *, anchor: Mapping[str, object],
        observed_turn: int, source_frame_sha256: str,
        expected_run_session_id: str, race_calendar: RaceCalendar,
    ) -> tuple[Mapping[str, object], tuple[str, ...], str, int]:
        """Validate the existing same-frame header contract without mutation.

        The supervisor owns freshness: it calls the provider on the final source
        frame in this run, never on a manual state-context anchor.
        """
        self.verify_hash_chain(ledger)
        if ledger.run_session_id != expected_run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        _require_positive_int(observed_turn, "observed_turn_invalid")
        _require_sha(source_frame_sha256, "goal_anchor_source_invalid")
        if observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        if (not isinstance(anchor, Mapping) or anchor.get("status") != "verified"
            or anchor.get("basis") != "same_frame_main_menu_header"
            or anchor.get("source_frame_sha256") != source_frame_sha256):
            raise CareerGoalLedgerError("career_goal_occurrence_unbound")
        time = anchor.get("career_time")
        evidence = anchor.get("evidence")
        if not isinstance(time, Mapping) or not isinstance(evidence, Mapping):
            raise CareerGoalLedgerError("career_goal_occurrence_unbound")
        if any(type(evidence.get(key)) not in (int, float) or not 0.95 <= evidence[key] <= 1
               for key in ("date_confidence", "goal_confidence")):
            raise CareerGoalLedgerError("career_goal_occurrence_unbound")
        try:
            turn = career_global_turn(stage=time.get("stage"), year_index=time.get("year_index"),
                                      month=time.get("month"), half=time.get("half"))
        except (RaceCalendarError, TypeError) as exc:
            raise CareerGoalLedgerError("career_goal_occurrence_unbound") from exc
        goal_text = evidence.get("goal_text")
        contract = match_main_menu_named_goal(goal_text) if type(goal_text) is str else None
        remaining = anchor.get("remaining_turns")
        remaining_field = evidence.get("remaining_field_evidence")
        candidates = tuple(contract.get("candidate_race_ids", ())) if contract else ()
        if (not candidates or list(candidates) != anchor.get("candidate_race_ids")
            or anchor.get("goal_kind") != "named_race_rank"
            or anchor.get("grade") != contract["grade"]
            or anchor.get("required_rank") != contract["required_rank"]
            or type(time.get("global_turn")) is not int or time["global_turn"] != turn
            or type(remaining) is not int or not 0 <= remaining <= 99
            or not isinstance(remaining_field, Mapping)
            or remaining_field.get("abstained") is not False
            or type(remaining_field.get("confidence")) not in (int, float)
            or not 0.8 <= remaining_field["confidence"] <= 1
            or type(remaining_field.get("value")) is not int or remaining_field["value"] != remaining):
            raise CareerGoalLedgerError("career_goal_occurrence_unbound")
        matches = [race_calendar.get(race_id) for race_id in candidates]
        matches = [entry for entry in matches if entry is not None
                   and entry.schedule_status == "verified" and entry.career_time is not None
                   and entry.grade == contract["grade"] and entry.career_time.global_turn == turn + remaining]
        if (len(matches) != 1 or anchor.get("target_race_id") != matches[0].race_id
            or anchor.get("target_calendar_turn") != matches[0].career_time.global_turn):
            raise CareerGoalLedgerError("career_goal_occurrence_conflict")
        return contract, candidates, matches[0].race_id, remaining

    def bind_count_goal_deadline(
        self, ledger: CareerGoalLedger, *, anchor: Mapping[str, object],
        observed_turn: int, source_frame_sha256: str,
        expected_run_session_id: str,
    ) -> LedgerMergeResult:
        """Bind only a persisted lower-bound count goal; never infer progress."""
        self.verify_hash_chain(ledger)
        if ledger.run_session_id != expected_run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        _require_positive_int(observed_turn, "observed_turn_invalid")
        _require_sha(source_frame_sha256, "goal_anchor_source_invalid")
        if observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        error = "career_goal_count_calendar_unbound"
        if (not isinstance(anchor, Mapping) or anchor.get("status") != "verified"
            or anchor.get("basis") != "same_frame_main_menu_header"
            or anchor.get("source_frame_sha256") != source_frame_sha256
            or "target_race_id" in anchor or "candidate_race_ids" in anchor):
            raise CareerGoalLedgerError(error)
        time, evidence = anchor.get("career_time"), anchor.get("evidence")
        if not isinstance(time, Mapping) or not isinstance(evidence, Mapping):
            raise CareerGoalLedgerError(error)
        if any(type(evidence.get(key)) not in (int, float) or not .95 <= evidence[key] <= 1
               for key in ("date_confidence", "goal_confidence")):
            raise CareerGoalLedgerError(error)
        date_text = evidence.get("date_text")
        date = re.fullmatch(r"(初级|经典级|高级)([1-9]|1[0-2])月(上|下)半月", date_text) if type(date_text) is str else None
        if date is None:
            raise CareerGoalLedgerError(error)
        stage, year = {"初级": ("junior", 1), "经典级": ("classic", 2), "高级": ("senior", 3)}[date[1]]
        month, half = int(date[2]), "first" if date[3] == "上" else "second"
        turn = career_global_turn(stage=stage, year_index=year, month=month, half=half)
        if (time.get("stage") != stage or time.get("half") != half
            or any(type(time.get(key)) is not int or time[key] != value
                   for key, value in (("year_index", year), ("month", month), ("global_turn", turn)))):
            raise CareerGoalLedgerError(error)
        split = evidence.get("split_date_regions")
        if split is not None:
            if not isinstance(split, (list, tuple)) or len(split) != 2:
                raise CareerGoalLedgerError(error)
            boxes = []
            for row in split:
                if (not isinstance(row, (list, tuple)) or len(row) != 3
                    or type(row[1]) is not str or type(row[2]) not in (int, float)
                    or not .95 <= row[2] <= 1 or not isinstance(row[0], (list, tuple)) or len(row[0]) != 4):
                    raise CareerGoalLedgerError(error)
                if any(not isinstance(point, (list, tuple)) or len(point) != 2
                       or any(type(v) not in (int, float) for v in point)
                       or not 155 <= point[0] <= 345 or not 4 <= point[1] <= 40 for point in row[0]):
                    raise CareerGoalLedgerError(error)
                xs, ys = [p[0] for p in row[0]], [p[1] + 32 for p in row[0]]
                boxes.append([min(xs), min(ys), max(xs), max(ys)])
            left, right = boxes
            if (split[0][1] != date[1] or split[1][1] != f"{month}月{date[3]}半月"
                or not left[0] < left[2] <= right[0] < right[2]
                or max(left[1], right[1]) >= min(left[3], right[3])
                or evidence.get("date_bbox") != [left[0], min(left[1], right[1]), right[2], max(left[3], right[3])]
                or evidence["date_confidence"] != min(split[0][2], split[1][2])):
                raise CareerGoalLedgerError(error)
        contract = match_main_menu_count_goal(evidence.get("goal_text"))
        remaining, deadline = anchor.get("remaining_turns"), anchor.get("deadline_turn")
        remaining_field = evidence.get("remaining_field_evidence")
        if (contract is None or any(anchor.get(k) != v for k, v in contract.items())
            or any(type(anchor.get(k)) is not int for k in ("required_rank", "required_count"))
            or type(remaining) is not int or not 0 <= remaining <= 99
            or type(deadline) is not int or deadline <= 0 or deadline < turn or deadline != turn + remaining
            or not isinstance(remaining_field, Mapping) or remaining_field.get("abstained") is not False
            or type(remaining_field.get("value")) is not int or remaining_field["value"] != remaining
            or type(remaining_field.get("confidence")) not in (int, float)
            or not .8 <= remaining_field["confidence"] <= 1):
            raise CareerGoalLedgerError(error)
        active = ledger.active_goals()
        if len(active) != 1:
            raise CareerGoalLedgerError("count_goal_binding_conflict")
        goal = active[0]
        if (goal.progress_basis != "verified_lower_bound"
            or any(getattr(goal, k) != v for k, v in contract.items())
            or goal.target_race_id is not None or goal.candidate_race_ids):
            raise CareerGoalLedgerError("count_goal_binding_conflict")
        if goal.deadline_turn is not None:
            if goal.deadline_turn != deadline:
                raise CareerGoalLedgerError("count_goal_deadline_conflict")
            return LedgerMergeResult(ledger, "no_change", "count_goal_deadline_already_bound")
        if source_frame_sha256 in goal.evidence_refs:
            raise CareerGoalLedgerError("goal_anchor_source_not_fresh")
        receipt_sha = _canonical_sha(anchor)
        bound = replace(goal, deadline_turn=deadline, remaining_turns=remaining,
            source_packet_sha256=receipt_sha,
            evidence_refs=tuple(dict.fromkeys((*goal.evidence_refs, source_frame_sha256, receipt_sha))))
        updated = _append_revision(ledger, observed_turn=observed_turn,
            goals=tuple(bound if g.goal_instance_id == goal.goal_instance_id else g for g in ledger.goals),
            race_results=ledger.race_results, event_kind="count_deadline_binding", source_sha256=receipt_sha)
        self.verify_hash_chain(updated)
        return LedgerMergeResult(updated, "updated", "count_goal_deadline_bound")

    def bind_named_goal_occurrence(
        self, ledger: CareerGoalLedger, *, anchor: Mapping[str, object],
        observed_turn: int, source_frame_sha256: str,
        expected_run_session_id: str, race_calendar: RaceCalendar,
    ) -> LedgerMergeResult:
        """Bind persisted summary content; this API cannot seed an empty ledger."""
        contract, candidates, race_id, remaining = self._validated_main_menu_occurrence(
            ledger, anchor=anchor, observed_turn=observed_turn,
            source_frame_sha256=source_frame_sha256,
            expected_run_session_id=expected_run_session_id, race_calendar=race_calendar,
        )
        matching = [goal for goal in ledger.active_goals() if goal.candidate_race_ids == candidates]
        if len(matching) != 1:
            raise CareerGoalLedgerError("career_goal_pending_content_missing")
        goal = matching[0]
        if (goal.grade != contract["grade"] or goal.required_rank != contract["required_rank"]
            or goal.remaining_turns != remaining
            or goal.target_race_id not in (None, race_id)):
            raise CareerGoalLedgerError("career_goal_occurrence_conflict")
        if goal.target_race_id is not None:
            return LedgerMergeResult(ledger, "no_change", "goal_occurrence_already_bound")
        if source_frame_sha256 in goal.evidence_refs:
            raise CareerGoalLedgerError("goal_anchor_source_not_fresh")
        receipt_sha = _canonical_sha(anchor)
        bound = replace(goal, target_race_id=race_id,
                        source_packet_sha256=receipt_sha,
                        evidence_refs=tuple(dict.fromkeys((*goal.evidence_refs, source_frame_sha256, receipt_sha))))
        updated = _append_revision(
            ledger, observed_turn=observed_turn,
            goals=tuple(bound if item.goal_instance_id == goal.goal_instance_id else item for item in ledger.goals),
            race_results=ledger.race_results, event_kind="goal_occurrence_binding", source_sha256=receipt_sha,
        )
        self.verify_hash_chain(updated)
        return LedgerMergeResult(updated, "updated", "goal_occurrence_bound")

    def recover_named_goal_from_main_menu(
        self, ledger: CareerGoalLedger, *, anchor: Mapping[str, object],
        observed_turn: int, source_frame_sha256: str,
        expected_run_session_id: str, race_calendar: RaceCalendar,
    ) -> LedgerMergeResult:
        """Explicit recovery from a fresh header, never a synthetic summary event."""
        contract, candidates, race_id, remaining = self._validated_main_menu_occurrence(
            ledger, anchor=anchor, observed_turn=observed_turn,
            source_frame_sha256=source_frame_sha256,
            expected_run_session_id=expected_run_session_id, race_calendar=race_calendar,
        )
        identity = _goal_instance_id(ledger.run_session_id, goal_kind="named_race_rank",
                                    grade=contract["grade"], deadline_turn=None, candidate_race_ids=candidates)
        if ledger.goals:
            if (len(ledger.goals) == 1 and ledger.goals[0].goal_instance_id == identity
                and ledger.goals[0].status == "active" and ledger.goals[0].target_race_id == race_id
                and ledger.goals[0].grade == contract["grade"]
                and ledger.goals[0].required_rank == contract["required_rank"]):
                return LedgerMergeResult(ledger, "no_change", "goal_main_menu_recovery_already_present")
            raise CareerGoalLedgerError("goal_main_menu_recovery_conflict")
        receipt_sha = _canonical_sha(anchor)
        goal = CareerGoal(
            goal_instance_id=identity, goal_kind="named_race_rank", grade=contract["grade"],
            required_rank=contract["required_rank"], required_count=1, completed_count=0,
            deadline_turn=None, status="active", source_packet_sha256=receipt_sha,
            evidence_refs=(source_frame_sha256, receipt_sha), target_race_id=race_id,
            remaining_turns=remaining, candidate_race_ids=candidates,
        )
        updated = _append_revision(
            ledger, observed_turn=observed_turn, goals=(goal,), race_results=ledger.race_results,
            event_kind="goal_main_menu_recovery", source_sha256=receipt_sha,
        )
        self.verify_hash_chain(updated)
        return LedgerMergeResult(updated, "updated", "goal_main_menu_recovered")

    def record_named_goal_completion(
        self, ledger: CareerGoalLedger, receipt: NamedGoalCompletionReceipt,
    ) -> LedgerMergeResult:
        self.verify_hash_chain(ledger)
        _validate_named_goal_completion_receipt(receipt)
        if receipt.run_session_id != ledger.run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        if any(revision["event_kind"] == "named_goal_completion"
               and revision["source_sha256"] == receipt.receipt_sha256 for revision in ledger.revisions):
            return LedgerMergeResult(ledger, "no_change", "named_goal_completion_duplicate")
        if receipt.observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        active = tuple(goal for goal in ledger.active_goals() if goal.goal_kind == "named_race_rank")
        if len(active) != 1 or active[0].goal_instance_id != receipt.goal_instance_id:
            raise CareerGoalLedgerError("named_goal_completion_goal_conflict")
        goal = active[0]
        if (goal.target_race_id != receipt.target_race_id or goal.grade != receipt.grade
            or goal.required_rank != receipt.required_rank or goal.required_count != receipt.required_count
            or goal.candidate_race_ids != receipt.candidate_race_ids
            or any(receipt.source_frame_sha256 in item.evidence_refs for item in ledger.goals)):
            raise CareerGoalLedgerError("named_goal_completion_goal_conflict")
        completed = replace(goal, completed_count=goal.required_count, status="completed",
            evidence_refs=tuple(dict.fromkeys((*goal.evidence_refs, receipt.source_frame_sha256, receipt.receipt_sha256))))
        updated = _append_revision(ledger, observed_turn=receipt.observed_turn,
            goals=tuple(completed if item.goal_instance_id == goal.goal_instance_id else item for item in ledger.goals),
            race_results=ledger.race_results, event_kind="named_goal_completion", source_sha256=receipt.receipt_sha256)
        self.verify_hash_chain(updated)
        return LedgerMergeResult(updated, "updated", "named_goal_completed", (goal.goal_instance_id,))

    def record_count_goal_completion(
        self, ledger: CareerGoalLedger, receipt: CountGoalCompletionReceipt,
    ) -> LedgerMergeResult:
        self.verify_hash_chain(ledger)
        _validate_count_goal_completion_receipt(receipt)
        if receipt.run_session_id != ledger.run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        if any(revision["event_kind"] == "count_goal_completion"
               and revision["source_sha256"] == receipt.receipt_sha256 for revision in ledger.revisions):
            return LedgerMergeResult(ledger, "no_change", "count_goal_completion_duplicate")
        if receipt.observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        active = ledger.active_goals()
        if len(active) != 1 or active[0].goal_instance_id != receipt.goal_instance_id:
            raise CareerGoalLedgerError("count_goal_completion_goal_conflict")
        goal = active[0]
        if (goal.goal_kind != "graded_race_rank_count" or goal.progress_basis != "verified_lower_bound"
            or goal.deadline_turn is None or goal.completed_count < goal.required_count
            or goal.grade != receipt.grade or goal.required_rank != receipt.required_rank
            or goal.required_count != receipt.required_count
            or any(receipt.source_frame_sha256 in item.evidence_refs for item in ledger.goals)):
            raise CareerGoalLedgerError("count_goal_completion_goal_conflict")
        completed = replace(goal, status="completed",
            evidence_refs=tuple(dict.fromkeys((*goal.evidence_refs, receipt.source_frame_sha256, receipt.receipt_sha256))))
        updated = _append_revision(ledger, observed_turn=receipt.observed_turn,
            goals=tuple(completed if item.goal_instance_id == goal.goal_instance_id else item for item in ledger.goals),
            race_results=ledger.race_results, event_kind="count_goal_completion", source_sha256=receipt.receipt_sha256)
        self.verify_hash_chain(updated)
        return LedgerMergeResult(updated, "updated", "count_goal_completed", (goal.goal_instance_id,))

    def record_count_goal_main_menu_completion(
        self, ledger: CareerGoalLedger, receipt: CountGoalMainMenuCompletionReceipt,
    ) -> LedgerMergeResult:
        self.verify_hash_chain(ledger)
        _validate_count_goal_main_menu_completion_receipt(receipt)
        if receipt.run_session_id != ledger.run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        if any(revision["event_kind"] == "count_goal_main_menu_completion"
               and revision["source_sha256"] == receipt.receipt_sha256 for revision in ledger.revisions):
            return LedgerMergeResult(ledger, "no_change", "count_goal_main_menu_completion_duplicate")
        if receipt.observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        if receipt.observed_turn != ledger.observed_turn + 1:
            raise CareerGoalLedgerError("count_goal_main_menu_next_turn_required")
        goal = _count_main_menu_completion_goal(ledger)
        if (receipt.ledger_revision != ledger.revision or receipt.ledger_view_sha256 != ledger.current_view_sha256
            or any(getattr(receipt, key) != getattr(goal, key) for key in (
                "goal_instance_id", "grade", "required_rank", "required_count", "deadline_turn"))
            or any(receipt.source_frame_sha256 in item.evidence_refs for item in ledger.goals)):
            raise CareerGoalLedgerError("count_goal_main_menu_ledger_binding_mismatch")
        completed = replace(goal, completed_count=goal.required_count, status="completed",
            evidence_refs=tuple(dict.fromkeys((*goal.evidence_refs, receipt.source_frame_sha256, receipt.receipt_sha256))))
        updated = _append_revision(ledger, observed_turn=receipt.observed_turn,
            goals=tuple(completed if item.goal_instance_id == goal.goal_instance_id else item for item in ledger.goals),
            race_results=ledger.race_results, event_kind="count_goal_main_menu_completion", source_sha256=receipt.receipt_sha256)
        self.verify_hash_chain(updated)
        return LedgerMergeResult(updated, "updated", "count_goal_main_menu_completed", (goal.goal_instance_id,))

    def record_race_result(
        self,
        ledger: CareerGoalLedger,
        result: object,
    ) -> LedgerMergeResult:
        self.verify_hash_chain(ledger)
        if any(goal.progress_basis == "verified_lower_bound" for goal in ledger.active_goals()):
            raise CareerGoalLedgerError("count_goal_result_activation_unverified")
        normalized = _normalize_race_result(result)
        if normalized["run_session_id"] != ledger.run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        observed_turn = int(normalized["observed_turn"])
        if observed_turn < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        if normalized["abstain_reasons"] or normalized["rank"] is None:
            return LedgerMergeResult(
                ledger=ledger,
                status="no_change",
                reason="race_result_abstained",
            )

        existing = next(
            (
                item
                for item in ledger.race_results
                if item["race_id"] == normalized["race_id"]
            ),
            None,
        )
        if existing is not None:
            if dict(existing) == normalized:
                return LedgerMergeResult(
                    ledger=ledger,
                    status="no_change",
                    reason="race_result_duplicate",
                )
            raise CareerGoalLedgerError("race_result_conflict")

        goals = list(ledger.goals)
        progressed: list[str] = []
        rank = int(normalized["rank"])
        for index, goal in enumerate(goals):
            if (
                goal.status == "active"
                and (
                    goal.goal_kind == "graded_race_rank_count"
                    or (
                        goal.goal_kind == "named_race_rank"
                        and goal.target_race_id == normalized["race_id"]
                    )
                )
                and goal.grade == normalized["grade"]
                and rank <= goal.required_rank
            ):
                completed_count = min(
                    goal.completed_count + 1,
                    goal.required_count,
                )
                goals[index] = replace(
                    goal,
                    completed_count=completed_count,
                    status=(
                        "completed"
                        if completed_count >= goal.required_count
                        else "active"
                    ),
                    evidence_refs=tuple(
                        dict.fromkeys(
                            (
                                *goal.evidence_refs,
                                str(normalized["observation_sha256"]),
                            )
                        )
                    ),
                )
                progressed.append(goal.goal_instance_id)

        updated = _append_revision(
            ledger,
            observed_turn=observed_turn,
            goals=tuple(sorted(goals, key=lambda item: item.goal_instance_id)),
            race_results=(*ledger.race_results, normalized),
            event_kind="race_result",
            source_sha256=str(normalized["observation_sha256"]),
        )
        return LedgerMergeResult(
            ledger=updated,
            status="updated",
            reason="race_result_recorded",
            progressed_goal_ids=tuple(sorted(progressed)),
        )

    def record_count_race_result(
        self, ledger: CareerGoalLedger, receipt: object, *, calendar: RaceCalendar,
    ) -> LedgerMergeResult:
        """Record one activation-bound result without claiming system completion."""
        from core.auto_trainer.race_result_observation import (
            RaceResultObservationError, validate_count_race_result_receipt,
        )

        self.verify_hash_chain(ledger)
        try:
            validate_count_race_result_receipt(receipt, calendar=calendar)
        except (RaceResultObservationError, OSError) as exc:
            raise CareerGoalLedgerError(f"count_race_result_invalid:{exc}") from exc
        normalized = _normalize_race_result(receipt.observation)
        if normalized["run_session_id"] != ledger.run_session_id:
            raise CareerGoalLedgerError("run_session_mismatch")
        # A genuine exact retry remains idempotent after later observations.
        if any(revision["event_kind"] == "count_race_result"
               and revision["source_sha256"] == receipt.receipt_sha256 for revision in ledger.revisions):
            return LedgerMergeResult(ledger, "no_change", "count_race_result_duplicate")
        if any(item["race_id"] == normalized["race_id"]
               or item["schedule_packet_sha256"] == normalized["schedule_packet_sha256"]
               for item in ledger.race_results):
            raise CareerGoalLedgerError("count_race_result_conflict")
        if normalized["observed_turn"] < ledger.observed_turn:
            raise CareerGoalLedgerError("observed_turn_regression")
        reservation = receipt.schedule["count_reservation"]
        goal_id = reservation["goal_instance_id"]
        active = ledger.active_goals()
        if (len(active) != 1 or active[0].goal_instance_id != goal_id
            or active[0].goal_kind != "graded_race_rank_count"
            or active[0].progress_basis != "verified_lower_bound"
            or active[0].deadline_turn is None):
            raise CareerGoalLedgerError("count_race_result_active_goal_mismatch")
        goal = active[0]
        revision_number = reservation["ledger_revision"]
        if revision_number > ledger.revision:
            raise CareerGoalLedgerError("count_race_result_schedule_history_mismatch")
        revision = ledger.revisions[revision_number - 1]
        view = revision["view"]
        if (_view_head_sha(view, revision["revision_sha256"]) != reservation["ledger_view_sha256"]
            or view["observed_turn"] > reservation["observed_runner_turn"]):
            raise CareerGoalLedgerError("count_race_result_schedule_history_mismatch")
        historical_active = [item for item in view["goals"] if item["status"] == "active"]
        contract_keys = ("goal_instance_id", "goal_kind", "grade", "required_rank", "required_count",
                         "deadline_turn", "progress_basis")
        if (len(historical_active) != 1
            or any(historical_active[0].get(key) != goal.to_dict().get(key) for key in contract_keys)
            or historical_active[0]["completed_count"] > goal.completed_count
            or reservation["grade"] != goal.grade or reservation["deadline_turn"] != goal.deadline_turn):
            raise CareerGoalLedgerError("count_race_result_schedule_goal_mismatch")
        activation = next((item for item in ledger.revisions
                           if any(g["goal_instance_id"] == goal_id for g in item["view"]["goals"])), None)
        activated_goal = next((item for item in activation["view"]["goals"]
                               if item["goal_instance_id"] == goal_id), {}) if activation else {}
        if (activation is None or activation["event_kind"] != "goal_observation"
            or activated_goal.get("progress_basis") != "verified_lower_bound"
            or activated_goal.get("status") != "active" or activated_goal.get("completed_count") != 0
            or activated_goal.get("deadline_turn") is not None
            or any(activated_goal.get(key) != goal.to_dict().get(key)
                   for key in ("goal_kind", "grade", "required_rank", "required_count"))
            or activation["revision"] > revision_number
            or activation["view"]["observed_turn"] > reservation["observed_runner_turn"]
            or _count_goal_instance_id(ledger.run_session_id, grade=goal.grade,
                required_rank=goal.required_rank, required_count=goal.required_count,
                activation_revision=activation["revision"]) != goal_id):
            raise CareerGoalLedgerError("count_race_result_activation_unverified")
        if normalized["abstain_reasons"] or normalized["rank"] is None:
            return LedgerMergeResult(ledger, "no_change", "race_result_abstained")
        progressed = normalized["grade"] == goal.grade and normalized["rank"] <= goal.required_rank
        updated_goal = replace(goal,
            completed_count=min(goal.required_count, goal.completed_count + int(progressed)),
            status="active",
            evidence_refs=tuple(dict.fromkeys((*goal.evidence_refs, receipt.receipt_sha256))))
        updated = _append_revision(ledger, observed_turn=normalized["observed_turn"],
            goals=tuple(updated_goal if item.goal_instance_id == goal_id else item for item in ledger.goals),
            race_results=(*ledger.race_results, normalized), event_kind="count_race_result",
            source_sha256=receipt.receipt_sha256)
        return LedgerMergeResult(updated, "updated", "count_race_result_recorded",
                                 (goal_id,) if progressed else ())

    def write_atomic(self, path: Path, ledger: CareerGoalLedger) -> None:
        self.verify_hash_chain(ledger)
        target = Path(path)
        payload = _encode_ledger(ledger)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="xb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.read_bytes() != payload:
                raise OSError("temporary verification failed")
            os.replace(temporary, target)
            temporary = None
            _fsync_parent_directory(target.parent)
        except OSError as exc:
            raise CareerGoalLedgerError("ledger_atomic_write_failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def verify_hash_chain(self, ledger: CareerGoalLedger) -> None:
        if not isinstance(ledger, CareerGoalLedger):
            raise CareerGoalLedgerError("ledger_required")
        _require_nonempty_string(
            ledger.run_session_id,
            "run_session_id_invalid",
        )
        _require_nonnegative_int(ledger.revision, "ledger_revision_invalid")
        _require_nonnegative_int(
            ledger.observed_turn,
            "ledger_observed_turn_invalid",
        )
        if ledger.schema_version != SCHEMA_VERSION:
            raise CareerGoalLedgerError("ledger_schema_invalid")
        if ledger.revision != len(ledger.revisions):
            raise CareerGoalLedgerError("ledger_history_length_mismatch")
        for goal in ledger.goals:
            _validate_goal(goal)
        for result in ledger.race_results:
            _validate_stored_race_result(result)
            if result["run_session_id"] != ledger.run_session_id:
                raise CareerGoalLedgerError("run_session_mismatch")

        previous_sha = _ZERO_SHA
        previous_turn = 0
        last_view: Mapping[str, object] | None = None
        for expected_revision, raw_revision in enumerate(
            ledger.revisions,
            start=1,
        ):
            if (
                not isinstance(raw_revision, Mapping)
                or frozenset(raw_revision) != _REVISION_KEYS
            ):
                raise CareerGoalLedgerError("ledger_revision_invalid")
            revision = _json_copy(raw_revision)
            if revision["revision"] != expected_revision:
                raise CareerGoalLedgerError("ledger_revision_sequence_invalid")
            if revision["previous_revision_sha256"] != previous_sha:
                raise CareerGoalLedgerError("ledger_previous_hash_mismatch")
            if revision["event_kind"] not in {
                "goal_observation",
                "goal_occurrence_binding",
                "count_deadline_binding",
                "goal_main_menu_recovery",
                "named_goal_completion",
                "count_goal_completion",
                "count_goal_main_menu_completion",
                "race_result",
                "count_race_result",
            }:
                raise CareerGoalLedgerError("ledger_event_kind_invalid")
            _require_sha(revision["source_sha256"], "ledger_source_sha_invalid")
            view = revision["view"]
            _validate_view(view)
            if view["run_session_id"] != ledger.run_session_id:
                raise CareerGoalLedgerError("run_session_mismatch")
            if view["revision"] != expected_revision:
                raise CareerGoalLedgerError("ledger_view_revision_mismatch")
            if view["observed_turn"] < previous_turn:
                raise CareerGoalLedgerError("observed_turn_regression")
            prior_goals = {goal["goal_instance_id"]: goal for goal in last_view["goals"]} if last_view else {}
            for goal in view["goals"]:
                if goal.get("progress_basis") == "verified_lower_bound" and goal["status"] == "completed":
                    prior = prior_goals.get(goal["goal_instance_id"])
                    if prior is None or (prior["status"] != "completed" and (
                        revision["event_kind"] not in {"count_goal_completion", "count_goal_main_menu_completion"}
                        or revision["source_sha256"] not in goal["evidence_refs"]
                        or revision["source_sha256"] in prior["evidence_refs"]
                    )):
                        raise CareerGoalLedgerError("count_goal_completion_history_required")
                    if prior is not None and prior["status"] != "completed" and revision["event_kind"] == "count_goal_main_menu_completion":
                        if (expected_revision < 3 or goal["completed_count"] != goal["required_count"]
                            or view["observed_turn"] != last_view["observed_turn"] + 1
                            or view["race_results"] != last_view["race_results"]
                            or any(goal.get(key) != prior.get(key) for key in prior
                                   if key not in {"completed_count", "status", "evidence_refs"})):
                            raise CareerGoalLedgerError("count_goal_main_menu_completion_history_invalid")
                        _validate_count_main_menu_predecessor(prior, ledger.revisions[expected_revision - 2],
                            ledger.revisions[expected_revision - 3]["view"])
            expected_sha = _canonical_sha(
                {
                    key: revision[key]
                    for key in (
                        "revision",
                        "previous_revision_sha256",
                        "event_kind",
                        "source_sha256",
                        "view",
                    )
                }
            )
            if revision["revision_sha256"] != expected_sha:
                raise CareerGoalLedgerError("ledger_revision_hash_mismatch")
            previous_sha = expected_sha
            previous_turn = int(view["observed_turn"])
            last_view = view

        current_view = _view_dict(
            run_session_id=ledger.run_session_id,
            revision=ledger.revision,
            observed_turn=ledger.observed_turn,
            goals=ledger.goals,
            race_results=ledger.race_results,
        )
        if last_view is not None and _canonical_json(last_view) != _canonical_json(
            current_view
        ):
            raise CareerGoalLedgerError("ledger_current_view_mismatch")
        if last_view is None and ledger.revision != 0:
            raise CareerGoalLedgerError("ledger_history_length_mismatch")
        expected_view_sha = _view_head_sha(current_view, previous_sha)
        if ledger.current_view_sha256 != expected_view_sha:
            raise CareerGoalLedgerError("ledger_current_view_hash_mismatch")


def _append_revision(
    ledger: CareerGoalLedger,
    *,
    observed_turn: int,
    goals: tuple[CareerGoal, ...],
    race_results: tuple[Mapping[str, object], ...],
    event_kind: str,
    source_sha256: str,
) -> CareerGoalLedger:
    _require_sha(source_sha256, "ledger_source_sha_invalid")
    revision_number = ledger.revision + 1
    view = _view_dict(
        run_session_id=ledger.run_session_id,
        revision=revision_number,
        observed_turn=observed_turn,
        goals=goals,
        race_results=race_results,
    )
    previous_sha = (
        str(ledger.revisions[-1]["revision_sha256"])
        if ledger.revisions
        else _ZERO_SHA
    )
    revision_payload = {
        "revision": revision_number,
        "previous_revision_sha256": previous_sha,
        "event_kind": event_kind,
        "source_sha256": source_sha256,
        "view": view,
    }
    revision_sha = _canonical_sha(revision_payload)
    revision_record = {
        **revision_payload,
        "revision_sha256": revision_sha,
    }
    return CareerGoalLedger(
        schema_version=SCHEMA_VERSION,
        run_session_id=ledger.run_session_id,
        revision=revision_number,
        observed_turn=observed_turn,
        goals=goals,
        race_results=tuple(dict(item) for item in race_results),
        revisions=(*ledger.revisions, revision_record),
        current_view_sha256=_view_head_sha(view, revision_sha),
    )


def _count_goal_instance_id(
    run_session_id: str, *, grade: str, required_rank: int,
    required_count: int, activation_revision: int,
) -> str:
    identity = {
        "run_session_id": run_session_id, "goal_kind": "graded_race_rank_count",
        "grade": grade, "required_rank": required_rank, "required_count": required_count,
        "activation_revision": activation_revision,
    }
    return f"goal-{_canonical_sha(identity)[:16]}"


def _goal_instance_id(
    run_session_id: str,
    *,
    goal_kind: str,
    grade: str,
    deadline_turn: int | None,
    target_race_id: str | None = None,
    candidate_race_ids: tuple[str, ...] = (),
) -> str:
    identity = {
        "run_session_id": run_session_id,
        "goal_kind": goal_kind,
        "grade": grade,
        "deadline_turn": deadline_turn,
    }
    if candidate_race_ids:
        identity["candidate_race_ids"] = list(candidate_race_ids)
    elif target_race_id is not None:
        identity["target_race_id"] = target_race_id
    digest = _canonical_sha(identity)
    return f"goal-{digest[:16]}"


def _normalize_race_result(result: object) -> dict[str, object]:
    required = (
        "run_session_id",
        "observed_turn",
        "race_id",
        "grade",
        "rank",
        "source_frame_sha256",
        "schedule_packet_sha256",
        "race_route_session_id",
        "confidence",
        "abstain_reasons",
    )
    try:
        values = {field: getattr(result, field) for field in required}
    except AttributeError as exc:
        raise CareerGoalLedgerError("race_result_invalid") from exc
    for field in (
        "run_session_id",
        "race_id",
        "grade",
        "race_route_session_id",
    ):
        _require_nonempty_string(values[field], "race_result_invalid")
    _require_nonnegative_int(values["observed_turn"], "race_result_invalid")
    rank = values["rank"]
    if rank is not None and (
        type(rank) is not int or rank <= 0
    ):
        raise CareerGoalLedgerError("race_result_invalid")
    for field in ("source_frame_sha256", "schedule_packet_sha256"):
        _require_sha(values[field], "race_result_sha_invalid")
    confidence = values["confidence"]
    if (
        type(confidence) not in {int, float}
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise CareerGoalLedgerError("race_result_invalid")
    abstain = values["abstain_reasons"]
    if (
        type(abstain) is not tuple
        or any(type(item) is not str or not item for item in abstain)
        or len(set(abstain)) != len(abstain)
    ):
        raise CareerGoalLedgerError("race_result_invalid")
    canonical = {
        **values,
        "confidence": float(confidence),
        "abstain_reasons": list(abstain),
    }
    canonical["observation_sha256"] = _canonical_sha(canonical)
    return canonical


def _validate_observation(observation: CareerGoalObservation) -> None:
    if not isinstance(observation, CareerGoalObservation):
        raise CareerGoalLedgerError("goal_observation_invalid")
    payload = career_goal_observation_to_dict(observation)
    expected_sha = payload.pop("observation_sha256")
    payload.pop("is_complete")
    if _canonical_sha(payload) != expected_sha:
        raise CareerGoalLedgerError("goal_observation_sha_mismatch")
    _require_sha(
        observation.source_frame_sha256,
        "goal_observation_source_sha_invalid",
    )
    if observation.progress_basis is not None:
        binding = observation.field_evidence.get("current_goal_binding", {})
        if (observation.progress_basis != "verified_lower_bound"
            or observation.goal_kind != "graded_race_rank_count"
            or type(observation.completed_count) is not int or observation.completed_count != 0
            or observation.cutoff_turn is not None or observation.target_race_id is not None
            or observation.candidate_race_ids or binding.get("verified") is not True
            or binding.get("source_frame_sha256") != observation.source_frame_sha256):
            raise CareerGoalLedgerError("count_goal_observation_invalid")
        _require_positive_int(observation.required_rank, "count_goal_observation_invalid")
        _require_positive_int(observation.required_count, "count_goal_observation_invalid")
        _require_nonnegative_int(observation.remaining_turns, "count_goal_observation_invalid")
    if observation.goal_kind == "named_race_rank" and observation.is_content_complete:
        if observation.completed_count != 0 or observation.cutoff_turn is not None:
            raise CareerGoalLedgerError("named_goal_observation_invalid")
    _validate_candidate_ids(observation.candidate_race_ids)
    if observation.candidate_race_ids and (
        observation.goal_kind != "named_race_rank"
        or observation.target_race_id is not None
        and observation.target_race_id not in observation.candidate_race_ids
    ):
        raise CareerGoalLedgerError("named_goal_observation_invalid")


def _validate_candidate_ids(candidates: tuple[str, ...]) -> None:
    if (type(candidates) is not tuple
        or any(type(item) is not str or not item for item in candidates)
        or len(set(candidates)) != len(candidates)):
        raise CareerGoalLedgerError("named_goal_candidates_invalid")


def _validate_goal(goal: CareerGoal) -> None:
    if not isinstance(goal, CareerGoal):
        raise CareerGoalLedgerError("ledger_goal_invalid")
    _require_nonempty_string(goal.goal_instance_id, "ledger_goal_invalid")
    _require_nonempty_string(goal.goal_kind, "ledger_goal_invalid")
    _require_nonempty_string(goal.grade, "ledger_goal_invalid")
    _require_positive_int(goal.required_rank, "ledger_goal_invalid")
    _require_positive_int(goal.required_count, "ledger_goal_invalid")
    _require_nonnegative_int(goal.completed_count, "ledger_goal_invalid")
    _validate_candidate_ids(goal.candidate_race_ids)
    if goal.progress_basis is not None:
        if (goal.progress_basis != "verified_lower_bound"
            or goal.goal_kind != "graded_race_rank_count"
            or goal.target_race_id is not None or goal.candidate_race_ids):
            raise CareerGoalLedgerError("ledger_goal_invalid")
        _require_nonnegative_int(goal.remaining_turns, "ledger_goal_invalid")
        if goal.deadline_turn is not None:
            _require_positive_int(goal.deadline_turn, "ledger_goal_invalid")
    elif goal.goal_kind == "named_race_rank":
        if not goal.candidate_race_ids or goal.target_race_id is not None:
            _require_nonempty_string(goal.target_race_id, "ledger_goal_invalid")
        if goal.candidate_race_ids and (
            goal.target_race_id is not None and goal.target_race_id not in goal.candidate_race_ids
            or goal.target_race_id is None and goal.completed_count != 0
        ):
            raise CareerGoalLedgerError("ledger_goal_invalid")
        _require_nonnegative_int(goal.remaining_turns, "ledger_goal_invalid")
        if goal.deadline_turn is not None or goal.required_count != 1:
            raise CareerGoalLedgerError("ledger_goal_invalid")
    else:
        _require_positive_int(goal.deadline_turn, "ledger_goal_invalid")
        if goal.target_race_id is not None or goal.remaining_turns is not None or goal.candidate_race_ids:
            raise CareerGoalLedgerError("ledger_goal_invalid")
    if goal.status not in {"active", "completed"}:
        raise CareerGoalLedgerError("ledger_goal_invalid")
    expected_status = (
        "completed"
        if goal.progress_basis is None and goal.completed_count >= goal.required_count
        else "active"
    )
    verified_count_completion = (goal.progress_basis == "verified_lower_bound"
        and goal.status == "completed" and goal.deadline_turn is not None
        and goal.completed_count >= goal.required_count)
    if goal.status != expected_status and not verified_count_completion:
        raise CareerGoalLedgerError("ledger_goal_status_invalid")
    _require_sha(goal.source_packet_sha256, "ledger_goal_invalid")
    if (
        type(goal.evidence_refs) is not tuple
        or not goal.evidence_refs
        or any(_SHA256_RE.fullmatch(item) is None for item in goal.evidence_refs)
        or len(set(goal.evidence_refs)) != len(goal.evidence_refs)
    ):
        raise CareerGoalLedgerError("ledger_goal_invalid")


def _validate_stored_race_result(result: Mapping[str, object]) -> None:
    if not isinstance(result, Mapping) or frozenset(result) != _RACE_RESULT_KEYS:
        raise CareerGoalLedgerError("ledger_race_result_invalid")
    copy = dict(result)
    observation_sha = copy.pop("observation_sha256")
    _require_sha(observation_sha, "ledger_race_result_invalid")
    if _canonical_sha(copy) != observation_sha:
        raise CareerGoalLedgerError("ledger_race_result_hash_mismatch")
    for field in (
        "run_session_id",
        "race_id",
        "grade",
        "race_route_session_id",
    ):
        _require_nonempty_string(copy[field], "ledger_race_result_invalid")
    _require_nonnegative_int(
        copy["observed_turn"],
        "ledger_race_result_invalid",
    )
    _require_positive_int(copy["rank"], "ledger_race_result_invalid")
    for field in ("source_frame_sha256", "schedule_packet_sha256"):
        _require_sha(copy[field], "ledger_race_result_invalid")
    confidence = copy["confidence"]
    if (
        type(confidence) not in {int, float}
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise CareerGoalLedgerError("ledger_race_result_invalid")
    normalized_abstain = copy["abstain_reasons"]
    if type(normalized_abstain) is not list or normalized_abstain:
        raise CareerGoalLedgerError("ledger_race_result_invalid")


def _view_dict(
    *,
    run_session_id: str,
    revision: int,
    observed_turn: int,
    goals: tuple[CareerGoal, ...],
    race_results: tuple[Mapping[str, object], ...],
) -> dict[str, object]:
    return {
        "run_session_id": run_session_id,
        "revision": revision,
        "observed_turn": observed_turn,
        "goals": [goal.to_dict() for goal in goals],
        "race_results": [dict(result) for result in race_results],
    }


def _validate_view(view: object) -> None:
    if not isinstance(view, Mapping) or frozenset(view) != _VIEW_KEYS:
        raise CareerGoalLedgerError("ledger_view_invalid")
    _require_nonempty_string(view["run_session_id"], "ledger_view_invalid")
    _require_positive_int(view["revision"], "ledger_view_invalid")
    _require_nonnegative_int(view["observed_turn"], "ledger_view_invalid")
    if type(view["goals"]) is not list or type(view["race_results"]) is not list:
        raise CareerGoalLedgerError("ledger_view_invalid")
    for raw_goal in view["goals"]:
        _goal_from_dict(raw_goal)
    for result in view["race_results"]:
        _validate_stored_race_result(result)


def _ledger_from_payload(payload: object) -> CareerGoalLedger:
    if not isinstance(payload, Mapping) or frozenset(payload) != _TOP_LEVEL_KEYS:
        raise CareerGoalLedgerError("ledger_payload_invalid")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise CareerGoalLedgerError("ledger_schema_invalid")
    if type(payload["goals"]) is not list or type(payload["race_results"]) is not list:
        raise CareerGoalLedgerError("ledger_payload_invalid")
    if type(payload["revisions"]) is not list:
        raise CareerGoalLedgerError("ledger_payload_invalid")
    goals = tuple(_goal_from_dict(item) for item in payload["goals"])
    if any(not isinstance(item, Mapping) for item in payload["race_results"]):
        raise CareerGoalLedgerError("ledger_payload_invalid")
    if any(not isinstance(item, Mapping) for item in payload["revisions"]):
        raise CareerGoalLedgerError("ledger_payload_invalid")
    race_results = tuple(dict(item) for item in payload["race_results"])
    revisions = tuple(_json_copy(item) for item in payload["revisions"])
    ledger = CareerGoalLedger(
        schema_version=str(payload["schema_version"]),
        run_session_id=payload["run_session_id"],
        revision=payload["revision"],
        observed_turn=payload["observed_turn"],
        goals=goals,
        race_results=race_results,
        revisions=revisions,
        current_view_sha256=payload["current_view_sha256"],
    )
    return ledger


def _goal_from_dict(value: object) -> CareerGoal:
    if not isinstance(value, Mapping):
        raise CareerGoalLedgerError("ledger_goal_invalid")
    keys = (
        _GOAL_KEYS | {"target_race_id", "remaining_turns"}
        if value.get("goal_kind") == "named_race_rank"
        else _GOAL_KEYS
    )
    if "progress_basis" in value:
        if value["progress_basis"] != "verified_lower_bound":
            raise CareerGoalLedgerError("ledger_goal_invalid")
        keys = keys | {"progress_basis", "remaining_turns"}
    if "candidate_race_ids" in value:
        keys = keys | {"candidate_race_ids"}
        if type(value["candidate_race_ids"]) is not list or not value["candidate_race_ids"]:
            raise CareerGoalLedgerError("ledger_goal_invalid")
    if frozenset(value) != keys:
        raise CareerGoalLedgerError("ledger_goal_invalid")
    refs = value["evidence_refs"]
    if type(refs) is not list:
        raise CareerGoalLedgerError("ledger_goal_invalid")
    goal = CareerGoal(
        goal_instance_id=value["goal_instance_id"],
        goal_kind=value["goal_kind"],
        grade=value["grade"],
        required_rank=value["required_rank"],
        required_count=value["required_count"],
        completed_count=value["completed_count"],
        deadline_turn=value["deadline_turn"],
        status=value["status"],
        source_packet_sha256=value["source_packet_sha256"],
        evidence_refs=tuple(refs),
        target_race_id=value.get("target_race_id"),
        remaining_turns=value.get("remaining_turns"),
        candidate_race_ids=tuple(value.get("candidate_race_ids", ())),
        progress_basis=value.get("progress_basis"),
    )
    _validate_goal(goal)
    return goal


def _encode_ledger(ledger: CareerGoalLedger) -> bytes:
    return (_canonical_json(ledger.to_dict()) + "\n").encode("utf-8")


def _view_head_sha(view: Mapping[str, object], head_sha: str) -> str:
    return _canonical_sha(
        {
            "view": view,
            "head_revision_sha256": head_sha,
        }
    )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CareerGoalLedgerError("ledger_noncanonical_value") from exc


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value: object) -> Any:
    return json.loads(_canonical_json(value))


def _require_sha(value: object, reason: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise CareerGoalLedgerError(reason)


def _require_nonempty_string(value: object, reason: str) -> None:
    if type(value) is not str or not value:
        raise CareerGoalLedgerError(reason)


def _require_nonnegative_int(value: object, reason: str) -> None:
    if type(value) is not int or value < 0:
        raise CareerGoalLedgerError(reason)


def _require_positive_int(value: object, reason: str) -> None:
    if type(value) is not int or value <= 0:
        raise CareerGoalLedgerError(reason)


def _reject_duplicate_members(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {value}")


def _fsync_parent_directory(parent: Path) -> None:
    try:
        descriptor = os.open(parent, os.O_RDONLY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
