"""Read the five Chinese mood labels from the existing, canonical HUD.

Small line crops tolerate the badge's horizontal motion. This reuses the
calendar OCR engine and does not request another frame or full-screen OCR.
"""
import math

import cv2
import numpy as np


MOOD_LABELS = {"极佳": "excellent", "上佳": "good", "普通": "normal",
               "不佳": "bad", "极差": "very_bad"}
MOOD_TEXT_ROIS = ((574, 143, 651, 181), (595, 143, 692, 181))


def read_mood_label(image, *, reader=None):
    empty = {"value": None, "confidence": 0., "abstained": True,
             "evidence": {"reader": "mood_line_ocr", "reason": "label_unreadable"}}
    if image is None or image.shape[:2] != (1280, 720):
        return empty
    header = cv2.cvtColor(image[137:190, 526:700], cv2.COLOR_BGR2HSV)
    # An erased/dim HUD is unknown; don't run OCR on arbitrary dark pixels.
    if np.mean((header[:, :, 1] > 80) & (header[:, :, 2] > 130)) < .18:
        return empty
    if reader is None:
        from core.auto_trainer.career_goal_perception import _GOAL_REGION_READER
        reader = _GOAL_REGION_READER
    for x1, y1, x2, y2 in MOOD_TEXT_ROIS:
        try:
            text, confidence = reader.read_line(image[y1:y2, x1:x2])
        except (RuntimeError, ValueError):
            return empty
        text = "".join(text.split())
        if text in MOOD_LABELS and math.isfinite(confidence) and confidence >= .9:
            return {"value": MOOD_LABELS[text], "confidence": float(confidence), "abstained": False,
                    "evidence": {"reader": "mood_line_ocr", "text": text,
                                 "roi": [x1, y1, x2, y2]}}
    return empty
