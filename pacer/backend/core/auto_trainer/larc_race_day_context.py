"""Source-bound race-day header observations; never training or action authority."""
import hashlib
from pathlib import Path
import re

import cv2
import numpy as np

from core.auto_trainer.larc_pages import LARC_SYSTEM_GOAL_IDS, is_larc_race_day
from core.auto_trainer.main_menu_state_provider import (
    _CALENDAR_REGION_READER, _confidence_is_valid, _parse_header_date_value,
)


def read_larc_race_day_context(path: str | Path, *, source_sha256: str, visible_text: str) -> dict:
    """The caller supplies text from the frame packet bound to source_sha256."""
    path = Path(path).resolve()
    result = {
        "schema_version": "larc_race_day_context.v1", "status": "unverified",
        "source": {"absolute_path": str(path), "sha256": None},
        "period": {"status": "unverified", "value": None, "evidence": {}},
        "target_id": None, "target_text": None,
        "target_evidence": {"status": "unverified"}, "decision_safe": False,
    }
    try:
        raw = path.read_bytes()
    except OSError:
        return {**result, "reason": "source_unreadable"}
    sha = hashlib.sha256(raw).hexdigest()
    result["source"]["sha256"] = sha
    result["period"]["source_frame_sha256"] = sha
    if sha != source_sha256:
        return {**result, "reason": "source_sha_mismatch"}
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape != (1280, 720, 3):
        return {**result, "reason": "unsupported_frame"}
    if not is_larc_race_day(image, {"screen_text": {"normalized_text": visible_text}}):
        return {**result, "reason": "race_day_unverified"}
    result["page_evidence"] = {"page_id": "race_day", "source_sha256": sha,
                               "reader": "is_larc_race_day"}

    date_roi = (155, 36, 345, 72)
    try:
        text, confidence = _CALENDAR_REGION_READER.read_line(image[36:72, 155:345])
        text = re.sub(r"\s+", "", text)
        if _confidence_is_valid(confidence) and confidence < .95 and text == "初级出道前":
            date_roi = (155, 36, 280, 72)
            text, confidence = _CALENDAR_REGION_READER.read_line(image[36:72, 155:280])
            text = re.sub(r"\s+", "", text)
        value = _parse_header_date_value(text) if date_roi[2] == 345 else None
        if text == "初级出道前":
            value = {"stage": "junior", "year_index": 1, "pre_debut": True}
        result["period"]["evidence"] = {
            "date_bbox": list(date_roi), "date_text": text,
            "date_confidence": confidence if _confidence_is_valid(confidence) else None,
            "date_reader": "rapidocr_rec_only",
        }
        if value is not None and _confidence_is_valid(confidence) and confidence >= .95:
            result["period"].update(status="verified", value=value)
        else:
            result["period"]["reason"] = "date_unverified"
    except Exception:
        result["period"]["reason"] = "date_ocr_failed"

    # Only this verified scenario hub has a fixed milestone but no calendar.
    compact_visible = re.sub(r"\s+", "", visible_text)
    if "凯旋门代表交流赛" in compact_visible and "比赛日" not in compact_visible:
        result["period"].update(status="verified", value={
            "stage": "junior", "year_index": 1, "month": 12, "half": "second"},
            evidence={"date_reader": "verified_scenario_milestone",
                      "milestone": "first_exchange", "date_visible": False,
                      "source_sha256": sha})
        result["period"].pop("reason", None)

    # The header icon starts to the right of this text-only crop. Including it
    # adds a false trailing character to the final Arc target on the fixture.
    target_roi = (232, 72, 520, 113)
    result["target_evidence"].update(roi=list(target_roi), reader="rapidocr_rec_only")
    try:
        text, confidence = _CALENDAR_REGION_READER.read_line(image[72:113, 232:520])
        text = re.sub(r"\s+", "", text)
        target_id = {**LARC_SYSTEM_GOAL_IDS, "参加初级新人战": "participate_in_debut_race"}.get(text)
        result["target_evidence"].update(
            text=text, confidence=confidence if _confidence_is_valid(confidence) else None,
        )
        if target_id is not None and _confidence_is_valid(confidence) and confidence >= .95:
            result.update(target_id=target_id, target_text=text)
            result["target_evidence"]["status"] = "verified"
        else:
            result["target_evidence"]["reason"] = "target_unverified"
    except Exception:
        result["target_evidence"]["reason"] = "target_ocr_failed"
    if result["period"]["status"] == result["target_evidence"]["status"] == "verified":
        result["status"] = "verified"
    else:
        result["reason"] = "header_field_unverified"
    return result
