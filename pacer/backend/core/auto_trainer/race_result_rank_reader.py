"""Source-bound rank reading for the canonical 720px race-result badge."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np

from .frame_safety import LocalRapidOCRScreenTextReader
from .numeric_ocr_shadow import PpOcrV5NumericReader
from core import numeric_ocr_crosscheck as crosscheck


_SCHEMA_VERSION = "uma.race_result_rank_read.v1"
_SECONDARY_SCHEMA_VERSION = "uma.race_result_rank_read.v2"
_SECONDARY_ENGINE = "local_rapidocr_numeric"
_SECONDARY_ROLE = "rank_secondary_recognition"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RANK_RE = re.compile(r"[1-9][0-9]*\Z")
_MIN_CONFIDENCE = 0.90
# Localization only supplies the complete crop; anchors and final PP reading
# retain the stricter recognition floor. Preserve every raw score and its min.
_MIN_LOCALIZER_CONFIDENCE = 0.85
_BADGE_ROI = (75, 240, 320, 390)
_RANK_PADDING = 8
_RAPID_ENGINE = "local_rapidocr"
_RECOGNITION_ENGINE = "ppocrv5_mobile_rec"
_ROLES = (
    "left_anchor",
    "rank_localizer",
    "right_anchor",
    "rank_recognition",
)


class RaceResultRankReaderError(ValueError):
    """Stable input or source-integrity failure from the rank reader."""


@dataclass(frozen=True)
class RaceResultRankRawItem:
    engine: str
    role: str
    text: str
    box: tuple[tuple[float, float], ...]
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "role": self.role,
            "text": self.text,
            "box": [[x, y] for x, y in self.box],
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class RaceResultRankRead:
    source_frame_sha256: str
    status: str
    rank: int | None
    confidence: float
    rank_roi: tuple[int, int, int, int] | None
    raw_items: tuple[RaceResultRankRawItem, ...]
    abstain_reasons: tuple[str, ...]
    schema_version: str = _SCHEMA_VERSION
    recognition_source: str = "primary"
    recognition_threshold: float = _MIN_CONFIDENCE
    primary_abstain_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_rank_read(self)

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema_version": self.schema_version,
            "source_frame_sha256": self.source_frame_sha256,
            "status": self.status,
            "rank": self.rank,
            "confidence": self.confidence,
            "rank_roi": list(self.rank_roi) if self.rank_roi is not None else None,
            "raw_items": [item.to_dict() for item in self.raw_items],
            "abstain_reasons": list(self.abstain_reasons),
        }

        if self.schema_version == _SECONDARY_SCHEMA_VERSION:
            result.update(recognition_source=self.recognition_source,
                recognition_threshold=self.recognition_threshold,
                primary_abstain_reasons=list(self.primary_abstain_reasons))
        return result


@dataclass(frozen=True)
class _Region:
    text: str
    box: tuple[tuple[float, float], ...]
    confidence: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = [point[0] for point in self.box]
        ys = [point[1] for point in self.box]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0


class RaceResultRankReader:
    """Locate the badge on the full result frame, then recognize its rank crop."""

    def __init__(
        self,
        *,
        region_reader: Any | None = None,
        rank_engine: Any | None = None,
        secondary_reader: Any | None = None,
        cpu_threads: int | None = None,
    ) -> None:
        self._region_reader = (
            region_reader
            if region_reader is not None
            else LocalRapidOCRScreenTextReader()
        )
        self._rank_engine = rank_engine
        self._secondary_reader = secondary_reader
        self._cpu_threads = cpu_threads

    def read(self, image_path: str | Path, *, source_frame_sha256: str) -> RaceResultRankRead:
        """Keep primary decisions; ask another local reader only for a known crop."""
        enabled = crosscheck.crosscheck_enabled()
        threshold = crosscheck.second_opinion_min_score()
        context: dict[str, Any] = {}
        try:
            primary = self._read_primary(image_path, source_frame_sha256=source_frame_sha256,
                                         _read_context=context)
        except RaceResultRankReaderError as exc:
            self._archive(source_frame_sha256, None, None, None, enabled, threshold, str(exc))
            raise
        secondary = {"status": "not_requested", "reason": "primary_accepted" if primary.status == "accepted"
                     else "crosscheck_disabled" if not enabled else "rank_localization_unavailable"}
        result = primary
        if primary.status == "abstained" and enabled and "crop" in context:
            try:
                secondary = crosscheck.read_number(context["crop"], reader=self._secondary_reader,
                    minimum=1, maximum=None, prefixes=("",), suffixes=("",))
                if not isinstance(secondary, Mapping):
                    raise ValueError("secondary_result_invalid")
                threshold = _confidence(secondary.get("min_score"))
                if (secondary.get("status") == "accepted" and type(secondary.get("value")) is int
                        and secondary["value"] > 0 and secondary.get("engine") == _SECONDARY_ENGINE
                        and type(secondary.get("raw_text")) is str
                        and _RANK_RE.fullmatch(_secondary_rank_text(secondary["raw_text"])) is not None
                        and int(_secondary_rank_text(secondary["raw_text"])) == secondary["value"]
                        and _confidence(secondary.get("confidence")) >= threshold):
                    raw = RaceResultRankRawItem(engine=_SECONDARY_ENGINE, role=_SECONDARY_ROLE,
                        text=secondary["raw_text"], box=_roi_box(primary.rank_roi),
                        confidence=float(secondary["confidence"]))
                    result = RaceResultRankRead(source_frame_sha256=source_frame_sha256,
                        status="accepted", rank=secondary["value"],
                        confidence=min(item.confidence for item in (*primary.raw_items[:3], raw)),
                        rank_roi=primary.rank_roi, raw_items=(*primary.raw_items, raw), abstain_reasons=(),
                        schema_version=_SECONDARY_SCHEMA_VERSION, recognition_source="secondary",
                        recognition_threshold=threshold, primary_abstain_reasons=primary.abstain_reasons)
            except Exception as exc:
                secondary = {"status": "abstained", "engine": _SECONDARY_ENGINE,
                             "reason": "secondary_reader_failed:" + type(exc).__name__}
        self._archive(source_frame_sha256, primary, secondary, result, enabled, threshold)
        return result

    @staticmethod
    def _archive(source_sha, primary, secondary, result, enabled, threshold, error=None):
        try:
            crosscheck.archive("race_result_rank", decision=result.status if result is not None else "error",
                source_frame_sha256=source_sha, primary=primary.to_dict() if primary is not None else None,
                second=secondary, rank=result.rank if result is not None else None,
                recognition_source=result.recognition_source if result is not None else None,
                reason=error or (list(primary.abstain_reasons) if primary is not None else []),
                min_score=threshold, crosscheck_enabled=enabled)
        except Exception:
            pass  # Diagnostic storage cannot change a rank decision.

    def _read_primary(
        self,
        image_path: str | Path,
        *,
        source_frame_sha256: str,
        _read_context: dict[str, Any] | None = None,
    ) -> RaceResultRankRead:
        _require_sha(source_frame_sha256)
        source = Path(image_path)
        try:
            encoded = source.read_bytes()
        except OSError as exc:
            raise RaceResultRankReaderError("source_frame_unreadable") from exc
        if hashlib.sha256(encoded).hexdigest() != source_frame_sha256:
            raise RaceResultRankReaderError("source_frame_sha256_mismatch")
        image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RaceResultRankReaderError("source_frame_decode_failed")
        if image.shape[:2] != (1280, 720):
            raise RaceResultRankReaderError("source_frame_size_invalid")

        try:
            regions = _parse_regions(self._region_reader.read_regions(image))
        except Exception:
            return _abstain(source_frame_sha256, "rank_regions_invalid")
        badge_regions = tuple(region for region in regions if _inside_badge(region))
        left = tuple(region for region in badge_regions if region.text.strip() == "第")
        right = tuple(region for region in badge_regions if region.text.strip() == "名")
        if not left or not right:
            return _abstain(source_frame_sha256, "rank_anchor_missing")
        if len(left) != 1 or len(right) != 1:
            return _abstain(source_frame_sha256, "rank_anchor_ambiguous")
        if min(left[0].confidence, right[0].confidence) < _MIN_CONFIDENCE:
            return _abstain(source_frame_sha256, "rank_anchor_low_confidence")

        candidates = tuple(
            region for region in badge_regions if region not in {left[0], right[0]}
        )
        if not candidates:
            return _abstain(source_frame_sha256, "rank_token_missing")
        if len(candidates) != 1:
            return _abstain(source_frame_sha256, "rank_token_ambiguous")
        localized = candidates[0]
        if localized.confidence < _MIN_LOCALIZER_CONFIDENCE:
            return _abstain(source_frame_sha256, "rank_token_low_confidence")
        if not _canonical_rank_layout(left[0], localized, right[0]):
            return _abstain(source_frame_sha256, "rank_layout_unverified")

        roi = _expanded_roi(localized)
        if roi is None:
            return _abstain(source_frame_sha256, "rank_roi_unbounded")
        x1, y1, x2, y2 = roi
        crop = np.ascontiguousarray(image[y1:y2, x1:x2])
        if _read_context is not None:
            _read_context["crop"] = crop
        try:
            text, score = _recognize(self._get_rank_engine(), crop)
        except Exception:
            return _abstain(
                source_frame_sha256,
                "rank_recognition_failed",
                roi=roi,
                raw_items=_localized_items(left[0], localized, right[0]),
            )
        recognized = RaceResultRankRawItem(
            engine=_RECOGNITION_ENGINE,
            role="rank_recognition",
            text=text,
            box=_roi_box(roi),
            confidence=score,
        )
        raw_items = (*_localized_items(left[0], localized, right[0]), recognized)
        if score < _MIN_CONFIDENCE:
            return _abstain(
                source_frame_sha256,
                "rank_recognition_low_confidence",
                roi=roi,
                raw_items=raw_items,
            )
        cleaned = text.strip()
        if _RANK_RE.fullmatch(cleaned) is None:
            return _abstain(
                source_frame_sha256,
                "rank_out_of_range",
                roi=roi,
                raw_items=raw_items,
            )
        rank = int(cleaned)
        confidence = min(item.confidence for item in raw_items)
        return RaceResultRankRead(
            source_frame_sha256=source_frame_sha256,
            status="accepted",
            rank=rank,
            confidence=confidence,
            rank_roi=roi,
            raw_items=raw_items,
            abstain_reasons=(),
        )

    def _get_rank_engine(self) -> Any:
        if self._rank_engine is None:
            self._rank_engine = PpOcrV5NumericReader._build_gain_engine(
                cpu_threads=self._cpu_threads
            )
        return self._rank_engine


def validate_race_result_rank_evidence(value: object) -> RaceResultRankRead:
    """Parse and strictly validate stored reader output without running OCR."""

    old_keys = {"schema_version", "source_frame_sha256", "status", "rank", "confidence",
                "rank_roi", "raw_items", "abstain_reasons"}
    extra_keys = {"recognition_source", "recognition_threshold", "primary_abstain_reasons"}
    if not isinstance(value, Mapping) or not (
        value.get("schema_version") == _SCHEMA_VERSION and set(value) == old_keys
        or value.get("schema_version") == _SECONDARY_SCHEMA_VERSION and set(value) == old_keys | extra_keys
    ):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    raw = value["raw_items"]
    if type(raw) is not list:
        raise RaceResultRankReaderError("rank_evidence_invalid")
    items: list[RaceResultRankRawItem] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {
            "engine", "role", "text", "box", "confidence"
        }:
            raise RaceResultRankReaderError("rank_evidence_invalid")
        items.append(
            RaceResultRankRawItem(
                engine=item["engine"],
                role=item["role"],
                text=item["text"],
                box=_parse_box(item["box"]),
                confidence=_confidence(item["confidence"]),
            )
        )
    roi_value = value["rank_roi"]
    roi = None if roi_value is None else _parse_roi(roi_value)
    reasons = value["abstain_reasons"]
    if type(reasons) is not list or any(type(reason) is not str for reason in reasons):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    primary_reasons = value.get("primary_abstain_reasons", [])
    if type(primary_reasons) is not list or any(type(reason) is not str for reason in primary_reasons):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    return RaceResultRankRead(
        schema_version=value["schema_version"],
        source_frame_sha256=value["source_frame_sha256"],
        status=value["status"],
        rank=value["rank"],
        confidence=_confidence(value["confidence"]),
        rank_roi=roi,
        raw_items=tuple(items),
        abstain_reasons=tuple(reasons),
        recognition_source=value.get("recognition_source", "primary"),
        recognition_threshold=value.get("recognition_threshold", _MIN_CONFIDENCE),
        primary_abstain_reasons=tuple(primary_reasons),
    )


def _parse_regions(value: object) -> tuple[_Region, ...]:
    try:
        iterator = iter(value)
    except TypeError as exc:
        raise RaceResultRankReaderError("rank_regions_invalid") from exc
    regions: list[_Region] = []
    for item in iterator:
        try:
            box = _parse_box(item[0])
            text = str(item[1])
            confidence = _confidence(item[2])
        except (IndexError, TypeError, ValueError, RaceResultRankReaderError) as exc:
            raise RaceResultRankReaderError("rank_regions_invalid") from exc
        regions.append(_Region(text=text, box=box, confidence=confidence))
    return tuple(regions)


def _parse_box(value: object) -> tuple[tuple[float, float], ...]:
    if type(value) not in {list, tuple} or len(value) != 4:
        raise RaceResultRankReaderError("rank_box_invalid")
    points: list[tuple[float, float]] = []
    for point in value:
        if type(point) not in {list, tuple} or len(point) != 2:
            raise RaceResultRankReaderError("rank_box_invalid")
        x, y = point
        if (
            type(x) not in {int, float}
            or isinstance(x, bool)
            or type(y) not in {int, float}
            or isinstance(y, bool)
            or not math.isfinite(float(x))
            or not math.isfinite(float(y))
        ):
            raise RaceResultRankReaderError("rank_box_invalid")
        points.append((float(x), float(y)))
    x1 = min(point[0] for point in points)
    x2 = max(point[0] for point in points)
    y1 = min(point[1] for point in points)
    y2 = max(point[1] for point in points)
    if x2 <= x1 or y2 <= y1:
        raise RaceResultRankReaderError("rank_box_invalid")
    return tuple(points)


def _parse_roi(value: object) -> tuple[int, int, int, int]:
    if type(value) not in {list, tuple} or len(value) != 4 or any(
        type(item) is not int for item in value
    ):
        raise RaceResultRankReaderError("rank_roi_invalid")
    roi = tuple(value)
    if roi[2] <= roi[0] or roi[3] <= roi[1]:
        raise RaceResultRankReaderError("rank_roi_invalid")
    return roi


def _confidence(value: object) -> float:
    if (
        type(value) not in {int, float}
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise RaceResultRankReaderError("rank_confidence_invalid")
    return float(value)


def _inside_badge(region: _Region) -> bool:
    x1, y1, x2, y2 = region.bounds
    bx1, by1, bx2, by2 = _BADGE_ROI
    return bx1 <= x1 < x2 <= bx2 and by1 <= y1 < y2 <= by2


def _canonical_rank_layout(left: _Region, rank: _Region, right: _Region) -> bool:
    lx1, ly1, lx2, ly2 = left.bounds
    nx1, ny1, nx2, ny2 = rank.bounds
    rx1, ry1, rx2, ry2 = right.bounds
    overlap = min(ly2, ry2) - max(ly1, ry1)
    anchor_height = min(ly2 - ly1, ry2 - ry1)
    anchor_y = (left.center[1] + right.center[1]) / 2.0
    return (
        lx2 < rx1
        and left.center[0] < rank.center[0] < right.center[0]
        and lx2 - 20 <= nx1 <= lx2 + 20
        and rx1 - 20 <= nx2 <= rx1 + 20
        and overlap >= 0.70 * anchor_height
        and abs(rank.center[1] - anchor_y) <= 30
        and 25 <= nx2 - nx1 <= 140
        and 50 <= ny2 - ny1 <= 130
    )


def _expanded_roi(region: _Region) -> tuple[int, int, int, int] | None:
    x1, y1, x2, y2 = region.bounds
    roi = (
        math.floor(x1) - _RANK_PADDING,
        math.floor(y1) - _RANK_PADDING,
        math.ceil(x2) + _RANK_PADDING,
        math.ceil(y2) + _RANK_PADDING,
    )
    bx1, by1, bx2, by2 = _BADGE_ROI
    if not (bx1 < roi[0] < roi[2] < bx2 and by1 < roi[1] < roi[3] < by2):
        return None
    return roi


def _localized_items(
    left: _Region, rank: _Region, right: _Region
) -> tuple[RaceResultRankRawItem, ...]:
    return tuple(
        RaceResultRankRawItem(
            engine=_RAPID_ENGINE,
            role=role,
            text=region.text,
            box=region.box,
            confidence=region.confidence,
        )
        for role, region in zip(_ROLES[:3], (left, rank, right))
    )


def _recognize(engine: Any, crop: np.ndarray) -> tuple[str, float]:
    results = list(engine.predict(input=[crop], batch_size=1))
    if len(results) != 1:
        raise RaceResultRankReaderError("rank_recognition_failed")
    result: Any = results[0]
    if hasattr(result, "json"):
        result = result.json
    if callable(result):
        result = result()
    if isinstance(result, Mapping) and isinstance(result.get("res"), Mapping):
        result = result["res"]
    if not isinstance(result, Mapping) or type(result.get("rec_text")) is not str:
        raise RaceResultRankReaderError("rank_recognition_failed")
    return result["rec_text"], _confidence(result.get("rec_score"))


def _roi_box(roi: tuple[int, int, int, int]) -> tuple[tuple[float, float], ...]:
    x1, y1, x2, y2 = roi
    return (
        (float(x1), float(y1)),
        (float(x2), float(y1)),
        (float(x2), float(y2)),
        (float(x1), float(y2)),
    )


def _abstain(
    source_frame_sha256: str,
    reason: str,
    *,
    roi: tuple[int, int, int, int] | None = None,
    raw_items: tuple[RaceResultRankRawItem, ...] = (),
) -> RaceResultRankRead:
    return RaceResultRankRead(
        source_frame_sha256=source_frame_sha256,
        status="abstained",
        rank=None,
        confidence=0.0,
        rank_roi=roi,
        raw_items=raw_items,
        abstain_reasons=(reason,),
    )


def _validate_rank_read(value: RaceResultRankRead) -> None:
    _require_sha(value.source_frame_sha256)
    if value.schema_version not in {_SCHEMA_VERSION, _SECONDARY_SCHEMA_VERSION} or value.status not in {
        "accepted", "abstained"
    }:
        raise RaceResultRankReaderError("rank_evidence_invalid")
    _confidence(value.confidence)
    if any(type(reason) is not str or not reason for reason in value.abstain_reasons):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if len(set(value.abstain_reasons)) != len(value.abstain_reasons):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    for item in value.raw_items:
        if (
            type(item.engine) is not str
            or type(item.role) is not str
            or type(item.text) is not str
        ):
            raise RaceResultRankReaderError("rank_evidence_invalid")
        _parse_box(item.box)
        _confidence(item.confidence)
    if value.schema_version == _SECONDARY_SCHEMA_VERSION:
        _validate_secondary_rank_read(value)
        return
    if (value.recognition_source != "primary" or value.recognition_threshold != _MIN_CONFIDENCE
            or value.primary_abstain_reasons):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if value.status == "abstained":
        if value.rank is not None or value.confidence != 0.0 or not value.abstain_reasons:
            raise RaceResultRankReaderError("rank_evidence_invalid")
        return
    if (
        type(value.rank) is not int
        or value.rank <= 0
        or value.rank_roi is None
        or value.abstain_reasons
        or len(value.raw_items) != 4
    ):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if tuple(item.role for item in value.raw_items) != _ROLES:
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if tuple(item.engine for item in value.raw_items) != (
        _RAPID_ENGINE,
        _RAPID_ENGINE,
        _RAPID_ENGINE,
        _RECOGNITION_ENGINE,
    ):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if any(item.confidence < (_MIN_LOCALIZER_CONFIDENCE if item.role == "rank_localizer"
                              else _MIN_CONFIDENCE) for item in value.raw_items):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    left = _Region(
        value.raw_items[0].text,
        value.raw_items[0].box,
        value.raw_items[0].confidence,
    )
    localized = _Region(
        value.raw_items[1].text,
        value.raw_items[1].box,
        value.raw_items[1].confidence,
    )
    right = _Region(
        value.raw_items[2].text,
        value.raw_items[2].box,
        value.raw_items[2].confidence,
    )
    if (
        left.text.strip() != "第"
        or right.text.strip() != "名"
        or not all(_inside_badge(item) for item in (left, localized, right))
        or not _canonical_rank_layout(left, localized, right)
        or _expanded_roi(localized) != value.rank_roi
    ):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if value.raw_items[-1].text.strip() != str(value.rank):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if value.raw_items[-1].box != _roi_box(value.rank_roi):
        raise RaceResultRankReaderError("rank_evidence_invalid")
    if abs(value.confidence - min(item.confidence for item in value.raw_items)) > 1e-12:
        raise RaceResultRankReaderError("rank_evidence_invalid")


def _secondary_rank_text(text: str) -> str:
    # The shared reader accepts full-width digits as formatting, not OCR guesses.
    return text.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _validate_secondary_rank_read(value: RaceResultRankRead) -> None:
    """A secondary result has its own score/floor; it cannot rewrite the primary."""
    invalid = RaceResultRankReaderError("rank_evidence_invalid")
    if (value.status != "accepted" or type(value.rank) is not int or value.rank <= 0
            or value.rank_roi is None or value.abstain_reasons
            or value.recognition_source != "secondary" or len(value.raw_items) not in {4, 5}
            or len(value.primary_abstain_reasons) != 1):
        raise invalid
    threshold = _confidence(value.recognition_threshold)
    reason = value.primary_abstain_reasons[0]
    anchors = value.raw_items[:3]
    if (tuple(item.role for item in anchors) != _ROLES[:3]
            or any(item.engine != _RAPID_ENGINE for item in anchors)
            or any(item.confidence < (_MIN_LOCALIZER_CONFIDENCE if item.role == "rank_localizer"
                                     else _MIN_CONFIDENCE) for item in anchors)):
        raise invalid
    left, localized, right = tuple(_Region(item.text, item.box, item.confidence) for item in anchors)
    if (left.text.strip() != "第" or right.text.strip() != "名"
            or not all(_inside_badge(item) for item in (left, localized, right))
            or not _canonical_rank_layout(left, localized, right)
            or _expanded_roi(localized) != value.rank_roi):
        raise invalid
    if len(value.raw_items) == 4:
        if reason != "rank_recognition_failed":
            raise invalid
    else:
        primary = value.raw_items[3]
        if (primary.role != "rank_recognition" or primary.engine != _RECOGNITION_ENGINE
                or primary.box != _roi_box(value.rank_roi)):
            raise invalid
        if reason == "rank_recognition_low_confidence":
            if primary.confidence >= _MIN_CONFIDENCE:
                raise invalid
        elif reason == "rank_out_of_range":
            if primary.confidence < _MIN_CONFIDENCE or _RANK_RE.fullmatch(primary.text.strip()) is not None:
                raise invalid
        else:
            raise invalid
    secondary = value.raw_items[-1]
    if (secondary.role != _SECONDARY_ROLE or secondary.engine != _SECONDARY_ENGINE
            or secondary.box != _roi_box(value.rank_roi)
            or secondary.confidence < threshold
            or _secondary_rank_text(secondary.text) != str(value.rank)
            or abs(value.confidence - min(item.confidence for item in (*anchors, secondary))) > 1e-12):
        raise invalid


def _require_sha(value: object) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise RaceResultRankReaderError("source_frame_sha256_invalid")
