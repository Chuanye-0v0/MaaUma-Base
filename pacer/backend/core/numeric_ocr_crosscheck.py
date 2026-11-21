"""Numeric fallback policy and metadata archive; no OCR engine is loaded on import."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any

from config import settings


SCHEMA_VERSION = "uma.numeric_ocr_archive.v1"
_VERIFIED_ARCHIVE_DIRS: set[Path] = set()


def archive_dir() -> Path:
    return Path(getattr(settings, "NUMERIC_OCR_ARCHIVE_DIR", "logs/numeric_ocr"))


def crosscheck_enabled() -> bool:
    return bool(getattr(settings, "NUMERIC_OCR_CROSSCHECK", True))


def archive_enabled() -> bool:
    return bool(getattr(settings, "NUMERIC_OCR_ARCHIVE", True))


def second_opinion_min_score() -> float:
    try:
        score = float(getattr(settings, "NUMERIC_OCR_SECOND_OPINION_MIN_SCORE", 0.70))
    except (TypeError, ValueError, OverflowError):
        return 0.70
    return min(0.99, max(0.0, score)) if math.isfinite(score) else 0.70


def _metadata(value: Any) -> Any:
    """Keep small JSON metadata, never image arrays or binary frame payloads."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _metadata(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_metadata(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return None


def archive(site: str, *, decision: str, **payload: Any) -> str | None:
    """Best effort only: archiving cannot change the recognition decision."""
    if not archive_enabled():
        return None
    try:
        now = datetime.now(timezone.utc)
        directory = archive_dir()
        if directory not in _VERIFIED_ARCHIVE_DIRS or not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            _VERIFIED_ARCHIVE_DIRS.add(directory)
        target = directory / f"numeric_ocr_{now:%Y%m%d}.jsonl"
        record = {
            "schema_version": SCHEMA_VERSION,
            **_metadata(payload),
            "timestamp": now.isoformat(),
            "site": str(site),
            "decision": str(decision),
            "crosscheck_enabled": crosscheck_enabled(),
        }
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        return str(target)
    except Exception:
        return None


def read_number(
    crop: Any,
    reader: Any = None,
    minimum: int = 0,
    maximum: int | None = None,
    prefixes: tuple[str, ...] = ("", "+"),
    suffixes: tuple[str, ...] = ("",),
) -> dict[str, Any]:
    """Read a caller-confirmed numeric crop with the existing local RapidOCR.

    The caller owns screen/row localization and invokes this only after its primary
    reader abstains. Reader injection avoids model initialization in unit tests.
    """
    threshold = second_opinion_min_score()
    result: dict[str, Any] = {
        "status": "abstained",
        "value": None,
        "confidence": None,
        "raw_text": "",
        "min_score": threshold,
        "engine": "local_rapidocr_numeric",
        "reason": "crosscheck_disabled",
    }
    if not crosscheck_enabled():
        return result
    if type(minimum) is not int or (maximum is not None and (type(maximum) is not int or maximum < minimum)):
        result["reason"] = "invalid_numeric_bounds"
        return result
    shape = getattr(crop, "shape", ())
    if len(shape) not in (2, 3) or any(size <= 0 for size in shape):
        result["reason"] = "empty_numeric_crop"
        return result
    try:
        if reader is None:
            from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader

            reader = LocalRapidOCRScreenTextReader()
        raw_text, confidence = (reader.read_line(crop) if hasattr(reader, "read_line") else reader(crop))
        result["raw_text"] = str(raw_text)
        if isinstance(confidence, bool):
            raise ValueError("boolean confidence")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("invalid confidence")
        result["confidence"] = confidence
    except Exception:
        result["reason"] = "secondary_reader_unavailable"
        return result
    text = result["raw_text"].strip().translate(str.maketrans("０１２３４５６７８９＋", "0123456789+"))
    values: set[int] = set()
    for prefix in prefixes:
        for suffix in suffixes:
            if not text.startswith(prefix) or (suffix and not text.endswith(suffix)):
                continue
            end = len(text) - len(suffix) if suffix else len(text)
            number = text[len(prefix):end]
            if re.fullmatch(r"(?:0|[1-9][0-9]*)", number):
                values.add(int(number))
    if len(values) != 1:
        result["reason"] = "secondary_numeric_text_invalid"
        return result
    value = next(iter(values))
    if value < minimum or (maximum is not None and value > maximum):
        result["reason"] = "secondary_numeric_out_of_range"
        return result
    if confidence < threshold:
        result["reason"] = "secondary_confidence_low"
        return result
    result.update(status="accepted", value=value, reason="secondary_numeric_accepted")
    return result
