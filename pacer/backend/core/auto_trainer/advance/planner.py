"""SafeAdvancePlanner orchestrator composed from classifier, transition, and handler mixins."""
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

from core.frame_io import io_path, read_image_with_retry
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
from core.auto_trainer.transition_wait import (
    TransitionBudget,
    career_comic_loading_visible,
    connecting_overlay_visible,
    evaluate_gentle_transition_wait,
)
from ._compat import (
    FrameSafetyAnalyzer,
    Perception,
    RaceRoutePlanner,
    SafeAdvanceExecutor,
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
from .constants import (
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
from .classifier import SafeAdvanceClassifierMixin
from .handlers import (
    DialogueAndEventsHandlerMixin,
    LarcCareerHandlerMixin,
    ModalsAndMechanicsHandlerMixin,
    SettlementHandlerMixin,
    SetupAndHomeHandlerMixin,
)
from .transition_policy import SafeAdvanceTransitionMixin


@dataclass
class _AdvancePlanState:
    training_check: TrainingMenuPrecheckResult | None = None
    atomic_main_menu_identified: bool = False
    atomic_ready_main_menu: bool = False
    main_menu_idle_hud_ready: bool = False
    crystal_reward: dict[str, Any] | None = None


class SafeAdvancePlanner(
    SafeAdvanceClassifierMixin,
    SafeAdvanceTransitionMixin,
    LarcCareerHandlerMixin,
    SetupAndHomeHandlerMixin,
    DialogueAndEventsHandlerMixin,
    ModalsAndMechanicsHandlerMixin,
    SettlementHandlerMixin,
):
    """Plan one conservative non-training-page advancement action."""

    def __init__(
        self,
        action_table: ActionTable | None = None,
        screen_graph: ScreenGraph | None = None,
        skill_evolution_choice: str = "evolve",
    ):
        self.action_table = action_table or ActionTable()
        self.screen_graph = screen_graph or ScreenGraph.load()
        if skill_evolution_choice not in {"evolve", "no_evolve"}:
            raise ValueError("skill_evolution_choice_invalid")
        self.skill_evolution_choice = skill_evolution_choice
        self._pending_skill_evolution_confirm = False
        self._default_gentle_budget = TransitionBudget()

    def plan(
        self,
        image_path: str | Path,
        *,
        visible_text: str | None = None,
        scenario: str | None = 'grand_masters',
        route_policy: SafeAdvanceRoutePolicy | None = None,
        allow_return_main_menu_from_training: bool = False,
        allow_larc_ss: bool = False,
        allow_larc_first_arc_skill_inspection: bool = False,
        allow_larc_final_arc_skill_inspection: bool = False,
        larc_ss_preview_only: bool = False,
        larc_ss_preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        larc_supporter_goal: dict[str, Any] | None = None,
        larc_adaptation_session: LarcAdaptationSession | None = None,
        allow_enter_training_from_global_home: bool = False,
        allow_resume_after_skill_handoff: bool = False,
        allow_career_end_finish_after_skill_handoff: bool = False,
        allow_finish_training_with_unused_skill_points: bool = False,
        allow_recruit_return_home: bool = False,
        prefer_scenario_hold: bool = False,
        authenticated_scenario_select: bool = False,
        career_goal_observed_turn: int | None = None,
        career_goal_text_order_verified: bool = False,
        persisted_career_goal_observation_sha256: str | None = None,
        persisted_career_goal_ledger_revision: int | None = None,
        allow_gentle_transition_wait: bool = False,
        gentle_transition_budget: TransitionBudget | None = None,
    ) -> dict[str, Any]:
        path = Path(image_path)
        img = read_image_with_retry(path)
        if img is None:
            return self._stop("image_read", image_path=str(path))

        image_perception = Perception().analyze_image(img)
        image_screen = str(image_perception.get("screen", "unknown"))
        perception = image_perception
        screen_text = analyze_screen_text(visible_text) if visible_text is not None else None
        if screen_text is not None:
            perception = fuse_perception_with_text(perception, screen_text)
        landscape_kind = classify_landscape_race_screen(img)
        if landscape_kind is not None:
            perception["landscape_race_kind"] = landscape_kind
        screen = str(perception.get("screen", "unknown"))
        evidence: dict[str, Any] = {"image": str(path), "perception": perception}
        training_check: TrainingMenuPrecheckResult | None = None
        if screen_text is not None:
            evidence["screen_text"] = screen_text
        state = _AdvancePlanState(training_check=training_check)
        decision = self._plan_core(
            state=state,
            path=path,
            img=img,
            image_perception=image_perception,
            image_screen=image_screen,
            perception=perception,
            screen_text=screen_text,
            screen=screen,
            evidence=evidence,
            visible_text=visible_text,
            scenario=scenario,
            route_policy=route_policy,
            allow_return_main_menu_from_training=allow_return_main_menu_from_training,
            allow_larc_ss=allow_larc_ss,
            allow_larc_first_arc_skill_inspection=allow_larc_first_arc_skill_inspection,
            allow_larc_final_arc_skill_inspection=allow_larc_final_arc_skill_inspection,
            larc_ss_preview_only=larc_ss_preview_only,
            larc_ss_preview_decider=larc_ss_preview_decider,
            larc_supporter_goal=larc_supporter_goal,
            larc_adaptation_session=larc_adaptation_session,
            allow_enter_training_from_global_home=allow_enter_training_from_global_home,
            allow_resume_after_skill_handoff=allow_resume_after_skill_handoff,
            allow_career_end_finish_after_skill_handoff=allow_career_end_finish_after_skill_handoff,
            allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
            allow_recruit_return_home=allow_recruit_return_home,
            prefer_scenario_hold=prefer_scenario_hold,
            authenticated_scenario_select=authenticated_scenario_select,
            career_goal_observed_turn=career_goal_observed_turn,
            career_goal_text_order_verified=career_goal_text_order_verified,
            persisted_career_goal_observation_sha256=persisted_career_goal_observation_sha256,
            persisted_career_goal_ledger_revision=persisted_career_goal_ledger_revision,
        )
        if allow_gentle_transition_wait or gentle_transition_budget is not None:
            effective_budget = (
                gentle_transition_budget
                if gentle_transition_budget is not None
                else self._default_gentle_budget
            )
            decision = evaluate_gentle_transition_wait(
                decision,
                img,
                budget=effective_budget,
                visible_text=visible_text,
                owner='safe_advance',
            )
            if scenario == 'larc':
                decision = annotate_larc_page(decision, route_policy)
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
        return decision

    def _plan_core(
        self,
        *,
        state: _AdvancePlanState,
        path: Path,
        img: np.ndarray,
        image_perception: dict[str, Any],
        image_screen: str,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        screen: str,
        evidence: dict[str, Any],
        visible_text: str | None,
        scenario: str | None,
        route_policy: SafeAdvanceRoutePolicy | None,
        allow_return_main_menu_from_training: bool,
        allow_larc_ss: bool,
        allow_larc_first_arc_skill_inspection: bool,
        allow_larc_final_arc_skill_inspection: bool,
        larc_ss_preview_only: bool,
        larc_ss_preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None,
        larc_supporter_goal: dict[str, Any] | None,
        larc_adaptation_session: LarcAdaptationSession | None,
        allow_enter_training_from_global_home: bool,
        allow_resume_after_skill_handoff: bool,
        allow_career_end_finish_after_skill_handoff: bool,
        allow_finish_training_with_unused_skill_points: bool,
        allow_recruit_return_home: bool,
        prefer_scenario_hold: bool,
        authenticated_scenario_select: bool,
        career_goal_observed_turn: int | None,
        career_goal_text_order_verified: bool,
        persisted_career_goal_observation_sha256: str | None,
        persisted_career_goal_ledger_revision: int | None,
    ) -> dict[str, Any]:
        for step in (
            lambda: self._plan_stage_larc_and_common_career(
                path=path,
                img=img,
                image_perception=image_perception,
                perception=perception,
                screen_text=screen_text,
                evidence=evidence,
                scenario=scenario,
                route_policy=route_policy,
                allow_return_main_menu_from_training=allow_return_main_menu_from_training,
                allow_larc_ss=allow_larc_ss,
                allow_larc_first_arc_skill_inspection=allow_larc_first_arc_skill_inspection,
                allow_larc_final_arc_skill_inspection=allow_larc_final_arc_skill_inspection,
                larc_ss_preview_only=larc_ss_preview_only,
                larc_ss_preview_decider=larc_ss_preview_decider,
                larc_supporter_goal=larc_supporter_goal,
                larc_adaptation_session=larc_adaptation_session,
                allow_enter_training_from_global_home=allow_enter_training_from_global_home,
                allow_career_end_finish_after_skill_handoff=allow_career_end_finish_after_skill_handoff,
                allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
            ),
            lambda: self._plan_stage_early_modals_and_safety_text(
                path=path,
                img=img,
                image_perception=image_perception,
                perception=perception,
                screen_text=screen_text,
                evidence=evidence,
                visible_text=visible_text,
                scenario=scenario,
                route_policy=route_policy,
                allow_enter_training_from_global_home=allow_enter_training_from_global_home,
                allow_recruit_return_home=allow_recruit_return_home,
                allow_resume_after_skill_handoff=allow_resume_after_skill_handoff,
                allow_return_main_menu_from_training=allow_return_main_menu_from_training,
                authenticated_scenario_select=authenticated_scenario_select,
                career_goal_observed_turn=career_goal_observed_turn,
                career_goal_text_order_verified=career_goal_text_order_verified,
                persisted_career_goal_observation_sha256=persisted_career_goal_observation_sha256,
                persisted_career_goal_ledger_revision=persisted_career_goal_ledger_revision,
            ),
            lambda: self._plan_stage_settlement_and_wisdom_checks(
                state=state,
                path=path,
                img=img,
                image_perception=image_perception,
                image_screen=image_screen,
                perception=perception,
                screen_text=screen_text,
                evidence=evidence,
                route_policy=route_policy,
                allow_enter_training_from_global_home=allow_enter_training_from_global_home,
                allow_career_end_finish_after_skill_handoff=allow_career_end_finish_after_skill_handoff,
            ),
            lambda: self._plan_stage_atomic_home_and_gm_mechanics(
                state=state,
                path=path,
                img=img,
                perception=perception,
                screen_text=screen_text,
                evidence=evidence,
                route_policy=route_policy,
                prefer_scenario_hold=prefer_scenario_hold,
                scenario=scenario,
            ),
            lambda: self._plan_stage_screen_graph_setup_and_race_stops(
                state=state,
                path=path,
                img=img,
                perception=perception,
                screen_text=screen_text,
                screen=screen,
                evidence=evidence,
                route_policy=route_policy,
                allow_return_main_menu_from_training=allow_return_main_menu_from_training,
                allow_resume_after_skill_handoff=allow_resume_after_skill_handoff,
            ),
        ):
            res = step()
            if res is not None:
                return res
        return self._plan_stage_dialogues_transitions_and_fallbacks(
            state=state,
            img=img,
            image_screen=image_screen,
            perception=perception,
            screen_text=screen_text,
            screen=screen,
            evidence=evidence,
            route_policy=route_policy,
            allow_resume_after_skill_handoff=allow_resume_after_skill_handoff,
            allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
        )

    def _plan_stage_larc_and_common_career(
        self,
        *,
        path: Path,
        img: np.ndarray,
        image_perception: dict[str, Any],
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        evidence: dict[str, Any],
        scenario: str | None,
        route_policy: SafeAdvanceRoutePolicy | None,
        allow_return_main_menu_from_training: bool,
        allow_larc_ss: bool,
        allow_larc_first_arc_skill_inspection: bool,
        allow_larc_final_arc_skill_inspection: bool,
        larc_ss_preview_only: bool,
        larc_ss_preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None,
        larc_supporter_goal: dict[str, Any] | None,
        larc_adaptation_session: LarcAdaptationSession | None,
        allow_enter_training_from_global_home: bool,
        allow_career_end_finish_after_skill_handoff: bool,
        allow_finish_training_with_unused_skill_points: bool,
    ) -> dict[str, Any] | None:
        if scenario == "larc":
            from core.auto_trainer.transition_wait import career_comic_loading_visible
            if career_comic_loading_visible(img):
                evidence["loading_source"] = "career_comic_visual"
                return {"action":"wait", "reason":"loading", "route_state":"loading", "evidence":evidence}
        if larc_adaptation_session is not None and scenario != "larc":
            return self._stop("larc_adaptation_scenario_mismatch", evidence=evidence)
        if scenario == "larc" and screen_text is not None:
            lines = str(screen_text.get("normalized_text", "")).strip().splitlines()
            if lines and lines[0] == "公告" and lines[-1] == "关闭":
                point = self._announcement_close_button(img)
                if point is not None:
                    return self._tap_decision("close_announcement", "safe_advance.announcement_close", point, evidence)
            reward = self._received_login_reward_skip_point(img, perception, screen_text)
            if reward is not None:
                return self._tap_decision("skip_received_login_reward", "safe_advance.bottom_right_skip", reward, evidence)
        if scenario == "larc" and screen_text is not None and not screen_text.get("dangerous"):
            text = "".join(str(screen_text.get("normalized_text", "")).split())
            disconnect = self._connection_conflict_return_title_button(img)
            if disconnect is not None:
                if ("连接已断开" in text and "返回标题页面" in text
                        and "长时间内未进行操作" in text and "与服务器的连接发生中断" in text
                        and not any(term in text for term in ("其他设备", "别的设备", "其他终端", "账号"))):
                    return self._tap_decision("return_title_from_connection_conflict",
                        "safe_advance.return_title", disconnect, evidence)
                return self._stop("connection_conflict", evidence=evidence)
            title = self._current_cn_title_start_point(img)
            if title is None and (screen_text.get("primary_hint") or {}).get("screen") == "title_screen":
                title = self._cn_title_start_point(img)
            if title is not None:
                return self._tap_decision("tap_title_start", "safe_advance.title_start", title, evidence)
            if self._continue_training_modal_text_verified(screen_text):
                point = self._continue_training_modal_button(image_perception, img)
                if point is not None:
                    return self._tap_decision("continue_training_modal", "safe_advance.continue_training", point, evidence)
        larc_career_home = bool(
            scenario == "larc"
            and self._looks_like_training_home_controls(img)
            and screen_text is not None
            and ("训练/群星赛" in str(screen_text.get("normalized_text", ""))
                 or all(term in str(screen_text.get("normalized_text", ""))
                        for term in ("海外远征中", "海外适应性", "训练")))
        )
        career_text = str((screen_text or {}).get("normalized_text", ""))
        if scenario == "larc" and not (screen_text or {}).get("dangerous"):
            if (larc_adaptation_session is not None
                    and larc_adaptation_session.stage == "awaiting_result"
                    and larc_adaptation_session.submitted_source_sha256 is not None
                    and "海外适应性等级提升" in career_text):
                # This is the already submitted upgrade's animation, not a
                # goal result. Keep it with its transaction owner, read-only.
                decision = plan_larc_adaptation(img, evidence, session=larc_adaptation_session)
                if decision is not None:
                    return annotate_larc_page(decision, route_policy)
            from core.auto_trainer.transition_wait import connecting_overlay_visible
            if connecting_overlay_visible(img):
                evidence["loading_source"] = "career_connecting"
                return {"action": "wait", "reason": "loading", "route_state": "loading", "evidence": evidence}
            skip_lines = set(str((screen_text or {}).get("normalized_text", "")).splitlines())
            if ({"跳过确认", "跳过该场景", "取消", "跳过"}.issubset(skip_lines)
                    and not (screen_text or {}).get("dangerous")):
                confirm = self._safe_green_confirm_button(perception, img, allow_obscured_background=True)
                if confirm is not None:
                    decision = self._tap_decision("confirm_larc_opening_skip",
                        "safe_advance.larc_opening_skip_confirm", confirm, evidence)
                    decision["route_state"] = "modal"
                    return annotate_larc_page(decision, route_policy)
            if (route_policy is not None and (route_policy.opening_context
                    or route_policy.pending_action_id in {"safe_advance.larc_opening_movie_skip","wait:larc_opening_movie_pending"})):
                movie=self._career_opening_movie_skip_button(img)
                if movie is not None:
                    if route_policy.pending_action_id in {"safe_advance.larc_opening_movie_skip","wait:larc_opening_movie_pending"}:
                        return {"action":"wait","reason":"larc_opening_movie_pending","route_state":"dialogue","evidence":evidence}
                    return self._tap_decision("skip_larc_opening_movie","safe_advance.larc_opening_movie_skip",movie,evidence)
        if (scenario == "larc" and route_policy is not None
                and route_policy.source == "pending" and route_policy.pending_action_id == "training.execute"
                and route_policy.action_source_sha256 and route_policy.allows_observed_state("loading")
                and all(label in career_text for label in ("训练", "还剩", "触发"))
                and not (screen_text or {}).get("dangerous")):
            return {"action": "wait", "reason": "training_action_animation_wait",
                    "route_state": "loading", "evidence": evidence}
        # The blue/white home-nav heuristic also matches career animations and
        # costumes. Shared career HUD evidence outranks that coarse colour guess;
        # do not enumerate event/result types here. The common planner still
        # decides the actual page/action, so this is not a main-menu shortcut.
        larc_career_hud = (scenario == "larc"
            and all(label in career_text for label in ("目标", "体力"))
            and any(label in career_text for label in ("还剩", "比赛日")))
        passive_home_resource_text = bool(
            screen_text and screen_text.get("resource_terms")
            and set(screen_text["resource_terms"]).issubset(_GLOBAL_HOME_PASSIVE_RESOURCE_TERMS)
        )
        if (scenario == "larc" and (screen_text is None or not screen_text.get("dangerous")
                                    or passive_home_resource_text)
                and not larc_career_home and not larc_career_hud
                and self.is_post_career_global_home(img)
                and self._goal_achieved_continue_button(perception, img, goal_layout_only=True) is None
                and not self._looks_like_goal_achievement_animation(perception, img, goal_layout_only=True)
                and rating_continue_point(img) is None):
            if allow_enter_training_from_global_home:
                point = self._game_home_training_button(perception, img)
                if point is not None:
                    return self._tap_decision("enter_training_from_home", "safe_advance.home_training", point, evidence)
            return self._stop("global_home", evidence=evidence)
        if scenario != "grand_masters":
            decision = self._plan_common_career_page(
                path, img, evidence, route_policy=route_policy,
                scenario=scenario,
                allow_return_main_menu_from_training=allow_return_main_menu_from_training,
                allow_larc_ss=allow_larc_ss,
                allow_larc_first_arc_skill_inspection=allow_larc_first_arc_skill_inspection,
                allow_larc_final_arc_skill_inspection=allow_larc_final_arc_skill_inspection,
                larc_ss_preview_only=larc_ss_preview_only,
                larc_ss_preview_decider=larc_ss_preview_decider,
                larc_supporter_goal=larc_supporter_goal,
                larc_adaptation_session=larc_adaptation_session,
                allow_career_end_finish_after_skill_handoff=allow_career_end_finish_after_skill_handoff,
                allow_finish_training_with_unused_skill_points=allow_finish_training_with_unused_skill_points,
            )
            return annotate_larc_page(decision, route_policy) if scenario == "larc" else decision
        return None

    def _plan_stage_early_modals_and_safety_text(
        self,
        *,
        path: Path,
        img: np.ndarray,
        image_perception: dict[str, Any],
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        evidence: dict[str, Any],
        visible_text: str | None,
        scenario: str | None,
        route_policy: SafeAdvanceRoutePolicy | None,
        allow_enter_training_from_global_home: bool,
        allow_recruit_return_home: bool,
        allow_resume_after_skill_handoff: bool,
        allow_return_main_menu_from_training: bool,
        authenticated_scenario_select: bool,
        career_goal_observed_turn: int | None,
        career_goal_text_order_verified: bool,
        persisted_career_goal_observation_sha256: str | None,
        persisted_career_goal_ledger_revision: int | None,
    ) -> dict[str, Any] | None:
        if screen_text is not None:
            goddess_wisdom_confirm = self._goddess_wisdom_acquisition_confirm_button(
                evidence,
                perception,
                img,
            )
            if goddess_wisdom_confirm is not None:
                return self._tap_decision(
                    "confirm_wisdom_learning_prompt",
                    "safe_advance.wisdom_learning_prompt_confirm",
                    goddess_wisdom_confirm,
                    evidence,
                )
        inheritance_confirm = self._inheritance_factor_result_confirm_button(
            screen_text,
            img,
        )
        if (
            inheritance_confirm is not None
            and route_policy is not None
            and route_policy.source == "pending"
            and route_policy.pending_action_id
            == "safe_advance.confirm_inheritance_factors"
            and route_policy.allows_observed_state("dialogue")
            and self._inheritance_factor_second_confirmation_text_verified(
                screen_text
            )
        ):
            return self._tap_decision(
                "confirm_inheritance_factors",
                "safe_advance.confirm_inheritance_factors",
                inheritance_confirm,
                evidence,
            )
        if self._looks_like_gur_race_confirmation_text(screen_text):
            return self._stop(
                "race_detail",
                route_state="race_detail_confirm",
                evidence=evidence,
            )
        if self._looks_like_wbc_race_confirmation(screen_text, img):
            return self._stop(
                "race_detail",
                route_state="race_detail_confirm",
                evidence=evidence,
            )
        pre_resource = self._plan_pre_resource_race_confirmation(
            img,
            screen_text,
            scenario or "",
            evidence,
        )
        if pre_resource is not None:
            return pre_resource
        # A final confirmation modal can be visually identified even when the
        # OCR fallback returns mojibake.  Stop before any generic green-button
        # route so an unreadable resource-spend confirmation can never be
        # mistaken for the support-deck start action.
        if (
            (screen_text is None or not bool(screen_text.get("dangerous")))
            and self._looks_like_unreadable_resource_modal(img)
        ):
            return self._stop("resource_prompt_unreadable", evidence=evidence)
        if self._continue_training_modal_text_verified(screen_text):
            continue_training = self._continue_training_modal_button(
                image_perception,
                img,
            )
            if continue_training is not None:
                return self._tap_decision(
                    "continue_training_modal",
                    "safe_advance.continue_training",
                    continue_training,
                    evidence,
                )
        clock_cancel = self._clock_retry_cancel_decision(img, screen_text, evidence)
        if clock_cancel is not None:
            return clock_cancel
        final_loss_next = self._final_loss_result_next_button(img, perception, screen_text, route_policy)
        if final_loss_next is not None:
            return self._tap_decision("continue_final_loss_result", "safe_advance.final_loss_result_continue",
                final_loss_next, evidence)
        shortened_event_settings = self._common_shorten_settings_decision(img, evidence, enable_skip_first=scenario == "larc")
        if shortened_event_settings is not None:
            return shortened_event_settings
        go_to_race = self._go_to_race_modal_button(perception, img)
        if go_to_race is not None:
            if not self._fan_goal_shortfall_warning_text_verified(screen_text):
                return self._stop("fan_goal_warning_text_unverified", evidence=evidence)
            return self._tap_decision(
                "go_to_race_modal",
                "safe_advance.go_to_race",
                go_to_race,
                evidence,
            )
        result_summary = None
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
        if screen_text is not None:
            evidence["screen_text"] = screen_text
            if allow_recruit_return_home:
                recruit_home = self._recruit_return_home_point(
                    path,
                    img,
                    screen_text,
                )
                if recruit_home is not None:
                    return self._tap_decision(
                        "return_home_from_recruit",
                        "safe_advance.recruit_return_home",
                        recruit_home,
                        evidence,
                    )
            received_reward_skip = self._received_login_reward_skip_point(
                img,
                perception,
                screen_text,
            )
            if received_reward_skip is not None:
                return self._tap_decision(
                    "skip_received_login_reward",
                    "safe_advance.bottom_right_skip",
                    received_reward_skip,
                    evidence,
                )
            if screen_text.get("dangerous"):
                resource_terms = {
                    str(term)
                    for term in screen_text.get("resource_terms", [])
                    if isinstance(term, str)
                }
                carrot_event_narrative = is_carrot_event_narrative(
                    str(screen_text.get("visible_text", "")),
                    tuple(resource_terms),
                )
                if (
                    carrot_event_narrative
                    and route_policy is not None
                    and route_policy.source == "pending"
                    and str(route_policy.pending_action_id or "").startswith(
                        "event_choice."
                    )
                    and route_policy.allows_observed_state("dialogue")
                    and self._looks_like_large_bottom_dialogue(img)
                    and self._looks_like_dialogue_continue_arrow(img)
                    and SpeedTogglePlanner._bottom_skip_level(img) == "double"
                ):
                    evidence["carrot_event_followup_scope"] = {
                        "pending_action_id": route_policy.pending_action_id,
                        "resource_terms": sorted(resource_terms),
                    }
                    return self._skip_bottom_dialogue(evidence, img)
                if (
                    detect_event_option_layout(img)["points"]
                    and carrot_event_narrative
                ):
                    return handoff(
                        "event_choice",
                        route_state="event_choice",
                        evidence=evidence,
                    )
                if (
                    resource_terms
                    and resource_terms.issubset(_GLOBAL_HOME_PASSIVE_RESOURCE_TERMS)
                    and self.is_post_career_global_home(img)
                ):
                    greeting_dialogue = self._global_home_greeting_dialogue_button(
                        screen_text,
                        img,
                    )
                    if greeting_dialogue is not None:
                        return self._tap_decision(
                            "dismiss_global_home_greeting",
                            "safe_advance.global_home_dialogue",
                            greeting_dialogue,
                            evidence,
                        )
                    if allow_enter_training_from_global_home:
                        home_training = self._game_home_training_button(
                            image_perception,
                            img,
                        )
                        if home_training is not None:
                            return self._tap_decision(
                                "enter_training_from_home",
                                "safe_advance.home_training",
                                home_training,
                                evidence,
                            )
                    return self._stop("global_home", evidence=evidence)
                announcement_close = self._announcement_close_button(img)
                if announcement_close is not None:
                    return self._tap_decision(
                        "close_announcement",
                        "safe_advance.announcement_close",
                        announcement_close,
                        evidence,
                    )
                trophy_reward_close = self._trophy_reward_close_button(img)
                if trophy_reward_close is not None:
                    return self._tap_decision(
                        "close_trophy_reward",
                        "safe_advance.trophy_reward_close",
                        trophy_reward_close,
                        evidence,
                    )
                final_loss_recovery = self._final_loss_recovery_decision(
                    img,
                    evidence,
                )
                if (
                    isinstance(final_loss_recovery, dict)
                    and final_loss_recovery.get("reason") == "cancel_clock_retry"
                ):
                    return final_loss_recovery
                training_pass_close = self._training_pass_reset_close_button(
                    screen_text,
                    img,
                )
                if training_pass_close is not None:
                    return self._tap_decision(
                        "close_training_pass_reset",
                        "safe_advance.training_pass_reset_close",
                        training_pass_close,
                        evidence,
                    )
                return self._stop("resource_prompt", evidence=evidence)

            normalized_text = "".join(
                str(screen_text.get("normalized_text", "")).split()
            )
            if (
                "\u65e5\u671f\u53d8\u5316" in normalized_text
                or "\u65e5\u671f\u53d1\u751f\u4e86\u53d8\u5316" in normalized_text
            ):
                date_change_confirm = self._safe_green_confirm_button(
                    perception,
                    img,
                    allow_obscured_background=True,
                )
                if date_change_confirm is not None:
                    return self._tap_decision(
                        "date_change_confirm",
                        "safe_advance.date_change_confirm",
                        date_change_confirm,
                        evidence,
                    )
            additional_data_download = self._additional_data_download_confirm_button(
                evidence,
                perception,
                img,
            )
            if additional_data_download is not None:
                return self._tap_decision(
                    "confirm_additional_data_download",
                    "safe_advance.additional_data_download_confirm",
                    additional_data_download,
                    evidence,
                )
        if (
            route_policy is not None
            and route_policy.allows_observed_state("training_menu")
        ):
            training_check = TrainingMenuPrechecker().check(path)
            evidence["training_menu_precheck"] = {
                "accepted": training_check.accepted,
                "reason": training_check.reason,
                "selected_tab": training_check.selected_tab,
                "failures": list(training_check.failures),
                "evidence": training_check.evidence,
            }
            if training_check.accepted:
                if allow_return_main_menu_from_training:
                    return self._configured_decision(
                        "return_main_menu_from_training", img, evidence
                    ) or self._stop(
                        "training_menu_return_action_missing", evidence=evidence
                    )
                decision = handoff("ready_training_menu", evidence=evidence)
                decision["route_state"] = "training_menu"
                return decision
        if career_goal_observed_turn is not None:
            try:
                goal_observation = perceive_goal_page(
                    path,
                    visible_text=visible_text or "",
                    source_frame_sha256=hashlib.sha256(io_path(path).read_bytes()).hexdigest(),
                    observed_turn=career_goal_observed_turn,
                    text_order_verified=career_goal_text_order_verified,
                )
            except CareerGoalPerceptionError as exc:
                return self._stop(
                    "career_goal_observation_invalid",
                    evidence={
                        **evidence,
                        "career_goal_error": str(exc),
                    },
                )
            if goal_observation.screen != "unknown":
                evidence["career_goal_observation"] = (
                    career_goal_observation_to_dict(goal_observation)
                )
                if goal_observation.screen == "goal_failure_summary":
                    return self._stop(
                        "career_goal_failure_evidence_required",
                        evidence=evidence,
                    )
                if not goal_observation.is_content_complete:
                    return self._stop(
                        "career_goal_observation_required",
                        evidence=evidence,
                    )
                if (
                    persisted_career_goal_observation_sha256
                    != goal_observation.observation_sha256
                    or type(persisted_career_goal_ledger_revision) is not int
                    or persisted_career_goal_ledger_revision <= 0
                ):
                    reason = (
                        "career_goal_observation_required"
                        if persisted_career_goal_observation_sha256 is None
                        and persisted_career_goal_ledger_revision is None
                        else "career_goal_persistence_mismatch"
                    )
                    return self._stop(reason, evidence=evidence)
                evidence["career_goal_persistence"] = {
                    "observation_sha256": (
                        persisted_career_goal_observation_sha256
                    ),
                    "ledger_revision": persisted_career_goal_ledger_revision,
                }

        if allow_resume_after_skill_handoff or (
            screen_text is not None
            and "技能获取" in str(screen_text.get("normalized_text", ""))
            and self._skill_acquisition_back_control(img) is not None
        ):
            skill_page = inspect_skill_acquisition_page(img)
            evidence[
                "skill_acquisition_resume_page" if allow_resume_after_skill_handoff
                else "skill_acquisition_page"
            ] = skill_page
            if skill_page.get("accepted") is True:
                back_control = self._skill_acquisition_back_control(img)
                if back_control is None:
                    return self._stop(
                        "skill_handoff_resume_back_control_unverified",
                        evidence=evidence,
                    )
                if not allow_resume_after_skill_handoff:
                    return handoff(
                        "skill_acquisition_handoff", evidence=evidence,
                        route_state="skill_acquisition", purchase_executed=False,
                        resume_available=True,
                    )
                return self._tap_decision(
                    "resume_after_skill_acquisition_handoff",
                    "safe_advance.skill_acquisition_back",
                    back_control,
                    evidence,
                )

        if authenticated_scenario_select:
            if not self._looks_like_scenario_select_page(img):
                return self._stop(
                    "authenticated_scenario_scope_mismatch",
                    evidence=evidence,
                )
            return self._configured_decision(
                "scenario_select_continue", img, evidence
            ) or self._stop("scenario_select", evidence=evidence)
        return None

    def _plan_stage_settlement_and_wisdom_checks(
        self,
        *,
        state: _AdvancePlanState,
        path: Path,
        img: np.ndarray,
        image_perception: dict[str, Any],
        image_screen: str,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        evidence: dict[str, Any],
        route_policy: SafeAdvanceRoutePolicy | None,
        allow_enter_training_from_global_home: bool,
        allow_career_end_finish_after_skill_handoff: bool,
    ) -> dict[str, Any] | None:
        if self.is_post_career_global_home(img):
            greeting_dialogue = self._global_home_greeting_dialogue_button(
                screen_text,
                img,
            )
            if greeting_dialogue is not None:
                return self._tap_decision(
                    "dismiss_global_home_greeting",
                    "safe_advance.global_home_dialogue",
                    greeting_dialogue,
                    evidence,
                )
            if allow_enter_training_from_global_home:
                home_training = self._game_home_training_button(perception, img)
                if home_training is not None:
                    return self._tap_decision(
                        "enter_training_from_home",
                        "safe_advance.home_training",
                        home_training,
                        evidence,
                    )
            return self._stop("global_home", evidence=evidence)

        team_prompt_close = self._team_formation_completion_prompt_close_button(
            screen_text,
            img,
        )
        if team_prompt_close is not None:
            return self._tap_decision(
                "close_team_formation_prompt",
                "safe_advance.team_formation_prompt_close",
                team_prompt_close,
                evidence,
            )

        activity_story_close = self._activity_story_unlock_close_button(
            screen_text,
            img,
        )
        if activity_story_close is not None:
            return self._tap_decision(
                "close_activity_story_unlock",
                "safe_advance.activity_story_unlock_close",
                activity_story_close,
                evidence,
            )

        login_walk_reward = self._login_walk_reward_continue_point(screen_text, img)
        if login_walk_reward is not None:
            return self._tap_decision(
                "continue_login_walk_reward",
                "safe_advance.login_walk_reward_continue",
                login_walk_reward,
                evidence,
            )

        if self._looks_like_final_support_reward(screen_text, img):
            height, width = img.shape[:2]
            decision = self._tap_decision(
                "next_support_reward_page",
                "safe_advance.support_reward_next_page",
                (int(width * 0.50), int(height * 0.936)),
                evidence,
            )
            decision["post_tap_confirmation"] = "career_complete_global_home"
            return decision

        if self._looks_like_factor_research_slot_upgrade(img):
            point = self._fullscreen_continue_button(perception, img)
            if point is None:
                return self._stop(
                    "factor_research_slot_upgrade:no_continue_button",
                    evidence=evidence,
                )
            return self._tap_decision(
                "continue_factor_research_slot_upgrade",
                "safe_advance.factor_research_slot_upgrade_continue",
                point,
                evidence,
            )

        if self._looks_like_pending_training_result_log(
            evidence, perception, img, route_policy=route_policy,
        ):
            return self._skip_bottom_dialogue(evidence, img)

        training_result_summary = self._training_result_summary_continue_button(
            evidence,
            perception,
            img,
        )
        if training_result_summary is not None:
            return self._tap_decision(
                "continue_training_result_summary",
                "safe_advance.training_result_continue",
                training_result_summary,
                evidence,
            )

        if (
            (
                self._looks_like_wbc_race_hub(img)
                or self._looks_like_wbc_entry_splash(img)
            )
            and not self._looks_like_story_dialogue_with_record_controls(img)
            and not self._looks_like_training_story_dialogue(img)
        ):
            return self._stop("wbc_race_hub", evidence=evidence)

        if (
            self._looks_like_gur_race_entry(img)
            and not self._looks_like_story_dialogue_with_record_controls(img)
            and not self._looks_like_training_story_dialogue(img)
        ):
            return handoff("race_list", evidence=evidence)

        if self._looks_like_event_point_reward(screen_text):
            point = self._fullscreen_continue_button(perception, img)
            if point is None:
                return self._stop(
                    "event_point_reward:no_continue_button",
                    evidence=evidence,
                )
            return self._tap_decision(
                "continue_event_point_reward",
                "safe_advance.event_point_reward_next_page",
                point,
                evidence,
            )

        if self._looks_like_event_reward_overlay(img):
            point = self._fullscreen_continue_button(perception, img)
            if point is None:
                return self._stop(
                    "event_reward_overlay:no_continue_button",
                    evidence=evidence,
                )
            return self._tap_decision(
                "continue_event_reward_overlay",
                "safe_advance.event_reward_overlay_continue",
                point,
                evidence,
            )

        goal_failure_summary = self._goal_failure_summary_continue_button(
            screen_text,
            perception,
            img,
        )
        if goal_failure_summary is not None:
            evidence['career_outcome']={'goal_failed':True,'source':'visible_goal_failure_summary'}
            return self._tap_decision(
                "continue_goal_failure_summary",
                "safe_advance.goal_failure_summary_continue",
                goal_failure_summary,
                evidence,
            )

        if self._looks_like_final_career_summary(screen_text, img):
            point = self._fullscreen_continue_button(
                perception,
                img,
            ) or self._final_settlement_bottom_green_button(img)
            if point is None:
                return self._stop(
                    "final_career_summary:no_continue_button",
                    evidence=evidence,
                )
            return self._tap_decision(
                "continue_final_career_summary",
                "safe_advance.final_career_summary_continue",
                point,
                evidence,
            )

        # The support-deck screen also contains generic continuation text.
        # Check its stronger visual/page evidence before the text-driven final
        # training rating classifier can claim the same frame.
        training_check = TrainingMenuPrechecker().check(path)
        if training_check.accepted:
            early_support_friend_slot = self._support_deck_empty_friend_slot_button(img)
            if early_support_friend_slot is not None:
                return self._configured_decision("support_deck_select_friend", img, evidence) or self._tap_decision(
                    "support_deck_select_friend",
                    "safe_advance.support_deck_friend_slot",
                    early_support_friend_slot,
                    evidence,
                )
            if self._looks_like_support_deck_page(img):
                return self._configured_decision("support_deck_start_training", img, evidence) or self._tap_decision(
                    "support_deck_start_training",
                    "safe_advance.support_deck_start",
                    (int(img.shape[1] * 0.50), int(img.shape[0] * 0.844)),
                    evidence,
                )

        if self._looks_like_route_bound_training_cut_in(
            img,
            route_policy=route_policy,
            training_check=training_check,
            perception=perception,
        ) or self._looks_like_route_bound_wisdom_acquisition_transition(
            img,
            route_policy=route_policy,
            training_check=training_check,
        ) or self._looks_like_training_action_animation(
            img,
            route_policy=route_policy,
            training_check=training_check,
            perception=perception,
        ):
            return {
                "action": "wait",
                "reason": "training_action_animation_wait",
                "route_state": "loading",
                "evidence": evidence,
            }

        if image_screen != "training_menu" and self._looks_like_final_career_reward(
            screen_text, img
        ):
            point = self._fullscreen_continue_button(perception, img)
            if point is None:
                return self._stop(
                    "final_career_reward:no_continue_button",
                    evidence=evidence,
                )
            return self._tap_decision(
                "continue_final_career_reward",
                "safe_advance.final_career_reward_continue",
                point,
                evidence,
            )

        if self._looks_like_final_training_rating(screen_text, img):
            point = self._fullscreen_continue_button(perception, img)
            if point is None:
                return self._stop(
                    "final_training_rating:no_continue_button",
                    evidence=evidence,
                )
            return self._tap_decision(
                "continue_final_training_rating",
                "safe_advance.final_training_rating_continue",
                point,
                evidence,
            )

        inheritance_confirm = self._inheritance_factor_result_confirm_button(
            screen_text,
            img,
        )
        if inheritance_confirm is not None:
            return self._tap_decision(
                "confirm_inheritance_factors",
                "safe_advance.confirm_inheritance_factors",
                inheritance_confirm,
                evidence,
            )

        if allow_career_end_finish_after_skill_handoff:
            career_end_finish = self._career_end_finish_button(screen_text, img)
            if career_end_finish is not None:
                return self._tap_decision(
                    "open_career_end_finish_confirmation",
                    "safe_advance.career_end_finish",
                    career_end_finish,
                    evidence,
                )

        career_end_skill = self._career_end_skill_acquisition_button(
            screen_text,
            img,
        )
        if career_end_skill is not None:
            return self._tap_decision(
                "open_career_end_skill_acquisition",
                "safe_advance.career_end_skill_acquisition",
                career_end_skill,
                evidence,
            )

        inheritance_factor = self._inheritance_factor_confirm_button(img)
        if inheritance_factor is not None:
            return self._tap_decision(
                "inheritance_factor_confirm",
                "safe_advance.inheritance_factor_confirm",
                inheritance_factor,
                evidence,
            )

        if self._looks_like_healthy_game_advisory(screen_text, img):
            height, width = img.shape[:2]
            point = self._fullscreen_continue_button(perception, img) or (
                int(width * 0.50),
                int(height * 0.95),
            )
            return self._tap_decision(
                "continue_healthy_game_advisory",
                "safe_advance.healthy_game_advisory_continue",
                point,
                evidence,
            )

        if self._continue_training_modal_text_verified(screen_text):
            continue_training = self._continue_training_modal_button(
                image_perception,
                img,
            )
            if continue_training is not None:
                return self._tap_decision(
                    "continue_training_modal",
                    "safe_advance.continue_training",
                    continue_training,
                    evidence,
                )

        final_loss_recovery = self._final_loss_recovery_decision(img, evidence)
        if final_loss_recovery is not None:
            return final_loss_recovery

        wisdom_candidate = self._looks_like_wisdom_fragment_candidate(img)
        if wisdom_candidate and self._looks_like_forbidden_spend_modal(img):
            return self._stop("resource_prompt_unreadable", evidence=evidence)
        if wisdom_candidate:
            wisdom_result = self._classify_wisdom_fragment_dialog(img)
            height, width = img.shape[:2]
            normalization = (
                "native_720"
                if (width, height) == (720, 1280)
                else "controlled_synthetic_1440_to_720"
            )
            evidence["wisdom_fragment_unavailable"] = {
                "normalization": normalization,
                "template_reference_size": [720, 1280],
                "match_state": wisdom_result.state.value,
            }
            if wisdom_result.state is WisdomFragmentDialogState.ASSET_ERROR:
                evidence["wisdom_fragment_unavailable"]["asset_error"] = wisdom_result.asset_error
                return self._stop("wisdom_fragment_template_unavailable", evidence=evidence)
            if wisdom_result.state is WisdomFragmentDialogState.NO_MATCH:
                return self._stop("wisdom_fragment_confirmation_unverified", evidence=evidence)
            if wisdom_result.tap is None:
                return self._stop("wisdom_fragment_confirmation_unverified", evidence=evidence)
            return {
                "action": "tap",
                "reason": "confirm_wisdom_fragment_unavailable",
                "route_state": "scenario_confirmation",
                "tap": {
                    "coordinate_name": "safe_advance.wisdom_fragment_unavailable_confirm",
                    "x": wisdom_result.tap[0],
                    "y": wisdom_result.tap[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }

        connection_conflict_return = self._connection_conflict_return_title_button(img)
        if connection_conflict_return is not None:
            return {
                "action": "tap",
                "reason": "return_title_from_connection_conflict",
                "tap": {
                    "coordinate_name": "safe_advance.return_title",
                    "x": connection_conflict_return[0],
                    "y": connection_conflict_return[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        state.training_check = training_check
        return None

    def _plan_stage_atomic_home_and_gm_mechanics(
        self,
        *,
        state: _AdvancePlanState,
        path: Path,
        img: np.ndarray,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        evidence: dict[str, Any],
        route_policy: SafeAdvanceRoutePolicy | None,
        prefer_scenario_hold: bool,
        scenario: str | None = None,
    ) -> dict[str, Any] | None:
        training_check = state.training_check
        atomic_main_menu = analyze_main_menu_state(
            io_path(path).read_bytes(),
            scenario=scenario or "grand_masters",
        )
        atomic_screen = atomic_main_menu.get("fields", {}).get("screen", {})
        atomic_evidence = {
            "screen": atomic_screen.get("value"),
            "confidence": atomic_screen.get("confidence"),
            "abstained": atomic_screen.get("abstained"),
            "valid_for_training": atomic_main_menu.get("valid_for_training"),
        }
        evidence["atomic_main_menu_state"] = atomic_evidence
        atomic_main_menu_identified = bool(
            atomic_evidence["screen"] == "main_menu"
            and not atomic_evidence["abstained"]
            and float(atomic_evidence["confidence"] or 0.0) >= HIGH_CONFIDENCE
        )
        atomic_ready_main_menu = bool(
            atomic_main_menu_identified
            and atomic_evidence["valid_for_training"] is True
        )
        if self._looks_like_post_action_connection_wait(
            ready_surface=atomic_ready_main_menu,
            screen_text=screen_text,
            route_policy=route_policy,
        ):
            evidence = dict(evidence)
            evidence["post_action_main_menu_connection_wait"] = True
            return {
                "action": "wait",
                "reason": "loading",
                "state": "connection_overlay_wait",
                "route_state": "connection_overlay",
                "evidence": evidence,
            }
        main_menu_idle_hud_ready = bool(
            route_policy is not None
            and route_policy.allows_observed_state("main_menu")
            and atomic_ready_main_menu
            and self._looks_like_training_home_controls(img)
            and not self._looks_like_blocking_bottom_dialogue(img)
        )
        title_start = (
            None
            if atomic_ready_main_menu
            else self._current_cn_title_start_point(img)
        )
        title_text_hint = (
            screen_text.get("primary_hint")
            if isinstance(screen_text, dict)
            else None
        )
        if (
            title_start is None
            and not atomic_ready_main_menu
            and isinstance(title_text_hint, dict)
            and title_text_hint.get("screen") == "title_screen"
            and float(title_text_hint.get("confidence", 0.0) or 0.0) >= 0.95
        ):
            title_start = (int(img.shape[1] * 0.50), int(img.shape[0] * 0.85))
        if title_start is not None:
            return {
                "action": "tap",
                "reason": "tap_title_start",
                "tap": {
                    "coordinate_name": "safe_advance.title_start",
                    "x": title_start[0],
                    "y": title_start[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            self._looks_like_race_day_main_menu(img)
            and self._looks_like_race_day_primary_action(img)
        ):
            if (
                self._looks_like_race_day_badge(img)
                and self._looks_like_active_bottom_skip_button(img)
                and SpeedTogglePlanner._bottom_skip_level(img) == "single"
            ):
                return self._skip_bottom_dialogue(evidence, img)
            return handoff("race_day_main_menu", evidence=evidence)
        if (
            atomic_main_menu_identified
            and self._looks_like_main_training_menu(img)
            and not (
                route_policy is not None
                and route_policy.allows_observed_state("main_menu")
                and atomic_ready_main_menu
            )
        ):
            if (
                self._looks_like_training_story_dialogue(img)
                and self._looks_like_training_hud_dialogue_box(img)
            ):
                skip_level = SpeedTogglePlanner._bottom_skip_level(img)
                if skip_level in {"off", "single"}:
                    return self._skip_bottom_dialogue(evidence, img)
                if (
                    skip_level == "double"
                    and self._looks_like_main_menu_short_dialogue(img)
                ):
                    evidence = dict(evidence)
                    evidence["bottom_skip_level"] = skip_level
                    return handoff("ready_main_menu", evidence=evidence)
                if skip_level == "double":
                    return self._skip_bottom_dialogue(evidence, img)
            if (
                not atomic_ready_main_menu
                and self._looks_like_training_home_controls(img)
                and not self._looks_like_training_story_dialogue(img)
                and not self._looks_like_blocking_bottom_dialogue(img)
            ):
                return handoff("ready_main_menu", evidence=evidence)
            return handoff("ready_training_menu", evidence=evidence)

        tutorial_offer = self._grand_masters_tutorial_offer_evidence(img)
        evidence["grand_masters_tutorial_offer"] = tutorial_offer
        if tutorial_offer["candidate"]:
            if not self._grand_masters_tutorial_offer_text_confirmed(screen_text):
                return self._stop(
                    "grand_masters_tutorial_offer_unconfirmed",
                    evidence=evidence,
                )
            bottom = tutorial_offer.get("bottom_point")
            if not isinstance(bottom, list) or len(bottom) != 2:
                return self._stop(
                    "grand_masters_tutorial_offer_unconfirmed",
                    evidence=evidence,
                )
            return self._tap_decision(
                "decline_grand_masters_tutorial",
                "safe_advance.grand_masters_tutorial_decline",
                (int(bottom[0]), int(bottom[1])),
                evidence,
            )

        tutorial_topics = self._grand_masters_tutorial_topic_menu_evidence(img)
        evidence["grand_masters_tutorial_topics"] = tutorial_topics
        if tutorial_topics.get("candidate"):
            if not tutorial_topics["present"]:
                return self._stop(
                    "grand_masters_tutorial_topics_unconfirmed",
                    evidence=evidence,
                )
            if screen_text is None:
                # Preserve the established offline planner contract for this
                # exact five-option menu.  Formal live execution always supplies
                # a FrameSafety text packet and therefore cannot use this path.
                option_layout = detect_event_option_layout(img)
                if int(option_layout["option_count"]) == 5:
                    return self._choose_default_event_option(
                        option_layout["points"],
                        5,
                        evidence,
                    )
                return self._stop(
                    "grand_masters_tutorial_topics_unconfirmed",
                    evidence=evidence,
                )
            if not self._grand_masters_tutorial_topic_text_confirmed(screen_text):
                return self._stop(
                    "grand_masters_tutorial_topics_unconfirmed",
                    evidence=evidence,
                )
            height, width = img.shape[:2]
            return self._tap_decision(
                "skip_grand_masters_tutorial_topics",
                "safe_advance.grand_masters_tutorial_skip",
                (
                    int(round(width * 360 / 720)),
                    int(round(height * 831 / 1280)),
                ),
                evidence,
            )

        grand_masters_tutorial_finish = self._grand_masters_tutorial_finish_button(img)
        if grand_masters_tutorial_finish is not None:
            return self._tap_decision(
                "finish_grand_masters_tutorial",
                "safe_advance.grand_masters_tutorial_close",
                grand_masters_tutorial_finish,
                evidence,
            )

        grand_masters_tutorial = self._grand_masters_tutorial_continue_button(
            perception,
            img,
        )
        if grand_masters_tutorial is not None:
            return self._tap_decision(
                "continue_grand_masters_tutorial",
                "safe_advance.grand_masters_tutorial_continue",
                grand_masters_tutorial,
                evidence,
            )

        if training_check is None:
            training_check = TrainingMenuPrechecker().check(path)
        evidence["training_menu_precheck"] = {
            "accepted": training_check.accepted,
            "reason": training_check.reason,
            "selected_tab": training_check.selected_tab,
            "failures": list(training_check.failures),
        }
        if self._looks_like_post_action_connection_wait(
            ready_surface=training_check.accepted,
            screen_text=screen_text,
            route_policy=route_policy,
        ):
            evidence = dict(evidence)
            evidence["post_action_training_menu_connection_wait"] = True
            return {
                "action": "wait",
                "reason": "loading",
                "state": "connection_overlay_wait",
                "route_state": "connection_overlay",
                "evidence": evidence,
            }
        # A support-deck page can contain generic "continue"/rating text in
        # its OCR stream.  Resolve its visual controls before text-only final
        # rating branches below; the formal safety gate still evaluates the
        # same frame packet before any live tap.
        support_friend_slot = self._support_deck_empty_friend_slot_button(img)
        if support_friend_slot is not None:
            return self._configured_decision("support_deck_select_friend", img, evidence) or self._tap_decision(
                "support_deck_select_friend",
                "safe_advance.support_deck_friend_slot",
                support_friend_slot,
                evidence,
            )
        if self._looks_like_support_deck_page(img):
            return self._configured_decision("support_deck_start_training", img, evidence) or self._tap_decision(
                "support_deck_start_training",
                "safe_advance.support_deck_start",
                (int(img.shape[1] * 0.50), int(img.shape[0] * 0.844)),
                evidence,
            )
        grand_masters_effects_close = self._grand_masters_effects_close_button(
            img,
            screen_text,
        )
        if grand_masters_effects_close is not None:
            evidence["grand_masters_effects_list"] = {
                "present": True,
                "source": "specific_text_and_layout",
            }
            return {
                "action": "tap",
                "reason": "close_grand_masters_effects",
                "route_state": "scenario_effects_close",
                "tap": {
                    "coordinate_name": "safe_advance.grand_masters_effects_close",
                    "x": grand_masters_effects_close[0],
                    "y": grand_masters_effects_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        crystal_reward = detect_grand_masters_crystal_reward_overlay(
            img,
            visible_text=screen_text,
        )
        evidence["grand_masters_crystal_reward"] = crystal_reward
        if crystal_reward["text_match"]:
            return self._grand_masters_crystal_reward_decision(evidence, img)
        if crystal_reward["present"]:
            return {
                "action": "wait",
                "reason": "grand_masters_crystal_reward_unconfirmed",
                "route_state": "loading",
                "evidence": evidence,
            }
        skill_evolution = self._skill_evolution_action(img, screen_text)
        if skill_evolution is not None:
            action, point = skill_evolution
            if action == "select":
                self._pending_skill_evolution_confirm = True
                return self._tap_decision(
                    "select_skill_evolution",
                    "safe_advance.skill_evolution_select",
                    point,
                    evidence,
                )
            if action == "confirm_selection":
                self._pending_skill_evolution_confirm = False
                return self._tap_decision(
                    "confirm_skill_evolution_selection",
                    "safe_advance.skill_evolution_confirm",
                    point,
                    evidence,
                )
            self._pending_skill_evolution_confirm = False
            return self._tap_decision(
                "confirm_skill_evolution",
                "safe_advance.skill_evolution_dialog_confirm",
                point,
                evidence,
            )
        else:
            self._pending_skill_evolution_confirm = False
        knowledge_close = self._knowledge_table_close_button(img)
        if knowledge_close is not None:
            return {
                "action": "tap",
                "reason": "close_knowledge_table",
                "tap": {
                    "coordinate_name": "safe_advance.knowledge_close",
                    "x": knowledge_close[0],
                    "y": knowledge_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        learn_wisdom = self._learn_wisdom_button(perception, img)
        if learn_wisdom is not None:
            if prefer_scenario_hold:
                hold_close = self._available_knowledge_table_close_button(img)
                if hold_close is None:
                    return self._stop(
                        "scenario_hold_close_unconfirmed",
                        evidence=evidence,
                    )
                return {
                    "action": "tap",
                    "reason": "hold_available_wisdom",
                    "route_state": "scenario_hold",
                    "tap": {
                        "coordinate_name": "safe_advance.knowledge_hold_close",
                        "x": hold_close[0],
                        "y": hold_close[1],
                        "scaled": False,
                    },
                    "evidence": evidence,
                }
            return {
                "action": "tap",
                "reason": "learn_wisdom_modal",
                "tap": {
                    "coordinate_name": "safe_advance.learn_wisdom",
                    "x": learn_wisdom[0],
                    "y": learn_wisdom[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        goal_remaining_summary = self._goal_remaining_text_continue_button(
            evidence,
            perception,
            img,
        )
        if goal_remaining_summary is not None:
            return {
                "action": "tap",
                "reason": "continue_goal_remaining_result",
                "tap": {
                    "coordinate_name": "safe_advance.goal_remaining_continue",
                    "x": goal_remaining_summary[0],
                    "y": goal_remaining_summary[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        trainee_detail_close = self._trainee_detail_close_button(
            screen_text,
            img,
        )
        if (
            trainee_detail_close is not None
            and route_policy is not None
            and route_policy.source == "pending"
            and route_policy.pending_action_id
            == "safe_advance.confirm_inheritance_factors"
            and route_policy.allows_observed_state("dialogue")
        ):
            return self._tap_decision(
                "close_trainee_detail",
                "safe_advance.trainee_detail_close",
                trainee_detail_close,
                evidence,
            )
        state.training_check = training_check
        state.atomic_main_menu_identified = atomic_main_menu_identified
        state.atomic_ready_main_menu = atomic_ready_main_menu
        state.main_menu_idle_hud_ready = main_menu_idle_hud_ready
        state.crystal_reward = crystal_reward
        return None

    def _plan_stage_screen_graph_setup_and_race_stops(
        self,
        *,
        state: _AdvancePlanState,
        path: Path,
        img: np.ndarray,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        screen: str,
        evidence: dict[str, Any],
        route_policy: SafeAdvanceRoutePolicy | None,
        allow_return_main_menu_from_training: bool,
        allow_resume_after_skill_handoff: bool,
    ) -> dict[str, Any] | None:
        training_check = state.training_check
        assert training_check is not None
        atomic_ready_main_menu = state.atomic_ready_main_menu
        main_menu_idle_hud_ready = state.main_menu_idle_hud_ready
        crystal_reward = state.crystal_reward
        assert crystal_reward is not None
        graph_state = dict(perception)
        graph_state["training_menu_precheck"] = evidence["training_menu_precheck"]
        graph_match = self.screen_graph.match(graph_state, img, image_path=str(path))
        if (
            graph_match is not None
            and graph_match.get("node_id") == "resource_prompt"
            and graph_match.get("evidence", {}).get("matched")
            == [{"text_terms": ["恢复"]}]
            and isinstance(screen_text, dict)
            and screen_text.get("dangerous") is False
            and not screen_text.get("resource_terms")
        ):
            hint_lines = tuple(
                "".join(line.split()).replace("！", "!")
                for line in str(screen_text.get("visible_text", "")).splitlines()
                if line.strip()
            )
            if hint_lines[:1] == ("启发等级提升!",) and any(
                re.fullmatch(r"“[^”]+”的启发等级提升了[1-9][0-9]*", line)
                for line in hint_lines[1:]
            ):
                graph_match = self.screen_graph.match(
                    graph_state, img, image_path=str(path),
                    candidate_node_ids=[
                        node["id"] for node in self.screen_graph.nodes
                        if node["id"] != "resource_prompt"
                    ],
                )
        graph_decision = graph_match.get("decision") if graph_match else None
        if (
            isinstance(graph_decision, dict)
            and graph_match.get("node_id") == "goal_achievement_result"
            and graph_decision.get("action") == "tap"
        ):
            graph_decision = dict(graph_decision)
            graph_decision["reason"] = "continue_goal_achievement_result"
            graph_tap = graph_decision.get("tap")
            if isinstance(graph_tap, dict):
                graph_decision["tap"] = {
                    **graph_tap,
                    "coordinate_name": "safe_advance.goal_achievement_continue",
                }
        if (
            isinstance(graph_decision, dict)
            and graph_decision.get("reason") == "skip_bottom_dialogue"
            and SpeedTogglePlanner._bottom_skip_state(img) == "on"
            and not (
                main_menu_idle_hud_ready
                and graph_match.get("node_id") == "bottom_dialogue_main_menu"
            )
        ):
            skip_evidence = dict(evidence)
            skip_evidence["screen_graph"] = graph_decision.get("evidence", {}).get(
                "screen_graph",
                {},
            )
            return self._skip_bottom_dialogue(skip_evidence, img)
        trainee_continue = self._trainee_select_continue_button(perception, img)
        if trainee_continue is not None:
            return self._configured_decision("trainee_select_continue", img, evidence) or self._tap_decision(
                "trainee_select_continue",
                "safe_advance.trainee_continue",
                trainee_continue,
                evidence,
            )
        inheritance_auto_select = self._inheritance_auto_select_button(img)
        if inheritance_auto_select is not None:
            return self._configured_decision("inheritance_auto_select", img, evidence) or self._tap_decision(
                "inheritance_auto_select",
                "safe_advance.inheritance_auto_select",
                inheritance_auto_select,
                evidence,
            )
        inheritance_continue = self._inheritance_select_continue_button(perception, img)
        if inheritance_continue is not None:
            return self._configured_decision("inheritance_select_continue", img, evidence) or self._tap_decision(
                "inheritance_select_continue",
                "safe_advance.inheritance_continue",
                inheritance_continue,
                evidence,
            )
        opening_skip = self._common_bottom_skip_decision(evidence, img)
        if opening_skip is not None:
            return opening_skip
        startup_promo_skip = self._startup_promo_skip_button(img)
        if startup_promo_skip is not None:
            if self._looks_like_partial_inheritance_select_page(img):
                return self._stop("ambiguous_inheritance_page", evidence=evidence)
            return self._tap_decision(
                "skip_startup_promo",
                "safe_advance.bottom_right_skip",
                startup_promo_skip,
                evidence,
            )
        if self._looks_like_support_card_event_dialogue(screen_text, img):
            return self._advance_bottom_dialogue(evidence, img)
        if (
            (graph_match is None or graph_match.get("node_id") == "goal_result")
            and not main_menu_idle_hud_ready
            and self._looks_like_training_top_hud(img)
            and self._looks_like_large_bottom_dialogue(img)
            and not self._looks_like_main_menu_short_dialogue(img)
            and not self._looks_like_race_list(img)
            and not self._looks_like_race_day_main_menu(img)
            and not self._looks_like_training_success_result(img)
            and not self._looks_like_bottom_skip_off_pill(img)
            and not self._looks_like_bottom_skip_toggle_off_control(img)
            and (
                self._looks_like_green_nameplate_bottom_dialogue(img)
                or self._looks_like_training_story_dialogue(img)
            )
        ):
            skip_state = SpeedTogglePlanner._bottom_skip_state(img)
            if skip_state == "on":
                return self._skip_bottom_dialogue(evidence, img)
            if skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                return self._enable_bottom_skip_toggle(evidence, img)
            return self._advance_bottom_dialogue(evidence, img)
        if self._looks_like_main_menu_short_dialogue(img):
            if self._looks_like_training_success_result(img):
                return self._skip_bottom_dialogue(evidence, img)
            return handoff("ready_main_menu", evidence=evidence)
        skill_back = self._skill_acquisition_back_button(img)
        if skill_back is not None:
            if allow_resume_after_skill_handoff:
                return {
                    "action": "tap",
                    "reason": "resume_after_skill_acquisition_handoff",
                    "tap": {
                        "coordinate_name": "safe_advance.skill_acquisition_back",
                        "x": skill_back[0],
                        "y": skill_back[1],
                        "scaled": False,
                    },
                    "evidence": evidence,
                }
            return handoff(
                "skill_acquisition_handoff",
                evidence=evidence,
                route_state="skill_acquisition",
                purchase_executed=False,
                resume_available=True,
            )
        support_start_confirm = self._support_deck_start_confirm_button(perception, img)
        if support_start_confirm is not None:
            return self._configured_decision("support_deck_start_confirm", img, evidence) or self._tap_decision(
                "support_deck_start_confirm",
                "safe_advance.support_deck_start_confirm",
                support_start_confirm,
                evidence,
            )
        if "settings_overlay" in set(training_check.failures):
            height, width = img.shape[:2]
            return {
                "action": "tap",
                "reason": "close_settings_overlay",
                "tap": {
                    "coordinate_name": "safe_advance.settings_cancel",
                    "x": int(width * 0.28),
                    "y": int(height * 0.923),
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            isinstance(graph_decision, dict)
            and graph_decision.get("action") in {"stop", "tap", "wait"}
            and graph_match.get("node_id") != "unknown_stop"
        ):
            race_list_like = self._looks_like_race_list(img)
            race_day_like = self._looks_like_race_day_main_menu(img)
            if training_check.accepted:
                race_day_like = bool(
                    race_day_like
                    and self._looks_like_grand_masters_final_race_day_main_menu(img)
                )
            race_primary_entry = self._bottom_center_primary_green_button(perception, img)
            if race_list_like or race_day_like:
                if (
                    race_day_like
                    and self._looks_like_race_day_badge(img)
                    and self._looks_like_active_bottom_skip_button(img)
                    and SpeedTogglePlanner._bottom_skip_level(img)
                    in {"single", "double"}
                ):
                    return self._skip_bottom_dialogue(evidence, img)
                goal_progress_continue = self._goal_progress_list_continue_button(perception, img)
                if goal_progress_continue is not None:
                    return {
                        "action": "tap",
                        "reason": "continue_goal_progress_result",
                        "tap": {
                            "coordinate_name": "safe_advance.goal_progress_continue",
                            "x": goal_progress_continue[0],
                            "y": goal_progress_continue[1],
                            "scaled": False,
                        },
                        "evidence": evidence,
                    }
                goal_achieved_continue = self._goal_achieved_continue_button(perception, img)
                if goal_achieved_continue is not None:
                    return {
                        "action": "tap",
                        "reason": "continue_goal_achievement_result",
                        "tap": {
                            "coordinate_name": "safe_advance.goal_achievement_continue",
                            "x": goal_achieved_continue[0],
                            "y": goal_achieved_continue[1],
                            "scaled": False,
                        },
                        "evidence": evidence,
                    }
                if race_primary_entry is not None:
                    if race_day_like:
                        return handoff("race_day_main_menu", evidence=evidence)
                    return handoff("race_list", evidence=evidence)
                goal_remaining_skip = self._goal_remaining_result_skip_button(perception, img)
                if goal_remaining_skip is not None:
                    if SpeedTogglePlanner._bottom_skip_level(img) in {"single", "double"}:
                        return self._skip_bottom_dialogue(evidence, img)
                    return {
                        "action": "tap",
                        "reason": "continue_goal_remaining_result",
                        "tap": {
                            "coordinate_name": "safe_advance.goal_remaining_continue",
                            "x": goal_remaining_skip[0],
                            "y": goal_remaining_skip[1],
                            "scaled": False,
                        },
                        "evidence": evidence,
                    }
                if self._looks_like_training_story_dialogue(img) or self._looks_like_story_dialogue_with_record_controls(img):
                    race_skip_state = SpeedTogglePlanner._bottom_skip_state(img)
                    race_fast_forward = self._race_story_fast_forward_button(
                        img,
                        allow_with_bottom_skip=True,
                    )
                    if race_fast_forward is not None:
                        return self._race_story_fast_forward_decision(evidence, race_fast_forward)
                    if race_skip_state == "on":
                        return self._skip_bottom_dialogue(evidence, img)
                    if race_skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                        return self._enable_bottom_skip_toggle(evidence, img)
                    return self._advance_bottom_dialogue(evidence, img)
                if race_day_like:
                    return handoff("race_day_main_menu", evidence=evidence)
                return handoff("race_list", evidence=evidence)
            if graph_match.get("node_id") == "training_success_result_dialogue":
                skip_state = SpeedTogglePlanner._bottom_skip_state(img)
                if skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                    return self._enable_bottom_skip_toggle(evidence, img)
                if skip_state == "on" and self._looks_like_active_bottom_skip_button(img):
                    return self._skip_bottom_dialogue(evidence, img)
            if graph_match.get("node_id") == "landscape_race_menu":
                landscape_menu_close = self._landscape_race_menu_close_button(img)
                if landscape_menu_close is not None:
                    decision_evidence = dict(evidence)
                    decision_evidence["screen_graph"] = graph_decision.get("evidence", {}).get("screen_graph", {})
                    return self._tap_decision(
                        "close_landscape_race_menu",
                        "safe_advance.landscape_race_menu_close",
                        landscape_menu_close,
                        decision_evidence,
                    )
            graph_reason = str(graph_decision.get("reason", ""))
            landscape_race_list_like = (
                str(perception.get("landscape_race_kind", "")) == "landscape_race_list"
            )
            ignore_graph_race_list = (
                graph_reason == "race_list"
                and not race_list_like
                and not race_day_like
                and not landscape_race_list_like
            )
            defer_training_menu_ready = (
                training_check.accepted
                and allow_return_main_menu_from_training
                and graph_match.get("node_id") == "training_menu_ready"
            )
            ignore_graph_bottom_dialogue_main_menu = (
                main_menu_idle_hud_ready
                and graph_match.get("node_id") == "bottom_dialogue_main_menu"
            )
            if (
                not ignore_graph_race_list
                and not defer_training_menu_ready
                and not ignore_graph_bottom_dialogue_main_menu
            ):
                if graph_reason in {
                    "skip_bottom_dialogue",
                    "enable_bottom_skip_toggle",
                }:
                    skip_evidence = dict(evidence)
                    skip_evidence["screen_graph"] = graph_decision.get(
                        "evidence", {}
                    ).get("screen_graph", {})
                    if graph_reason == "enable_bottom_skip_toggle":
                        return self._enable_bottom_skip_toggle(skip_evidence, img)
                    return self._skip_bottom_dialogue(skip_evidence, img)
                decision = dict(graph_decision)
                decision_evidence = dict(decision.get("evidence", {}))
                decision_evidence.update(evidence)
                decision_evidence["screen_graph"] = graph_decision.get("evidence", {}).get("screen_graph", {})
                decision["evidence"] = decision_evidence
                return decision
        if training_check.accepted:
            if allow_return_main_menu_from_training and not crystal_reward["present"]:
                decision = self._configured_decision(
                    "return_main_menu_from_training",
                    img,
                    evidence,
                )
                if isinstance(decision, dict) and decision.get("action") == "tap":
                    return decision
                return self._stop(
                    "return_main_menu_from_training_action_missing",
                    evidence=evidence,
                )
            return handoff("ready_training_menu", evidence=evidence)
        home_training = self._game_home_training_button(perception, img)
        if home_training is not None:
            return {
                "action": "tap",
                "reason": "enter_training_from_home",
                "tap": {
                    "coordinate_name": "safe_advance.home_training",
                    "x": home_training[0],
                    "y": home_training[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        friend_support = self._friend_support_first_card_button(img)
        if friend_support is not None:
            return self._configured_decision("friend_support_select_first", img, evidence) or self._tap_decision(
                "friend_support_select_first",
                "safe_advance.friend_support_first_card",
                friend_support,
                evidence,
            )
        record_close = self._record_modal_close_button(img)
        if record_close is not None:
            return {
                "action": "tap",
                "reason": "close_record_modal",
                "tap": {
                    "coordinate_name": "safe_advance.record_close",
                    "x": record_close[0],
                    "y": record_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        reservation_close = self._race_reservation_list_close_button(img)
        if reservation_close is not None:
            return {
                "action": "tap",
                "reason": "race_reservation_list_close",
                "tap": {
                    "coordinate_name": "safe_advance.race_reservation_close",
                    "x": reservation_close[0],
                    "y": reservation_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        training_result_summary = self._training_result_summary_continue_button(
            evidence,
            perception,
            img,
        )
        if training_result_summary is not None:
            return {
                "action": "tap",
                "reason": "continue_training_result_summary",
                "tap": {
                    "coordinate_name": "safe_advance.training_result_continue",
                    "x": training_result_summary[0],
                    "y": training_result_summary[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        goal_progress_continue = self._goal_progress_list_continue_button(perception, img)
        if goal_progress_continue is not None:
            return {
                "action": "tap",
                "reason": "continue_goal_progress_result",
                "tap": {
                    "coordinate_name": "safe_advance.goal_progress_continue",
                    "x": goal_progress_continue[0],
                    "y": goal_progress_continue[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        announcement_close = self._announcement_close_button(img)
        if announcement_close is not None:
            return {
                "action": "tap",
                "reason": "close_announcement",
                "tap": {
                    "coordinate_name": "safe_advance.announcement_close",
                    "x": announcement_close[0],
                    "y": announcement_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        race_playback_menu_close = self._race_playback_menu_close_button(img)
        if race_playback_menu_close is not None:
            return {
                "action": "tap",
                "reason": "close_menu_panel",
                "tap": {
                    "coordinate_name": "safe_advance.menu_close",
                    "x": race_playback_menu_close[0],
                    "y": race_playback_menu_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        trophy_reward_close = self._trophy_reward_close_button(img)
        if trophy_reward_close is not None:
            return {
                "action": "tap",
                "reason": "close_trophy_reward",
                "tap": {
                    "coordinate_name": "safe_advance.trophy_reward_close",
                    "x": trophy_reward_close[0],
                    "y": trophy_reward_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        continue_training = self._continue_training_modal_button(perception, img)
        if continue_training is not None:
            return {
                "action": "tap",
                "reason": "continue_training_modal",
                "tap": {
                    "coordinate_name": "safe_advance.continue_training",
                    "x": continue_training[0],
                    "y": continue_training[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        goal_achieved_continue = self._goal_achieved_continue_button(perception, img)
        if goal_achieved_continue is not None:
            return {
                "action": "tap",
                "reason": "continue_goal_achievement_result",
                "tap": {
                    "coordinate_name": "safe_advance.goal_achievement_continue",
                    "x": goal_achieved_continue[0],
                    "y": goal_achieved_continue[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if self._looks_like_gur_race_entry(img) or (
            self._looks_like_race_list(img)
            and self._bottom_center_primary_green_button(perception, img) is not None
        ):
            return handoff("race_list", evidence=evidence)
        goal_remaining_skip = self._goal_remaining_result_skip_button(perception, img)
        if goal_remaining_skip is not None:
            if SpeedTogglePlanner._bottom_skip_level(img) in {"single", "double"}:
                return self._skip_bottom_dialogue(evidence, img)
            return {
                "action": "tap",
                "reason": "continue_goal_remaining_result",
                "tap": {
                    "coordinate_name": "safe_advance.goal_remaining_continue",
                    "x": goal_remaining_skip[0],
                    "y": goal_remaining_skip[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            self._looks_like_training_top_hud(img)
            and self._looks_like_large_bottom_dialogue(img)
            and self._looks_like_dialogue_continue_arrow(img)
            and (
            self._looks_like_bottom_skip_toggle_off_control(img)
            or self._looks_like_bottom_skip_off_pill(img)
            )
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        cn_title_start = (
            None
            if atomic_ready_main_menu or self._looks_like_training_top_hud(img)
            else self._cn_title_start_point(img)
        )
        if cn_title_start is not None:
            return {
                "action": "tap",
                "reason": "tap_title_start",
                "tap": {
                    "coordinate_name": "safe_advance.title_start",
                    "x": cn_title_start[0],
                    "y": cn_title_start[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        race_story_dialogue = (
            self._looks_like_story_dialogue_with_record_controls(img)
            or self._looks_like_training_story_dialogue(img)
        )
        race_primary_entry = self._bottom_center_primary_green_button(perception, img)
        if (
            race_primary_entry is None
            and (self._looks_like_race_day_main_menu(img) or self._looks_like_race_list(img))
            and race_story_dialogue
        ):
            race_skip_state = SpeedTogglePlanner._bottom_skip_state(img)
            race_fast_forward = self._race_story_fast_forward_button(
                img,
                allow_with_bottom_skip=True,
            )
            if race_fast_forward is not None:
                return self._race_story_fast_forward_decision(evidence, race_fast_forward)
            if race_skip_state == "on":
                return self._skip_bottom_dialogue(evidence, img)
            if race_skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                return self._enable_bottom_skip_toggle(evidence, img)
            return self._advance_bottom_dialogue(evidence, img)
        if self._looks_like_race_day_main_menu(img):
            return handoff("race_day_main_menu", evidence=evidence)
        if screen == "race_participant_list":
            from core.auto_trainer.race_route import RaceRoutePlanner

            if RaceRoutePlanner._grand_masters_final_selected_list(evidence, img) is not None:
                return handoff("race_list", evidence=evidence)
            if {"查看结果", "前往赛事"}.issubset(
                set(str((screen_text or {}).get("normalized_text", "")).splitlines())
            ) and RaceRoutePlanner._race_preview_action_points(img) is not None:
                return self._stop("race_detail", evidence=evidence)
        if (
            self._looks_like_race_detail_action_page(perception, img)
            or self._looks_like_strong_portrait_race_detail(img)
        ):
            return self._stop("race_detail", evidence=evidence)
        if self._looks_like_race_list(img):
            return handoff("race_list", evidence=evidence)
        if self._looks_like_gur_race_entry(img) and not race_story_dialogue:
            return handoff("race_list", evidence=evidence)
        if self._looks_like_race_scene_controls(perception, img):
            if self._looks_like_training_story_dialogue(img):
                race_scene_skip_state = SpeedTogglePlanner._bottom_skip_state(img)
                if race_scene_skip_state == "on" and self._looks_like_active_bottom_skip_button(img):
                    return self._skip_bottom_dialogue(evidence, img)
                if race_scene_skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                    return self._enable_bottom_skip_toggle(evidence, img)
                return self._advance_bottom_dialogue(evidence, img)
            return self._stop("race_scene", evidence=evidence)
        honor_title_confirm = self._honor_title_confirm_button(perception, img)
        if honor_title_confirm is not None:
            return self._configured_decision("honor_title", img, evidence) or self._tap_decision(
                "close_honor_title",
                "safe_advance.honor_title_confirm",
                honor_title_confirm,
                evidence,
            )
        early_event_option_layout = detect_event_option_layout(img)
        race_story_dialogue = self._race_day_story_dialogue_action(evidence, img)
        race_story_single_option = self._single_green_event_option_button(img)
        if (
            race_story_single_option is not None
            and SpeedTogglePlanner._bottom_skip_level(img) in {"single", "double"}
        ):
            if route_policy is not None and route_policy.constrained:
                return handoff("event_choice", evidence=evidence)
            return {
                "action": "tap",
                "reason": "choose_single_event_option",
                "tap": {
                    "coordinate_name": "safe_advance.single_event_option",
                    "x": race_story_single_option[0],
                    "y": race_story_single_option[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            race_story_dialogue is not None
            and not early_event_option_layout["points"]
            and self._looks_like_race_day_badge(img)
        ):
            return race_story_dialogue
        single_event_option = race_story_single_option
        if single_event_option is not None:
            if route_policy is not None and route_policy.constrained:
                return handoff("event_choice", evidence=evidence)
            return {
                "action": "tap",
                "reason": "choose_single_event_option",
                "tap": {
                    "coordinate_name": "safe_advance.single_event_option",
                    "x": single_event_option[0],
                    "y": single_event_option[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            self._looks_like_blocking_bottom_dialogue(img)
            and self._looks_like_bottom_skip_toggle_off_control(img)
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        if (
            not atomic_ready_main_menu
            and self._looks_like_training_top_hud(img)
            and self._looks_like_training_result_dialogue(img)
            and not early_event_option_layout["points"]
            and SpeedTogglePlanner._bottom_skip_state(img) == "on"
            and self._looks_like_active_bottom_skip_button(img)
        ):
            return self._skip_bottom_dialogue(evidence, img)
        title_start = (
            None
            if atomic_ready_main_menu or self._looks_like_training_top_hud(img)
            else self._title_start_point(img)
        )
        if title_start is not None:
            return {
                "action": "tap",
                "reason": "tap_title_start",
                "tap": {
                    "coordinate_name": "safe_advance.title_start",
                    "x": title_start[0],
                    "y": title_start[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        return None

    def _plan_stage_dialogues_transitions_and_fallbacks(
        self,
        *,
        state: _AdvancePlanState,
        img: np.ndarray,
        image_screen: str,
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        screen: str,
        evidence: dict[str, Any],
        route_policy: SafeAdvanceRoutePolicy | None,
        allow_resume_after_skill_handoff: bool,
        allow_finish_training_with_unused_skill_points: bool,
    ) -> dict[str, Any]:
        training_check = state.training_check
        assert training_check is not None
        atomic_main_menu_identified = state.atomic_main_menu_identified
        atomic_ready_main_menu = state.atomic_ready_main_menu
        main_menu_idle_hud_ready = state.main_menu_idle_hud_ready
        skip_state = SpeedTogglePlanner._bottom_skip_state(img)
        if (
            self._looks_like_training_home_controls(img)
            and self._go_to_race_modal_button(perception, img) is None
            and self._safe_green_confirm_button(perception, img) is None
            and not self._looks_like_confirm_modal_panel(img)
        ):
            home_dialogue = (
                self._looks_like_training_hud_dialogue_box(img)
                or self._looks_like_training_story_dialogue(img)
            )
            home_blocking = (
                (
                    "blocking_screen" in set(training_check.failures)
                    and not main_menu_idle_hud_ready
                )
                or (
                    self._looks_like_training_story_dialogue(img)
                    and not main_menu_idle_hud_ready
                )
            )
            if (
                home_dialogue
                and skip_state == "on"
                and self._looks_like_active_bottom_skip_button(img)
                and not self._looks_like_ready_wisdom_fragment_panel(img)
                and not main_menu_idle_hud_ready
            ):
                return self._skip_bottom_dialogue(evidence, img)
            if home_dialogue and home_blocking:
                if skip_state == "on":
                    return self._skip_bottom_dialogue(evidence, img)
                if skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                    return self._enable_bottom_skip_toggle(evidence, img)
                return self._advance_bottom_dialogue(evidence, img)
            if (
                screen == "event_choice"
                and skip_state == "on"
                and home_dialogue
                and home_blocking
            ):
                return self._skip_bottom_dialogue(evidence, img)
            if (
                screen == "event_choice"
                and skip_state == "off"
                and home_dialogue
                and home_blocking
            ):
                return self._enable_bottom_skip_toggle(evidence, img)
            if (
                home_dialogue
                and not self._looks_like_ready_wisdom_fragment_panel(img)
                and self._looks_like_live_bottom_skip_toggle_off_control(img)
            ):
                return self._enable_bottom_skip_toggle(evidence, img)
            if (
                not home_dialogue
                and not self._looks_like_ready_wisdom_fragment_panel(img)
                and self._looks_like_live_bottom_skip_toggle_off_control(img)
            ):
                return self._enable_bottom_skip_toggle(evidence, img)
            decision = handoff("ready_main_menu", evidence=evidence)
            if (
                route_policy is not None
                and route_policy.allows_observed_state("main_menu")
                and atomic_ready_main_menu
            ):
                decision["route_state"] = "main_menu"
            return decision
        participation_close = self._post_career_participation_prompt_action(
            img,
            screen_text,
        )
        if participation_close is not None:
            return self._tap_decision(
                "close_post_career_participation_prompt",
                "safe_advance.post_career_participation_close",
                participation_close,
                evidence,
            )
        if self._looks_like_team_formation_page(img):
            team_close = self._team_formation_page_close_button(img)
            if team_close is not None:
                return self._tap_decision(
                    "close_team_formation_page",
                    "safe_advance.team_formation_page_close",
                    team_close,
                    evidence,
                )
            return self._configured_decision("team_formation", img, evidence) or self._stop(
                "team_formation",
                evidence=evidence,
            )
        event_option_layout = detect_event_option_layout(img)
        event_option_points = event_option_layout["points"]
        event_option_count = int(event_option_layout["option_count"])
        friend_training_effect = self._looks_like_friend_training_effect_frame(img)
        blocking_bottom_dialogue = self._looks_like_blocking_bottom_dialogue(img)
        scenario_select_page = self._looks_like_scenario_select_page(img)
        training_result_dialogue = (
            self._looks_like_training_result_dialogue(img)
            and not scenario_select_page
        )
        bottom_dialogue_scene = (
            (blocking_bottom_dialogue or self._looks_like_training_story_dialogue(img))
            and not scenario_select_page
        )
        dimmed_training_narration = self._looks_like_dimmed_training_narration(img)
        # A post-training gain animation keeps the training HUD and the global
        # skip/shorten controls visible.  The generic fullscreen-button detector
        # can merge those two adjacent green controls and mistake them for a
        # continue button.  Route the frame through the skip-level policy first:
        # single upgrades once; double waits for the already-enabled auto skip.
        if (
            screen == "training_menu"
            and self._looks_like_main_training_menu(img)
            and training_result_dialogue
            and SpeedTogglePlanner._bottom_skip_level(img) in {"single", "double"}
            and not main_menu_idle_hud_ready
        ):
            return self._skip_bottom_dialogue(evidence, img)
        wisdom_prompt_confirm = self._wisdom_learning_prompt_confirm_button(
            evidence,
            perception,
            img,
        )
        if wisdom_prompt_confirm is not None:
            return {
                "action": "tap",
                "reason": "confirm_wisdom_learning_prompt",
                "tap": {
                    "coordinate_name": "safe_advance.wisdom_learning_prompt_confirm",
                    "x": wisdom_prompt_confirm[0],
                    "y": wisdom_prompt_confirm[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        rest_confirm = self._common_rest_confirmation_decision(evidence, perception, img)
        if rest_confirm is not None:
            return rest_confirm
        swbc_skill_shortage_result = self._swbc_skill_shortage_result_continue_button(
            evidence,
            perception,
            img,
        )
        if swbc_skill_shortage_result is not None:
            return {
                "action": "tap",
                "reason": "continue_swbc_skill_shortage_result",
                "tap": {
                    "coordinate_name": "safe_advance.swbc_result_continue",
                    "x": swbc_skill_shortage_result[0],
                    "y": swbc_skill_shortage_result[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        swbc_ranked_reward = self._swbc_ranked_reward_continue_button(
            evidence,
            perception,
            img,
        )
        if swbc_ranked_reward is not None:
            return {
                "action": "tap",
                "reason": "continue_swbc_ranked_reward",
                "tap": {
                    "coordinate_name": "safe_advance.swbc_ranked_reward_continue",
                    "x": swbc_ranked_reward[0],
                    "y": swbc_ranked_reward[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        swbc_growth_summary = self._swbc_growth_summary_continue_button(
            evidence,
            perception,
            img,
        )
        if swbc_growth_summary is not None:
            return {
                "action": "tap",
                "reason": "continue_swbc_growth_summary",
                "tap": {
                    "coordinate_name": "safe_advance.swbc_growth_summary_continue",
                    "x": swbc_growth_summary[0],
                    "y": swbc_growth_summary[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        unused_points_finish = self._finish_training_unused_points_confirm_button(
            perception, screen_text, img
        )
        if unused_points_finish is not None:
            if not allow_finish_training_with_unused_skill_points:
                return self._tap_decision(
                    "cancel_career_end_finish",
                    "safe_advance.career_end_finish_cancel",
                    self._finish_training_cancel_for_unused_points(screen_text, img),
                    evidence,
                )
            return self._tap_decision(
                "finish_training_confirm",
                "safe_advance.finish_training",
                unused_points_finish,
                evidence,
            )
        safe_confirm = self._safe_green_confirm_button(perception, img)
        if safe_confirm is not None:
            return {
                "action": "tap",
                "reason": "confirm_safe_modal",
                "tap": {
                    "coordinate_name": "safe_advance.safe_confirm",
                    "x": safe_confirm[0],
                    "y": safe_confirm[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        race_recommendation_confirm = self._race_recommendation_top_confirm_button(
            evidence,
            perception,
            img,
        )
        if race_recommendation_confirm is not None:
            return {
                "action": "tap",
                "reason": "confirm_race_recommendation_top",
                "tap": {
                    "coordinate_name": "safe_advance.race_recommendation_top_confirm",
                    "x": race_recommendation_confirm[0],
                    "y": race_recommendation_confirm[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if allow_resume_after_skill_handoff:
            skill_page = inspect_skill_acquisition_page(img)
            evidence["skill_acquisition_resume_page"] = skill_page
            if skill_page.get("accepted") is True:
                back_control = self._skill_acquisition_back_control(img)
                if back_control is None:
                    return self._stop(
                        "skill_handoff_resume_back_control_unverified",
                        evidence=evidence,
                    )
                return {
                    "action": "tap",
                    "reason": "resume_after_skill_acquisition_handoff",
                    "tap": {
                        "coordinate_name": "safe_advance.skill_acquisition_back",
                        "x": back_control[0],
                        "y": back_control[1],
                        "scaled": False,
                    },
                    "evidence": evidence,
                }
        if (
            SpeedTogglePlanner._bottom_skip_level(img) == "double"
            and self._looks_like_large_bottom_dialogue(img)
            and self._looks_like_dialogue_continue_arrow(img)
        ):
            return self._skip_bottom_dialogue(evidence, img)
        white_flash_training_pending = bool(
            route_policy is not None
            and route_policy.constrained
            and route_policy.source == "pending"
            and route_policy.pending_action_id == "training.execute"
            and route_policy.allows_observed_state("loading")
            and re.fullmatch(r"[0-9a-f]{64}", str(route_policy.action_source_sha256 or ""))
            and str((screen_text or {}).get("normalized_text", "")).splitlines()[:1] == ["训练"]
            and not bottom_dialogue_scene
            and not self._looks_like_training_hud_dialogue_box(img)
        )
        if self._looks_like_friendship_training_trigger_animation(
            img, allow_white_flash=white_flash_training_pending,
        ):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "friendship_training_trigger_animation",
                "evidence": evidence,
            }
        if self._looks_like_blank_white_loading(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "blank_white_loading",
                "evidence": evidence,
            }
        fullscreen_continue = self._fullscreen_continue_button(perception, img)
        if (
            fullscreen_continue is not None
            and not dimmed_training_narration
            and not blocking_bottom_dialogue
            and not bottom_dialogue_scene
            and event_option_count < 4
        ):
            return {
                "action": "tap",
                "reason": "continue_fullscreen_result",
                "tap": {
                    "coordinate_name": "safe_advance.fullscreen_continue",
                    "x": fullscreen_continue[0],
                    "y": fullscreen_continue[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        go_to_race = self._go_to_race_modal_button(perception, img)
        if go_to_race is not None:
            if not self._fan_goal_shortfall_warning_text_verified(screen_text):
                return self._stop("fan_goal_warning_text_unverified", evidence=evidence)
            return {
                "action": "tap",
                "reason": "go_to_race_modal",
                "tap": {
                    "coordinate_name": "safe_advance.go_to_race",
                    "x": go_to_race[0],
                    "y": go_to_race[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        finish_training = self._finish_training_confirm_button(perception, img)
        if finish_training is not None:
            finish_cancel = self._finish_training_cancel_for_unused_points(
                screen_text, img
            )
            if finish_cancel is not None:
                return self._tap_decision(
                    "cancel_career_end_finish",
                    "safe_advance.career_end_finish_cancel",
                    finish_cancel,
                    evidence,
                )
            return self._configured_decision("finish_training_confirm", img, evidence) or self._tap_decision(
                "finish_training_confirm",
                "safe_advance.finish_training",
                finish_training,
                evidence,
            )
        safe_confirm = self._safe_green_confirm_button(perception, img)
        if safe_confirm is not None:
            return {
                "action": "tap",
                "reason": "confirm_safe_modal",
                "tap": {
                    "coordinate_name": "safe_advance.safe_confirm",
                    "x": safe_confirm[0],
                    "y": safe_confirm[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        # Login-reward pages can momentarily resemble a stack of event-choice
        # cards while their reward icons animate.  Their dedicated bottom-right
        # skip control is more specific than the generic option-layout detector,
        # so resolve it before event-choice routing.
        reward_skip = self._bottom_right_skip_button(img)
        if (
            reward_skip is not None
            and SpeedTogglePlanner._bottom_skip_state(img) == "absent"
            and not self._looks_like_blocking_bottom_dialogue(img)
            and not self._looks_like_bottom_skip_toggle_off_control(img)
            and not self._looks_like_bottom_skip_off_pill(img)
        ):
            return {
                "action": "tap",
                "reason": "skip_bottom_right_reward",
                "tap": {
                    "coordinate_name": "safe_advance.bottom_right_skip",
                    "x": reward_skip[0],
                    "y": reward_skip[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            screen_text is not None
            and is_goal_achievement_title((str(screen_text.get("normalized_text", "")).splitlines() or [None])[0])
            and self._looks_like_goal_achievement_animation(perception, img)
        ):
            return {"action": "wait", "reason": "loading",
                    "route_state": "goal_achievement_animation", "evidence": evidence}
        single_event_option = self._single_green_event_option_button(img)
        if single_event_option is not None and not friend_training_effect:
            if route_policy is not None and route_policy.constrained:
                return handoff("event_choice", evidence=evidence)
            return {
                "action": "tap",
                "reason": "choose_single_event_option",
                "tap": {
                    "coordinate_name": "safe_advance.single_event_option",
                    "x": single_event_option[0],
                    "y": single_event_option[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if event_option_points and not friend_training_effect:
            return self._choose_default_event_option(event_option_points, event_option_count, evidence)
        if (
            skip_state == "off"
            and blocking_bottom_dialogue
            and self._looks_like_training_story_dialogue(img)
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        if (
            skip_state == "off"
            and self._looks_like_bottom_skip_toggle_off_control(img)
            and (
                blocking_bottom_dialogue
                or self._looks_like_training_story_dialogue(img)
                or self._looks_like_story_dialogue_with_record_controls(img)
                or self._looks_like_hot_nameplate_bottom_dialogue(img)
            )
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        if (
            skip_state == "absent"
            and blocking_bottom_dialogue
            and self._looks_like_bottom_skip_toggle_off_control(img)
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        if (
            skip_state in {"off", "on"}
            and self._looks_like_training_success_result(img)
        ):
            return self._skip_bottom_dialogue(evidence, img)
        if (
            skip_state == "on"
            and training_result_dialogue
            and (
                self._looks_like_active_bottom_skip_button(img)
                or self._looks_like_blocking_bottom_dialogue(img)
            )
        ):
            return self._skip_bottom_dialogue(evidence, img)
        if self._looks_like_scenario_connecting_overlay(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "scenario_connecting",
                "evidence": evidence,
            }
        if self._looks_like_game_home_transition(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "game_home_transition",
                "evidence": evidence,
            }
        if scenario_select_page:
            return self._configured_decision("scenario_select_continue", img, evidence) or self._stop(
                "scenario_select",
                evidence=evidence,
            )
        participation_close = self._post_career_participation_prompt_action(
            img,
            screen_text,
        )
        if participation_close is not None:
            return self._tap_decision(
                "close_post_career_participation_prompt",
                "safe_advance.post_career_participation_close",
                participation_close,
                evidence,
            )
        if self._looks_like_team_formation_page(img):
            team_close = self._team_formation_page_close_button(img)
            if team_close is not None:
                return self._tap_decision(
                    "close_team_formation_page",
                    "safe_advance.team_formation_page_close",
                    team_close,
                    evidence,
                )
            return self._configured_decision("team_formation", img, evidence) or self._stop(
                "team_formation",
                evidence=evidence,
            )
        fullscreen_continue = self._fullscreen_continue_button(perception, img)
        if (
            fullscreen_continue is not None
            and not dimmed_training_narration
            and not bottom_dialogue_scene
        ):
            return {
                "action": "tap",
                "reason": "continue_fullscreen_result",
                "tap": {
                    "coordinate_name": "safe_advance.fullscreen_continue",
                    "x": fullscreen_continue[0],
                    "y": fullscreen_continue[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            dimmed_training_narration
            and not event_option_points
            and SpeedTogglePlanner._bottom_skip_level(img) in {"single", "double"}
        ):
            return self._skip_bottom_dialogue(evidence, img)
        if event_option_points and not friend_training_effect:
            return self._choose_default_event_option(event_option_points, event_option_count, evidence)
        if self._looks_like_main_training_menu(img):
            if (
                not atomic_main_menu_identified
                and not self._looks_like_training_home_controls(img)
                and training_result_dialogue
                and SpeedTogglePlanner._bottom_skip_level(img) == "double"
            ):
                return self._skip_bottom_dialogue(evidence, img)
            if self._looks_like_blocking_bottom_dialogue(img):
                if skip_state == "off":
                    return self._enable_bottom_skip_toggle(evidence, img)
                if skip_state == "on":
                    return self._skip_bottom_dialogue(evidence, img)
                return self._advance_bottom_dialogue(evidence, img)
            if self._looks_like_bottom_skip_toggle_off_control(img):
                return self._enable_bottom_skip_toggle(evidence, img)
            return handoff("ready_main_menu", evidence=evidence)
        if skip_state == "off" and self._looks_like_inheritance_performance_scene(img):
            return self._enable_bottom_skip_toggle(evidence, img)
        if skip_state == "off" and self._looks_like_blocking_bottom_dialogue(img):
            if not self._looks_like_green_nameplate_bottom_dialogue(img):
                return self._advance_bottom_dialogue(evidence, img)
            return self._enable_bottom_skip_toggle(evidence, img)
        if skip_state == "on" and self._looks_like_blocking_bottom_dialogue(img):
            return self._skip_bottom_dialogue(evidence, img)
        if (
            skip_state in {"on", "off"}
            and self._looks_like_blocking_bottom_dialogue(img)
        ):
            return self._advance_bottom_dialogue(evidence, img)
        if skip_state in {"on", "off"} and friend_training_effect:
            return self._advance_dialogue(evidence)
        if self._looks_like_transition_wipe(perception, img):
            return {
                "action": "wait",
                "reason": "transition",
                "route_state": "transition",
                "evidence": evidence,
            }
        if self._looks_like_loading_spinner(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "loading",
                "evidence": evidence,
            }
        if self._looks_like_white_loading_tips(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "loading_tips",
                "evidence": evidence,
            }
        if self._looks_like_cygames_splash(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "cygames_splash",
                "evidence": evidence,
            }
        if self._looks_like_bilibili_splash(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "bilibili_splash",
                "evidence": evidence,
            }
        if self._looks_like_grand_masters_fragment_animation(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "grand_masters_fragment_animation",
                "evidence": evidence,
            }
        if self._looks_like_grand_masters_wisdom_learn_animation(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "grand_masters_wisdom_learn_animation",
                "evidence": evidence,
            }
        if screen == "landscape_action" and self._looks_like_landscape_racecourse_establishing_loading(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "landscape_racecourse_loading",
                "evidence": evidence,
            }
        if self._looks_like_blurred_loading_overlay(img):
            return self._configured_decision("blurred_loading", img, evidence) or {
                "action": "wait",
                "reason": "loading",
                "route_state": "blurred_loading",
                "evidence": evidence,
            }
        if self._looks_like_startup_progress(img):
            return {
                "action": "wait",
                "reason": "loading",
                "route_state": "startup_loading",
                "evidence": evidence,
            }
        title_start = (
            None
            if atomic_ready_main_menu or self._looks_like_training_top_hud(img)
            else self._title_start_point(img)
        )
        if title_start is not None:
            return {
                "action": "tap",
                "reason": "tap_title_start",
                "tap": {
                    "coordinate_name": "safe_advance.title_start",
                    "x": title_start[0],
                    "y": title_start[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        go_to_race = self._go_to_race_modal_button(perception, img)
        if go_to_race is not None:
            if not self._fan_goal_shortfall_warning_text_verified(screen_text):
                return self._stop("fan_goal_warning_text_unverified", evidence=evidence)
            return {
                "action": "tap",
                "reason": "go_to_race_modal",
                "tap": {
                    "coordinate_name": "safe_advance.go_to_race",
                    "x": go_to_race[0],
                    "y": go_to_race[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        finish_training = self._finish_training_confirm_button(perception, img)
        if finish_training is not None:
            finish_cancel = self._finish_training_cancel_for_unused_points(
                screen_text, img
            )
            if finish_cancel is not None:
                return self._tap_decision(
                    "cancel_career_end_finish",
                    "safe_advance.career_end_finish_cancel",
                    finish_cancel,
                    evidence,
                )
            return self._configured_decision("finish_training_confirm", img, evidence) or self._tap_decision(
                "finish_training_confirm",
                "safe_advance.finish_training",
                finish_training,
                evidence,
            )
        safe_confirm = self._safe_green_confirm_button(perception, img)
        if safe_confirm is not None:
            return {
                "action": "tap",
                "reason": "confirm_safe_modal",
                "tap": {
                    "coordinate_name": "safe_advance.safe_confirm",
                    "x": safe_confirm[0],
                    "y": safe_confirm[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            training_result_dialogue
            and skip_state == "off"
            and self._looks_like_training_story_dialogue(img)
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        menu_close = self._menu_panel_close_button(img)
        if menu_close is not None:
            return {
                "action": "tap",
                "reason": "close_menu_panel",
                "tap": {
                    "coordinate_name": "safe_advance.menu_close",
                    "x": menu_close[0],
                    "y": menu_close[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        reward_skip = self._bottom_right_skip_button(img)
        if (
            reward_skip is not None
            and SpeedTogglePlanner._bottom_skip_state(img) == "absent"
            and not self._looks_like_blocking_bottom_dialogue(img)
            and not self._looks_like_bottom_skip_toggle_off_control(img)
            and not self._looks_like_bottom_skip_off_pill(img)
        ):
            return {
                "action": "tap",
                "reason": "skip_bottom_right_reward",
                "tap": {
                    "coordinate_name": "safe_advance.bottom_right_skip",
                    "x": reward_skip[0],
                    "y": reward_skip[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if (
            screen == "modal_confirm"
            and training_result_dialogue
            and not self._looks_like_confirm_modal_panel(img)
        ):
            return self._advance_bottom_dialogue(evidence, img)
        if screen == "modal_confirm":
            return self._stop("modal_confirm", evidence=evidence)
        fullscreen_continue = self._fullscreen_continue_button(perception, img)
        if fullscreen_continue is not None and not bottom_dialogue_scene:
            return {
                "action": "tap",
                "reason": "continue_fullscreen_result",
                "tap": {
                    "coordinate_name": "safe_advance.fullscreen_continue",
                    "x": fullscreen_continue[0],
                    "y": fullscreen_continue[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        if training_result_dialogue:
            if skip_state == "on":
                return self._skip_bottom_dialogue(evidence, img)
            return self._advance_bottom_dialogue(evidence, img)
        if (
            self._looks_like_training_story_dialogue(img)
            and self._looks_like_bottom_skip_toggle_off_control(img)
        ):
            return self._enable_bottom_skip_toggle(evidence, img)
        if screen in {"portrait_ui", "unknown", "game_home"} and self._looks_like_story_dialogue_with_record_controls(img):
            if skip_state == "on" and self._looks_like_active_bottom_skip_button(img):
                return self._skip_bottom_dialogue(evidence, img)
            if skip_state == "off" or self._looks_like_bottom_skip_toggle_off_control(img):
                return self._enable_bottom_skip_toggle(evidence, img)
            return self._advance_bottom_dialogue(evidence, img)
        if (
            image_screen == "training_menu"
            and not atomic_ready_main_menu
            and self._looks_like_training_top_hud(img)
        ):
            if (
                skip_state == "on"
                and self._looks_like_active_bottom_skip_button(img)
            ):
                return self._skip_bottom_dialogue(evidence, img)
            if skip_state == "absent":
                return {
                    "action": "wait",
                    "reason": "training_menu_transition",
                    "route_state": "loading",
                    "evidence": evidence,
                }
        race_story_fast_forward = self._race_story_fast_forward_button(img)
        if race_story_fast_forward is not None:
            return {
                "action": "tap",
                "reason": "skip_race_story_scene",
                "tap": {
                    "coordinate_name": "safe_advance.race_story_fast_forward",
                    "x": race_story_fast_forward[0],
                    "y": race_story_fast_forward[1],
                    "scaled": False,
                },
                "evidence": evidence,
            }
        story_fast_forward = self._career_opening_closeup_fast_forward_button(
            img
        ) or self._current_vr_opening_fast_forward_button(
            img,
            screen_text,
        ) or self._story_bottom_right_fast_forward_button(img)
        if story_fast_forward is not None:
            return self._configured_decision("story_fast_forward", img, evidence) or self._tap_decision(
                "story_fast_forward",
                "safe_advance.story_fast_forward",
                story_fast_forward,
                evidence,
            )
        if skip_state in {"on", "off"} and screen in {"event_choice", "training_menu", "unknown", "portrait_ui"}:
            return self._advance_dialogue(evidence)
        if screen == "game_home":
            goal_achieved_overlay = self._goal_achieved_animation_overlay_point(img)
            if goal_achieved_overlay is not None:
                return self._tap_decision(
                    "continue_goal_achieved_overlay",
                    "safe_advance.goal_achieved_overlay",
                    goal_achieved_overlay,
                    evidence,
                )
        return self._stop(f"unsupported_screen:{screen}", evidence=evidence)

    def _configured_decision(
        self,
        state: str,
        img: np.ndarray,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.action_table.decision("safe_advance", state, img, evidence=evidence)

    @staticmethod
    def _tap_decision(
        reason: str,
        coordinate_name: str,
        point: tuple[int, int],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "action": "tap",
            "reason": reason,
            "tap": {
                "coordinate_name": coordinate_name,
                "x": point[0],
                "y": point[1],
                "scaled": False,
            },
            "evidence": evidence,
        }

    @staticmethod
    def _stop(
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        data = {"action": "stop", "reason": reason, "evidence": evidence or {}}
        data.update(extra)
        return data
