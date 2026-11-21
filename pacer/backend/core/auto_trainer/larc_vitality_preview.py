"""Read the training bar's normal, dark-cost and bright-recovery segments.

Only the small bar crop is processed. No OCR, extra screenshot or game input.
The bar grows with maximum vitality; output is a percentage of its actual
width, never an invented absolute number of game vitality points.
"""
from __future__ import annotations

import math

SCHEMA = "larc_vitality_preview.v1"
UNIT = "percent_of_current_max"
_ROI = (228, 158, 578, 166)  # Interior centre band in the 720 x 1280 layout.


def _runs(labels):
    import numpy as np

    edges = np.r_[0, np.flatnonzero(labels[1:] != labels[:-1]) + 1, len(labels)]
    return [(int(a), int(b), int(labels[a])) for a, b in zip(edges[:-1], edges[1:])]


def accepted_preview(value):
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA
            or value.get("status") != "accepted" or value.get("unit") != UNIT):
        return False
    numbers = [value.get(key) for key in ("current_percent", "after_percent", "delta_percent")]
    if any(type(number) not in {int, float} or not math.isfinite(number) for number in numbers):
        return False
    before, after, delta = numbers
    return 0 <= before <= 100 and 0 <= after <= 100 and abs(after - before - delta) <= .02


def read_training_vitality_preview(image, training_type):
    import cv2
    import numpy as np

    result = {"schema_version": SCHEMA, "status": "abstained", "unit": UNIT,
              "training_type": training_type, "current_percent": None,
              "after_percent": None, "delta_percent": None}
    if (not isinstance(image, np.ndarray) or image.shape not in {(1280, 720, 3), (2560, 1440, 3)}
            or image.dtype != np.uint8
            or training_type not in {"speed", "stamina", "power", "guts", "wisdom"}):
        return dict(result, reason="unsupported_training_image")
    scale = image.shape[1] // 720
    x1, y1, x2, y2 = _ROI
    crop = image[y1 * scale:y2 * scale, x1 * scale:x2 * scale]
    if scale != 1:
        crop = cv2.resize(crop, (x2 - x1, y2 - y1), interpolation=cv2.INTER_AREA)
    profile = np.median(crop, axis=0).astype(np.uint8)
    hsv = cv2.cvtColor(profile[None], cv2.COLOR_BGR2HSV)[0]
    saturation, brightness = hsv[:, 1], hsv[:, 2]
    labels = np.zeros(len(profile), dtype=np.uint8)
    labels[(saturation < 35) & (brightness >= 95) & (brightness <= 140)] = 1  # Empty rail.
    labels[(saturation >= 150) & (brightness >= 205)] = 2  # Normal gradient.
    labels[(saturation >= 100) & (brightness >= 85) & (brightness <= 185)] = 3  # Dark cost.
    labels[(saturation >= 30) & (saturation < 150) & (brightness >= 225)] = 4  # Light recovery.

    # The right rim is a several-pixel interruption. One-pixel colour blends
    # at segment boundaries are tolerated, not counted as extra segments.
    end = next((a for a, b, kind in _runs(labels) if kind == 0 and b - a >= 3), None)
    if end is None or not 265 <= end <= 340:
        return dict(result, reason="bar_geometry_unrecognized")
    body = labels[:end].copy()
    for a, b, kind in _runs(body):
        if kind == 0:
            body[a:b] = body[a - 1] if a else body[b]
    for a, b, kind in _runs(body):
        if (kind == 4 and b - a <= 2 and a > 0 and b < len(body)
                and body[a - 1] == 2 and body[b] == 3):
            # A one-pixel bright divider sometimes marks the cost boundary;
            # it is not a simultaneous recovery segment.
            body[a:b] = 3
    segments = _runs(body)
    if len(segments) > 1 and segments[-1][2] == 4 and segments[-1][1] - segments[-1][0] <= 2:
        # The anti-aliased right rim of a full bar can be pastel as well.
        end = segments[-1][0]
        body = body[:end]
        segments = _runs(body)
    pattern = [kind for _, _, kind in segments]
    normal = int(np.count_nonzero(body == 2))
    cost = int(np.count_nonzero(body == 3))
    recovery = int(np.count_nonzero(body == 4))
    if training_type == "wisdom":
        allowed = ([2, 4, 1], [2, 4], [4, 1], [4], [2])
        before, after = normal, normal + recovery
    else:
        allowed = ([2, 3, 1], [2, 3], [3, 1], [3], [1])
        before, after = normal + cost, normal
    if pattern not in allowed:
        return dict(result, reason="preview_segment_unrecognized")
    current_percent, after_percent = round(100 * before / end, 2), round(100 * after / end, 2)
    return {**result, "status": "accepted", "reason": "visible_training_bar_segments",
            "current_percent": current_percent, "after_percent": after_percent,
            "delta_percent": round(after_percent - current_percent, 2),
            "kind": "consumption" if cost else "recovery" if recovery else "unchanged",
            "bar_width_pixels": end, "resolution_percent": round(200 / end, 2),
            "evidence": {"canonical_roi": list(_ROI), "bar_right_x": x1 + end,
                         "normal_pixels": normal, "cost_pixels": cost, "recovery_pixels": recovery}}


def read_overseas_adaptation_gain(image) -> dict:
    """Read the '+NNN' overseas training adaptation-pt bubble when present."""
    import re
    import cv2
    import numpy as np

    result = {"schema_version": "larc_adaptation_gain_preview.v1", "status": "abstained", "adaptation_pt": None}
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        return dict(result, reason="unsupported_training_image")
    canonical = image if image.shape == (1280, 720, 3) else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    badge = cv2.cvtColor(canonical[335:395, 24:180], cv2.COLOR_BGR2HSV)
    badge_blue = float(np.mean((badge[:, :, 0] >= 88) & (badge[:, :, 0] <= 125) & (badge[:, :, 1] >= 75) & (badge[:, :, 2] >= 130)))
    if badge_blue < 0.14:
        return dict(result, reason="overseas_badge_absent")
    roi = canonical[290:338, 35:180]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 0] >= 4) & (hsv[:, :, 0] <= 30) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 140)).astype(np.uint8) * 255
    if int(np.count_nonzero(mask)) < 40:
        return dict(result, reason="adaptation_bubble_digits_absent")
    crop = cv2.resize(255 - mask, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    crop = cv2.copyMakeBorder(crop, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    try:
        from PIL import Image
        import pytesseract
        raw_text = pytesseract.image_to_string(
            Image.fromarray(crop),
            config="--psm 7 -c tessedit_char_whitelist=+0123456789",
        ).strip()
    except Exception:
        return dict(result, reason="ocr_unavailable")
    m = re.search(r"\+?([0-9]{2,3})", raw_text.replace(" ", ""))
    if not m:
        return dict(result, reason="adaptation_bubble_unparsed", raw_text=raw_text)
    val = int(m.group(1))
    if not (30 <= val <= 450 and val % 10 == 0):
        return dict(result, reason="adaptation_bubble_out_of_range", raw_text=raw_text, parsed=val)
    return {**result, "status": "accepted", "reason": "visible_overseas_bubble", "adaptation_pt": val}
