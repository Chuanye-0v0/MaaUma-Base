from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable
import re

import cv2
import numpy as np

from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal, urgent_larc_supporter_goal
from core.auto_trainer.larc_ss_count import read_larc_ss_count
from core.auto_trainer.safe_advance_transition import (
    SafeAdvanceRoutePolicy, expected_next_states_for_decision, observed_route_state,
)


LARC_SYSTEM_GOAL_IDS = {
    "参加初级新人战": "participate_in_debut_race",
    "全国德比中进入前3": "larc_japan_derby_top3",
    "参加尼尔锦标": "larc_niel_participation",
    "参加凯旋门奖": "larc_arc_participation",
    "宝冢纪念中进入前3": "larc_takarazuka_top3",
    "参加福伊锦标": "larc_foy_participation",
    "凯旋门奖中进入前5": "larc_final_arc_top5",
}


def before_first_expedition_preparation(period: dict[str, Any]) -> bool:
    """Sampling stops on classic June second half, before its turn is spent."""
    value = period.get("value", {})
    month = value.get("month")
    return bool(period.get("status") == "verified" and value.get("year_index") == 2
                and value.get("stage") == "classic" and type(month) is int
                and value.get("half") in {"first", "second"}
                and (1 <= month <= 5 or (month == 6 and value["half"] == "first")))


def is_larc_race_day(image: np.ndarray, evidence: dict[str, Any]) -> bool:
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    race_title = ("比赛日" in text and "凯旋门信息" in text) or "凯旋门代表交流赛" in text
    if not race_title or "海外适应性" not in text or not ("实战" in text or "技能" in text):
        return False
    return larc_race_day_controls(image)


def larc_race_day_controls(image: np.ndarray) -> bool:
    """Cheap control readiness only; the page owner still validates its text."""
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    left = hsv[1030:1140, 30:206]
    center = hsv[1010:1140, 240:498]
    right = hsv[1030:1140, 518:691]
    return bool(
        np.mean((left[:, :, 0] >= 80) & (left[:, :, 0] <= 105) & (left[:, :, 1] > 70)) > 0.15
        and np.mean((center[:, :, 0] >= 145) & (center[:, :, 0] <= 175) & (center[:, :, 1] > 60)) > 0.15
        and np.mean((right[:, :, 0] >= 90) & (right[:, :, 0] <= 125) & (right[:, :, 1] > 60)) > 0.15
    )


def larc_adaptation_warning_button(image: np.ndarray, evidence: dict[str, Any]) -> tuple[int, int] | None:
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    if not all(label in text for label in ("参赛确认", "海外适应性", "取消", "参赛")):
        return None
    height, width = image.shape[:2]
    if width > height:
        return None
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    green = ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 95)
             & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 90))
    white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 175)
    if (np.mean(green[392:435, 20:700]) < 0.70
            or np.mean(white[455:775, 30:690]) < 0.75
            or np.mean(green[810:860, 390:650]) < 0.70):
        return None
    return round(518 * width / 720), round(834 * height / 1280)


def larc_exchange_schedule_continue_button(image: np.ndarray, evidence: dict[str, Any]) -> tuple[int, int] | None:
    from core.auto_trainer.career_result_layouts import exchange_history_continue_point
    history = exchange_history_continue_point(image)
    if history is not None:
        return history
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    if "凯旋门代表交流赛" not in text:
        return None
    if "继续" not in text:
        return None
    height, width = image.shape[:2]
    if width > height:
        return None
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    button = hsv[1084:1138, 234:486]
    if np.mean((button[:, :, 0] >= 35) & (button[:, :, 0] <= 95)
               & (button[:, :, 1] > 90) & (button[:, :, 2] > 90)) < 0.75:
        return None
    return round(360 * width / 720), round(1110 * height / 1280)


def larc_race_transition_kind(image: np.ndarray, evidence: dict[str, Any]) -> str | None:
    """Identify known no-tap transitions; the caller must retain race wait authority."""
    height, width = image.shape[:2]
    if (width, height) not in {(720, 1280), (1440, 2560)}:
        return None
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    intro_text = (all(label in text for label in ("属性", "技能", "基础能力", "适应性", "前往赛事"))
                  and re.search(r"实况[:：]", text) is not None)
    if text and not intro_text:
        return None
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    if not text:
        return "black" if np.mean(hsv[:, :, 2] < 8) > 0.995 else None
    title = hsv[25:65, 90:630]
    tab = hsv[135:165, 365:500]
    menu = hsv[1140:1200, 610:675]
    # The ready preview has bright action buttons; the narration veil does not.
    if (np.mean((title[:, :, 1] < 60) & (title[:, :, 2] > 175)) > 0.75
            and np.mean((tab[:, :, 0] >= 35) & (tab[:, :, 0] <= 95)
                        & (tab[:, :, 1] > 80) & (tab[:, :, 2] > 120)) > 0.70
            and np.mean((menu[:, :, 1] < 60) & (menu[:, :, 2] > 175)) > 0.50
            and (np.mean(hsv[1080:1280, :, 2] < 115) > 0.85
                 or np.median(hsv[1170:1220, 395:545, 2]) < 160)):
        return "preview"
    return None


@lru_cache(maxsize=1)
def _ss_preview_reward_templates():
    import json
    from pathlib import Path
    from core.larc_reward_reader import LarcRewardReader

    root = Path(__file__).resolve().parents[2] / "assets/larc_support"
    manifest = json.loads((root / "ss_information_templates.json").read_text(encoding="utf-8"))
    labels = [(item["kind"], cv2.imread(str(root / item["file"]), cv2.IMREAD_GRAYSCALE))
              for item in manifest["labels"]
              if item["kind"] in {"stat_gain", "skill_pt", "charming", "practice_good"}]
    return LarcRewardReader(), labels


def _read_ss_preview_reward(canonical: np.ndarray, slot: int) -> dict[str, Any]:
    from core.larc_ss_information import text_mask

    icons, labels = _ss_preview_reward_templates()
    offset = 110 * slot
    icon = icons.read_icon(canonical[267 + offset:298 + offset, 72:103])
    # The shared arrows cannot distinguish attributes from skill PT, and the
    # condition icon cannot distinguish charming from practice-good.
    families = {"numeric_gain": {"stat_gain", "skill_pt"},
                "positive_condition": {"charming", "practice_good"}}
    kind = icon if icon not in families else None
    if icon in families:
        patch = text_mask(canonical[269 + offset:294 + offset, 104:305])
        scores = []
        if patch is not None:
            for label, template in labels:
                ratio = patch.shape[1] / template.shape[1]
                # Preview labels are smaller than the information-page font.
                # Preserve string length; do not stretch unlike text to fit.
                if label not in families[icon] or not 0.82 <= ratio <= 0.96:
                    continue
                padded = cv2.copyMakeBorder(patch, 5, 5, 0, 0, cv2.BORDER_CONSTANT, value=255)
                candidate = cv2.resize(padded, (template.shape[1], round(padded.shape[0] / ratio)),
                                       interpolation=cv2.INTER_LINEAR)
                score = float(cv2.matchTemplate(candidate, template[2:-2, 2:-2], cv2.TM_CCOEFF_NORMED).max())
                scores.append((score, label))
        scores.sort(reverse=True)
        if scores and scores[0][0] >= 0.84 and (len(scores) == 1 or scores[0][0] - scores[1][0] >= 0.05):
            kind = scores[0][1]
    return {"status": "accepted" if kind else "unknown", "kind": kind, "amount": None,
            "earned": False, "reason": "visible_preview_reward" if kind else "preview_reward_unrecognized"}


def _read_ss_preview_bond(hsv: np.ndarray, slot: int, scale: int) -> dict[str, Any]:
    """Read the support-only bond track beside a current SS participant."""
    offset = 110 * slot
    result = {"status": "not_visible", "reason": "bond_bar_absent",
              "fill_ratio_estimate": None, "color": None, "is_max": None,
              "bbox": [210 * scale, (242 + offset) * scale, 293 * scale, (260 + offset) * scale]}
    caps = hsv[249 + offset:254 + offset, [211, 290]]
    if not bool(np.all((np.median(caps[:, :, 2], axis=0) < 155)
                       & (np.median(caps[:, :, 1], axis=0) < 65))):
        return result
    # Rounded outlines shift a pixel across the five rows. Both are needed;
    # a grey background or a covered bar is not evidence of zero bond.
    for start, end in ((243, 248), (254, 259)):
        edge = hsv[start + offset:end + offset, 217:285]
        dark = (edge[:, :, 1] < 65) & (edge[:, :, 2] < 155)
        if float(dark.mean(axis=1).max()) < .9:
            return dict(result, status="abstained", reason="bond_outline_unknown")
    columns = np.median(hsv[248 + offset:254 + offset, 213:289], axis=0)
    colored = (columns[:, 1] > 65) & (columns[:, 2] > 160)
    filled = cv2.morphologyEx(colored.astype(np.uint8)[None, :], cv2.MORPH_CLOSE,
                              np.ones((1, 3), np.uint8))[0] > 0
    if not filled.any():
        return dict(result, status="abstained", reason="bond_fill_unknown")
    end = int(np.flatnonzero(filled)[-1]) + 1
    if not filled[:3].any() or float(filled[:end].mean()) < .9:
        return dict(result, status="abstained", reason="non_prefix_bond_fill")
    hue = float(np.median(columns[colored, 0]))
    color = "cyan" if 80 <= hue <= 110 else "green" if 32 <= hue < 80 else "orange" if 8 <= hue < 32 else None
    if color is None:
        return dict(result, status="abstained", reason="bond_colour_unknown")
    return dict(result, status="accepted", reason="visible_ss_bond_fill", color=color,
                fill_ratio_estimate=round(end / len(filled), 2),
                is_max=False if color in {"cyan", "green"} else None)


def _read_larc_ss_participants(image: np.ndarray, *, expected_count: int, kind: str = 'SS') -> dict[str, Any]:
    """Observe the preview's left-hand rows, never infer them from charge state."""
    result = {
        "schema_version": "larc_ss_participants.v1", "status": "abstained",
        "expected_count": expected_count, "visible_portrait_count": None,
        "participants": [], "reason": "unsupported_frame", "decision_safe": False,
    }
    if (not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3
            or image.dtype != np.uint8
            or image.shape[:2] not in {(1280, 720), (2560, 1440)}):
        return result
    scale = image.shape[1] // 720
    canonical = image if scale == 1 else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(canonical, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(canonical, cv2.COLOR_BGR2GRAY)
    participants = []
    win_rates = []
    for slot in range(5):
        offset = 110 * slot
        badge = hsv[207 + offset:230 + offset, 214:284]
        blue = ((badge[:, :, 0] >= 95) & (badge[:, :, 0] <= 125)
                & (badge[:, :, 1] > 80) & (badge[:, :, 2] > 130))
        green = ((badge[:, :, 0] >= 35) & (badge[:, :, 0] <= 85)
                 & (badge[:, :, 1] > 60) & (badge[:, :, 2] > 130))
        win_band='blue' if float(blue.mean())>=.50 else 'green' if float(green.mean())>=.50 else None
        if win_band is not None:
            win_rates.append({'slot':slot,'band':win_band,'guaranteed':kind=='SSS' or win_band=='blue'})
        face = gray[210 + offset:255 + offset, 111:174]
        # Both badge colors prove a visible row; only blue guarantees an SS win.
        # Face texture prevents
        # a blank/covered portrait from being handed to the run-local roster.
        if win_band is None or float(face.std()) < 20:
            continue
        participants.append({
            "slot": slot,
            "bbox": [80 * scale, (190 + offset) * scale, 205 * scale, (270 + offset) * scale],
            "current_reward": _read_ss_preview_reward(canonical, slot),
            "bond": _read_ss_preview_bond(hsv, slot, scale),
            "win_rate_band": win_band,
            "win_guaranteed": kind=='SSS' or win_band=='blue',
            "win_guarantee_basis": 'sss_scenario_rule' if kind=='SSS' else 'visible_'+win_band+'_badge',
        })
    complete = len(participants) == expected_count and [p["slot"] for p in participants] == list(range(expected_count))
    result.update(
        status="accepted" if complete else "abstained", participants=participants,
        visible_portrait_count=len(participants),
        win_rates=win_rates,win_rates_status='accepted' if len(win_rates)==expected_count
            and [row['slot'] for row in win_rates]==list(range(expected_count)) else 'abstained',
        reason="visible_preview_rows" if complete else "unresolved_portrait_slots",
    )
    return result


@lru_cache(maxsize=1)
def _ss_gain_parser():
    from core.training_parser import TrainingParser
    return TrainingParser(use_mimo=False, recognition_mode="lightweight")


def plan_larc_ss_preview(
    image: np.ndarray, evidence: dict[str, Any], *, allow_execute: bool = True,
    larc_supporter_goal: dict[str, Any] | None = None,
    preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Preview deadline rescues without spending on insufficient known gains."""
    urgent = urgent_larc_supporter_goal(larc_supporter_goal)
    deadline = deadline_larc_supporter_goal(larc_supporter_goal)
    raw_text = str(evidence.get("screen_text", {}).get("normalized_text", ""))
    text = "".join(raw_text.split())
    header_lines = ["".join(line.split()) for line in raw_text.splitlines() if line.strip()]
    has_ss_header = (
        text.startswith(("群星赛", "超星赛"))
        or any(line.startswith(("群星赛", "超星赛")) for line in header_lines[:4])
    )
    count = re.search(r"(?<!\d)([1-5])\s*人", raw_text)
    if (not has_ss_header
            or (not deadline and "全胜时" not in text) or "协助" not in text):
        return None
    ready = int(count.group(1)) if count else None
    if ready is None and re.search(r"\d+\s*人", raw_text) is None:
        observation = read_larc_ss_count(image, stage="preview")
        evidence["larc_ss_count"] = observation
        ready = observation["count"] if observation["status"] == "accepted" else None
    if ready not in ({1, 2, 3, 4, 5} if deadline else {3, 4, 5} if preview_decider else {4, 5}):
        return None
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    label=hsv[1052:1085,612:701]
    pink=((label[:,:,0]>=140)|(label[:,:,0]<=5))&(label[:,:,1]>80)&(label[:,:,2]>130)
    # Both modes retain 群星赛 in the header. The actual SSS control has the
    # pink 超星赛 label; a prose mention elsewhere cannot grant the exception.
    kind='SSS' if '超星赛' in text and float(pink.mean())>=.35 else 'SS'
    ribbon = hsv[1090:1175, 606:702]
    gold = ((ribbon[:, :, 0] >= 15) & (ribbon[:, :, 0] <= 38)
            & (ribbon[:, :, 1] > 100) & (ribbon[:, :, 2] > 150)).astype(np.uint8)
    _, _, components, _ = cv2.connectedComponentsWithStats(gold, connectivity=8)
    chevrons = [row for row in components[1:]
                if row[cv2.CC_STAT_WIDTH] >= 48 and 12 <= row[cv2.CC_STAT_HEIGHT] <= 35
                and row[cv2.CC_STAT_AREA] >= 250]
    if not (any(row[cv2.CC_STAT_TOP] < 28 for row in chevrons)
            and any(row[cv2.CC_STAT_TOP] > 40 for row in chevrons)):
        return None
    points = re.search(r"全胜时[^+]*\+([\d,，]+)积分", text)
    evidence["larc_ss"] = {
        "status": "accepted", "ready_count": ready,
        "stage": "preview", "kind": kind,
        "kind_basis": 'pink_sss_control_and_text' if kind=='SSS' else 'ordinary_ss',
        "consumes_turn": True,
        "supporter_points_if_all_win": int(points.group(1).replace(",", "").replace("，", "")) if points else None,
        "participants_observation": _read_larc_ss_participants(image, expected_count=ready,
            kind=kind),
    }
    from core.training_parser import TrainingData
    gains = TrainingData()
    _ss_gain_parser()._extract_via_dynamic_ocr(
        image, gains, lightweight=True, scenario="larc")
    gain_ocr = gains.stats_gain_ocr or {}
    evidence["larc_ss"].update(
        stats_gain={column["attribute_id"]: column["canonical_value"]
                    for column in gain_ocr.get("columns", [])
                    if column.get("canonical_status") == "accepted"},
        stats_gain_ocr=gain_ocr,
    )
    if urgent:
        gain = evidence["larc_ss"]["supporter_points_if_all_win"]
        covers = gain >= larc_supporter_goal["remaining_points"] if gain is not None else None
        evidence["larc_ss"].update(supporter_goal=larc_supporter_goal,
            gain_covers_shortfall_if_all_win=covers,
            priority_reason="covers_shortfall_if_all_win" if covers else "urgent_progress_no_known_sufficient_alternative")
        if deadline and covers is not True:
            # A projected shortfall is not an input/permission failure. A
            # recognized SS may still be the best available ordinary action.
            evidence["larc_ss"].update(priority_reason="deadline_best_effort",
                projected_goal_shortfall=True, goal_completion_claimed=False)
    comparison = preview_decider(evidence["larc_ss"]) if preview_decider is not None else None
    if comparison is not None:
        evidence["larc_ss"]["comparison"] = comparison
    from core.auto_trainer.larc_ss_visible import ss_victory_evidence
    victory=ss_victory_evidence(evidence['larc_ss'])
    evidence['larc_ss']['win_policy']=victory
    from core.auto_trainer.larc_ss_deadline_execution import current_execution_permit
    deadline_permit=current_execution_permit(evidence['larc_ss'])
    deadline_known_risk=deadline_permit.get('executable') is True
    if deadline_known_risk:
        evidence['larc_ss']['ss_deadline']=deadline_permit['status']
        evidence['larc_ss']['win_policy']={**victory,'known_risk_accepted_by_user_deadline':True,
            'risk_bands':deadline_permit['risk_bands']}
    if not victory['guaranteed'] and not deadline_known_risk and comparison is not None and comparison.get('selected') is True:
        comparison={**comparison,'selected':False,'reason':'ss_victory_unverified',
                    'requested_selection':True}
        evidence['larc_ss']['comparison']=comparison
    if (not allow_execute or not(victory['guaranteed']or deadline_known_risk) or (comparison is not None and comparison.get("selected") is not True)
            or (comparison is None and ready < (1 if deadline else 4 if urgent else 5))):
        return {"action": "stop", "reason": "ready_larc_ss_preview",
                "route_state": "training_menu", "evidence": evidence}
    evidence["larc_ss"]["policy"] = "local_candidate_comparison" if comparison is not None else "urgent_supporter_goal" if urgent else "full_five_collection"
    height, width = image.shape[:2]
    return {
        "action": "tap", "reason": "larc_ss_confirm", "route_state": "training_menu",
        "tap": {"coordinate_name": "safe_advance.larc_ss_confirm",
                "x": round(654 * width / 720), "y": round(1120 * height / 1280),
                "scaled": False}, "evidence": evidence,
    }


def plan_larc_ss_entry(
    image: np.ndarray, evidence: dict[str, Any], *, allow_execute: bool = True,
    larc_supporter_goal: dict[str, Any] | None = None,
    preview_only: bool = False,
) -> dict[str, Any] | None:
    """Open full SS, urgent four-member SS, or a nonzero last-turn preview."""
    raw_text = str(evidence.get("screen_text", {}).get("normalized_text", ""))
    text = "".join(raw_text.split())
    count = re.search(r"(?<!\d)([0-5])\s*人", raw_text)
    if "群星赛" not in text:
        return None
    ready = int(count.group(1)) if count else None
    if ready is None and re.search(r"\d+\s*人", raw_text) is None:
        observation = read_larc_ss_count(image, stage="entry")
        evidence["larc_ss_count"] = observation
        ready = observation["count"] if observation["status"] == "accepted" else None
    if ready is None:
        return None
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    button = hsv[1035:1135, 605:705]
    blue = ((button[:, :, 0] >= 90) & (button[:, :, 0] <= 125)
            & (button[:, :, 1] > 70) & (button[:, :, 2] > 130))
    if float(np.mean(blue)) < 0.35:
        return None
    evidence["larc_ss"] = {"status": "accepted", "ready_count": ready, "stage": "entry"}
    urgent = urgent_larc_supporter_goal(larc_supporter_goal)
    deadline = deadline_larc_supporter_goal(larc_supporter_goal)
    if urgent:
        evidence["larc_ss"]["supporter_goal"] = larc_supporter_goal
    if (not allow_execute and not preview_only) or ready < (1 if deadline else 3 if preview_only else 4 if urgent else 5):
        return None
    evidence["larc_ss"]["policy"] = "urgent_supporter_goal" if urgent else "full_five_preview"
    height, width = image.shape[:2]
    return {
        "action": "tap", "reason": "larc_ss_open", "route_state": "training_menu",
        "tap": {"coordinate_name": "safe_advance.larc_ss_open",
                "x": round(654 * width / 720), "y": round(1080 * height / 1280),
                "scaled": False}, "evidence": evidence,
    }


def plan_larc_explanation(
    image: np.ndarray, evidence: dict[str, Any]
) -> dict[str, Any] | None:
    """Dismiss a recognized L'Arc explanation using its bottom control."""
    from core.auto_trainer.event_options import help_carousel_visible
    if help_carousel_visible(image):
        height, width = image.shape[:2]
        evidence["larc_explanation"] = {"bottom_control": "close",
            "kind": "scenario_explanation", "basis": "paged_illustration_footer"}
        return {"action": "tap", "reason": "larc_explanation_continue", "route_state": "modal",
            "tap": {"coordinate_name": "safe_advance.larc_explanation_continue",
                    "x": round(width * .5), "y": round(1205 * height / 1280), "scaled": False},
            "evidence": evidence}
    text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
    supporter_shortage = "目标所需协助者积分不足" in text and "关闭" in text
    supporter_reward = not supporter_shortage and "获得协助者积分" in text and "关闭" in text
    if not (supporter_reward or supporter_shortage) and (not any(label in text for label in ("继续", "关闭")) or not any(
        topic in text for topic in ("凯旋门", "群星赛", "海外适应性", "海外远征")
    )):
        return None
    height, width = image.shape[:2]
    if width > height:
        return None
    hsv = cv2.cvtColor(cv2.resize(image, (720, 1280)), cv2.COLOR_BGR2HSV)
    green = ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 95)
             & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 90))
    white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 175)
    schedule_point = larc_exchange_schedule_continue_button(image, evidence)
    if supporter_reward:
        if (np.mean(green[392:435, 20:700]) < 0.70 or np.mean(white[455:775, 30:690]) < 0.75
                or np.mean(white[810:860, 240:480]) < 0.70):
            return None
        schedule_point = (round(360 * width / 720), round(834 * height / 1280))
    elif supporter_shortage:
        if (np.mean(green[302:345, 20:700]) < 0.70 or np.mean(white[365:975, 30:690]) < 0.75
                or np.mean(white[887:947, 240:480]) < 0.70):
            return None
        schedule_point = (round(360 * width / 720), round(917 * height / 1280))
    button = white if "关闭" in text else green
    if schedule_point is None and (np.mean(button[1178:1232, 230:492]) < 0.75 or np.mean(white[975:1132, 14:704]) < 0.65):
        return None
    evidence["larc_explanation"] = {
        "bottom_control": "close" if "关闭" in text else "continue",
        "kind": "exchange_schedule" if schedule_point is not None else "scenario_explanation",
    }
    if supporter_reward:
        evidence["larc_explanation"]["kind"] = "supporter_points_reward"
    elif supporter_shortage:
        evidence["larc_explanation"]["kind"] = "supporter_points_shortage"
    return {
        "action": "tap", "reason": "larc_explanation_continue", "route_state": "modal",
        "tap": {"coordinate_name": "safe_advance.larc_explanation_continue",
                "x": schedule_point[0] if schedule_point else round(width * 0.5),
                "y": schedule_point[1] if schedule_point else round(1205 * height / 1280),
                "scaled": False},
        "evidence": evidence,
    }


_PAGE_ROUTES = {
    "ready_main_menu": ("training_home", "local_decision"),
    "ready_training_menu": ("training", "local_decision"),
    "ready_larc_ss_preview": ("ss_preview", "local_decision"),
    "larc_ss_confirm": ("ss_preview", "safe_advance"),
    "larc_ss_open": ("training", "safe_advance"),
    "return_main_menu_from_training": ("training", "safe_advance"),
    "event_choice": ("event_choice", "event_route"),
    "larc_tutorial_skip": ("tutorial_topics", "safe_advance"),
    "larc_tutorial_decline": ("tutorial_offer", "safe_advance"),
    "larc_ss_information_close": ("ss_information", "safe_advance"),
    "race_day_main_menu": ("race_day", "race_route"),
    "race_list": ("race_list", "race_route"),
    "fan_goal_warning_handoff": ("fan_warning", "local_decision"),
    "continue_goal_remaining_result": ("goal_summary", "safe_advance"),
    "continue_goal_achievement_result": ("goal_result", "safe_advance"),
    "inheritance_factor_confirm": ("inheritance", "safe_advance"),
    "confirm_rest": ("rest_confirmation", "safe_advance"),
    "confirm_rest_outing": ("rest_confirmation", "safe_advance"),
    "select_shorten_all_events": ("shorten_settings", "safe_advance"),
    "skip_larc_opening_movie": ("opening_movie", "safe_advance"),
    "confirm_larc_opening_skip": ("opening_movie", "safe_advance"),
    "larc_opening_movie_pending": ("opening_movie", "safe_advance"),
    "larc_goal_start_animation": ("goal_start", "safe_advance"),
    "enable_shorten_settings_skip_toggle": ("shorten_settings", "safe_advance"),
    "shorten_settings_skip_loading": ("shorten_settings", "safe_advance"),
    "uncheck_do_not_shorten_unread": ("shorten_settings", "safe_advance"),
    "shorten_unread_checkbox_unconfirmed": ("shorten_settings", "safe_advance"),
    "confirm_shorten_all_events": ("shorten_settings", "safe_advance"),
    "career_end_confirmation": ("career_end", "local_decision"),
    "open_career_end_finish_confirmation": ("career_end", "safe_advance"),
    "cancel_career_end_finish": ("finish_confirmation", "safe_advance"),
    "finish_training_confirm": ("finish_confirmation", "safe_advance"),
    "terminal_rating_verified": ("training_rating", "terminal"),
    "resource_prompt": ("resource_prompt", "safety_gate"),
    "resource_prompt_unreadable": ("resource_prompt", "safety_gate"),
    "loading": ("loading", "safe_advance"),
    "training_action_animation_wait": ("training_animation", "safe_advance"),
}


def annotate_larc_page(
    decision: dict[str, Any], route_policy: SafeAdvanceRoutePolicy | None = None,
) -> dict[str, Any]:
    """Describe the matched page and handoff without granting execution authority.

    Reuse the existing action transitions; do not maintain a second route ledger.
    This metadata never changes the decision, pending action, or executor gates.
    """
    evidence = decision["evidence"]
    reason = decision.get("reason")
    state = observed_route_state(decision)
    page, owner = _PAGE_ROUTES.get(reason, ("unknown", None))
    if reason == "larc_explanation_continue":
        kind = evidence["larc_explanation"].get("kind")
        page = "supporter_reward" if kind == "supporter_points_reward" else kind
        owner = "safe_advance"
    elif reason == "larc_adaptation_open":
        page = "race_day" if evidence.get("larc_race_day", {}).get("verified") else "training_home"
        owner = "larc_adaptation"
    elif str(reason).startswith("larc_adaptation_"):
        page, owner = "adaptation", "larc_adaptation"
    elif reason in {"enable_bottom_skip_toggle", "bottom_skip_double_enabled", "advance_dialogue"}:
        page, owner = ("training_home" if state == "main_menu" else "dialogue"), "safe_advance"
    elif reason == "continue_goal_achievement_result":
        text = str(evidence.get("screen_text", {}).get("normalized_text", ""))
        if "已达成所有目标" in text or "养成目标达成" in text:
            page = "all_goals_complete"
    if reason == "return_main_menu_from_training" and evidence.get("larc_ss", {}).get("stage") == "preview":
        page = "ss_preview"
    evidence["larc_page"] = {
        "schema_version": "uma.larc_page.v1", "page_id": page, "owner": owner,
        "route_state": state,
        "expected_next_states": list(expected_next_states_for_decision(decision)),
        "allowed_observed_states": list(route_policy.allowed_observed_states) if route_policy else None,
        "in_expected_scope": route_policy.allows_observed_state(state) if route_policy else None,
    }
    return decision


_RACE_PAGE_ROUTES = {
    "ready_main_menu": ("training_home", "local_decision", "main_menu"),
    "ready_training_menu": ("training", "local_decision", "training_menu"),
    "post_race_dialogue": ("post_race_dialogue", "safe_advance", "dialogue"),
    "event_choice": ("event_choice", "event_route", "event_choice"),
    "goal_achievement_loading": ("goal_animation", "safe_advance", "loading"),
    "goal_achievement_result": ("goal_result", "safe_advance", "dialogue"),
    "resource_prompt": ("resource_prompt", "safety_gate", "resource_prompt"),
    "race_day": ("race_day", "race_route", "race_day_main_menu"),
    "race_day_main_menu": ("race_day", "race_route", "race_day_main_menu"),
    "race_list": ("race_list", "race_route", "race_list"),
    "race_detail": ("race_detail", "race_route", "race_detail"),
    "race_preview": ("race_preview", "race_route", "race_preview"),
    "race_confirm": ("race_confirmation", "race_route", "race_detail_confirm"),
    "race_detail_confirm": ("race_confirmation", "race_route", "race_detail_confirm"),
    "race_entry_confirm": ("race_confirmation", "race_route", "race_entry_confirm"),
    "race_playback_confirm": ("race_confirmation", "race_route", "race_playback_confirm"),
    "race_scene": ("race_scene", "race_route", "race_scene"),
    "race_menu": ("race_menu", "race_route", "race_menu"),
    "race_result": ("race_result", "race_route", "race_result"),
    "loading": ("loading", "race_route", "loading"),
    "race_detail_loading": ("loading", "race_route", "race_detail_loading"),
    "transition": ("transition", "race_route", "transition"),
}


def annotate_larc_race_page(decision: dict[str, Any]) -> dict[str, Any]:
    """Describe only an already matched race page, without a second recognition pass.

    RaceRouteSession uses single-use capabilities, not SafeAdvanceRoutePolicy.
    Leave common-route scope/successors unset; they cannot authorize this action.
    """
    evidence = decision["evidence"]
    reason = str(decision.get("reason", ""))
    route = decision.get("route_state") or reason.split(":", 1)[0]
    page, owner, state = _RACE_PAGE_ROUTES.get(route, ("unknown", None, None))
    transition = evidence.get("larc_race_transition", {}).get("kind")
    if transition in {"black", "preview"}:
        page, owner, state = f"race_{transition}_transition", "race_route", "race_scene"
    elif evidence.get("count_portrait_watch_controls") is not None:
        page, owner, state = "race_camera", "race_route", "race_scene"
    elif evidence.get("larc_race_participants", {}).get("verified"):
        page, owner, state = "race_participants", "race_route", "race_scene"
    elif evidence.get("larc_exchange_schedule", {}).get("verified"):
        page, owner, state = "exchange_schedule", "race_route", "race_result"
    elif evidence.get("larc_adaptation_warning", {}).get("verified"):
        page, owner, state = "adaptation_warning", "race_route", "race_entry_confirm"
    elif evidence.get("larc_explanation", {}).get("kind") == "supporter_points_reward":
        page, owner, state = "supporter_reward", "race_route", "modal"
    elif reason == "close_trophy_reward":
        page, owner, state = "trophy_reward", "race_route", "race_result"
    elif page == "race_result":
        text = str(evidence.get("screen_text", {}).get("normalized_text", ""))
        if "粉丝数" in text and "获得道具" in text:
            page = "race_rewards"
    if owner == "local_decision" and decision.get("action") not in {"stop", "handoff"}:
        owner = "race_route"
    evidence["larc_page"] = {
        "schema_version": "uma.larc_page.v1", "page_id": page, "owner": owner,
        "route_state": decision.get("route_state") or state,
        "expected_next_states": None, "allowed_observed_states": None,
        "in_expected_scope": None,
    }
    return decision
