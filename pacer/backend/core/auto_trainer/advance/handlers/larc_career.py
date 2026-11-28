"""L'Arc and shared common-career page handlers for SafeAdvance."""
from __future__ import annotations
from core.auto_trainer.runtime_control import reobserving_step, controlled_sleep

import hashlib
import inspect
import json
import math
import re
import copy
import tempfile
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

import cv2
import numpy as np

from core.frame_io import read_image_with_retry
from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_start_perception import CareerStartPerceptor
from core.auto_trainer.global_home_layout import is_global_home, global_home_portal_point
from core.auto_trainer.career_goal_perception import (
    CareerGoalPerceptionError,
    career_goal_observation_to_dict,
    is_goal_achievement_title,
    match_main_menu_count_goal,
    match_main_menu_named_goal,
)
from core.auto_trainer.career_goal_ledger import (
    CareerGoalLedger,
    CareerGoalLedgerError,
    CareerGoalLedgerStore,
    build_count_goal_completion_receipt,
    build_named_goal_completion_receipt,
)
from core.auto_trainer.main_menu_state_provider import TARGET_IDS, _CALENDAR_REGION_READER
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_page,
    plan_larc_explanation,
    plan_larc_ss_entry,
    plan_larc_ss_preview,
)
from core.auto_trainer.larc_race_day_context import read_larc_race_day_context
from core.auto_trainer.larc_adaptation import (
    LarcAdaptationSession,
    plan_larc_adaptation,
    plan_larc_adaptation_entry,
    plan_larc_adaptation_return,
)
from core.auto_trainer.career_start_route import _post_binding_valid
from core.auto_trainer.event_options import (
    event_option_rows_ready,
    single_story_event_option_point,
    is_carrot_event_narrative,
)
from core.auto_trainer.career_result_layouts import (
    diagonal_goal_band,
    exchange_history_continue_point,
)
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver, FreshFrameEvidence
from core.auto_trainer.frame_safety import FormalActionSafetyGate
from core.auto_trainer.screen_graph import ScreenGraph, classify_landscape_race_screen
from core.auto_trainer.skip_session import RunSkipSession, SkipSessionError, SkipSessionState
from core.auto_trainer.route_transition_session import (
    POST_ACTION_ROUTE_SUCCESSORS,
    RouteTransitionError,
    RouteTransitionSession,
    RouteTransitionStatus,
)
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from core.auto_trainer.safe_advance_transition import (
    SafeAdvanceRoutePolicy,
    action_allowed_from_state,
    expected_next_states_for_decision,
    is_global_route_interrupt,
    observed_route_state,
)
from core.auto_trainer.skill_handoff import (
    SkillAcquisitionHandoffSafetyError,
    SkillHandoffResumeAuthorization,
    SkillHandoffSession,
    validate_resume_authorization,
    validate_resume_session,
)
from core.coordinates import CoordinateConfig, CoordinateScaler
from core.grand_masters_overlay import detect_grand_masters_crystal_reward_overlay
from core.main_menu_state import HIGH_CONFIDENCE
from core.training_menu_precheck import TrainingMenuPrecheckResult
from .._compat import (
    FrameSafetyAnalyzer,
    Perception,
    RaceRoutePlanner,
    SafeAdvanceExecutor,
    SafeAdvancePlanner,
    SpeedTogglePlanner,
    TesseractTsvTokenReader,
    TrainingMenuPrechecker,
    analyze_main_menu_state,
    analyze_screen_text,
    detect_event_option_layout,
    fuse_perception_with_text,
    inspect_skill_acquisition_page,
    is_larc_race_day,
    perceive_goal_page,
    rating_continue_point,
    sa_attr,
)
from ..constants import (
    ADVANCE_CONFIRM_TOKEN,
    DIALOGUE_NEXT_POINT,
    LONG_SETTLE_REASONS,
    POST_WAIT_FREEZE_DIFF_HISTORY_LIMIT,
    POST_WAIT_FREEZE_MAX_FRAME_DIFF,
    POST_WAIT_FREEZE_POLL_SECONDS,
    POST_WAIT_FREEZE_STABLE_FRAMES,
    POST_WAIT_FREEZE_TIMEOUT_SECONDS,
    POST_WAIT_TRAINING_HUD_REFERENCE_SIZE,
    POST_WAIT_TRAINING_HUD_STABILITY_ROIS,
    RECRUIT_HOME_NAV_REFERENCE_PATH,
    RECRUIT_HOME_NAV_REFERENCE_SHA256,
    SPECIAL_PAGE_WAIT_DEFAULT_LIMIT,
    SPECIAL_PAGE_WAIT_MAX_LIMIT,
    SPECIAL_PAGE_WAIT_REASONS,
    START_TRAINING_SETTLE_SECONDS,
    SUPPORTED_ADVANCE_SCREEN_SIZES,
    WISDOM_FRAGMENT_DIALOG_ANCHORS,
    WISDOM_FRAGMENT_DIALOG_MANIFEST_PATH,
    WISDOM_FRAGMENT_DIALOG_ROI,
    WISDOM_FRAGMENT_DIALOG_SOURCE_PATH,
    WISDOM_FRAGMENT_DIALOG_SOURCE_SHA256,
    WISDOM_FRAGMENT_DIALOG_TEMPLATE_PATH,
    WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256,
    SpecialPageWaitBudget,
    WisdomFragmentDialogResult,
    WisdomFragmentDialogState,
    _GLOBAL_HOME_PASSIVE_RESOURCE_TERMS,
    _OPTIONAL_NAVIGATION_PAIRS,
    _UNBOUND_GOAL_SUMMARY_ACTION_IDS,
    _UNBOUND_GOAL_SUMMARY_ACTION_PAIRS,
    _WisdomFragmentTemplateLoad,
    _career_end_skill_points_from_visible_text,
    _unused_skill_points_from_visible_text,
    post_wait_navigation_reobserve,
)


class LarcCareerHandlerMixin:
    """L'Arc scenario and shared common-career routing handlers."""

    def _plan_common_career_page(
        self, path: Path, img: np.ndarray, evidence: dict[str, Any], *,
        route_policy: SafeAdvanceRoutePolicy | None,
        scenario: str | None,
        allow_return_main_menu_from_training: bool,
        allow_larc_ss: bool,
        allow_larc_first_arc_skill_inspection: bool = False,
        allow_larc_final_arc_skill_inspection: bool = False,
        larc_ss_preview_only: bool = False,
        larc_ss_preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        larc_supporter_goal: dict[str, Any] | None = None,
        larc_adaptation_session: LarcAdaptationSession | None = None,
        allow_career_end_finish_after_skill_handoff: bool = False,
        allow_finish_training_with_unused_skill_points: bool = False,
    ) -> dict[str, Any]:
        """Bounded shared-page recognition; never fall through to scenario rules.

        This is not a complete scenario runner. Unrecognized pages stop, and
        event choices remain the responsibility of the dedicated event route.
        """
        early = self._plan_common_priority_modals(
            img,
            evidence,
            scenario=scenario,
            allow_career_end_finish_after_skill_handoff=allow_career_end_finish_after_skill_handoff,
            allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
        )
        if early is not None:
            return early
        mid = self._plan_common_larc_and_modals(
            path,
            img,
            evidence,
            route_policy=route_policy,
            scenario=scenario,
            allow_return_main_menu_from_training=allow_return_main_menu_from_training,
            allow_larc_ss=allow_larc_ss,
            allow_larc_first_arc_skill_inspection=allow_larc_first_arc_skill_inspection,
            allow_larc_final_arc_skill_inspection=allow_larc_final_arc_skill_inspection,
            larc_ss_preview_only=larc_ss_preview_only,
            larc_ss_preview_decider=larc_ss_preview_decider,
            larc_supporter_goal=larc_supporter_goal,
            larc_adaptation_session=larc_adaptation_session,
        )
        if mid is not None:
            return mid
        return self._plan_common_menus_and_dialogue(
            path,
            img,
            evidence,
            route_policy=route_policy,
            scenario=scenario,
            allow_return_main_menu_from_training=allow_return_main_menu_from_training,
            allow_larc_ss=allow_larc_ss,
            larc_ss_preview_only=larc_ss_preview_only,
            larc_supporter_goal=larc_supporter_goal,
            larc_adaptation_session=larc_adaptation_session,
        )

    def _plan_common_priority_modals(
        self,
        img: np.ndarray,
        evidence: dict[str, Any],
        *,
        scenario: str | None,
        allow_career_end_finish_after_skill_handoff: bool,
        allow_finish_training_with_unused_skill_points: bool,
    ) -> dict[str, Any] | None:
        evidence["recognition_scope"] = "common_career"
        screen_text = evidence.get("screen_text")
        if screen_text is not None and screen_text.get("dangerous"):
            if self._is_account_login_conflict_text(screen_text.get("normalized_text", "")):
                return self._stop("account_login_conflict", evidence=evidence)
            pass_renewal_cancel = self._pass_renewal_cancel_button(screen_text, img)
            if pass_renewal_cancel is not None:
                return self._tap_decision(
                    "close_pass_renewal_popup",
                    "safe_advance.pass_renewal_cancel",
                    pass_renewal_cancel,
                    evidence,
                )
            return self._stop("resource_prompt", evidence=evidence)
        if scenario == "larc":
            recommendation = self._race_recommendation_top_confirm_button(evidence,
                evidence["perception"], img, current_setting_only=True)
            if recommendation is not None:
                decision = self._tap_decision("confirm_race_recommendation_settings",
                    "safe_advance.race_recommendation_top_confirm", recommendation, evidence)
                decision["route_state"] = "modal"
                return decision
        if scenario == "larc":
            goal_continue = self._goal_achieved_continue_button(
                evidence["perception"], img, goal_layout_only=True)
            if goal_continue is not None:
                return self._tap_decision("continue_goal_achievement_result",
                    "safe_advance.goal_achievement_continue", goal_continue, evidence)
            if self._looks_like_goal_achievement_animation(
                    evidence["perception"], img, goal_layout_only=True):
                return {"action": "wait", "reason": "loading",
                        "route_state": "goal_achievement_animation", "evidence": evidence}
        if (scenario == "larc" and screen_text is not None
            and str(screen_text.get("normalized_text", "")).splitlines() == ["目标达成!"]):
            height, width = img.shape[:2]
            if height >= 600 and width >= 320:
                center = img[int(height * 0.25):int(height * 0.65),
                             int(width * 0.10):int(width * 0.90)]
                if center.size and float(np.mean(np.all(center > 205, axis=2))) > 0.70:
                    return {"action": "wait", "reason": "loading",
                            "route_state": "goal_achievement_animation", "evidence": evidence}
        if scenario == "larc" and "技能获取" in str((screen_text or {}).get("normalized_text", "")):
            skill_page = inspect_skill_acquisition_page(img)
            evidence["skill_acquisition_page"] = skill_page
            if skill_page.get("accepted") is True and self._skill_acquisition_back_control(img) is not None:
                return handoff("skill_acquisition_handoff", evidence=evidence,
                                  route_state="skill_acquisition", purchase_executed=False)
        if (scenario == "larc" and "".join(str((screen_text or {}).get("normalized_text", "")).split())
                == "日期变化日期发生了变化确定"):
            button = self._safe_green_confirm_button(evidence["perception"], img, allow_obscured_background=True)
            if button is not None:
                return self._tap_decision("larc_date_change_confirm", "safe_advance.date_change_confirm", button, evidence)
        if scenario == "larc" and self._fan_goal_shortfall_warning_text_verified(screen_text):
            # This automatic reminder overlays the home menu. Cancel it so the
            # next fresh home observation can measure the actual fan shortfall.
            button = self._go_to_race_modal_button(evidence["perception"], img)
            if button is None or "取消" not in screen_text["normalized_text"]:
                return self._stop("fan_goal_warning_layout_unverified", evidence=evidence)
            return self._tap_decision("cancel_larc_fan_warning", "safe_advance.larc_fan_warning_cancel",
                (round(img.shape[1] * 0.279), button[1]), evidence)
        notice = str((screen_text or {}).get("normalized_text", ""))
        if scenario == "larc" and "无法参加目标赛事" in notice and "前往赛事" in notice:
            point = self._go_to_race_modal_button(evidence["perception"], img)
            if point is not None:
                if "零胜段位" in notice or "零胜战" in notice:
                    evidence["race_prerequisite"] = {
                        "source": "current_game_notice",
                        "kind": "first_win",
                    }
                    decision = self._tap_decision(
                        "go_to_race_entry_requirement",
                        "safe_advance.go_to_race",
                        point,
                        evidence,
                    )
                    decision["route_state"] = "modal"
                    return decision
                if (
                    "取消" in notice
                    and "赛事详情" not in notice
                    and "要参加比赛吗" not in notice
                ):
                    return self._tap_decision(
                        "cancel_larc_fan_warning",
                        "safe_advance.larc_fan_warning_cancel",
                        (round(img.shape[1] * 0.279), point[1]),
                        evidence,
                    )
        career_end = self._career_end_finish_button(screen_text, img)
        if career_end is not None:
            if allow_career_end_finish_after_skill_handoff:
                return self._tap_decision("open_career_end_finish_confirmation",
                    "safe_advance.career_end_finish", career_end, evidence)
            return handoff("career_end_confirmation", route_state="modal", evidence=evidence)
        unused_points_finish = self._finish_training_unused_points_confirm_button(evidence["perception"], screen_text, img)
        if unused_points_finish is not None:
            return self._plan_finish_training_confirmation(
                unused_points_finish, screen_text, img, evidence,
                allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
            )
        if scenario == "larc":
            history = exchange_history_continue_point(img)
            if history is not None:
                evidence["larc_explanation"] = {"kind":"exchange_schedule", "bottom_control":"continue"}
                decision = self._tap_decision("larc_explanation_continue",
                    "safe_advance.larc_explanation_continue", history, evidence)
                decision["route_state"] = "modal"
                return decision
            finish_training = self._finish_training_confirm_button(evidence["perception"], img)
            if finish_training is not None:
                return self._plan_finish_training_confirmation(
                    finish_training, screen_text, img, evidence,
                    allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
                    visual_fallback=True,
                )
            if self._looks_like_final_training_rating(screen_text, img, rating_layout_only=True):
                evidence["terminal_rating"] = {"verified": True, "completion_scope": "training_rating"}
                return handoff("terminal_rating_verified", route_state="modal", evidence=evidence)
        download_text = str((screen_text or {}).get("normalized_text", ""))
        if scenario == "larc" and "正在下载" in download_text and "请在网络良好的状态下进行" in download_text:
            return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
        shortened_event_settings = self._common_shorten_settings_decision(img, evidence, enable_skip_first=scenario == "larc")
        if shortened_event_settings is not None:
            return shortened_event_settings
        return None

    def _plan_pre_resource_race_confirmation(
        self,
        img: np.ndarray,
        screen_text: dict[str, Any] | None,
        scenario: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Intercept verified race/adaptation confirmations before generic resource-modal stop."""
        if scenario == "larc":
            from core.auto_trainer.larc_pages import larc_adaptation_warning_button

            if larc_adaptation_warning_button(img, evidence) is not None:
                cancel_point = (
                    round(202 * img.shape[1] / 720),
                    round(834 * img.shape[0] / 1280),
                )
                decision = self._tap_decision(
                    "cancel_larc_adaptation_race_warning",
                    "safe_advance.larc_adaptation_warning_cancel",
                    cancel_point,
                    evidence,
                )
                decision["route_state"] = "modal"
                return decision
        normalized = "".join(str((screen_text or {}).get("normalized_text", "")).split())
        if (
            "赛事详情" in normalized
            and "取消" in normalized
            and "参赛" in normalized
            and ("要参加比赛吗" in normalized or "是否参加比赛" in normalized)
        ):
            from core.auto_trainer.race_route import RaceRoutePlanner

            if RaceRoutePlanner._looks_like_race_modal(img):
                return self._stop(
                    "race_detail",
                    route_state="race_detail_confirm",
                    evidence=evidence,
                )
        return None

    def _plan_common_larc_and_modals(
        self,
        path: Path,
        img: np.ndarray,
        evidence: dict[str, Any],
        *,
        route_policy: SafeAdvanceRoutePolicy | None,
        scenario: str | None,
        allow_return_main_menu_from_training: bool,
        allow_larc_ss: bool,
        allow_larc_first_arc_skill_inspection: bool,
        allow_larc_final_arc_skill_inspection: bool,
        larc_ss_preview_only: bool,
        larc_ss_preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None,
        larc_supporter_goal: dict[str, Any] | None,
        larc_adaptation_session: LarcAdaptationSession | None,
    ) -> dict[str, Any] | None:
        screen_text = evidence.get('screen_text')
        # A verified aptitude confirmation has the same generic white/green
        # silhouette as a resource modal. Its exact plan/text/layout contract
        # must be evaluated first; real dangerous text and the action safety
        # gate remain mandatory.
        if scenario == "larc":
            if route_policy is not None and route_policy.allows_observed_state("larc_ss_information"):
                from core.auto_trainer.larc_ss_information_probe import read_navigation

                navigation = read_navigation(img)
                if navigation["page_verified"] and navigation["close"] is not None:
                    decision = self._tap_decision("larc_ss_information_close", "larc_ss_information.close",
                                                  tuple(navigation["close"]), evidence)
                    decision["route_state"] = "larc_ss_information"
                    return decision
            if (larc_adaptation_session is not None and larc_adaptation_session.stage == "awaiting_result"
                    and "海外适应性等级提升" in str((screen_text or {}).get("normalized_text", ""))):
                return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
            # A first-open help page contains example levels/points but is not
            # the purchase UI. Its foreground close/continue control comes first.
            explanation = plan_larc_explanation(img, evidence)
            if explanation is not None:
                return explanation
            adaptation = plan_larc_adaptation(img, evidence, session=larc_adaptation_session)
            if adaptation is not None:
                if (larc_adaptation_session is not None and larc_adaptation_session.stay_on_grid
                        and adaptation.get("reason") == "larc_adaptation_inspection_complete"):
                    return adaptation
                return plan_larc_adaptation_return(img, adaptation)
        pre_resource = self._plan_pre_resource_race_confirmation(
            img,
            screen_text,
            scenario,
            evidence,
        )
        if pre_resource is not None:
            return pre_resource
        if self._looks_like_unreadable_resource_modal(img):
            return self._stop("resource_prompt_unreadable", evidence=evidence)
        lines = set(str((screen_text or {}).get("normalized_text", "")).splitlines())
        if {"外出", "取消"}.issubset(lines) and "外出确认" not in lines:
            from core.larc_outing_reader import MeiOutingReader
            chooser = MeiOutingReader().read(img)
            if chooser.get("screen") in {"mei_outing_chooser", "outing_chooser"} and chooser.get("cancel"):
                decision = self._tap_decision("cancel_unconsumed_outing_chooser",
                    "safe_advance.outing_chooser_cancel", tuple(chooser["cancel"]), evidence)
                decision["route_state"] = "mei_outing_chooser"
                return decision
        failure_summary = self._goal_failure_summary_continue_button(screen_text, evidence["perception"], img)
        if failure_summary is not None:
            evidence["career_outcome"] = {"goal_failed": True, "source": "visible_goal_failure_summary"}
            decision = self._tap_decision("continue_goal_failure_summary",
                "safe_advance.goal_failure_summary_continue", failure_summary, evidence)
            decision["route_state"] = "modal"
            return decision
        if "因子继承" in str((screen_text or {}).get("normalized_text", "")):
            inheritance = self._inheritance_factor_confirm_button(img,named_scene=True)
            if inheritance is not None:
                return self._tap_decision(
                    "inheritance_factor_confirm", "safe_advance.inheritance_factor_confirm",
                    inheritance, evidence,
                )
        if self._fan_goal_shortfall_warning_text_verified(screen_text):
            go_to_race = self._go_to_race_modal_button(evidence["perception"], img)
            if go_to_race is not None:
                return {"action": "stop", "reason": "fan_goal_warning_handoff",
                        "route_state": "modal", "evidence": evidence}
        if scenario == "larc":
            tutorial_text = str((screen_text or {}).get("normalized_text", ""))
            tutorial_lines = {line.strip() for line in tutorial_text.splitlines()}
            if {"新手教程", "凯旋门计划", "不听取说明"}.issubset(tutorial_lines):
                offer = self._grand_masters_tutorial_offer_evidence(img)
                if offer["candidate"]:
                    evidence["larc_tutorial"] = {**offer, "choice": "decline"}
                    decision = self._tap_decision("larc_tutorial_decline", "safe_advance.larc_tutorial_decline",
                                                  tuple(offer["bottom_point"]), evidence)
                    decision["route_state"] = "event_choice"
                    return decision
            if {"新手教程", "凯旋门计划", "不需要了"}.issubset(tutorial_lines):
                options = detect_event_option_layout(img)
                if options["option_count"] == 5 and self._grand_masters_tutorial_topic_menu_evidence(img).get("present"):
                    evidence["larc_tutorial"] = {"option_count": 5, "choice": "not_needed"}
                    decision = self._tap_decision("larc_tutorial_skip", "safe_advance.larc_tutorial_skip",
                                                  options["points"]["bottom"], evidence)
                    decision["route_state"] = "event_choice"
                    return decision
            lines = str((screen_text or {}).get("normalized_text", "")).splitlines()
            if {"赛事一览", "预测", "参赛", "参赛计划"}.issubset({line.strip() for line in lines}):
                return {"action": "stop", "reason": "race_list", "route_state": "race_list", "evidence": evidence}
            summary_text = "".join(lines)
            if (all(label in summary_text for label in ("目标剩余","回合","开始!"))
                    and any(re.fullmatch(r'\d{1,3}',line.strip()) for line in lines)
                    and any(target in summary_text for target in LARC_SYSTEM_GOAL_IDS)
                    and not any(label in summary_text for label in ("点击","继续","确定","取消","是否"))
                    and self._larc_goal_start_card_visible(img)):
                evidence["larc_goal_start"]={"verified":True,"stage":"animation","input_required":False}
                return {"action":"wait","reason":"larc_goal_start_animation","route_state":"loading","evidence":evidence}
            if (all(label in summary_text for label in ("目标剩余", "回合", "开始!", "点击"))
                    and re.search(r"获得[0-9,]*点协助者积分", summary_text)):
                # This is a free page advance. The supporter-goal label and
                # current card/click controls suffice; its number is not used
                # to select or record a local target.
                h, w = img.shape[:2]
                hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                card = hsv[int(h*.515):int(h*.588), int(w*.08):int(w*.92)]
                prompt = hsv[int(h*.856):int(h*.895), int(w*.43):int(w*.58)]
                if (h > w and card.size and prompt.size
                        and self._larc_goal_start_card_visible(img)
                        and np.mean((prompt[:, :, 1] < 60) & (prompt[:, :, 2] > 220)) > .08):
                    evidence["larc_goal_start"] = {"verified": True, "active_target": "supporter_points"}
                    return self._tap_decision("continue_goal_remaining_result", "safe_advance.goal_remaining_continue",
                                              (round(w*.5), round(h*.88)), evidence)
            remaining = re.search(r"距离下一个目标还剩(.*?)回合", summary_text)
            if remaining and "协助者积分" in summary_text and "目标达成数" in summary_text and "继续" in summary_text:
                point = self._goal_progress_list_continue_button(evidence["perception"], img, scenario="larc")
                if point is not None:
                    # The verified list and Continue control own navigation.
                    # An absent/malformed counter stays unknown in the report.
                    evidence["larc_goal_list"] = {
                        "remaining_turns": (int(remaining.group(1))
                            if re.fullmatch(r"\d+", remaining.group(1)) else None),
                        "listed_supporter_targets": [int(v) for v in re.findall(r"获得(\d+)点协助者积分", summary_text)],
                        "active_target": None,
                    }
                    return self._tap_decision("continue_goal_remaining_result", "safe_advance.goal_remaining_continue", point, evidence)
            if is_larc_race_day(img, evidence):
                evidence["larc_race_day"] = {"verified": True}
                adaptation_entry = plan_larc_adaptation_entry(img, evidence, larc_adaptation_session, race_day=True)
                if adaptation_entry is not None:
                    return adaptation_entry
                if allow_larc_first_arc_skill_inspection or allow_larc_final_arc_skill_inspection:
                    race_context = read_larc_race_day_context(
                        path, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        visible_text=str((screen_text or {}).get("normalized_text", "")),
                    )
                    evidence["larc_race_day_context"] = race_context
                    first_arc = (allow_larc_first_arc_skill_inspection
                        and race_context["period"]["value"] == {
                                "stage": "classic", "year_index": 2, "month": 10, "half": "first",
                            }
                        and race_context["target_id"] == "larc_arc_participation")
                    final_arc = (allow_larc_final_arc_skill_inspection
                        and race_context["period"]["value"] == {
                                "stage": "senior", "year_index": 3, "month": 10, "half": "first",
                            }
                        and race_context["target_id"] == "larc_final_arc_top5")
                    if race_context["status"] == "verified" and (first_arc or final_arc):
                        label, confidence = _CALENDAR_REGION_READER.read_line(img[1080:1130, 60:155])
                        evidence["larc_skill_entry"] = {"label": label, "confidence": confidence,
                                                         "bbox": [60, 1080, 155, 1130]}
                        if label == "技能" and confidence >= .95:
                            reason = "open_larc_first_arc_skills" if first_arc else "open_larc_final_arc_skills"
                            action_id = ("safe_advance.larc_first_arc_skill_open" if first_arc
                                         else "safe_advance.larc_final_arc_skill_open")
                            decision = self._tap_decision(reason, action_id, (108, 1095), evidence)
                            decision["route_state"] = "main_menu"
                            return decision
                return {"action": "stop", "reason": "race_day_main_menu", "route_state": "main_menu", "evidence": evidence}
            ss_preview = plan_larc_ss_preview(img, evidence, allow_execute=allow_larc_ss and not larc_ss_preview_only,
                                              larc_supporter_goal=larc_supporter_goal,
                                              preview_decider=larc_ss_preview_decider)
            if ss_preview is not None:
                if ss_preview["reason"] == "ready_larc_ss_preview" and allow_return_main_menu_from_training:
                    return self._configured_decision(
                        "return_main_menu_from_training", img, evidence,
                    ) or self._stop("training_menu_return_action_missing", evidence=evidence)
                return ss_preview
        return None

    def _plan_common_menus_and_dialogue(
        self,
        path: Path,
        img: np.ndarray,
        evidence: dict[str, Any],
        *,
        route_policy: SafeAdvanceRoutePolicy | None,
        scenario: str | None,
        allow_return_main_menu_from_training: bool,
        allow_larc_ss: bool,
        larc_ss_preview_only: bool,
        larc_supporter_goal: dict[str, Any] | None,
        larc_adaptation_session: LarcAdaptationSession | None,
    ) -> dict[str, Any]:
        screen_text = evidence.get('screen_text')
        rest = self._common_rest_confirmation_decision(evidence, evidence["perception"], img)
        if rest is not None:
            return rest
        training_check = TrainingMenuPrechecker(
            scenario="larc" if scenario == "larc" else "grand_masters",
        ).check(path)
        evidence["training_menu_precheck"] = {
            "accepted": training_check.accepted,
            "reason": training_check.reason,
            "selected_tab": training_check.selected_tab,
            "failures": list(training_check.failures),
        }
        if self._looks_like_post_action_connection_wait(
            ready_surface=training_check.accepted,
            screen_text=screen_text, route_policy=route_policy,
        ):
            return {"action": "wait", "reason": "loading",
                    "state": "connection_overlay_wait", "route_state": "connection_overlay",
                    "evidence": evidence}
        if self._looks_like_training_action_animation(
            img, route_policy=route_policy, training_check=training_check,
            perception=evidence["perception"], scenario=scenario,
        ):
            return {"action": "wait", "reason": "training_action_animation_wait",
                    "route_state": "loading", "evidence": evidence}
        story_single=single_story_event_option_point(img) if scenario=='larc' else None
        story_labels={line.strip() for line in str((screen_text or {}).get('normalized_text','')).splitlines()}
        if story_single is not None and story_labels.intersection({'主线事件','剧本事件'}):
            evidence['single_story_event']={'option_count':1,'heading_verified':True}
            decision=self._tap_decision('choose_single_event_option','safe_advance.single_event_option',story_single,evidence)
            decision['route_state']='event_choice'
            return decision
        if training_check.accepted:
            if scenario == "larc" and training_check.selected_tab != "ss":
                ss_entry = plan_larc_ss_entry(img, evidence, allow_execute=allow_larc_ss,
                                            larc_supporter_goal=larc_supporter_goal,
                                            preview_only=larc_ss_preview_only)
                if ss_entry is not None:
                    return ss_entry
            if scenario == "larc" and training_check.selected_tab == "ss":
                evidence["larc_ss"] = {"status": "partial", "stage": "preview",
                    "kind": None, "ready_count": None, "supporter_points_if_all_win": None}
            if allow_return_main_menu_from_training:
                return self._configured_decision(
                    "return_main_menu_from_training", img, evidence,
                ) or self._stop("training_menu_return_action_missing", evidence=evidence)
            reason = "ready_larc_ss_preview" if scenario == "larc" and training_check.selected_tab == "ss" else "ready_training_menu"
            decision = self._stop(reason, evidence=evidence)
            decision["route_state"] = "training_menu"
            return decision
        if ((self._looks_like_main_training_menu(img) and self._looks_like_training_home_controls(img))
                or scenario == "larc" and SafeAdvanceExecutor._career_home_controls_ready(img)):
            main_skip = self._common_bottom_skip_decision(evidence, img, event_rows_only=scenario == "larc")
            if main_skip is not None and main_skip.get("reason") == "ready_main_menu" and scenario == "larc":
                adaptation_entry = plan_larc_adaptation_entry(img, evidence, larc_adaptation_session)
                if adaptation_entry is not None:
                    return adaptation_entry
            if main_skip is not None:
                return main_skip
        if (event_option_rows_ready(img) if scenario == "larc" else detect_event_option_layout(img)["points"]):
            return handoff("event_choice", evidence=evidence)
        narration_text = str((screen_text or {}).get("normalized_text", ""))
        narration_compact = "".join(narration_text.split())
        larc_hud_dialogue = (
            all(term in narration_compact for term in ("目标", "体力", "缩短版", "记录"))
            and any(term in narration_compact for term in ("还剩", "比赛日"))
            and (
                "跳过" in narration_compact
                or SpeedTogglePlanner._bottom_skip_level(img) in {"off", "single", "double"}
            )
            and not self._looks_like_confirm_modal_panel(img)
            and not any(
                excl in narration_compact
                for excl in ("赛事一览", "赛事详情", "要参加比赛吗", "是否参加比赛", "重新挑战", "取消", "确定", "确认")
            )
        )
        if (
            scenario == "larc"
            and (
                any(title in narration_text for title in ("养成优俊少女事件", "协助卡事件", "剧本事件", "主线事件"))
                or larc_hud_dialogue
            )
            and (self._looks_like_large_bottom_dialogue(img) or (
                self._looks_like_blocking_bottom_dialogue(img)
                and self._looks_like_active_bottom_skip_button(img)
                and SpeedTogglePlanner._bottom_skip_state(img) == "on"
            ))
            and self._looks_like_dialogue_continue_arrow(img)
            and not self._looks_like_training_home_controls(img)
            and self._safe_green_confirm_button(evidence["perception"], img) is None
        ):
            return self._skip_bottom_dialogue(evidence, img)
        if not (
            route_policy is not None
            and route_policy.allows_observed_state("training_menu")
        ):
            result_summary = self._race_result_summary_continue_button(img, screen_text)
            if result_summary is not None:
                return self._tap_decision(
                    "continue_race_result",
                    "safe_advance.race_result_continue",
                    result_summary,
                    evidence,
                )
        opening_skip = self._common_bottom_skip_decision(evidence, img, event_rows_only=scenario == "larc")
        if opening_skip is not None:
            return opening_skip
        if (scenario == "larc" and "主线事件" in story_labels
                and re.search(r"代表交流赛第[0-9一二三四五六七八九十]+战赛后",
                              "".join(narration_text.split()))
                and not story_labels.intersection({"取消", "确认", "确定"})
                and self._looks_like_training_animation_controls(img)
                and not (screen_text or {}).get("dangerous")):
            # The exchange story heading can appear before any dialogue control.
            # Let the common route reread it rather than waking the race owner.
            evidence["loading_source"] = "larc_exchange_story_heading"
            return {"action": "wait", "reason": "loading",
                    "route_state": "loading", "evidence": evidence}
        if self._looks_like_blank_white_loading(img):
            return {"action": "wait", "reason": "loading",
                    "route_state": "blank_white_loading", "evidence": evidence}
        if (scenario == "larc" and larc_adaptation_session is not None
                and larc_adaptation_session.returned
                and route_policy is not None and route_policy.source == "pending"
                and route_policy.pending_action_id in {
                    "safe_advance.larc_adaptation_return", "wait:larc_adaptation_return_loading"}
                and set(route_policy.allowed_observed_states) == {"loading", "main_menu"}
                and self._looks_like_training_animation_controls(img)
                and not self._looks_like_confirm_modal_panel(img)
                and not self._looks_like_large_bottom_dialogue(img)):
            # Returning from the grid briefly leaves only the scene and bottom
            # controls. Stay in this return, without tapping or reopening it.
            return {"action": "wait", "reason": "larc_adaptation_return_loading",
                    "route_state": "loading", "evidence": evidence}
        return self._stop("common_career_page_unrecognized", evidence=evidence)

    def _common_shorten_settings_decision(
        self, img: np.ndarray, evidence: dict[str, Any], *, enable_skip_first: bool = False,
    ) -> dict[str, Any] | None:
        settings = self._shortened_event_settings_action(img)
        if settings is None:
            return None
        action, point = settings
        if action == "unread_unknown":
            return self._stop("shorten_unread_checkbox_unconfirmed", evidence=evidence)
        if enable_skip_first:
            level=SpeedTogglePlanner._bottom_skip_level(img)
            if level in {"off","single"}:
                decision=self._enable_bottom_skip_toggle(evidence,img)
                decision.update(reason="enable_shorten_settings_skip_toggle",route_state="modal")
                return decision
            if level!="double":
                return {"action":"wait","reason":"shorten_settings_skip_loading","route_state":"modal","evidence":evidence}
        reason, coordinate = {
            "select_all": ("select_shorten_all_events", "safe_advance.shorten_all_events"),
            "uncheck_unread": ("uncheck_do_not_shorten_unread", "safe_advance.shorten_unread_toggle"),
            "confirm": ("confirm_shorten_all_events", "safe_advance.shorten_events_confirm"),
        }[action]
        return self._tap_decision(reason, coordinate, point, evidence)

    @staticmethod
    def _larc_goal_start_card_visible(img: np.ndarray) -> bool:
        # Reuse the established goal-start white banner geometry. Text binds
        # its goal identity; this helper never grants a click on "开始".
        h,w=img.shape[:2]
        if h<=w:return False
        hsv=cv2.cvtColor(img,cv2.COLOR_BGR2HSV)
        card=hsv[int(h*.515):int(h*.588),int(w*.08):int(w*.92)]
        return bool(card.size and np.mean((card[:,:,1]<60)&(card[:,:,2]>210))>.65)

    def _common_bottom_skip_decision(
        self, evidence: dict[str, Any], img: np.ndarray, *, event_rows_only: bool = False,
    ) -> dict[str, Any] | None:
        """Apply the bounded skip policy to a verified career surface."""
        main_menu = (self._looks_like_main_training_menu(img)
                     and self._looks_like_training_home_controls(img))
        if event_rows_only and not main_menu:
            # Gold overseas stats are not a different owner. Reuse the same
            # home/control anchors and modal/event exclusions as common dispatch.
            main_menu = SafeAdvanceExecutor._career_home_controls_ready(img)
        if (
            ((event_option_rows_ready(img) if event_rows_only else detect_event_option_layout(img)["points"])
             and not main_menu)
            or self._looks_like_confirm_modal_panel(img)
            or (self._looks_like_story_dialogue_with_record_controls(img) and not main_menu)
            or SpeedTogglePlanner._bottom_skip_level(img) not in {"off", "single", "double"}
        ):
            return None
        if not (
            main_menu
            or self._looks_like_large_bottom_dialogue(img)
        ):
            return None
        if main_menu:
            if SpeedTogglePlanner._bottom_skip_level(img) == "double":
                decision = handoff("ready_main_menu", evidence=evidence)
                decision["route_state"] = "main_menu"
                return decision
            decision = self._skip_bottom_dialogue(evidence, img)
            decision["route_state"] = "main_menu"
            return decision
        return self._skip_bottom_dialogue(evidence, img)

    def _common_rest_confirmation_decision(
        self, evidence: dict[str, Any], perception: dict[str, Any], img: np.ndarray,
    ) -> dict[str, Any] | None:
        point = self._rest_outing_confirm_button(evidence, perception, img)
        if point is not None:
            return self._tap_decision(
                "confirm_rest_outing", "safe_advance.rest_outing_confirm", point, evidence,
            )
        point = self._rest_confirm_button(evidence, perception, img)
        if point is not None:
            return self._tap_decision(
                "confirm_rest", "safe_advance.rest_confirm", point, evidence,
            )
        return None

    @staticmethod
    def _fan_goal_shortfall_warning_text_verified(screen_text: dict[str, Any] | None) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        return (
            (
                "粉丝数未达到目标赛事要求" in normalized
                or "目标粉丝数不足" in normalized
            )
            and (
                "距离目标赛事剩余" in normalized
                or (
                    "距离目标期限还剩" in normalized
                    and "回合" in normalized
                )
            )
            and "通过参赛获得粉丝吧" in normalized
            and "前往赛事" in normalized
            and "赛事详情" not in normalized
            and "要参加比赛吗" not in normalized
        )

    @staticmethod
    def _go_to_race_modal_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.23) : int(height * 0.31), int(width * 0.01) : int(width * 0.99)]
        warning = hsv[int(height * 0.31) : int(height * 0.40), int(width * 0.05) : int(width * 0.95)]
        if header.size == 0 or warning.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        warning_red_orange = np.mean(
            (
                ((warning[:, :, 0] <= 12) | ((warning[:, :, 0] >= 170) & (warning[:, :, 0] <= 179)))
                | ((warning[:, :, 0] >= 5) & (warning[:, :, 0] <= 30))
            )
            & (warning[:, :, 1] > 70)
            & (warning[:, :, 2] > 100)
        )
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.45 <= cx <= width * 0.85):
                continue
            if not (height * 0.66 <= cy <= height * 0.76):
                continue
            if not (width * 0.30 <= w <= width * 0.55):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if header_green > 0.30 and warning_red_orange > 0.04 and len(candidates) == 1:
            _area, x, y = candidates[0]
            return x, y
        return None

    @staticmethod
    def _shortened_unread_checkbox_state(img: np.ndarray) -> str:
        """Classify the unread-event checkbox without treating disabled gray as opt-out."""
        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        checkbox = hsv[800:855, 200:250]
        if checkbox.size == 0:
            return "unknown"
        green = (
            (checkbox[:, :, 0] >= 38)
            & (checkbox[:, :, 0] <= 92)
            & (checkbox[:, :, 1] > 60)
            & (checkbox[:, :, 2] > 80)
        )
        active_green = (
            (checkbox[:, :, 0] >= 38)
            & (checkbox[:, :, 0] <= 92)
            & (checkbox[:, :, 1] > 100)
            & (checkbox[:, :, 2] > 170)
        )
        bright_background = float(np.mean(checkbox[:, :, 2] > 200))
        if bright_background >= 0.65:
            if float(np.mean(active_green)) >= 0.05:
                return "checked_green"
            gray_check = (checkbox[:, :, 1] < 60) & (checkbox[:, :, 2] >= 120) & (checkbox[:, :, 2] < 235)
            return "unchecked_gray" if float(np.mean(gray_check)) >= 0.05 else "unknown"
        if bright_background <= 0.20 and float(np.mean(green)) >= 0.05:
            return "disabled_gray"
        return "unknown"

    @staticmethod
    def _shortened_event_settings_action(
        img: np.ndarray,
    ) -> tuple[str, tuple[int, int]] | None:
        height, width = img.shape[:2]
        if width > height:
            return None

        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)

        def ratio(box: tuple[int, int, int, int], mask: np.ndarray) -> float:
            x1, y1, x2, y2 = box
            return float(np.mean(mask[y1:y2, x1:x2]))

        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        white = (hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 215)
        dark = hsv[:, :, 2] < 35
        if (
            ratio((0, 0, 720, 280), dark) < 0.90
            or ratio((8, 295, 712, 360), green) < 0.70
            or ratio((8, 360, 712, 985), white) < 0.75
            or ratio((210, 875, 510, 965), green) < 0.55
        ):
            return None

        radio_centers = (493, 579, 665, 751)
        radio_green = [
            ratio((38, center_y - 28, 92, center_y + 28), green)
            for center_y in radio_centers
        ]
        selected_index = int(np.argmax(radio_green))
        if radio_green[selected_index] < 0.15:
            return None

        scale_x = width / 720.0
        scale_y = height / 1280.0
        if selected_index != 1:
            point = (int(round(360 * scale_x)), int(round(579 * scale_y)))
            return "select_all", point

        unread_point = (int(round(225 * scale_x)), int(round(827 * scale_y)))
        unread_state = SafeAdvancePlanner._shortened_unread_checkbox_state(img)
        if unread_state == "checked_green":
            return "uncheck_unread", unread_point
        if unread_state == "unchecked_gray":
            point = (int(round(360 * scale_x)), int(round(918 * scale_y)))
            return "confirm", point
        return "unread_unknown", unread_point
