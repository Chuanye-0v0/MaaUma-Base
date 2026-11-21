"""L'Arc exchange race, reward footer, and rank-point overlay handlers."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry
from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_perception import is_goal_achievement_title
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_race_page,
    larc_race_transition_kind,
    plan_larc_explanation,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate
from core.auto_trainer.runtime_control import reobserving_step, controlled_sleep
from core.auto_trainer.race_clock_retry import _ordinary_result_blocked, retry_prompt_visible
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from .._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    RaceRouteExecutor,
    RaceRoutePlanner,
    SafeAdvancePlanner,
    TrainingMenuPrechecker,
    _current_clock_retry,
    analyze_screen_text,
    classify_landscape_race_screen,
    fuse_perception_with_text,
    match_main_menu_named_goal,
    rr_attr,
)
from ..models import (
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RACE_LIST_GRADE_REOBSERVATION_RETRIES,
    RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS,
    RACE_ROUTE_CONFIRM_TOKEN,
    RACE_SCENE_WAIT_REASONS,
    RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS,
    RESULT_TRANSITION_RECAPTURE_LIMIT,
    SUPPORTED_RACE_ROUTE_SCREEN_SIZES,
    RaceRoutePolicy,
    RaceRouteSession,
    _RACE_ACCOUNT_CONFLICT_TEXT,
    is_race_scene_wait_result,
    larc_exchange_entry_released,
)


_LARC_OVERLAY_EXCLUDED_CAREER_TERMS = (
    "体力",
    "缩短版",
    "记录",
    "启发等级提升",
    "适应性积分提升了",
    "群星赛",
    "还剩",
    "主线事件",
    "协助卡事件",
    "养成优俊少女事件",
    "剧本事件",
)


class LarcExpeditionHandlerMixin:
    """L'Arc representative exchange and overseas race handlers."""

    @staticmethod
    def _larc_reward_footer(img: np.ndarray, visible_text: str | None) -> dict[str, Any] | None:
        """A rank, coach bubble and divided reward panel identify this result page.

        Locate the broad green footer's filled band rather than its mascot or
        the rank animation's fixed tap point. Missing/ambiguous controls retain
        the result owner and wait for a new observation.
        """
        height, width = img.shape[:2]
        if width >= height:
            return None
        text = "".join(str(visible_text or "").split())
        if any(term in text for term in (
                "赛事一览", "赛事详情", "是否参加", "是否出赛", "取消", "选项效果",
                "主线事件", "协助卡事件", "养成优俊少女事件", *_RACE_ACCOUNT_CONFLICT_TEXT)):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 175)
        gold = ((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 40)
                & (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 120))
        green = ((hsv[:, :, 0] >= 38) & (hsv[:, :, 0] <= 92)
                 & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 80))
        def ratio(mask, x0, x1, y0, y1):
            region = mask[int(height*y0):int(height*y1), int(width*x0):int(width*x1)]
            return float(np.mean(region)) if region.size else 0.0
        ratios = {
            "coach_bubble_white": ratio(white, .20, .95, .015, .08),
            "rank_gold": ratio(gold, .04, .45, .15, .33),
            "reward_panel_white": ratio(white, .02, .98, .48, .875),
        }
        # Two broad, separated section dividers distinguish rewards from a
        # white ranking table, race-detail card or ordinary character page.
        rows = np.mean(green[int(height*.50):int(height*.86),
                             int(width*.04):int(width*.95)], axis=1)
        header_rows = np.flatnonzero(rows > .70)
        groups = np.split(header_rows, np.flatnonzero(np.diff(header_rows) > 1) + 1)
        bands = [group for group in groups if len(group) >= height*.006]
        if not (ratios["coach_bubble_white"] > .70 and ratios["rank_gold"] > .10
                and ratios["reward_panel_white"] > .65 and len(bands) >= 2
                and bands[-1][0]-bands[0][-1] > height*.04):
            return None
        proof: dict[str, Any] = {"page": "ranked_race_rewards",
            "ratios": {key: round(value, 4) for key, value in ratios.items()},
            "point": None, "button_bbox": None}
        # Ranked rewards can place Next on the right beside another control.
        # Search the complete current footer; a centered crop clipped that
        # enabled Next and shadowed the later generic result handler.
        x0, x1 = int(width*.02), int(width*.98)
        y0, y1 = int(height*.86), int(height*.98)
        mask = green[y0:y1, x0:x1].astype(np.uint8)*255
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if not (.24 <= w/width <= .55 and .025 <= h/height <= .12
                    and area > width*height*.004
                    and width*.22 <= x0+x+w/2 <= width*.88
                    and x > 0 and y > 0 and x+w < mask.shape[1] and y+h < mask.shape[0]):
                continue
            # An outline alone is not an enabled control. Its lower half must
            # contain a broad filled band; use that band's current vertical
            # position, safely below the mascot that overlaps the white area.
            occupancy = np.mean(mask[y+h//2:y+h, x:x+w] > 0, axis=1)
            filled = np.flatnonzero(occupancy > .55)
            if len(filled) < height*.008:
                continue
            cy = y0+y+h//2+int(np.median(filled))
            candidates.append({"point": [x0+x+w//2, cy], "button_bbox": [x0+x, y0+y, w, h]})
        if len(candidates) == 1:
            proof.update(candidates[0])
        return proof

    @staticmethod
    def _larc_rank_points_overlay(img: np.ndarray, visible_text: str, *, visual_only=False) -> dict[str,Any] | None:
        """Identify the result overlay; OCR values are optional observations.

        Callers own continuation policy/session checks. The foreground badge
        and award geometry identify this page even when OCR misses its rank,
        anchors or points. This proof never certifies a placing or a win.
        """
        height, width = img.shape[:2]
        if width >= height or width * 16 != height * 9:
            return None
        text=''.join(str(visible_text).split())
        if not visual_only and any(
            word in text
            for word in (
                '更改战术', '前往赛事', '查看结果', '参赛条件', '获得奖励',
                '支付', '消耗', '萝卜', '闹钟', '购买', '充值', '宝石', '钻石', '预览',
                *_LARC_OVERLAY_EXCLUDED_CAREER_TERMS,
            )
        ):
            return None
        point_text=re.sub(r'[^\S\r\n]', '', str(visible_text))
        points=re.findall(r'([0-9]{1,3}(?:[,，.][0-9]{3})+|[0-9]+)积分',point_text)
        ranks=re.findall(r'第([1-9][0-9]*)名|(?<![0-9A-Za-z.+-])([1-9][0-9]*)第名|(?<![0-9A-Za-z.+-])([1-9][0-9]*)名第',text)
        rank_values=[int(next(value for value in row if value)) for row in ranks]
        # Conflicting or incomplete captions remain unknown metadata. Page
        # identity, rather than a unique rank read, owns harmless continuation.
        earned=int(re.sub(r'[,，.]','',points[0])) if len(points)==1 else None
        image=img if img.shape[:2]==(1280,720) else cv2.resize(img,(720,1280))
        hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
        rank=hsv[710:890,205:500]
        rank_white = float(np.mean((rank[:,:,1]<75)&(rank[:,:,2]>185)))
        if rank_white < .20:
            return None
        for compact, badge, award in (
            (False, hsv[875:952,235:309], hsv[882:947,315:575]),
            (True, hsv[875:952,285:355], hsv[882:947,355:465]),
        ):
            blue = (badge[:,:,0]>92)&(badge[:,:,0]<125)&(badge[:,:,1]>110)&(badge[:,:,2]>100)
            ratio = {
                'rank_white':rank_white,
                'pt_badge_blue':float(np.mean(blue)),
                'pt_badge_white':float(np.mean((badge[:,:,1]<75)&(badge[:,:,2]>185))),
                'award_white':float(np.mean((award[:,:,1]<75)&(award[:,:,2]>185))),
                'award_orange':float(np.mean((award[:,:,0]>5)&(award[:,:,0]<35)&(award[:,:,1]>75)&(award[:,:,2]>120))),
            }
            if (ratio['pt_badge_blue']<.12 or ratio['pt_badge_white']<.05
                    or ratio['award_white']<.12 or ratio['award_orange']<.08):
                continue
            if compact:
                # A short award (e.g. 0积分) centers the Pt emblem and label
                # as one row. Require its blue emblem shape, not a blue patch
                # in the character art, and keep real result controls separate.
                if any(label in text for label in ('重新挑战','下一页','下一步',*_RACE_ACCOUNT_CONFLICT_TEXT)):
                    continue
                mask = cv2.morphologyEx(blue.astype(np.uint8), cv2.MORPH_CLOSE,
                                        np.ones((3,3), dtype=np.uint8))
                _, _, boxes, _ = cv2.connectedComponentsWithStats(mask)
                if not any(46 <= w <= 65 and 43 <= h <= 65
                           and .25 <= area/(w*h) <= .55 for _, _, w, h, area in boxes[1:]):
                    continue
            return {'rank':rank_values[0] if len(rank_values)==1 else None,'displayed_points':earned,
                    'basis':'foreground_rank_blue_pt_badge_and_orange_white_points',
                    'click_caption_required':False,'ratios':{key:round(value,4) for key,value in ratio.items()},
                    **({'compact_award':True} if compact else {})}
        return None

    def larc_exchange_entry_postcondition(
        self,
        post_path: str | Path,
        *,
        entry_decision: dict[str, Any],
        session: RaceRouteSession,
        visible_text: str | None = None,
    ) -> dict[str, Any] | None:
        """Read the exchange hub's direct detail successor without executing it."""
        evidence = entry_decision.get("evidence", {})
        text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
        verification = entry_decision.get("locked_target_verification")
        if (
            entry_decision.get("action") != "tap"
            or entry_decision.get("reason") != "enter_race_from_race_day"
            or "凯旋门代表交流赛" not in text
            or not isinstance(verification, dict)
            or session.pending_locked_target_race != verification
        ):
            return None
        # Reuse the existing source/action/single-use contract; never restage the
        # caller's session or consume its capability during this read-only check.
        try:
            RaceRouteSession().stage_locked_target_race_entry(entry_decision)
        except ValueError:
            return None
        successor = self.plan(
            post_path,
            policy=RaceRoutePolicy(scenario="larc", allow_confirm_race=True,visible_text=visible_text),
            session=session,
        )
        if (
            successor.get("action") == "tap"
            and successor.get("reason") == "confirm_race_entry"
            and successor.get("consume_target_race_verification") is True
            and successor.get("evidence", {}).get("prior_target_race_verification") == verification
        ):
            return successor
        return None

    @staticmethod
    def _larc_exchange_owner(session, evidence, *, policy, current_detail=False):
        if (policy.scenario != "larc" or session is None or session.scheduled_binding_active
                or policy.inspection_only or policy.wbc_only or policy.count_reserved_only):
            return None
        if any(value is not None for value in (
            session.pending_target_race, session.pending_scenario_final_race,
            session.pending_fan_goal_recommendation, session.pending_fan_goal_shortfall_selected,
            session.pending_wbc_hub_race, session.pending_gur_race,
        )):
            return None
        text = "".join(str(evidence.get("screen_text", {}).get("normalized_text", "")).split())
        if (evidence.get("screen_text", {}).get("dangerous")
                or any(label in text for label in ("尼尔锦标", "福伊锦标", "凯旋门奖"))):
            return None
        if current_detail:
            # A missing title is compatible with a retained owner. A readable
            # different title is a conflict: cached exchange intent cannot
            # override current foreground identity. Reuse the existing race
            # title recognizer rather than requiring an exact question string.
            from core.auto_trainer.race_clock_retry import _result_text_titles
            caption = str(evidence.get("screen_text", {}).get("normalized_text", ""))
            foreground = caption.split("赛事详情", 1)[-1]
            foreground = re.split(
                r"陆间|草地|泥地|比赛难关|赛事信息|已攻克的难关|是否|要参加|要出赛|取消|参赛",
                foreground, maxsplit=1)[0]
            conflicts = []
            compact_foreground = "".join(foreground.split())
            fragments = [*re.split(r"[\s:：,，。!?！？]+", foreground), compact_foreground]
            for fragment in dict.fromkeys(fragments):
                # OCR can omit the last 标 of a named 锦标 race. Completion is
                # used only to detect conflict, never to authorize entry.
                candidate = fragment + "标" if len(fragment) >= 3 and fragment.endswith("锦") else fragment
                for title in _result_text_titles(candidate):
                    # The title recognizer also returns category/deictic
                    # phrases. These do not name another race; nor does this
                    # filter let them establish a fresh owner below.
                    generic_phrase = re.fullmatch(
                        r"(?:(?:本|该|此|这|某)(?:场|次|项|回)?|当前|目标|普通|国内|海外)?"
                        r"(?:比赛|赛事|(?:代表)?交流赛)", title)
                    if (generic_phrase is None and "代表交流赛" not in title
                            and "凯旋门代表交流" not in title):
                        conflicts.append(title)
            if conflicts:
                evidence["larc_exchange_identity_conflict"] = {
                    "source": "current_detail_title", "titles": conflicts}
                return None
        path = Path(str(evidence.get("image", "")))
        if not path.is_file():
            return None
        img = cv2.imread(str(path))
        if img is None:
            return None
        # Ownership and live controls are independent facts. A cached intent
        # cannot click a blank page, and a generic confirm cannot name a race.
        detail_controls = (current_detail and RaceRoutePlanner._looks_like_race_modal(img)
                           and RaceRoutePlanner._primary_confirm_button(
                               evidence.get("perception", {}), img) is not None)
        warning_controls = evidence.get("larc_adaptation_warning", {}).get("verified") is True
        if not (detail_controls or warning_controls):
            return None
        pending = session.pending_larc_exchange
        if pending is not None:
            if (isinstance(pending, dict) and pending.get("race_kind") == "larc_exchange"
                    and pending.get("source") == "larc_exchange.current_detail"):
                evidence["prior_larc_exchange_verification"] = dict(pending)
                return dict(pending)
            return None
        if not detail_controls or not policy.allow_confirm_race:
            return None
        intent = session.pending_locked_target_race or {}
        owned_intent = (intent.get("source") == "race_day_main_menu.locked_target"
                        and intent.get("race_kind") == "larc_exchange")
        named_exchange = "代表交流赛" in text or "凯旋门代表交流" in text
        if not (owned_intent or named_exchange):
            return None
        proof = {"source": "larc_exchange.current_detail", "race_kind": "larc_exchange",
                 "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        evidence["larc_exchange_adaptation_advice"] = {
            "incomplete": "未克服" in text,
            "reason": "普通交流赛可带未克服难关参赛，适应性作为策略参考。",
        }
        return proof

    @staticmethod
    def _larc_exchange_confirm_tap(state, button, evidence, proof, *, consume_target=False):
        decision = RaceRoutePlanner._tap("confirm_race_entry", state,
            "race_confirm.primary", button, evidence)
        decision["larc_exchange_verification"] = proof
        if consume_target:
            decision["consume_target_race_verification"] = True
        return decision
