from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.dashboard import _summarize_support_right_lane_shadow


TRAINING_ORDER = ("speed", "stamina", "power", "guts", "wisdom")
STAT_KEYS = (*TRAINING_ORDER, "skill_pt")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def build_training_evidence(
    packet: Mapping[str, Any],
    *,
    turn_packet_sha256: str,
    workspace_root: str | Path,
    archived_image_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(turn_packet_sha256, str) or _SHA256_RE.fullmatch(turn_packet_sha256) is None:
        return _conflict("turn_packet_sha256_invalid", str(turn_packet_sha256))
    capture = packet.get("capture") if isinstance(packet, Mapping) else None
    options = capture.get("options") if isinstance(capture, Mapping) else None
    if not isinstance(options, list):
        return _conflict("training_options_missing", turn_packet_sha256)
    if archived_image_paths is not None and set(archived_image_paths) != set(TRAINING_ORDER):
        return _conflict("training_archive_invalid", turn_packet_sha256)

    root = Path(workspace_root).resolve()
    by_type: dict[str, dict[str, Any]] = {}
    for option in options:
        training_type = option.get("training_type") if isinstance(option, Mapping) else None
        if training_type not in TRAINING_ORDER or training_type in by_type:
            return _conflict("training_type_set_invalid", turn_packet_sha256)
        requested_image = (
            archived_image_paths.get(training_type)
            if archived_image_paths is not None
            else option.get("image_path")
        )
        image_path = _workspace_image_path(requested_image, root)
        if image_path is None:
            return _conflict("training_image_path_invalid", turn_packet_sha256)
        by_type[training_type] = _build_card(
            option,
            training_type=training_type,
            image_path=image_path,
            turn_packet_sha256=turn_packet_sha256,
        )

    if set(by_type) != set(TRAINING_ORDER):
        return _conflict("training_type_set_invalid", turn_packet_sha256)
    return {
        "status": "reliable",
        "turn_packet_sha256": turn_packet_sha256,
        "cards": [by_type[training_type] for training_type in TRAINING_ORDER],
    }


def _conflict(reason: str, turn_packet_sha256: str) -> dict[str, Any]:
    return {
        "status": "conflict",
        "reason": reason,
        "turn_packet_sha256": turn_packet_sha256,
        "cards": [],
    }


def _workspace_image_path(value: Any, root: Path) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    requested = Path(value)
    candidate = requested if requested.is_absolute() else root / requested
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return relative.as_posix()


def _build_card(
    option: Mapping[str, Any],
    *,
    training_type: str,
    image_path: str,
    turn_packet_sha256: str,
) -> dict[str, Any]:
    gains = _normalize_gains(option.get("stats_gain_total"))
    failure_rate = _failure_rate(option.get("failure_rate"))
    rejected = option.get("rejected") is True or bool(option.get("rejected_reason"))
    people = _accepted_people(option.get("support_slots"))
    visible_count = _nonnegative_int(option.get("visible_portrait_count"))
    unresolved = _nonnegative_int(option.get("unresolved_portrait_count"))
    if unresolved is None:
        unresolved = max(0, (visible_count or 0) - len(people))
    return {
        "training_type": training_type,
        "image_path": image_path,
        "turn_packet_sha256": turn_packet_sha256,
        "gains": gains,
        "gain_status": _gain_status(option, gains),
        "failure_rate": failure_rate,
        "failure_status": "reliable" if failure_rate is not None else "unknown",
        "executable": failure_rate is not None and not rejected,
        "people": people,
        "visible_portrait_count": visible_count,
        "unresolved_portrait_count": unresolved,
        "support_right_lane_shadow": _summarize_support_right_lane_shadow(
            option.get("support_right_lane_shadow")
        ),
    }


def _normalize_gains(value: Any) -> dict[str, int | None]:
    source = value if isinstance(value, Mapping) else {}
    return {key: _nonnegative_int(source.get(key)) for key in STAT_KEYS}


def _gain_status(option: Mapping[str, Any], gains: Mapping[str, int | None]) -> str:
    declared = option.get("decision_summary_status")
    if declared in {"reliable", "accepted"}:
        return "reliable"
    if declared == "partial":
        return "partial"
    known_count = sum(value is not None for value in gains.values())
    if known_count == len(STAT_KEYS):
        return "reliable"
    return "partial" if known_count else "unknown"


def _failure_rate(value: Any) -> int | None:
    rate = _nonnegative_int(value)
    return rate if rate is not None and rate <= 100 else None


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _accepted_people(value: Any) -> list[dict[str, str | None]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str | None]] = []
    for slot in value:
        if not isinstance(slot, Mapping):
            continue
        identity = slot.get("identity") if isinstance(slot.get("identity"), Mapping) else {}
        status = slot.get("identity_status") or identity.get("status")
        if status != "known":
            continue
        entity_kind = slot.get("entity_kind") or identity.get("entity_kind")
        matched_card_id = slot.get("matched_card_id") or identity.get("matched_card_id")
        if entity_kind == "support_card" and isinstance(matched_card_id, str) and matched_card_id:
            name = _accepted_name(slot, identity, support=True)
            if name:
                result.append({"kind": "support_card", "card_id": matched_card_id, "display_name": name})
        elif entity_kind == "non_support_npc":
            name = _accepted_name(slot, identity, support=False)
            if name:
                result.append({"kind": "npc", "card_id": None, "display_name": name})
    return result


def _accepted_name(slot: Mapping[str, Any], identity: Mapping[str, Any], *, support: bool) -> str | None:
    support_card = slot.get("support_card") if isinstance(slot.get("support_card"), Mapping) else {}
    keys = (
        ("identity_character_name", slot),
        ("character_name", identity),
        ("character_name", support_card),
        ("identity_display_name", slot),
        ("display_name", identity),
    ) if support else (
        ("identity_display_name", slot),
        ("display_name", identity),
        ("character_name", identity),
    )
    for key, source in keys:
        candidate = source.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


__all__ = ["TRAINING_ORDER", "build_training_evidence"]
