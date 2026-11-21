"""Single fixed-ROI local read for an SS count omitted by full-frame text."""
import math
import re

import cv2
import numpy as np

from core.auto_trainer.frame_safety import (
    LocalRapidOCRScreenTextReader, RapidOCRTextReaderError, _RAPIDOCR_MIN_CONFIDENCE,
)


_READER = LocalRapidOCRScreenTextReader()
_COUNT_ROIS = {"entry": (642, 992, 699, 1029), "preview": (642, 941, 699, 973)}


def read_larc_ss_count(image: np.ndarray, *, stage: str = "entry") -> dict:
    """Read N人 once; callers retain page verification and execution authority."""
    result = {"status": "unknown", "count": None, "text": "", "confidence": None,
              "source": {"reader": "rapidocr_rec_only", "stage": stage, "roi": None}}
    if stage not in _COUNT_ROIS:
        return {**result, "reason": "unsupported_stage"}
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.shape not in {(1280, 720, 3), (2560, 1440, 3)}):
        return {**result, "reason": "unsupported_frame"}
    scale = image.shape[1] // 720
    x1, y1, x2, y2 = _COUNT_ROIS[stage]
    result["source"]["roi"] = [coordinate * scale for coordinate in (x1, y1, x2, y2)]
    canonical = image if scale == 1 else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    try:
        text, confidence = _READER.read_line(canonical[y1:y2, x1:x2])
    except RapidOCRTextReaderError:
        return {**result, "reason": "count_ocr_failed"}
    result["text"] = text
    result["confidence"] = confidence if math.isfinite(confidence) else None
    match = re.fullmatch(r"(?:VS)?([0-5])人", re.sub(r"\s+", "", text), flags=re.IGNORECASE)
    if match is None or not math.isfinite(confidence) or confidence < _RAPIDOCR_MIN_CONFIDENCE:
        return {**result, "reason": "count_unverified"}
    return {**result, "status": "accepted", "count": int(match.group(1)), "reason": "count_badge_read"}
