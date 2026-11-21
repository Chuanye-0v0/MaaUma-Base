"""Unified transition, wipe, splash, loading, and gentle fade waiting policy."""
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

from core.auto_trainer.transition_wait import (
    GentleTransitionWaitPolicy,
    TransitionBudget,
    blank_white_loading_visible,
    blue_white_transition_visible,
    career_comic_loading_visible,
    classify_gentle_transition_frame,
    connecting_overlay_visible,
    evaluate_gentle_transition_wait,
    gentle_fade_or_blur_kind,
    orange_wipe_visible,
    pastel_diagonal_wipe_visible,
    technical_wait_stop,
    transient_observation,
    transition_phase,
)
from ._compat import (
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


class SafeAdvanceTransitionMixin:
    """Visual loading, wipe, splash, and connection-wait detectors."""

    @staticmethod
    def _looks_like_transition_wipe(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        return bool(orange_wipe_visible(img) or pastel_diagonal_wipe_visible(img))

    @staticmethod
    def _looks_like_loading_spinner(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        dark = np.mean(hsv[:, :, 2] < 70)
        center = hsv[int(height * 0.52) : int(height * 0.72), int(width * 0.35) : int(width * 0.65)]
        if center.size == 0:
            return False
        center_bright = np.mean((center[:, :, 2] > 120) & (center[:, :, 1] > 25))
        full_bright = np.mean((hsv[:, :, 2] > 120) & (hsv[:, :, 1] > 25))
        return bool(dark > 0.80 and center_bright > 0.003 and 0.0001 < full_bright < 0.06)

    @staticmethod
    def _looks_like_white_loading_tips(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        full_white = np.mean((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170))
        mid_tip = hsv[int(height * 0.38) : int(height * 0.60), int(width * 0.05) : int(width * 0.95)]
        if mid_tip.size == 0:
            return False
        tip_green = np.mean(
            (mid_tip[:, :, 0] >= 38)
            & (mid_tip[:, :, 0] <= 92)
            & (mid_tip[:, :, 1] > 40)
            & (mid_tip[:, :, 2] > 100)
        )
        tip_brown = np.mean(
            (mid_tip[:, :, 0] >= 5)
            & (mid_tip[:, :, 0] <= 30)
            & (mid_tip[:, :, 1] > 50)
            & (mid_tip[:, :, 2] > 80)
        )
        return bool(full_white > 0.90 and tip_green > 0.04 and tip_brown > 0.03)

    @staticmethod
    def _looks_like_blank_white_loading(img: np.ndarray) -> bool:
        return bool(blank_white_loading_visible(img))

    @staticmethod
    def _looks_like_cygames_splash(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        full_white = np.mean((hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 210))
        saturated = np.mean((hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 100))
        top = hsv[0 : int(height * 0.25), :]
        bottom = hsv[int(height * 0.75) : height, :]
        logo = hsv[int(height * 0.39) : int(height * 0.55), int(width * 0.10) : int(width * 0.90)]
        if top.size == 0 or bottom.size == 0 or logo.size == 0:
            return False
        top_white = np.mean((top[:, :, 1] < 45) & (top[:, :, 2] > 210))
        bottom_white = np.mean((bottom[:, :, 1] < 45) & (bottom[:, :, 2] > 210))
        logo_dark = np.mean(logo[:, :, 2] < 160)
        return bool(
            full_white > 0.94
            and saturated < 0.01
            and top_white > 0.98
            and bottom_white > 0.98
            and 0.08 < logo_dark < 0.35
        )

    @staticmethod
    def _looks_like_bilibili_splash(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white = (hsv[:, :, 1] < 45) & (hsv[:, :, 2] > 210)
        logo = hsv[int(height * 0.42) : int(height * 0.55), int(width * 0.20) : int(width * 0.80)]
        if logo.size == 0:
            return False
        logo_pink = (
            ((logo[:, :, 0] >= 160) | (logo[:, :, 0] <= 5))
            & (logo[:, :, 1] > 100)
            & (logo[:, :, 2] > 150)
        )
        logo_bands = np.array_split(logo_pink, 3, axis=1)
        top_white = np.mean(white[: int(height * 0.35), :])
        bottom_white = np.mean(white[int(height * 0.60) :, :])
        return bool(
            np.mean(white) > 0.97
            and top_white > 0.995
            and bottom_white > 0.995
            and 0.12 < np.mean(logo_pink) < 0.25
            and all(0.10 < np.mean(band) < 0.25 for band in logo_bands)
        )

    @staticmethod
    def _looks_like_scenario_connecting_overlay(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_right = hsv[int(height * 0.04) : int(height * 0.105), int(width * 0.68) : int(width * 0.98)]
        continue_button = hsv[int(height * 0.80) : int(height * 0.90), int(width * 0.25) : int(width * 0.75)]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if top_right.size == 0 or continue_button.size == 0 or bottom_nav.size == 0:
            return False
        top_white = np.mean((top_right[:, :, 1] < 65) & (top_right[:, :, 2] > 175))
        top_brown = np.mean(
            (top_right[:, :, 0] >= 5)
            & (top_right[:, :, 0] <= 30)
            & (top_right[:, :, 1] > 50)
            & (top_right[:, :, 2] > 80)
        )
        top_orange = np.mean(
            (top_right[:, :, 0] >= 5)
            & (top_right[:, :, 0] <= 38)
            & (top_right[:, :, 1] > 70)
            & (top_right[:, :, 2] > 100)
        )
        continue_green = np.mean(
            (continue_button[:, :, 0] >= 38)
            & (continue_button[:, :, 0] <= 92)
            & (continue_button[:, :, 1] > 70)
            & (continue_button[:, :, 2] > 80)
        )
        continue_white = np.mean((continue_button[:, :, 1] < 65) & (continue_button[:, :, 2] > 175))
        nav_green = np.mean(
            (bottom_nav[:, :, 0] >= 38)
            & (bottom_nav[:, :, 0] <= 92)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 80)
        )
        return bool(
            top_white > 0.20
            and top_brown > 0.04
            and top_orange > 0.055
            and continue_green > 0.65
            and continue_white < 0.08
            and nav_green > 0.25
        )

    @staticmethod
    def _looks_like_game_home_transition(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_resources = hsv[int(height * 0.02) : int(height * 0.11), int(width * 0.18) : int(width * 0.90)]
        center = hsv[int(height * 0.20) : int(height * 0.76), int(width * 0.05) : int(width * 0.95)]
        main_actions = hsv[int(height * 0.70) : int(height * 0.92), :]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if top_resources.size == 0 or center.size == 0 or main_actions.size == 0 or bottom_nav.size == 0:
            return False
        top_white = np.mean((top_resources[:, :, 1] < 65) & (top_resources[:, :, 2] > 170))
        top_orange = np.mean(
            (top_resources[:, :, 0] >= 5)
            & (top_resources[:, :, 0] <= 30)
            & (top_resources[:, :, 1] > 70)
            & (top_resources[:, :, 2] > 100)
        )
        center_white = np.mean((center[:, :, 1] < 65) & (center[:, :, 2] > 170))
        center_orange = np.mean(
            (center[:, :, 0] >= 5)
            & (center[:, :, 0] <= 30)
            & (center[:, :, 1] > 70)
            & (center[:, :, 2] > 100)
        )
        center_blue = np.mean(
            (center[:, :, 0] >= 85)
            & (center[:, :, 0] <= 125)
            & (center[:, :, 1] > 60)
            & (center[:, :, 2] > 90)
        )
        center_saturated = np.mean((center[:, :, 1] > 70) & (center[:, :, 2] > 90))
        main_green = np.mean(
            (main_actions[:, :, 0] >= 38)
            & (main_actions[:, :, 0] <= 92)
            & (main_actions[:, :, 1] > 70)
            & (main_actions[:, :, 2] > 80)
        )
        nav_green = np.mean(
            (bottom_nav[:, :, 0] >= 38)
            & (bottom_nav[:, :, 0] <= 92)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 80)
        )
        nav_blue = np.mean(
            (bottom_nav[:, :, 0] >= 85)
            & (bottom_nav[:, :, 0] <= 125)
            & (bottom_nav[:, :, 1] > 60)
            & (bottom_nav[:, :, 2] > 90)
        )
        return bool(
            top_white > 0.20
            and top_orange > 0.06
            and center_white < 0.20
            and center_orange < 0.03
            and center_blue > 0.25
            and center_saturated > 0.65
            and main_green > 0.55
            and nav_green > 0.25
            and nav_blue > 0.05
        )

    @staticmethod
    def _looks_like_blurred_loading_overlay(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        white = np.mean((hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 175))
        graywhite = np.mean((hsv[:, :, 1] < 90) & (hsv[:, :, 2] > 120))
        saturated = np.mean((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 90))
        bottom = hsv[int(height * 0.78) : height, :]
        bottom_white = np.mean((bottom[:, :, 1] < 70) & (bottom[:, :, 2] > 175)) if bottom.size else 0.0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edge_variance = cv2.Laplacian(gray, cv2.CV_64F).var()
        return bool(
            white > 0.90
            and graywhite > 0.94
            and saturated < 0.08
            and bottom_white > 0.90
            and edge_variance < 8.0
        )

    @staticmethod
    def _looks_like_post_action_connection_wait(
        *,
        ready_surface: bool,
        screen_text: dict[str, Any] | None,
        route_policy: SafeAdvanceRoutePolicy | None,
    ) -> bool:
        if not ready_surface or screen_text is None:
            return False
        if bool(screen_text.get("dangerous")):
            return False
        if (
            route_policy is None
            or route_policy.source != "pending"
            or route_policy.pending_action_id
            not in ({"training.execute", "safe_advance.rest_confirm", "safe_advance.rest_outing_confirm"}
                    | {f"outing.satake_mei.{stage}" for stage in range(1, 6)})
            or not route_policy.allows_observed_state("loading")
            or not route_policy.allows_observed_state("main_menu")
            or not route_policy.allows_observed_state("event_choice")
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(route_policy.action_source_sha256 or ""),
            )
            is None
        ):
            return False
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        return "连接中" in normalized
