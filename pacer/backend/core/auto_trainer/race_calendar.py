"""Versioned, read-only career race calendar.

The APK race database is metadata-only.  Explicit career dates must come from
the separately frozen schedule source; this module never derives dates from
the opaque ``prize`` field.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RACE_ID_RE = re.compile(r"race\.[a-z0-9][a-z0-9._-]*\Z")
_STAGE_OFFSETS = {
    "junior": 1,
    "classic": 25,
    "senior": 49,
    "final": 73,
}
_STAGE_YEAR_INDEX = {
    "junior": 1,
    "classic": 2,
    "senior": 3,
    "final": 4,
}
_ENTRY_KEYS = frozenset(
    {
        "schema_version",
        "race_id",
        "apk_file_ref",
        "names",
        "career_time",
        "grade",
        "surface",
        "distance_m",
        "distance_category",
        "direction",
        "venue",
        "entry_requirements",
        "schedule_status",
        "source_refs",
        "apk_metadata_sha256",
    }
)
_CALENDAR_KEYS = frozenset(
    {
        "schema_version",
        "calendar_id",
        "generated_at",
        "apk_database_sha256",
        "entries",
    }
)
_CALENDAR_KEYS_WITH_SHA = _CALENDAR_KEYS | {"calendar_sha256"}
_SOURCE_REF_KEYS = frozenset(
    {"source_id", "url", "retrieved_at", "content_sha256"}
)
_GRADES = frozenset({"G1", "G2", "G3", "OP"})
_SCHEDULE_STATUSES = frozenset({"verified", "unknown", "conflict", "unavailable"})


class RaceCalendarError(ValueError):
    """Stable validation failure for calendar data."""


@dataclass(frozen=True)
class CareerTime:
    stage: Literal["junior", "classic", "senior", "final"]
    year_index: int
    month: int
    half: Literal["first", "second"]
    global_turn: int


@dataclass(frozen=True)
class RaceCalendarEntry:
    schema_version: str
    race_id: str
    apk_file_ref: str
    names: Mapping[str, object]
    career_time: CareerTime | None
    grade: Literal["G1", "G2", "G3", "OP"]
    surface: str
    distance_m: int
    distance_category: str
    direction: str
    venue: str
    entry_requirements: Mapping[str, object]
    schedule_status: Literal["verified", "unknown", "conflict", "unavailable"]
    source_refs: tuple[Mapping[str, object], ...]
    apk_metadata_sha256: str


@dataclass(frozen=True)
class RaceCalendar:
    schema_version: str
    calendar_id: str
    generated_at: str
    apk_database_sha256: str
    entries: tuple[RaceCalendarEntry, ...]
    calendar_sha256: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "RaceCalendar":
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RaceCalendarError("calendar_file_invalid") from exc
        if type(payload) is not dict:
            raise RaceCalendarError("calendar_root_not_object")
        bound_sha = payload.get("calendar_sha256")
        if not _is_sha256(bound_sha):
            raise RaceCalendarError("calendar_sha256_missing")
        hash_payload = dict(payload)
        del hash_payload["calendar_sha256"]
        if _canonical_sha256(hash_payload) != bound_sha:
            raise RaceCalendarError("calendar_sha256_mismatch")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: object) -> "RaceCalendar":
        if type(payload) is not dict:
            raise RaceCalendarError("calendar_root_not_object")
        keys = frozenset(payload)
        if keys not in {_CALENDAR_KEYS, _CALENDAR_KEYS_WITH_SHA}:
            raise RaceCalendarError("calendar_keys_invalid")
        if payload.get("schema_version") != "race_calendar.v1":
            raise RaceCalendarError("calendar_schema_version_invalid")
        calendar_id = _required_text(payload.get("calendar_id"), "calendar_id_invalid")
        generated_at = _required_date(payload.get("generated_at"), "generated_at_invalid")
        apk_database_sha256 = _required_sha256(
            payload.get("apk_database_sha256"),
            "apk_database_sha256_invalid",
        )
        rows = payload.get("entries")
        if type(rows) is not list:
            raise RaceCalendarError("entries_not_array")
        entries = tuple(_parse_entry(row) for row in rows)
        race_ids = [item.race_id for item in entries]
        if len(race_ids) != len(set(race_ids)):
            raise RaceCalendarError("duplicate_race_id")
        calendar_sha256 = payload.get("calendar_sha256")
        if calendar_sha256 is not None and not _is_sha256(calendar_sha256):
            raise RaceCalendarError("calendar_sha256_invalid")
        if (
            calendar_sha256 is not None
            and canonical_calendar_sha256(payload) != calendar_sha256
        ):
            raise RaceCalendarError("calendar_sha256_mismatch")
        return cls(
            schema_version="race_calendar.v1",
            calendar_id=calendar_id,
            generated_at=generated_at,
            apk_database_sha256=apk_database_sha256,
            entries=entries,
            calendar_sha256=calendar_sha256,
        )

    def query_turn(
        self,
        global_turn: int,
        *,
        formal_only: bool,
    ) -> tuple[RaceCalendarEntry, ...]:
        if type(global_turn) is not int or global_turn <= 0:
            raise RaceCalendarError("global_turn_invalid")
        return tuple(
            item
            for item in self.entries
            if item.career_time is not None
            and item.career_time.global_turn == global_turn
            and (not formal_only or item.schedule_status == "verified")
        )

    def query_window(
        self,
        start_turn: int,
        end_turn: int,
        *,
        formal_only: bool,
    ) -> tuple[RaceCalendarEntry, ...]:
        if (
            type(start_turn) is not int
            or type(end_turn) is not int
            or start_turn <= 0
            or end_turn < start_turn
        ):
            raise RaceCalendarError("turn_window_invalid")
        return tuple(
            item
            for item in self.entries
            if item.career_time is not None
            and start_turn <= item.career_time.global_turn <= end_turn
            and (not formal_only or item.schedule_status == "verified")
        )

    def get(self, race_id: str) -> RaceCalendarEntry | None:
        if type(race_id) is not str:
            return None
        return next((item for item in self.entries if item.race_id == race_id), None)


def career_global_turn(
    *,
    stage: str,
    year_index: int,
    month: int,
    half: str,
) -> int:
    if stage not in _STAGE_OFFSETS:
        raise RaceCalendarError("career_time_stage_invalid")
    if type(year_index) is not int or year_index != _STAGE_YEAR_INDEX[stage]:
        raise RaceCalendarError("career_time_year_index_invalid")
    if type(month) is not int or not 1 <= month <= 12:
        raise RaceCalendarError("career_time_month_invalid")
    if half not in {"first", "second"}:
        raise RaceCalendarError("career_time_half_invalid")
    return _STAGE_OFFSETS[stage] + (month - 1) * 2 + (0 if half == "first" else 1)


def build_entry(
    *,
    apk_record: Mapping[str, object],
    schedule_record: Mapping[str, object] | None,
    apk_database_sha256: str,
) -> dict[str, object]:
    """Build one entry without deriving a date from APK metadata."""
    if schedule_record is None:
        raise RaceCalendarError("missing_explicit_career_time")
    if type(apk_record) is not dict or type(schedule_record) is not dict:
        raise RaceCalendarError("source_record_invalid")
    if not _is_sha256(apk_database_sha256):
        raise RaceCalendarError("apk_database_sha256_invalid")
    raise RaceCalendarError("build_entry_not_implemented")


def canonical_calendar_sha256(payload: Mapping[str, object]) -> str:
    if type(payload) is not dict:
        raise RaceCalendarError("calendar_root_not_object")
    hash_payload = dict(payload)
    hash_payload.pop("calendar_sha256", None)
    return _canonical_sha256(hash_payload)


def _parse_entry(row: object) -> RaceCalendarEntry:
    if type(row) is not dict:
        raise RaceCalendarError("entry_not_object")
    if frozenset(row) != _ENTRY_KEYS:
        raise RaceCalendarError("entry_keys_invalid")
    if row.get("schema_version") != "race_calendar_entry.v1":
        raise RaceCalendarError("entry_schema_version_invalid")
    race_id = _required_text(row.get("race_id"), "race_id_invalid")
    if _RACE_ID_RE.fullmatch(race_id) is None:
        raise RaceCalendarError("race_id_invalid")
    apk_file_ref = _required_text(row.get("apk_file_ref"), "apk_file_ref_invalid")
    names = _parse_names(row.get("names"))
    career_time = _parse_career_time(row.get("career_time"))
    grade = row.get("grade")
    if type(grade) is not str or grade not in _GRADES:
        raise RaceCalendarError("grade_invalid")
    schedule_status = row.get("schedule_status")
    if type(schedule_status) is not str or schedule_status not in _SCHEDULE_STATUSES:
        raise RaceCalendarError("schedule_status_invalid")
    if schedule_status == "verified" and career_time is None:
        raise RaceCalendarError("verified_schedule_missing_career_time")
    surface = _required_text(row.get("surface"), "surface_invalid")
    distance_m = row.get("distance_m")
    if type(distance_m) is not int or not 100 <= distance_m <= 10000:
        raise RaceCalendarError("distance_m_invalid")
    distance_category = _required_text(
        row.get("distance_category"),
        "distance_category_invalid",
    )
    direction = _required_text(row.get("direction"), "direction_invalid")
    venue = _required_text(row.get("venue"), "venue_invalid")
    entry_requirements = _parse_entry_requirements(row.get("entry_requirements"))
    source_refs = _parse_source_refs(
        row.get("source_refs"),
        grade=grade,
        schedule_status=schedule_status,
        has_career_time=career_time is not None,
    )
    apk_metadata_sha256 = _required_sha256(
        row.get("apk_metadata_sha256"),
        "apk_metadata_sha256_invalid",
    )
    return RaceCalendarEntry(
        schema_version="race_calendar_entry.v1",
        race_id=race_id,
        apk_file_ref=apk_file_ref,
        names=names,
        career_time=career_time,
        grade=grade,
        surface=surface,
        distance_m=distance_m,
        distance_category=distance_category,
        direction=direction,
        venue=venue,
        entry_requirements=entry_requirements,
        schedule_status=schedule_status,
        source_refs=source_refs,
        apk_metadata_sha256=apk_metadata_sha256,
    )


def _parse_names(value: object) -> Mapping[str, object]:
    if type(value) is not dict or frozenset(value) != {"ja_jp", "zh_cn", "aliases"}:
        raise RaceCalendarError("names_invalid")
    ja_jp = _required_text(value.get("ja_jp"), "ja_jp_name_invalid")
    zh_cn = value.get("zh_cn")
    if zh_cn is not None and (type(zh_cn) is not str or not zh_cn.strip()):
        raise RaceCalendarError("zh_cn_name_invalid")
    aliases = value.get("aliases")
    if type(aliases) is not list or any(
        type(item) is not str or not item.strip() for item in aliases
    ):
        raise RaceCalendarError("aliases_invalid")
    return MappingProxyType(
        {
            "ja_jp": ja_jp,
            "zh_cn": zh_cn,
            "aliases": tuple(aliases),
        }
    )


def _parse_career_time(value: object) -> CareerTime | None:
    if value is None:
        return None
    if type(value) is not dict or frozenset(value) != {
        "stage",
        "year_index",
        "month",
        "half",
        "global_turn",
    }:
        raise RaceCalendarError("career_time_invalid")
    stage = value.get("stage")
    year_index = value.get("year_index")
    month = value.get("month")
    half = value.get("half")
    global_turn = value.get("global_turn")
    if type(stage) is not str or type(half) is not str:
        raise RaceCalendarError("career_time_invalid")
    expected_turn = career_global_turn(
        stage=stage,
        year_index=year_index,
        month=month,
        half=half,
    )
    if type(global_turn) is not int or global_turn != expected_turn:
        raise RaceCalendarError("career_time_global_turn_mismatch")
    return CareerTime(
        stage=stage,
        year_index=year_index,
        month=month,
        half=half,
        global_turn=global_turn,
    )


def _parse_entry_requirements(value: object) -> Mapping[str, object]:
    if type(value) is not dict or frozenset(value) != {"fan_count"}:
        raise RaceCalendarError("entry_requirements_invalid")
    fan_count = value.get("fan_count")
    if type(fan_count) is not int or fan_count < 0:
        raise RaceCalendarError("fan_count_invalid")
    return MappingProxyType({"fan_count": fan_count})


def _parse_source_refs(
    value: object,
    *,
    grade: str,
    schedule_status: str,
    has_career_time: bool,
) -> tuple[Mapping[str, object], ...]:
    if type(value) is not list:
        raise RaceCalendarError("source_refs_not_array")
    if schedule_status == "verified":
        minimum = 3 if grade == "G1" else 2
    elif schedule_status == "conflict":
        minimum = 1
    elif schedule_status == "unknown" and has_career_time:
        minimum = 1
    else:
        minimum = 0
    if len(value) < minimum:
        raise RaceCalendarError("source_refs_insufficient")
    result: list[Mapping[str, object]] = []
    source_ids: set[str] = set()
    for item in value:
        if type(item) is not dict or frozenset(item) != _SOURCE_REF_KEYS:
            raise RaceCalendarError("source_ref_invalid")
        source_id = _required_text(item.get("source_id"), "source_id_invalid")
        if source_id in source_ids:
            raise RaceCalendarError("duplicate_source_id")
        source_ids.add(source_id)
        url = _required_text(item.get("url"), "source_url_invalid")
        if not url.startswith("https://"):
            raise RaceCalendarError("source_url_invalid")
        retrieved_at = _required_date(
            item.get("retrieved_at"),
            "source_retrieved_at_invalid",
        )
        content_sha256 = _required_sha256(
            item.get("content_sha256"),
            "source_content_sha256_invalid",
        )
        if content_sha256 == "0" * 64:
            raise RaceCalendarError("source_content_sha256_invalid")
        result.append(
            MappingProxyType(
                {
                    "source_id": source_id,
                    "url": url,
                    "retrieved_at": retrieved_at,
                    "content_sha256": content_sha256,
                }
            )
        )
    return tuple(result)


def _required_text(value: object, reason: str) -> str:
    if type(value) is not str or not value.strip():
        raise RaceCalendarError(reason)
    return value


def _required_date(value: object, reason: str) -> str:
    text = _required_text(value, reason)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise RaceCalendarError(reason) from exc
    return text


def _required_sha256(value: object, reason: str) -> str:
    if not _is_sha256(value):
        raise RaceCalendarError(reason)
    return value


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
