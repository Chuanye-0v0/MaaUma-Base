"""Structured, fail-closed perception for career goal result pages."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import cv2
import numpy as np

from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REMAINING_RE = re.compile(
    r"(?:距离|中商)下一个目标还剩(?P<count>[0-9]+)回合"
)
_NEXT_GOAL_MARKER_RE = re.compile(r"(?:↓|√)下一个")
_GOAL_REQUIREMENT_RE = re.compile(
    r"(?:进入前[0-9]+|获得第[0-9]+名|获得[0-9]+名粉丝)"
)
_FAILURE_TERM = "目标未达成"
_CATALOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "career_goals"
    / "goal_catalog_v1.json"
)
_CATALOG_KEYS = frozenset({"schema_version", "catalog_id", "entries"})
_CATALOG_ENTRY_KEYS = frozenset(
    {"goal_kind", "grade_terms", "text_pattern", "supported_screens"}
)
_NAMED_CATALOG_ENTRY_KEYS = frozenset(
    {"goal_kind", "grade", "target_race_id", "text_pattern", "supported_screens"}
)
_GOAL_REGION_READER = LocalRapidOCRScreenTextReader()


class CareerGoalPerceptionError(ValueError):
    """Stable validation failure for goal-page perception."""


@dataclass(frozen=True)
class CareerGoalObservation:
    schema_version: str
    source_frame_sha256: str
    observed_turn: int
    screen: str
    goal_kind: str | None
    grade: str | None
    required_rank: int | None
    required_count: int | None
    completed_count: int | None
    remaining_turns: int | None
    cutoff_turn: int | None
    confidence: float
    field_evidence: Mapping[str, Mapping[str, object]]
    abstain_reasons: tuple[str, ...]
    observation_sha256: str
    target_race_id: str | None = None
    candidate_race_ids: tuple[str, ...] = ()
    progress_basis: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.is_content_complete and (
            self.goal_kind != "named_race_rank" or self.target_race_id is not None
        ) and (self.progress_basis is None or self.cutoff_turn is not None)

    @property
    def is_content_complete(self) -> bool:
        return (
            self.screen == "goal_achievement_result"
            and self.goal_kind is not None
            and self.grade is not None
            and self.required_rank is not None
            and self.required_count is not None
            and self.completed_count is not None
            and self.remaining_turns is not None
            and (
                self.cutoff_turn is not None
                or self.goal_kind == "named_race_rank" and (
                    self.target_race_id is not None or bool(self.candidate_race_ids)
                )
                or self.goal_kind == "graded_race_rank_count"
                and self.progress_basis == "verified_lower_bound"
            )
            and not self.abstain_reasons
        )


def career_goal_observation_to_dict(
    observation: CareerGoalObservation,
) -> dict[str, object]:
    payload = {
        "schema_version": observation.schema_version,
        "source_frame_sha256": observation.source_frame_sha256,
        "observed_turn": observation.observed_turn,
        "screen": observation.screen,
        "goal_kind": observation.goal_kind,
        "grade": observation.grade,
        "required_rank": observation.required_rank,
        "required_count": observation.required_count,
        "completed_count": observation.completed_count,
        "remaining_turns": observation.remaining_turns,
        "cutoff_turn": observation.cutoff_turn,
        "confidence": observation.confidence,
        "field_evidence": {
            key: dict(value)
            for key, value in observation.field_evidence.items()
        },
        "abstain_reasons": list(observation.abstain_reasons),
        "observation_sha256": observation.observation_sha256,
        "is_complete": observation.is_complete,
    }
    if observation.target_race_id is not None:
        payload["target_race_id"] = observation.target_race_id
    if observation.candidate_race_ids:
        payload["candidate_race_ids"] = list(observation.candidate_race_ids)
    if observation.progress_basis is not None:
        payload["progress_basis"] = observation.progress_basis
    return payload


def perceive_goal_page(
    image_path: str | Path,
    *,
    visible_text: str,
    source_frame_sha256: str,
    observed_turn: int,
    text_order_verified: bool = False,
    catalog_path: str | Path = _CATALOG_PATH,
) -> CareerGoalObservation:
    if type(observed_turn) is not int or observed_turn <= 0:
        raise CareerGoalPerceptionError("observed_turn_invalid")
    if type(visible_text) is not str:
        raise CareerGoalPerceptionError("visible_text_invalid")
    if (
        type(source_frame_sha256) is not str
        or _SHA256_RE.fullmatch(source_frame_sha256) is None
    ):
        raise CareerGoalPerceptionError("source_frame_sha256_invalid")
    source = Path(image_path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise CareerGoalPerceptionError("source_frame_unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != source_frame_sha256:
        raise CareerGoalPerceptionError("source_frame_sha256_mismatch")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CareerGoalPerceptionError("source_frame_decode_failed")

    normalized_text = re.sub(r"\s+", "", visible_text)
    layout = _measure_goal_layout(image)
    field_evidence: dict[str, Mapping[str, object]] = {
        "source_frame": MappingProxyType(
            {
                "sha256": source_frame_sha256,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
            }
        ),
        "layout": MappingProxyType(layout),
        "visible_text": MappingProxyType(
            {
                "normalized_sha256": hashlib.sha256(
                    normalized_text.encode("utf-8")
                ).hexdigest(),
                "character_count": len(normalized_text),
            }
        ),
    }
    if not layout["verified"]:
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="unknown",
            field_evidence=field_evidence,
            abstain_reasons=("goal_layout_not_verified",),
        )

    text_lines = tuple(
        re.sub(r"\s+", "", line)
        for line in visible_text.splitlines()
        if line.strip()
    )
    rating_scores = tuple(
        int(line.replace(",", ""))
        for line in text_lines
        if re.fullmatch(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,6})", line)
    )
    if (
        text_lines
        and text_lines[0] == "养成等级"
        and text_lines.count("评分") == 1
        and text_lines[-1] == "下一页"
        and len(rating_scores) == 1
        and 1 <= rating_scores[0] <= 999999
        and "目标" not in normalized_text
        and _NEXT_GOAL_MARKER_RE.search(normalized_text) is None
        and _GOAL_REQUIREMENT_RE.search(normalized_text) is None
        and _REMAINING_RE.search(normalized_text) is None
    ):
        field_evidence["final_training_rating"] = MappingProxyType(
            {"excluded_from_goal_perception": True, "score": rating_scores[0]}
        )
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="unknown",
            field_evidence=field_evidence,
            abstain_reasons=("final_training_rating_not_goal",),
        )

    if (
        _FAILURE_TERM in normalized_text
        and float(layout["top_blue_ratio"]) >= 0.03
    ):
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_failure_summary",
            field_evidence=field_evidence,
            abstain_reasons=("goal_failure_summary_evidence_only",),
        )

    remaining_match = _REMAINING_RE.search(normalized_text)
    requirement_count = len(_GOAL_REQUIREMENT_RE.findall(normalized_text))
    catalog = _load_catalog(Path(catalog_path))
    named_entries = tuple(entry for entry in catalog if entry["goal_kind"] == "named_race_rank")
    if (
        _NEXT_GOAL_MARKER_RE.search(normalized_text)
        or (remaining_match is not None and requirement_count >= 2)
    ) and any(
        re.search(str(entry["text_pattern"]), normalized_text) for entry in named_entries
    ):
        spatial = _perceive_spatial_named_goal(
            image, normalized_text=normalized_text,
            entries=tuple(entry for entry in catalog if "goal_achievement_result" in entry["supported_screens"]),
            source_frame_sha256=source_frame_sha256, observed_turn=observed_turn,
            field_evidence=field_evidence,
        )
        if (
            spatial.abstain_reasons
            and remaining_match is not None
            and requirement_count >= 2
        ):
            field_evidence["multi_goal_summary"] = MappingProxyType(
                {
                    "remaining_marker_before_goal_text": True,
                    "independent_next_goal_marker": (
                        _NEXT_GOAL_MARKER_RE.search(normalized_text) is not None
                    ),
                    "goal_requirement_count": requirement_count,
                    "spatial_binding_reason": spatial.abstain_reasons[0],
                }
            )
            return _make_observation(
                source_frame_sha256=source_frame_sha256,
                observed_turn=observed_turn,
                screen="goal_achievement_result",
                field_evidence=field_evidence,
                abstain_reasons=("goal_remaining_unbound_multi_goal_summary",),
            )
        return spatial
    goal_match: tuple[Mapping[str, object], re.Match[str]] | None = None
    for entry in catalog:
        if entry["goal_kind"] != "graded_race_rank_count":
            continue
        match = re.search(str(entry["text_pattern"]), normalized_text)
        if match is not None:
            goal_match = (entry, match)
            break
    if goal_match is None and remaining_match is not None and requirement_count >= 2:
        field_evidence["multi_goal_summary"] = MappingProxyType(
            {
                "remaining_marker_before_goal_text": None,
                "independent_next_goal_marker": False,
                "goal_requirement_count": requirement_count,
                "catalog_goal_match": False,
            }
        )
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_achievement_result",
            field_evidence=field_evidence,
            abstain_reasons=("goal_remaining_unbound_multi_goal_summary",),
        )
    if goal_match is None or remaining_match is None:
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_achievement_result",
            field_evidence=field_evidence,
            abstain_reasons=("goal_text_unsupported",),
        )

    entry, match = goal_match
    grade_term = match.group("grade_term")
    grade = entry["grade_terms"].get(grade_term)
    if type(grade) is not str:
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_achievement_result",
            field_evidence=field_evidence,
            abstain_reasons=("goal_grade_unsupported",),
        )
    if text_order_verified is not True:
        field_evidence["text_order"] = MappingProxyType({"verified": False})
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_achievement_result",
            field_evidence=field_evidence,
            abstain_reasons=("goal_text_order_unverified",),
        )
    independent_next_marker = _NEXT_GOAL_MARKER_RE.search(normalized_text)
    if (
        remaining_match.start() < match.start()
        or independent_next_marker is not None
        or requirement_count >= 2
    ):
        field_evidence["multi_goal_summary"] = MappingProxyType(
            {
                "remaining_marker_before_goal_text": (
                    remaining_match.start() < match.start()
                ),
                "independent_next_goal_marker": (
                    independent_next_marker is not None
                ),
                "goal_requirement_count": requirement_count,
            }
        )
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_achievement_result",
            field_evidence=field_evidence,
            abstain_reasons=("goal_remaining_unbound_multi_goal_summary",),
        )
    required_count = int(match.group("required_count"))
    required_rank = int(match.group("required_rank"))
    remaining_turns = int(remaining_match.group("count"))
    if (
        required_count <= 0
        or required_rank <= 0
        or remaining_turns < 0
    ):
        return _make_observation(
            source_frame_sha256=source_frame_sha256,
            observed_turn=observed_turn,
            screen="goal_achievement_result",
            field_evidence=field_evidence,
            abstain_reasons=("goal_numeric_field_invalid",),
        )
    field_evidence["goal_text"] = MappingProxyType(
        {
            "goal_kind": entry["goal_kind"],
            "grade_term": grade_term,
            "required_count": required_count,
            "required_rank": required_rank,
        }
    )
    field_evidence["remaining_turns"] = MappingProxyType(
        {
            "value": remaining_turns,
            "cutoff_turn": None,
            "deadline_basis": "pending_calendar_main_menu_anchor",
        }
    )
    return _make_observation(
        source_frame_sha256=source_frame_sha256,
        observed_turn=observed_turn,
        screen="goal_achievement_result",
        goal_kind=str(entry["goal_kind"]),
        grade=grade,
        required_rank=required_rank,
        required_count=required_count,
        # The goal-achievement page announces the *next* career goal.  The
        # numeric requirement describes work still to do; it is not progress
        # already earned.  Progress is added only by verified race results.
        completed_count=0,
        remaining_turns=remaining_turns,
        cutoff_turn=None,
        confidence=1.0,
        field_evidence=field_evidence,
        abstain_reasons=("goal_calendar_unbound",),
    )


def is_goal_achievement_title(title: object, *, include_career_complete: bool = False) -> bool:
    titles = {"目标达成", "自标达成"}
    if include_career_complete:
        titles.add("养成目标达成")
    return type(title) is str and title.strip().rstrip("!！。:：").strip() in titles


def match_main_menu_named_goal(goal_text: str) -> dict[str, object] | None:
    if goal_text.splitlines() != [goal_text]:
        return None
    goal_text = "".join(goal_text.split())
    matches = []
    for entry in _load_catalog(_CATALOG_PATH):
        if entry["goal_kind"] != "named_race_rank" or "main_menu" not in entry["supported_screens"]:
            continue
        match = re.fullmatch(str(entry["text_pattern"]), goal_text)
        if match is not None and int(match.group("required_rank")) > 0:
            matches.append({
                "goal_kind": "named_race_rank", "grade": entry["grade"],
                "target_race_id": entry.get("target_race_id"),
                "required_rank": int(match.group("required_rank")),
                **({"candidate_race_ids": list(entry["candidate_race_ids"])}
                   if "candidate_race_ids" in entry else {}),
            })
    return matches[0] if len(matches) == 1 else None


def match_main_menu_count_goal(goal_text: str) -> dict[str, object] | None:
    if type(goal_text) is not str or goal_text.splitlines() != [goal_text]:
        return None
    goal_text = "".join(goal_text.split())
    matches = []
    for entry in _load_catalog(_CATALOG_PATH):
        if entry["goal_kind"] != "graded_race_rank_count" or "main_menu" not in entry["supported_screens"]:
            continue
        match = re.fullmatch(str(entry["text_pattern"]), goal_text)
        if match is None:
            continue
        grade = entry["grade_terms"].get(match.group("grade_term"))
        rank, count = int(match.group("required_rank")), int(match.group("required_count"))
        if grade is not None and rank > 0 and count > 0:
            matches.append({"goal_kind": "graded_race_rank_count", "grade": grade,
                            "required_rank": rank, "required_count": count})
    return matches[0] if len(matches) == 1 else None


def _perceive_spatial_named_goal(
    image: np.ndarray,
    *,
    normalized_text: str,
    entries: tuple[Mapping[str, object], ...],
    source_frame_sha256: str,
    observed_turn: int,
    field_evidence: dict[str, Mapping[str, object]],
) -> CareerGoalObservation:
    def abstain(reason: str) -> CareerGoalObservation:
        return _make_observation(
            source_frame_sha256=source_frame_sha256, observed_turn=observed_turn,
            screen="goal_achievement_result", field_evidence=field_evidence,
            abstain_reasons=(reason,),
        )

    canonical = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
    yellow = cv2.inRange(hsv, np.array((15, 30, 180)), np.array((40, 160, 255)))
    _, _, stats, _ = cv2.connectedComponentsWithStats(yellow)
    cards = [
        (int(x), int(y), int(x + w), int(y + h))
        for x, y, w, h, area in stats[1:]
        if x <= 80 and x + w >= 640 and 250 <= y <= 950
        and 45 <= h <= 110 and area / (w * h) >= 0.65
    ]
    if len(cards) != 1:
        return abstain("goal_highlight_not_unique")
    card = cards[0]
    regions: list[tuple[tuple[float, float, float, float], str]] = []
    try:
        for box, text, confidence in _GOAL_REGION_READER.read_regions(image):
            points = np.asarray(box, dtype=float)
            if points.shape != (4, 2) or not np.isfinite(points).all():
                continue
            region_text = re.sub(r"\s+", "", str(text))
            minimum_confidence = 0.80 if _NEXT_GOAL_MARKER_RE.fullmatch(region_text) else 0.85
            if not np.isfinite(float(confidence)) or float(confidence) < minimum_confidence:
                continue
            points *= np.array((720 / image.shape[1], 1280 / image.shape[0]))
            bbox = (float(points[:, 0].min()), float(points[:, 1].min()),
                    float(points[:, 0].max()), float(points[:, 1].max()))
            if not (0 <= bbox[0] < bbox[2] <= 720 and 0 <= bbox[1] < bbox[3] <= 1280):
                continue
            regions.append((bbox, region_text))
    except Exception:
        return abstain("goal_spatial_ocr_failed")
    markers = [(box, text) for box, text in regions if _NEXT_GOAL_MARKER_RE.fullmatch(text)]
    countdowns = [(box, _REMAINING_RE.search(text)) for box, text in regions
                  if _REMAINING_RE.search(text)]
    if len(markers) != 1 or len(countdowns) != 1:
        return abstain("goal_spatial_marker_not_unique")
    marker, _ = markers[0]
    countdown, remaining = countdowns[0]
    if not (
        card[0] <= marker[0] < marker[2] <= card[2]
        and card[1] - 45 <= marker[1] < marker[3] <= card[1] + 5
        and 100 <= countdown[0] < countdown[2] <= 700
        and 120 <= countdown[1] < countdown[3] <= 240
        and countdown[3] < marker[1]
    ):
        return abstain("goal_spatial_marker_unbound")
    contained = [(box, text) for box, text in regions
                 if card[0] <= box[0] < box[2] <= card[2]
                 and card[1] <= box[1] < box[3] <= card[3]]
    requirements = [(box, text) for box, text in contained if _GOAL_REQUIREMENT_RE.search(text)]
    if len(requirements) != 1:
        return abstain("goal_current_row_not_unique")
    goal_box, goal_text = requirements[0]
    matches = [(entry, re.fullmatch(str(entry["text_pattern"]), goal_text)) for entry in entries]
    matches = [(entry, match) for entry, match in matches if match is not None]
    if len(matches) != 1:
        return abstain("goal_current_row_unsupported")
    entry, match = matches[0]
    remaining_values = [int(item.group("count")) for item in _REMAINING_RE.finditer(normalized_text)]
    remaining_turns = int(remaining.group("count"))
    required_rank = int(match.group("required_rank"))
    if remaining_values != [remaining_turns] or goal_text not in normalized_text:
        return abstain("goal_spatial_text_conflict")
    if required_rank <= 0:
        return abstain("goal_numeric_field_invalid")
    is_count = entry["goal_kind"] == "graded_race_rank_count"
    required_count = int(match.group("required_count")) if is_count else 1
    if required_count <= 0:
        return abstain("goal_numeric_field_invalid")
    field_evidence["current_goal_binding"] = MappingProxyType({
        "verified": True, "source_frame_sha256": source_frame_sha256,
        "highlight_bbox": list(card), "marker_bbox": list(marker),
        "goal_bbox": list(goal_box), "remaining_bbox": list(countdown),
        "goal_text": goal_text, "target_race_id": entry.get("target_race_id"),
        **({"candidate_race_ids": list(entry["candidate_race_ids"])}
           if "candidate_race_ids" in entry else {}),
    })
    field_evidence["remaining_turns"] = MappingProxyType({
        "value": remaining_turns, "cutoff_turn": None,
        "deadline_basis": "remaining_only_no_verified_calendar_turn",
    })
    if is_count:
        field_evidence["completed_count"] = MappingProxyType({
            "value": 0, "progress_basis": "verified_lower_bound",
            "actual_progress": "unknown",
        })
    return _make_observation(
        source_frame_sha256=source_frame_sha256, observed_turn=observed_turn,
        screen="goal_achievement_result", goal_kind=str(entry["goal_kind"]),
        grade=str(entry["grade_terms"][match.group("grade_term")] if is_count else entry["grade"]),
        target_race_id=entry.get("target_race_id"),
        candidate_race_ids=tuple(entry.get("candidate_race_ids", ())),
        required_rank=required_rank, required_count=required_count, completed_count=0,
        remaining_turns=remaining_turns, cutoff_turn=None, confidence=1.0,
        field_evidence=field_evidence, abstain_reasons=(),
        progress_basis="verified_lower_bound" if is_count else None,
    )


def _measure_goal_layout(image: np.ndarray) -> dict[str, object]:
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        return {"verified": False, "reason": "empty_image"}
    scale = width / 720.0
    if not (0.9 <= height / width / (16.0 / 9.0) <= 1.1):
        return {"verified": False, "reason": "aspect_ratio_mismatch"}
    resized = cv2.resize(
        image,
        (720, 1280),
        interpolation=cv2.INTER_AREA if scale >= 1.0 else cv2.INTER_LINEAR,
    )
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    top = hsv[70:210, :]
    panel = hsv[120:1000, :]
    bottom = hsv[1050:1240, :]
    top_blue_ratio = _mask_ratio(
        top,
        lower=(90, 70, 80),
        upper=(130, 255, 255),
    )
    top_white_ratio = _mask_ratio(
        top,
        lower=(0, 0, 200),
        upper=(179, 60, 255),
    )
    panel_white_ratio = _mask_ratio(
        panel,
        lower=(0, 0, 200),
        upper=(179, 60, 255),
    )
    bottom_green_ratio = _mask_ratio(
        bottom,
        lower=(35, 80, 80),
        upper=(95, 255, 255),
    )
    verified = (
        top_white_ratio >= 0.70
        and panel_white_ratio >= 0.45
        and bottom_green_ratio >= 0.10
    )
    return {
        "verified": verified,
        "canonical_width": 720,
        "canonical_height": 1280,
        "top_blue_ratio": round(top_blue_ratio, 6),
        "top_white_ratio": round(top_white_ratio, 6),
        "panel_white_ratio": round(panel_white_ratio, 6),
        "bottom_green_ratio": round(bottom_green_ratio, 6),
    }


def _mask_ratio(
    hsv: np.ndarray,
    *,
    lower: tuple[int, int, int],
    upper: tuple[int, int, int],
) -> float:
    mask = cv2.inRange(
        hsv,
        np.asarray(lower, dtype=np.uint8),
        np.asarray(upper, dtype=np.uint8),
    )
    return float(np.mean(mask > 0))


def _load_catalog(path: Path) -> tuple[Mapping[str, object], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CareerGoalPerceptionError("goal_catalog_invalid") from exc
    if type(payload) is not dict or frozenset(payload) != _CATALOG_KEYS:
        raise CareerGoalPerceptionError("goal_catalog_invalid")
    if payload.get("schema_version") != "career_goal_catalog.v1":
        raise CareerGoalPerceptionError("goal_catalog_invalid")
    if type(payload.get("catalog_id")) is not str or not payload["catalog_id"]:
        raise CareerGoalPerceptionError("goal_catalog_invalid")
    rows = payload.get("entries")
    if type(rows) is not list or not rows:
        raise CareerGoalPerceptionError("goal_catalog_invalid")
    result: list[Mapping[str, object]] = []
    goal_kinds: set[str] = set()
    named_ids: set[str] = set()
    named_patterns: set[str] = set()
    for row in rows:
        if type(row) is dict and row.get("goal_kind") == "named_race_rank":
            candidates = row.get("candidate_race_ids")
            candidate_keys = (_NAMED_CATALOG_ENTRY_KEYS - {"target_race_id"}) | {"candidate_race_ids"}
            ids = candidates if candidates is not None else [row.get("target_race_id")]
            if (
                frozenset(row) not in (_NAMED_CATALOG_ENTRY_KEYS, candidate_keys)
                or any(type(row.get(key)) is not str or not row[key]
                       for key in ("grade", "text_pattern"))
                or type(ids) is not list or not ids
                or any(type(item) is not str or not item for item in ids)
                or len(set(ids)) != len(ids)
                or named_ids.intersection(ids)
                or row["text_pattern"] in named_patterns
                or row.get("supported_screens") not in (
                    ["goal_achievement_result"], ["goal_achievement_result", "main_menu"],
                )
            ):
                raise CareerGoalPerceptionError("goal_catalog_invalid")
            try:
                compiled = re.compile(row["text_pattern"])
                if "required_rank" not in compiled.groupindex:
                    raise ValueError("missing rank")
            except (re.error, ValueError) as exc:
                raise CareerGoalPerceptionError("goal_catalog_invalid") from exc
            named_ids.update(ids)
            named_patterns.add(row["text_pattern"])
            result.append(MappingProxyType(dict(row)))
            continue
        if type(row) is not dict or frozenset(row) != _CATALOG_ENTRY_KEYS:
            raise CareerGoalPerceptionError("goal_catalog_invalid")
        goal_kind = row.get("goal_kind")
        grade_terms = row.get("grade_terms")
        text_pattern = row.get("text_pattern")
        screens = row.get("supported_screens")
        if (
            type(goal_kind) is not str
            or not goal_kind
            or goal_kind in goal_kinds
            or type(grade_terms) is not dict
            or not grade_terms
            or any(
                type(key) is not str
                or not key
                or type(value) is not str
                or not value
                for key, value in grade_terms.items()
            )
            or type(text_pattern) is not str
            or not text_pattern
            or type(screens) is not list
            or (screens != ["goal_achievement_result"] and not (
                goal_kind == "graded_race_rank_count"
                and screens == ["goal_achievement_result", "main_menu"]
            ))
        ):
            raise CareerGoalPerceptionError("goal_catalog_invalid")
        try:
            re.compile(text_pattern)
        except re.error as exc:
            raise CareerGoalPerceptionError("goal_catalog_invalid") from exc
        goal_kinds.add(goal_kind)
        result.append(MappingProxyType(dict(row)))
    return tuple(result)


def _make_observation(
    *,
    source_frame_sha256: str,
    observed_turn: int,
    screen: str,
    field_evidence: Mapping[str, Mapping[str, object]],
    abstain_reasons: tuple[str, ...],
    goal_kind: str | None = None,
    grade: str | None = None,
    required_rank: int | None = None,
    required_count: int | None = None,
    completed_count: int | None = None,
    remaining_turns: int | None = None,
    cutoff_turn: int | None = None,
    confidence: float = 0.0,
    target_race_id: str | None = None,
    candidate_race_ids: tuple[str, ...] = (),
    progress_basis: str | None = None,
) -> CareerGoalObservation:
    canonical = {
        "schema_version": "career_goal_observation.v1",
        "source_frame_sha256": source_frame_sha256,
        "observed_turn": observed_turn,
        "screen": screen,
        "goal_kind": goal_kind,
        "grade": grade,
        "required_rank": required_rank,
        "required_count": required_count,
        "completed_count": completed_count,
        "remaining_turns": remaining_turns,
        "cutoff_turn": cutoff_turn,
        "confidence": confidence,
        "field_evidence": {
            key: dict(value) for key, value in sorted(field_evidence.items())
        },
        "abstain_reasons": list(abstain_reasons),
    }
    if target_race_id is not None:
        canonical["target_race_id"] = target_race_id
    if candidate_race_ids:
        canonical["candidate_race_ids"] = list(candidate_race_ids)
    if progress_basis is not None:
        canonical["progress_basis"] = progress_basis
    observation_sha256 = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return CareerGoalObservation(
        schema_version="career_goal_observation.v1",
        source_frame_sha256=source_frame_sha256,
        observed_turn=observed_turn,
        screen=screen,
        goal_kind=goal_kind,
        grade=grade,
        required_rank=required_rank,
        required_count=required_count,
        completed_count=completed_count,
        remaining_turns=remaining_turns,
        cutoff_turn=cutoff_turn,
        confidence=confidence,
        field_evidence=MappingProxyType(dict(field_evidence)),
        abstain_reasons=abstain_reasons,
        observation_sha256=observation_sha256,
        target_race_id=target_race_id,
        candidate_race_ids=candidate_race_ids,
        progress_basis=progress_basis,
    )
