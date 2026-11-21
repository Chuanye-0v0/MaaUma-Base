"""RaceRoutePlanner orchestrator composed from classifier and handler mixins."""
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
from core.auto_trainer.speed_toggle import SpeedTogglePlanner
from core.auto_trainer.transition_wait import (
    TransitionBudget,
    career_comic_loading_visible,
    evaluate_gentle_transition_wait,
)
from ._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    SafeAdvancePlanner,
    TrainingMenuPrechecker,
    _current_clock_retry,
    analyze_screen_text,
    classify_landscape_race_screen,
    fuse_perception_with_text,
    match_main_menu_named_goal,
    rr_attr,
)
from .models import (
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
from .classifier import RaceClassifierMixin
from .handlers import (
    ClockRetryHandlerMixin,
    EntryAndListHandlerMixin,
    LarcExpeditionHandlerMixin,
    SceneAndResultHandlerMixin,
)


@dataclass
class _RacePlanState:
    early_preview_actions: dict[str, Any] | None = None
    safe_planner: Any = None
    training_check: Any = None
    is_visual_race_list: bool = False
    early_selected_race_card: dict[str, Any] | None = None
    early_race_list_title: bool = False
    early_text_verified_race_list: bool = False


class RaceRoutePlanner(
    RaceClassifierMixin,
    ClockRetryHandlerMixin,
    LarcExpeditionHandlerMixin,
    EntryAndListHandlerMixin,
    SceneAndResultHandlerMixin,
):
    """Plan one guarded race-route action from a screenshot."""

    def __init__(
        self,
        action_table: ActionTable | None = None,
        *,
        fan_goal_text_reader: Callable[[np.ndarray], tuple[str, str]] | None = None,
        count_card_reader: Callable[[Path], dict[str, Any]] | None = None,
    ):
        self.action_table = action_table or ActionTable()
        self.count_card_reader = count_card_reader
        self.fan_goal_text_reader = (
            fan_goal_text_reader or self._localized_fan_goal_texts
        )
        self._fan_shortfall_regions_reader = LocalRapidOCRScreenTextReader()
        self._default_gentle_budget = TransitionBudget()
        self._last_plan_image: np.ndarray | None = None

    def plan(
        self,
        image_path: str | Path,
        *,
        policy: RaceRoutePolicy | None = None,
        session: RaceRouteSession | None = None,
        allow_gentle_transition_wait: bool = False,
        gentle_transition_budget: TransitionBudget | None = None,
    ) -> dict[str, Any]:
        policy = policy or RaceRoutePolicy()
        self._last_plan_image = None
        decision = self._plan(image_path, policy=policy, session=session)
        if allow_gentle_transition_wait or gentle_transition_budget is not None:
            effective_budget = (
                gentle_transition_budget
                if gentle_transition_budget is not None
                else self._default_gentle_budget
            )
            img = (
                self._last_plan_image
                if self._last_plan_image is not None
                else (
                    None
                    if decision.get("reason") == "image_read"
                    else read_image_with_retry(Path(image_path))
                )
            )
            if img is not None:
                decision = evaluate_gentle_transition_wait(
                    decision,
                    img,
                    budget=effective_budget,
                    visible_text=policy.visible_text,
                    owner='race_route',
                )
        if (
            gentle_transition_budget is None
            and (
                decision.get("action") in {"tap", "swipe", "handoff"}
                or decision.get("handoff") is True
                or (
                    decision.get("action") == "stop"
                    and decision.get("reason")
                    in {
                        "ready_main_menu",
                        "ready_training_menu",
                        "ready_larc_ss_preview",
                        "race_day_main_menu",
                        "global_home",
                        "race_inspection_complete",
                    }
                )
            )
        ):
            self._default_gentle_budget.reset()
        self._last_plan_image = None
        self.last_decision = annotate_larc_race_page(decision) if policy.scenario == 'larc' else decision
        return self.last_decision

    def _plan(
        self,
        image_path: str | Path,
        *,
        policy: RaceRoutePolicy,
        session: RaceRouteSession | None,
    ) -> dict[str, Any]:
        path = Path(image_path)
        img = read_image_with_retry(path)
        self._last_plan_image = img
        if img is None:
            return self._stop("image_read", image_path=str(path))

        perception = Perception().analyze_image(img)
        landscape_kind = classify_landscape_race_screen(img)
        if landscape_kind is not None:
            perception["landscape_race_kind"] = landscape_kind
        screen_text = analyze_screen_text(policy.visible_text) if policy.visible_text is not None else None
        if screen_text is not None:
            perception = fuse_perception_with_text(perception, screen_text)
        screen = str(perception.get("screen", "unknown"))
        # A white costume can resemble a dialogue panel. While a requested
        # result is still pending, require career chrome before giving up
        # ownership; generic page labels alone are not a handoff signal.
        can_handoff_dialogue = True
        if (policy.scenario == "larc" and session is not None
                and (policy.post_entry_only or session.result_transition_pending)):
            from core.auto_trainer.speed_toggle import SpeedTogglePlanner
            text = str((screen_text or {}).get("normalized_text", ""))
            can_handoff_dialogue = (
                (SpeedTogglePlanner._bottom_skip_level(img) in {"off", "single", "double"}
                    and any(term in text for term in ('跳过','记录','主线事件','养成优俊少女事件','协助卡事件')))
                or all(term in text for term in ("还剩", "目标", "体力")))
        evidence: dict[str, Any] = {"image": str(path), "perception": perception}
        current_text = "".join(str((screen_text or {}).get("normalized_text", "")).split())
        observed_free_control = (policy.ordinary_observed_control_scope()
            and not policy.clock_retry_unresolved
            and not (session is not None and session.clock_retry_entry_pending)
            and not _ordinary_result_blocked(policy.visible_text)
            and not any(label in current_text for label in ("参赛!", "要参加比赛", "播放设置")))
        result_owner = (policy.post_entry_only or session is not None and session.result_transition_pending)
        state = _RacePlanState()
        for step in (
            lambda: self._plan_stage_larc_clock_and_expedition(
                path=path,
                img=img,
                perception=perception,
                screen_text=screen_text,
                evidence=evidence,
                policy=policy,
                session=session,
                can_handoff_dialogue=can_handoff_dialogue,
                observed_free_control=observed_free_control,
            ),
            lambda: self._plan_stage_preview_home_and_detail_confirm(
                state=state,
                path=path,
                img=img,
                perception=perception,
                screen_text=screen_text,
                screen=screen,
                evidence=evidence,
                current_text=current_text,
                policy=policy,
                session=session,
                observed_free_control=observed_free_control,
                result_owner=result_owner,
            ),
            lambda: self._plan_stage_landscape_and_portrait_results(
                state=state,
                path=path,
                img=img,
                perception=perception,
                landscape_kind=landscape_kind,
                screen_text=screen_text,
                screen=screen,
                evidence=evidence,
                policy=policy,
                session=session,
                can_handoff_dialogue=can_handoff_dialogue,
                observed_free_control=observed_free_control,
                result_owner=result_owner,
            ),
            lambda: self._plan_stage_race_list_and_modals(
                state=state,
                path=path,
                img=img,
                perception=perception,
                screen_text=screen_text,
                screen=screen,
                evidence=evidence,
                current_text=current_text,
                policy=policy,
                session=session,
                observed_free_control=observed_free_control,
                result_owner=result_owner,
            ),
        ):
            res = step()
            if res is not None:
                return res
        return self._plan_stage_preview_scene_and_fallbacks(
            state=state,
            img=img,
            perception=perception,
            screen=screen,
            evidence=evidence,
            policy=policy,
            session=session,
            can_handoff_dialogue=can_handoff_dialogue,
            observed_free_control=observed_free_control,
            result_owner=result_owner,
        )

    def _plan_stage_larc_clock_and_expedition(
        self,
        *,
        path: Path,
        img: np.ndarray,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        evidence: dict[str, Any],
        policy: RaceRoutePolicy,
        session: RaceRouteSession | None,
        can_handoff_dialogue: bool,
        observed_free_control: bool,
    ) -> dict[str, Any] | None:
        observed_retry = None
        if policy.scenario == "larc":
            from core.auto_trainer.transition_wait import career_comic_loading_visible
            if career_comic_loading_visible(img):
                if screen_text is not None:
                    evidence["screen_text"] = screen_text
                evidence["loading_source"] = "career_comic_visual"
                return {"action":"wait", "reason":"loading", "route_state":"loading", "evidence":evidence}
        if (policy.scenario == 'larc' and policy.clock_retry_unresolved and policy.post_entry_only
                and not policy.inspection_only and not policy.count_reserved_only
                and retry_prompt_visible(policy.visible_text)):
            return {'action':'wait','reason':'clock_retry_outcome_pending','route_state':'race_result','evidence':evidence}
        if (policy.scenario=='larc' and policy.allow_clock_retry
                and (policy.allow_debut_retry_entry or policy.allow_result_retry_entry)
                and policy.post_entry_only and not policy.inspection_only and not policy.count_reserved_only
                and session is not None and session.clock_retries_used<5
                and not retry_prompt_visible(policy.visible_text)):
            from core.auto_trainer.race_clock_retry import (
                ENTRY_ACTION_ID, result_entry_text, read_result_entry_and_candidate, result_retry_candidate, entry_point,
            )
            if policy.allow_result_retry_entry or result_entry_text(policy.visible_text) is not None:
                entry, candidate = read_result_entry_and_candidate(img, policy.visible_text, ordinary=policy.allow_result_retry_entry)
                if entry is not None:
                    if policy.clock_retry_unresolved:
                        return {'action':'wait','reason':'clock_retry_outcome_pending','route_state':'race_result','evidence':evidence}
                    if session.clock_retry_entry_pending:
                        return {'action':'wait','reason':'clock_retry_entry_transition','route_state':'race_result','evidence':evidence}
                    evidence['clock_retry_entry']=entry
                    return self._tap('open_lost_race_retry','race_result',ENTRY_ACTION_ID,entry_point(entry),evidence)
                if candidate is None:
                    # An absent first observation still gets the original
                    # candidate re-read; only successful reads are shared.
                    candidate = result_retry_candidate(img, policy.visible_text,
                        ordinary=policy.allow_result_retry_entry)
                if candidate is None and policy.allow_result_retry_entry:
                    # The strict entry reader may have no rank at all. An
                    # identifiable paired result still owns the requested
                    # retry decision; do not silently spend that choice on Next.
                    candidate = self._result_retry_page_candidate(img, policy.visible_text)
                if (candidate is not None and candidate.get("rank") == 1
                        and not policy.clock_retry_unresolved and not session.clock_retry_entry_pending):
                    evidence["clock_retry_not_needed"] = candidate
                    candidate = None
                if candidate is not None:
                    if candidate.get('retry_enabled') is False:
                        # A disabled retry is already observed, not unreadable.
                        # Keep pending input ownership; otherwise use only the
                        # existing, separately authorized result continuation.
                        if policy.clock_retry_unresolved:
                            return {'action':'wait','reason':'clock_retry_outcome_pending','route_state':'race_result','evidence':evidence}
                        if session.clock_retry_entry_pending:
                            return {'action':'wait','reason':'clock_retry_entry_transition','route_state':'race_result','evidence':evidence}
                        evidence['clock_retry_disabled'] = candidate
                    else:
                        # An enabled or unresolved retry still needs fresh
                        # observation, never a guessed tap or a silent Next.
                        evidence['clock_retry_entry_unverified'] = candidate
                        return {'action':'wait', 'reason':'clock_retry_entry_unverified',
                                'route_state':'race_result', 'evidence':evidence}
        if (policy.scenario == "larc" and (policy.clock_retry_remaining is not None or not policy.allow_clock_retry)
                and policy.post_entry_only and not policy.inspection_only and not policy.count_reserved_only
                and session is not None and retry_prompt_visible(policy.visible_text)):
            from core.auto_trainer.race_clock_retry import decline_retry, read_retry, decline_point
            observed_retry = _current_clock_retry(img, policy.visible_text,
                allow_stock_reobserve=policy.clock_retry_remaining is not None) if policy.allow_clock_retry else None
            if (not policy.allow_clock_retry or observed_retry is None
                    or observed_retry.get("free") is not True and policy.clock_retry_remaining is not None and policy.clock_retry_remaining <= 0):
                decline = decline_retry(img, policy.visible_text)
                if decline is not None:
                    evidence["clock_retry_decline"] = decline
                    evidence["clock_retry_decline_reason"] = ("disabled" if not policy.allow_clock_retry
                        else "unavailable" if observed_retry is None else "career_budget_exhausted")
                    return self._tap("cancel_clock_retry", "race_result", "safe_advance.clock_retry_cancel",
                        decline_point(decline), evidence)
        if (policy.scenario == "larc" and policy.allow_clock_retry and policy.post_entry_only
                and not policy.inspection_only and not policy.count_reserved_only
                and session is not None and retry_prompt_visible(policy.visible_text)):
            from core.auto_trainer.race_clock_retry import ACTION_ID, read_retry, retry_point
            proof = observed_retry if policy.clock_retry_remaining is not None else read_retry(img, policy.visible_text)
            if proof is None:
                return self._stop("clock_retry_unavailable", evidence=evidence)
            evidence["clock_retry"] = proof
            if session.clock_retries_used >= 5:
                return self._stop("clock_retry_limit", evidence=evidence)
            if (session.last_clock_retry_remaining is not None
                    and proof["remaining"] >= session.last_clock_retry_remaining):
                return {"action": "wait", "reason": "clock_retry_transition", "route_state": "race_result", "evidence": evidence}
            return self._tap("retry_lost_race", "race_result", ACTION_ID, retry_point(proof), evidence)
        if screen_text is not None:
            evidence["screen_text"] = screen_text
            reward_text = str(screen_text.get("normalized_text", ""))
            if (policy.scenario == "larc" and policy.post_entry_only
                    and "加载中" in reward_text and self._looks_like_loading(img)):
                return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
            if ((policy.allow_result_continue or observed_free_control) and not policy.count_reserved_only
                    and all(label in reward_text for label in ("奖杯陈列室", "关闭"))):
                close = SafeAdvancePlanner._trophy_reward_close_button(img)
                if close is not None:
                    if not policy.allow_result_continue:
                        self._bind_observed_free_control(evidence, "safe_advance.trophy_reward_close")
                    return self._tap("close_trophy_reward", "race_result", "safe_advance.trophy_reward_close", close, evidence)
            if screen_text.get("dangerous"):
                return self._stop("resource_prompt", evidence=evidence)

            if (policy.scenario == "larc"
                    and "".join(reward_text.split()) == "日期变化日期发生了变化确定"):
                if not (policy.allow_result_continue or policy.allow_confirm_race or policy._confirmed_live_ordinary_route):
                    return self._stop("date_change:confirm_not_allowed", evidence=evidence)
                # Reuse the existing exact system-modal handler and button
                # geometry. This is not authorization for a generic confirm.
                date_decision=SafeAdvancePlanner().plan(path,scenario="larc",visible_text=policy.visible_text)
                if date_decision.get("tap",{}).get("coordinate_name")=="safe_advance.date_change_confirm":
                    return {**date_decision,"route_state":"safe_modal"}
                return self._stop("date_change:control_unverified",evidence=evidence)

            if "连接中" in {
                line.strip()
                for line in str(screen_text.get("normalized_text", "")).splitlines()
            }:
                if SafeAdvancePlanner._connection_conflict_return_title_button(img) is not None:
                    return self._stop("connection_conflict", evidence=evidence)
                # A loading foreground may retain stale race-list labels/badges.
                # It authorizes waiting only, never a background tap.
                return {
                    "action": "wait",
                    "reason": "loading",
                    "route_state": "loading",
                    "evidence": evidence,
                }

        if policy.scenario == "larc":
            reward_footer = self._larc_reward_footer(img, policy.visible_text)
            if reward_footer is not None:
                # Reward sections can leave the generic perception button list
                # empty. Recognize the page before the rank-animation fallback,
                # then use only a control located in this frame's footer.
                evidence["larc_reward_footer"] = {
                    **reward_footer,
                    "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                point = reward_footer["point"]
                if point is None and (policy.allow_result_continue or observed_free_control):
                    return {"action": "wait", "reason": "race_result_controls_loading",
                            "route_state": "race_result", "evidence": evidence}
                return self._continue_observed_result(policy, observed_free_control, point, evidence)
            if session is not None and session.result_transition_pending:
                point = self._pending_rank_overlay_point(img, str((screen_text or {}).get("normalized_text", "")),
                    result_owned=(policy.post_entry_only and not policy.inspection_only
                        and not policy.count_reserved_only and not policy.wbc_only))
                if point is not None:
                    evidence["pending_rank_overlay"] = {"rank": None, "navigation_only": True}
                    return self._continue_observed_result(policy, observed_free_control, point, evidence)
            if policy.post_entry_only and self._looks_like_blue_white_transition(img):
                # Blank striped wipes can produce punctuation from OCR and a
                # spurious blue G1 badge; they are not a selectable race list.
                return {"action": "wait", "reason": "loading",
                        "route_state": "loading", "evidence": evidence}
            if (policy.post_entry_only and screen_text is not None
                    and not str(screen_text.get("normalized_text", "")).strip()
                    and self._looks_like_loading(img)):
                # Race animation flashes match the existing loading detector,
                # but generic list perception can otherwise stop first.
                return {"action": "wait", "reason": "loading",
                        "route_state": "loading", "evidence": evidence}
            from core.auto_trainer.larc_control_wait import modal_content_pending
            if modal_content_pending(img):
                return {"action": "wait", "reason": "race_modal_content_loading",
                        "route_state": "loading", "evidence": evidence}

        # Career choices can retain the race-day HUD and a "race victory"
        # title. Route the actual choice before generic race-scene controls.
        career_text = str((screen_text or {}).get("normalized_text", ""))
        if (policy.scenario == "larc" and not policy.inspection_only
                and not policy.count_reserved_only
                and any(label in career_text for label in
                        ("选项效果", "协助卡事件", "主线事件", "养成优俊少女事件"))
                and self._looks_like_event_choice_layout(img)):
            return handoff("event_choice", evidence=evidence)

        if (policy.count_reserved_only and not policy.count_result_persisted
            and self._looks_like_ranking_list_result(img)):
            # The count owner must persist the exact rank before clearing it.
            return self._stop("race_result:continue_not_allowed", route_state="race_result", evidence=evidence)
        larc_result_surface = (
            (policy.post_entry_only and not policy.inspection_only and not policy.wbc_only)
            or (session is not None and session.result_transition_pending)
        )
        if policy.scenario == "larc":
            post_race_text = str((screen_text or {}).get("normalized_text", ""))
            if (larc_result_surface
                    and not policy.count_reserved_only
                    and '恭喜' in post_race_text and '排名表' in post_race_text
                    and any(word in post_race_text for word in ('协助者积分','段位','粉丝数'))
                    and not any(word in post_race_text for word in ('继续','点击','下一步','前往演出','获得道具','下一页'))
                    and self._race_result_next_button_from_image(img) is None
                    and self._race_result_continue_button(perception, img, screen_text) is None):
                # The points/rank table enters before its continue caption.
                # Only wait in a confirmed result owner; never synthesize a
                # click or treat the underlying stat labels as training.
                evidence['larc_exchange_rank_table_pending']=True
                return {'action':'wait','reason':'larc_exchange_rank_table_loading',
                    'route_state':'race_result','evidence':evidence}
            if (larc_result_surface
                    and not policy.count_reserved_only and "点击" in post_race_text
                    and (re.search(r"[0-9][0-9,，.]*积分", post_race_text)
                         or ("排名表" in post_race_text and any(w in post_race_text for w in ("段位", "粉丝数", "协助者"))))):
                # Exchange races display a large animated rank; OCR can miss
                # the digit while the points and tap-to-continue are readable.
                hsv = cv2.cvtColor(cv2.resize(img, (720, 1280)), cv2.COLOR_BGR2HSV)
                rank = hsv[650:1030, 80:650]
                gold = (rank[:, :, 0] >= 15) & (rank[:, :, 0] <= 38) & (rank[:, :, 1] > 100) & (rank[:, :, 2] > 180)
                if img.shape[0] > img.shape[1] and gold.mean() > .08:
                    evidence["larc_exchange_rank_overlay"] = True
                    return self._continue_observed_result(policy, observed_free_control,
                        (round(img.shape[1]*.5), round(img.shape[0]*.88)), evidence)
        if (policy.scenario=='larc' and larc_result_surface
                and not policy.inspection_only and not policy.count_reserved_only and not policy.wbc_only):
            overlay=self._larc_rank_points_overlay(img,career_text)
            if overlay is not None:
                if overlay.get('compact_award'):
                    if policy.clock_retry_unresolved or (session is not None and session.clock_retry_entry_pending):
                        return {'action':'wait','reason':'clock_retry_outcome_pending',
                                'route_state':'race_result','evidence':evidence}
                    overlay['source_image_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                evidence['larc_rank_points_overlay']=overlay
                return self._continue_observed_result(policy, observed_free_control,
                    (round(img.shape[1]*.5),round(img.shape[0]*.83)), evidence)
            compact=''.join(career_text.split())
            rank_points_text=(re.search(r'第[1-9][0-9]*名|(?<![0-9A-Za-z.+-])[1-9][0-9]*第名|(?<![0-9A-Za-z.+-])[1-9][0-9]*名第',compact)
                and re.search(r'[0-9][0-9,，.]*积分',compact))
            visual_candidate=self._larc_rank_points_overlay(img,'',visual_only=True)
            if ((rank_points_text or visual_candidate is not None)
                    and self._portrait_race_result_click_point(perception,img) is None):
                # A rejected foreground award must not fall into a broader
                # stat-table/body detector and become a guessed result tap.
                evidence['larc_rank_points_overlay_unverified']=True
                return {'action':'wait','reason':'race_result_overlay_unverified',
                        'route_state':'race_result','evidence':evidence}
        if ((policy.count_reserved_only and session is not None and session.count_phase == "watching"
             and session.scheduled_entry_consumed is True)
            or (policy.scenario == "larc" and not policy.count_reserved_only)
            or observed_free_control):
            controls = self._count_portrait_watch_controls(img, policy.visible_text)
            if controls is not None:
                evidence["count_portrait_watch_controls"] = controls
                if controls["skip_point"] is not None and (policy.allow_skip_race_scene or observed_free_control):
                    return self._skip_observed_scene(policy, controls["skip_point"], evidence)
                return {"action": "wait", "reason": "race_scene_running", "route_state": "race_scene", "evidence": evidence}
        if policy.inspection_only:
            error = self._inspection_context_error(policy, session)
            if error is not None:
                return self._stop(error, evidence=evidence)
            assert session is not None
            safe = SafeAdvancePlanner(action_table=self.action_table)
            if self._looks_like_training_home_ready_for_race_entry(perception, img, safe):
                if session.inspection_phase == "returned":
                    return self._stop("race_inspection_complete", route_state="ready_main_menu", evidence=evidence)
                if session.inspection_phase == "new":
                    height, width = img.shape[:2]
                    return self._tap("enter_race_from_main_menu", "ready_main_menu", "main_menu.race",
                                     (int(width * 0.839), int(height * 0.867)), evidence)
            recommendation = safe._race_recommendation_top_confirm_button(evidence, perception, img)
            if session.inspection_phase == "opened" and recommendation is not None:
                return self._tap("confirm_race_recommendation_top", "fan_goal_recommendation",
                                 "safe_advance.race_recommendation_top_confirm", recommendation, evidence)
            selected = self._selected_race_card_evidence(img)
            if (session.inspection_phase in {"opened", "confirmed"}
                and self._looks_like_race_list_postcondition(img,policy.visible_text)):
                evidence["selected_race"] = selected
                evidence["inspection"] = {"eligibility_status": "not_assessed", "selected_race": selected,
                    "grade_status": "read" if isinstance(selected,dict) and selected.get("grade") else "unknown"}
                height, width = img.shape[:2]
                return self._tap("leave_inspected_race_list", "race_list", "race_list.back",
                                 (int(width * 0.105), int(height * 0.963)), evidence)
            return self._stop("race_inspection:screen_unverified", evidence=evidence)

        if "赛事推荐功能" in str((screen_text or {}).get("normalized_text", "")):
            recommendation = SafeAdvancePlanner._race_recommendation_top_confirm_button(
                evidence, perception, img,
            )
            if recommendation is None:
                return self._stop("fan_goal_recommendation:confirm_unverified",
                                  route_state="fan_goal_recommendation", evidence=evidence)
            if not (policy.allow_enter_race and policy.allow_fan_goal_recommended_race):
                return self._stop("fan_goal_recommendation:confirm_not_allowed",
                                  route_state="fan_goal_recommendation", evidence=evidence)
            return self._tap("confirm_race_recommendation_top", "fan_goal_recommendation",
                             "safe_advance.race_recommendation_top_confirm", recommendation, evidence)

        if policy.scenario == "larc":
            post_race_text = str((screen_text or {}).get("normalized_text", ""))
            if (can_handoff_dialogue and "主线事件" in post_race_text and "赛后" in post_race_text
                    and SafeAdvancePlanner._looks_like_training_result_dialogue(img)):
                return handoff("post_race_dialogue", evidence=evidence)
            if policy.allow_skip_race_scene and not policy.count_reserved_only:
                transition = larc_race_transition_kind(img, evidence)
                if transition is not None:
                    evidence["larc_race_transition"] = {"kind": transition}
                    return {"action": "wait", "reason": "race_scene_running",
                            "route_state": "race_scene", "evidence": evidence}
            if str((screen_text or {}).get("normalized_text", "")).startswith("获得协助者积分"):
                notification = plan_larc_explanation(img, evidence)
                if notification is not None:
                    observed_reward = (observed_free_control and
                        evidence.get("larc_explanation", {}).get("kind") == "supporter_points_reward")
                    return self._verified_result_notice(policy, observed_reward, notification, evidence)
            from core.auto_trainer.larc_pages import (
                larc_adaptation_warning_button, larc_exchange_schedule_continue_button,
            )
            exchange_continue = larc_exchange_schedule_continue_button(img, evidence)
            if exchange_continue is not None:
                evidence["larc_exchange_schedule"] = {"verified": True}
                return self._continue_observed_result(policy, observed_free_control, exchange_continue, evidence)
            adaptation_warning = larc_adaptation_warning_button(img, evidence)
            if adaptation_warning is not None:
                evidence["larc_adaptation_warning"] = {"verified": True}
                if policy.allow_leave_race_detail and not policy.allow_confirm_race:
                    hsv = cv2.cvtColor(cv2.resize(img, (720, 1280)), cv2.COLOR_BGR2HSV)
                    cancel = hsv[810:860, 65:335]
                    if np.mean((cancel[:, :, 1] < 60) & (cancel[:, :, 2] > 175)) > 0.70:
                        return self._tap("cancel_unverified_race_confirm", "race_detail_confirm",
                                         "race_detail.back", (round(img.shape[1] * 202 / 720), adaptation_warning[1]), evidence)
                exchange = self._larc_exchange_owner(session, evidence, policy=policy)
                exchange_allowed = exchange is not None and (
                    policy.allow_confirm_race or policy.post_entry_only and policy.allow_locked_race_entry)
                if not (exchange_allowed or policy.allow_larc_unupgraded_entry and (policy.allow_confirm_race
                        or policy.post_entry_only and policy.allow_locked_race_entry)):
                    return self._stop("larc_adaptation_warning:confirmation_not_allowed", evidence=evidence)
                decision = self._tap("confirm_larc_unupgraded_entry", "race_entry_confirm",
                                     "race_confirm.primary", adaptation_warning, evidence)
                if exchange_allowed:
                    decision["larc_exchange_verification"] = exchange
                    evidence["larc_exchange_adaptation_advice"] = {
                        "incomplete": True, "reason": "普通交流赛继续，未克服难关仅作策略提示。"}
                return decision
        return None

    def _plan_stage_preview_home_and_detail_confirm(
        self,
        *,
        state: _RacePlanState,
        path: Path,
        img: np.ndarray,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        screen: str,
        evidence: dict[str, Any],
        current_text: str,
        policy: RaceRoutePolicy,
        session: RaceRouteSession | None,
        observed_free_control: bool,
        result_owner: bool,
    ) -> dict[str, Any] | None:
        early_preview_actions = self._race_preview_action_points(img)
        preview_text = "".join(str((screen_text or {}).get("normalized_text", "")).split())
        if (policy.scenario == "larc" and policy.post_entry_only and early_preview_actions is None
                and all(label in preview_text for label in ("基础能力", "速度", "耐力", "力量", "智力"))):
            # After the confirmed entry, stat panels fade in before the two
            # preview controls. Wait for those controls; do not classify again.
            return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
        if (policy.scenario == "larc" and policy.desired_run_style is not None
                and not policy.count_reserved_only and not policy.wbc_only
                and not policy.inspection_only and not policy.allow_leave_race_detail
                and (policy.allow_quick_result or policy.allow_skip_race_scene)):
            from core.auto_trainer.larc_race_tactics import race_tactic_control
            tactic = race_tactic_control(img, preview_text, policy.desired_run_style,
                                        preview=early_preview_actions is not None
                                        and ("前往赛事" in preview_text or "查看结果" in preview_text))
            if tactic is not None:
                evidence["race_tactic"] = tactic
                if tactic["kind"] == "wait":
                    return {"action": "wait", "reason": tactic["reason"], "route_state": "loading", "evidence": evidence}
                if tactic["kind"] != "ready":
                    point = tuple(round(v * scale) for v, scale in zip(
                        tactic["point"], (img.shape[1] / 720, img.shape[0] / 1280)))
                    return self._tap("race_tactic_" + tactic["kind"],
                        "race_preview" if tactic["kind"] == "open" else "modal",
                        "race_tactic." + tactic["kind"], point, evidence)
        if (policy.scenario == "larc" and not policy.count_reserved_only and not policy.wbc_only
                and not policy.allow_leave_race_detail and early_preview_actions is not None
                and ("前往赛事" in preview_text or "查看结果" in preview_text)):
            # Entry is over: only the two preview buttons matter, not candidate OCR.
            if policy.allow_quick_result and not early_preview_actions["view_result_locked"]:
                return self._configured_decision("race_detail_view_result", img, evidence) or self._tap(
                    "view_race_result", "race_preview", "race_detail.view_result",
                    early_preview_actions["view_result"], evidence)
            if (policy.allow_quick_result and early_preview_actions["view_result_locked"]
                    and session is not None and session.result_transition_pending):
                return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
            if early_preview_actions["view_result_locked"] and policy.allow_locked_race_entry is not True:
                return self._stop("race_preview:view_result_locked", route_state="race_detail", evidence=evidence)
            if not (policy.allow_confirm_race or policy.allow_skip_race_scene):
                return self._stop("race_preview:advance_not_allowed", evidence=evidence)
            return self._tap("go_to_race_from_detail", "race_detail", "race_detail.go_to_race",
                             early_preview_actions["go_to_race"], evidence)
        if (policy.scenario == "larc" and policy.post_entry_only
                and "赛事一览" in str((screen_text or {}).get("normalized_text", ""))):
            # The confirmed list can remain visible while the race loads. Never re-enter.
            return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
        early_quick_result_available = (
            policy.allow_quick_result is True
            and early_preview_actions is not None
            and not early_preview_actions.get("view_result_locked")
        )
        center_race_detail_action_layout = (
            self._looks_like_center_race_detail_action_layout(img)
        )
        pending_target_race = bool(
            policy.allow_confirm_race is True
            and center_race_detail_action_layout
            and (
                self._has_pending_target_race(session, evidence)
                or (session is not None and session.custom_scheduled_race_verification() is not None)
            )
        )
        if screen != "race_detail_confirm" and pending_target_race:
            button = self._bottom_center_green_button(
                perception,
                img,
                min_y_ratio=0.80,
            )
            if button is None:
                return self._stop(
                    "race_detail:no_go_to_race_button",
                    route_state="race_detail",
                    evidence=evidence,
                )
            return self._tap(
                "go_to_race_from_detail",
                "race_detail",
                "race_detail.go_to_race",
                button,
                evidence,
            )
        scenario_final_selected_list = bool(
            policy.allow_enter_race is True
            and policy.allow_grand_masters_final_race_confirm is True
            and session is not None
            and not session.scheduled_binding_active
            and session.scenario_final_race_verification() is not None
            and self._grand_masters_final_selected_list(evidence, img) is not None
        )
        scenario_final_confirmation_pending = bool(
            policy.allow_confirm_race is True
            and policy.allow_grand_masters_final_race_confirm is True
            and session is not None
            and not session.scheduled_binding_active
            and session.scenario_final_race_verification() is not None
            and self._grand_masters_final_confirmation(evidence) is not None
        )
        if (
            policy.allow_leave_race_detail is True
            and not early_quick_result_available
            and not pending_target_race
            and not scenario_final_selected_list
            and not scenario_final_confirmation_pending
            and screen != "race_detail_confirm"
            and center_race_detail_action_layout
        ):
            back = self._race_detail_back_button(img)
            if back is not None:
                return self._tap(
                    "leave_race_detail",
                    "race_detail",
                    "race_detail.back",
                    back,
                    evidence,
                )

        if SafeAdvancePlanner.is_post_career_global_home(img):
            return handoff("ready_main_menu", evidence=evidence)

        final_loss_recovery = (
            None
            if screen == "race_detail_confirm"
            else SafeAdvancePlanner._final_loss_recovery_decision(img, evidence)
        )
        if final_loss_recovery is not None:
            if final_loss_recovery.get("reason") in {"continue_scenario_score_record", "next_support_reward_page"}:
                return self._verified_result_notice(policy, observed_free_control and result_owner,
                                                    final_loss_recovery, evidence)
            return final_loss_recovery

        goal_animation = SafeAdvancePlanner._looks_like_goal_achievement_animation(
            perception, img, goal_layout_only=policy.scenario == "larc")
        goal_continue = SafeAdvancePlanner._goal_achieved_continue_button(
            perception, img, goal_layout_only=policy.scenario == "larc") is not None
        if goal_animation or goal_continue:
            if policy.scenario == "larc":
                # This is a navigation handoff after the race, not a new
                # decision based on the reward/goal description. The existing
                # visual goal layout does not require the generic catalog.
                goal_text_verified = True
                marker_verified = True
            else:
                goal_lines = str((screen_text or {}).get("normalized_text", "")).splitlines()
                goal_text_verified = (
                    len(goal_lines) >= 2
                    and is_goal_achievement_title(goal_lines[0])
                    and match_main_menu_named_goal(goal_lines[1]) is not None
                )
                marker_verified = (
                    goal_animation
                    and len(goal_lines) in {2, 3}
                    and (len(goal_lines) == 2 or goal_lines[2] == "完成!")
                ) or (
                    goal_continue
                    and len(goal_lines) == 3
                    and goal_lines[2] == "继续"
                )
            if (
                goal_animation == goal_continue
                or not goal_text_verified
                or not marker_verified
            ):
                return self._stop("goal_achievement_unverified", evidence=evidence)
            return self._stop(
                "goal_achievement_loading" if goal_animation else "goal_achievement_result",
                evidence=evidence,
            )

        early_goal_continue = self._goal_achieved_continue_button(img)
        if early_goal_continue is not None:
            observed_goal = (observed_free_control and result_owner and "继续" in current_text
                and any(is_goal_achievement_title(line.strip())
                    for line in str((screen_text or {}).get("normalized_text", "")).splitlines()))
            return self._continue_observed_result(policy, observed_goal, early_goal_continue, evidence)

        safe_planner = SafeAdvancePlanner()
        if policy.scenario == "larc":
            from core.auto_trainer.larc_pages import is_larc_race_day, larc_race_day_controls
            caption = ''.join(str(evidence.get('screen_text', {}).get('normalized_text', '')).split())
            if (all(term in caption for term in ('比赛日','参赛条件已达成','目标','体力'))
                    and not any(term in caption for term in ('参赛确认','海外适应性升级确认','查看结果'))
                    and not self._looks_like_race_list_postcondition(img,policy.visible_text)
                    and not larc_race_day_controls(img)):
                return {'action':'wait','reason':'larc_race_day_controls_loading',
                    'route_state':'loading','evidence':evidence}
            if is_larc_race_day(img, evidence):
                return self._race_day_main_menu_decision(img, policy, evidence)
        if (
            self._looks_like_race_day_main_menu_action_layout(img)
            or safe_planner._looks_like_race_day_main_menu(img)
        ):
            return self._race_day_main_menu_decision(
                img,
                policy,
                evidence,
                scenario_final=safe_planner._looks_like_grand_masters_final_race_day_main_menu(img),
            )

        if (screen in {"portrait_ui", "race_result_ready"}
                and not self._looks_like_race_modal(img)
                and not self._looks_like_race_playback_modal(img)):
            safe_goal_decision = SafeAdvancePlanner().plan(
                path,
                scenario=policy.scenario,
                visible_text=policy.visible_text,
            )
            safe_goal_tap = safe_goal_decision.get("tap")
            if (
                safe_goal_decision.get("reason") == "continue_goal_achievement_result"
                and isinstance(safe_goal_tap, dict)
                and isinstance(safe_goal_tap.get("x"), int)
                and isinstance(safe_goal_tap.get("y"), int)
            ):
                return self._stop("goal_achievement_result" if policy.scenario == "larc"
                                  else "goal_achievement_unverified", evidence=evidence)
            if (
                safe_goal_decision.get("reason") in {
                    "continue_fullscreen_result",
                    "continue_swbc_skill_shortage_result",
                    "continue_swbc_ranked_reward",
                    "continue_swbc_growth_summary",
                }
                and isinstance(safe_goal_tap, dict)
                and isinstance(safe_goal_tap.get("x"), int)
                and isinstance(safe_goal_tap.get("y"), int)
            ):
                observed_result = observed_free_control and (
                    safe_goal_decision["reason"] != "continue_fullscreen_result"
                    or result_owner and ("比赛结果" in current_text or "排名表" in current_text))
                return self._continue_observed_result(policy, observed_result,
                    (safe_goal_tap["x"], safe_goal_tap["y"]), evidence)

        if (screen == "race_result_ready" and not self._looks_like_race_modal(img)
                and not self._looks_like_race_playback_modal(img)):
            if not policy.allow_result_continue:
                if result_owner or self._looks_like_ranking_list_result(img):
                    button = self._race_result_continue_button(perception, img, screen_text)
                else:
                    button = self._race_result_next_button_from_image(img)
                return self._continue_observed_result(policy, observed_free_control, button, evidence)
            button = self._race_result_continue_button(perception, img, screen_text)
            if button is not None:
                return self._tap(
                    "race_result_continue",
                    "race_result",
                    "race_result.continue",
                    button,
                    evidence,
                )
            return self._configured_decision("race_result_continue", img, evidence) or self._tap(
                "race_result_continue",
                "race_result",
                "race_result.continue",
                self._screen_continue_point(img),
                evidence,
            )

        training_check = TrainingMenuPrechecker(scenario=policy.scenario).check(path)
        evidence["training_menu_precheck"] = {
            "accepted": training_check.accepted,
            "reason": training_check.reason,
            "selected_tab": training_check.selected_tab,
            "failures": list(training_check.failures),
        }
        if policy.scenario != "larc" and self._looks_like_wbc_race_hub(img):
            if not policy.allow_enter_race:
                return self._stop("wbc_hub:enter_not_allowed", evidence=evidence)
            height, width = img.shape[:2]
            decision = self._configured_decision("wbc_race_hub", img, evidence) or self._tap(
                "enter_race_from_wbc_hub",
                "wbc_race_hub",
                "wbc_hub.race",
                (int(width * 0.71), int(height * 0.835)),
                evidence,
            )
            decision["wbc_hub_verification"] = {
                "source": "wbc_race_hub",
                "scenario": "grand_masters",
                "action_id": "wbc_hub.race",
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "single_use": True,
            }
            return decision
        safe_scenario_decision = safe_planner.plan(
            path,
            visible_text=policy.visible_text,
            scenario=policy.scenario,
        )
        safe_scenario_reason = str(safe_scenario_decision.get("reason") or "")
        if policy.scenario == "larc" and safe_scenario_reason in {"ready_main_menu", "ready_training_menu"}:
            if (safe_scenario_reason == "ready_main_menu" and policy.allow_enter_race_from_main_menu
                and self._looks_like_training_home_ready_for_race_entry(perception, img, safe_planner)):
                height, width = img.shape[:2]
                return self._tap("enter_race_from_main_menu", "ready_main_menu", "main_menu.race",
                                 (int(width * 0.839), int(height * 0.867)), evidence)
            return self._stop(safe_scenario_reason, evidence=evidence)
        if safe_scenario_reason in {
            "close_knowledge_table",
            "confirm_wisdom_learning_prompt",
            "learn_wisdom_modal",
        }:
            evidence["scenario_mechanic"] = {
                "source": "safe_advance_specific_detector",
                "reason": safe_scenario_reason,
            }
            return self._stop(
                "scenario_mechanic:handoff_required",
                route_state="scenario_mechanic",
                evidence=evidence,
            )
        if (
            safe_scenario_reason in {"go_to_race_modal", "fan_goal_warning_handoff"}
            and self._has_fan_goal_shortfall_warning_text(evidence)
        ):
            safe_tap = safe_scenario_decision.get("tap")
            if safe_scenario_reason == "fan_goal_warning_handoff":
                point = safe_planner._go_to_race_modal_button(perception, img)
                safe_tap = {"x": point[0], "y": point[1]} if point is not None else None
            if not (
                isinstance(safe_tap, dict)
                and isinstance(safe_tap.get("x"), int)
                and isinstance(safe_tap.get("y"), int)
            ):
                return self._stop(
                    "fan_goal_modal:entry_unverified",
                    route_state="fan_goal_modal",
                    evidence=evidence,
                )
            if not (
                policy.allow_enter_race
                and policy.allow_fan_goal_recommended_race
            ):
                return self._stop(
                    "fan_goal_modal:entry_not_allowed",
                    route_state="fan_goal_modal",
                    evidence=evidence,
                )
            return self._tap(
                "go_to_race_modal",
                "fan_goal_modal",
                "safe_advance.go_to_race",
                (safe_tap["x"], safe_tap["y"]),
                evidence,
            )
        grand_masters_final_race_day = (
            safe_planner._looks_like_grand_masters_final_race_day_main_menu(img)
        )
        if (
            grand_masters_final_race_day
            or self._looks_like_race_day_main_menu_action_layout(img)
        ):
            return self._race_day_main_menu_decision(
                img,
                policy,
                evidence,
                scenario_final=grand_masters_final_race_day,
            )
        if (
            safe_planner._looks_like_race_day_main_menu(img)
            and not training_check.accepted
        ):
            return self._race_day_main_menu_decision(
                img,
                policy,
                evidence,
                scenario_final=grand_masters_final_race_day,
            )
        if policy.scenario != "larc" and safe_planner._looks_like_gur_race_entry(img):
            if not policy.allow_enter_race:
                return self._stop("gur_race:enter_not_allowed", evidence=evidence)
            height, width = img.shape[:2]
            decision = self._tap(
                "enter_gur_race",
                "gur_race_entry",
                "gur_race.primary_entry",
                (int(width * 0.70), int(height * 0.855)),
                evidence,
            )
            decision["gur_race_verification"] = {
                "source": "gur_race_entry.structured_frame",
                "scenario": "grand_masters",
                "action_id": "gur_race.primary_entry",
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "single_use": True,
            }
            return decision
        if training_check.accepted:
            return handoff("ready_training_menu", evidence=evidence)

        race_detail_confirm_text = (
            isinstance(screen_text, dict)
            and isinstance(screen_text.get("primary_hint"), dict)
            and screen_text["primary_hint"].get("screen") == "race_detail_confirm"
        )
        if screen == "race_detail_confirm" or race_detail_confirm_text:
            state = "race_detail_confirm"
            if policy.allow_leave_race_detail and not policy.allow_confirm_race:
                return self._tap(
                    "cancel_unverified_race_confirm",
                    state,
                    "race_detail.back",
                    self._race_confirm_cancel_point(evidence, img),
                    evidence,
                )
            if not policy.allow_confirm_race:
                return self._stop(
                    "race_confirm:confirm_not_allowed",
                    route_state=state,
                    evidence=evidence,
                )
            has_pending_target = self._has_pending_target_race(session, evidence)
            exchange = self._larc_exchange_owner(session, evidence, policy=policy, current_detail=True)
            if exchange is not None:
                button = self._primary_confirm_button(perception, img) or self._bottom_center_green_region(img, min_y_ratio=0.66)
                if button is None:
                    return self._stop("race_confirm:no_confirm_button", route_state=state, evidence=evidence)
                return self._larc_exchange_confirm_tap(state, button, evidence, exchange,
                    consume_target=has_pending_target)
            fan_goal_recommendation = self._pending_fan_goal_recommendation(
                session,
                evidence,
            )
            fan_goal_shortfall = self._pending_fan_goal_shortfall_selected(
                session,
                evidence,
            )
            custom_scheduled = (
                session.custom_scheduled_race_verification()
                if session is not None
                else None
            )
            scenario_final_page = self._grand_masters_final_confirmation(
                evidence
            )
            scenario_final_capability = self._pending_scenario_final_race(
                session,
                evidence,
            )
            wbc_confirmation = self._wbc_race_confirmation(evidence, img)
            wbc_capability = self._pending_wbc_hub_race(session, evidence)
            gur_capability = self._pending_gur_race(session, evidence)
            capability_count = sum(
                (
                    int(has_pending_target),
                    int(fan_goal_recommendation is not None),
                    int(fan_goal_shortfall is not None),
                    int(custom_scheduled is not None),
                    int(scenario_final_capability is not None),
                    int(wbc_capability is not None),
                    int(gur_capability is not None),
                )
            )
            if capability_count > 1:
                return self._stop(
                    "race_confirm:ambiguous_session_capability",
                    route_state=state,
                    evidence=evidence,
                )
            scenario_final_allowed = (
                policy.allow_grand_masters_final_race_confirm is True
                and scenario_final_page is not None
                and scenario_final_capability is not None
            )
            wbc_allowed = wbc_confirmation is not None and wbc_capability is not None
            shortfall_confirmation = self._fan_goal_shortfall_confirmation(
                evidence,
                fan_goal_shortfall,
                img,
            )
            if fan_goal_shortfall is not None and shortfall_confirmation is None:
                return self._stop(
                    "race_confirm:fan_goal_shortfall_identity_mismatch",
                    route_state=state,
                    evidence=evidence,
                )
            if (
                not has_pending_target
                and fan_goal_recommendation is None
                and fan_goal_shortfall is None
                and custom_scheduled is None
                and not scenario_final_allowed
                and not wbc_allowed
                and gur_capability is None
            ):
                if policy.allow_leave_race_detail is True:
                    return self._tap(
                        "cancel_unverified_race_confirm",
                        state,
                        "race_detail.back",
                        self._race_confirm_cancel_point(evidence, img),
                        evidence,
                    )
                if (
                    scenario_final_page is not None
                    and policy.allow_cancel_unverified_grand_masters_final is True
                ):
                    return self._tap(
                        "cancel_unverified_grand_masters_final",
                        state,
                        "race_confirm.grand_masters_final_cancel",
                        self._race_confirm_cancel_point(evidence, img),
                        evidence,
                    )
                return self._stop(
                    "race_confirm:target_verification_required",
                    route_state=state,
                    evidence=evidence,
                )
            button = self._primary_confirm_button(perception, img) or self._bottom_center_green_region(
                img,
                min_y_ratio=0.66,
            )
            if button is None:
                return self._stop("race_confirm:no_confirm_button", route_state=state, evidence=evidence)
            if scenario_final_allowed and not has_pending_target:
                evidence["scenario_final_verification"] = {
                    **scenario_final_capability,
                    "confirmation_source": scenario_final_page["source"],
                }
                decision = self._tap(
                    "confirm_grand_masters_final_race",
                    state,
                    "race_confirm.grand_masters_final",
                    button,
                    evidence,
                )
                decision["consume_scenario_final_verification"] = True
                return decision
            if wbc_allowed:
                evidence["wbc_hub_verification"] = {
                    **wbc_capability,
                    "confirmation_source": wbc_confirmation["source"],
                }
                decision = self._tap(
                    "confirm_wbc_hub_race",
                    state,
                    "race_confirm.wbc_hub",
                    button,
                    evidence,
                )
                decision["consume_wbc_hub_verification"] = True
                return decision
            if gur_capability is not None:
                evidence["gur_race_verification"] = {
                    **gur_capability,
                    "confirmation_source": "race_detail_confirm",
                }
                decision = self._tap(
                    "confirm_gur_race",
                    state,
                    "race_confirm.gur",
                    button,
                    evidence,
                )
                decision["consume_gur_race_verification"] = True
                return decision
            if fan_goal_recommendation is not None:
                evidence["fan_goal_recommendation_verification"] = {
                    **fan_goal_recommendation,
                    "confirmation_source": "race_detail_confirm",
                }
                decision = self._tap(
                    "confirm_fan_goal_recommended_race",
                    state,
                    "race_confirm.fan_goal_recommended",
                    button,
                    evidence,
                )
                decision["consume_fan_goal_recommendation_verification"] = True
                return decision
            if fan_goal_shortfall is not None:
                evidence["fan_goal_shortfall_selected_verification"] = {
                    **fan_goal_shortfall,
                    "confirmation_source": shortfall_confirmation["source"],
                }
                decision = self._tap(
                    "confirm_fan_goal_shortfall_selected_race",
                    state,
                    "race_confirm.fan_goal_shortfall_selected",
                    button,
                    evidence,
                )
                decision["consume_fan_goal_shortfall_selected_verification"] = True
                return decision
            if custom_scheduled is not None:
                from core.auto_trainer.larc_custom_race import match_custom_race_name
                confirm_text = str((screen_text or {}).get("normalized_text", ""))
                if not match_custom_race_name(confirm_text, custom_scheduled):
                    if policy.allow_leave_race_detail is True:
                        return self._tap(
                            "cancel_unverified_race_confirm",
                            state,
                            "race_detail.back",
                            self._race_confirm_cancel_point(evidence, img),
                            evidence,
                        )
                    return self._stop(
                        "race_confirm:custom_race_identity_mismatch",
                        route_state=state,
                        evidence=evidence,
                    )
                evidence["custom_scheduled_race_verification"] = {
                    **custom_scheduled,
                    "confirmation_source": "race_detail_confirm",
                }
                decision = self._tap(
                    "confirm_custom_scheduled_race",
                    state,
                    "race_confirm.primary",
                    button,
                    evidence,
                )
                decision["consume_custom_scheduled_race_verification"] = True
                return decision
            return self._target_race_confirm_tap(state, button, evidence)
        state.early_preview_actions = early_preview_actions
        state.safe_planner = safe_planner
        state.training_check = training_check
        return None

    def _plan_stage_landscape_and_portrait_results(
        self,
        *,
        state: _RacePlanState,
        path: Path,
        img: np.ndarray,
        perception: dict[str, Any],
        landscape_kind: str | None,
        screen_text: dict[str, Any] | None,
        screen: str,
        evidence: dict[str, Any],
        policy: RaceRoutePolicy,
        session: RaceRouteSession | None,
        can_handoff_dialogue: bool,
        observed_free_control: bool,
        result_owner: bool,
    ) -> dict[str, Any] | None:
        safe_planner = state.safe_planner
        training_check = state.training_check
        if landscape_kind == "landscape_loading":
            text = str((screen_text or {}).get("normalized_text", ""))
            if (policy.scenario == "larc" and policy.post_entry_only
                    and all(label in text for label in ("获得歌曲", "已获得乐曲", "关闭"))
                    and img.shape[:2] == (720, 1280)):
                return self._tap(
                    "close_larc_song_reward", "race_reward", "race_reward.close_song",
                    (640, 555), evidence,
                )
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "loading",
                "evidence": evidence,
            }
        if landscape_kind == "landscape_race_menu":
            close_button = safe_planner._landscape_race_menu_close_button(img)
            if close_button is None:
                return self._stop("race_menu:no_close_button", route_state="race_menu", evidence=evidence)
            return self._tap(
                "close_landscape_race_menu",
                "race_menu",
                "race_menu.close",
                close_button,
                evidence,
            )
        if landscape_kind == "landscape_race_list":
            button = self._bottom_center_green_region(img, min_y_ratio=0.82)
            if button is None:
                return self._stop("race_list:no_primary_entry", evidence=evidence)
            if not policy.allow_enter_race:
                return self._stop("race_list:enter_not_allowed", evidence=evidence)
            return self._tap(
                "enter_race_from_list",
                "race_list",
                "race_list.primary_entry",
                button,
                evidence,
            )
        if landscape_kind in {"landscape_race_title", "landscape_race_pre_start"}:
            start_button = self._landscape_bottom_center_green_action_button(img)
            if start_button is not None:
                if landscape_kind == "landscape_race_pre_start":
                    height, width = img.shape[:2]
                    start_button = (width // 2, max(start_button[1], int(height * 0.90)))
                if not (policy.allow_enter_race or policy.allow_confirm_race):
                    return self._stop("race_scene:start_not_allowed", evidence=evidence)
                return self._tap(
                    "race_scene_start_or_continue",
                    "race_scene",
                    "race_scene.start_or_continue",
                    start_button,
                    evidence,
                )

        if landscape_kind == "landscape_race_running":
            if not (policy.allow_skip_race_scene or observed_free_control):
                return self._stop("race_scene:skip_not_allowed", evidence=evidence)
            skip_point = self._right_bottom_race_skip_to_end_point(img)
            if skip_point is not None:
                return self._skip_observed_scene(policy, skip_point, evidence)
            return {
                "action": "wait",
                "reason": "race_scene_running",
                "route_state": "race_scene",
                "evidence": evidence,
            }

        if landscape_kind is None:
            landscape_skip_state = self._landscape_race_skip_toggle_state(img)
        else:
            landscape_skip_state = None
        if landscape_skip_state in {"on", "off"}:
            if not (policy.allow_skip_race_scene or observed_free_control):
                return self._stop("race_scene:skip_not_allowed", evidence=evidence)
            skip_point = self._right_bottom_race_skip_to_end_point(img)
            if skip_point is not None:
                return self._skip_observed_scene(policy, skip_point, evidence)
            return {
                "action": "wait",
                "reason": "landscape_race_skip_enabled",
                "route_state": "race_scene",
                "evidence": evidence,
            }

        if policy.scenario != "larc" and "race_hub" in set(training_check.failures):
            if not policy.allow_enter_race:
                return self._stop("wbc_hub:enter_not_allowed", evidence=evidence)
            height, width = img.shape[:2]
            decision = self._configured_decision("wbc_race_hub", img, evidence) or self._tap(
                "enter_race_from_wbc_hub",
                "wbc_race_hub",
                "wbc_hub.race",
                (int(width * 0.71), int(height * 0.835)),
                evidence,
            )
            decision["wbc_hub_verification"] = {
                "source": "wbc_race_hub",
                "scenario": "grand_masters",
                "action_id": "wbc_hub.race",
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "single_use": True,
            }
            return decision
        if safe_planner._connection_conflict_return_title_button(img) is not None:
            return self._stop("connection_conflict", evidence=evidence)
        if safe_planner._looks_like_race_day_main_menu(img):
            if not policy.allow_enter_race:
                return self._stop("race_day:enter_not_allowed", evidence=evidence)
            return self._race_day_main_menu_decision(
                img,
                policy,
                evidence,
            )

        is_visual_race_list = safe_planner._looks_like_race_list(img)
        early_selected_race_card = self._selected_race_card_evidence(img)
        early_race_list_title = self._screen_text_has_race_list_title(evidence)
        early_text_verified_race_list = bool(
            early_race_list_title
            and early_selected_race_card is not None
        )
        norm_check_text = "".join(str((screen_text or {}).get("normalized_text", "")).split())
        if (
            not early_race_list_title
            and self._screen_text_has_fan_count_result(evidence)
            and (self._looks_like_rank_points_result(img) or "获得道具" in norm_check_text or "排名表" in norm_check_text)
            and not any(m in norm_check_text for m in ("赛事一览", "赛事详情", "前往赛事", "查看结果", "预测"))
        ):
            is_visual_race_list = False

        reward_continue = self._race_reward_continue_button(perception, img)
        if reward_continue is not None:
            return self._continue_observed_result(
                policy, observed_free_control and result_owner, reward_continue, evidence)

        if self._looks_like_race_reward_next_page(perception, img):
            button = self._bottom_center_green_button(perception, img, min_y_ratio=0.80)
            if button is None and (policy.allow_result_continue or observed_free_control):
                return self._stop("race_result:no_continue_button", evidence=evidence)
            return self._continue_observed_result(policy, observed_free_control, button, evidence)

        image_result_next = self._race_result_next_button_from_image(img)
        if image_result_next is not None:
            return self._continue_observed_result(policy, observed_free_control, image_result_next, evidence)

        # A fan/reward list has a real next button; it is not the rank-only
        # animation even when its upper gold badge also matches that layout.
        portrait_result_click = self._portrait_race_result_click_point(perception, img)
        if (
            not is_visual_race_list
            and not early_race_list_title
            and self._screen_text_has_fan_count_result(evidence)
            and (self._looks_like_rank_points_result(img)
                 or policy.scenario == "larc" and policy.post_entry_only and portrait_result_click is not None)
        ):
            point = self._race_result_continue_button(perception, img, screen_text)
            if not policy.allow_result_continue:
                return self._continue_observed_result(policy, observed_free_control, point, evidence)
            if point is None and policy.scenario == "larc" and policy.post_entry_only:
                # Item headers can change the white/green panel ratios, and
                # the mascot may hide the footer label. Require its actual
                # green control instead of the animation's tap-anywhere point.
                return {"action":"wait", "reason":"race_result_controls_loading",
                        "route_state":"race_result", "evidence":evidence}
            point = point or self._rank_points_next_button(perception, img) or self._screen_continue_point(img)
            return self._tap(
                "race_result_continue", "race_result", "race_result.continue", point, evidence,
            )

        normalized_result_text = "".join(
            str((screen_text or {}).get("normalized_text", "")).split()
        )
        if (
            not is_visual_race_list
            and not early_race_list_title
            and not early_text_verified_race_list
            and not self._has_fan_goal_shortfall_warning_text(evidence)
            and not any(
                marker in normalized_result_text
                for marker in ("赛事详情", "前往赛事", "查看结果", "目标粉丝数")
            )
        ):
            summary_point = safe_planner._race_result_summary_continue_button(
                img, screen_text
            )
            if summary_point is not None:
                return self._continue_observed_result(
                    policy,
                    observed_free_control and result_owner,
                    summary_point,
                    evidence,
                )

        if (portrait_result_click is not None
                and "下一页" in str((screen_text or {}).get("normalized_text", ""))):
            # A finished ranking list can share the gold animation artwork.
            # Its visible Next control takes precedence over tapping the overlay.
            portrait_result_click = self._race_result_continue_button(perception, img, screen_text) or portrait_result_click
        if portrait_result_click is None:
            brown_overlay = self._compact_brown_result_overlay(perception, img,
                result_owned=(policy.scenario == "larc" and policy.post_entry_only
                    and session is not None and session.result_transition_pending
                    and not policy.inspection_only and not policy.count_reserved_only and not policy.wbc_only))
            if brown_overlay is not None:
                evidence["compact_brown_overlay_evidence"] = {
                    **brown_overlay, "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                portrait_result_click = (int(img.shape[1] * 0.50), int(img.shape[0] * 0.83))
        if (
            portrait_result_click is None
            and "".join(str((screen_text or {}).get("normalized_text", "")).split()) in {"点击", "名第点击"}
            and self._compact_gold_result_layout(perception, img)
        ):
            local_rank = self._read_compact_result_rank(path, img)
            if local_rank is not None:
                evidence["compact_rank_evidence"] = local_rank
                portrait_result_click = (int(img.shape[1] * 0.50), int(img.shape[0] * 0.83))
        if portrait_result_click is not None:
            return self._continue_observed_result(
                policy, observed_free_control and result_owner, portrait_result_click, evidence)

        if (
            not early_race_list_title
            and self._looks_like_fan_rank_result(img)
        ):
            point = self._rank_points_next_button(perception, img)
            if point is None and policy.allow_result_continue:
                point = self._screen_continue_point(img)
            return self._continue_observed_result(policy, observed_free_control, point, evidence)

        # Story dialogue may retain the race-list header behind its overlay. Resolve the
        # foreground dialogue before text fusion can route the stale header into a
        # non-G1 leave action.
        if (can_handoff_dialogue and self._looks_like_training_story_dialogue(img)
            and not self._looks_like_training_home_ready_for_race_entry(perception, img, safe_planner)):
            return handoff("post_race_dialogue", evidence=evidence)

        participant_text = str(evidence.get("screen_text", {}).get("normalized_text", ""))
        if (policy.scenario == "larc" and screen == "race_participant_list"
                and participant_text.count("人气第") >= 3
                and "参赛!" in participant_text and "赛事一览" not in participant_text):
            button = self._bottom_center_green_region(img, min_y_ratio=0.82)
            if button is not None:
                evidence["larc_race_participants"] = {"verified": True}
                # This control starts the race. A free skip cannot authorize it.
                if not policy.allow_skip_race_scene:
                    return self._stop("race_scene:skip_not_allowed", evidence=evidence)
                return self._tap("race_scene_start_or_continue", "race_scene",
                    "race_scene.start_or_continue", button, evidence)
        state.is_visual_race_list = is_visual_race_list
        state.early_selected_race_card = early_selected_race_card
        state.early_race_list_title = early_race_list_title
        state.early_text_verified_race_list = early_text_verified_race_list
        return None

    def _plan_stage_race_list_and_modals(
        self,
        *,
        state: _RacePlanState,
        path: Path,
        img: np.ndarray,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        screen: str,
        evidence: dict[str, Any],
        current_text: str,
        policy: RaceRoutePolicy,
        session: RaceRouteSession | None,
        observed_free_control: bool,
        result_owner: bool,
    ) -> dict[str, Any] | None:
        early_preview_actions = state.early_preview_actions
        safe_planner = state.safe_planner
        is_visual_race_list = state.is_visual_race_list
        early_selected_race_card = state.early_selected_race_card
        early_race_list_title = state.early_race_list_title
        early_text_verified_race_list = state.early_text_verified_race_list
        positive_non_goal_race = self._looks_like_non_goal_graded_selected_race(img)
        positive_target_race = self._looks_like_target_race_list(img)
        selected_race_candidate = early_selected_race_card
        text_verified_race_list = early_text_verified_race_list
        looks_like_race_list = (
            is_visual_race_list or positive_target_race or early_race_list_title
        )
        selected_race = selected_race_candidate if looks_like_race_list else None
        if policy.count_reserved_only and selected_race is not None:
            if self.count_card_reader is None:
                return self._stop("count_race:selected_identity_required", evidence=evidence)
            try:
                card = self.count_card_reader(path)
                if (card.get("source_frame_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
                    or session is None or card.get("race_id") not in session.planned_race_ids
                    or card.get("grade") != selected_race.get("grade")):
                    raise ValueError("selected_card_mismatch")
            except ValueError as exc:
                return self._stop(f"count_race:{exc}", evidence=evidence)
            evidence["count_selected_card"] = card
            selected_race = {**selected_race, "race_id": card["race_id"],
                             "source_image_sha256": card["source_frame_sha256"]}
        if text_verified_race_list:
            evidence["race_list_text_verification"] = {
                "source": "screen_text.exact_title_and_selected_grade_badge",
                "title": "赛事一览",
            }
        if selected_race is not None:
            evidence["selected_race"] = selected_race
        if policy.scenario == "larc" and policy.allow_first_win_rescue and looks_like_race_list:
            from core.auto_trainer.larc_first_win import selected_first_win_candidate
            if selected_first_win_candidate(img, (screen_text or {}).get("normalized_text")):
                entry = self._bottom_center_green_button(perception, img, min_y_ratio=.78)
                if policy.allow_enter_race and entry is not None:
                    decision = self._tap("enter_selected_first_win_race", "race_list",
                        "race_list.primary_entry", entry, evidence)
                    decision["target_race_verification"] = {
                        "source": "race_list.current_first_win_requirement", "grade": "MAIDEN",
                        "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                    return decision
        if (
            policy.scenario == "larc"
            and policy.allow_larc_custom_scheduled_race
            and isinstance(policy.custom_target_race, dict)
            and looks_like_race_list
            and screen != "race_participant_list"
        ):
            from core.auto_trainer.larc_custom_race import inspect_race_list_for_custom_target
            custom_match = inspect_race_list_for_custom_target(
                img, policy.custom_target_race, self._fan_shortfall_regions_reader
            )
            evidence["custom_race_inspection"] = custom_match
            height, width = img.shape[:2]
            if custom_match.get("status") == "selected_match":
                entry = (
                    self._bottom_center_green_button(perception, img, min_y_ratio=0.78)
                    or self._bottom_center_green_region(img, min_y_ratio=0.82)
                )
                if policy.allow_enter_race and entry is not None:
                    decision = self._tap(
                        "enter_custom_scheduled_race",
                        "race_list",
                        "race_list.primary_entry",
                        entry,
                        evidence,
                    )
                    decision["custom_scheduled_race_verification"] = {
                        **policy.custom_target_race,
                        "matched_text": custom_match.get("matched_text"),
                        "source": "race_list.custom_scheduled_selected_card",
                        "action_id": "race_list.primary_entry",
                        "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "single_use": True,
                    }
                    return decision
                if policy.allow_leave_race_list:
                    return self._tap(
                        "leave_unavailable_custom_scheduled_race_list",
                        "race_list",
                        "race_list.back",
                        (int(width * 0.105), int(height * 0.963)),
                        evidence,
                    )
            elif custom_match.get("status") == "unselected_match":
                center = custom_match["card_center"]
                return self._tap(
                    "select_custom_scheduled_race_card",
                    "race_list",
                    "race_list.select_custom_card",
                    (int(center[0]), int(center[1])),
                    evidence,
                )
            elif custom_match.get("status") == "not_visible":
                viewport_sha256 = self._race_list_viewport_sha256(img)
                if (
                    session is not None
                    and len(session.race_list_scan_viewports) < 2
                    and viewport_sha256 not in session.race_list_scan_viewports
                ):
                    evidence["race_list_scan"] = {
                        "source": "race_list.viewport_pixels",
                        "viewport_sha256": viewport_sha256,
                        "scan_index": len(session.race_list_scan_viewports) + 1,
                        "max_scans": 2,
                    }
                    return {
                        "action": "swipe",
                        "reason": "scan_race_list_for_visible_g1",
                        "route_state": "race_list",
                        "gesture": {
                            "coordinate_name": "race_list.scan_for_visible_g1",
                            "x1": int(round(width * 0.50)),
                            "y1": int(round(height * 0.76)),
                            "x2": int(round(width * 0.50)),
                            "y2": int(round(height * 0.55)),
                            "duration_ms": 450,
                            "scaled": False,
                            "direction": "up",
                        },
                        "evidence": evidence,
                    }
                if policy.allow_leave_race_list:
                    return self._tap(
                        "leave_unavailable_custom_scheduled_race_list",
                        "race_list",
                        "race_list.back",
                        (int(width * 0.105), int(height * 0.963)),
                        evidence,
                    )
            else:
                return self._stop(
                    "custom_race:list_ocr_unreadable",
                    route_state="race_list",
                    evidence=evidence,
                )
        selected_grade = selected_race.get("grade") if isinstance(selected_race, dict) else None
        normalized_screen_text = "".join(
            str(evidence.get("screen_text", {}).get("normalized_text", "")).split()
        )
        fan_shortfall_list, fan_shortfall_candidate = (
            self._fan_goal_shortfall_candidate_evidence(img, selected_race)
            if looks_like_race_list
            and (
                "参赛条件剩余" in normalized_screen_text
                or "达成条件剩余" in normalized_screen_text
            )
            else (False, None)
        )
        # The ordinary fan-shortfall list has its own proof and must not spend
        # another localized OCR pass trying to classify the older 3000-fan
        # recommendation card.
        fan_goal_recommendation = (
            None
            if fan_shortfall_list
            else (
                self._fan_goal_recommended_race_evidence(evidence, img)
                if looks_like_race_list
                else None
            )
        )
        locked_target_verification = (
            session.locked_target_race_verification()
            if (
                policy.allow_locked_race_entry is True
                and session is not None
            )
            else None
        )
        if (
            policy.allow_leave_race_list is True
            and fan_shortfall_list
            and fan_shortfall_candidate is None
            and "暂无可以参加的比赛" in normalized_screen_text
            and text_verified_race_list
            and (
                session is None
                or (
                    not session.scheduled_binding_active
                    and all(
                        pending is None
                        for pending in (
                            session.pending_target_race,
                            session.pending_locked_target_race,
                            session.pending_scenario_final_race,
                            session.pending_fan_goal_recommendation,
                            session.pending_fan_goal_shortfall_selected,
                            session.pending_wbc_hub_race,
                            session.pending_gur_race,
                        )
                    )
                )
            )
        ):
            height, width = img.shape[:2]
            return self._tap(
                "leave_empty_fan_goal_shortfall_race_list",
                "race_list",
                "race_list.back",
                (int(width * 0.105), int(height * 0.963)),
                evidence,
            )
        if (
            policy.allow_leave_race_list is True
            and policy.allow_enter_race is False
            and policy.allow_confirm_race is False
            and screen == "race_participant_list"
            and text_verified_race_list
            and "赛事一览" in {
                line.strip()
                for line in str((screen_text or {}).get("normalized_text", "")).splitlines()
            }
            and selected_grade in {"G2", "G3", "OPEN"}
            and fan_goal_recommendation is None
            and not fan_shortfall_list
            and (
                session is None
                or (
                    not session.scheduled_binding_active
                    and all(pending is None for pending in (
                        session.pending_target_race,
                        session.pending_locked_target_race,
                        session.pending_scenario_final_race,
                        session.pending_fan_goal_recommendation,
                        session.pending_fan_goal_shortfall_selected,
                        session.pending_wbc_hub_race,
                        session.pending_gur_race,
                    ))
                )
            )
        ):
            height, width = img.shape[:2]
            return self._tap(
                "leave_selected_non_g1_race_list", "race_list", "race_list.back",
                (int(width * 0.105), int(height * 0.963)), evidence,
            )
        if (
            policy.allow_leave_race_list
            and not policy.allow_enter_race
            and is_visual_race_list
            and fan_goal_recommendation is not None
        ):
            height, width = img.shape[:2]
            return self._tap(
                "leave_fan_goal_recommendation_list",
                "race_list",
                "race_list.back",
                (int(width * 0.105), int(height * 0.963)),
                evidence,
            )
        if (
            policy.allow_leave_race_list
            and policy.allow_leave_unverified_g1_race_list
            and not policy.allow_enter_race
            and looks_like_race_list
            and selected_grade == "G1"
        ):
            height, width = img.shape[:2]
            return self._tap(
                "leave_unverified_g1_race_list",
                "race_list",
                "race_list.back",
                (int(width * 0.105), int(height * 0.963)),
                evidence,
            )
        if (
            policy.allow_leave_race_list
            and locked_target_verification is None
            and not positive_target_race
            and fan_goal_recommendation is None
            and not fan_shortfall_list
            and screen != "race_participant_list"
            and (is_visual_race_list or text_verified_race_list)
            and selected_grade in {"G2", "G3", "OPEN"}
        ):
            height, width = img.shape[:2]
            return self._tap(
                "leave_selected_non_g1_race_list",
                "race_list",
                "race_list.back",
                (int(width * 0.105), int(height * 0.963)),
                evidence,
            )
        if (
            screen != "race_participant_list"
            and is_visual_race_list
            and positive_non_goal_race
            and positive_target_race
        ):
            return self._stop(
                "race_list:target_non_goal_conflict",
                route_state="race_list",
                evidence=evidence,
            )
        if (
            policy.allow_fan_goal_recommended_race is True
            and fan_goal_recommendation is not None
            and selected_grade == "G1"
        ):
            entry = (
                self._bottom_center_green_button(
                    perception,
                    img,
                    min_y_ratio=0.78,
                )
                or self._bottom_center_green_region(
                    img,
                    min_y_ratio=0.82,
                )
            )
            if entry is not None:
                decision = self._tap(
                    "enter_fan_goal_recommended_race",
                    "race_list",
                    "race_list.fan_goal_recommended_entry",
                    entry,
                    evidence,
                )
                decision["fan_goal_recommendation_verification"] = {
                    **fan_goal_recommendation,
                    "source_image_sha256": hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest(),
                    "action_id": "race_list.fan_goal_recommended_entry",
                    "single_use": True,
                }
                return decision
        if (fan_shortfall_list and policy.return_fan_shortfall_to_home
                and "赛事一览" in normalized_screen_text
                and not policy.allow_fan_goal_recommended_race
                and not policy.count_reserved_only
                and (session is None or (not session.scheduled_binding_active and all(
                    pending is None for pending in (
                        session.pending_target_race, session.pending_scenario_final_race,
                        session.pending_fan_goal_recommendation, session.pending_fan_goal_shortfall_selected,
                        session.pending_wbc_hub_race, session.pending_gur_race))))):
            # No race has been submitted by opening this list. Return the
            # ordinary turn to its training/SS owner instead of treating the
            # closed supplemental-race switch as a fatal route error.
            height, width = img.shape[:2]
            return self._tap("leave_fan_goal_shortfall_race_list", "race_list", "race_list.back",
                (int(width * 0.105), int(height * 0.963)), evidence)
        if fan_shortfall_list and not (
            policy.count_reserved_only and selected_race is not None
        ):
            if not (
                policy.allow_enter_race
                and policy.allow_fan_goal_recommended_race
            ):
                return self._stop(
                    "fan_goal_shortfall:entry_not_allowed",
                    route_state="race_list",
                    evidence=evidence,
                )
            if (
                fan_shortfall_candidate is None
                or (
                    session is not None
                    and (
                        session.scheduled_binding_active
                        or any(
                            pending is not None
                            for pending in (
                                session.pending_target_race,
                                session.pending_locked_target_race,
                                session.pending_scenario_final_race,
                                session.pending_fan_goal_recommendation,
                                session.pending_fan_goal_shortfall_selected,
                                session.pending_wbc_hub_race,
                                session.pending_gur_race,
                            )
                        )
                    )
                )
            ):
                return self._stop(
                    "fan_goal_shortfall:candidate_unverified",
                    route_state="race_list",
                    evidence=evidence,
                )
            evidence["fan_goal_shortfall_candidate"] = fan_shortfall_candidate
            if not fan_shortfall_candidate["selected"]:
                center = fan_shortfall_candidate["card_center"]
                return self._tap(
                    "select_fan_goal_shortfall_candidate",
                    "race_list",
                    "race_list.fan_goal_shortfall_candidate",
                    (int(center[0]), int(center[1])),
                    evidence,
                )
            entry = self._bottom_center_green_button(
                perception,
                img,
                min_y_ratio=0.78,
            )
            if entry is None:
                return self._stop(
                    "fan_goal_shortfall:entry_unverified",
                    route_state="race_list",
                    evidence=evidence,
                )
            decision = self._tap(
                "enter_fan_goal_shortfall_selected_race",
                "race_list",
                "race_list.fan_goal_shortfall_selected_entry",
                entry,
                evidence,
            )
            decision["fan_goal_shortfall_selected_verification"] = {
                **fan_shortfall_candidate,
                "source": "race_list.fan_goal_shortfall_selected_card",
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "action_id": "race_list.fan_goal_shortfall_selected_entry",
                "single_use": True,
            }
            return decision
        scenario_final_capability = self._pending_scenario_final_race(
            session,
            evidence,
        )
        if (
            policy.allow_select_visible_g1
            and locked_target_verification is None
            and scenario_final_capability is None
            and (
                is_visual_race_list
                or positive_target_race
                or text_verified_race_list
            )
            and selected_grade != "G1"
            and (session is None or not session.scheduled_binding_active)
        ):
            visible_g1 = self._visible_g1_race_candidate(img)
            if visible_g1 is not None:
                evidence["visible_g1_candidate"] = visible_g1
                center = visible_g1["center"]
                return self._tap(
                    "select_visible_g1_candidate",
                    "race_list",
                    "race_list.visible_g1_candidate",
                    (int(center[0]), int(center[1])),
                    evidence,
                )
            if policy.allow_scan_race_list_for_g1:
                if session is None:
                    return self._stop(
                        "race_list_scan:session_required",
                        route_state="race_list",
                        evidence=evidence,
                    )
                viewport_sha256 = self._race_list_viewport_sha256(img)
                if viewport_sha256 in session.race_list_scan_viewports:
                    return self._stop(
                        "race_list_scan:viewport_repeated",
                        route_state="race_list",
                        evidence=evidence,
                    )
                if len(session.race_list_scan_viewports) >= 2:
                    return self._stop(
                        "race_list_scan:limit_reached",
                        route_state="race_list",
                        evidence=evidence,
                    )
                evidence["race_list_scan"] = {
                    "source": "race_list.viewport_pixels",
                    "viewport_sha256": viewport_sha256,
                    "scan_index": len(session.race_list_scan_viewports) + 1,
                    "max_scans": 2,
                }
                return {
                    "action": "swipe",
                    "reason": "scan_race_list_for_visible_g1",
                    "route_state": "race_list",
                    "gesture": {
                        "coordinate_name": "race_list.scan_for_visible_g1",
                        "x1": int(round(img.shape[1] * 0.50)),
                        "y1": int(round(img.shape[0] * 0.76)),
                        "x2": int(round(img.shape[1] * 0.50)),
                        "y2": int(round(img.shape[0] * 0.55)),
                        "duration_ms": 450,
                        "scaled": False,
                        "direction": "up",
                    },
                    "evidence": evidence,
                }
        if (
            policy.allow_leave_race_list
            and fan_goal_recommendation is None
            and scenario_final_capability is None
            and screen != "race_participant_list"
            and is_visual_race_list
            and selected_grade != "G1"
        ):
            if positive_non_goal_race:
                height, width = img.shape[:2]
                return self._tap(
                    "leave_non_goal_race_list",
                    "race_list",
                    "race_list.back",
                    (int(width * 0.105), int(height * 0.963)),
                    evidence,
                )
            if not positive_target_race:
                return self._stop(
                    "race_list:leave_not_confirmed_non_goal",
                    route_state="race_list",
                    evidence=evidence,
                )
        if positive_non_goal_race and scenario_final_capability is None:
            return self._stop("race_list:no_goal_race_candidate", route_state="race_list", evidence=evidence)
        if (
            screen == "race_participant_list"
            and not looks_like_race_list
            and early_preview_actions is None
        ):
            button = self._bottom_center_green_region(img, min_y_ratio=0.82)
            if button is None:
                return self._stop("race_list:no_primary_entry", evidence=evidence)
            if policy.scenario == "larc":
                evidence["larc_race_participants"] = {"verified": True}
            if policy.allow_skip_race_scene:
                return self._tap(
                    "race_scene_start_or_continue",
                    "race_scene",
                    "race_scene.start_or_continue",
                    button,
                    evidence,
                )
            if not policy.allow_enter_race:
                return self._stop("race_list:enter_not_allowed", evidence=evidence)
            return self._tap(
                "enter_race_from_list",
                "race_list",
                "race_list.primary_entry",
                button,
                evidence,
            )
        if self._looks_like_portrait_race_title(perception, img):
            return self._portrait_scene_skip(policy, observed_free_control and result_owner,
                img, self._portrait_race_title_skip_button(img), evidence)

        if self._looks_like_large_rank_result_animation(perception, img):
            # The visual result layout and its foreground tap caption suffice.
            # The rank number is not needed to dismiss this overlay.
            observed_overlay = observed_free_control and result_owner and "点击" in current_text
            return self._continue_observed_result(policy, observed_overlay, self._screen_continue_point(img), evidence)

        if self._looks_like_rank_overlay_result(perception, img):
            # The visual result layout and its foreground tap caption suffice.
            # The rank number is not needed to dismiss this overlay.
            observed_overlay = observed_free_control and result_owner and "点击" in current_text
            return self._continue_observed_result(policy, observed_overlay, self._screen_continue_point(img), evidence)

        if self._looks_like_gold_race_transition_controls(perception, img):
            return self._portrait_scene_skip(policy, observed_free_control and result_owner,
                img, self._portrait_race_intro_skip_button(img), evidence)

        if self._looks_like_portrait_race_scene_bottom_controls(perception, img):
            return self._portrait_scene_skip(policy, observed_free_control and result_owner,
                img, self._portrait_race_intro_skip_button(img), evidence)

        if self._looks_like_post_race_victory_controls(perception, img):
            return self._portrait_scene_skip(policy, observed_free_control and result_owner,
                img, self._portrait_race_intro_skip_button(img), evidence)

        if self._looks_like_training_home_ready_for_race_entry(perception, img, safe_planner):
            if not policy.allow_enter_race_from_main_menu:
                return handoff("ready_main_menu", evidence=evidence)
            height, width = img.shape[:2]
            return self._tap(
                "enter_race_from_main_menu",
                "ready_main_menu",
                "main_menu.race",
                (int(width * 0.839), int(height * 0.867)),
                evidence,
            )

        race_entry_modal = self._looks_like_race_modal(img)
        race_playback_modal = self._looks_like_race_playback_modal(img)
        if race_entry_modal or race_playback_modal:
            visual_state = "race_entry_confirm" if race_entry_modal else "race_playback_confirm"
            if (
                race_entry_modal
                and policy.allow_leave_race_detail
                and not policy.allow_confirm_race
            ):
                return self._tap(
                    "cancel_unverified_race_confirm",
                    visual_state,
                    "race_detail.back",
                    self._race_confirm_cancel_point(evidence, img),
                    evidence,
                )
            if not (policy.allow_confirm_race or (race_playback_modal
                    and not race_entry_modal and policy.allow_skip_race_scene)):
                return self._stop(
                    "race_confirm:confirm_not_allowed",
                    route_state=visual_state,
                    evidence=evidence,
                )
            has_pending_target = self._has_pending_target_race(session, evidence)
            exchange = self._larc_exchange_owner(session, evidence, policy=policy, current_detail=True)
            if race_entry_modal and exchange is not None:
                button = self._primary_confirm_button(perception, img)
                if button is None:
                    return self._stop("race_confirm:no_confirm_button", route_state=visual_state, evidence=evidence)
                return self._larc_exchange_confirm_tap(visual_state, button, evidence, exchange,
                    consume_target=has_pending_target)
            wbc_capability = self._pending_wbc_hub_race(session, evidence)
            gur_capability = self._pending_gur_race(session, evidence)
            if (
                race_entry_modal
                and not has_pending_target
                and wbc_capability is None
                and gur_capability is None
            ):
                if policy.allow_leave_race_detail is True:
                    return self._tap(
                        "cancel_unverified_race_confirm",
                        visual_state,
                        "race_detail.back",
                        self._race_confirm_cancel_point(evidence, img),
                        evidence,
                    )
                return self._stop(
                    "race_confirm:target_verification_required",
                    route_state=visual_state,
                    evidence=evidence,
                )
            button = self._primary_confirm_button(perception, img)
            if button is None:
                return self._stop("race_confirm:no_confirm_button", route_state=visual_state, evidence=evidence)
            reason = (
                "confirm_race_entry"
                if race_entry_modal and button[1] / max(img.shape[0], 1) >= 0.69
                else "confirm_race_playback"
            )
            state = (
                "race_entry_confirm"
                if reason == "confirm_race_entry"
                else "race_playback_confirm"
            )
            if reason == "confirm_race_entry":
                if wbc_capability is not None:
                    evidence["wbc_hub_verification"] = {
                        **wbc_capability,
                        "confirmation_source": "race_entry_confirm.visual_modal",
                    }
                    decision = self._tap(
                        "confirm_wbc_hub_race",
                        state,
                        "race_confirm.wbc_hub",
                        button,
                        evidence,
                    )
                    decision["consume_wbc_hub_verification"] = True
                    return decision
                if gur_capability is not None:
                    evidence["gur_race_verification"] = {
                        **gur_capability,
                        "confirmation_source": "race_entry_confirm.visual_modal",
                    }
                    decision = self._tap(
                        "confirm_gur_race",
                        state,
                        "race_confirm.gur",
                        button,
                        evidence,
                    )
                    decision["consume_gur_race_verification"] = True
                    return decision
                return self._target_race_confirm_tap(state, button, evidence)
            return self._tap(reason, state, "race_confirm.primary", button, evidence)
        if screen == "modal_confirm":
            if not policy.allow_confirm_race:
                return self._stop("modal_confirm:confirm_not_allowed", evidence=evidence)
            safe_confirm = safe_planner._safe_green_confirm_button(perception, img)
            if safe_confirm is not None:
                return self._tap(
                    "confirm_safe_modal",
                    "safe_modal",
                    "safe_advance.safe_confirm",
                    safe_confirm,
                    evidence,
                )
            return self._stop("modal_confirm:unsupported", evidence=evidence)

        if (
            safe_planner._looks_like_race_participant_list(img)
            and not looks_like_race_list
            and early_preview_actions is None
        ):
            button = self._bottom_center_green_region(img, min_y_ratio=0.82)
            if button is None:
                return self._stop("race_list:no_primary_entry", evidence=evidence)
            if policy.scenario == "larc":
                evidence["larc_race_participants"] = {"verified": True}
            if policy.allow_skip_race_scene:
                return self._tap(
                    "race_scene_start_or_continue",
                    "race_scene",
                    "race_scene.start_or_continue",
                    button,
                    evidence,
                )
            if not policy.allow_enter_race:
                return self._stop("race_list:enter_not_allowed", evidence=evidence)
            return self._tap(
                "enter_race_from_list",
                "race_list",
                "race_list.primary_entry",
                button,
                evidence,
            )

        race_list_entry = (
            self._bottom_center_green_button(perception, img, min_y_ratio=0.78)
            or self._bottom_center_green_region(img, min_y_ratio=0.82)
            if looks_like_race_list
            else None
        )
        has_enterable_race_list = looks_like_race_list and race_list_entry is not None

        if not has_enterable_race_list and self._looks_like_rank_points_result(img):
            point = self._rank_points_next_button(perception, img)
            if point is None and policy.allow_result_continue:
                point = self._screen_continue_point(img)
            return self._continue_observed_result(policy, observed_free_control, point, evidence)

        if not has_enterable_race_list and self._looks_like_ranking_list_result(img):
            point = self._rank_points_next_button(perception, img)
            if point is None and policy.allow_result_continue:
                point = self._screen_continue_point(img)
            return self._continue_observed_result(policy, observed_free_control, point, evidence)

        if looks_like_race_list:
            if not policy.allow_enter_race:
                return self._stop("race_list:enter_not_allowed", evidence=evidence)
            if race_list_entry is None:
                return self._stop("race_list:no_primary_entry", evidence=evidence)
            final_capability = scenario_final_capability
            final_card = self._grand_masters_final_selected_list(evidence, img)
            if (policy.allow_grand_masters_final_race_confirm is True
                and session is not None and not session.scheduled_binding_active
                and final_capability is not None and final_card is not None):
                if final_capability.get("list_entry_source_image_sha256") is not None:
                    return self._stop("race_list:scenario_final_entry_consumed", route_state="race_list", evidence=evidence)
                evidence["scenario_final_selected_card"] = final_card
                decision = self._tap("enter_grand_masters_final_from_list", "race_list",
                                     "race_list.primary_entry", race_list_entry, evidence)
                decision["scenario_final_list_entry_verification"] = {
                    **final_capability, "list_entry_source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                return decision
            if (
                policy.allow_fan_goal_recommended_race is True
                and fan_goal_recommendation is not None
                and selected_grade == "G1"
            ):
                decision = self._tap(
                    "enter_fan_goal_recommended_race",
                    "race_list",
                    "race_list.fan_goal_recommended_entry",
                    race_list_entry,
                    evidence,
                )
                decision["fan_goal_recommendation_verification"] = {
                    **fan_goal_recommendation,
                    "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "action_id": "race_list.fan_goal_recommended_entry",
                    "single_use": True,
                }
                return decision
            if selected_race is None or selected_race.get("grade") != "G1":
                locked_verification = locked_target_verification
                if (
                    locked_verification is not None
                    and selected_grade in {"G2", "G3", "OPEN"}
                    and (positive_target_race or text_verified_race_list)
                ):
                    evidence["prior_locked_target_verification"] = (
                        locked_verification
                    )
                    return self._tap(
                        "enter_locked_target_race_from_list",
                        "race_list",
                        "race_list.primary_entry",
                        race_list_entry,
                        evidence,
                    )
                if (
                    policy.allow_locked_race_entry is True
                    and selected_grade in {"G2", "G3", "OPEN"}
                    and positive_target_race
                    and not positive_non_goal_race
                    and session is not None
                    and not session.scheduled_binding_active
                    and all(
                        pending is None
                        for pending in (
                            session.pending_target_race,
                            session.pending_locked_target_race,
                            session.pending_scenario_final_race,
                            session.pending_fan_goal_recommendation,
                            session.pending_fan_goal_shortfall_selected,
                            session.pending_wbc_hub_race,
                            session.pending_gur_race,
                        )
                    )
                ):
                    decision = self._tap(
                        "enter_locked_target_race_from_list",
                        "race_list",
                        "race_list.primary_entry",
                        race_list_entry,
                        evidence,
                    )
                    decision["locked_target_verification"] = {
                        "source": "race_list.same_frame_locked_target",
                        "action_id": "race_list.primary_entry",
                        "source_image_sha256": hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest(),
                        "grade": selected_grade,
                        "single_use": True,
                    }
                    return decision
                return self._stop(
                    "race_list:selected_grade_not_g1",
                    route_state="race_list",
                    evidence=evidence,
                )
            if session is not None and session.scheduled_binding_active:
                if session.scheduled_entry_consumed:
                    if (policy.count_reserved_only and session.count_phase == "confirmed"
                        and len(session.planned_race_ids) == 1
                        and selected_race.get("race_id") == session.planned_race_ids[0]
                        and selected_race.get("source_image_sha256") == hashlib.sha256(path.read_bytes()).hexdigest()):
                        return {"action": "wait", "reason": "count_race_post_confirm_wait",
                                "route_state": "race_list", "evidence": evidence}
                    return self._stop(
                        "race_list:scheduled_session_consumed",
                        route_state="race_list",
                        evidence=evidence,
                    )
                selected_race_id = selected_race.get("race_id")
                if not isinstance(selected_race_id, str) or not selected_race_id:
                    return self._stop(
                        "race_list:selected_race_id_required",
                        route_state="race_list",
                        evidence=evidence,
                    )
                if selected_race_id not in session.planned_race_ids:
                    return self._stop(
                        "race_list:selected_race_not_planned",
                        route_state="race_list",
                        evidence=evidence,
                    )
                selected_race = {
                    **selected_race,
                    "schedule_packet_sha256": session.schedule_packet_sha256,
                }
            locked_verification = (
                session.locked_target_race_verification()
                if (
                    policy.allow_locked_race_entry is True
                    and session is not None
                )
                else None
            )
            if locked_verification is not None:
                evidence["prior_locked_target_verification"] = (
                    locked_verification
                )
            decision = self._tap(
                "enter_selected_target_race",
                "race_list",
                "race_list.primary_entry",
                race_list_entry,
                evidence,
            )
            decision["target_race_verification"] = selected_race
            return decision
        return None

    def _plan_stage_preview_scene_and_fallbacks(
        self,
        *,
        state: _RacePlanState,
        img: np.ndarray,
        perception: dict[str, Any],
        screen: str,
        evidence: dict[str, Any],
        policy: RaceRoutePolicy,
        session: RaceRouteSession | None,
        can_handoff_dialogue: bool,
        observed_free_control: bool,
        result_owner: bool,
    ) -> dict[str, Any]:
        safe_planner = state.safe_planner
        if can_handoff_dialogue and self._looks_like_training_story_dialogue(img):
            return handoff("post_race_dialogue", evidence=evidence)
        if (
            can_handoff_dialogue and safe_planner._looks_like_story_dialogue_with_record_controls(img)
            and self._bottom_center_green_button(perception, img, min_y_ratio=0.72) is None
        ):
            return handoff("post_race_dialogue", evidence=evidence)
        if (
            safe_planner._single_green_event_option_button(img) is not None
            and safe_planner._looks_like_blocking_bottom_dialogue(img)
        ):
            return handoff("event_choice", evidence=evidence)

        if self._looks_like_portrait_race_intro_controls(perception, img):
            return self._portrait_scene_skip(policy, observed_free_control and result_owner,
                img, self._portrait_race_intro_skip_button(img), evidence)

        if self._looks_like_portrait_race_scene_controls(perception, img):
            return self._portrait_scene_skip(policy, observed_free_control and result_owner,
                img, self._portrait_race_intro_skip_button(img), evidence)

        if safe_planner._looks_like_main_training_menu(img):
            return handoff("ready_main_menu", evidence=evidence)

        race_preview_actions = self._race_preview_action_points(img)
        quick_result_available = (
            policy.allow_quick_result is True
            and race_preview_actions is not None
            and not race_preview_actions.get("view_result_locked")
        )
        if (
            policy.allow_leave_race_detail is True
            and not quick_result_available
            and self._looks_like_race_detail_go_to_race(perception, img, screen)
        ):
            back = self._race_detail_back_button(img)
            if back is None:
                return self._stop(
                    "race_detail:no_back_button",
                    route_state="race_detail",
                    evidence=evidence,
                )
            return self._tap(
                "leave_race_detail",
                "race_detail",
                "race_detail.back",
                back,
                evidence,
            )

        if race_preview_actions is not None:
            if policy.allow_quick_result is True and not race_preview_actions.get("view_result_locked"):
                return (None if policy.wbc_only or policy.count_reserved_only else self._configured_decision("race_detail_view_result", img, evidence)) or self._tap(
                    "view_race_result",
                    "race_preview",
                    "race_detail.view_result",
                    race_preview_actions["view_result"],
                    evidence,
                )
            if policy.allow_leave_race_detail is True:
                back = self._race_detail_back_button(img)
                if back is None:
                    return self._stop(
                        "race_detail:no_back_button",
                        route_state="race_detail",
                        evidence=evidence,
                    )
                return self._tap(
                    "leave_race_detail",
                    "race_detail",
                    "race_detail.back",
                    back,
                    evidence,
                )
            if (
                policy.allow_quick_result is True
                and race_preview_actions.get("view_result_locked")
                and session is not None
                and session.result_transition_pending
            ):
                return {
                    "action": "wait",
                    "reason": "loading",
                    "route_state": "loading",
                    "evidence": evidence,
                }
            if (
                race_preview_actions.get("view_result_locked")
                and policy.allow_locked_race_entry is not True
                and not (policy.count_reserved_only and session is not None
                         and session.count_phase == "confirmed" and session.scheduled_entry_consumed is True)
            ):
                return self._stop(
                    "race_preview:view_result_locked",
                    route_state="race_detail",
                    evidence=evidence,
                )
            if race_preview_actions.get("view_result_locked") and policy.allow_quick_result is True:
                evidence["quick_result_unavailable_locked"] = True
            if not (policy.allow_confirm_race or policy.allow_skip_race_scene):
                return self._stop("race_preview:advance_not_allowed", evidence=evidence)
            return self._tap(
                "go_to_race_from_detail",
                "race_detail",
                "race_detail.go_to_race",
                race_preview_actions["go_to_race"],
                evidence,
            )

        if race_preview_actions is None and self._looks_like_race_detail_loading(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "race_detail_loading",
                "evidence": evidence,
            }

        if (
            race_preview_actions is None
            and self._looks_like_unknown_race_preview_action_layout(img)
            and self._swbc_special_entry_go_to_race_button(img) is None
        ):
            return self._stop(
                "race_preview:unknown_action_layout",
                route_state="race_detail",
                evidence=evidence,
            )

        swbc_special_entry = self._swbc_special_entry_go_to_race_button(img)
        if swbc_special_entry is not None:
            if not policy.allow_confirm_race:
                return self._stop(
                    "race_detail:confirm_not_allowed",
                    route_state="race_detail",
                    evidence=evidence,
                )
            return self._tap(
                "go_to_race_from_detail",
                "race_detail",
                "race_detail.go_to_race",
                swbc_special_entry,
                evidence,
            )

        if self._looks_like_event_choice_layout(img):
            return handoff("event_choice", evidence=evidence)
        if screen == "event_choice" and self._looks_like_transition_wipe(perception, img):
            return {
                "action": "wait",
                "reason": "transition",
                "route_state": "transition",
                "evidence": evidence,
            }
        if can_handoff_dialogue and screen == "event_choice" and safe_planner._looks_like_training_result_dialogue(img):
            return handoff("post_race_dialogue", evidence=evidence)
        if can_handoff_dialogue and screen == "training_menu" and safe_planner._looks_like_training_result_dialogue(img):
            return handoff("post_race_dialogue", evidence=evidence)

        if self._looks_like_race_detail_go_to_race(perception, img, screen):
            button = self._bottom_center_green_button(perception, img, min_y_ratio=0.80)
            if button is None:
                return self._stop("race_detail:no_go_to_race_button", evidence=evidence)
            center_participate_layout = self._looks_like_center_race_detail_action_layout(img)
            if (
                policy.allow_quick_result
                and not center_participate_layout
                and session is not None
                and session.result_transition_pending
            ):
                return {
                    "action": "wait",
                    "reason": "loading",
                    "route_state": "loading",
                    "evidence": evidence,
                }
            if policy.allow_quick_result and not center_participate_layout:
                return self._configured_decision("race_detail_view_result", img, evidence) or self._tap(
                    "view_race_result",
                    "race_detail",
                    "race_detail.view_result",
                    self._view_result_button(img, button),
                    evidence,
                )
            if not policy.allow_confirm_race:
                return self._stop("race_detail:confirm_not_allowed", evidence=evidence)
            return self._tap(
                "go_to_race_from_detail",
                "race_detail",
                "race_detail.go_to_race",
                button,
                evidence,
            )

        if self._looks_like_result_continue(perception, img):
            # A green footer alone is not verified result ownership.
            if not policy.allow_result_continue:
                return self._stop("race_result:continue_not_allowed", evidence=evidence)
            button = self._bottom_center_green_button(perception, img, min_y_ratio=0.72)
            if button is None:
                return self._stop("race_result:no_continue_button", evidence=evidence)
            return self._configured_decision("race_result_continue", img, evidence) or self._tap(
                "race_result_continue",
                "race_result",
                "race_result.continue",
                button,
                evidence,
            )

        if self._is_landscape(perception):
            # Generic landscape does not establish a race or a skip control.
            if not policy.allow_skip_race_scene:
                return self._stop("race_scene:skip_not_allowed", evidence=evidence)
            return {
                "action": "wait",
                "reason": "race_scene_running",
                "route_state": "race_scene",
                "evidence": evidence,
            }

        if self._looks_like_portrait_race_start(perception, img, policy):
            button = self._bottom_center_green_region(img, min_y_ratio=0.86)
            if button is None:
                return self._stop("race_scene:no_start_button", evidence=evidence)
            return self._tap(
                "race_scene_start_or_continue",
                "race_scene",
                "race_scene.start_or_continue",
                button,
                evidence,
            )

        if self._looks_like_loading(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "loading",
                "evidence": evidence,
            }

        if self._looks_like_race_detail_loading(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "race_detail_loading",
                "evidence": evidence,
            }

        if self._looks_like_portrait_race_scene(perception, img, policy):
            return {
                "action": "wait",
                "reason": "race_scene_running",
                "route_state": "race_scene",
                "evidence": evidence,
            }

        if (
            session is not None
            and session.result_transition_pending
            and screen in {"portrait_ui", "training_menu"}
        ):
            evidence["result_transition_pending"] = True
            return {
                "action": "wait",
                "reason": "race_scene_running",
                "route_state": "race_scene",
                "evidence": evidence,
            }

        return self._stop(f"unsupported_screen:{screen}", evidence=evidence)
