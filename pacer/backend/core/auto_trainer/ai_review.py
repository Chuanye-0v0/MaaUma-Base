from __future__ import annotations

from core.auto_trainer.rejection_policy import is_hard_rejected
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol

from core.auto_trainer.decision import parse_ai_review
from core.auto_trainer.state import TrainingTurnState


_UNSAFE_ADVISOR_KEYS = {
    "absolute_path",
    "bbox",
    "capture",
    "raw_candidate",
    "raw_data",
    "relative_path",
    "roi",
    "source_crop",
    "x",
    "y",
}
_UNSAFE_ADVISOR_KEY_TERMS = (
    "adb",
    "click",
    "coordinate",
    "image",
    "screenshot",
    "tap",
)
_IMAGE_EXTENSION_RE = re.compile(r"\.(?:png|jpe?g)(?:$|[^a-z0-9])", re.IGNORECASE)
_DROP = object()


def _safe_advisor_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return (
        lowered not in _UNSAFE_ADVISOR_KEYS
        and not lowered.endswith("_path")
        and not any(term in lowered for term in _UNSAFE_ADVISOR_KEY_TERMS)
    )


def _safe_advisor_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    if (
        lowered in _UNSAFE_ADVISOR_KEYS
        or lowered.endswith("_path")
        or "\\" in value
        or "/" in value
        or _IMAGE_EXTENSION_RE.search(value)
        or any(term in lowered for term in _UNSAFE_ADVISOR_KEY_TERMS)
    ):
        return None
    return value


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _numeric_summary(value: Any) -> Any:
    number = _finite_number(value)
    if number is not None:
        return number
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not _safe_advisor_key(key):
                continue
            summary = _numeric_summary(item)
            if summary is not _DROP:
                result[key] = summary
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            summary = _numeric_summary(item)
            if summary is not _DROP:
                result.append(summary)
        return result
    return _DROP


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := _safe_advisor_text(item)) is not None]


def _safe_semantic_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    number = _finite_number(value)
    if number is not None:
        return number
    text = _safe_advisor_text(value)
    if text is not None:
        return text
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not _safe_advisor_key(key):
                continue
            semantic_value = _safe_semantic_value(item)
            if semantic_value is not _DROP:
                result[key] = semantic_value
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            semantic_value = _safe_semantic_value(item)
            if semantic_value is not _DROP:
                result.append(semantic_value)
        return result
    return _DROP


def _option_value(data: dict[str, Any], key: str) -> Any:
    value = data.get(key)
    if value is not None:
        return value
    raw_data = data.get("raw_data")
    if isinstance(raw_data, dict):
        return raw_data.get(key)
    return None


def _support_dtos(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    allowed_text = ("name", "support_name", "card_id", "bond_status")
    allowed_values = ("bond", "bond_value", "bond_level", "friendship")
    supports = []
    for item in value:
        if not isinstance(item, dict):
            continue
        support = {}
        for key in allowed_text:
            text = _safe_advisor_text(item.get(key))
            if text is not None:
                support[key] = text
        for key in allowed_values:
            semantic_value = _safe_semantic_value(item.get(key))
            if semantic_value is not _DROP and semantic_value is not None:
                support[key] = semantic_value
        if support:
            supports.append(support)
    return supports


def _fragment_dtos(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    fragments = []
    for item in value:
        if not isinstance(item, dict):
            continue
        fragment = {}
        for key in ("color", "kind", "stat", "state"):
            text = _safe_advisor_text(item.get(key))
            if text is not None:
                fragment[key] = text
        for key in ("count", "confidence"):
            number = _finite_number(item.get(key))
            if number is not None:
                fragment[key] = number
        if fragment:
            fragments.append(fragment)
    return fragments


def _training_option_dto(option: Any) -> dict[str, Any]:
    data = option.to_dict()
    dto: dict[str, Any] = {}

    training_type = _safe_advisor_text(_option_value(data, "training_type"))
    if training_type is not None:
        dto["training_type"] = training_type

    for key in ("stats_gain", "stats_total", "stats_breakdown"):
        summary = _numeric_summary(_option_value(data, key))
        if summary is not _DROP:
            dto[key] = summary

    for key in (
        "failure_rate",
        "effective_count",
        "visible_count",
        "support_count",
        "unresolved_count",
        "effective_support_count",
        "visible_support_count",
        "unresolved_support_count",
        "hint_count",
        "fragment_count",
        "fragment_confidence",
        "vitality_delta",
        "confidence",
    ):
        number = _finite_number(_option_value(data, key))
        if number is not None:
            dto[key] = number

    friendship = _safe_semantic_value(_option_value(data, "friendship"))
    if friendship is not _DROP and friendship is not None:
        dto["friendship"] = friendship

    for key in ("fragment_color", "fragment_kind", "fragment_stat", "fragment_state"):
        text = _safe_advisor_text(_option_value(data, key))
        if text is not None:
            dto[key] = text

    fragments = _fragment_dtos(_option_value(data, "fragments"))
    if fragments:
        dto["fragments"] = fragments

    unknown_fields = _safe_string_list(_option_value(data, "unknown_fields"))
    if unknown_fields:
        dto["unknown_fields"] = unknown_fields

    for source_key in ("supports", "support_identities", "support_cards"):
        supports = _support_dtos(_option_value(data, source_key))
        if supports:
            dto["supports"] = supports
            break

    return dto


def _candidate_dto(candidate: dict[str, Any]) -> dict[str, Any]:
    dto: dict[str, Any] = {}
    for key in ("action_id", "type"):
        text = _safe_advisor_text(candidate.get(key))
        if text is not None:
            dto[key] = text
    for key in ("score", "source_confidence"):
        number = _finite_number(candidate.get(key))
        if number is not None:
            dto[key] = number
    components = _numeric_summary(candidate.get("components"))
    if components is not _DROP:
        dto["components"] = components
    risk_flags = _safe_string_list(candidate.get("risk_flags"))
    if risk_flags:
        dto["risk_flags"] = risk_flags
    for key in ("rejected", "hard_rejected"):
        if isinstance(candidate.get(key), bool):
            dto[key] = candidate[key]
    return dto


def _deterministic_decision_dto(decision: dict[str, Any]) -> dict[str, Any]:
    dto: dict[str, Any] = {}
    for key in ("action", "target", "reason"):
        text = _safe_advisor_text(decision.get(key))
        if text is not None:
            dto[key] = text
    confidence = _finite_number(decision.get("confidence"))
    if confidence is not None:
        dto["confidence"] = confidence
    risk_flags = _safe_string_list(decision.get("risk_flags"))
    if risk_flags:
        dto["risk_flags"] = risk_flags
    candidates = decision.get("scored_candidates", [])
    if isinstance(candidates, list):
        dto["scored_candidates"] = [
            _candidate_dto(item) for item in candidates if isinstance(item, dict)
        ]
    return dto


_GRAND_MASTERS_NESTED_TEXT_FIELDS = (
    "state",
    "color",
    "kind",
    "stat",
    "effect",
)
_GRAND_MASTERS_NESTED_NUMBER_FIELDS = (
    "count",
    "confidence",
    "remaining",
)


def _grand_masters_semantic_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    number = _finite_number(value)
    if number is not None:
        return number
    text = _safe_advisor_text(value)
    if text is not None:
        return text
    return _DROP


def _grand_masters_semantic_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    dto: dict[str, Any] = {}
    semantic_value = _grand_masters_semantic_scalar(value.get("value"))
    if semantic_value is not _DROP:
        dto["value"] = semantic_value
    for key in _GRAND_MASTERS_NESTED_TEXT_FIELDS:
        text = _safe_advisor_text(value.get(key))
        if text is not None:
            dto[key] = text
    for key in _GRAND_MASTERS_NESTED_NUMBER_FIELDS:
        number = _finite_number(value.get(key))
        if number is not None:
            dto[key] = number
    if isinstance(value.get("active"), bool):
        dto["active"] = value["active"]
    return dto


def _grand_masters_semantic_collection(
    value: Any,
    *,
    allow_scalar_items: bool,
) -> Any:
    if isinstance(value, dict):
        return _grand_masters_semantic_record(value)
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            if isinstance(item, dict):
                projected = _grand_masters_semantic_record(item)
            elif allow_scalar_items:
                projected = _grand_masters_semantic_scalar(item)
            else:
                projected = _DROP
            if projected is not _DROP and projected != {}:
                items.append(projected)
        return items
    if allow_scalar_items:
        return _grand_masters_semantic_scalar(value)
    return _DROP


def _grand_masters_scenario_state_dto(
    scenario_state: dict[str, Any],
) -> dict[str, Any]:
    scenario = _safe_advisor_text(scenario_state.get("scenario"))
    if scenario not in (None, "grand_masters"):
        return {}

    dto: dict[str, Any] = {}
    for key in ("scenario", "mood", "period_text", "fragment_state"):
        text = _safe_advisor_text(scenario_state.get(key))
        if text is not None:
            dto[key] = text

    fragment_count = _finite_number(scenario_state.get("fragment_count"))
    if fragment_count is not None:
        dto["fragment_count"] = fragment_count

    for key in ("fragments", "knowledge", "knowledge_fragments", "wisdom"):
        projected = _grand_masters_semantic_collection(
            scenario_state.get(key),
            allow_scalar_items=key in ("knowledge", "knowledge_fragments"),
        )
        if projected is not _DROP and projected not in ({}, []):
            dto[key] = projected

    if isinstance(scenario_state.get("goddess_wisdom_ready"), bool):
        dto["goddess_wisdom_ready"] = scenario_state["goddess_wisdom_ready"]

    goddess_wisdom = _grand_masters_semantic_collection(
        scenario_state.get("goddess_wisdom"),
        allow_scalar_items=False,
    )
    if goddess_wisdom is not _DROP and goddess_wisdom not in ({}, []):
        dto["goddess_wisdom"] = goddess_wisdom

    return dto


class AITrainingReviewClient(Protocol):
    def review_training(self, prompt: dict[str, Any]) -> str:
        """Return strict JSON selecting one candidate action."""


class CommandAIReviewClient:
    """Ask an external local command to review the candidate-bound prompt."""

    def __init__(
        self,
        command: str,
        *,
        timeout_seconds: int = 60,
        cwd: str | Path | None = None,
    ) -> None:
        if not command.strip():
            raise ValueError("ai_review_command_required")
        self.command = command
        self.timeout_seconds = int(timeout_seconds)
        self.cwd = Path(cwd) if cwd is not None else None

    def review_training(self, prompt: dict[str, Any]) -> str:
        completed = subprocess.run(
            self.command,
            input=json.dumps(prompt, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            shell=True,
            timeout=self.timeout_seconds,
            cwd=str(self.cwd) if self.cwd is not None else None,
        )
        stdout = completed.stdout.strip()
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise RuntimeError(
                f"ai_review_command_failed:{completed.returncode}:{stderr[:500]}"
            )
        if not stdout:
            raise RuntimeError("ai_review_command_empty_stdout")
        return stdout


def build_training_review_prompt(
    state: TrainingTurnState,
    deterministic_decision: dict[str, Any],
) -> dict[str, Any]:
    candidates = [
        item
        for item in deterministic_decision.get("scored_candidates", [])
        if (
            isinstance(item, dict)
            and isinstance(item.get("action_id"), str)
            and _safe_advisor_text(item["action_id"]) is not None
            and item.get("eligible", True)
            and item.get("allowed") is not False
            and not is_hard_rejected(item)
        )
    ]
    allowed_action_ids = sorted(item["action_id"] for item in candidates)
    packet = {
        "schema_version": "ug_parent_training_ai_review_prompt.v1",
        "task": "select_training_action",
        "instructions": [
            "Do not invent actions; selected_action_id must be one of allowed_action_ids.",
            "Treat scenario_state as plugin data and avoid single-horse schedule assumptions.",
            "Return strict JSON only.",
        ],
        "allowed_action_ids": allowed_action_ids,
        "state": {
            "screen": _safe_advisor_text(state.screen) or "",
            "turn": _finite_number(state.turn),
            "vitality": _finite_number(state.vitality),
            "current_stats": _numeric_summary(dict(state.current_stats)),
            "scenario_state": _grand_masters_scenario_state_dto(
                dict(state.scenario_state)
            ),
            "warnings": _safe_string_list(state.warnings),
            "errors": _safe_string_list(state.errors),
        },
        "training_options": [
            _training_option_dto(item) for item in state.training_options
        ],
        "deterministic_decision": _deterministic_decision_dto(
            deterministic_decision
        ),
        "required_response_schema": {
            "decision_packet_sha256": "64 lowercase hexadecimal characters matching the prompt",
            "selected_action_id": "string from allowed_action_ids",
            "confidence": "number from 0.0 to 1.0",
            "reason": "non-empty string",
            "risk_flags": "list of strings",
            "veto": "boolean",
        },
    }
    canonical_packet = json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    packet["decision_packet_sha256"] = hashlib.sha256(
        canonical_packet.encode("utf-8")
    ).hexdigest()
    return packet


def review_training_decision(
    *,
    state: TrainingTurnState,
    deterministic_decision: dict[str, Any],
    client: AITrainingReviewClient,
) -> dict[str, Any]:
    prompt = build_training_review_prompt(state, deterministic_decision)
    try:
        raw_response = client.review_training(prompt)
    except Exception as exc:
        raw_response = None
        review = {
            "valid": False,
            "errors": [f"ai_review_client_error:{type(exc).__name__}"],
            "selected_action_id": None,
            "confidence": 0.0,
            "reason": "",
            "risk_flags": [],
            "veto": False,
        }
    else:
        review = parse_ai_review(
            raw_response,
            set(prompt["allowed_action_ids"]),
            prompt["decision_packet_sha256"],
        )
    review["applied"] = False
    review["raw_response"] = raw_response
    review["prompt_schema_version"] = prompt["schema_version"]
    review["decision_packet_sha256"] = prompt["decision_packet_sha256"]
    review["decision_packet"] = prompt
    return review
