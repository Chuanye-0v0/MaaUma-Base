"""Visual and layout classifiers for SafeAdvance pages and HUD controls."""
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


class SafeAdvanceClassifierMixin:
    """Stateless visual/HUD page classifiers for SafeAdvancePlanner."""

    @staticmethod
    def _looks_like_main_training_menu(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.70) : height, :]
        if bottom.size == 0:
            return False

        green = np.mean(
            (bottom[:, :, 0] >= 38)
            & (bottom[:, :, 0] <= 92)
            & (bottom[:, :, 1] > 70)
            & (bottom[:, :, 2] > 80)
        )
        blue = np.mean(
            (bottom[:, :, 0] >= 85)
            & (bottom[:, :, 0] <= 125)
            & (bottom[:, :, 1] > 60)
            & (bottom[:, :, 2] > 90)
        )
        yellow_or_orange = np.mean(
            (bottom[:, :, 0] >= 5)
            & (bottom[:, :, 0] <= 38)
            & (bottom[:, :, 1] > 70)
            & (bottom[:, :, 2] > 100)
        )
        saturated = np.mean((bottom[:, :, 1] > 70) & (bottom[:, :, 2] > 90))
        if green > 0.07 and blue > 0.03 and yellow_or_orange > 0.03 and saturated > 0.18:
            return True

        lower = hsv[int(height * 0.80) : height, :]
        lower_green = np.mean(
            (lower[:, :, 0] >= 38)
            & (lower[:, :, 0] <= 92)
            & (lower[:, :, 1] > 70)
            & (lower[:, :, 2] > 80)
        )
        lower_blue = np.mean(
            (lower[:, :, 0] >= 85)
            & (lower[:, :, 0] <= 125)
            & (lower[:, :, 1] > 60)
            & (lower[:, :, 2] > 90)
        )
        lower_pink = np.mean(
            (lower[:, :, 0] >= 140)
            & (lower[:, :, 0] <= 175)
            & (lower[:, :, 1] > 60)
            & (lower[:, :, 2] > 90)
        )
        lower_orange = np.mean(
            (lower[:, :, 0] >= 5)
            & (lower[:, :, 0] <= 28)
            & (lower[:, :, 1] > 70)
            & (lower[:, :, 2] > 100)
        )
        return bool(
            lower_green > 0.07
            and lower_blue > 0.03
            and lower_pink > 0.05
            and lower_orange > 0.03
            and lower_orange < 0.24
        )

    @staticmethod
    def _looks_like_training_home_controls(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            y1: float,
            y2: float,
            x1: float,
            x2: float,
            mask: Callable[[np.ndarray], np.ndarray],
        ) -> float:
            region = hsv[int(height * y1) : int(height * y2), int(width * x1) : int(width * x2)]
            if region.size == 0:
                return 0.0
            return float(np.mean(mask(region)))

        green = lambda region: (
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        blue = lambda region: (
            (region[:, :, 0] >= 85)
            & (region[:, :, 0] <= 125)
            & (region[:, :, 1] > 60)
            & (region[:, :, 2] > 90)
        )
        orange = lambda region: (
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 38)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 100)
        )
        pink = lambda region: (
            (region[:, :, 0] >= 140)
            & (region[:, :, 0] <= 175)
            & (region[:, :, 1] > 60)
            & (region[:, :, 2] > 90)
        )
        rest_green = ratio(0.72, 0.84, 0.02, 0.29, green)
        train_blue = ratio(0.72, 0.84, 0.30, 0.70, blue)
        skill_blue = ratio(0.72, 0.84, 0.70, 0.98, blue)
        outing_orange = ratio(0.84, 0.94, 0.22, 0.52, orange)
        race_pink = ratio(0.84, 0.94, 0.70, 0.98, pink)
        return bool(
            rest_green > 0.10
            and train_blue > 0.12
            and skill_blue > 0.08
            and (outing_orange > 0.12 or race_pink > 0.10)
        )

    @staticmethod
    def _looks_like_training_animation_controls(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if (width, height) == (1440, 2560):
            img = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        elif (width, height) != (720, 1280):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def white_ratio(y1: int, y2: int, x1: int, x2: int) -> float:
            roi = hsv[y1:y2, x1:x2]
            return float(np.mean((roi[:, :, 1] < 70) & (roi[:, :, 2] > 160)))

        def green_ratio(y1: int, y2: int, x1: int, x2: int) -> float:
            roi = hsv[y1:y2, x1:x2]
            return float(
                np.mean(
                    (roi[:, :, 0] >= 38)
                    & (roi[:, :, 0] <= 92)
                    & (roi[:, :, 1] > 70)
                    & (roi[:, :, 2] > 80)
                )
            )

        def graywhite_ratio(y1: int, y2: int, x1: int, x2: int) -> float:
            roi = hsv[y1:y2, x1:x2]
            return float(np.mean((roi[:, :, 1] < 95) & (roi[:, :, 2] > 105)))

        return bool(
            (
                white_ratio(1215, 1278, 175, 335) > 0.24
                or SafeAdvancePlanner._looks_like_active_bottom_skip_button(img)
            )
            and green_ratio(1210, 1278, 337, 497) > 0.32
            and graywhite_ratio(1188, 1278, 505, 595) > 0.42
            and graywhite_ratio(1188, 1278, 598, 690) > 0.42
        )

    @staticmethod
    def _looks_like_race_list(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        if SafeAdvancePlanner._looks_like_race_participant_list(img):
            return True
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        card_region = hsv[
            int(height * 0.55) : int(height * 0.82),
            int(width * 0.02) : int(width * 0.98),
        ]
        bottom_region = hsv[int(height * 0.80) : height, :]
        title_region = hsv[0 : int(height * 0.08), 0 : int(width * 0.35)]
        if card_region.size == 0 or bottom_region.size == 0 or title_region.size == 0:
            return False
        title_blue = np.mean(
            (title_region[:, :, 0] >= 85)
            & (title_region[:, :, 0] <= 125)
            & (title_region[:, :, 1] > 60)
            & (title_region[:, :, 2] > 90)
        )
        card_white = np.mean((card_region[:, :, 1] < 60) & (card_region[:, :, 2] > 175))
        card_blue = np.mean(
            (card_region[:, :, 0] >= 85)
            & (card_region[:, :, 0] <= 125)
            & (card_region[:, :, 1] > 60)
            & (card_region[:, :, 2] > 90)
        )
        card_orange = np.mean(
            (card_region[:, :, 0] >= 5)
            & (card_region[:, :, 0] <= 38)
            & (card_region[:, :, 1] > 70)
            & (card_region[:, :, 2] > 100)
        )
        card_red = np.mean(
            ((card_region[:, :, 0] <= 8) | (card_region[:, :, 0] >= 170))
            & (card_region[:, :, 1] > 70)
            & (card_region[:, :, 2] > 100)
        )
        bottom_white = np.mean((bottom_region[:, :, 1] < 60) & (bottom_region[:, :, 2] > 175))
        entry_zone = hsv[int(height * 0.80) : int(height * 0.90), int(width * 0.25) : int(width * 0.75)]
        entry_green = np.mean(
            (entry_zone[:, :, 0] >= 38)
            & (entry_zone[:, :, 0] <= 92)
            & (entry_zone[:, :, 1] > 70)
            & (entry_zone[:, :, 2] > 80)
        )
        if (
            entry_green < 0.30
            and SpeedTogglePlanner._bottom_skip_state(img) in {"on", "off"}
            and (
                SafeAdvancePlanner._looks_like_training_result_dialogue(img)
                or SafeAdvancePlanner._looks_like_blocking_bottom_dialogue(img)
            )
        ):
            return False
        modal_header = hsv[int(height * 0.22) : int(height * 0.31), int(width * 0.01) : int(width * 0.99)]
        modal_body = hsv[int(height * 0.31) : int(height * 0.79), int(width * 0.01) : int(width * 0.99)]
        modal_buttons = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.05) : int(width * 0.95)]
        modal_header_green = np.mean(
            (modal_header[:, :, 0] >= 38)
            & (modal_header[:, :, 0] <= 92)
            & (modal_header[:, :, 1] > 70)
            & (modal_header[:, :, 2] > 80)
        )
        modal_body_white = np.mean((modal_body[:, :, 1] < 65) & (modal_body[:, :, 2] > 170))
        modal_body_blue = np.mean(
            (modal_body[:, :, 0] >= 85)
            & (modal_body[:, :, 0] <= 125)
            & (modal_body[:, :, 1] > 60)
            & (modal_body[:, :, 2] > 90)
        )
        modal_buttons_green = np.mean(
            (modal_buttons[:, :, 0] >= 38)
            & (modal_buttons[:, :, 0] <= 92)
            & (modal_buttons[:, :, 1] > 70)
            & (modal_buttons[:, :, 2] > 80)
        )
        race_detail_modal = (
            modal_header_green > 0.35
            and modal_body_white > 0.55
            and modal_body_blue > 0.06
            and modal_buttons_green > 0.15
        )
        return bool(
            race_detail_modal
            or (
                card_white > 0.65
                and bottom_white > 0.65
                and (
                    card_blue > 0.14
                    or (entry_green > 0.30 and (card_blue + card_orange + card_red) > 0.15)
                    or (title_blue > 0.30 and entry_green > 0.40 and card_orange > 0.08)
                    or (
                        title_blue > 0.20
                        and (card_blue > 0.07 or (card_blue + card_orange) > 0.18)
                    )
                )
            )
        )

    @staticmethod
    def _looks_like_race_participant_list(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.05) : int(height * 0.10), int(width * 0.02) : int(width * 0.98)]
        rows = hsv[int(height * 0.15) : int(height * 0.82), int(width * 0.02) : int(width * 0.98)]
        bottom_button = hsv[int(height * 0.86) : int(height * 0.96), int(width * 0.20) : int(width * 0.80)]
        right_scroll = hsv[int(height * 0.12) : int(height * 0.86), int(width * 0.94) : int(width * 0.99)]
        if header.size == 0 or rows.size == 0 or bottom_button.size == 0 or right_scroll.size == 0:
            return False
        header_white = np.mean((header[:, :, 1] < 60) & (header[:, :, 2] > 175))
        rows_white = np.mean((rows[:, :, 1] < 60) & (rows[:, :, 2] > 175))
        button_green = np.mean(
            (bottom_button[:, :, 0] >= 38)
            & (bottom_button[:, :, 0] <= 92)
            & (bottom_button[:, :, 1] > 70)
            & (bottom_button[:, :, 2] > 80)
        )
        scroll_white = np.mean((right_scroll[:, :, 1] < 60) & (right_scroll[:, :, 2] > 175))
        return bool(header_white > 0.55 and rows_white > 0.65 and button_green > 0.45 and scroll_white > 0.50)

    @staticmethod
    def _looks_like_race_day_main_menu(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if SafeAdvancePlanner._looks_like_grand_masters_final_race_day_main_menu(img):
            return True
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        practice = hsv[
            int(height * 0.73) : int(height * 0.84),
            int(width * 0.26) : int(width * 0.74),
        ]
        date_badge = hsv[
            int(height * 0.02) : int(height * 0.16),
            0 : int(width * 0.23),
        ]
        top_goal = hsv[
            int(height * 0.05) : int(height * 0.18),
            int(width * 0.20) : int(width * 0.99),
        ]
        attribute_strip = hsv[
            int(height * 0.69) : int(height * 0.77),
            :,
        ]
        attribute_strip_gray = cv2.cvtColor(
            img[int(height * 0.69) : int(height * 0.77), :],
            cv2.COLOR_BGR2GRAY,
        )
        bottom_actions = hsv[int(height * 0.76) : int(height * 0.92), :]
        if (
            practice.size == 0
            or date_badge.size == 0
            or top_goal.size == 0
            or attribute_strip.size == 0
            or attribute_strip_gray.size == 0
            or bottom_actions.size == 0
        ):
            return False

        date_red = np.mean(
            ((date_badge[:, :, 0] <= 8) | (date_badge[:, :, 0] >= 170))
            & (date_badge[:, :, 1] > 70)
            & (date_badge[:, :, 2] > 100)
        )
        date_orange = np.mean(
            (date_badge[:, :, 0] >= 5)
            & (date_badge[:, :, 0] <= 30)
            & (date_badge[:, :, 1] > 70)
            & (date_badge[:, :, 2] > 100)
        )
        date_white = np.mean((date_badge[:, :, 1] < 70) & (date_badge[:, :, 2] > 170))
        top_goal_white = np.mean((top_goal[:, :, 1] < 70) & (top_goal[:, :, 2] > 170))
        top_goal_saturated = np.mean((top_goal[:, :, 1] > 70) & (top_goal[:, :, 2] > 90))
        practice_red = np.mean(
            ((practice[:, :, 0] <= 8) | (practice[:, :, 0] >= 170))
            & (practice[:, :, 1] > 70)
            & (practice[:, :, 2] > 100)
        )
        practice_orange = np.mean(
            (practice[:, :, 0] >= 5)
            & (practice[:, :, 0] <= 30)
            & (practice[:, :, 1] > 70)
            & (practice[:, :, 2] > 100)
        )
        practice_pink = np.mean(
            (practice[:, :, 0] >= 140)
            & (practice[:, :, 0] <= 175)
            & (practice[:, :, 1] > 50)
            & (practice[:, :, 2] > 100)
        )
        practice_blue = np.mean(
            (practice[:, :, 0] >= 85)
            & (practice[:, :, 0] <= 125)
            & (practice[:, :, 1] > 60)
            & (practice[:, :, 2] > 90)
        )
        bottom_green = np.mean(
            (bottom_actions[:, :, 0] >= 38)
            & (bottom_actions[:, :, 0] <= 92)
            & (bottom_actions[:, :, 1] > 70)
            & (bottom_actions[:, :, 2] > 80)
        )
        bottom_blue = np.mean(
            (bottom_actions[:, :, 0] >= 85)
            & (bottom_actions[:, :, 0] <= 125)
            & (bottom_actions[:, :, 1] > 60)
            & (bottom_actions[:, :, 2] > 90)
        )
        bottom_hot = np.mean(
            (
                ((bottom_actions[:, :, 0] <= 8) | (bottom_actions[:, :, 0] >= 170))
                | ((bottom_actions[:, :, 0] >= 5) & (bottom_actions[:, :, 0] <= 30))
                | ((bottom_actions[:, :, 0] >= 140) & (bottom_actions[:, :, 0] <= 175))
            )
            & (bottom_actions[:, :, 1] > 55)
            & (bottom_actions[:, :, 2] > 95)
        )
        attribute_strip_white = np.mean(
            (attribute_strip[:, :, 1] < 70)
            & (attribute_strip[:, :, 2] > 170)
        )
        attribute_strip_edge = np.mean(
            cv2.Canny(attribute_strip_gray, 80, 160) > 0
        )
        practice_hot = practice_red + practice_orange + practice_pink
        date_hot = date_red + date_orange
        return bool(
            0.25 < date_hot < 0.55
            and date_white > 0.30
            and top_goal_white > 0.55
            and top_goal_saturated < 0.40
            and practice_hot > 0.35
            and practice_red > 0.12
            and practice_orange < 0.40
            and practice_blue < 0.10
            and bottom_hot > 0.24
            and bottom_green < 0.08
            # Ordinary race-day pages carry a dense, pale five-attribute strip
            # immediately above the large race action.  Requiring its geometry
            # prevents a red training outfit from impersonating the race
            # button while keeping the dedicated final-race detector above.
            and attribute_strip_white > 0.42
            and attribute_strip_edge > 0.10
            # The race-day screen keeps the blue attribute strip immediately
            # above the action row.  Character/background drift can move that
            # strip into this coarse crop, so retain a bounded allowance while
            # the red date badge, goal panel, and hot race button remain the
            # primary positive evidence.
            and bottom_blue < 0.16
        )

    @staticmethod
    def _looks_like_race_day_primary_action(img: np.ndarray) -> bool:
        """Confirm the large hot-coloured race action in the bottom centre.

        The atomic main-menu recognizer can accept a race-day frame because the
        three remaining actions still resemble ordinary career controls.  This
        local confirmation keeps a generic race-day colour heuristic from
        overriding a genuine training menu while giving the mandatory race
        button precedence over that atomic false positive.
        """
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        action = hsv[
            int(height * 0.78) : int(height * 0.92),
            int(width * 0.28) : int(width * 0.70),
        ]
        if action.size == 0:
            return False
        hue = action[:, :, 0]
        saturation = action[:, :, 1]
        value = action[:, :, 2]
        red = np.mean(
            ((hue <= 8) | (hue >= 170))
            & (saturation > 70)
            & (value > 100)
        )
        orange = np.mean(
            (hue >= 5)
            & (hue <= 30)
            & (saturation > 70)
            & (value > 100)
        )
        pink = np.mean(
            (hue >= 140)
            & (hue <= 175)
            & (saturation > 50)
            & (value > 100)
        )
        blue = np.mean(
            (hue >= 85)
            & (hue <= 125)
            & (saturation > 60)
            & (value > 90)
        )
        # The animated sparkle layer shifts the same race button between
        # orange and pink from frame to frame.  Treat their combined hot-colour
        # mass as stable evidence while retaining an independent red floor.
        return bool(red > 0.20 and orange + pink > 0.23 and blue < 0.18)

    @staticmethod
    def _looks_like_wbc_race_hub(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(mask: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> float:
            crop = mask[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            if crop.size == 0:
                return 0.0
            return float(np.mean(crop))

        blue = (
            (hsv[:, :, 0] >= 85)
            & (hsv[:, :, 0] <= 130)
            & (hsv[:, :, 1] > 40)
            & (hsv[:, :, 2] > 80)
        )
        gold = (
            (hsv[:, :, 0] >= 15)
            & (hsv[:, :, 0] <= 35)
            & (hsv[:, :, 1] > 60)
            & (hsv[:, :, 2] > 120)
        )
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        pink = (
            (hsv[:, :, 0] >= 140)
            & (hsv[:, :, 0] <= 175)
            & (hsv[:, :, 1] > 60)
            & (hsv[:, :, 2] > 100)
        )
        white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 175)

        title_blue = ratio(blue, 0.15, 0.90, 0.08, 0.30)
        title_gold = ratio(gold, 0.15, 0.90, 0.08, 0.30)
        title_center_gold = ratio(gold, 0.25, 0.75, 0.10, 0.25)
        title_white = ratio(white, 0.15, 0.90, 0.08, 0.30)
        left_white = ratio(white, 0.04, 0.48, 0.78, 0.90)
        left_green = ratio(green, 0.04, 0.48, 0.78, 0.90)
        right_pink = ratio(pink, 0.48, 0.96, 0.76, 0.91)
        right_gold = ratio(gold, 0.48, 0.96, 0.76, 0.91)
        return bool(
            title_blue > 0.45
            and title_gold > 0.04
            and title_center_gold > 0.10
            and title_white < 0.25
            and (
                (left_white > 0.35 and left_green > 0.08)
                or (right_pink + right_gold > 0.30)
            )
        )

    @classmethod
    def _looks_like_wbc_entry_splash(cls, img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height <= width:
            return False
        if (
            not cls._looks_like_training_top_hud(img)
            or not cls._looks_like_training_animation_controls(img)
            or cls._looks_like_training_home_controls(img)
            or cls._looks_like_blocking_bottom_dialogue(img)
        ):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            y0: float,
            y1: float,
            x0: float,
            x1: float,
            mask: Callable[[np.ndarray], np.ndarray],
        ) -> float:
            region = hsv[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            return float(np.mean(mask(region))) if region.size else 0.0

        green = lambda region: (
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        gold = lambda region: (
            (region[:, :, 0] >= 15)
            & (region[:, :, 0] <= 35)
            & (region[:, :, 1] > 60)
            & (region[:, :, 2] > 120)
        )
        blue = lambda region: (
            (region[:, :, 0] >= 85)
            & (region[:, :, 0] <= 130)
            & (region[:, :, 1] > 40)
            & (region[:, :, 2] > 80)
        )
        white = lambda region: (
            (region[:, :, 1] < 60) & (region[:, :, 2] > 175)
        )
        return bool(
            ratio(0.16, 0.28, 0.00, 0.82, green) > 0.25
            and ratio(0.16, 0.28, 0.00, 0.82, blue) > 0.20
            and ratio(0.38, 0.56, 0.24, 0.76, gold) > 0.15
            and ratio(0.53, 0.67, 0.25, 0.75, green) > 0.05
            and ratio(0.53, 0.67, 0.25, 0.75, white) > 0.50
        )

    @staticmethod
    def _looks_like_grand_masters_final_race_day_main_menu(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height or not SafeAdvancePlanner._looks_like_race_day_badge(img):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left_action = hsv[
            int(height * 0.78) : int(height * 0.92),
            int(width * 0.02) : int(width * 0.30),
        ]
        center_action = hsv[
            int(height * 0.78) : int(height * 0.92),
            int(width * 0.28) : int(width * 0.70),
        ]
        right_action = hsv[
            int(height * 0.78) : int(height * 0.92),
            int(width * 0.70) : int(width * 0.98),
        ]
        action_band = hsv[int(height * 0.76) : int(height * 0.92), :]
        if (
            left_action.size == 0
            or center_action.size == 0
            or right_action.size == 0
            or action_band.size == 0
        ):
            return False

        left_cyan = np.mean(
            (left_action[:, :, 0] >= 80)
            & (left_action[:, :, 0] <= 110)
            & (left_action[:, :, 1] > 70)
            & (left_action[:, :, 2] > 90)
        )
        center_hot = np.mean(
            ((center_action[:, :, 0] <= 30) | (center_action[:, :, 0] >= 140))
            & (center_action[:, :, 1] > 55)
            & (center_action[:, :, 2] > 95)
        )
        right_red = np.mean(
            ((right_action[:, :, 0] <= 8) | (right_action[:, :, 0] >= 170))
            & (right_action[:, :, 1] > 70)
            & (right_action[:, :, 2] > 100)
        )
        band_green = np.mean(
            (action_band[:, :, 0] >= 38)
            & (action_band[:, :, 0] <= 92)
            & (action_band[:, :, 1] > 70)
            & (action_band[:, :, 2] > 80)
        )
        return bool(
            left_cyan > 0.12
            and center_hot > 0.60
            and right_red > 0.20
            and band_green < 0.05
        )

    @staticmethod
    def _looks_like_race_detail_action_page(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if perception.get("screen") != "bottom_action":
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_left = hsv[0 : int(height * 0.08), 0 : int(width * 0.20)]
        right_panel = hsv[
            int(height * 0.10) : int(height * 0.67),
            int(width * 0.48) : int(width * 0.98),
        ]
        bottom_buttons = hsv[
            int(height * 0.86) : int(height * 0.98),
            int(width * 0.15) : int(width * 0.85),
        ]
        if top_left.size == 0 or right_panel.size == 0 or bottom_buttons.size == 0:
            return False
        top_left_red = np.mean(
            ((top_left[:, :, 0] <= 8) | (top_left[:, :, 0] >= 170))
            & (top_left[:, :, 1] > 70)
            & (top_left[:, :, 2] > 100)
        )
        right_green = np.mean(
            (right_panel[:, :, 0] >= 38)
            & (right_panel[:, :, 0] <= 92)
            & (right_panel[:, :, 1] > 70)
            & (right_panel[:, :, 2] > 80)
        )
        right_white = np.mean((right_panel[:, :, 1] < 70) & (right_panel[:, :, 2] > 170))
        bottom_green = np.mean(
            (bottom_buttons[:, :, 0] >= 38)
            & (bottom_buttons[:, :, 0] <= 92)
            & (bottom_buttons[:, :, 1] > 70)
            & (bottom_buttons[:, :, 2] > 80)
        )
        return bool(
            top_left_red > 0.30
            and right_green > 0.05
            and right_white > 0.35
            and bottom_green > 0.10
        )

    @staticmethod
    def _looks_like_strong_portrait_race_detail(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        title = hsv[
            int(height * 0.015) : int(height * 0.065),
            int(width * 0.04) : int(width * 0.96),
        ]
        backdrop = hsv[
            int(height * 0.06) : int(height * 0.70),
            0 : int(width * 0.48),
        ]
        right_panel = hsv[
            int(height * 0.10) : int(height * 0.67),
            int(width * 0.48) : int(width * 0.98),
        ]
        bottom_buttons = hsv[
            int(height * 0.86) : int(height * 0.98),
            int(width * 0.15) : int(width * 0.85),
        ]
        if (
            title.size == 0
            or backdrop.size == 0
            or right_panel.size == 0
            or bottom_buttons.size == 0
        ):
            return False
        title_white = np.mean(
            (title[:, :, 1] < 70) & (title[:, :, 2] > 170)
        )
        backdrop_red = np.mean(
            ((backdrop[:, :, 0] <= 8) | (backdrop[:, :, 0] >= 170))
            & (backdrop[:, :, 1] > 70)
            & (backdrop[:, :, 2] > 100)
        )
        right_green = np.mean(
            (right_panel[:, :, 0] >= 38)
            & (right_panel[:, :, 0] <= 92)
            & (right_panel[:, :, 1] > 70)
            & (right_panel[:, :, 2] > 80)
        )
        right_white = np.mean(
            (right_panel[:, :, 1] < 70) & (right_panel[:, :, 2] > 170)
        )
        bottom_green = np.mean(
            (bottom_buttons[:, :, 0] >= 38)
            & (bottom_buttons[:, :, 0] <= 92)
            & (bottom_buttons[:, :, 1] > 70)
            & (bottom_buttons[:, :, 2] > 80)
        )
        bottom_dark = np.mean(bottom_buttons[:, :, 2] < 95)
        return bool(
            0.50 < title_white < 0.90
            and backdrop_red > 0.25
            and right_green > 0.10
            and 0.12 < right_white < 0.35
            and bottom_green > 0.12
            and bottom_dark > 0.35
        )

    @staticmethod
    def _looks_like_gur_race_entry(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if SafeAdvancePlanner._looks_like_blocking_bottom_dialogue(img):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        button = hsv[int(height * 0.72) : int(height * 0.94), int(width * 0.40) : int(width * 0.92)]
        title = hsv[int(height * 0.05) : int(height * 0.28), int(width * 0.05) : int(width * 0.95)]
        if button.size == 0 or title.size == 0:
            return False
        pink = np.mean(
            (button[:, :, 0] >= 140)
            & (button[:, :, 0] <= 175)
            & (button[:, :, 1] > 60)
            & (button[:, :, 2] > 90)
        )
        green = np.mean(
            (button[:, :, 0] >= 38)
            & (button[:, :, 0] <= 92)
            & (button[:, :, 1] > 70)
            & (button[:, :, 2] > 80)
        )
        red = np.mean(
            ((button[:, :, 0] <= 8) | (button[:, :, 0] >= 170))
            & (button[:, :, 1] > 70)
            & (button[:, :, 2] > 100)
        )
        white = np.mean((button[:, :, 1] < 60) & (button[:, :, 2] > 175))
        title_cyan = np.mean(
            (title[:, :, 0] >= 85)
            & (title[:, :, 0] <= 105)
            & (title[:, :, 1] > 60)
            & (title[:, :, 2] > 90)
        )
        title_white = np.mean((title[:, :, 1] < 60) & (title[:, :, 2] > 175))
        return bool(
            pink > 0.07
            and green > 0.06
            and red > 0.08
            and white > 0.25
            and title_cyan > 0.08
            and title_white > 0.08
        )

    @staticmethod
    def _looks_like_training_result_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower = hsv[int(height * 0.68) : int(height * 0.93), int(width * 0.02) : int(width * 0.98)]
        if lower.size == 0:
            return False
        white = np.mean((lower[:, :, 1] < 60) & (lower[:, :, 2] > 175))
        graywhite = np.mean((lower[:, :, 1] < 90) & (lower[:, :, 2] > 120))
        red_or_orange = np.mean(
            (
                ((lower[:, :, 0] <= 12) | ((lower[:, :, 0] >= 170) & (lower[:, :, 0] <= 179)))
                | ((lower[:, :, 0] >= 5) & (lower[:, :, 0] <= 30))
            )
            & (lower[:, :, 1] > 70)
            & (lower[:, :, 2] > 100)
        )
        return bool(
            (white > 0.16 and red_or_orange > 0.01)
            or (graywhite > 0.52 and red_or_orange > 0.05)
        )

    @staticmethod
    def _looks_like_training_hud_dialogue_box(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        y1, y2 = int(height * 0.55), int(height * 0.64)
        x1, x2 = int(width * 0.50), int(width * 0.80)
        region = hsv[y1:y2, x1:x2]
        gray_region = gray[y1:y2, x1:x2]
        if region.size == 0 or gray_region.size == 0:
            return False
        white = np.mean((region[:, :, 1] < 60) & (region[:, :, 2] > 175))
        graywhite = np.mean((region[:, :, 1] < 90) & (region[:, :, 2] > 120))
        edges = cv2.Canny(gray_region, 60, 150)
        edge_ratio = np.mean(edges > 0)
        if white > 0.84 and graywhite > 0.86 and edge_ratio < 0.13:
            return True

        banner = gray[int(height * 0.47) : int(height * 0.55), :]
        text_panel = hsv[
            int(height * 0.54) : int(height * 0.64),
            int(width * 0.30) : int(width * 0.83),
        ]
        if banner.size == 0 or text_panel.size == 0:
            return False
        banner_dark = np.mean(banner < 80)
        panel_white = np.mean((text_panel[:, :, 1] < 60) & (text_panel[:, :, 2] > 175))
        panel_graywhite = np.mean((text_panel[:, :, 1] < 90) & (text_panel[:, :, 2] > 120))
        return bool(banner_dark > 0.18 and panel_white > 0.78 and panel_graywhite > 0.82)

    @staticmethod
    def _looks_like_training_story_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        dialogue = hsv[
            int(height * 0.70) : int(height * 0.90),
            int(width * 0.02) : int(width * 0.98),
        ]
        bottom = hsv[int(height * 0.88) : height, :]
        top_left = hsv[0 : int(height * 0.20), 0 : int(width * 0.30)]
        if dialogue.size == 0 or bottom.size == 0 or top_left.size == 0:
            return False
        dialogue_white = np.mean(
            (dialogue[:, :, 1] < 65) & (dialogue[:, :, 2] > 170)
        )
        bottom_white = np.mean((bottom[:, :, 1] < 65) & (bottom[:, :, 2] > 170))
        hud_red = np.mean(
            ((top_left[:, :, 0] <= 8) | (top_left[:, :, 0] >= 170))
            & (top_left[:, :, 1] > 70)
            & (top_left[:, :, 2] > 120)
        )
        return bool(dialogue_white > 0.48 and bottom_white < 0.35 and hud_red > 0.08)

    @classmethod
    def _looks_like_dimmed_training_narration(cls, img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height or not cls._looks_like_training_top_hud(img):
            return False
        if not cls._looks_like_active_bottom_skip_button(img):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[
            int(height * 0.72) : int(height * 0.89),
            int(width * 0.03) : int(width * 0.97),
        ]
        if panel.size == 0:
            return False
        graywhite = np.mean((panel[:, :, 1] < 90) & (panel[:, :, 2] > 100))
        dark_text = np.mean(panel[:, :, 2] < 100)
        brown_text = np.mean(
            (panel[:, :, 0] >= 5)
            & (panel[:, :, 0] <= 30)
            & (panel[:, :, 1] > 35)
            & (panel[:, :, 2] > 45)
        )
        return bool(graywhite > 0.75 and dark_text > 0.08 and brown_text > 0.02)

    @classmethod
    def _race_day_story_dialogue_action(
        cls,
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> dict[str, Any] | None:
        if not cls._looks_like_race_day_story_dialogue(img):
            return None
        if cls._menu_panel_close_button(img) is not None:
            return None
        height, width = img.shape[:2]
        skip_state = SpeedTogglePlanner._bottom_skip_state(img)
        if skip_state == "on":
            return None
        if (
            skip_state == "off"
            or cls._looks_like_bottom_skip_toggle_off_control(img)
            or cls._looks_like_race_story_skip_toggle_off_control(img)
        ):
            return cls._enable_bottom_skip_toggle(evidence, img)
        return cls._advance_bottom_dialogue(evidence, img)

    @classmethod
    def _looks_like_race_day_story_dialogue(cls, img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width or not cls._looks_like_blocking_bottom_dialogue(img):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_left = hsv[0 : int(height * 0.18), 0 : int(width * 0.24)]
        goal_band = hsv[int(height * 0.05) : int(height * 0.17), int(width * 0.20) : int(width * 0.98)]
        if top_left.size == 0 or goal_band.size == 0:
            return False
        red = (
            ((top_left[:, :, 0] <= 8) | (top_left[:, :, 0] >= 170))
            & (top_left[:, :, 1] > 70)
            & (top_left[:, :, 2] > 110)
        )
        band_white = np.mean((goal_band[:, :, 1] < 70) & (goal_band[:, :, 2] > 170))
        band_red_or_orange = np.mean(
            (
                ((goal_band[:, :, 0] <= 8) | (goal_band[:, :, 0] >= 170))
                | ((goal_band[:, :, 0] >= 5) & (goal_band[:, :, 0] <= 35))
            )
            & (goal_band[:, :, 1] > 70)
            & (goal_band[:, :, 2] > 100)
        )
        return bool(np.mean(red) > 0.12 and band_white > 0.30 and band_red_or_orange > 0.05)

    @staticmethod
    def _looks_like_race_day_badge(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        badge_text = hsv[int(height * 0.03) : int(height * 0.13), int(width * 0.04) : int(width * 0.12)]
        if badge_text.size == 0:
            return False
        orange = np.mean(
            (badge_text[:, :, 0] >= 5)
            & (badge_text[:, :, 0] <= 35)
            & (badge_text[:, :, 1] > 70)
            & (badge_text[:, :, 2] > 100)
        )
        red = np.mean(
            ((badge_text[:, :, 0] <= 8) | (badge_text[:, :, 0] >= 170))
            & (badge_text[:, :, 1] > 70)
            & (badge_text[:, :, 2] > 110)
        )
        white = np.mean((badge_text[:, :, 1] < 70) & (badge_text[:, :, 2] > 170))
        return bool(orange > 0.16 and red > 0.25 and white > 0.30)

    @staticmethod
    def _looks_like_blocking_bottom_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.70) : int(height * 0.92), int(width * 0.02) : int(width * 0.98)]
        nameplate = hsv[int(height * 0.68) : int(height * 0.78), int(width * 0.02) : int(width * 0.55)]
        if bottom.size == 0 or nameplate.size == 0:
            return False
        bottom_graywhite = np.mean((bottom[:, :, 1] < 110) & (bottom[:, :, 2] > 100))
        nameplate_green = np.mean(
            (nameplate[:, :, 0] >= 38)
            & (nameplate[:, :, 0] <= 92)
            & (nameplate[:, :, 1] > 50)
            & (nameplate[:, :, 2] > 60)
        )
        nameplate_blue = np.mean(
            (nameplate[:, :, 0] >= 90)
            & (nameplate[:, :, 0] <= 125)
            & (nameplate[:, :, 1] > 40)
            & (nameplate[:, :, 2] > 60)
        )
        return bool(bottom_graywhite > 0.60 and (nameplate_green > 0.20 or nameplate_blue > 0.20))

    @staticmethod
    def _global_home_greeting_dialogue_button(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        if screen_text.get("dangerous") or screen_text.get("resource_terms"):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if "开始训练前" not in normalized or "得先去买" not in normalized:
            return None
        height, width = img.shape[:2]
        if (height, width) != (1280, 720):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        dialogue = hsv[
            int(height * 0.55) : int(height * 0.68),
            int(width * 0.08) : int(width * 0.72),
        ]
        if dialogue.size == 0:
            return None
        dialogue_light = np.mean(
            (dialogue[:, :, 1] < 110) & (dialogue[:, :, 2] > 120)
        )
        if dialogue_light < 0.70:
            return None
        return 360, 800

    @staticmethod
    def _looks_like_main_menu_short_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        red = (
            ((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 170))
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 100)
        )
        white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170)
        orange = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 35)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 100)
        )
        gray = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 80) & (hsv[:, :, 2] < 190)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )

        def ratio(mask: np.ndarray, x0: float, x1: float, y0: float, y1: float) -> float:
            crop = mask[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]
            if crop.size == 0:
                return 0.0
            return float(np.mean(crop))

        top_red = ratio(red, 0.0, 0.30, 0.0, 0.20)
        mid_white = ratio(white, 0.30, 0.83, 0.54, 0.64)
        dialogue_white = ratio(white, 0.02, 0.98, 0.68, 0.93)
        bottom_green = ratio(green, 0.20, 0.48, 0.925, 0.985)
        top_green = ratio(green, 0.0, 1.0, 0.0, 0.14)
        effect_badge_orange = ratio(orange, 0.0, 0.22, 0.48, 0.56)
        effect_badge_white = ratio(white, 0.0, 0.22, 0.48, 0.56)
        effect_badge_green = ratio(green, 0.0, 0.22, 0.48, 0.56)
        connect_orange = ratio(orange, 0.70, 0.99, 0.04, 0.13)
        connect_gray = ratio(gray, 0.70, 0.99, 0.04, 0.13)
        connect_white = ratio(white, 0.70, 0.99, 0.04, 0.13)
        final_phase_context = (
            (
                0.243 < effect_badge_orange < 0.28
                and effect_badge_white > 0.45
                and effect_badge_green < 0.02
            )
            or (connect_orange > 0.15 and connect_gray > 0.25 and connect_white < 0.45)
        )
        return bool(
            top_red > 0.12
            and mid_white > 0.40
            and dialogue_white > 0.40
            and bottom_green < 0.35
            and top_green < 0.08
            and final_phase_context
        )

    @staticmethod
    def _looks_like_hot_nameplate_bottom_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.70) : int(height * 0.92), int(width * 0.02) : int(width * 0.98)]
        nameplate = hsv[int(height * 0.68) : int(height * 0.78), int(width * 0.02) : int(width * 0.55)]
        if bottom.size == 0 or nameplate.size == 0:
            return False
        bottom_graywhite = np.mean((bottom[:, :, 1] < 110) & (bottom[:, :, 2] > 100))
        nameplate_hot = np.mean(
            (
                ((nameplate[:, :, 0] <= 8) | (nameplate[:, :, 0] >= 170))
                | ((nameplate[:, :, 0] >= 5) & (nameplate[:, :, 0] <= 30))
                | ((nameplate[:, :, 0] >= 140) & (nameplate[:, :, 0] <= 175))
            )
            & (nameplate[:, :, 1] > 50)
            & (nameplate[:, :, 2] > 80)
        )
        return bool(bottom_graywhite > 0.60 and nameplate_hot > 0.20)

    @staticmethod
    def _looks_like_green_nameplate_bottom_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.70) : int(height * 0.92), int(width * 0.02) : int(width * 0.98)]
        nameplate = hsv[int(height * 0.68) : int(height * 0.78), int(width * 0.02) : int(width * 0.55)]
        if bottom.size == 0 or nameplate.size == 0:
            return False
        bottom_graywhite = np.mean((bottom[:, :, 1] < 110) & (bottom[:, :, 2] > 100))
        nameplate_green = np.mean(
            (nameplate[:, :, 0] >= 38)
            & (nameplate[:, :, 0] <= 92)
            & (nameplate[:, :, 1] > 50)
            & (nameplate[:, :, 2] > 60)
        )
        return bool(bottom_graywhite > 0.60 and nameplate_green > 0.20)

    @staticmethod
    def _looks_like_story_dialogue_with_record_controls(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        nameplate = hsv[int(height * 0.72) : int(height * 0.83), int(width * 0.02) : int(width * 0.50)]
        left_skip = hsv[int(height * 0.93) : int(height * 0.995), int(width * 0.20) : int(width * 0.48)]
        record_area = hsv[int(height * 0.93) : int(height * 0.995), int(width * 0.58) : int(width * 0.78)]
        menu_area = hsv[int(height * 0.93) : int(height * 0.995), int(width * 0.78) : int(width * 0.98)]
        if nameplate.size == 0 or left_skip.size == 0 or record_area.size == 0 or menu_area.size == 0:
            return False
        nameplate_green = np.mean(
            (nameplate[:, :, 0] >= 38)
            & (nameplate[:, :, 0] <= 92)
            & (nameplate[:, :, 1] > 50)
            & (nameplate[:, :, 2] > 60)
        )
        left_skip_white = np.mean((left_skip[:, :, 1] < 65) & (left_skip[:, :, 2] > 170))
        record_graywhite = np.mean((record_area[:, :, 1] < 90) & (record_area[:, :, 2] > 120))
        menu_white = np.mean((menu_area[:, :, 1] < 65) & (menu_area[:, :, 2] > 170))
        return bool(
            nameplate_green > 0.20
            and left_skip_white > 0.18
            and record_graywhite > 0.40
            and menu_white > 0.45
        )

    @staticmethod
    def _looks_like_friend_training_effect_frame(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mid = hsv[int(height * 0.18) : int(height * 0.72), :]
        if mid.size == 0:
            return False
        green = np.mean(
            (mid[:, :, 0] >= 38)
            & (mid[:, :, 0] <= 92)
            & (mid[:, :, 1] > 70)
            & (mid[:, :, 2] > 80)
        )
        orange = np.mean(
            (mid[:, :, 0] >= 5)
            & (mid[:, :, 0] <= 25)
            & (mid[:, :, 1] > 70)
            & (mid[:, :, 2] > 100)
        )
        white = np.mean((mid[:, :, 1] < 65) & (mid[:, :, 2] > 170))
        return bool(green > 0.12 and orange > 0.25 and white < 0.35)

    @staticmethod
    def _looks_like_friendship_training_trigger_animation(
        img: np.ndarray, *, allow_white_flash: bool = False,
    ) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mid = hsv[int(height * 0.18) : int(height * 0.72), :]
        lower_text = hsv[int(height * 0.78) : int(height * 0.96), int(width * 0.20) : int(width * 0.90)]
        lower_controls = hsv[int(height * 0.90) : height, :]
        if mid.size == 0 or lower_text.size == 0 or lower_controls.size == 0:
            return False
        mid_orange = np.mean(
            (mid[:, :, 0] >= 5)
            & (mid[:, :, 0] <= 25)
            & (mid[:, :, 1] > 70)
            & (mid[:, :, 2] > 100)
        )
        text_cyan = np.mean(
            (lower_text[:, :, 0] >= 80)
            & (lower_text[:, :, 0] <= 100)
            & (lower_text[:, :, 1] > 70)
            & (lower_text[:, :, 2] > 140)
        )
        text_pink = np.mean(
            (lower_text[:, :, 0] >= 135)
            & (lower_text[:, :, 0] <= 170)
            & (lower_text[:, :, 1] > 50)
            & (lower_text[:, :, 2] > 140)
        )
        text_white = np.mean((lower_text[:, :, 1] < 75) & (lower_text[:, :, 2] > 175))
        controls_green = np.mean(
            (lower_controls[:, :, 0] >= 38)
            & (lower_controls[:, :, 0] <= 92)
            & (lower_controls[:, :, 1] > 70)
            & (lower_controls[:, :, 2] > 80)
        )
        controls_white = np.mean((lower_controls[:, :, 1] < 65) & (lower_controls[:, :, 2] > 170))
        classic_friendship_splash = (
            mid_orange > 0.25
            and text_cyan > 0.04
            and text_pink > 0.04
            and text_white > 0.14
            and controls_green < 0.12
            and controls_white < 0.30
        )
        diagonal_friendship_splash = (
            mid_orange > 0.05
            and text_cyan > 0.04
            and text_pink > 0.14
            and text_white > 0.14
            and controls_green < 0.24
            and controls_white < 0.30
        )
        bold_text_friendship_splash = (
            mid_orange > 0.12
            and text_cyan > 0.05
            and text_pink > 0.03
            and text_white > 0.28
            and controls_green < 0.12
            and controls_white > 0.42
        )
        white_flash_splash = False
        if (
            allow_white_flash
            and mid_orange > 0.35
            and text_white > 0.60
            and controls_green < 0.15
            and controls_white > 0.50
        ):
            # The cut-in's long rising white border is distinct from a flat
            # result/dialogue panel.  Color alone does not authorize this wait.
            top = hsv[: int(height * 0.13)]
            white_mask = np.uint8((top[:, :, 1] < 65) & (top[:, :, 2] > 190)) * 255
            lines = cv2.HoughLinesP(
                cv2.Canny(white_mask, 50, 150), 1, np.pi / 180,
                threshold=max(1, int(width * 0.11)),
                minLineLength=int(width * 0.65), maxLineGap=int(width * 0.03),
            )
            white_flash_splash = lines is not None and any(
                x1 - x0 > width * 0.65 and -0.25 < (y1 - y0) / (x1 - x0) < -0.05
                for x0, y0, x1, y1 in lines[:, 0]
            )
        fullscreen_friendship_splash = False
        if (
            allow_white_flash
            and mid_orange > 0.15
            and text_cyan > 0.015
            and text_white > 0.65
            and controls_green < 0.08
            and controls_white > 0.50
        ):
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            lines = cv2.HoughLinesP(
                cv2.Canny(gray, 80, 180), 1, np.pi / 180,
                threshold=max(1, int(width * 0.11)),
                minLineLength=int(width * 0.80), maxLineGap=int(width * 0.08),
            )
            fullscreen_friendship_splash = lines is not None and any(
                x1 - x0 > width * 0.80
                and -0.20 < (y1 - y0) / (x1 - x0) < -0.08
                and height * 0.38 < (y0 + y1) / 2 < height * 0.55
                for x0, y0, x1, y1 in lines[:, 0]
            )
        split_friendship_splash = False
        if (
            allow_white_flash
            and mid_orange > 0.05
            and text_cyan > 0.03
            and text_pink > 0.03
            and text_white > 0.35
            and controls_green < 0.08
            and controls_white > 0.35
        ):
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            lines = cv2.HoughLinesP(
                cv2.Canny(gray, 80, 180), 1, np.pi / 180,
                threshold=max(1, int(width * 0.11)),
                minLineLength=int(width * 0.80), maxLineGap=int(width * 0.08),
            )
            split_friendship_splash = lines is not None and any(
                x1 - x0 > width * 0.80
                and -0.20 < (y1 - y0) / (x1 - x0) < -0.08
                and height * 0.38 < (y0 + y1) / 2 < height * 0.55
                for x0, y0, x1, y1 in lines[:, 0]
            )
        return bool(
            classic_friendship_splash
            or diagonal_friendship_splash
            or bold_text_friendship_splash
            or white_flash_splash
            or fullscreen_friendship_splash
            or split_friendship_splash
        )

    @staticmethod
    def _looks_like_landscape_racecourse_establishing_loading(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width <= height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        sky = hsv[0 : int(height * 0.45), :]
        if sky.size == 0:
            return False
        sky_blue = np.mean(
            (sky[:, :, 0] >= 85)
            & (sky[:, :, 0] <= 115)
            & (sky[:, :, 1] > 35)
            & (sky[:, :, 2] > 120)
        )
        full_green = np.mean(
            (hsv[:, :, 0] >= 35)
            & (hsv[:, :, 0] <= 95)
            & (hsv[:, :, 1] > 45)
            & (hsv[:, :, 2] > 70)
        )
        return bool(sky_blue > 0.58 and full_green < 0.18)

    @staticmethod
    def _looks_like_inheritance_performance_scene(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if height < width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        middle = hsv[int(height * 0.42) : int(height * 0.73), int(width * 0.02) : int(width * 0.98)]
        lower = hsv[int(height * 0.68) : int(height * 0.91), int(width * 0.02) : int(width * 0.98)]
        if middle.size == 0 or lower.size == 0:
            return False
        middle_gold = np.mean(
            (middle[:, :, 0] >= 16)
            & (middle[:, :, 0] <= 38)
            & (middle[:, :, 1] > 80)
            & (middle[:, :, 2] > 130)
        )
        middle_white = np.mean((middle[:, :, 1] < 65) & (middle[:, :, 2] > 170))
        lower_deep_gold = np.mean(
            (lower[:, :, 0] >= 18)
            & (lower[:, :, 0] <= 32)
            & (lower[:, :, 1] > 110)
            & (lower[:, :, 2] > 140)
        )
        lower_white = np.mean((lower[:, :, 1] < 65) & (lower[:, :, 2] > 170))
        return bool(
            (middle_gold > 0.18 and middle_white > 0.45)
            or (lower_deep_gold > 0.12 and lower_white > 0.45)
        )

    @staticmethod
    def _looks_like_race_scene_controls(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        if str(perception.get("screen")) != "portrait_ui":
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower = hsv[int(height * 0.80) : height, :]
        controls = hsv[int(height * 0.90) : int(height * 0.995), int(width * 0.55) : int(width * 0.99)]
        if lower.size == 0 or controls.size == 0:
            return False
        lower_green = np.mean(
            (lower[:, :, 0] >= 38)
            & (lower[:, :, 0] <= 92)
            & (lower[:, :, 1] > 70)
            & (lower[:, :, 2] > 80)
        )
        controls_white = np.mean((controls[:, :, 1] < 65) & (controls[:, :, 2] > 170))
        controls_orange = np.mean(
            (controls[:, :, 0] >= 5)
            & (controls[:, :, 0] <= 30)
            & (controls[:, :, 1] > 60)
            & (controls[:, :, 2] > 90)
        )
        return bool(lower_green > 0.45 and controls_white > 0.18 and controls_orange > 0.04)

    @staticmethod
    def _looks_like_large_bottom_dialogue(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        dialogue = hsv[
            int(height * 0.70) : int(height * 0.91),
            int(width * 0.02) : int(width * 0.98),
        ]
        if dialogue.size == 0:
            return False
        dialogue_white = np.mean((dialogue[:, :, 1] < 65) & (dialogue[:, :, 2] > 170))
        dialogue_brown = np.mean(
            (dialogue[:, :, 0] >= 5)
            & (dialogue[:, :, 0] <= 30)
            & (dialogue[:, :, 1] > 45)
            & (dialogue[:, :, 2] > 70)
        )
        return bool(dialogue_white > 0.42 and dialogue_brown > 0.01)

    @staticmethod
    def _looks_like_training_top_hud(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_left = hsv[0 : int(height * 0.18), 0 : int(width * 0.30)]
        if top_left.size == 0:
            return False
        red = np.mean(
            ((top_left[:, :, 0] <= 8) | (top_left[:, :, 0] >= 170))
            & (top_left[:, :, 1] > 70)
            & (top_left[:, :, 2] > 100)
        )
        white = np.mean((top_left[:, :, 1] < 65) & (top_left[:, :, 2] > 170))
        orange = np.mean(
            (top_left[:, :, 0] >= 5)
            & (top_left[:, :, 0] <= 30)
            & (top_left[:, :, 1] > 60)
            & (top_left[:, :, 2] > 100)
        )
        return bool(0.08 < red < 0.50 and white > 0.12 and orange < 0.25)

    @classmethod
    def _looks_like_training_action_animation(
        cls,
        img: np.ndarray,
        *,
        route_policy: SafeAdvanceRoutePolicy | None,
        training_check: TrainingMenuPrecheckResult,
        perception: dict[str, Any],
        scenario: str = "grand_masters",
    ) -> bool:
        ss_animation_source = (scenario == "larc" and route_policy is not None
            and route_policy.pending_action_id in {
                "safe_advance.larc_ss_confirm", "wait:larc_ss_animation", "wait:bottom_skip_double_enabled"})
        if (
            route_policy is None
            or route_policy.source != "pending"
            or (route_policy.pending_action_id
                not in {"training.execute", "safe_advance.rest_confirm", "safe_advance.rest_outing_confirm"}
                and not ss_animation_source)
            or route_policy.action_source_sha256 is None
            or not route_policy.allows_observed_state("loading")
        ):
            return False
        option_layout = detect_event_option_layout(img)
        top_hud = (
            bool(training_check.evidence.get("scenario_layout", {}).get("blue_header"))
            if scenario == "larc" else cls._looks_like_training_top_hud(img)
        )
        # L'Arc's pink/cream diagonal wipe may retain the bottom controls.
        # Check only the scene; its pale background resembles a dialogue panel.
        text = str((perception.get("screen_text") or {}).get("normalized_text", ""))
        if (scenario == "larc" and top_hud and not training_check.accepted
                and (route_policy.pending_action_id == "training.execute" or ss_animation_source)
                and re.fullmatch(r"[0-9a-f]{64}", route_policy.action_source_sha256)
                and (perception.get("screen") == "unknown"
                     or (perception.get("screen") == "portrait_ui"
                         and cls._looks_like_training_animation_controls(img))
                     # The training HUD's "参赛条件" may override the visual screen.
                     or (route_policy.pending_action_id == "training.execute"
                         and perception.get("screen") == "race_participant_list"
                         and perception.get("screen_text_override") is True
                         and ((perception.get("screen_text") or {}).get("primary_hint") or {}).get("reason")
                         == "race_participant_text"
                         and cls._looks_like_training_animation_controls(img)))
                and (text.startswith(("群星赛\n", "超星赛\n")) if ss_animation_source else "训练" in text)
                and ("距离代表交流赛还剩" in text
                     or all(label in text for label in ("回合结束后", "代表交流赛开始")))
                and img.shape[:2] in {(1280, 720), (2560, 1440)}):
            scene = cv2.cvtColor(cv2.resize(img, (720, 1280)), cv2.COLOR_BGR2HSV)[200:1180]
            pink = ((scene[:, :, 0] >= 145) & (scene[:, :, 0] <= 170)
                    & (scene[:, :, 1] >= 15) & (scene[:, :, 1] <= 65)
                    & (scene[:, :, 2] >= 235))
            cyan = ((scene[:, :, 0] >= 80) & (scene[:, :, 0] <= 110)
                    & (scene[:, :, 1] >= 15) & (scene[:, :, 1] <= 65)
                    & (scene[:, :, 2] >= 235))
            accent = pink | cyan
            cream = ((scene[:, :, 0] >= 20) & (scene[:, :, 0] <= 40)
                     & (scene[:, :, 1] <= 45) & (scene[:, :, 2] >= 235))
            if (np.mean(accent) > 0.20 and np.mean(cream) > 0.20
                    and np.mean(accent | cream) > 0.98
                    and np.mean(accent[:-120, :-120] == accent[120:, 120:]) > 0.97
                    and np.mean(accent[:, :-120] != accent[:, 120:]) > 0.20):
                return True
        return bool(
            (not ss_animation_source or text.startswith(("群星赛\n", "超星赛\n")))
            and top_hud
            and cls._looks_like_training_animation_controls(img)
            and not training_check.accepted
            and not cls._looks_like_training_home_controls(img)
            and (ss_animation_source or not cls._looks_like_blocking_bottom_dialogue(img))
            and not cls._looks_like_training_story_dialogue(img)
            and not cls._looks_like_training_hud_dialogue_box(img)
            and not (event_option_rows_ready(img) if scenario == "larc" else option_layout["points"])
            and not cls._looks_like_confirm_modal_panel(img)
            and perception.get("screen") not in {"modal_confirm", "event_choice"}
        )

    @classmethod
    def _looks_like_route_bound_wisdom_acquisition_transition(
        cls,
        img: np.ndarray,
        *,
        route_policy: SafeAdvanceRoutePolicy | None,
        training_check: TrainingMenuPrecheckResult,
    ) -> bool:
        if (
            route_policy is None
            or route_policy.source != "pending"
            or route_policy.pending_action_id != "training.execute"
            or re.fullmatch(
                r"[0-9a-f]{64}", str(route_policy.action_source_sha256 or "")
            )
            is None
            or not route_policy.allows_observed_state("loading")
            or training_check.accepted
        ):
            return False
        height, width = img.shape[:2]
        if height <= width or not cls._looks_like_active_bottom_skip_button(img):
            return False
        if (
            cls._looks_like_blocking_bottom_dialogue(img)
            or cls._looks_like_confirm_modal_panel(img)
        ):
            return False
        graph_bgr = img[
            int(height * 0.38) : int(height * 0.78),
            int(width * 0.05) : int(width * 0.95),
        ]
        if graph_bgr.size == 0:
            return False
        graph = cv2.cvtColor(graph_bgr, cv2.COLOR_BGR2HSV)
        graph_blur = float(
            cv2.Laplacian(
                cv2.cvtColor(graph_bgr, cv2.COLOR_BGR2GRAY),
                cv2.CV_64F,
            ).var()
        )
        graph_yellow = float(
            np.mean(
                (graph[:, :, 0] >= 15)
                & (graph[:, :, 0] <= 45)
                & (graph[:, :, 1] > 70)
                & (graph[:, :, 2] > 110)
            )
        )
        graph_blue = float(
            np.mean(
                (graph[:, :, 0] >= 95)
                & (graph[:, :, 0] <= 130)
                & (graph[:, :, 1] > 60)
                & (graph[:, :, 2] > 80)
            )
        )
        graph_white = float(
            np.mean((graph[:, :, 1] < 65) & (graph[:, :, 2] > 170))
        )
        full_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        full_dark = float(np.mean(full_hsv[:, :, 2] < 120))
        return bool(
            graph_blur < 8.0
            and graph_yellow > 0.06
            and graph_blue > 0.10
            and graph_white < 0.02
            and full_dark > 0.30
        )

    @classmethod
    def _looks_like_route_bound_training_cut_in(
        cls,
        img: np.ndarray,
        *,
        route_policy: SafeAdvanceRoutePolicy | None,
        training_check: TrainingMenuPrecheckResult,
        perception: dict[str, Any],
    ) -> bool:
        if (
            route_policy is None
            or route_policy.source != "pending"
            or route_policy.pending_action_id != "training.execute"
            or re.fullmatch(
                r"[0-9a-f]{64}", str(route_policy.action_source_sha256 or "")
            )
            is None
            or not route_policy.allows_observed_state("loading")
            or training_check.accepted
        ):
            return False
        screen_text = perception.get("screen_text")
        if not isinstance(screen_text, dict):
            return False
        lines = str(screen_text.get("normalized_text", "")).splitlines()
        normalized_text = "".join(lines)
        option_layout = detect_event_option_layout(img)
        if (
            len(lines) <= 3
            and (
                ("发" in normalized_text and "训练" in normalized_text)
                or normalized_text.rstrip("!") == "友情训练"
            )
            and cls._looks_like_friendship_training_trigger_animation(
                img,
                allow_white_flash=True,
            )
            and not option_layout["points"]
            and not cls._looks_like_training_home_controls(img)
            and not cls._looks_like_blocking_bottom_dialogue(img)
            and not cls._looks_like_training_story_dialogue(img)
            and not cls._looks_like_training_hud_dialogue_box(img)
            and not cls._looks_like_confirm_modal_panel(img)
        ):
            return True
        if lines[:1] != ["训练"]:
            return False
        if "触发" in normalized_text:
            return bool(
                cls._looks_like_training_top_hud(img)
                and not option_layout["points"]
                and not cls._looks_like_training_home_controls(img)
                and not cls._looks_like_blocking_bottom_dialogue(img)
                and not cls._looks_like_training_story_dialogue(img)
                and not cls._looks_like_training_hud_dialogue_box(img)
                and not cls._looks_like_confirm_modal_panel(img)
            )
        if perception.get("screen") != "game_home" or perception.get("buttons"):
            return False
        height, width = img.shape[:2]
        if height <= width:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower_glow = hsv[
            int(height * 0.78) : int(height * 0.96),
            int(width * 0.20) : int(width * 0.90),
        ]
        bottom = hsv[int(height * 0.90) : height, :]
        if lower_glow.size == 0 or bottom.size == 0:
            return False
        lower_white = float(
            np.mean((lower_glow[:, :, 1] < 75) & (lower_glow[:, :, 2] > 175))
        )
        bottom_white = float(
            np.mean((bottom[:, :, 1] < 65) & (bottom[:, :, 2] > 170))
        )
        bottom_green = float(
            np.mean(
                (bottom[:, :, 0] >= 38)
                & (bottom[:, :, 0] <= 92)
                & (bottom[:, :, 1] > 70)
                & (bottom[:, :, 2] > 80)
            )
        )
        return bool(
            lower_white > 0.55
            and bottom_white > 0.45
            and bottom_green < 0.12
            and not option_layout["points"]
            and not cls._looks_like_training_home_controls(img)
            and not cls._looks_like_confirm_modal_panel(img)
        )

    @staticmethod
    def _looks_like_bottom_skip_off_pill(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[
            int(height * 0.94) : int(height * 0.995),
            int(width * 0.22) : int(width * 0.47),
        ]
        if region.size == 0:
            return False
        white = np.mean((region[:, :, 1] < 65) & (region[:, :, 2] > 170))
        brown = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 30)
            & (region[:, :, 1] > 45)
            & (region[:, :, 2] > 70)
        )
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        return bool(white > 0.25 and brown > 0.025 and green < 0.05)

    @staticmethod
    def _looks_like_dialogue_continue_arrow(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        arrow = hsv[
            int(height * 0.84) : int(height * 0.91),
            int(width * 0.80) : int(width * 0.93),
        ]
        if arrow.size == 0:
            return False
        green = np.mean(
            (arrow[:, :, 0] >= 38)
            & (arrow[:, :, 0] <= 92)
            & (arrow[:, :, 1] > 70)
            & (arrow[:, :, 2] > 80)
        )
        blue = np.mean(
            (arrow[:, :, 0] >= 100)
            & (arrow[:, :, 0] <= 140)
            & (arrow[:, :, 1] > 40)
            & (arrow[:, :, 2] > 70)
        )
        return bool(green > 0.06 or blue > 0.08)

    @classmethod
    def _looks_like_support_card_event_dialogue(
        cls,
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if "协助卡事件" not in normalized:
            return False
        return bool(
            cls._looks_like_training_top_hud(img)
            and cls._looks_like_large_bottom_dialogue(img)
            and cls._looks_like_dialogue_continue_arrow(img)
            and not cls._looks_like_training_home_controls(img)
            and not cls._looks_like_confirm_modal_panel(img)
            and not detect_event_option_layout(img)["points"]
        )

    @staticmethod
    def _looks_like_confirm_modal_panel(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.22) : int(height * 0.30), int(width * 0.02) : int(width * 0.98)]
        confirm_zone = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.50) : int(width * 0.95)]
        if header.size == 0 or confirm_zone.size == 0:
            return False
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        confirm_green = np.mean(
            (confirm_zone[:, :, 0] >= 38)
            & (confirm_zone[:, :, 0] <= 92)
            & (confirm_zone[:, :, 1] > 70)
            & (confirm_zone[:, :, 2] > 80)
        )
        return bool(header_green > 0.30 and confirm_green > 0.20)

    @staticmethod
    def _looks_like_training_success_result(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        title = hsv[int(height * 0.15) : int(height * 0.25), : int(width * 0.75)]
        cards = hsv[int(height * 0.72) : int(height * 0.94), int(width * 0.02) : int(width * 0.98)]
        if title.size == 0 or cards.size == 0:
            return False
        title_green = np.mean(
            (title[:, :, 0] >= 38)
            & (title[:, :, 0] <= 92)
            & (title[:, :, 1] > 70)
            & (title[:, :, 2] > 80)
        )
        card_blue = np.mean(
            (cards[:, :, 0] >= 85)
            & (cards[:, :, 0] <= 125)
            & (cards[:, :, 1] > 45)
            & (cards[:, :, 2] > 70)
        )
        card_gray = np.mean((cards[:, :, 1] < 55) & (cards[:, :, 2] > 80) & (cards[:, :, 2] < 210))
        card_white = np.mean((cards[:, :, 1] < 60) & (cards[:, :, 2] > 175))
        card_orange = np.mean(
            (cards[:, :, 0] >= 5)
            & (cards[:, :, 0] <= 35)
            & (cards[:, :, 1] > 60)
            & (cards[:, :, 2] > 120)
        )
        return bool(
            title_green > 0.15
            and card_blue > 0.16
            and card_gray > 0.12
            and card_white > 0.35
            and card_orange > 0.07
        )

    @staticmethod
    def _bottom_center_primary_green_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        buttons = perception.get("buttons", [])
        if not isinstance(buttons, list):
            return None
        for button in buttons:
            if not isinstance(button, dict):
                continue
            if button.get("kind") != "primary_green":
                continue
            center = button.get("center")
            bbox = button.get("bbox")
            if (
                not isinstance(center, tuple)
                or len(center) != 2
                or not isinstance(bbox, tuple)
                or len(bbox) != 4
            ):
                continue
            center_x, center_y = center
            _, _, box_w, box_h = bbox
            if (
                width * 0.30 <= center_x <= width * 0.70
                and center_y >= height * 0.78
                and box_w >= width * 0.22
                and box_h >= height * 0.04
            ):
                return int(center_x), int(center_y)
        return None

    @staticmethod
    def _looks_like_active_bottom_skip_button(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[int(height * 0.925) : int(height * 0.985), int(width * 0.20) : int(width * 0.48)]
        if region.size == 0:
            return False
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        white = np.mean((region[:, :, 1] < 60) & (region[:, :, 2] > 175))
        return bool(green > 0.22 and white < 0.50)

    @staticmethod
    def _looks_like_bottom_skip_toggle_off_control(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[int(height * 0.925) : int(height * 0.985), int(width * 0.20) : int(width * 0.48)]
        if region.size == 0:
            return False
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        white = np.mean((region[:, :, 1] < 70) & (region[:, :, 2] > 170))
        gray = np.mean((region[:, :, 1] < 60) & (region[:, :, 2] > 80) & (region[:, :, 2] < 210))
        brown = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 30)
            & (region[:, :, 1] > 50)
            & (region[:, :, 2] > 70)
        )
        orange = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 35)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 120)
        )
        return bool(green < 0.05 and white > 0.15 and gray > 0.25 and (brown > 0.02 or orange > 0.02))

    @staticmethod
    def _looks_like_live_bottom_skip_toggle_off_control(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[int(height * 0.925) : int(height * 0.985), int(width * 0.20) : int(width * 0.48)]
        if region.size == 0:
            return False
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        white = np.mean((region[:, :, 1] < 70) & (region[:, :, 2] > 170))
        gray_edge = np.mean(
            (region[:, :, 1] < 60)
            & (region[:, :, 2] > 80)
            & (region[:, :, 2] < 210)
        )
        brown_or_orange = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 35)
            & (region[:, :, 1] > 50)
            & (region[:, :, 2] > 70)
        )
        return bool(
            green < 0.05
            and white > 0.35
            and 0.02 < gray_edge < 0.18
            and brown_or_orange > 0.05
        )

    @staticmethod
    def _looks_like_race_story_skip_toggle_off_control(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[int(height * 0.925) : int(height * 0.985), int(width * 0.20) : int(width * 0.48)]
        if region.size == 0:
            return False
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        white = np.mean((region[:, :, 1] < 70) & (region[:, :, 2] > 170))
        brown_or_orange = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 35)
            & (region[:, :, 1] > 50)
            & (region[:, :, 2] > 70)
        )
        return bool(green < 0.05 and white > 0.20 and brown_or_orange > 0.035)

    @staticmethod
    def _looks_like_ready_wisdom_fragment_panel(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.28) : int(height * 0.56), int(width * 0.02) : int(width * 0.21)]
        if panel.size == 0:
            return False
        saturated = np.mean((panel[:, :, 1] > 80) & (panel[:, :, 2] > 100))
        gray = np.mean((panel[:, :, 1] < 50) & (panel[:, :, 2] > 80) & (panel[:, :, 2] < 230))
        red = np.mean(
            ((panel[:, :, 0] <= 8) | (panel[:, :, 0] >= 170))
            & (panel[:, :, 1] > 70)
            & (panel[:, :, 2] > 100)
        )
        yellow = np.mean(
            (panel[:, :, 0] >= 18)
            & (panel[:, :, 0] <= 38)
            & (panel[:, :, 1] > 70)
            & (panel[:, :, 2] > 120)
        )
        return bool(saturated > 0.34 and gray < 0.10 and red > 0.12 and yellow > 0.10)
