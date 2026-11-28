"""Extract and durably record finishing placement for Classic October 1st L'Arc (classic_arc)."""
from __future__ import annotations

from pathlib import Path
import re
import time
import unicodedata

from core.auto_trainer.cut_farming import atomic_json

SCHEMA_VERSION = "larc.classic_arc_result.v1"
CLASSIC_ARC_RESULT_PATH = "session/larc_classic_arc_result.json"

_SETTLEMENT_REASONS = frozenset({
    "post_race_dialogue",
    "event_choice",
    "ready_main_menu",
    "ready_training_menu",
    "goal_achievement_loading",
    "goal_achievement_result",
})
_RESULT_STEP_REASONS = frozenset({
    "race_result_continue",
    "open_lost_race_retry",
    "cancel_clock_retry",
    "larc_rank_points_continue",
    "larc_reward_continue",
})
_HEADER_STOP_RE = re.compile(
    r"凯旋门奖|排名表|(?:草地|泥地|ダート)[0-9]{3,4}米|重新挑战|下一页|粉丝数|获得道具|协助者积分"
)
_RANK_TOKEN_RE = re.compile(
    r"第([1-9][0-9]*)[.．]?名|(?<![0-9A-Za-z.+-])([1-9][0-9]*)[.．]?(?:名第|第名|名)"
)
_POINTS_RE = re.compile(r"([0-9]{1,3}(?:[,，.][0-9]{3})+|[0-9]+)积分")
_NONWINNER_COACH_TERMS = (
    "错过了近在",
    "与排名靠前的优俊少女相比",
    "中途体力耗尽",
    "速度不足",
    "耐力不足",
    "力量不足",
    "技能不足",
)


def _compact(text: str | None) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def _step_result_and_decision(row: dict) -> tuple[dict, dict]:
    if not isinstance(row, dict):
        return {}, {}
    res = row.get("result")
    res_map = res if isinstance(res, dict) else {}
    dec = res_map.get("decision")
    dec_map = dec if isinstance(dec, dict) else {}
    return res_map, dec_map


def is_classic_arc_report(report: dict, *, context_period: dict | None = None) -> bool:
    """Return True when a race report corresponds to Classic Year Oct 1st L'Arc."""
    if not isinstance(report, dict):
        return False
    if isinstance(context_period, dict):
        year = context_period.get("year_index")
        if type(year) is int and year != 2:
            return False
    steps = report.get("step_reports") or report.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return False
    compact_texts = []
    has_detail_or_result = False
    for row in steps:
        res, _ = _step_result_and_decision(row)
        compact = _compact((res.get("frame_safety") or {}).get("visible_text"))
        if not compact:
            continue
        if "凯旋门奖中进入前5" in compact:
            return False
        compact_texts.append(compact)
        if "凯旋门奖" in compact and (
            "赛事详情" in compact
            or "排名表" in compact
            or "重新挑战" in compact
            or "草地2400米" in compact
        ):
            has_detail_or_result = True
    joined = "".join(compact_texts)
    if "凯旋门奖" not in joined:
        return False
    if "参加凯旋门奖" in joined:
        return True
    if not has_detail_or_result:
        return False
    if isinstance(context_period, dict) and context_period.get("year_index") == 2 and context_period.get("month") in (9, 10):
        return True
    return re.search(r"已攻克的难关[0-8]/9", joined) is not None


def _header_ranks_for_arc(visible_text: str | None) -> set[int]:
    """Parse finishing rank strictly from the header above course/standings/fan tiers."""
    lines = []
    for raw_line in str(visible_text or "").splitlines():
        compact = _compact(raw_line)
        if _HEADER_STOP_RE.search(compact):
            prefix = _HEADER_STOP_RE.split(compact, maxsplit=1)[0]
            if prefix:
                lines.append(prefix)
            break
        lines.append(compact)
    header = _HEADER_STOP_RE.split("".join(lines), maxsplit=1)[0]
    ranks: set[int] = set()
    for match in _RANK_TOKEN_RE.finditer(header):
        value = next((g for g in match.groups() if g is not None), None)
        if value is not None:
            parsed = int(value)
            if 1 <= parsed <= 20:
                ranks.add(parsed)
    return ranks


def _read_arc_roi_ranks(image_path: str | Path, *, reader=None) -> set[int]:
    ranks: set[int] = set()
    try:
        import cv2
        from core.auto_trainer.race_clock_retry import (
            RESULT_HEADER_ROI,
            RESULT_RANK_ROI,
            _confident,
            _line,
        )
    except (ImportError, OSError):
        return ranks

    path = Path(str(image_path))
    if not path.is_file():
        return ranks
    img = cv2.imread(str(path))
    if img is None or len(img.shape) != 3 or img.shape[0] <= img.shape[1]:
        return ranks
    norm = cv2.resize(img, (720, 1280)) if img.shape[:2] != (1280, 720) else img

    for roi, pattern in (
        (RESULT_RANK_ROI, r"([1-9][0-9]*)"),
        (RESULT_HEADER_ROI, r"(?:第)?([1-9][0-9]*)[.．]?名"),
    ):
        try:
            label, confidence = _line(norm, roi, reader)
        except Exception:
            continue
        match = re.fullmatch(pattern, _compact(label))
        if match and _confident(confidence):
            parsed = int(match[1])
            if 1 <= parsed <= 20:
                ranks.add(parsed)
    return ranks


def extract_classic_arc_result(
    report: dict,
    *,
    read_roi: bool = True,
    context_period: dict | None = None,
    reader=None,
) -> dict | None:
    """Extract settled finishing rank from a classic_arc race report (fail-closed)."""
    if not is_classic_arc_report(report, context_period=context_period):
        return None
    steps = report.get("step_reports") or report.get("steps") or []
    last_retry_idx = max(
        (
            idx
            for idx, row in enumerate(steps)
            if (res := _step_result_and_decision(row))[0].get("performed") is True
            and res[1].get("reason") == "retry_lost_race"
        ),
        default=-1,
    )
    final_steps = steps[last_retry_idx + 1 :]
    if not final_steps:
        return None

    stop_reason = report.get("stop_reason")
    settled = stop_reason in _SETTLEMENT_REASONS or any(
        (res := _step_result_and_decision(row))[0].get("performed") is True
        and res[1].get("reason") == "race_result_continue"
        and "凯旋门奖" in (c := _compact((res[0].get("frame_safety") or {}).get("visible_text")))
        and ("排名表" in c or "草地2400米" in c or "重新挑战" in c or "获得道具" in c)
        for row in final_steps
    )
    if not settled:
        return None

    header_ranks: set[int] = set()
    overlay_ranks: set[int] = set()
    roi_ranks: set[int] = set()
    displayed_points: int | None = None
    win_coach_message = False
    nonwinner_coach_message = False

    for row in final_steps:
        res, dec = _step_result_and_decision(row)
        if dec.get("reason") not in _RESULT_STEP_REASONS:
            continue
        raw_text = (res.get("frame_safety") or {}).get("visible_text") or ""
        compact = _compact(raw_text)
        ev = dec.get("evidence") or {}

        header_ranks |= _header_ranks_for_arc(raw_text)

        overlay = ev.get("larc_rank_points_overlay")
        if isinstance(overlay, dict):
            ov_rank = overlay.get("rank")
            if type(ov_rank) is int and 1 <= ov_rank <= 20:
                overlay_ranks.add(ov_rank)
            if displayed_points is None and type(overlay.get("displayed_points")) is int:
                displayed_points = overlay["displayed_points"]
        if displayed_points is None and "凯旋门奖" not in compact:
            pts_matches = _POINTS_RE.findall(_compact(re.sub(r"[^\S\r\n]", "", str(raw_text))))
            if len(pts_matches) == 1:
                displayed_points = int(re.sub(r"[,，.]", "", pts_matches[0]))

        if "凯旋门奖" in compact:
            if "恭喜" in compact and "一路赢下去" in compact:
                win_coach_message = True
            if any(term in compact for term in _NONWINNER_COACH_TERMS):
                nonwinner_coach_message = True
            if read_roi and ev.get("image"):
                roi_ranks |= _read_arc_roi_ranks(ev["image"], reader=reader)

    all_ranks = roi_ranks | header_ranks | overlay_ranks
    if len(all_ranks) == 1:
        candidate = next(iter(all_ranks))
        if (candidate == 1 and nonwinner_coach_message) or (candidate > 1 and win_coach_message):
            rank = None
            source = "conflicting_rank_and_coach_message"
        else:
            rank = candidate
            source = (
                "roi_and_text_verified"
                if roi_ranks
                else "header_text_verified"
                if header_ranks
                else "overlay_rank_verified"
            )
    elif not all_ranks and win_coach_message and not nonwinner_coach_message:
        rank = 1
        source = "win_coach_message_verified"
    else:
        rank = None
        source = "rank_ambiguous_or_unreadable"

    raw_retries = report.get("clock_retries_used")
    clock_retries = raw_retries if type(raw_retries) is int and raw_retries >= 0 else None
    return {
        "schema_version": SCHEMA_VERSION,
        "race_id": "classic_arc",
        "rank": rank,
        "won": (rank == 1) if rank is not None else None,
        "clock_retries_used": clock_retries,
        "displayed_points": displayed_points,
        "win_coach_message": win_coach_message,
        "nonwinner_coach_message": nonwinner_coach_message,
        "observed_ranks": sorted(all_ranks),
        "roi_ranks": sorted(roi_ranks),
        "header_ranks": sorted(header_ranks),
        "overlay_ranks": sorted(overlay_ranks),
        "source": source,
    }


def save_classic_arc_result(
    run_root: str | Path,
    run_id: str,
    result: dict,
    *,
    turn_index: int | None = None,
    report_path: str | None = None,
) -> dict:
    payload = {
        **result,
        "schema_version": SCHEMA_VERSION,
        "run_session_id": run_id,
        "race_id": "classic_arc",
        "turn_index": turn_index,
        "report_path": report_path,
        "observed_at": time.time(),
    }
    atomic_json(Path(run_root) / CLASSIC_ARC_RESULT_PATH, payload)
    return payload


def load_classic_arc_result(run_root: str | Path, run_id: str) -> dict | None:
    path = Path(run_root) / CLASSIC_ARC_RESULT_PATH
    if not path.is_file():
        return None
    try:
        data = path.read_text(encoding="utf-8-sig")
        import json

        parsed = json.loads(data)
    except (OSError, ValueError, TypeError):
        return None
    if (
        not isinstance(parsed, dict)
        or parsed.get("run_session_id") != run_id
        or parsed.get("race_id") != "classic_arc"
    ):
        return None
    return parsed
