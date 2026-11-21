"""Fail-closed numeric OCR normalization for offline shadow evaluation.

The live screen reader remains unchanged.  This module only turns numeric
engine text into a structured candidate and refuses ambiguous values.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Iterable

import cv2
import numpy as np


_PAIR_RE = re.compile(r"(?P<value>\d{1,6})\D{0,12}/\D{0,4}(?P<upper>\d{1,6})")
_OCR_BOUND_RE = re.compile(r"(?P<value>\d{1,6})\D{0,20}1(?P<upper>\d{4})")
_NUMBER_RE = re.compile(r"(?<!\d)\d{1,6}(?!\d)")
_PREPROCESS_VARIANTS = ("gray_up2", "contrast_up2", "binary_up2")
_SINGLE_GAIN_REVIEW_THRESHOLD = 60
_GAIN_CROSSCHECK_CONFLICT = "__gain_crosscheck_conflict__"

# Canonical 720x1280 main-menu value cells.  These are deliberately field
# crops: PP-OCRv5 sees six bounded strings, never one call per glyph and never
# the full anime frame.  The existing template reader remains authoritative.
_FIELD_ROIS: dict[str, tuple[int, int, int, int]] = {
    "speed": (74, 854, 134, 884),
    "stamina": (188, 854, 248, 884),
    "power": (300, 854, 360, 884),
    "guts": (413, 854, 473, 884),
    "wisdom": (526, 854, 586, 884),
    "skill_pt": (586, 862, 694, 892),
}
_FIELD_UPPER_BOUNDS = {
    "speed": 1200,
    "stamina": 1200,
    "power": 1200,
    "guts": 1200,
    "wisdom": 1200,
}

# Canonical 720x1280 training gain row.  Each crop is a column, not a glyph:
# OCR may return one or more ``+N`` components which are summed by the parser.
_GAIN_FIELD_ROIS: dict[str, tuple[int, int, int, int]] = {
    "speed": (10, 745, 140, 840),
    "stamina": (125, 745, 255, 840),
    "power": (240, 745, 370, 840),
    "guts": (350, 745, 485, 840),
    "wisdom": (465, 745, 585, 840),
    "skill_pt": (570, 745, 720, 840),
}
_GAIN_ROW_Y = ((740, 800), (795, 827))
_GAIN_PLUS_ROW_Y = ((740, 790), (790, 840))
_SKILL_GAIN_X = (585, 705)
_GAIN_STRATEGIES = ("fixed", "legacy", "dynamic")


@dataclass(frozen=True)
class GainRowEvidence:
    row_index: int
    occupancy: str
    status: str
    raw_text: str = ""
    value: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class NumericCandidate:
    engine: str
    raw_text: str
    value: int | None
    upper_bound: int | None
    reason: str = ""
    alternatives: tuple[int, ...] = ()
    rows: tuple[GainRowEvidence, ...] = ()


@dataclass(frozen=True)
class NumericReconciliation:
    status: str
    value: int | None
    upper_bound: int | None
    reason: str


@dataclass(frozen=True)
class _GainBatchItem:
    batch_index: int
    field: str
    row_index: int
    variant: str
    crop: np.ndarray


def _normalize_batch_prediction(result: Any) -> str | None:
    first: Any = result
    if hasattr(first, "json"):
        first = first.json
    if callable(first):
        first = first()
    if isinstance(first, dict) and isinstance(first.get("res"), dict):
        first = first["res"]
    if not isinstance(first, dict):
        return None
    direct_text = first.get("rec_text")
    if isinstance(direct_text, str):
        return direct_text
    texts = first.get("rec_texts")
    if not isinstance(texts, list):
        return None
    return "".join(str(text) for text in texts if isinstance(text, str))


def _predict_batch(engine: Any, items: list[_GainBatchItem]) -> dict[int, str]:
    if not items:
        return {}
    results = list(
        engine.predict(
            input=[item.crop for item in items],
            batch_size=len(items),
        )
    )
    if len(results) != len(items):
        return {}
    texts: dict[int, str] = {}
    for item, result in zip(items, results):
        text = _normalize_batch_prediction(result)
        if text is None:
            return {}
        texts[item.batch_index] = text
    return texts


def _gain_mask_crop(crop: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (
        (((hsv[:, :, 0] < 25) | (hsv[:, :, 0] > 170))
         & (hsv[:, :, 1] > 60)
         & (hsv[:, :, 2] > 80))
        .astype(np.uint8)
        * 255
    )
    return cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)


def _skill_gain_needs_review(candidate: NumericCandidate) -> bool:
    # This is the existing production review boundary, not a game-value cap.
    return (
        candidate.value is not None
        and candidate.value > _SINGLE_GAIN_REVIEW_THRESHOLD
        and "+" in candidate.raw_text
    )


class PpOcrV5NumericReader:
    """Lazy PP-OCRv5 field reader for shadow comparison only.

    The constructor accepts an injected engine for deterministic tests.  A
    real engine is created only when the reader is explicitly instantiated;
    importing this module never imports PaddleOCR or downloads a model.
    """

    engine_name = "ppocrv5_mobile"

    def __init__(
        self,
        engine: Any | None = None,
        gain_engine: Any | None = None,
        *,
        cpu_threads: int | None = None,
    ) -> None:
        if cpu_threads is not None and (
            isinstance(cpu_threads, bool)
            or not isinstance(cpu_threads, int)
            or cpu_threads < 1
        ):
            raise ValueError("cpu_threads_invalid")
        # Dynamic gains use only the recognition engine. Load the full
        # detector/recognizer only if a field-reading path actually needs it.
        self._engine = engine
        self._gain_engine = (
            gain_engine
            if gain_engine is not None
            else engine
            if engine is not None
            else self._build_gain_engine(cpu_threads=cpu_threads)
        )
        self._model_warm = False
        self._model_warmup_ms = 0.0
        self._warmup_failed = False

    @staticmethod
    def _build_engine() -> Any:
        from paddleocr import PaddleOCR

        return PaddleOCR(
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )

    @staticmethod
    def _build_gain_engine(*, cpu_threads: int | None = None) -> Any:
        # Preserve the default OCR import order before initializing native
        # Paddle thread pools (Paddle-first can break Torch DLL loading).
        from paddleocr import TextRecognition

        if cpu_threads is not None:
            from paddle.base import core

            core.set_num_threads(cpu_threads)

        kwargs = {"model_name": "PP-OCRv5_mobile_rec"}
        if cpu_threads is not None:
            kwargs["cpu_threads"] = cpu_threads
        return TextRecognition(**kwargs)

    def read_fields(self, image: Any) -> dict[str, NumericCandidate]:
        array = np.asarray(image)
        if array.ndim not in (2, 3):
            raise ValueError("numeric_image_shape_invalid")
        height, width = array.shape[:2]
        if (width, height) != (720, 1280):
            raise ValueError("numeric_image_size_invalid")

        values: dict[str, NumericCandidate] = {}
        for field, (x1, y1, x2, y2) in _FIELD_ROIS.items():
            crop = array[y1:y2, x1:x2]
            raw_text = self._recognize(crop)
            if field == "skill_pt":
                candidate = parse_numeric_text(raw_text, field="pt", expected_upper=None)
            else:
                # The denominator is part of the game UI and varies by
                # scenario (for example /1500 versus /1400).  Parse it from
                # this same crop, then apply the structural pair/range gate.
                pair = _PAIR_RE.search(_clean(raw_text))
                inferred_upper = int(pair.group("upper")) if pair else None
                if inferred_upper is not None:
                    candidate = parse_numeric_text(
                        raw_text, field="stat", expected_upper=inferred_upper
                    )
                else:
                    plain = parse_numeric_text(raw_text, field="pt", expected_upper=None)
                    candidate = NumericCandidate(
                        self.engine_name,
                        raw_text,
                        plain.value if plain.value is not None and plain.value <= 1200 else None,
                        None,
                        "stat_value" if plain.value is not None and plain.value <= 1200 else "stat_value_invalid",
                    )
            values[field] = NumericCandidate(
                self.engine_name,
                candidate.raw_text,
                candidate.value,
                candidate.upper_bound,
                candidate.reason,
                candidate.alternatives,
            )
        return values

    def warm_up(self) -> bool:
        if self._warmup_failed:
            return False
        self._model_warm = False
        started = perf_counter()
        stat_crop = np.zeros((60, 130, 3), dtype=np.uint8)
        skill_gray_crop = np.zeros((152, 272, 3), dtype=np.uint8)
        items = [
            _GainBatchItem(0, "__warmup__", 0, "stat_primary", stat_crop),
            _GainBatchItem(1, "__warmup__", 0, "skill_gray", skill_gray_crop),
        ]
        try:
            if not _predict_batch(self._gain_engine, items):
                raise RuntimeError("numeric_ocr_warmup_invalid_result")
        except Exception:
            self._warmup_failed = True
            self._model_warmup_ms = (perf_counter() - started) * 1000.0
            return False
        self._model_warm = True
        self._model_warmup_ms = (perf_counter() - started) * 1000.0
        return True

    def read_gains(
        self, image: Any, *, strategy: str = "fixed"
    ) -> dict[str, NumericCandidate]:
        """Read six training-gain columns for shadow comparison only.

        ``fixed`` is the existing bounded reader. ``legacy`` trims each row
        using a left-to-right red/orange scan before recognition. ``dynamic``
        classifies plus occupancy in each fixed row ROI before recognition.
        All three strategies share the same parser.
        """

        values, _metrics = self._read_gains_execution(
            image, strategy=strategy, include_batch_signature=False
        )
        return values

    def read_gains_with_metrics(
        self,
        image: Any,
        *,
        strategy: str = "dynamic",
        include_batch_signature: bool = True,
    ) -> tuple[dict[str, NumericCandidate], dict[str, Any]]:
        return self._read_gains_execution(
            image,
            strategy=strategy,
            include_batch_signature=include_batch_signature,
        )

    def _attach_model_metrics(self, metrics: dict[str, Any]) -> None:
        engine = self._gain_engine
        metrics.update(
            {
                "model_warm": self._model_warm,
                "model_name": getattr(engine, "model_name", self.engine_name),
                "model_version": getattr(
                    engine,
                    "model_version",
                    getattr(engine, "model_name", "PP-OCRv5_mobile"),
                ),
                "execution_provider": getattr(
                    engine, "execution_provider", "unknown"
                ),
                "model_warmup_ms": self._model_warmup_ms,
            }
        )

    def _unavailable_gain_candidates(self) -> dict[str, NumericCandidate]:
        return {
            field: NumericCandidate(
                self.engine_name,
                "",
                None,
                None,
                "ppocrv5_unavailable",
            )
            for field in _GAIN_FIELD_ROIS
        }

    def _read_gains_execution(
        self,
        image: Any,
        *,
        strategy: str,
        include_batch_signature: bool = False,
    ) -> tuple[dict[str, NumericCandidate], dict[str, Any]]:
        metrics: dict[str, Any] = {
            "occupancy_ms": 0.0,
            "batch_build_ms": 0.0,
            "ocr_ms": 0.0,
            "row_validate_ms": 0.0,
            "batch_count": 0,
            "batch_item_count": 0,
        }
        if include_batch_signature:
            metrics["batch_signature"] = []
        if self._warmup_failed:
            self._attach_model_metrics(metrics)
            return self._unavailable_gain_candidates(), metrics
        array = np.asarray(image)
        if array.ndim not in (2, 3):
            raise ValueError("numeric_image_shape_invalid")
        height, width = array.shape[:2]
        if (width, height) != (720, 1280):
            raise ValueError("numeric_image_size_invalid")
        if strategy not in _GAIN_STRATEGIES:
            raise ValueError("numeric_gain_strategy_invalid")
        values = self._read_gains_from_rois(
            array,
            _GAIN_FIELD_ROIS,
            strategy=strategy,
            metrics=metrics,
        )
        self._attach_model_metrics(metrics)
        return values, metrics

    @staticmethod
    def _predict_dynamic_variants(
        engine: Any,
        rows: list[dict[str, Any]],
        variants: tuple[str, ...],
        *,
        extra_items: Iterable[tuple[dict[str, Any], str]] = (),
        metrics: dict[str, Any] | None = None,
    ) -> None:
        build_started = perf_counter()
        pending: list[tuple[dict[str, Any], _GainBatchItem]] = []
        entries = ((row, variant) for row in rows for variant in variants)
        for row, variant in (*entries, *extra_items):
            crop = row["crops"].get(variant)
            if crop is None:
                continue
            item = _GainBatchItem(
                len(pending), row["field"], row["row_index"], variant, crop
            )
            pending.append((row, item))
        if metrics is not None:
            metrics["batch_build_ms"] += (perf_counter() - build_started) * 1000.0
            if pending:
                metrics["batch_count"] += 1
                metrics["batch_item_count"] += len(pending)
        if not pending:
            return
        ocr_started = perf_counter()
        results = _predict_batch(engine, [item for _, item in pending])
        if metrics is not None:
            metrics["ocr_ms"] += (perf_counter() - ocr_started) * 1000.0
        expected_indexes = {item.batch_index for _, item in pending}
        mapping_ok = (
            isinstance(results, dict)
            and len(results) == len(pending)
            and set(results) == expected_indexes
        )
        batch_signature = (
            metrics.get("batch_signature")
            if metrics is not None
            else None
        )
        if isinstance(batch_signature, list):
            batch_signature.append(
                {
                    "batch_index": len(batch_signature),
                    "expected_items": [
                        [str(item.field), int(item.row_index), str(item.variant)]
                        for _, item in pending
                    ],
                    "mapped_item_count": (
                        len(results) if isinstance(results, dict) else 0
                    ),
                    "status": "ok" if mapping_ok else "mapping_failed",
                }
            )
        if not mapping_ok:
            for row, _ in pending:
                row["batch_failed"] = True
            return
        for row, item in pending:
            row["texts"][item.variant] = results.get(item.batch_index, "")

    @staticmethod
    def _predict_dynamic_variant(
        engine: Any,
        rows: list[dict[str, Any]],
        variant: str,
        *,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        PpOcrV5NumericReader._predict_dynamic_variants(
            engine, rows, (variant,), metrics=metrics
        )

    def _read_dynamic_gains_batch(
        self,
        array: np.ndarray,
        rois: dict[str, tuple[int, int, int, int]],
        *,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, NumericCandidate]:
        row_records: dict[str, list[dict[str, Any]]] = {field: [] for field in rois}
        row_evidence: dict[str, list[GainRowEvidence | None]] = {
            field: [None, None] for field in rois
        }
        batch_build_started = perf_counter()
        occupancy_elapsed = 0.0
        for field, (x1, _y1, x2, _y2) in rois.items():
            for row_index, (row_y1, row_y2) in enumerate(_GAIN_ROW_Y):
                plus_y1, plus_y2 = _GAIN_PLUS_ROW_Y[row_index]
                plus_crop = array[plus_y1:plus_y2, x1:x2]
                occupancy_started = perf_counter()
                present = _has_visual_gain_plus(plus_crop)
                occupancy_elapsed += perf_counter() - occupancy_started
                if not present:
                    row_evidence[field][row_index] = GainRowEvidence(
                        row_index, "absent", "absent"
                    )
                    continue
                if field == "skill_pt":
                    sx1, sx2 = _SKILL_GAIN_X
                    crop = array[row_y1:row_y2, sx1:sx2]
                    crop = cv2.copyMakeBorder(
                        crop,
                        8,
                        8,
                        8,
                        8,
                        cv2.BORDER_CONSTANT,
                        value=(255, 255, 255),
                    )
                    crop = cv2.resize(
                        crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC
                    )
                    visual_plus = False
                else:
                    crop = array[row_y1:row_y2, x1:x2]
                    visual_plus = row_index == 0 and _has_blue_visual_gain_token(
                        array[_GAIN_ROW_Y[0][0] : _GAIN_ROW_Y[0][1]],
                        field_x1=x1,
                        field_x2=x2,
                    )
                row_records[field].append(
                    {
                        "field": field,
                        "row_index": row_index,
                        "crop": crop,
                        "occupancy_plus": True,
                        "visual_plus": visual_plus,
                        "batch_failed": False,
                        "crops": {"primary": crop},
                        "texts": {},
                    }
                )

        if metrics is not None:
            build_elapsed = perf_counter() - batch_build_started
            metrics["occupancy_ms"] += occupancy_elapsed * 1000.0
            metrics["batch_build_ms"] += max(
                0.0, build_elapsed - occupancy_elapsed
            ) * 1000.0

        stat_rows = [
            row
            for field, rows in row_records.items()
            if field != "skill_pt"
            for row in rows
        ]
        skill_rows = row_records.get("skill_pt", [])
        batch_build_started = perf_counter()
        for row in stat_rows:
            row["crops"]["gray"] = cv2.cvtColor(
                cv2.cvtColor(row["crop"], cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR
            )
        for row in skill_rows:
            gray = cv2.cvtColor(row["crop"], cv2.COLOR_BGR2GRAY)
            row["crops"]["gray"] = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            row["crops"]["color"] = row["crop"]
        if metrics is not None:
            metrics["batch_build_ms"] += (
                perf_counter() - batch_build_started
            ) * 1000.0
        self._predict_dynamic_variants(
            self._gain_engine,
            stat_rows,
            ("primary", "gray"),
            extra_items=[(row, "gray") for row in skill_rows],
            metrics=metrics,
        )
        batch_build_started = perf_counter()
        stat_tight_rows: list[dict[str, Any]] = []
        stat_mask_rows: list[dict[str, Any]] = []
        for row in stat_rows:
            primary = parse_gain_text(
                row["texts"].get("primary", ""), field=row["field"]
            )
            if row["batch_failed"]:
                continue
            gray = parse_gain_text(
                row["texts"].get("gray", ""), field=row["field"]
            )
            if not self._stat_mask_output_independent(
                primary, gray, visual_plus=row["visual_plus"]
            ):
                row["crops"]["mask"] = _gain_mask_crop(row["crop"])
                stat_mask_rows.append(row)
            if primary.value is None or row["visual_plus"]:
                tight = _tight_red_gain_crop(row["crop"])
                if tight is not None:
                    row["crops"]["tight"] = tight
                    stat_tight_rows.append(row)
        if metrics is not None:
            metrics["batch_build_ms"] += (
                perf_counter() - batch_build_started
            ) * 1000.0

        batch_build_started = perf_counter()
        skill_review_rows: list[dict[str, Any]] = []
        for row in skill_rows:
            gray = parse_skill_gain_text(row["texts"].get("gray", ""))
            if row["batch_failed"] or not _skill_gain_needs_review(gray):
                continue
            tight = _tight_red_gain_crop(row["crop"])
            if tight is None:
                continue
            row["crops"]["tight"] = tight
            row["crops"]["tight_gray"] = cv2.cvtColor(
                cv2.cvtColor(tight, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR
            )
            skill_review_rows.append(row)
        skill_color_rows = [
            row
            for row in skill_rows
            if not row["batch_failed"]
            and not (
                (gray := parse_skill_gain_text(row["texts"].get("gray", ""))).value is not None
                and gray.reason != "skill_gain_bare_numeric"
            )
        ]
        if metrics is not None:
            metrics["batch_build_ms"] += (
                perf_counter() - batch_build_started
            ) * 1000.0
        batch_build_started = perf_counter()
        skill_tight_rows: list[dict[str, Any]] = []
        for row in skill_color_rows:
            tight = _tight_red_gain_crop(row["crop"])
            if tight is None:
                continue
            row["crops"]["tight"] = tight
            skill_tight_rows.append(row)
        if metrics is not None:
            metrics["batch_build_ms"] += (
                perf_counter() - batch_build_started
            ) * 1000.0

        # A malformed heterogeneous result fails closed for every pending row;
        # no item may be reassigned to another field or variant.
        self._predict_dynamic_variants(
            self._gain_engine,
            stat_tight_rows,
            ("tight",),
            extra_items=(
                [(row, "mask") for row in stat_mask_rows]
                + [(row, "color") for row in skill_color_rows]
                + [(row, "tight") for row in skill_tight_rows]
                + [(row, "tight") for row in skill_review_rows]
                + [(row, "tight_gray") for row in skill_review_rows]
            ),
            metrics=metrics,
        )

        stat_validate_started = perf_counter()
        for row in stat_rows:
            if row["batch_failed"]:
                raw_text = ""
                parsed = parse_gain_text(raw_text, field=row["field"])
            else:
                raw_text = self._resolve_dynamic_stat_row(row)
                parsed = parse_gain_text(raw_text, field=row["field"])
            row_evidence[row["field"]][row["row_index"]] = GainRowEvidence(
                row["row_index"],
                "present",
                "accepted" if parsed.value is not None else "unresolved",
                raw_text,
                parsed.value,
                parsed.reason,
            )
        if metrics is not None:
            metrics["row_validate_ms"] += (
                perf_counter() - stat_validate_started
            ) * 1000.0

        batch_build_started = perf_counter()
        skill_tight_gray_rows: list[dict[str, Any]] = []
        for row in skill_tight_rows:
            if row["batch_failed"]:
                continue
            color = parse_gain_text(row["texts"].get("color", ""), field="skill_pt")
            if color.value is not None:
                continue
            tight = parse_gain_text(row["texts"].get("tight", ""), field="skill_pt")
            if tight.value is not None:
                continue
            tight_gray = cv2.cvtColor(
                cv2.cvtColor(row["crops"]["tight"], cv2.COLOR_BGR2GRAY),
                cv2.COLOR_GRAY2BGR,
            )
            row["crops"]["tight_gray"] = tight_gray
            skill_tight_gray_rows.append(row)
        if metrics is not None:
            metrics["batch_build_ms"] += (
                perf_counter() - batch_build_started
            ) * 1000.0
        self._predict_dynamic_variant(
            self._gain_engine, skill_tight_gray_rows, "tight_gray", metrics=metrics
        )
        batch_build_started = perf_counter()
        skill_mask_rows: list[dict[str, Any]] = []
        for row in skill_rows:
            if row["batch_failed"]:
                continue
            gray = parse_skill_gain_text(row["texts"].get("gray", ""))
            color = parse_gain_text(row["texts"].get("color", ""), field="skill_pt")
            tight = parse_gain_text(row["texts"].get("tight", ""), field="skill_pt")
            tight_gray = parse_gain_text(
                row["texts"].get("tight_gray", ""), field="skill_pt"
            )
            if color.value is not None or tight.value is not None or tight_gray.value is not None:
                continue
            if gray.value is not None:
                continue
            row["crops"]["mask"] = _gain_mask_crop(row["crop"])
            skill_mask_rows.append(row)
        if metrics is not None:
            metrics["batch_build_ms"] += (
                perf_counter() - batch_build_started
            ) * 1000.0
        self._predict_dynamic_variant(
            self._gain_engine, skill_mask_rows, "mask", metrics=metrics
        )
        skill_validate_started = perf_counter()
        for row in skill_rows:
            if row["batch_failed"]:
                raw_text = ""
                parsed = parse_skill_gain_text(raw_text)
            else:
                raw_text, parsed = self._resolve_dynamic_skill_row(row)
            row_evidence[row["field"]][row["row_index"]] = GainRowEvidence(
                row["row_index"],
                "present",
                "accepted" if parsed.value is not None else "unresolved",
                raw_text,
                parsed.value,
                parsed.reason,
            )

        values: dict[str, NumericCandidate] = {}
        for field in rois:
            evidence = tuple(row for row in row_evidence[field] if row is not None)
            accepted = [row for row in evidence if row.status == "accepted"]
            unresolved = [row for row in evidence if row.status == "unresolved"]
            column_raw_text = " ".join(row.raw_text for row in evidence if row.raw_text)
            if unresolved:
                values[field] = NumericCandidate(
                    self.engine_name,
                    column_raw_text,
                    None,
                    None,
                    "gain_rows_unresolved",
                    rows=evidence,
                )
            elif accepted:
                values[field] = NumericCandidate(
                    self.engine_name,
                    column_raw_text,
                    sum(int(row.value) for row in accepted),
                    None,
                    "gain_rows_verified",
                    rows=evidence,
                )
            else:
                values[field] = NumericCandidate(
                    self.engine_name,
                    "",
                    None,
                    None,
                    "gain_rows_not_found",
                    rows=evidence,
                )
        if metrics is not None:
            metrics["row_validate_ms"] += (
                perf_counter() - skill_validate_started
            ) * 1000.0
        return values

    @staticmethod
    def _stat_mask_output_independent(
        primary: NumericCandidate,
        gray: NumericCandidate,
        *,
        visual_plus: bool,
    ) -> bool:
        return (
            not visual_plus
            and bool(primary.raw_text.strip())
            and bool(gray.raw_text.strip())
            and primary.value is not None
            and gray.value is not None
            and primary.value == gray.value
            and primary.value > 5
            and not primary.raw_text.lstrip().startswith("-")
        )

    @staticmethod
    def _resolve_dynamic_stat_row(row: dict[str, Any]) -> str:
        field = row["field"]
        texts = row["texts"]
        raw_text = texts.get("primary", "")
        primary = parse_gain_text(raw_text, field=field)
        visual_plus = row["visual_plus"]
        tight_text = texts.get("tight", "")
        tight = parse_gain_text(tight_text, field=field)
        if primary.value is None and tight.value is not None and not visual_plus:
            return tight_text

        retry_text = texts.get("gray", "")
        retry = parse_gain_text(retry_text, field=field)
        if primary.value is None and retry.value is not None:
            return retry_text
        if primary.value is None and retry.value is None:
            bare = _parse_bare_gain_text(retry_text)
            if bare is not None and _has_red_orange_foreground(row["crop"]):
                return f"+{bare}"

        mask_text = texts.get("mask", "")
        masked = parse_gain_text(mask_text, field=field)
        if primary.value is None and retry.value is None and masked.value is not None:
            return mask_text
        if (
            primary.value is None
            and retry.value is None
            and masked.value is None
            and (visual_plus or row.get("occupancy_plus") is True)
        ):
            recovered = _visual_plus_gain_consensus(
                (raw_text, retry_text, mask_text, tight_text), field=field
            )
            if recovered is not None:
                return f"+{recovered}"
        if primary.value is not None and masked.value is None:
            masked_bare = _parse_bare_gain_text(mask_text)
            if (
                masked_bare is not None and masked_bare <= 20 and primary.value <= 5
                and (
                    retry.value != primary.value
                    or (
                        raw_text.strip().startswith("-")
                        and not retry_text.strip().startswith("-")
                    )
                )
            ):
                return f"+{masked_bare}"
        if primary.value is not None:
            candidates = [
                candidate
                for candidate in (primary, retry, masked)
                if candidate.value is not None
            ]
            if (
                masked.value is not None
                and masked.value != primary.value
                and retry.value == primary.value
                and raw_text.strip().startswith("-")
                and not retry_text.strip().startswith("-")
            ):
                return mask_text
            counts = Counter(candidate.value for candidate in candidates)
            winners = [value for value, count in counts.items() if count >= 2]
            if len(winners) == 1:
                for candidate, text in (
                    (primary, raw_text),
                    (retry, retry_text),
                    (masked, mask_text),
                ):
                    if candidate.value == winners[0]:
                        return text
            if visual_plus:
                recovered = _visual_plus_gain_consensus(
                    (raw_text, retry_text, mask_text, tight_text), field=field
                )
                if recovered is not None:
                    return f"+{recovered}"
            return _GAIN_CROSSCHECK_CONFLICT
        return raw_text

    @staticmethod
    def _resolve_dynamic_skill_row(
        row: dict[str, Any],
    ) -> tuple[str, NumericCandidate]:
        texts = row["texts"]
        gray_text = texts.get("gray", "")
        gray_candidate = parse_skill_gain_text(gray_text)
        color_text = texts.get("color", "")
        color_candidate = parse_gain_text(color_text, field="skill_pt")
        if _skill_gain_needs_review(gray_candidate):
            tight_text = texts.get("tight", "")
            tight = parse_gain_text(tight_text, field="skill_pt")
            tight_gray = parse_gain_text(texts.get("tight_gray", ""), field="skill_pt")
            if (
                tight.value is not None
                and 0 < tight.value <= _SINGLE_GAIN_REVIEW_THRESHOLD
                and tight.value == tight_gray.value
            ):
                return tight_text, tight
            return gray_text, gray_candidate
        if gray_candidate.value is not None and gray_candidate.reason != "skill_gain_bare_numeric":
            return gray_text, gray_candidate
        if color_candidate.value is not None:
            return color_text, color_candidate
        tight_text = texts.get("tight", "")
        tight_candidate = parse_gain_text(tight_text, field="skill_pt")
        if tight_candidate.value is not None:
            return tight_text, tight_candidate
        tight_gray_text = texts.get("tight_gray", "")
        tight_gray_candidate = parse_gain_text(tight_gray_text, field="skill_pt")
        if tight_gray_candidate.value is not None:
            return tight_gray_text, tight_gray_candidate
        if gray_candidate.value is not None:
            return gray_text, gray_candidate
        mask_text = texts.get("mask", "")
        mask_candidate = parse_gain_text(mask_text, field="skill_pt")
        if mask_candidate.value is not None:
            return mask_text, mask_candidate
        if row.get("occupancy_plus") is True:
            artifact_match = re.fullmatch(r"(\d{1,2})\)", _clean(gray_text))
            if artifact_match is not None:
                recovered_text = f"+{artifact_match.group(1)}"
                recovered = parse_gain_text(recovered_text, field="skill_pt")
                if recovered.value is not None and recovered.value <= 60:
                    return recovered_text, recovered
            glow_match = re.fullmatch(r"(?:4|14|44)(\d{2})", _clean(gray_text))
            if glow_match is not None:
                recovered_text = f"+{glow_match.group(1)}"
                recovered = parse_gain_text(recovered_text, field="skill_pt")
                if recovered.value is not None and recovered.value <= 60:
                    return recovered_text, recovered
        return gray_text or color_text or mask_text, gray_candidate

    def _read_gains_from_rois(
        self,
        array: np.ndarray,
        rois: dict[str, tuple[int, int, int, int]],
        *,
        strategy: str,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, NumericCandidate]:
        if strategy == "dynamic" and (
            type(self)._recognize_gain_row is PpOcrV5NumericReader._recognize_gain_row
            and type(self)._recognize_skill_gain_row
            is PpOcrV5NumericReader._recognize_skill_gain_row
        ):
            return self._read_dynamic_gains_batch(array, rois, metrics=metrics)
        values: dict[str, NumericCandidate] = {}
        for field, (x1, y1, x2, y2) in rois.items():
            if strategy == "dynamic":
                row_evidence: list[GainRowEvidence] = []
                for row_index, (row_y1, row_y2) in enumerate(_GAIN_ROW_Y):
                    plus_y1, plus_y2 = _GAIN_PLUS_ROW_Y[row_index]
                    plus_crop = array[plus_y1:plus_y2, x1:x2]
                    if not _has_visual_gain_plus(plus_crop):
                        row_evidence.append(GainRowEvidence(row_index, "absent", "absent"))
                        continue
                    if field == "skill_pt":
                        sx1, sx2 = _SKILL_GAIN_X
                        ocr_crop = array[row_y1:row_y2, sx1:sx2]
                        ocr_crop = cv2.copyMakeBorder(
                            ocr_crop,
                            8,
                            8,
                            8,
                            8,
                            cv2.BORDER_CONSTANT,
                            value=(255, 255, 255),
                        )
                        ocr_crop = cv2.resize(
                            ocr_crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC
                        )
                        raw_text, parsed = self._recognize_skill_gain_row(ocr_crop)
                    else:
                        ocr_crop = array[row_y1:row_y2, x1:x2]
                        raw_text = self._recognize_gain_row(
                            ocr_crop,
                            field=field,
                            engine=self._gain_engine,
                            strict_crosscheck=True,
                            foreground_gate=False,
                            allow_visual_plus_recovery=(
                                row_index == 0
                                and _has_blue_visual_gain_token(
                                    array[_GAIN_ROW_Y[0][0] : _GAIN_ROW_Y[0][1]],
                                    field_x1=x1,
                                    field_x2=x2,
                                )
                            ),
                        )
                        parsed = parse_gain_text(raw_text, field=field)
                    status = "accepted" if parsed.value is not None else "unresolved"
                    row_evidence.append(
                        GainRowEvidence(
                            row_index,
                            "present",
                            status,
                            raw_text,
                            parsed.value,
                            parsed.reason,
                        )
                    )
                accepted = [row for row in row_evidence if row.status == "accepted"]
                unresolved = [row for row in row_evidence if row.status == "unresolved"]
                column_raw_text = " ".join(row.raw_text for row in row_evidence if row.raw_text)
                if unresolved:
                    values[field] = NumericCandidate(
                        self.engine_name,
                        column_raw_text,
                        None,
                        None,
                        "gain_rows_unresolved",
                        rows=tuple(row_evidence),
                    )
                elif accepted:
                    values[field] = NumericCandidate(
                        self.engine_name,
                        column_raw_text,
                        sum(int(row.value) for row in accepted),
                        None,
                        "gain_rows_verified",
                        rows=tuple(row_evidence),
                    )
                else:
                    values[field] = NumericCandidate(
                        self.engine_name,
                        "",
                        None,
                        None,
                        "gain_rows_not_found",
                        rows=tuple(row_evidence),
                    )
                continue
            row_candidates: list[NumericCandidate] = []
            for row_index, (row_y1, row_y2) in enumerate(_GAIN_ROW_Y):
                crop = array[row_y1:row_y2, x1:x2]
                if strategy == "legacy" and field != "skill_pt":
                    crop = _trim_gain_row_left_to_right(crop)
                if field == "skill_pt":
                    # The upper skill-point token is a small red ``+1`` in
                    # some pages.  Use the same isolated, padded upscale as
                    # the lower token so PP-OCRv5 does not drop that row.
                    sx1, sx2 = _SKILL_GAIN_X
                    crop = array[row_y1:row_y2, sx1:sx2]
                    crop = cv2.copyMakeBorder(
                        crop, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=(255, 255, 255)
                    )
                    crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                if field == "skill_pt":
                    raw_text, parsed = self._recognize_skill_gain_row(crop)
                else:
                    allow_visual_plus_recovery = row_index == 0 and _has_blue_visual_gain_token(
                        array[_GAIN_ROW_Y[0][0] : _GAIN_ROW_Y[0][1]],
                        field_x1=x1,
                        field_x2=x2,
                    )
                    raw_text = self._recognize_gain_row(
                        crop,
                        field=field,
                        engine=self._gain_engine,
                        strict_crosscheck=strategy == "dynamic",
                        foreground_gate=strategy == "dynamic",
                        allow_visual_plus_recovery=allow_visual_plus_recovery,
                    )
                    parsed = parse_gain_text(raw_text, field=field)
                row_candidates.append(parsed)
            valid = [candidate for candidate in row_candidates if candidate.value is not None]
            raw_text = " ".join(candidate.raw_text for candidate in row_candidates if candidate.raw_text)
            if _GAIN_CROSSCHECK_CONFLICT in raw_text:
                values[field] = NumericCandidate(
                    self.engine_name,
                    raw_text,
                    None,
                    None,
                    "gain_crosscheck_conflict",
                )
                continue
            if not valid:
                values[field] = NumericCandidate(
                    self.engine_name, raw_text, None, None, "gain_rows_not_found"
                )
                continue
            total = sum(int(candidate.value) for candidate in valid)
            reason = "gain_two_rows" if len(valid) == 2 else "gain_row_partial"
            values[field] = NumericCandidate(self.engine_name, raw_text, total, None, reason)
        return values

    def _recognize_skill_gain_row(self, crop: np.ndarray) -> tuple[str, NumericCandidate]:
        """Read a skill-point row with a gray-first bounded cross-check.

        The small red skill-point digits are prone to valid-looking color OCR
        errors (for example ``+8`` instead of ``+3`` or ``+63`` instead of
        ``+6``).  Always run both gray and color crops, preferring gray.  A
        bare number is accepted only from the isolated gray crop; the normal
        gain parser remains strict for every other field and for the color
        crop.
        """

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        gray_text = self._recognize(gray, engine=self._engine)
        gray_candidate = parse_skill_gain_text(gray_text)
        color_text = self._recognize(crop, engine=self._engine)
        color_candidate = parse_gain_text(color_text, field="skill_pt")
        if _skill_gain_needs_review(gray_candidate):
            tight = _tight_red_gain_crop(crop)
            if tight is None:
                return gray_text, gray_candidate
            tight_text = self._recognize(tight, engine=self._engine)
            tight_gray = cv2.cvtColor(
                cv2.cvtColor(tight, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR
            )
            tight_gray_text = self._recognize(tight_gray, engine=self._engine)
            return self._resolve_dynamic_skill_row({"texts": {
                "gray": gray_text, "tight": tight_text, "tight_gray": tight_gray_text,
            }})
        if gray_candidate.value is not None and gray_candidate.reason != "skill_gain_bare_numeric":
            return gray_text, gray_candidate
        if color_candidate.value is not None:
            return color_text, color_candidate
        tight = _tight_red_gain_crop(crop)
        if tight is not None:
            tight_text = self._recognize(tight, engine=self._engine)
            tight_candidate = parse_gain_text(tight_text, field="skill_pt")
            if tight_candidate.value is not None:
                return tight_text, tight_candidate
            tight_gray = cv2.cvtColor(tight, cv2.COLOR_BGR2GRAY)
            tight_gray = cv2.cvtColor(tight_gray, cv2.COLOR_GRAY2BGR)
            tight_gray_text = self._recognize(tight_gray, engine=self._engine)
            tight_gray_candidate = parse_gain_text(tight_gray_text, field="skill_pt")
            if tight_gray_candidate.value is not None:
                return tight_gray_text, tight_gray_candidate
        if gray_candidate.value is not None:
            return gray_text, gray_candidate

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = (
            (((hsv[:, :, 0] < 25) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 80))
            .astype(np.uint8)
            * 255
        )
        mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        mask_text = self._recognize(mask, engine=self._engine)
        mask_candidate = parse_gain_text(mask_text, field="skill_pt")
        if mask_candidate.value is not None:
            return mask_text, mask_candidate
        return gray_text or color_text or mask_text, gray_candidate

    def _recognize_gain_row(
        self,
        crop: np.ndarray,
        *,
        field: str,
        engine: Any,
        strict_crosscheck: bool = False,
        foreground_gate: bool = False,
        allow_visual_plus_recovery: bool = False,
    ) -> str:
        """Return the first bounded preprocessing result with explicit ``+N``."""

        if foreground_gate and not _has_gain_token_foreground(crop):
            return ""

        visual_plus = strict_crosscheck and allow_visual_plus_recovery
        tight_text = ""
        raw_text = self._recognize(crop, engine=engine)
        primary = parse_gain_text(raw_text, field=field)
        # A tight color crop is a recovery path when the full row has no
        # explicit token.  Do not let it override a valid full-row value:
        # badges and neighboring red glyphs can otherwise become a different
        # number (the crop is deliberately only a bounded fallback).
        if strict_crosscheck and primary.value is None:
            tight = _tight_red_gain_crop(crop)
            if tight is not None:
                tight_text = self._recognize(tight, engine=engine)
                tight_candidate = parse_gain_text(tight_text, field=field)
                if tight_candidate.value is not None and not visual_plus:
                    return tight_text
        if primary.value is not None and not strict_crosscheck and not (
            primary.reason == "gain_value"
            and primary.value >= _SINGLE_GAIN_REVIEW_THRESHOLD
        ):
            return raw_text

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        retry_text = self._recognize(gray, engine=engine)
        retry = parse_gain_text(retry_text, field=field)
        if primary.value is None and retry.value is not None:
            return retry_text
        if primary.value is None and retry.value is None:
            bare = _parse_bare_gain_text(retry_text)
            if bare is not None and _has_red_orange_foreground(crop):
                return f"+{bare}"

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = (
            (((hsv[:, :, 0] < 25) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 80))
            .astype(np.uint8)
            * 255
        )
        mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        mask_text = self._recognize(mask, engine=engine)
        masked = parse_gain_text(mask_text, field=field)
        if primary.value is None and retry.value is None and masked.value is not None:
            return mask_text
        if (
            primary.value is None
            and retry.value is None
            and masked.value is None
            and visual_plus
        ):
            if not tight_text:
                tight = _tight_red_gain_crop(crop)
                if tight is not None:
                    tight_text = self._recognize(tight, engine=engine)
            recovered = _visual_plus_gain_consensus(
                (raw_text, retry_text, mask_text, tight_text),
                field=field,
            )
            if recovered is not None:
                return f"+{recovered}"

        # A red-only mask can preserve a small single digit when the full row
        # recognizer glues it to a neighbouring glyph (for example ``+3`` vs
        # the isolated ``8``).  Do not override matching signed reads without
        # a primary minus artifact; two-digit noise such as ``43`` stays rejected.
        if strict_crosscheck and primary.value is not None and masked.value is None:
            masked_bare = _parse_bare_gain_text(mask_text)
            if (
                masked_bare is not None and masked_bare <= 20 and primary.value <= 5
                and (
                    retry.value != primary.value
                    or (
                        raw_text.strip().startswith("-")
                        and not retry_text.strip().startswith("-")
                    )
                )
            ):
                return f"+{masked_bare}"

        if primary.value is not None:
            candidates = [
                candidate
                for candidate in (primary, retry, masked)
                if candidate.value is not None
            ]
            # A leading minus is not a valid training-gain token.  PP-OCR can
            # occasionally attach a decorative stroke to the first ``+`` and
            # then misread the digit (the observed ``-+3``/``+3``/``+8``
            # pattern).  In that narrow case the red-only mask is the
            # independent glyph read and should win; ordinary disagreements
            # remain fail-closed below.
            if (
                strict_crosscheck
                and masked.value is not None
                and masked.value != primary.value
                and retry.value == primary.value
                and raw_text.strip().startswith("-")
                and not retry_text.strip().startswith("-")
            ):
                return mask_text
            counts = Counter(candidate.value for candidate in candidates)
            winners = [value for value, count in counts.items() if count >= 2]
            if len(winners) == 1:
                for candidate, text in (
                    (primary, raw_text),
                    (retry, retry_text),
                    (masked, mask_text),
                ):
                    if candidate.value == winners[0]:
                        return text
            if strict_crosscheck and visual_plus:
                if not tight_text:
                    tight = _tight_red_gain_crop(crop)
                    if tight is not None:
                        tight_text = self._recognize(tight, engine=engine)
                recovered = _visual_plus_gain_consensus(
                    (raw_text, retry_text, mask_text, tight_text),
                    field=field,
                )
                if recovered is not None:
                    return f"+{recovered}"
            if strict_crosscheck:
                return _GAIN_CROSSCHECK_CONFLICT
            # A high single-token value without two independent agreeing
            # preprocessings is not safe to pass downstream.
            return _GAIN_CROSSCHECK_CONFLICT
        return raw_text

    def _recognize(self, crop: np.ndarray, *, engine: Any | None = None) -> str:
        if engine is None and self._engine is None:
            self._engine = self._build_engine()
        active_engine = self._engine if engine is None else engine
        result = list(active_engine.predict(crop))
        if not result:
            return ""
        first: Any = result[0]
        if hasattr(first, "json"):
            first = first.json
        if callable(first):
            first = first()
        if isinstance(first, dict) and isinstance(first.get("res"), dict):
            first = first["res"]
        if not isinstance(first, dict):
            return ""
        direct_text = first.get("rec_text")
        if isinstance(direct_text, str):
            return direct_text
        texts = first.get("rec_texts", [])
        return "".join(str(text) for text in texts if isinstance(text, str))


def _trim_gain_row_left_to_right(crop: np.ndarray) -> np.ndarray:
    """Trim a gain row to its left-to-right red/orange foreground tokens."""

    if crop.ndim == 2:
        bgr = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
    else:
        bgr = crop
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red_orange = (
        (((hsv[:, :, 0] < 28) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 90))
        .astype(np.uint8)
        * 255
    )
    columns = np.flatnonzero(red_orange.any(axis=0))
    if columns.size == 0:
        return crop
    left = max(0, int(columns[0]) - 4)
    right = min(crop.shape[1], int(columns[-1]) + 5)
    return crop[:, left:right]


def _parse_bare_gain_text(text: str) -> int | None:
    match = re.fullmatch(r"\d{1,2}", _clean(text))
    if match is None:
        return None
    value = int(match.group(0))
    return value if 0 <= value <= 100 else None


def _has_red_orange_foreground(crop: np.ndarray) -> bool:
    bgr = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR) if crop.ndim == 2 else crop
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = (
        (((hsv[:, :, 0] < 28) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 90))
    )
    return bool(mask.sum() >= 8)


def _has_visual_gain_plus(crop: np.ndarray) -> bool:
    """Detect a bounded red/orange plus token inside one gain row ROI."""

    bgr = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR) if crop.ndim == 2 else crop
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = (
        (((hsv[:, :, 0] < 28) | (hsv[:, :, 0] > 170))
         & (hsv[:, :, 1] > 70)
         & (hsv[:, :, 2] > 90)).astype(np.uint8)
        * 255
    )
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    for x, y, width, height, area in stats[1:]:
        if (
            x <= 1
            or y <= 1
            or x + width >= crop.shape[1] - 1
            or y + height >= crop.shape[0] - 1
        ):
            continue
        if not (13 <= width <= 23 and 13 <= height <= 23 and area >= 70):
            continue
        component = mask[y : y + height, x : x + width] > 0
        horizontal = int((component.sum(axis=1) >= 0.75 * width).sum())
        vertical = int((component.sum(axis=0) >= 0.75 * height).sum())
        if horizontal >= 3 and vertical >= 3:
            return True
    return False


def _has_gain_token_foreground(crop: np.ndarray) -> bool:
    """Require a bounded digit-sized red/orange component for dynamic OCR."""

    bgr = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR) if crop.ndim == 2 else crop
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = (
        (((hsv[:, :, 0] < 28) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 90))
        .astype(np.uint8)
        * 255
    )
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    for x, y, width, height, area in stats[1:]:
        if x <= 3 or x + width >= crop.shape[1] - 3:
            continue
        if width >= 15 and height >= 15 and area >= 100:
            return True
    return False


def _has_blue_visual_gain_token(
    top_row: np.ndarray,
    *,
    field_x1: int,
    field_x2: int,
) -> bool:
    """Verify the local blue outline beside a visual ``+digits`` token."""

    bgr = cv2.cvtColor(top_row, cv2.COLOR_GRAY2BGR) if top_row.ndim == 2 else top_row
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red_mask = (
        (((hsv[:, :, 0] < 25) | (hsv[:, :, 0] > 170))
         & (hsv[:, :, 1] > 60)
         & (hsv[:, :, 2] > 80))
        .astype(np.uint8)
        * 255
    )
    field_mask = red_mask[:, field_x1:field_x2]
    _, _, stats, _ = cv2.connectedComponentsWithStats(field_mask, 8)
    components = [tuple(int(value) for value in row) for row in stats[1:]]
    for x, y, width, height, area in components:
        if not (17 <= width <= 20 and 17 <= height <= 20):
            continue
        aspect = width / height
        fill = area / (width * height)
        if not (0.90 <= aspect <= 1.10 and 0.60 <= fill <= 0.70):
            continue
        component = field_mask[y : y + height, x : x + width] > 0
        horizontal = int((component.sum(axis=1) >= 0.85 * width).sum())
        vertical = int((component.sum(axis=0) >= 0.85 * height).sum())
        if horizontal < 5 or vertical < 5:
            continue
        digit_components: list[tuple[int, int, int, int, int]] = []
        for digit_x, digit_y, digit_width, digit_height, digit_area in components:
            gap = digit_x - (x + width)
            if not (2 <= gap <= 6 and 3 <= digit_width <= 23):
                continue
            if not (24 <= digit_height <= 32 and digit_area >= 80):
                continue
            plus_center_y = y + height / 2
            digit_center_y = digit_y + digit_height / 2
            if abs(plus_center_y - digit_center_y) <= 7:
                digit_components.append(
                    (digit_x, digit_y, digit_width, digit_height, digit_area)
                )
        if not digit_components:
            continue
        first_digit = min(digit_components, key=lambda item: item[0])
        digit_right = first_digit[0] + first_digit[2]
        remaining = sorted(
            (
                component
                for component in components
                if 24 <= component[3] <= 32
                and component[4] >= 80
                and component[0] >= first_digit[0]
                and abs(component[1] - first_digit[1]) <= 4
            ),
            key=lambda item: item[0],
        )
        for digit in remaining:
            if digit[0] <= digit_right + 6:
                digit_right = max(digit_right, digit[0] + digit[2])
        digit_right += field_x1
        first_digit_top = first_digit[1]

        cyan_mask = (
            ((hsv[:, :, 0] >= 75)
             & (hsv[:, :, 0] <= 110)
             & (hsv[:, :, 1] > 50)
             & (hsv[:, :, 2] > 100))
            .astype(np.uint8)
            * 255
        )
        _, _, cyan_stats, _ = cv2.connectedComponentsWithStats(cyan_mask, 8)
        for cyan_x, cyan_y, cyan_width, cyan_height, cyan_area in cyan_stats[1:]:
            if cyan_x <= 0 or cyan_y <= 0:
                continue
            if cyan_x + cyan_width >= top_row.shape[1] or cyan_y + cyan_height >= top_row.shape[0]:
                continue
            if not (-2 <= cyan_x - digit_right <= 4):
                continue
            if not (0 <= cyan_y - first_digit_top <= 6):
                continue
            if not (20 <= cyan_width <= 27 and 23 <= cyan_height <= 30):
                continue
            if not (270 <= cyan_area <= 390):
                continue
            fill = cyan_area / (cyan_width * cyan_height)
            if 0.48 <= fill <= 0.65:
                return True
    return False


def _visual_plus_gain_consensus(
    texts: Iterable[str],
    *,
    field: str,
) -> int | None:
    """Recover a visually verified gain only when three OCR paths agree."""

    votes: list[int] = []
    for text in texts:
        cleaned = _clean(text)
        match = re.search(r"(\d{1,2})$", cleaned)
        if match is None:
            continue
        value = int(match.group(1))
        if parse_gain_text(f"+{value}", field=field).value != value:
            continue
        votes.append(value)
    counts = Counter(votes)
    winners = [value for value, count in counts.items() if count >= 3]
    if len(winners) != 1:
        return None
    return winners[0]


def _tight_red_gain_crop(crop: np.ndarray) -> np.ndarray | None:
    if not _has_red_orange_foreground(crop):
        return None
    bgr = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR) if crop.ndim == 2 else crop
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = (
        (((hsv[:, :, 0] < 28) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 90))
        .astype(np.uint8)
        * 255
    )
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        return None
    left = max(0, int(xs.min()) - 5)
    right = min(crop.shape[1], int(xs.max()) + 6)
    top = max(0, int(ys.min()) - 5)
    bottom = min(crop.shape[0], int(ys.max()) + 6)
    tight = bgr[top:bottom, left:right]
    # Keep the isolated token bounded; the source glyphs are already large
    # enough for the mobile recognizer at 4x and larger scaling blurs edges.
    return cv2.resize(tight, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)


def build_numeric_shadow_report(
    image: Any,
    primary_fields: dict[str, Any],
    *,
    reader: Any = None,
) -> dict[str, Any]:
    """Compare PP-OCRv5 fields without mutating or authorizing primary state."""

    if reader is None:
        if os.environ.get("UMA_NUMERIC_OCR_SHADOW") != "1":
            return {
                "status": "abstained",
                "reason": "shadow_disabled_by_default",
                "engine": PpOcrV5NumericReader.engine_name,
                "fields": {},
            }
        reader = PpOcrV5NumericReader()
    shadow = reader.read_fields(image)
    fields: dict[str, Any] = {}
    for field, candidate in shadow.items():
        primary = primary_fields.get(field)
        primary_value = primary.get("value") if isinstance(primary, dict) else None
        status = "match" if candidate.value is not None and candidate.value == primary_value else "mismatch"
        if candidate.value is None:
            status = "abstain"
        fields[field] = {
            "status": status,
            "primary_value": primary_value,
            "shadow_value": candidate.value,
            "shadow_upper_bound": candidate.upper_bound,
            "raw_text": candidate.raw_text,
            "reason": candidate.reason,
        }
    return {"status": "shadow_only", "engine": PpOcrV5NumericReader.engine_name, "fields": fields}


def build_gain_shadow_report(
    image: Any,
    primary_fields: dict[str, Any],
    *,
    reader: Any = None,
) -> dict[str, Any]:
    """Compare training-gain totals without changing parser or decision state."""

    if reader is None:
        if os.environ.get("UMA_NUMERIC_OCR_SHADOW") != "1":
            return {
                "status": "abstained",
                "reason": "shadow_disabled_by_default",
                "engine": PpOcrV5NumericReader.engine_name,
                "fields": {},
            }
        reader = PpOcrV5NumericReader()
    shadow = reader.read_gains(image)
    fields: dict[str, Any] = {}
    for field, candidate in shadow.items():
        primary = primary_fields.get(field)
        primary_value = primary.get("value") if isinstance(primary, dict) else primary
        status = "match" if candidate.value is not None and candidate.value == primary_value else "mismatch"
        if candidate.value is None:
            status = "abstain"
        fields[field] = {
            "status": status,
            "primary_value": primary_value,
            "shadow_value": candidate.value,
            "raw_text": candidate.raw_text,
            "reason": candidate.reason,
        }
    return {"status": "shadow_only", "engine": PpOcrV5NumericReader.engine_name, "fields": fields}


def _clean(text: str) -> str:
    return (
        str(text or "")
        .replace("／", "/")
        .replace("﹨", "/")
        .replace("，", ",")
        .replace(" ", "")
    )


def _remove_glued_suffix(value: int, upper_bound: int) -> tuple[int, bool]:
    if value <= upper_bound:
        return value, False
    raw = str(value)
    # OCR often glues a one-character grade/icon to the current value.  Keep
    # the longest prefix that is still a legal current value.
    for suffix_len in range(1, len(raw)):
        prefix = int(raw[:-suffix_len])
        if 0 <= prefix <= upper_bound:
            return prefix, True
    return value, False


def parse_numeric_text(
    text: str,
    *,
    field: str,
    expected_upper: int | None,
) -> NumericCandidate:
    """Parse one stat ``value/upper`` pair or a skill-PT integer.

    ``expected_upper`` is supplied by the fixed 720x1280 field geometry and
    current training context.  It is a validation constraint, never a value
    source: a mismatching upper bound is rejected.
    """

    cleaned = _clean(text)
    if field == "pt":
        values = [int(item) for item in _NUMBER_RE.findall(cleaned)]
        values = [item for item in values if 0 <= item <= 3000]
        if not values:
            return NumericCandidate("unknown", text, None, None, "pt_not_found")
        return NumericCandidate("unknown", text, values[-1], None, "pt_value")

    if field != "stat" or expected_upper is None or expected_upper < 0:
        return NumericCandidate("unknown", text, None, None, "invalid_field_spec")

    matches = list(_PAIR_RE.finditer(cleaned))
    if not matches:
        matches = list(_OCR_BOUND_RE.finditer(cleaned))
    if not matches:
        return NumericCandidate("unknown", text, None, None, "stat_pair_not_found")
    match = matches[-1]
    raw_value = int(match.group("value"))
    upper_bound = int(match.group("upper"))
    if upper_bound != expected_upper:
        return NumericCandidate("unknown", text, None, upper_bound, "upper_bound_mismatch")
    value, glued = _remove_glued_suffix(raw_value, upper_bound)
    alternatives: list[int] = []
    if raw_value > upper_bound:
        raw = str(raw_value)
        for suffix_len in range(1, len(raw)):
            alternatives.append(int(raw[:-suffix_len]))
            alternatives.append(int(raw[suffix_len:]))
        alternatives = [item for item in alternatives if 0 <= item <= upper_bound]
        alternatives = list(dict.fromkeys(alternatives))
    if value < 0 or value > upper_bound:
        return NumericCandidate("unknown", text, None, upper_bound, "value_out_of_range")
    return NumericCandidate(
        "unknown",
        text,
        value,
        upper_bound,
        "glued_suffix_removed" if glued else "structural_pair",
        tuple(alternatives),
    )


def parse_gain_text(text: str, *, field: str) -> NumericCandidate:
    """Parse one gain column, summing explicit ``+N`` components."""

    cleaned = _clean(text)
    if "+" not in cleaned:
        return NumericCandidate("unknown", text, None, None, "gain_plus_not_found")
    # A column contains at most two explicit, one-to-three-digit gain rows.
    # Do not truncate a four-digit current attribute or discard one large row.
    tokens = re.findall(r"\+[0-9]{1,3}(?![0-9])", cleaned)
    residue = re.sub(r"\+[0-9]{1,3}(?![0-9])", "", cleaned)
    if not 1 <= len(tokens) <= 2 or re.search(r"[0-9+]", residue):
        return NumericCandidate("unknown", text, None, None, "gain_not_found")
    numbers = [int(token[1:]) for token in tokens]
    return NumericCandidate("unknown", text, sum(numbers), None, "gain_components" if len(numbers) > 1 else "gain_value")


def parse_skill_gain_text(text: str) -> NumericCandidate:
    """Parse a skill-point gain, allowing one bare digit-only gray token.

    The allowance is intentionally separate from :func:`parse_gain_text` and
    is used only after the skill-point capsule has been isolated and rendered
    through the gray-first path.  Text containing multiple numeric tokens or
    an out-of-range value remains rejected.
    """

    explicit = parse_gain_text(text, field="skill_pt")
    if explicit.value is not None:
        return explicit
    cleaned = _clean(text)
    match = re.fullmatch(r"(\d{1,2})", cleaned)
    if match is None:
        return NumericCandidate("unknown", text, None, None, "skill_gain_plus_not_found")
    value = int(match.group(1))
    if value > 60:
        return NumericCandidate("unknown", text, None, None, "skill_gain_out_of_range")
    return NumericCandidate("unknown", text, value, None, "skill_gain_bare_numeric")


def reconcile_numeric_candidates(candidates: Iterable[NumericCandidate]) -> NumericReconciliation:
    valid = [candidate for candidate in candidates if candidate.value is not None or candidate.alternatives]
    if not valid:
        return NumericReconciliation("abstain", None, None, "no_valid_candidate")
    candidate_sets = []
    for candidate in valid:
        values = set(candidate.alternatives)
        if candidate.value is not None:
            values.add(candidate.value)
        candidate_sets.append({(value, candidate.upper_bound) for value in values})
    keys = set.intersection(*candidate_sets)
    if len(keys) == 1:
        value, upper_bound = next(iter(keys))
        return NumericReconciliation("accepted", value, upper_bound, "engines_agree")
    primary_counts = Counter(
        (candidate.value, candidate.upper_bound)
        for candidate in valid
        if candidate.value is not None
    )
    if primary_counts:
        best_key, best_count = primary_counts.most_common(1)[0]
        tied = sum(1 for count in primary_counts.values() if count == best_count)
        if best_count >= 2 and tied == 1:
            value, upper_bound = best_key
            return NumericReconciliation("accepted", value, upper_bound, "engine_majority")
    if len(valid) == 1 and valid[0].value is not None:
        return NumericReconciliation(
            "accepted", valid[0].value, valid[0].upper_bound, "single_structural_candidate"
        )
    return NumericReconciliation("conflict", None, None, "valid_values_conflict")


def build_numeric_preprocess_variants(image: Any) -> tuple[str, ...]:
    """Return the bounded preprocessing schedule used by numeric shadow OCR."""

    return _PREPROCESS_VARIANTS


def preprocess_numeric_image(image: Any, variant: str) -> np.ndarray:
    """Apply one bounded numeric preprocessing variant."""

    if variant not in _PREPROCESS_VARIANTS:
        raise ValueError("numeric_preprocess_variant_invalid")
    array = np.asarray(image)
    if array.ndim == 3:
        gray = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
    elif array.ndim == 2:
        gray = array.astype(np.uint8, copy=False)
    else:
        raise ValueError("numeric_image_shape_invalid")
    if variant == "gray_up2":
        prepared = gray
    elif variant == "contrast_up2":
        prepared = cv2.convertScaleAbs(gray, alpha=1.6, beta=-48)
    else:
        prepared = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    return cv2.resize(prepared, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)


__all__ = [
    "GainRowEvidence",
    "NumericCandidate",
    "NumericReconciliation",
    "PpOcrV5NumericReader",
    "build_numeric_shadow_report",
    "build_gain_shadow_report",
    "build_numeric_preprocess_variants",
    "preprocess_numeric_image",
    "parse_numeric_text",
    "parse_gain_text",
    "parse_skill_gain_text",
    "reconcile_numeric_candidates",
]
