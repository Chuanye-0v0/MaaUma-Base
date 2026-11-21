"""Optional fixed-crop growth observations; no extra captures or navigation."""
import re


def _line(image, roi, reader=None):
    if reader is None:
        from core.auto_trainer.career_goal_perception import _GOAL_REGION_READER
        reader = _GOAL_REGION_READER.read_line
    x1, y1, x2, y2 = roi
    try:
        text, confidence = reader(image[y1:y2, x1:x2])
        return re.sub(r"\s+", "", text), float(confidence)
    except Exception:
        return "", 0.


def _balance_regions(period):
    # Overseas removes one lower-row button, shifting the balance left.
    domestic = (422, 1170, 510, 1208)
    overseas = (355, 1167, 435, 1203)
    year, month = (period or {}).get("year_index"), (period or {}).get("month")
    if year == 1:
        return (domestic,)
    if year in (2, 3) and type(month) is int and 1 <= month <= 12:
        return (overseas if 7 <= month <= 9 else domestic,)
    return domestic, overseas


def read_menu_growth(image, turn, *, reader=None, period=None):
    result = {"status": "unknown", "turn_index": turn, "evidence": {}}
    if image is None or image.shape[:2] != (1280, 720):
        return result
    text, confidence = _line(image, (35, 302, 179, 348), reader)
    match = re.fullmatch(r"([0-9]{1,3}(?:\.[0-9]+)?)[%％]", text)
    if match and confidence >= .9 and 0 <= float(match[1]) <= 200:
        result["expectation"] = float(match[1])
        result["evidence"]["expectation"] = {"text": text, "confidence": confidence}
    balances = []
    for roi in _balance_regions(period):
        text, confidence = _line(image, roi, reader)
        primary_number = int(text) if re.fullmatch(r"[0-9]{1,4}", text) else None
        mode = 'original_field'
        if primary_number is None or confidence < .9:
            # The capsule's right/bottom edge can be read as a decimal dot.
            # Keep its left edge (including a four-digit leading character),
            # and re-read only this same field. No new frame or looser parsing.
            inner = (roi[0], roi[1] + 4, roi[2] - 5, roi[3] - 5)
            fallback_text, fallback_confidence = _line(image, inner, reader)
            fallback_number = int(fallback_text) if re.fullmatch(r"[0-9]{1,4}", fallback_text) else None
            if (fallback_number is not None and fallback_confidence >= .95
                    and (primary_number is None or primary_number == fallback_number)):
                text, confidence, roi, mode = fallback_text, fallback_confidence, inner, 'trimmed_border_same_frame'
            else:
                continue
        if re.fullmatch(r"[0-9]{1,4}", text) and confidence >= .9:
            balances.append((int(text), text, confidence, roi, mode))
    if balances and len({row[0] for row in balances}) == 1:
        amount, text, confidence, roi, mode = max(balances, key=lambda row: row[2])
        result["adaptation_points"] = amount
        result["evidence"]["adaptation_points"] = {
            "text": text, "confidence": confidence, "roi": list(roi), "read_mode": mode}
    if result["evidence"]:
        result["status"] = "accepted"
    return result


def read_training_growth(image, training_type, *, reader=None):
    result = {"status": "unknown"}
    if image is None or image.shape[:2] != (1280, 720):
        return result
    text, confidence = _line(image, (94, 198, 221, 233), reader)
    match = re.search(r"(?:等级|等級|[Ll][Vv]\.?)\s*([1-5])$", text)
    if match and confidence >= .9:
        result.update(status="accepted", training_level=int(match[1]),
                      evidence={"text": text, "confidence": confidence})
    return result
