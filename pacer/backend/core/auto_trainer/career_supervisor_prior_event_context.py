"""Read-only prior-action context for same-turn event disambiguation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle


PRIOR_CONTEXT_SCHEMA = "uma.prior_turn_event_context.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRAINING_TYPES = frozenset({"speed", "stamina", "power", "guts", "wisdom"})
_ACTIONS = frozenset(
    {
        "train",
        "rest",
        "outing",
        "clinic",
        "race",
        "scenario",
    }
)
_EVENT_DIMENSIONS = frozenset(
    {
        "calendar",
        "story_context",
        "race_result",
        "training_result",
        "extra_training_offer",
        "previous_training",
    }
)
_UNKNOWN_DIMENSIONS = _EVENT_DIMENSIONS | {"game_period"}


class PriorTurnEventContextError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PriorTurnEventContextAuthority:
    schema_version: str
    run_session_id: str
    global_action_turn: int
    game_period: str
    previous_action: str
    previous_training: str | None
    state_context_snapshot_sha256: str
    decision_sha256: str
    context_tokens: tuple[str, ...]
    unknown_dimensions: tuple[str, ...]
    authority_sha256: str
    action_eligible: bool = False
    usable_for_action: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != PRIOR_CONTEXT_SCHEMA:
            raise PriorTurnEventContextError("prior_context_schema_invalid")
        if not isinstance(self.run_session_id, str) or not self.run_session_id:
            raise PriorTurnEventContextError("prior_context_run_session_invalid")
        if type(self.global_action_turn) is not int or self.global_action_turn < 0:
            raise PriorTurnEventContextError("prior_context_turn_invalid")
        if not isinstance(self.game_period, str) or not self.game_period:
            raise PriorTurnEventContextError("prior_context_game_period_invalid")
        if self.previous_action not in _ACTIONS:
            raise PriorTurnEventContextError("prior_context_action_invalid")
        if self.previous_action == "train":
            if self.previous_training not in _TRAINING_TYPES:
                raise PriorTurnEventContextError(
                    "prior_context_training_target_invalid"
                )
            expected_tokens = (f"previous_training:{self.previous_training}",)
        else:
            if self.previous_training is not None:
                raise PriorTurnEventContextError(
                    "prior_context_training_target_invalid"
                )
            expected_tokens = ("previous_training:none",)
        _require_sha(
            self.state_context_snapshot_sha256,
            "prior_context_snapshot_sha_invalid",
        )
        _require_sha(self.decision_sha256, "prior_context_decision_sha_invalid")
        if self.context_tokens != expected_tokens:
            raise PriorTurnEventContextError("prior_context_tokens_invalid")
        if (
            not isinstance(self.unknown_dimensions, tuple)
            or tuple(sorted(self.unknown_dimensions)) != self.unknown_dimensions
            or len(set(self.unknown_dimensions)) != len(self.unknown_dimensions)
            or any(
                item not in _UNKNOWN_DIMENSIONS for item in self.unknown_dimensions
            )
        ):
            raise PriorTurnEventContextError(
                "prior_context_unknown_dimensions_invalid"
            )
        if "previous_training" in self.unknown_dimensions:
            raise PriorTurnEventContextError("prior_context_known_unknown_conflict")
        if (self.game_period == "unknown") != (
            "game_period" in self.unknown_dimensions
        ):
            raise PriorTurnEventContextError(
                "prior_context_game_period_unknown_contract_invalid"
            )
        if self.action_eligible is not False or self.usable_for_action is not False:
            raise PriorTurnEventContextError("prior_context_must_be_read_only")
        _require_sha(self.authority_sha256, "prior_context_authority_sha_invalid")
        material = self.to_dict()
        material.pop("authority_sha256")
        if _canonical_sha(material) != self.authority_sha256:
            raise PriorTurnEventContextError(
                "prior_context_authority_sha_mismatch"
            )

    def matches_turn_authority(self, authority: TurnAuthorityBundle) -> bool:
        return isinstance(authority, TurnAuthorityBundle) and (
            self.run_session_id == authority.run_session_id
            and self.global_action_turn == authority.global_action_turn
        )

    def event_projection(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return (
            self.context_tokens,
            tuple(
                item
                for item in self.unknown_dimensions
                if item in _EVENT_DIMENSIONS
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_session_id": self.run_session_id,
            "global_action_turn": self.global_action_turn,
            "game_period": self.game_period,
            "previous_action": self.previous_action,
            "previous_training": self.previous_training,
            "state_context_snapshot_sha256": self.state_context_snapshot_sha256,
            "decision_sha256": self.decision_sha256,
            "context_tokens": list(self.context_tokens),
            "unknown_dimensions": list(self.unknown_dimensions),
            "authority_sha256": self.authority_sha256,
            "action_eligible": False,
            "usable_for_action": False,
        }


def build_prior_turn_event_context_authority(
    *,
    run_session_id: str,
    global_action_turn: int,
    turn_root: str | Path,
    one_turn_report: Mapping[str, object],
) -> PriorTurnEventContextAuthority:
    if not isinstance(one_turn_report, Mapping):
        raise PriorTurnEventContextError("prior_context_report_invalid")
    if (
        one_turn_report.get("is_valid") is not True
        or one_turn_report.get("status") != "completed"
    ):
        raise PriorTurnEventContextError("prior_context_report_incomplete")
    snapshot_path, snapshot_sha = _validated_snapshot(
        turn_root=Path(turn_root),
        report=one_turn_report,
    )
    try:
        state_context = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PriorTurnEventContextError(
            "prior_context_snapshot_unreadable"
        ) from exc
    if not isinstance(state_context, Mapping):
        raise PriorTurnEventContextError("prior_context_snapshot_invalid")
    if state_context.get("turn") != global_action_turn:
        raise PriorTurnEventContextError("prior_context_turn_mismatch")

    stages = one_turn_report.get("stages")
    live_loop = stages.get("live_loop") if isinstance(stages, Mapping) else None
    decision = live_loop.get("decision") if isinstance(live_loop, Mapping) else None
    execution = live_loop.get("execution") if isinstance(live_loop, Mapping) else None
    if not isinstance(decision, Mapping) or not isinstance(execution, Mapping):
        raise PriorTurnEventContextError("prior_context_action_evidence_missing")
    if execution.get("performed") is not True:
        raise PriorTurnEventContextError("prior_context_action_not_performed")
    action = decision.get("action")
    if action not in _ACTIONS:
        raise PriorTurnEventContextError("prior_context_action_invalid")
    previous_training: str | None = None
    if action == "train":
        target = decision.get("target")
        if target not in _TRAINING_TYPES:
            raise PriorTurnEventContextError(
                "prior_context_training_target_invalid"
            )
        previous_training = str(target)
        context_tokens = (f"previous_training:{previous_training}",)
    else:
        context_tokens = ("previous_training:none",)
    game_period = state_context.get("game_calendar")
    if not isinstance(game_period, str) or not game_period.strip():
        game_period = "unknown"
    else:
        game_period = game_period.strip()
    unknown = {
        "calendar",
        "extra_training_offer",
        "race_result",
        "story_context",
        "training_result",
    }
    if game_period == "unknown":
        unknown.add("game_period")
    decision_sha = _canonical_sha(decision)
    payload: dict[str, object] = {
        "schema_version": PRIOR_CONTEXT_SCHEMA,
        "run_session_id": run_session_id,
        "global_action_turn": global_action_turn,
        "game_period": game_period,
        "previous_action": action,
        "previous_training": previous_training,
        "state_context_snapshot_sha256": snapshot_sha,
        "decision_sha256": decision_sha,
        "context_tokens": list(context_tokens),
        "unknown_dimensions": sorted(unknown),
        "action_eligible": False,
        "usable_for_action": False,
    }
    payload["authority_sha256"] = _canonical_sha(payload)
    return PriorTurnEventContextAuthority(
        schema_version=PRIOR_CONTEXT_SCHEMA,
        run_session_id=run_session_id,
        global_action_turn=global_action_turn,
        game_period=game_period,
        previous_action=str(action),
        previous_training=previous_training,
        state_context_snapshot_sha256=snapshot_sha,
        decision_sha256=decision_sha,
        context_tokens=context_tokens,
        unknown_dimensions=tuple(sorted(unknown)),
        authority_sha256=str(payload["authority_sha256"]),
    )


def _validated_snapshot(
    *,
    turn_root: Path,
    report: Mapping[str, object],
) -> tuple[Path, str]:
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise PriorTurnEventContextError("prior_context_artifacts_missing")
    raw_path = artifacts.get("state_context_snapshot_path")
    raw_sha = artifacts.get("state_context_snapshot_sha256")
    expected_sha = _require_sha(raw_sha, "prior_context_snapshot_sha_invalid")
    if not isinstance(raw_path, str) or not raw_path:
        raise PriorTurnEventContextError("prior_context_snapshot_path_invalid")
    try:
        root = turn_root.resolve(strict=True)
        snapshot = Path(raw_path).resolve(strict=True)
    except OSError as exc:
        raise PriorTurnEventContextError(
            "prior_context_snapshot_unreadable"
        ) from exc
    if snapshot == root or root not in snapshot.parents:
        raise PriorTurnEventContextError("prior_context_snapshot_path_invalid")
    try:
        actual_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    except OSError as exc:
        raise PriorTurnEventContextError(
            "prior_context_snapshot_unreadable"
        ) from exc
    if actual_sha != expected_sha:
        raise PriorTurnEventContextError("prior_context_snapshot_sha_mismatch")
    return snapshot, expected_sha


def _require_sha(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PriorTurnEventContextError(code)
    return value


def _canonical_sha(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "PRIOR_CONTEXT_SCHEMA",
    "PriorTurnEventContextAuthority",
    "PriorTurnEventContextError",
    "build_prior_turn_event_context_authority",
]
