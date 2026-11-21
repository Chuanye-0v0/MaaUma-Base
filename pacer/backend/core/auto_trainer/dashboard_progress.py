from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.pacer_storage import _io, _rglob


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TURN_RE = re.compile(r"^turn_(\d+)$")
_DECK_SCHEMA = "uma_current_support_deck.v1"
_SETUP_SCHEMA = "uma.career_setup_cli.v1"
_SUPPORT_TRACKER_SCHEMA = "ug_parent_dashboard.support_progress.v1"


@dataclass(frozen=True)
class _DeckCard:
    slot: int
    card_id: str
    display_name: str
    rarity: str
    card_type: str

    @property
    def total(self) -> int | None:
        if self.card_type == "group":
            return None
        return 3 if self.rarity == "SSR" else 2 if self.rarity == "SR" else None


@dataclass(frozen=True)
class _VerifiedDeck:
    cards: tuple[_DeckCard, ...]
    evidence_sha256: str


def build_verified_deck_status(
    run_dir: str | Path,
    *,
    deck_manifest_path: str | Path,
) -> dict[str, Any]:
    manifest_cards = _load_deck_manifest(Path(deck_manifest_path))
    verified = _select_verified_deck(Path(run_dir), manifest_cards)
    if verified is None:
        return {"status": "unknown", "cards": [], "card_ids": []}
    return {
        "status": "observed",
        "evidence_sha256": verified.evidence_sha256,
        "card_ids": [card.card_id for card in verified.cards],
        "cards": [
            {
                "slot": card.slot,
                "card_id": card.card_id,
                "display_name": card.display_name,
                "rarity": card.rarity,
                "card_type": card.card_type,
            }
            for card in verified.cards
        ],
    }


def build_progress_snapshot(
    run_dir: str | Path,
    turn_dir: str | Path,
    *,
    deck_manifest_path: str | Path,
) -> dict[str, Any]:
    run = Path(run_dir)
    turn = Path(turn_dir)
    manifest_cards = _load_deck_manifest(Path(deck_manifest_path))
    verified = _select_verified_deck(run, manifest_cards)
    if verified is None:
        return _unknown_snapshot()

    ordinary = tuple(card for card in verified.cards if card.card_type != "group")
    group_cards = tuple(card for card in verified.cards if card.card_type == "group")
    if len(ordinary) != 5 or len(group_cards) != 1:
        return _unknown_snapshot()

    tracker, tracker_status = _load_support_tracker(turn)
    review_events: list[dict[str, Any]] = []
    if tracker is None:
        history_complete = tracker_status == "missing" and _turn_index(turn) == 1
        events: list[Any] = []
        if tracker_status == "conflict":
            review_events.append({"reason": "tracker_schema_invalid", "source_sha256": None})
    else:
        history_complete = tracker["history_complete"]
        events = tracker["events"]

    completed: dict[str, int | None] = {
        card.card_id: 0 if history_complete else None for card in ordinary
    }
    observed = {card.card_id: 0 for card in ordinary}
    totals = {card.card_id: card.total for card in ordinary}
    duplicate_count = 0
    seen: set[tuple[str, str]] = set()
    for raw_event in events:
        if not isinstance(raw_event, Mapping):
            review_events.append({"reason": "event_schema_invalid", "source_sha256": None})
            continue
        event_id = raw_event.get("event_id")
        source_sha256 = raw_event.get("source_sha256")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(source_sha256, str)
            or _SHA256_RE.fullmatch(source_sha256) is None
        ):
            review_events.append({"reason": "event_schema_invalid", "source_sha256": None})
            continue
        key = (event_id, source_sha256)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)

        card_id = raw_event.get("matched_card_id")
        if raw_event.get("binding_status") != "accepted" or card_id not in observed:
            review_events.append({
                "reason": "event_binding_unresolved",
                "source_sha256": source_sha256,
            })
            continue
        observed[card_id] += 1
        if history_complete:
            assert completed[card_id] is not None
            assert totals[card_id] is not None
            if completed[card_id] >= totals[card_id]:
                review_events.append({
                    "reason": "event_progress_overflow",
                    "source_sha256": source_sha256,
                })
                continue
            completed[card_id] += 1

    rows = [
        {
            "card_id": card.card_id,
            "display_name": card.display_name,
            "rarity": card.rarity,
            "completed": completed[card.card_id],
            "observed_since_handoff": observed[card.card_id],
            "total": card.total,
            "total_source": "rarity_default",
            "completed_source": "complete_structured_history" if history_complete else "partial_structured_history",
        }
        for card in ordinary
    ]
    team = group_cards[0]
    return {
        "support_progress": {
            "status": "observed",
            "history_complete": history_complete,
            "tracker_status": tracker_status,
            "deck_evidence_sha256": verified.evidence_sha256,
            "cards": rows,
            "duplicate_event_count": duplicate_count,
            "review_event_count": len(review_events),
            "review_events": review_events,
        },
        "outing_progress": {
            "status": "observed",
            "team": {
                "card_id": team.card_id,
                "display_name": team.display_name,
                "unlocked": False if history_complete else None,
                "progress": 0 if history_complete else None,
                "progress_source": "initial_state" if history_complete else "history_incomplete",
            },
            "deck_evidence_sha256": verified.evidence_sha256,
        },
    }


def _unknown_snapshot() -> dict[str, Any]:
    return {
        "support_progress": {"status": "unknown", "cards": []},
        "outing_progress": {"status": "unknown"},
    }


def _load_deck_manifest(path: Path) -> tuple[_DeckCard, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    cards = payload.get("cards") if isinstance(payload, dict) and payload.get("schema_version") == _DECK_SCHEMA else None
    if not isinstance(cards, list) or len(cards) != 6:
        return ()
    normalized: list[_DeckCard] = []
    for raw in cards:
        if not isinstance(raw, Mapping):
            return ()
        slot = raw.get("slot")
        card_id = raw.get("card_id")
        display_name = raw.get("character_name")
        rarity = raw.get("rarity")
        card_type = raw.get("type")
        if (
            not isinstance(slot, int)
            or isinstance(slot, bool)
            or not isinstance(card_id, str)
            or not card_id
            or not isinstance(display_name, str)
            or not display_name
            or rarity not in {"SSR", "SR"}
            or not isinstance(card_type, str)
            or not card_type
            or raw.get("manual_status") != "verified"
        ):
            return ()
        normalized.append(_DeckCard(slot, card_id, display_name, rarity, card_type))
    normalized.sort(key=lambda card: card.slot)
    if [card.slot for card in normalized] != list(range(6)):
        return ()
    if len({card.card_id for card in normalized}) != 6:
        return ()
    return tuple(normalized)


def _select_verified_deck(run: Path, manifest_cards: tuple[_DeckCard, ...]) -> _VerifiedDeck | None:
    if len(manifest_cards) != 6:
        return None
    expected_ids = [card.card_id for card in manifest_cards]
    try:
        candidates = sorted(
            _rglob(run, "career_setup_result.json"),
            key=lambda path: _io(path).stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in candidates:
        try:
            raw = _io(path).read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        packet = payload.get("packet") if isinstance(payload, dict) and payload.get("schema_version") == _SETUP_SCHEMA else None
        if not isinstance(packet, Mapping) or packet.get("page_state") != "support_select_ready":
            continue
        if packet.get("identities") != expected_ids or packet.get("identity_matches") != [True] * 6:
            continue
        return _VerifiedDeck(manifest_cards, hashlib.sha256(raw).hexdigest())
    return None


def _load_support_tracker(turn: Path) -> tuple[dict[str, Any] | None, str]:
    path = turn / "support_progress.json"
    if not path.exists():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "conflict"
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SUPPORT_TRACKER_SCHEMA
        or type(payload.get("history_complete")) is not bool
        or not isinstance(payload.get("events"), list)
    ):
        return None, "conflict"
    return payload, "observed"


def _turn_index(turn: Path) -> int | None:
    match = _TURN_RE.fullmatch(turn.name)
    return int(match.group(1)) if match else None


__all__ = ["build_progress_snapshot", "build_verified_deck_status"]
