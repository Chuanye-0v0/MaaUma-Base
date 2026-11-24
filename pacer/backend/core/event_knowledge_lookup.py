"""Read-only lookup over reviewed event snapshots and a local candidate index."""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from scripts.validate_event_knowledge import validate_snapshot


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "data/event_knowledge/schema/event_knowledge.schema.json"
REVIEWED_PATHS = (
    ROOT / "data/event_knowledge/staging/larc_observed_20260924_v1.json",
    ROOT / "data/event_knowledge/staging/larc_mei_outing3_observed_20260925_v1.json",
    ROOT / "data/event_knowledge/staging/larc_with_observed_20260925_v1.json",
    ROOT / "data/event_knowledge/staging/mcqueen_30139_reference_v1.json",
    ROOT / "data/event_knowledge/staging/current_deck_sample_v1.json",
    ROOT / "data/event_knowledge/staging/real_fixture_winning_ticket_stage2_v1.json",
    ROOT / "data/event_knowledge/staging/christmas_oguri_cn_observed_20260928_v1.json",
    ROOT / "data/event_knowledge/staging/current_run_support_cn_observed_20260928_v1.json",
)
PACKAGED_PERSONAL_PATH = ROOT / "data/event_knowledge/umaassistant_personal_v1.json"
LEGACY_PERSONAL_PATH = ROOT / ".cache/event_knowledge/umaassistant_personal_v1.json"
PERSONAL_PATH = PACKAGED_PERSONAL_PATH if PACKAGED_PERSONAL_PATH.is_file() else LEGACY_PERSONAL_PATH
CHARACTER_CN_OVERLAY_PATH = ROOT / "data/event_knowledge/character_cn_title_overlay_v1.json"
PACKAGED_CN_TITLE_CATALOG_PATH = ROOT / "data/event_knowledge/bwiki_cn_titles_v1.json"
LEGACY_CN_TITLE_CATALOG_PATH = ROOT / ".cache/event_knowledge/bwiki_cn_titles_v1.json"
CN_TITLE_CATALOG_PATH = (
    PACKAGED_CN_TITLE_CATALOG_PATH
    if PACKAGED_CN_TITLE_CATALOG_PATH.is_file()
    else LEGACY_CN_TITLE_CATALOG_PATH
)
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'))
_STAGE = re.compile(r"^(support_chain_step|support_outing_step):(\d+)$")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    for left, right in _QUOTE_PAIRS:
        if value.startswith(left) and value.endswith(right):
            value = value[len(left) : -len(right)].strip()
    return " ".join(value.split())


def _titles(event: dict[str, Any]) -> Iterable[str]:
    titles = event["titles"]
    for key, value in titles.items():
        if key == "aliases":
            yield from (alias["text"] for alias in value)
        elif isinstance(value, str):
            yield value


def _title_match(event: dict[str, Any], key: str) -> dict[str, str] | None:
    for locale, value in event["titles"].items():
        if locale == "aliases":
            for alias in value:
                if normalize_text(alias["text"]) == key:
                    return alias
        elif isinstance(value, str) and normalize_text(value) == key:
            return {"text": value, "locale": locale}
    return None


def _option_matches(event: dict[str, Any], option_texts: list[str] | None) -> bool:
    if option_texts is None:
        return True
    options = sorted(event["options"], key=lambda option: option["index"])
    if len(options) != len(option_texts):
        return False
    return all(
        normalize_text(text) in {normalize_text(value) for value in option["texts"].values()}
        for text, option in zip(option_texts, options)
    )


def _public_event(event: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "story_id": event["story_id"],
        "owner": event["owner"],
        "event_kind": event["event_kind"],
        "trigger_conditions": event["trigger_conditions"],
        "chain_stage": chain_stage(event),
        "terminal_option_ids": [
            option["option_id"] for option in event["options"]
            if any(effect["kind"] == "chain_end" for effect in option["effects"])
        ],
        "terminal_branch_outcomes": [
            {"option_id": option["option_id"], "outcome": branch["outcome"]}
            for option in event["options"] for branch in option["conditional_branches"]
            if any(effect["kind"] == "chain_end" for effect in branch["effects"])
        ],
        "titles": event["titles"],
        "title_match": _title_match(event, key),
        "options": sorted(event["options"], key=lambda option: option["index"]),
        "sources": event["sources"],
        "review_status": event["review_status"],
        "region_availability": event["region_availability"],
        "usable_for_action": False,
    }


def chain_stage(event: dict[str, Any]) -> dict[str, Any] | None:
    for condition in event["trigger_conditions"]:
        match = _STAGE.fullmatch(condition)
        if match:
            return {"kind": "chain" if match[1] == "support_chain_step" else "outing", "step": int(match[2])}
    return None


def _file_version(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=8)
def _reviewed_catalog(schema_version, versions):
    schema = json.loads(Path(schema_version[0]).read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = []
    for path, _, _ in versions:
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
        validate_snapshot(snapshot, schema)
        events.extend(snapshot["events"])
    return events


@lru_cache(maxsize=2)
def _character_cn_overlay_cached(version: tuple[str, int, int] | None) -> dict[str, Any]:
    if version is None:
        return {"extra_characters": {}, "events": {}}
    data = json.loads(Path(version[0]).read_text(encoding="utf-8"))
    if data.get("format") != "uma.event_knowledge.character_cn_title_overlay.v1":
        raise ValueError("character CN title overlay format changed")
    return data


def load_character_cn_overlay(overlay_path: Path | None = CHARACTER_CN_OVERLAY_PATH) -> dict[str, Any]:
    if overlay_path is None:
        return {"extra_characters": {}, "events": {}}
    path = Path(overlay_path)
    return _character_cn_overlay_cached(_file_version(path) if path.is_file() else None)


def _apply_cn_overlay(event: dict[str, Any], overlay_events: dict[str, Any]) -> dict[str, Any]:
    patch = overlay_events.get(event.get("source_id"))
    if not patch or event.get("titles", {}).get("zh_cn"):
        return event
    updated = deepcopy(event)
    updated.setdefault("titles", {})["zh_cn"] = patch["zh_cn"]
    align = dict(updated.get("cn_title_alignment") or {})
    if not align.get("candidate_story_ids") and patch.get("candidate_story_ids"):
        align["candidate_story_ids"] = list(patch["candidate_story_ids"])
    align.setdefault("method", patch.get("method") or "character_cn_title_overlay_v1")
    updated["cn_title_alignment"] = align
    return updated


def load_reviewed_events(reviewed_paths: Iterable[Path] = REVIEWED_PATHS) -> list[dict[str, Any]]:
    # Validate once per file version; callers cannot mutate the cached library.
    return deepcopy(_reviewed_catalog(_file_version(SCHEMA_PATH),
                                     tuple(_file_version(path) for path in reviewed_paths)))


def lookup_event(
    title: str,
    option_texts: list[str] | None = None,
    *,
    owner_id: str | None = None,
    reviewed_paths: Iterable[Path] = REVIEWED_PATHS,
    personal_path: Path | None = PERSONAL_PATH,
    title_catalog_path: Path | None = CN_TITLE_CATALOG_PATH,
) -> dict[str, Any]:
    """Return exact candidates; never infer an effect from title alone on collision."""
    reviewed = load_reviewed_events(reviewed_paths)
    key = normalize_text(title)
    title_matches = [
        event for event in reviewed
        if _title_match(event, key) is not None
        and (owner_id is None or event["owner"]["owner_id"] == owner_id)
    ]
    exact = [event for event in title_matches if _option_matches(event, option_texts)]
    if exact:
        candidates = [_public_event(event, key) for event in exact]
        complete = all(
            option["effects"] or option["conditional_branches"]
            for option in exact[0]["options"]
        ) if len(exact) == 1 else False
        status = "ambiguous" if len(exact) > 1 else "incomplete" if not complete else (
            "source_reference" if exact[0]["event_id"].startswith("source.unverified:") else "reviewed"
        )
        return {
            "status": status,
            "candidates": candidates,
            "usable_for_action": False,
        }
    if title_matches:
        return {"status": "option_mismatch", "candidates": [_public_event(event, key) for event in title_matches], "usable_for_action": False}

    if personal_path is not None and Path(personal_path).exists():
        p_path = Path(personal_path)
        index = json.loads(p_path.read_text(encoding="utf-8"))
        if index.get("format") != "uma.event_knowledge.personal_candidates.v1":
            raise ValueError("personal candidate index format changed")
        overlay_events = (
            load_character_cn_overlay().get("events", {})
            if p_path.resolve() in {
                PERSONAL_PATH.resolve(),
                PACKAGED_PERSONAL_PATH.resolve(),
                LEGACY_PERSONAL_PATH.resolve(),
            }
            else {}
        )
        matches = []
        for raw_event in index["events"]:
            event = _apply_cn_overlay(raw_event, overlay_events) if overlay_events else raw_event
            if (
                key in {normalize_text(value) for value in _titles(event)}
                and (owner_id is None or event["owner_name_ja"] == owner_id or event.get("owner_name_zh_tw") == owner_id)
                and _option_matches(event, option_texts)
            ):
                matches.append(event)
        if matches:
            return {"status": "source_candidate" if len(matches) == 1 else "ambiguous", "candidates": matches, "usable_for_action": False}
    if owner_id is None and personal_path is not None and title_catalog_path is not None and Path(title_catalog_path).exists():
        catalog = json.loads(Path(title_catalog_path).read_text(encoding="utf-8"))
        if catalog.get("format") != "uma.event_knowledge.cn_titles.v1":
            raise ValueError("CN title catalog format changed")
        story_ids = catalog["by_title"].get(key)
        if story_ids:
            return {
                "status": "title_only",
                "candidates": [{
                    "source_id": "bwiki:cn_title",
                    "candidate_story_ids": story_ids,
                    "titles": {"zh_cn": key},
                    "options": [],
                    "source_url": catalog["source_url"],
                    "review_status": "title_only",
                    "cn_release_status": "unknown",
                    "usable_for_action": False,
                }],
                "usable_for_action": False,
            }
    return {"status": "unknown", "candidates": [], "usable_for_action": False}


@lru_cache(maxsize=2)
def _personal_cn_title_index(
    version: tuple[str, int, int],
    overlay_version: tuple[str, int, int] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    index = json.loads(Path(version[0]).read_text(encoding="utf-8"))
    if index.get("format") != "uma.event_knowledge.personal_candidates.v1":
        raise ValueError("personal candidate index format changed")
    overlay_events = _character_cn_overlay_cached(overlay_version).get("events", {}) if overlay_version is not None else {}
    by_title: dict[str, list[dict[str, Any]]] = {}
    for raw_event in index["events"]:
        event = _apply_cn_overlay(raw_event, overlay_events) if overlay_events else raw_event
        title = event["titles"].get("zh_cn")
        if title:
            by_title.setdefault(normalize_text(title), []).append(event)
    return by_title


@lru_cache(maxsize=2)
def _cn_title_id_index(version: tuple[str, int, int]) -> dict[str, list[str]]:
    catalog = json.loads(Path(version[0]).read_text(encoding="utf-8"))
    if catalog.get("format") != "uma.event_knowledge.cn_titles.v1":
        raise ValueError("CN title catalog format changed")
    return catalog["by_title"]


def personal_candidate_reference(
    visible_text: str,
    option_count: int,
    *,
    personal_path: Path = PERSONAL_PATH,
    title_catalog_path: Path = CN_TITLE_CATALOG_PATH,
    max_candidates: int = 5,
) -> dict[str, Any]:
    """Expose CN-title candidate options as reference only for a visible event.

    A title and option count cannot establish the localized option order or CN
    release. Callers must not turn these Japanese references into an action.
    """
    path = Path(personal_path)
    catalog_path = Path(title_catalog_path)
    if not path.is_file() and not catalog_path.is_file():
        return {"status": "unavailable", "candidates": [], "usable_for_action": False}
    lines = [normalize_text(line) for line in visible_text.splitlines() if line.strip()]
    overlay_ver = (
        _file_version(CHARACTER_CN_OVERLAY_PATH)
        if path.is_file()
        and path.resolve() in {
            PERSONAL_PATH.resolve(),
            PACKAGED_PERSONAL_PATH.resolve(),
            LEGACY_PERSONAL_PATH.resolve(),
        }
        and CHARACTER_CN_OVERLAY_PATH.is_file()
        else None
    )
    index = _personal_cn_title_index(_file_version(path), overlay_ver) if path.is_file() else {}
    title_hits = [line for line in lines if lines.count(line) == 1 and line in index]
    if len(title_hits) != 1:
        if title_hits:
            return {"status": "ambiguous_title", "candidates": [], "usable_for_action": False}
        catalog = _cn_title_id_index(_file_version(catalog_path)) if catalog_path.is_file() else {}
        id_hits = [line for line in lines if lines.count(line) == 1 and line in catalog]
        if len(id_hits) == 1:
            return {"status": "title_only", "title_zh_cn": id_hits[0],
                    "candidate_story_ids": catalog[id_hits[0]], "candidates": [],
                    "usable_for_action": False}
        return {"status": "unknown" if not id_hits else "ambiguous_title",
                "candidates": [], "usable_for_action": False}
    title = title_hits[0]
    matches = [event for event in index[title]
               if len(event["options"]) == option_count and all(option["index"] > 0 for option in event["options"])]
    candidates = [{
        "source_id": event["source_id"],
        "owner_type": event["owner_type"],
        "owner_name_ja": event["owner_name_ja"],
        "owner_name_zh_tw": event.get("owner_name_zh_tw"),
        "candidate_story_ids": event.get("cn_title_alignment", {}).get("candidate_story_ids", []),
        "options": [{"index": option["index"], "text_ja": option["texts"].get("ja"),
                     "effect_text_ja": option["effect_text"]} for option in event["options"]],
        "source_url": event["source_url"],
        "cn_release_status": event["cn_release_status"],
    } for event in matches[:max_candidates]]
    return {"status": "source_candidate" if len(matches) == 1 else "ambiguous" if matches else "option_count_mismatch",
            "title_zh_cn": title, "candidate_count": len(matches), "candidates": candidates,
            "option_order_verified": False, "usable_for_action": False}


def coverage(
    *, reviewed_paths: Iterable[Path] = REVIEWED_PATHS,
    personal_path: Path | None = PERSONAL_PATH,
    title_catalog_path: Path | None = CN_TITLE_CATALOG_PATH,
) -> dict[str, int]:
    events = load_reviewed_events(reviewed_paths)
    complete = sum(all(option["effects"] or option["conditional_branches"] for option in event["options"]) for event in events)
    cn_titles = sum(bool(event["titles"].get("zh_cn")) for event in events)
    cn_options = sum(all(option["texts"].get("zh_cn") for option in event["options"]) for event in events)
    result = {"reviewed_events": len(events), "complete_structured_events": complete, "cn_title_events": cn_titles, "cn_option_events": cn_options}
    if personal_path is not None and Path(personal_path).exists():
        candidates = json.loads(Path(personal_path).read_text(encoding="utf-8"))["events"]
        result.update({
            "personal_candidates": len(candidates),
            "candidates_with_effect_text": sum(all(option["effect_text"] for option in event["options"]) for event in candidates),
            "candidates_with_zh_tw_title": sum(bool(event["titles"].get("zh_tw")) for event in candidates),
            "candidates_with_zh_cn_title": sum(bool(event["titles"].get("zh_cn")) for event in candidates),
        })
    if personal_path is not None and title_catalog_path is not None and Path(title_catalog_path).exists():
        catalog = json.loads(Path(title_catalog_path).read_text(encoding="utf-8"))
        if catalog.get("format") != "uma.event_knowledge.cn_titles.v1":
            raise ValueError("CN title catalog format changed")
        result["cn_title_id_count"] = catalog["title_id_count"]
        result["cn_distinct_titles"] = len(catalog["by_title"])
    return result
