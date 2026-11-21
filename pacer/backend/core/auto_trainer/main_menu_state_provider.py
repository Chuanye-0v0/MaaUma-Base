from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from core.main_menu_state import (
    HIGH_CONFIDENCE, OPTIONAL_DECISION_FIELDS, REQUIRED_FOR_NUMERIC_STATE, SUPPORTED_SIZES,
    analyze_main_menu_state, field_confidence_accepted, _is_valid_for_training,
    _normalize_main_menu_image, _numeric_fields_complete,
)
from core.auto_trainer.career_goal_perception import (
    _GOAL_REGION_READER as _CALENDAR_REGION_READER,
    match_main_menu_count_goal,
    match_main_menu_named_goal,
)
from core.auto_trainer.race_calendar import RaceCalendar, career_global_turn


SCHEMA_VERSION = "main_menu_state_context.v1"
TARGET_IDS = {"参加初级新人战": "participate_in_debut_race"}
STAT_FIELDS = OPTIONAL_DECISION_FIELDS


class MainMenuStateProviderError(RuntimeError):
    """Stable failure raised when a provider artifact cannot be safely written."""


def provide_main_menu_state(
    source_png_path: str | Path,
    output_path: str | Path,
    *,
    turn_index: int,
    scenario: str | None = "grand_masters",
    tracker: Any | None = None,
    consensus_png_paths: list[str | Path] | tuple[str | Path, ...] | None = None,
    race_calendar: RaceCalendar | None = None,
) -> dict[str, Any]:
    """Analyze one ready-main-menu PNG and atomically persist its state context."""
    source_path = Path(source_png_path)
    destination = Path(output_path)
    if _paths_conflict(source_path, destination):
        raise MainMenuStateProviderError("source_output_path_conflict")

    if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
        raise MainMenuStateProviderError("main_menu_state_provider_turn_index_invalid")

    try:
        png_bytes = source_path.read_bytes()
    except OSError as error:
        raise MainMenuStateProviderError(
            "main_menu_state_provider_read_failed"
        ) from error

    actual_sha256 = hashlib.sha256(png_bytes).hexdigest()
    observation = _analyze_for_scenario(png_bytes, scenario)
    calendar_anchor = (
        _build_career_calendar_anchor(png_bytes, observation, race_calendar)
        if race_calendar is not None and scenario == "grand_masters" else None
    )
    observation, consensus = _apply_ready_main_menu_consensus(
        source_path=source_path,
        source_sha256=actual_sha256,
        observation=observation,
        consensus_png_paths=consensus_png_paths,
        scenario=scenario,
    )
    tracker_observation = tracker.observe(observation) if tracker is not None else None
    report = _build_report(
        source_path=source_path,
        png_bytes=png_bytes,
        actual_sha256=actual_sha256,
        observation=observation,
        turn_index=turn_index,
        scenario=scenario,
        tracker_observation=tracker_observation,
        consensus=consensus,
    )
    if calendar_anchor is not None:
        calendar_anchor["goal_achieved_marker"] = _read_goal_achieved_marker(
            png_bytes, calendar_anchor,
        )
        report["career_calendar_anchor"] = calendar_anchor
        if report["state_context"] is not None:
            report["state_context"]["scenario_state"]["career_calendar_anchor"] = copy.deepcopy(calendar_anchor)
    if scenario == "larc":
        period = read_main_menu_date(png_bytes, observation, allow_pre_debut=True)
        report["larc_period"] = period
        if report["state_context"] is not None:
            report["state_context"]["scenario_state"]["larc_period"] = copy.deepcopy(period)
            from core.larc_growth_reader import read_menu_growth
            image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            growth = read_menu_growth(image, turn_index,
                period=period.get("value") if period.get("status") == "verified" else None)
            growth["source_frame_sha256"] = actual_sha256
            report["state_context"]["scenario_state"]["larc_growth_observation"] = growth
    _atomic_write_json(destination, report)
    return report


def _analyze_for_scenario(png_bytes: bytes, scenario: str | None) -> dict[str, Any]:
    if scenario == "grand_masters":
        return analyze_main_menu_state(png_bytes)
    return analyze_main_menu_state(png_bytes, scenario=scenario)


def _read_goal_achieved_marker(
    png_bytes: bytes, calendar_anchor: dict[str, Any],
) -> dict[str, Any]:
    """Verify the orange target-header phrase only after the same-frame anchor."""
    source_sha = hashlib.sha256(png_bytes).hexdigest()
    roi = [496, 104, 609, 132]
    marker: dict[str, Any] = {
        "status": "unverified", "verified": False, "present": False,
        "source_frame_sha256": source_sha, "roi": roi,
        "text": None, "confidence": None, "bbox": None, "color_evidence": {},
    }

    def reject(reason: str) -> dict[str, Any]:
        marker["reason"] = reason
        return marker

    if calendar_anchor.get("status") != "verified":
        return reject("goal_marker_anchor_unverified")
    if calendar_anchor.get("source_frame_sha256") != source_sha:
        return reject("goal_marker_source_sha_mismatch")
    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return reject("goal_marker_layout_unsupported")
    crop = image[roi[1]:roi[3], roi[0]:roi[2]]
    orange = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV),
                         np.array([5, 120, 140]), np.array([30, 255, 255]))
    orange_pixels = int(cv2.countNonZero(orange))
    orange_ratio = orange_pixels / orange.size
    marker["color_evidence"] = {
        "hsv_lower": [5, 120, 140], "hsv_upper": [30, 255, 255],
        "orange_pixels": orange_pixels, "orange_ratio": orange_ratio,
    }
    if orange_pixels < 100 or not 0.05 <= orange_ratio <= 0.50:
        return reject("goal_marker_orange_unverified")
    # The native short phrase is too small for stable OCR. Keep both the scale
    # and padding fixed, then map the text box back into the original ROI.
    enlarged = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    padded = cv2.copyMakeBorder(enlarged, 16, 16, 16, 16,
                               cv2.BORDER_CONSTANT, value=(255, 255, 255))
    try:
        regions = _CALENDAR_REGION_READER.read_regions(padded)
        if calendar_anchor.get("goal_kind") == "fan_count":
            shortfalls = []
            for box, text, confidence in regions:
                match = re.fullmatch(
                    r"([1-9][0-9,]*)人",
                    re.sub(r"\s+", "", str(text)),
                )
                points = np.asarray(box, dtype=float)
                if (
                    match is None
                    or not _confidence_is_valid(confidence)
                    or float(confidence) < 0.95
                    or points.shape != (4, 2)
                    or not np.isfinite(points).all()
                ):
                    continue
                mapped = (points - 16) / 2 + (roi[0], roi[1])
                bounds = [
                    float(mapped[:, 0].min()),
                    float(mapped[:, 1].min()),
                    float(mapped[:, 0].max()),
                    float(mapped[:, 1].max()),
                ]
                if not (
                    roi[0] - 4 <= bounds[0] < bounds[2] <= roi[2] + 4
                    and roi[1] - 4 <= bounds[1] < bounds[3] <= roi[3] + 4
                ):
                    continue
                bounds = [
                    max(float(roi[0]), bounds[0]),
                    max(float(roi[1]), bounds[1]),
                    min(float(roi[2]), bounds[2]),
                    min(float(roi[3]), bounds[3]),
                ]
                shortfalls.append(
                    (str(text), float(confidence), bounds, int(match.group(1).replace(",", "")))
                )
            if len(shortfalls) == 1:
                text, confidence, bounds, remaining_fans = shortfalls[0]
                marker.update(
                    status="verified",
                    verified=True,
                    present=False,
                    reason="fan_shortfall_verified",
                    text=text,
                    confidence=confidence,
                    bbox=bounds,
                    remaining_fans=remaining_fans,
                )
                return marker
        if len(regions) != 1:
            return reject("goal_marker_text_not_unique")
        box, text, confidence = regions[0]
        if (re.sub(r"\s+", "", str(text)).replace("！", "!") != "目标达成!"
            or not _confidence_is_valid(confidence) or float(confidence) < 0.95):
            return reject("goal_marker_text_unverified")
        points = np.asarray(box, dtype=float)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            return reject("goal_marker_bbox_unverified")
        points = (points - 16) / 2 + (roi[0], roi[1])
        bounds = [float(points[:, 0].min()), float(points[:, 1].min()),
                  float(points[:, 0].max()), float(points[:, 1].max())]
        if not (roi[0] <= bounds[0] < bounds[2] <= roi[2]
                and roi[1] <= bounds[1] < bounds[3] <= roi[3]):
            return reject("goal_marker_bbox_unverified")
    except Exception:
        return reject("goal_marker_ocr_failed")
    marker.update(status="verified", verified=True, present=True,
                  text=str(text), confidence=float(confidence), bbox=bounds)
    return marker


def _calendar_foreground_date(image: np.ndarray):
    """One bounded same-frame OCR variant; never guess unreadable dates."""
    roi = (155, 36, 345, 72)
    crop = image[roi[1]:roi[3], roi[0]:roi[2]]
    if crop.shape[:2] != (36, 190):
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # The local CN date glyphs are dark on a light header. Exclude the top
    # decorative edge, isolate the glyph bounds and remove excessive padding.
    mask = cv2.inRange(hsv, (0, 0, 0), (179, 255, 160))
    mask[:6] = 0
    points = cv2.findNonZero(mask)
    if points is None:
        return None
    x, y, width, height = cv2.boundingRect(points)
    if not (3 <= x and x+width < 188 and 60 <= width <= 185
            and 12 <= height <= 26 and y+height <= 34):
        return None
    density = float(np.mean(mask[y:y+height, x:x+width] > 0))
    if not .12 <= density <= .80:
        return None
    line = 255-mask[max(0,y-2):min(36,y+height+2), max(0,x-2):min(190,x+width+2)]
    line = cv2.resize(line, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    line = cv2.copyMakeBorder(line, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    try:
        text, confidence = _CALENDAR_REGION_READER.read_line(line)
    except Exception:
        return None
    text = re.sub(r"\s+", "", str(text))
    if (not _confidence_is_valid(confidence) or float(confidence) < .95
            or (text != "初级出道前" and _parse_header_date_value(text) is None)):
        return None
    return ([roi[0]+x, roi[1]+y, roi[0]+x+width, roi[1]+y+height], text, float(confidence))


def _read_main_menu_header(
    png_bytes: bytes,
    atomic_observation: dict[str, Any],
    *,
    require_remaining: bool,
    date_line_only: bool = False,
) -> dict[str, Any]:
    source_sha = hashlib.sha256(png_bytes).hexdigest()

    def reject(reason: str) -> dict[str, Any]:
        return {"status": "unverified", "reason": reason, "source_frame_sha256": source_sha}

    if atomic_observation.get("source", {}).get("sha256") != source_sha:
        return reject("anchor_source_sha_mismatch")
    fields = atomic_observation.get("fields", {})
    screen = fields.get("screen")
    if not _field_is_high_confidence(screen) or screen.get("value") != "main_menu":
        return reject("anchor_main_menu_unverified")
    if (
        atomic_observation.get("source", {}).get("size") != [720, 1280]
        or atomic_observation.get("normalization", {}).get("native") is not True
    ):
        return reject("anchor_layout_unsupported")
    remaining = _verified_remaining_turns(atomic_observation)
    if require_remaining and (remaining is None or not 0 <= remaining <= 99):
        return reject("anchor_remaining_unverified")
    image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (1280, 720):
        return reject("anchor_layout_unsupported")
    if date_line_only:
        roi = (155, 36, 345, 72)
        try:
            text, confidence = _CALENDAR_REGION_READER.read_line(image[roi[1]:roi[3], roi[0]:roi[2]])
            if (_confidence_is_valid(confidence) and confidence < 0.95
                    and re.sub(r"\s+", "", text) == "初级出道前"):
                # The short pre-debut label leaves excessive right padding in
                # the full calendar ROI. Verify its own crop at the same gate.
                roi = (155, 36, 280, 72)
                text, confidence = _CALENDAR_REGION_READER.read_line(image[roi[1]:roi[3], roi[0]:roi[2]])
                if re.sub(r"\s+", "", text) != "初级出道前":
                    return reject("anchor_date_unverified")
        except Exception:
            text, confidence = "", None  # Not evidence; try the other bounded reader.
        compact_date = re.sub(r"\s+", "", text)
        if _confidence_is_valid(confidence) and confidence >= 0.95 and (compact_date == "初级出道前"
                or _parse_header_date_value(compact_date) is not None):
            return {"status": "verified", "source_frame_sha256": source_sha,
                    "fields": fields, "remaining": remaining,
                    "slots": {"date": [(list(roi), re.sub(r"\s+", "", text), confidence)], "goal": []},
                    "date_evidence": {"date_reader": "rapidocr_rec_only"},
                    "goal_regions": [], "goal_raw_texts": []}
        foreground_date = _calendar_foreground_date(image)
        if foreground_date is not None:
            return {"status":"verified", "source_frame_sha256":source_sha,
                "fields":fields, "remaining":remaining,
                "slots":{"date":[foreground_date], "goal":[]},
                "date_evidence":{"date_reader":"rapidocr_date_foreground",
                    "foreground_threshold":160}, "goal_regions":[], "goal_raw_texts":[]}
        # Fixed crops can include background punctuation even at high OCR
        # confidence. Verify actual date syntax, then fall back to the same
        # frame's header detector with unchanged .95/ROI gates.
    slots: dict[str, list[tuple[list[float], str, float]]] = {"date": [], "goal": []}
    date_regions = []
    goal_raw_texts = []
    goal_regions = []
    try:
        # The provider has no pre-existing bound OCR regions. Read only this
        # header once, using the shared long-lived local reader. Numeric OCR
        # results are ignored: remaining comes from the same atomic frame.
        regions = _CALENDAR_REGION_READER.read_regions(image[32:120, 0:720])
        for box, text, confidence in regions:
            points = np.asarray(box, dtype=float)
            if (
                points.shape != (4, 2) or not np.isfinite(points).all()
                or not _confidence_is_valid(confidence) or float(confidence) < 0.95
            ):
                continue
            if not ((points >= (0, 0)).all() and (points <= (720, 88)).all()):
                continue
            points[:, 1] += 32
            bounds = [float(points[:, 0].min()), float(points[:, 1].min()),
                      float(points[:, 0].max()), float(points[:, 1].max())]
            for name, roi in (("date", (155, 36, 345, 72)), ("goal", (232, 72, 605, 113))):
                if roi[0] <= bounds[0] < bounds[2] <= roi[2] and roi[1] <= bounds[1] < bounds[3] <= roi[3]:
                    slots[name].append((bounds, re.sub(r"\s+", "", str(text)), float(confidence)))
                    if name == "date":
                        date_regions.append(copy.deepcopy([box, text, confidence]))
                    else:
                        goal_raw_texts.append(str(text))
                        goal_regions.append(copy.deepcopy([box, text, confidence]))
    except Exception:
        return reject("anchor_header_ocr_failed")
    split_date_evidence = {"date_reader": "rapidocr_regions"} if date_line_only else {}
    if not slots["date"] and not date_line_only:
        # The complete state/calendar path uses the same field fallback as
        # takeover/header-only reads. Ambiguous/conflicting boxes do not use it.
        foreground_date = _calendar_foreground_date(image)
        if foreground_date is not None:
            slots["date"] = [foreground_date]
            split_date_evidence = {"date_reader":"rapidocr_date_foreground",
                "foreground_threshold":160}
    if len(slots["date"]) == 2:
        ordered = sorted(zip(slots["date"], date_regions), key=lambda item: item[0][0][0])
        (left, left_raw), (right, right_raw) = ordered
        if (left_raw[1] not in {"初级", "经典级", "高级"}
            or re.fullmatch(r"([1-9]|1[0-2])月(上|下)半月", str(right_raw[1])) is None
            # RapidOCR boxes for adjacent date runs can overlap by a few
            # pixels even though the recognized strings and baseline agree.
            or left[0][2] - right[0][0] > 4.0
            or max(left[0][1], right[0][1]) >= min(left[0][3], right[0][3])):
            return reject("anchor_date_unverified")
        slots["date"] = [([left[0][0], min(left[0][1], right[0][1]), right[0][2], max(left[0][3], right[0][3])],
                          left[1] + right[1], min(left[2], right[2]))]
        split_date_evidence = {"split_date_regions": [left_raw, right_raw]}
    elif len(slots["date"]) != 1:
        return reject("anchor_date_unverified")
    return {
        "status": "verified", "source_frame_sha256": source_sha,
        "fields": fields, "remaining": remaining, "slots": slots,
        "date_evidence": split_date_evidence, "goal_regions": goal_regions,
        "goal_raw_texts": goal_raw_texts,
    }


def read_main_menu_date(
    png_bytes: bytes, observation: dict[str, Any], *, allow_pre_debut: bool = False,
) -> dict[str, Any]:
    """Read a same-frame calendar date, without inferring a goal or global turn."""
    header = _read_main_menu_header(png_bytes, observation, require_remaining=False,
                                    date_line_only=allow_pre_debut)
    if header["status"] != "verified":
        return header
    box, text, confidence = header["slots"]["date"][0]
    value = _parse_header_date_value(text)
    if allow_pre_debut and text == "初级出道前":
        value = {"stage": "junior", "year_index": 1, "pre_debut": True}
    if value is None:
        return {"status": "unverified", "reason": "anchor_date_unverified",
                "source_frame_sha256": header["source_frame_sha256"]}
    return {"status": "verified", "source_frame_sha256": header["source_frame_sha256"],
            "value": value,
            "evidence": {"header_roi": [0, 32, 720, 120], "date_bbox": box,
                         "date_text": text, "date_confidence": confidence,
                         **header["date_evidence"]}}


def _parse_header_date_value(text: str) -> dict[str, Any] | None:
    match = re.fullmatch(r"(初级|经典级|高级)([1-9]|1[0-2])月(上|下)半月", text)
    if match is None:
        return None
    stage, year = {"初级": ("junior", 1), "经典级": ("classic", 2), "高级": ("senior", 3)}[match[1]]
    return {"stage": stage, "year_index": year, "month": int(match[2]),
            "half": "first" if match[3] == "上" else "second"}


def _match_main_menu_fan_goal(goal_text: object) -> dict[str, object] | None:
    if type(goal_text) is not str or goal_text.splitlines() != [goal_text]:
        return None
    match = re.fullmatch(r"获得([1-9][0-9]*)名粉丝", "".join(goal_text.split()))
    if match is None:
        return None
    return {"goal_kind": "fan_count", "required_fans": int(match.group(1))}


def _build_career_calendar_anchor(
    png_bytes: bytes,
    atomic_observation: dict[str, Any],
    calendar: RaceCalendar,
) -> dict[str, Any]:
    header = _read_main_menu_header(png_bytes, atomic_observation, require_remaining=True)
    if header["status"] != "verified":
        return header
    source_sha = header["source_frame_sha256"]
    fields, remaining, slots = header["fields"], header["remaining"], header["slots"]
    split_date_evidence = header["date_evidence"]
    goal_raw_texts, goal_regions = header["goal_raw_texts"], header["goal_regions"]

    def reject(reason: str) -> dict[str, Any]:
        return {"status": "unverified", "reason": reason, "source_frame_sha256": source_sha}
    split_goal_evidence = {}
    if len(slots["goal"]) == 2:
        ordered = sorted(zip(slots["goal"], goal_regions), key=lambda item: item[0][0][0])
        (left, left_raw), (right, right_raw) = ordered
        if (any(not isinstance(raw[1], str) or raw[1].splitlines() != [raw[1]]
                for raw in (left_raw, right_raw))
            or left[0][2] > right[0][0]
            or not left[0][1] <= (right[0][1] + right[0][3]) / 2 <= left[0][3]
            or not right[0][1] <= (left[0][1] + left[0][3]) / 2 <= right[0][3]
            or (
                match_main_menu_named_goal(left[1] + right[1]) is None
                and _match_main_menu_fan_goal(left[1] + right[1]) is None
            )):
            return reject("anchor_goal_unverified")
        slots["goal"] = [([left[0][0], min(left[0][1], right[0][1]), right[0][2], max(left[0][3], right[0][3])],
                          left[1] + right[1], min(left[2], right[2]))]
        split_goal_evidence = {"split_goal_regions": [left_raw, right_raw]}
    elif len(slots["goal"]) != 1:
        return reject("anchor_goal_unverified")
    date_box, date_text, date_confidence = slots["date"][0]
    goal_box, goal_text, goal_confidence = slots["goal"][0]
    date_value = _parse_header_date_value(date_text)
    if date_value is None:
        return reject("anchor_date_unverified")
    stage, year = date_value["stage"], date_value["year_index"]
    month, half = date_value["month"], date_value["half"]
    turn = career_global_turn(stage=stage, year_index=year, month=month, half=half)
    goal = match_main_menu_named_goal(goal_text)
    if goal is None:
        fan_goal = _match_main_menu_fan_goal(goal_text)
        if fan_goal is not None:
            return {
                "status": "verified", "basis": "same_frame_main_menu_header",
                "source_frame_sha256": source_sha,
                "career_time": {"stage": stage, "year_index": year, "month": month,
                                "half": half, "global_turn": turn},
                "remaining_turns": remaining, **fan_goal,
                "deadline_turn": turn + remaining,
                "evidence": {
                    "header_roi": [0, 32, 720, 120], "date_bbox": date_box,
                    "goal_bbox": goal_box, "date_text": date_text,
                    "goal_text": goal_text, "date_confidence": date_confidence,
                    "goal_confidence": goal_confidence,
                    "remaining_field_evidence": copy.deepcopy(fields["remaining_turns"]),
                    **split_date_evidence,
                    **split_goal_evidence,
                },
            }
        count_goal = match_main_menu_count_goal(goal_raw_texts[0])
        if count_goal is None:
            return reject("anchor_goal_unsupported")
        return {
            "status": "verified", "basis": "same_frame_main_menu_header",
            "source_frame_sha256": source_sha,
            "career_time": {"stage": stage, "year_index": year, "month": month,
                            "half": half, "global_turn": turn},
            "remaining_turns": remaining, **count_goal, "deadline_turn": turn + remaining,
            "evidence": {
                "header_roi": [0, 32, 720, 120], "date_bbox": date_box, "goal_bbox": goal_box,
                "date_text": date_text, "goal_text": goal_raw_texts[0],
                "date_confidence": date_confidence, "goal_confidence": goal_confidence,
                "remaining_field_evidence": copy.deepcopy(fields["remaining_turns"]),
                **split_date_evidence,
            },
        }
    if goal.get("candidate_race_ids"):
        matches = [calendar.get(race_id) for race_id in goal["candidate_race_ids"]]
        matches = [entry for entry in matches if entry is not None
                   and entry.schedule_status == "verified" and entry.career_time is not None
                   and entry.grade == goal["grade"]
                   and entry.career_time.global_turn == turn + remaining]
        if len(matches) != 1:
            return reject("anchor_target_date_conflict")
        goal = {**goal, "target_race_id": matches[0].race_id}
    entry = calendar.get(str(goal["target_race_id"]))
    if entry is None or entry.schedule_status != "verified" or entry.career_time is None:
        return reject("anchor_race_unverified")
    if turn + remaining != entry.career_time.global_turn:
        return reject("anchor_target_date_conflict")
    return {
        "status": "verified", "basis": "same_frame_main_menu_header",
        "source_frame_sha256": source_sha,
        "career_time": {"stage": stage, "year_index": year, "month": month,
                        "half": half, "global_turn": turn},
        "remaining_turns": remaining, **goal,
        "target_calendar_turn": entry.career_time.global_turn,
        "evidence": {
            "header_roi": [0, 32, 720, 120], "date_bbox": date_box, "goal_bbox": goal_box,
            "date_text": date_text, "goal_text": goal_text,
            "date_confidence": date_confidence, "goal_confidence": goal_confidence,
            "remaining_field_evidence": copy.deepcopy(fields["remaining_turns"]),
            **split_date_evidence,
            **split_goal_evidence,
        },
    }


def _paths_conflict(source: Path, destination: Path) -> bool:
    if source.exists() and destination.exists():
        try:
            return os.path.samefile(source, destination)
        except OSError:
            pass
    return os.path.normcase(str(source.resolve(strict=False))) == os.path.normcase(
        str(destination.resolve(strict=False))
    )


def _apply_ready_main_menu_consensus(
    *,
    source_path: Path,
    source_sha256: str,
    observation: dict[str, Any],
    consensus_png_paths: list[str | Path] | tuple[str | Path, ...] | None,
    scenario: str | None = "grand_masters",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not consensus_png_paths:
        return observation, None

    sources = [{"path": str(source_path), "sha256": source_sha256}]
    observations = [observation]
    seen_sha256 = {source_sha256}
    for raw_path in consensus_png_paths:
        path = Path(raw_path)
        try:
            png_bytes = path.read_bytes()
        except OSError as error:
            raise MainMenuStateProviderError(
                "main_menu_state_provider_consensus_read_failed"
            ) from error
        sha256 = hashlib.sha256(png_bytes).hexdigest()
        sources.append({"path": str(path), "sha256": sha256})
        if sha256 in seen_sha256:
            return _reject_consensus(observation, sources, "source_sha256_not_independent")
        seen_sha256.add(sha256)
        observations.append(_analyze_for_scenario(png_bytes, scenario))

    layout = _ready_main_menu_layout(observations[0])
    remaining_turns = _verified_remaining_turns(observations[0])
    if layout is None:
        return _reject_consensus(observation, sources, "primary_not_verified_ready_main_menu")
    if remaining_turns is None:
        return _reject_consensus(observation, sources, "primary_remaining_turns_unverified")

    for candidate in observations[1:]:
        if _ready_main_menu_layout(candidate) != layout:
            return _reject_consensus(observation, sources, "screen_or_layout_mismatch")
        if _verified_remaining_turns(candidate) != remaining_turns:
            return _reject_consensus(observation, sources, "remaining_turns_conflict")

    merged = copy.deepcopy(observation)
    fields = merged.get("fields")
    if not isinstance(fields, dict):
        return _reject_consensus(observation, sources, "primary_fields_invalid")
    merged_fields: list[str] = []
    field_names = set().union(
        *(item.get("fields", {}).keys() for item in observations if isinstance(item.get("fields"), dict))
    )
    for name in sorted(field_names):
        candidates = [
            item.get("fields", {}).get(name)
            for item in observations
            if isinstance(item.get("fields"), dict)
        ]
        confident = [item for item in candidates if _field_is_high_confidence(item)]
        if not confident:
            continue
        reference = confident[0].get("value")
        if any(not _values_match(reference, item.get("value")) for item in confident[1:]):
            return _reject_consensus(observation, sources, f"field_conflict:{name}")
        if len(confident) != len(observations):
            continue
        chosen = max(confident, key=lambda item: float(item["confidence"]))
        evidence = copy.deepcopy(chosen.get("evidence", {}))
        if not isinstance(evidence, dict):
            evidence = {}
        evidence["ready_main_menu_consensus"] = {
            "source_sha256": [item["sha256"] for item in sources],
            "source_count": len(sources),
            "value_agreement": True,
        }
        chosen = copy.deepcopy(chosen)
        chosen["evidence"] = evidence
        fields[name] = chosen
        merged_fields.append(name)

    merged["numeric_state_complete"] = _numeric_fields_complete(fields)
    merged["valid_for_training"] = (_is_valid_for_training(fields) if scenario == "larc"
                                    else merged["numeric_state_complete"])
    return merged, {
        "status": "accepted",
        "source_count": len(sources),
        "sources": sources,
        "layout": layout,
        "remaining_turns": remaining_turns,
        "merged_fields": merged_fields,
    }


def _reject_consensus(
    observation: dict[str, Any], sources: list[dict[str, str]], reason: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    rejected = copy.deepcopy(observation)
    rejected.update(valid_for_training=False, numeric_state_complete=False)
    errors = rejected.get("errors")
    if not isinstance(errors, list):
        errors = []
        rejected["errors"] = errors
    errors.append(f"ready_main_menu_consensus_{reason}")
    return rejected, {
        "status": "rejected",
        "reason": reason,
        "source_count": len(sources),
        "sources": sources,
    }


def _ready_main_menu_layout(observation: dict[str, Any]) -> dict[str, Any] | None:
    fields = observation.get("fields")
    if not isinstance(fields, dict):
        return None
    screen = fields.get("screen")
    if not _field_is_high_confidence(screen) or screen.get("value") != "main_menu":
        return None
    source = observation.get("source")
    normalization = observation.get("normalization")
    canonical_size = observation.get("canonical_size")
    if not isinstance(source, dict) or not isinstance(normalization, dict):
        return None
    size = source.get("size")
    if not isinstance(size, list) or not isinstance(canonical_size, list):
        return None
    mode = normalization.get("mode")
    scale = normalization.get("scale")
    if not isinstance(mode, str) or not isinstance(scale, list):
        return None
    return {
        "source_size": copy.deepcopy(size),
        "canonical_size": copy.deepcopy(canonical_size),
        "normalization_mode": mode,
        "normalization_scale": copy.deepcopy(scale),
    }


def _verified_remaining_turns(observation: dict[str, Any]) -> int | None:
    fields = observation.get("fields")
    if not isinstance(fields, dict):
        return None
    remaining = fields.get("remaining_turns")
    if not _field_is_high_confidence(remaining):
        return None
    value = remaining.get("value")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _field_is_high_confidence(field: Any) -> bool:
    return field_confidence_accepted(field)


def _values_match(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def _build_report(
    *,
    source_path: Path,
    png_bytes: bytes,
    actual_sha256: str,
    observation: dict[str, Any],
    turn_index: int,
    scenario: str | None,
    tracker_observation: dict[str, Any] | None,
    consensus: dict[str, Any] | None,
) -> dict[str, Any]:
    analyzer = {
        "version": observation.get("version"),
        "warnings": copy.deepcopy(observation.get("warnings", [])),
        "errors": copy.deepcopy(observation.get("errors", [])),
        "fields": _normalized_fields(observation.get("fields", {})),
    }
    source_observation = observation.get("source", {})
    source = {
        "path": str(source_path),
        "sha256": actual_sha256,
        "byte_size": len(png_bytes),
        "size": copy.deepcopy(source_observation.get("size")),
        "canonical_size": copy.deepcopy(observation.get("canonical_size")),
        "normalization": copy.deepcopy(observation.get("normalization")),
    }
    numeric_complete = (observation.get("numeric_state_complete", observation.get("valid_for_training")) is True
                        and _numeric_fields_complete(analyzer["fields"]))
    image = (cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
             if scenario == "larc" and not numeric_complete else None)
    navigation = main_menu_navigation_check(
        {**analyzer, "numeric_state_complete": numeric_complete}, image, scenario=scenario,
    )
    executable = navigation["accepted"]
    if executable and not numeric_complete:
        analyzer["warnings"].append("current_stats_deferred_to_training_page")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "valid_for_training": executable,
        "numeric_state_complete": numeric_complete,
        "source": source,
        "analyzer": analyzer,
        "state_context": (
            _project_state_context(analyzer["fields"], turn_index, scenario)
            if executable
            else None
        ),
        "persistence": {
            "mode": "atomic_replace",
            "parent_directory_fsync": "best_effort",
        },
    }
    if tracker_observation is not None:
        report["tracker"] = copy.deepcopy(tracker_observation)
    if consensus is not None:
        report["consensus"] = copy.deepcopy(consensus)
    return report


def main_menu_navigation_check(
    observation: dict[str, Any], image: np.ndarray | None, *, scenario: str | None,
) -> dict[str, Any]:
    """Share the existing entry-only allowance, not a training-action gate.

    A complete analyzer result keeps its normal meaning. On L'Arc only, unread
    stats may instead be obtained on the training page; screen, remaining turns,
    vitality and ready controls are still required on this same frame. This
    helper neither reads another frame nor fills in missing numbers.
    """
    numeric_complete = observation.get("numeric_state_complete", observation.get("valid_for_training")) is True
    result: dict[str, Any] = {
        "accepted": numeric_complete,
        "numeric_state_complete": numeric_complete,
        "reason": "main_menu_numeric_complete" if numeric_complete else "main_menu_fields_unverified",
        "field_failures": {},
    }
    if numeric_complete:
        return result
    fields = observation.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    for name in REQUIRED_FOR_NUMERIC_STATE:
        field = fields.get(name)
        if not isinstance(field, dict):
            result["field_failures"][name] = "missing_field"
        elif field.get("abstained", True):
            evidence = field.get("evidence")
            result["field_failures"][name] = (
                evidence.get("rejection_reason", "abstained")
                if isinstance(evidence, dict) else "abstained"
            )
        elif not _field_is_high_confidence(field):
            result["field_failures"][name] = "low_or_invalid_confidence"
    screen = fields.get("screen")
    if not isinstance(screen, dict) or screen.get("value") != "main_menu":
        result["field_failures"]["screen"] = "not_main_menu"
    if scenario != "larc":
        return result
    if observation.get("errors"):
        result.update(reason="main_menu_analysis_errors", errors=list(observation["errors"]))
        return result
    if not _is_valid_for_training(fields):
        result["reason"] = "main_menu_navigation_fields_unverified"
        return result
    from core.auto_trainer.safe_advance import SafeAdvanceExecutor
    if image is not None and image.shape[1::-1] not in SUPPORTED_SIZES:
        image = _normalize_main_menu_image(image)
    if not SafeAdvanceExecutor._career_home_controls_ready(image):
        result["reason"] = "main_menu_controls_not_ready"
        return result
    result.update(accepted=True, reason="current_stats_deferred_to_training_page")
    return result


def _normalized_fields(fields: Any) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    normalized = copy.deepcopy(fields)
    for name, field in normalized.items():
        if not isinstance(field, dict):
            continue
        confidence = _normalize_confidence(field.get("confidence"))
        if confidence is not None:
            field["confidence"] = confidence
            if name == "target_text":
                _normalize_target_field(field)
            continue
        rejected = field.get("confidence")
        evidence = field.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        evidence["provider_rejection_reason"] = "invalid_confidence"
        evidence["provider_rejected_confidence"] = _confidence_label(rejected)
        field.update(
            {
                "value": None,
                "confidence": 0.0,
                "abstained": True,
                "evidence": evidence,
            }
        )
    return normalized


def _normalize_target_field(field: dict[str, Any]) -> None:
    value = field.get("value")
    if field.get("abstained", True):
        rejection_reason = "target_abstained"
    elif float(field["confidence"]) < HIGH_CONFIDENCE:
        rejection_reason = "target_not_high_confidence"
    elif not _json_safe_target_string(value):
        rejection_reason = "invalid_target_value"
    else:
        return

    evidence = field.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    evidence["provider_target_status"] = "unknown"
    evidence["provider_rejection_reason"] = rejection_reason
    if rejection_reason == "invalid_target_value":
        evidence["provider_rejected_value_type"] = type(value).__name__
    field.update(
        {
            "value": None,
            "confidence": 0.0,
            "abstained": True,
            "evidence": evidence,
        }
    )


def _json_safe_target_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return False
    return True


def _confidence_is_valid(value: Any) -> bool:
    return _normalize_confidence(value) is not None


def _normalize_confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _confidence_label(value: Any) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    if value == float("inf"):
        return "+inf"
    if value == float("-inf"):
        return "-inf"
    return "out_of_range"


def _project_state_context(
    fields: dict[str, Any], turn_index: int, scenario: str | None
) -> dict[str, Any]:
    target_field = fields.get("target_text")
    target_text = _verified_target_text(target_field)
    target_id = TARGET_IDS.get(target_text) if scenario == "grand_masters" else None
    fragments = _optional_value(fields.get("fragments"))
    vitality = _vitality_percent(fields.get("vitality"))
    metadata = {
        "turn": _runner_field_metadata(turn_index),
        "turn_index": _runner_field_metadata(turn_index),
        "remaining_turns": _field_metadata(fields["remaining_turns"]),
        "target_text": _target_field_metadata(target_field),
        "target_id": {
            "value": target_id,
            "source": "verified_target_mapping",
            "confidence": _target_confidence(target_field, target_text),
            "abstained": target_text is None,
            "evidence": {
                "visible_target_text": _optional_value(target_field),
                "provider_target_status": "verified" if target_text else "unknown",
            },
        },
        "vitality": _field_metadata(fields.get("vitality")),
        "mood": _field_metadata(fields.get("mood")),
        "fragments": _field_metadata(fields.get("fragments")),
    }
    for name in STAT_FIELDS:
        field = fields.get(name)
        metadata[name] = _field_metadata(field)
        value = metadata[name]["value"]
        if not _field_is_high_confidence(field) or type(value) is not int or value < 0:
            metadata[name].update(value=None, abstained=True)
    scenario_state = {
        "scenario": scenario,
        "mood": _mood_value(fields.get("mood")),
        "mood_risk": _mood_risk(fields.get("mood")),
    }
    if scenario == "grand_masters":
        scenario_state.update(fragment_count=fragments, fragment_state=_fragment_state(fragments))
    else:
        metadata.pop("fragments")
        metadata["target_id"].update(source="scenario_mapping_not_applied", confidence=0.0, abstained=True)

    return {
        "turn": turn_index,
        "turn_index": turn_index,
        "remaining_turns": fields["remaining_turns"]["value"],
        "target_text": target_text,
        "target_id": target_id,
        "vitality": vitality,
        "current_stats": {name: metadata[name]["value"] for name in STAT_FIELDS},
        "scenario_state": scenario_state,
        "condition_snapshot": {
            "visible_condition_signature": None,
            "suspected_negative": False,
        },
        "field_metadata": metadata,
    }


def _verified_target_text(field: dict[str, Any] | None) -> str | None:
    value = _optional_value(field)
    if (
        not isinstance(value, str)
        or not _confidence_is_valid(field.get("confidence") if field else None)
        or float(field["confidence"]) < HIGH_CONFIDENCE
        or value not in TARGET_IDS
    ):
        return None
    return value


def _target_field_metadata(field: dict[str, Any] | None) -> dict[str, Any]:
    metadata = _field_metadata(field)
    if _verified_target_text(field) is None:
        metadata["value"] = None
        metadata["abstained"] = True
    return metadata


def _target_confidence(field: dict[str, Any] | None, target_text: str | None) -> float:
    if target_text is None or not field:
        return 0.0
    return field.get("confidence", 0.0)


def _field_metadata(field: dict[str, Any] | None) -> dict[str, Any]:
    field = field or {}
    return {
        "value": copy.deepcopy(field.get("value")),
        "confidence": field.get("confidence", 0.0),
        "abstained": bool(field.get("abstained", True)),
        "source": "numeric_ocr_secondary" if field.get("source") == "numeric_ocr_secondary" else "pixel_analyzer",
        "evidence": copy.deepcopy(field.get("evidence", {})),
    }


def _runner_field_metadata(turn_index: int) -> dict[str, Any]:
    return {
        "value": turn_index,
        "confidence": 1.0,
        "abstained": False,
        "source": "runner_monotonic_index",
        "evidence": {"runner_turn_index": turn_index},
    }


def _optional_value(field: dict[str, Any] | None) -> Any:
    if (
        not field
        or field.get("abstained", True)
        or not _confidence_is_valid(field.get("confidence"))
    ):
        return None
    return field.get("value")


def _mood_value(field: dict[str, Any] | None) -> str:
    value = _optional_value(field)
    return value if isinstance(value, str) and value else "unknown"


def _mood_risk(field: dict[str, Any] | None) -> str | None:
    return None if _mood_value(field) != "unknown" else "mood_unknown"


def _vitality_percent(field: dict[str, Any] | None) -> float | None:
    value = _optional_value(field)
    if not isinstance(value, dict):
        return None
    percent = value.get("percent")
    return percent if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None


def _fragment_state(fragment_count: Any) -> str:
    if not isinstance(fragment_count, int) or isinstance(fragment_count, bool):
        return "unknown"
    if fragment_count == 8:
        return "full"
    if 0 <= fragment_count <= 7:
        return "available"
    return "unknown"


def _atomic_write_json(destination: Path, report: dict[str, Any]) -> None:
    try:
        payload = _serialize_report(report)
    except (TypeError, ValueError) as error:
        raise MainMenuStateProviderError(
            "main_menu_state_provider_serialize_failed"
        ) from error

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise MainMenuStateProviderError(
            "main_menu_state_provider_write_failed"
        ) from error

    temporary_path: Path | None = None
    stage = "write"
    try:
        temporary_path = _write_temp_json(destination, payload)
        stage = "replace"
        os.replace(temporary_path, destination)
        temporary_path = None
    except OSError as error:
        raise MainMenuStateProviderError(
            f"main_menu_state_provider_{stage}_failed"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    # Directory fsync is not available on all Windows filesystems; replacement remains atomic.
    _fsync_parent_directory(destination.parent)


def _serialize_report(report: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            report,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_temp_json(destination: Path, payload: bytes) -> Path:
    temporary_path: Path | None = None
    complete = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        complete = True
        return temporary_path
    finally:
        if temporary_path is not None and not complete:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_parent_directory(parent: Path) -> None:
    try:
        descriptor = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
