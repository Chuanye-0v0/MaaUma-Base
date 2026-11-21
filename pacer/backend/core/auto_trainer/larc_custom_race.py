"""Runtime session & race-list OCR matcher for L'Arc user-scheduled custom races."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import time
from typing import Any

import cv2
import numpy as np

from core.auto_trainer.cut_farming import atomic_json
from core.auto_trainer.larc_custom_race_catalog import due_custom_race_for_period

SESSION_REL = "session/larc_custom_races.json"
SCHEMA_VERSION = "uma.larc_custom_races.v1"


def _norm_name(text: str) -> str:
    cleaned = re.sub(r"[\s·・()（）【】\[\]「」『』!！?？:：,，.。/／\-—~～]+", "", str(text or ""))
    return cleaned.upper()


def _load_state(run_root: Path) -> dict[str, Any]:
    path = Path(run_root) / SESSION_REL
    progress_path = Path(run_root) / "progress.json"
    run_id = Path(run_root).name
    device = ""
    if progress_path.is_file():
        prog = json.loads(progress_path.read_text(encoding="utf-8-sig"))
        run_id = str(prog.get("run_session_id") or run_id)
        device = str(prog.get("device") or "")
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "run_session_id": run_id,
            "device": device,
            "receipts": {},
        }
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("run_session_id") != run_id:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_session_id": run_id,
            "device": device,
            "receipts": {},
        }
    data.setdefault("receipts", {})
    return data


def custom_race_snapshot(run_root: str | Path) -> dict[str, Any]:
    return deepcopy(_load_state(Path(run_root)))


def due_custom_race(run_root: str | Path, goal: Any, period: Any) -> dict[str, Any] | None:
    custom_ids = getattr(goal, "custom_races", None)
    if not custom_ids:
        return None
    spec = due_custom_race_for_period(custom_ids, period)
    if spec is None:
        return None
    state = _load_state(Path(run_root))
    existing = (state.get("receipts") or {}).get(str(spec["larc_ordinal"]))
    if isinstance(existing, dict) and existing.get("status") in {"completed", "skipped_unavailable"}:
        return None
    return spec


def record_custom_race_outcome(
    run_root: str | Path,
    *,
    ordinal: int,
    race_id: str,
    status: str,
    reason: str,
    turn_index: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"completed", "skipped_unavailable"}:
        raise ValueError(f"invalid_custom_race_status:{status}")
    root = Path(run_root)
    state = _load_state(root)
    entry = {
        "larc_ordinal": int(ordinal),
        "race_id": str(race_id),
        "status": status,
        "reason": str(reason),
        "turn_index": int(turn_index),
        "recorded_at": round(time.time(), 3),
        **(deepcopy(extra) if isinstance(extra, dict) else {}),
    }
    state["receipts"][str(int(ordinal))] = entry
    atomic_json(root / SESSION_REL, state)
    return deepcopy(entry)


def match_custom_race_name(observed_text: str, target_race: dict[str, Any]) -> bool:
    norm_obs = _norm_name(observed_text)
    if not norm_obs:
        return False
    candidates = [
        target_race.get("name_zh_cn", ""),
        target_race.get("short_name_zh_cn", ""),
        *(target_race.get("ocr_names") or []),
    ]
    for raw in candidates:
        norm_cand = _norm_name(raw)
        if not norm_cand:
            continue
        if norm_obs == norm_cand:
            return True
        if len(norm_cand) >= 3 and (norm_cand in norm_obs or (len(norm_obs) >= 3 and norm_obs in norm_cand)):
            return True
    return False


def inspect_race_list_for_custom_target(
    img: np.ndarray,
    target_race: dict[str, Any],
    regions_reader: Any,
) -> dict[str, Any]:
    """Inspect a 赛事一览 frame for ``target_race``.

    Returns a dict with:
      - ``status``: ``"selected_match"`` | ``"unselected_match"`` | ``"not_visible"`` | ``"ocr_error"``
      - ``card_center``: ``[x, y]`` when ``status == "unselected_match"``
      - ``matched_text``: OCR string matched
      - ``detail_header_text``: OCR string in top selected banner
    """
    height, width = img.shape[:2]
    if width > height:
        return {"status": "ocr_error", "reason": "landscape_frame"}
    try:
        regions = regions_reader.read_regions(img)
    except Exception as exc:
        return {"status": "ocr_error", "reason": f"{type(exc).__name__}:{exc}"}

    parsed: list[tuple[tuple[int, int, int, int], str, float]] = []
    for item in regions:
        try:
            box, text, confidence = item[0], str(item[1]).strip(), float(item[2])
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
        except (IndexError, TypeError, ValueError):
            continue
        if not text or confidence < 0.65:
            continue
        parsed.append(((int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), text, confidence))

    # 1. Top selected detail title area (y in 0.22..0.36, x in 0.34..0.93)
    detail_items = [
        item
        for item in parsed
        if int(width * 0.34) <= item[0][0]
        and item[0][2] <= int(width * 0.93)
        and int(height * 0.22) <= item[0][1]
        and item[0][3] <= int(height * 0.36)
        and not item[1].startswith(("G", "+", "★"))
        and "人" not in item[1]
    ]
    detail_items.sort(key=lambda it: (it[0][1], it[0][0]))
    detail_text = "".join(it[1] for it in detail_items)
    if detail_text and match_custom_race_name(detail_text, target_race):
        return {
            "status": "selected_match",
            "matched_text": detail_text,
            "detail_header_text": detail_text,
            "match_source": "top_selected_detail_banner",
        }

    # 2. Scan visible race list cards in lower half (y in 0.50..0.82)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    green = (
        (hsv[:, :, 0] >= 38)
        & (hsv[:, :, 0] <= 92)
        & (hsv[:, :, 1] > 70)
        & (hsv[:, :, 2] > 80)
    )
    # Group left-column race title OCR boxes (x < 0.38*W, 0.50*H <= y <= 0.82*H) by vertical proximity
    title_items = [
        item
        for item in parsed
        if int(width * 0.03) <= item[0][0] < int(width * 0.38)
        and int(height * 0.50) <= item[0][1]
        and item[0][3] <= int(height * 0.82)
        and not item[1].startswith(("G", "★", "+"))
        and "人" not in item[1]
    ]
    title_items.sort(key=lambda it: (it[0][1], it[0][0]))

    clusters: list[list[tuple[tuple[int, int, int, int], str, float]]] = []
    for item in title_items:
        if clusters and abs(item[0][1] - clusters[-1][-1][0][3]) <= int(height * 0.035):
            clusters[-1].append(item)
        else:
            clusters.append([item])

    for cluster in clusters:
        joined = "".join(it[1] for it in cluster)
        if not match_custom_race_name(joined, target_race):
            continue
        y_top = min(it[0][1] for it in cluster)
        y_bot = max(it[0][3] for it in cluster)
        cy = int((y_top + y_bot) / 2)
        row_top_ratio = max(0.50, (y_top / max(height, 1)) - 0.03)
        row_bot_ratio = min(0.83, (y_bot / max(height, 1)) + 0.04)
        corners = [
            float(
                np.mean(
                    green[
                        int(height * y0) : int(height * y1),
                        int(width * x0) : int(width * x1),
                    ]
                )
            )
            for x0, x1, y0, y1 in (
                (0.014, 0.090, row_top_ratio, row_top_ratio + 0.045),
                (0.903, 0.993, row_top_ratio, row_top_ratio + 0.045),
            )
        ]
        is_selected = all(v > 0.07 for v in corners)
        if is_selected:
            return {
                "status": "selected_match",
                "matched_text": joined,
                "detail_header_text": detail_text,
                "match_source": "selected_list_card",
            }
        return {
            "status": "unselected_match",
            "matched_text": joined,
            "detail_header_text": detail_text,
            "card_center": [int(width * 0.50), cy],
            "match_source": "unselected_list_card",
        }

    return {
        "status": "not_visible",
        "detail_header_text": detail_text,
        "visible_titles": ["".join(it[1] for it in cl) for cl in clusters],
    }
