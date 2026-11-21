"""Career goals, clock-cancel recovery, race summaries, and terminal settlement."""
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


class SettlementHandlerMixin:
    """Goal progress/failure, clock cancel, and career-end settlement handlers."""

    @staticmethod
    def _career_end_skill_acquisition_button(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        has_title_text = "养成结束确认" in normalized
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(box: tuple[float, float, float, float], mask: np.ndarray) -> float:
            x0, y0, x1, y1 = box
            crop = mask[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            return float(np.mean(crop)) if crop.size else 0.0

        cyan = (
            (hsv[:, :, 0] >= 75)
            & (hsv[:, :, 0] <= 105)
            & (hsv[:, :, 1] > 80)
            & (hsv[:, :, 2] > 120)
        )
        pink = (
            (hsv[:, :, 0] >= 145)
            & (hsv[:, :, 0] <= 179)
            & (hsv[:, :, 1] > 50)
            & (hsv[:, :, 2] > 120)
        )
        white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 175)
        purple = (
            (hsv[:, :, 0] >= 115)
            & (hsv[:, :, 0] <= 160)
            & (hsv[:, :, 1] > 40)
            & (hsv[:, :, 2] > 60)
        )
        title = (0.0, 0.0, 0.32, 0.051)
        left = (0.055, 0.758, 0.486, 0.875)
        right = (0.514, 0.758, 0.917, 0.875)
        if not (
            (has_title_text or ratio(title, purple) > 0.20)
            and
            ratio(left, cyan) > 0.18
            and ratio(left, pink) < 0.08
            and ratio(left, white) > 0.20
            and ratio(right, pink) > 0.25
            and ratio(right, cyan) < 0.12
            and ratio(right, white) > 0.20
        ):
            return None
        return round(width * 0.287), round(height * 0.817)

    @staticmethod
    def _career_end_finish_button(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if SafeAdvancePlanner._career_end_skill_acquisition_button(
            screen_text,
            img,
        ) is None:
            return None
        height, width = img.shape[:2]
        return round(width * 0.715), round(height * 0.817)

    @staticmethod
    def _inheritance_factor_confirm_button(img: np.ndarray, *, named_scene=False) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        scene_like = SafeAdvancePlanner._looks_like_inheritance_performance_scene(img)
        skip_present = SpeedTogglePlanner._bottom_skip_state(img) in {"on", "off"}
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # Sample the orb interior rather than joining it to gold clothing and
        # particles through a large connected component.
        orb = hsv[round(height * 970 / 1280):round(height * 1125 / 1280),
                  round(width * 285 / 720):round(width * 435 / 720)]
        if (scene_like or named_scene) and skip_present and orb.size:
            orange = ((orb[:, :, 0] >= 8) & (orb[:, :, 0] <= 40)
                      & (orb[:, :, 1] > 110) & (orb[:, :, 2] > 180))
            if float(orange.mean()) > .80:
                return round(width * .5), round(height * 1047 / 1280)
        side_left = hsv[
            int(height * 0.05) : int(height * 0.75),
            0 : int(width * 0.18),
        ]
        side_right = hsv[
            int(height * 0.05) : int(height * 0.75),
            int(width * 0.82) : width,
        ]
        side_pixels = np.concatenate(
            (side_left.reshape(-1, 3), side_right.reshape(-1, 3)),
            axis=0,
        )
        closeup_side_navy = np.mean(
            (side_pixels[:, 0] >= 95)
            & (side_pixels[:, 0] <= 135)
            & (side_pixels[:, 1] > 30)
            & (side_pixels[:, 2] < 140)
        )
        x0 = int(width * 0.15)
        x1 = int(width * 0.85)
        y0 = int(height * 0.65)
        y1 = int(height * 0.92)
        roi = hsv[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        mask = (
            (roi[:, :, 0] >= 8)
            & (roi[:, :, 0] <= 45)
            & (roi[:, :, 1] > 70)
            & (roi[:, :, 2] > 120)
        ).astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        n_labels, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        min_area = int(width * height * 0.02)
        best: tuple[int, int, int] | None = None
        for index in range(1, n_labels):
            area = int(stats[index, cv2.CC_STAT_AREA])
            if area < min_area:
                continue
            bw = int(stats[index, cv2.CC_STAT_WIDTH])
            bh = int(stats[index, cv2.CC_STAT_HEIGHT])
            if bh <= 0:
                continue
            aspect = bw / bh
            max_aspect = 1.35 if scene_like else 1.65
            if not 0.65 <= aspect <= max_aspect:
                continue
            if not scene_like and not (
                skip_present
                and closeup_side_navy > 0.55
                and area >= int(width * height * 0.04)
                and bw >= int(width * 0.32)
                and bh >= int(height * 0.16)
            ):
                continue
            cx = int(round(float(centroids[index][0]) + x0))
            cy = int(round(float(centroids[index][1]) + y0))
            if not (int(width * 0.32) <= cx <= int(width * 0.68)):
                continue
            if not (int(height * 0.70) <= cy <= int(height * 0.91)):
                continue
            if best is None or area > best[2]:
                best = (cx, cy, area)
        if best is None:
            return None
        return best[0], best[1]

    @staticmethod
    def _looks_like_goal_achievement_animation(
        perception: dict[str, Any],
        img: np.ndarray,
        *, goal_layout_only: bool = False,
    ) -> bool:
        height, width = img.shape[:2]
        if width > height or (not goal_layout_only
                and str(perception.get("screen")) not in {"bottom_action", "portrait_ui"}):
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.18) : int(height * 0.83), int(width * 0.01) : int(width * 0.99)]
        center = hsv[int(height * 0.28) : int(height * 0.62), int(width * 0.10) : int(width * 0.90)]
        lines = str(perception.get("screen_text", {}).get("normalized_text", "")).splitlines()
        larc_goal = (len(lines) >= 2 and is_goal_achievement_title(lines[0])
                     and lines[1] in LARC_SYSTEM_GOAL_IDS)
        # L'Arc navigation only needs the distinctive white/green card. OCR
        # of its decorative title is not proof required for a free Continue.
        larc_goal = larc_goal or goal_layout_only
        if larc_goal:
            center = np.concatenate((
                hsv[int(height * 0.28):int(height * 0.62), int(width * 0.10):int(width * 0.38)],
                hsv[int(height * 0.28):int(height * 0.62), int(width * 0.62):int(width * 0.90)],
            ), axis=1)
        top_band = hsv[int(height * 0.10) : int(height * 0.30), :]
        button = hsv[int(height * 0.82) : int(height * 0.92), int(width * 0.25) : int(width * 0.75)]
        if any(region.size == 0 for region in (panel, center, top_band, button)):
            return False
        if goal_layout_only and not diagonal_goal_band(top_band, height):
            return False
        panel_white = np.mean((panel[:, :, 1] < 65) & (panel[:, :, 2] > 170))
        center_white = np.mean((center[:, :, 1] < 65) & (center[:, :, 2] > 170))
        top_green = np.mean((top_band[:, :, 0] >= 38) & (top_band[:, :, 0] <= 92)
            & (top_band[:, :, 1] > 70) & (top_band[:, :, 2] > 80))
        button_green = np.mean((button[:, :, 0] >= 38) & (button[:, :, 0] <= 92)
            & (button[:, :, 1] > 70) & (button[:, :, 2] > 80))
        # Keep the existing goal-panel thresholds; the continue slot is still empty.
        return bool(panel_white > (0.60 if larc_goal else 0.72) and center_white > 0.85 and top_green > 0.18
            and button_green == 0)

    @staticmethod
    def _goal_achieved_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
        *, goal_layout_only: bool = False,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        screen = str(perception.get("screen"))
        screen_text = perception.get("screen_text")
        goal_lines = (
            str(screen_text.get("normalized_text", "")).splitlines()
            if isinstance(screen_text, dict)
            else []
        )
        text_bound_current_layout = (
            len(goal_lines) == 3
            and is_goal_achievement_title(goal_lines[0])
            and goal_lines[2] == "继续"
            and (
                (screen == "game_home" and re.fullmatch(
                    r"(?:进入前[0-9]+|获得第[0-9]+名|获得[0-9]+名粉丝)", goal_lines[1]) is not None)
                or (screen in {"bottom_action", "portrait_ui"} and goal_lines[1] in LARC_SYSTEM_GOAL_IDS)
            )
        )
        all_goals_layout = (screen in {"bottom_action", "portrait_ui"}
                            and goal_lines == ["养成目标达成!", "已达成所有目标!", "继续"])
        if all_goals_layout:
            text_bound_current_layout = True
        if (screen not in {"bottom_action", "portrait_ui"}
                and not text_bound_current_layout and not goal_layout_only):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        panel = hsv[int(height * 0.18) : int(height * 0.83), int(width * 0.01) : int(width * 0.99)]
        top_band = hsv[int(height * 0.10) : int(height * 0.30), :]
        bottom_band = hsv[int(height * 0.65) : int(height * 0.88), :]
        center = hsv[int(height * 0.28) : int(height * 0.62), int(width * 0.10) : int(width * 0.90)]
        if goal_layout_only or all_goals_layout or (text_bound_current_layout and
                goal_lines[1] in LARC_SYSTEM_GOAL_IDS):
            # The all-goals card has a chibi character in its center. Verify
            # the white card on both sides without making outfit color a gate.
            center = np.concatenate((
                hsv[int(height * 0.28):int(height * 0.62), int(width * 0.10):int(width * 0.38)],
                hsv[int(height * 0.28):int(height * 0.62), int(width * 0.62):int(width * 0.90)],
            ), axis=1)
        button = hsv[int(height * 0.82) : int(height * 0.92), int(width * 0.25) : int(width * 0.75)]
        if panel.size == 0 or top_band.size == 0 or bottom_band.size == 0 or center.size == 0 or button.size == 0:
            return None
        if goal_layout_only and not diagonal_goal_band(top_band, height):
            return None
        panel_white = np.mean((panel[:, :, 1] < 65) & (panel[:, :, 2] > 170))
        center_white = np.mean((center[:, :, 1] < 65) & (center[:, :, 2] > 170))
        top_green = np.mean(
            (top_band[:, :, 0] >= 38)
            & (top_band[:, :, 0] <= 92)
            & (top_band[:, :, 1] > 70)
            & (top_band[:, :, 2] > 80)
        )
        bottom_green = np.mean(
            (bottom_band[:, :, 0] >= 38)
            & (bottom_band[:, :, 0] <= 92)
            & (bottom_band[:, :, 1] > 70)
            & (bottom_band[:, :, 2] > 80)
        )
        button_green = np.mean(
            (button[:, :, 0] >= 38)
            & (button[:, :, 0] <= 92)
            & (button[:, :, 1] > 70)
            & (button[:, :, 2] > 80)
        )
        if not (
            panel_white > (0.60 if goal_layout_only or text_bound_current_layout else 0.72)
            and center_white > 0.85
            and top_green > 0.18
            and bottom_green > 0.14
            and button_green > 0.30
        ):
            return None
        return int(width * 0.50), int(height * 0.868)

    @staticmethod
    def _goal_remaining_result_skip_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if str(perception.get("screen")) not in {"bottom_action", "portrait_ui", "training_menu"}:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center_text = hsv[
            int(height * 0.35) : int(height * 0.62),
            int(width * 0.05) : int(width * 0.95),
        ]
        skip_region = hsv[
            int(height * 0.92) : int(height * 0.99),
            int(width * 0.20) : int(width * 0.48),
        ]
        if center_text.size == 0 or skip_region.size == 0:
            return None
        center_white = np.mean((center_text[:, :, 1] < 65) & (center_text[:, :, 2] > 170))
        center_blue = np.mean(
            (center_text[:, :, 0] >= 85)
            & (center_text[:, :, 0] <= 125)
            & (center_text[:, :, 1] > 45)
            & (center_text[:, :, 2] > 100)
        )
        skip_green = np.mean(
            (skip_region[:, :, 0] >= 38)
            & (skip_region[:, :, 0] <= 92)
            & (skip_region[:, :, 1] > 70)
            & (skip_region[:, :, 2] > 80)
        )
        if not (center_white > 0.25 and center_blue > 0.30 and skip_green > 0.30):
            return None
        return int(width * 0.355), int(height * 0.956)

    @staticmethod
    def _goal_progress_list_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
        *, scenario: str | None = None,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        legacy_screen = str(perception.get("screen")) in {"bottom_action", "portrait_ui"}
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_band = hsv[0 : int(height * 0.12), :]
        bottom_band = hsv[int(height * 0.78) : int(height * 0.95), :]
        list_area = hsv[int(height * 0.25) : int(height * 0.78), int(width * 0.02) : int(width * 0.98)]
        coach_bubble = hsv[int(height * 0.10) : int(height * 0.22), int(width * 0.15) : int(width * 0.98)]
        button = hsv[int(height * 0.82) : int(height * 0.92), int(width * 0.25) : int(width * 0.75)]
        wbc_cards = hsv[int(height * 0.40) : int(height * 0.72), int(width * 0.03) : int(width * 0.97)]
        wbc_title = hsv[int(height * 0.18) : int(height * 0.42), int(width * 0.12) : int(width * 0.88)]
        if (
            top_band.size == 0
            or bottom_band.size == 0
            or list_area.size == 0
            or coach_bubble.size == 0
            or button.size == 0
            or wbc_cards.size == 0
            or wbc_title.size == 0
        ):
            return None
        top_green = np.mean(
            (top_band[:, :, 0] >= 38)
            & (top_band[:, :, 0] <= 92)
            & (top_band[:, :, 1] > 70)
            & (top_band[:, :, 2] > 80)
        )
        bottom_green = np.mean(
            (bottom_band[:, :, 0] >= 38)
            & (bottom_band[:, :, 0] <= 92)
            & (bottom_band[:, :, 1] > 70)
            & (bottom_band[:, :, 2] > 80)
        )
        list_white = np.mean((list_area[:, :, 1] < 65) & (list_area[:, :, 2] > 170))
        if scenario == "larc":
            # Completed L'Arc goal cards are neutral gray, not white.
            list_white += np.mean((list_area[:, :, 1] < 25)
                                  & (list_area[:, :, 2] >= 100)
                                  & (list_area[:, :, 2] <= 150))
        coach_white = np.mean((coach_bubble[:, :, 1] < 65) & (coach_bubble[:, :, 2] > 170))
        button_green = np.mean(
            (button[:, :, 0] >= 38)
            & (button[:, :, 0] <= 92)
            & (button[:, :, 1] > 70)
            & (button[:, :, 2] > 80)
        )
        top_white = np.mean((top_band[:, :, 1] < 65) & (top_band[:, :, 2] > 170))
        wbc_card_coral = np.mean(
            ((wbc_cards[:, :, 0] <= 8) | (wbc_cards[:, :, 0] >= 170))
            & (wbc_cards[:, :, 1] > 70)
            & (wbc_cards[:, :, 2] > 120)
        )
        wbc_title_orange = np.mean(
            (wbc_title[:, :, 0] >= 5)
            & (wbc_title[:, :, 0] <= 35)
            & (wbc_title[:, :, 1] > 60)
            & (wbc_title[:, :, 2] > 100)
        )
        legacy_goal_progress = (
            legacy_screen
            and top_green > 0.18
            and bottom_green > 0.25
            and list_white > 0.85
            and coach_white > 0.75
            and button_green > 0.45
        )
        wbc_progress = (
            top_white > 0.75
            and bottom_green > 0.25
            and list_white > 0.55
            and button_green > 0.40
            and wbc_card_coral > 0.25
            and wbc_title_orange > 0.10
        )
        if not (legacy_goal_progress or wbc_progress):
            return None
        return int(width * 0.50), int(height * 0.868)

    @staticmethod
    def _looks_like_team_formation_page(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_left = hsv[0 : int(height * 0.07), 0 : int(width * 0.35)]
        card_area = hsv[int(height * 0.35) : int(height * 0.75), :]
        center_button = hsv[int(height * 0.78) : int(height * 0.89), int(width * 0.25) : int(width * 0.75)]
        back_button = hsv[int(height * 0.78) : int(height * 0.89), 0 : int(width * 0.25)]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if (
            top_left.size == 0
            or card_area.size == 0
            or center_button.size == 0
            or back_button.size == 0
            or bottom_nav.size == 0
        ):
            return False

        top_purple = np.mean(
            (top_left[:, :, 0] >= 120)
            & (top_left[:, :, 0] <= 155)
            & (top_left[:, :, 1] > 30)
            & (top_left[:, :, 2] > 70)
        )
        top_white = np.mean((top_left[:, :, 1] < 65) & (top_left[:, :, 2] > 170))
        cards_white = np.mean((card_area[:, :, 1] < 65) & (card_area[:, :, 2] > 170))
        cards_orange = np.mean(
            (card_area[:, :, 0] >= 5)
            & (card_area[:, :, 0] <= 30)
            & (card_area[:, :, 1] > 70)
            & (card_area[:, :, 2] > 100)
        )
        center_green = np.mean(
            (center_button[:, :, 0] >= 38)
            & (center_button[:, :, 0] <= 92)
            & (center_button[:, :, 1] > 70)
            & (center_button[:, :, 2] > 80)
        )
        back_white = np.mean((back_button[:, :, 1] < 65) & (back_button[:, :, 2] > 170))
        nav_white = np.mean((bottom_nav[:, :, 1] < 65) & (bottom_nav[:, :, 2] > 170))
        nav_green = np.mean(
            (bottom_nav[:, :, 0] >= 38)
            & (bottom_nav[:, :, 0] <= 92)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 80)
        )
        nav_orange = np.mean(
            (bottom_nav[:, :, 0] >= 5)
            & (bottom_nav[:, :, 0] <= 30)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 100)
        )
        return bool(
            top_purple > 0.20
            and top_white > 0.15
            and cards_white > 0.45
            and cards_orange > 0.06
            and center_green > 0.30
            and back_white > 0.75
            and nav_white > 0.50
            and nav_green > 0.05
            and nav_orange > 0.08
        )

    @staticmethod
    def _final_loss_result_next_button(
        img: np.ndarray, perception: dict[str, Any], screen_text: dict[str, Any] | None,
        route_policy: SafeAdvanceRoutePolicy | None,
    ) -> tuple[int, int] | None:
        if (not isinstance(screen_text, dict) or route_policy is None or route_policy.source != "pending"
            or route_policy.pending_action_id != "safe_advance.clock_retry_cancel"
            or not route_policy.action_source_sha256 or not route_policy.allows_observed_state("dialogue")
            or img.shape[:2] != (1280, 720)):
            return None
        lines = str(screen_text.get("normalized_text") or "").splitlines()
        if (not {"荣耀女神杯", "特殊", "下一页", "重新挑战"}.issubset(lines)
            or re.search(r"第(?:[2-9]|1[0-8])名", "".join(lines)) is None):
            return None
        from core.auto_trainer.race_route import RaceRoutePlanner

        if not RaceRoutePlanner._looks_like_ranking_list_result(img):
            return None
        point = RaceRoutePlanner._race_result_continue_button(perception, img, screen_text)
        if point is None or not (360 < point[0] < 684 and 1100 < point[1] < 1254):
            return None
        return point

    @staticmethod
    def _clock_retry_cancel_decision(
        img: np.ndarray, screen_text: dict[str, Any] | None, evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not isinstance(screen_text, dict):
            return None
        text = str(screen_text.get("normalized_text") or "")
        if (screen_text.get("dangerous") is not True
            or "是否使用闹钟进行重新挑战" not in text or "取消" not in text.splitlines()):
            return None
        decision = SafeAdvancePlanner._final_loss_recovery_decision(img, evidence)
        return decision if decision is not None and decision.get("reason") == "cancel_clock_retry" else None

    @staticmethod
    def _final_loss_recovery_decision(
        img: np.ndarray,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Recognize the post-loss chain before generic green-button handling."""

        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            y0: float,
            y1: float,
            x0: float,
            x1: float,
            mask: np.ndarray,
        ) -> float:
            region = hsv[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]
            return float(np.mean(mask(region))) if region.size else 0.0

        def green(region: np.ndarray) -> np.ndarray:
            return (
                (region[:, :, 0] >= 38)
                & (region[:, :, 0] <= 92)
                & (region[:, :, 1] > 70)
                & (region[:, :, 2] > 80)
            )

        def white(region: np.ndarray) -> np.ndarray:
            return (region[:, :, 1] < 65) & (region[:, :, 2] > 175)

        def pink(region: np.ndarray) -> np.ndarray:
            return (
                (region[:, :, 0] >= 145)
                & (region[:, :, 0] <= 178)
                & (region[:, :, 1] > 35)
                & (region[:, :, 2] > 120)
            )

        if SafeAdvancePlanner._looks_like_unreadable_resource_modal(img):
            return SafeAdvancePlanner._stop("resource_prompt_unreadable", evidence=evidence)

        # Retry confirmation: green title + clock badge + paired cancel/retry actions.
        if (
            ratio(0.23, 0.30, 0.02, 0.98, green) > 0.45
            and ratio(0.31, 0.77, 0.02, 0.98, white) > 0.60
            and ratio(0.55, 0.65, 0.20, 0.42, pink) > 0.04
            and ratio(0.68, 0.77, 0.08, 0.48, white) > 0.55
            and ratio(0.68, 0.77, 0.52, 0.92, green) > 0.45
        ):
            return SafeAdvancePlanner._tap_decision(
                "cancel_clock_retry",
                "safe_advance.clock_retry_cancel",
                (round(width * 0.28), round(height * 0.717)),
                evidence,
            )

        # Without trusted text, a resource-spend modal is fail-closed.  This deliberately
        # requires a centered item card as well as the paired actions, so ordinary confirms
        # and skill dialogs retain their established routes.
        if (
            ratio(0.23, 0.30, 0.02, 0.98, green) > 0.45
            and ratio(0.31, 0.77, 0.02, 0.98, white) > 0.60
            and ratio(0.68, 0.77, 0.08, 0.48, white) > 0.55
            and ratio(0.68, 0.77, 0.52, 0.92, green) > 0.45
            and ratio(0.53, 0.66, 0.20, 0.42, pink) > 0.08
        ):
            return SafeAdvancePlanner._stop("resource_prompt_unreadable", evidence=evidence)

        # Trainee detail is a full record sheet: header, stat strip, tabs, then one close action.
        if (
            ratio(0.02, 0.08, 0.01, 0.99, green) > 0.40
            and ratio(0.24, 0.31, 0.02, 0.98, green) > 0.28
            and ratio(0.40, 0.45, 0.02, 0.98, white) > 0.65
            and ratio(0.89, 0.97, 0.25, 0.75, white) > 0.70
        ):
            return SafeAdvancePlanner._tap_decision(
                "close_trainee_detail",
                "safe_advance.trainee_detail_close",
                (int(width * 0.50), int(height * 0.927)),
                evidence,
            )

        # Scenario score refresh is a celebratory blue scene with two green ribbons and one continue.
        if (
            ratio(0.00, 0.23, 0.00, 1.00, lambda r: (r[:, :, 0] >= 90) & (r[:, :, 0] <= 125) & (r[:, :, 1] > 55) & (r[:, :, 2] > 120)) > 0.30
            and ratio(0.23, 0.34, 0.00, 1.00, green) > 0.18
            and ratio(0.54, 0.68, 0.00, 1.00, green) > 0.15
            and ratio(0.84, 0.92, 0.25, 0.75, green) > 0.45
            and SafeAdvancePlanner._career_opening_closeup_fast_forward_button(
                img
            )
            is None
        ):
            return SafeAdvancePlanner._tap_decision(
                "continue_scenario_score_record",
                "safe_advance.scenario_score_record_continue",
                (int(width * 0.50), int(height * 0.868)),
                evidence,
            )

        # Support rewards have five populated card slots, two named reward sections, and a bottom next page.
        support_card_slots = [
            ratio(0.20, 0.34, x0, x1, lambda r: (r[:, :, 1] > 90) & (r[:, :, 2] > 100))
            for x0, x1 in ((0.05, 0.21), (0.23, 0.39), (0.41, 0.57), (0.59, 0.75), (0.77, 0.93))
        ]
        if (
            ratio(0.02, 0.10, 0.20, 0.80, green) > 0.15
            and ratio(0.10, 0.15, 0.02, 0.98, green) > 0.30
            and ratio(0.38, 0.42, 0.02, 0.98, green) > 0.40
            and ratio(0.48, 0.52, 0.02, 0.98, green) > 0.25
            and min(support_card_slots) > 0.30
            and ratio(0.42, 0.48, 0.02, 0.98, white) > 0.25
            and ratio(0.52, 0.86, 0.02, 0.98, white) > 0.35
            and ratio(0.88, 0.97, 0.25, 0.75, green) > 0.45
        ):
            decision = SafeAdvancePlanner._tap_decision(
                "next_support_reward_page",
                "safe_advance.support_reward_next_page",
                (int(width * 0.50), int(height * 0.936)),
                evidence,
            )
            decision["post_tap_confirmation"] = "career_complete_global_home"
            return decision
        return None

    @staticmethod
    def _race_result_summary_continue_button(
        img: np.ndarray,
        screen_text: dict[str, Any] | None,
    ) -> tuple[int, int] | None:
        """Race-result summary pages (prize/fan breakdown) are repeatedly
        misclassified as dialogue boxes.  Detect the summary layout and return
        the real bottom-right continue button."""
        if screen_text is None:
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if not any(marker in normalized for marker in ("粉丝数", "奖金", "获得奖励")):
            return None
        if any(
            marker in normalized
            for marker in ("参加比赛", "赛事一览", "参赛计划")
        ):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.875) : int(height * 0.985), :, :]
        if bottom.size == 0:
            return None
        green = (
            (bottom[:, :, 0] >= 35)
            & (bottom[:, :, 0] <= 90)
            & (bottom[:, :, 1] > 70)
            & (bottom[:, :, 2] > 80)
        ).astype(np.uint8)
        white = ((bottom[:, :, 1] < 60) & (bottom[:, :, 2] > 175)).astype(np.uint8)
        best_green: tuple[int, int, int] | None = None
        best_white: tuple[int, int, int] | None = None
        for mask, min_area, best in (
            (green, 1500, "green"),
            (white, 3000, "white"),
        ):
            count, _, stats, cents = cv2.connectedComponentsWithStats(
                mask, connectivity=8
            )
            for index in range(1, count):
                area = int(stats[index, cv2.CC_STAT_AREA])
                if area < min_area:
                    continue
                point = (
                    int(round(float(cents[index, 0]))),
                    int(round(float(cents[index, 1]))) + int(height * 0.875),
                )
                candidate = (area, point[0], point[1])
                if best == "green":
                    if best_green is None or area > best_green[0]:
                        best_green = candidate
                elif best_white is None or area > best_white[0]:
                    best_white = candidate
        chosen = best_green or best_white
        if chosen is None:
            return None
        return (chosen[1], chosen[2])

    @staticmethod
    def _trophy_reward_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.23) : int(height * 0.30), int(width * 0.01) : int(width * 0.99)]
        body = hsv[int(height * 0.30) : int(height * 0.76), int(width * 0.01) : int(width * 0.99)]
        close = hsv[int(height * 0.68) : int(height * 0.76), int(width * 0.25) : int(width * 0.75)]
        if header.size == 0 or body.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        body_white = np.mean((body[:, :, 1] < 65) & (body[:, :, 2] > 170))
        close_white = np.mean((close[:, :, 1] < 65) & (close[:, :, 2] > 170))
        close_brown = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 30)
            & (close[:, :, 1] > 50)
            & (close[:, :, 2] > 80)
        )
        if header_green > 0.45 and body_white > 0.80 and close_white > 0.85 and close_brown > 0.015:
            return int(width * 0.50), int(height * 0.72)
        return None

    @staticmethod
    def _honor_title_confirm_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.02) : int(height * 0.08), int(width * 0.01) : int(width * 0.99)]
        title_list = hsv[int(height * 0.08) : int(height * 0.78), int(width * 0.01) : int(width * 0.99)]
        checkbox_text = hsv[int(height * 0.78) : int(height * 0.88), int(width * 0.20) : int(width * 0.80)]
        bottom = hsv[int(height * 0.86) : int(height * 0.98), int(width * 0.20) : int(width * 0.80)]
        if header.size == 0 or title_list.size == 0 or checkbox_text.size == 0 or bottom.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        list_green = np.mean(
            (title_list[:, :, 0] >= 38)
            & (title_list[:, :, 0] <= 92)
            & (title_list[:, :, 1] > 70)
            & (title_list[:, :, 2] > 80)
        )
        list_white = np.mean((title_list[:, :, 1] < 65) & (title_list[:, :, 2] > 170))
        checkbox_white = np.mean((checkbox_text[:, :, 1] < 65) & (checkbox_text[:, :, 2] > 170))
        checkbox_brown_or_orange = np.mean(
            (
                ((checkbox_text[:, :, 0] >= 5) & (checkbox_text[:, :, 0] <= 30))
                & (checkbox_text[:, :, 1] > 50)
                & (checkbox_text[:, :, 2] > 80)
            )
        )
        bottom_green = np.mean(
            (bottom[:, :, 0] >= 38)
            & (bottom[:, :, 0] <= 92)
            & (bottom[:, :, 1] > 70)
            & (bottom[:, :, 2] > 80)
        )
        if not (
            header_green > 0.65
            and list_green > 0.15
            and list_white > 0.55
            and checkbox_white > 0.65
            and checkbox_brown_or_orange > 0.05
            and bottom_green > 0.25
        ):
            return None

        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.25 <= cx <= width * 0.75):
                continue
            if cy < height * 0.86:
                continue
            if not (width * 0.25 <= w <= width * 0.55):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if not candidates:
            return None
        _area, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _record_modal_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.025) : int(height * 0.075), int(width * 0.01) : int(width * 0.99)]
        panel = hsv[int(height * 0.08) : int(height * 0.90), int(width * 0.02) : int(width * 0.98)]
        blue_tab = hsv[int(height * 0.09) : int(height * 0.16), int(width * 0.03) : int(width * 0.97)]
        close = hsv[int(height * 0.895) : int(height * 0.965), int(width * 0.25) : int(width * 0.75)]
        if header.size == 0 or panel.size == 0 or blue_tab.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        panel_white = np.mean((panel[:, :, 1] < 70) & (panel[:, :, 2] > 170))
        tab_blue = np.mean(
            (blue_tab[:, :, 0] >= 85)
            & (blue_tab[:, :, 0] <= 125)
            & (blue_tab[:, :, 1] > 50)
            & (blue_tab[:, :, 2] > 90)
        )
        close_white = np.mean((close[:, :, 1] < 70) & (close[:, :, 2] > 170))
        close_brown = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 30)
            & (close[:, :, 1] > 50)
            & (close[:, :, 2] > 70)
        )
        if header_green > 0.40 and panel_white > 0.80 and tab_blue > 0.25 and close_white > 0.75 and close_brown > 0.01:
            return int(width * 0.50), int(height * 0.925)
        return None

    @staticmethod
    def _goal_achieved_animation_overlay_point(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        title = hsv[
            int(height * 0.20) : int(height * 0.42),
            int(width * 0.12) : int(width * 0.88),
        ]
        panel = hsv[
            int(height * 0.52) : int(height * 0.64),
            int(width * 0.04) : int(width * 0.96),
        ]
        if title.size == 0 or panel.size == 0:
            return None
        title_green = np.mean(
            (title[:, :, 0] >= 38)
            & (title[:, :, 0] <= 95)
            & (title[:, :, 1] > 60)
            & (title[:, :, 2] > 100)
        )
        title_yellow = np.mean(
            (title[:, :, 0] >= 18)
            & (title[:, :, 0] <= 38)
            & (title[:, :, 1] > 60)
            & (title[:, :, 2] > 130)
        )
        panel_white = np.mean((panel[:, :, 1] < 65) & (panel[:, :, 2] > 170))
        panel_brown = np.mean(
            (panel[:, :, 0] >= 5)
            & (panel[:, :, 0] <= 30)
            & (panel[:, :, 1] > 45)
            & (panel[:, :, 2] > 70)
        )
        if title_green > 0.04 and title_yellow > 0.04 and panel_white > 0.70 and panel_brown > 0.02:
            return int(width * 0.50), int(height * 0.685)
        return None

    @staticmethod
    def _finish_training_cancel_for_unused_points(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        match = re.search(r"剩余技能点数(\d+)", normalized)
        fallback_values = [
            int(value)
            for value in re.findall(r"(?<!\d)(\d{1,5})(?!\d)", normalized)
        ]
        unused_points = (
            int(match.group(1))
            if match is not None
            else max(fallback_values, default=0)
        )
        if (
            "养成结束确认" not in normalized
            or unused_points <= 0
            or "未使用的技能点数将作废" not in normalized
        ):
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        return round(width * 0.279), round(height * 0.718)

    @staticmethod
    def _finish_training_confirm_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if perception.get("screen") != "modal_confirm":
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.22) : int(height * 0.30), int(width * 0.02) : int(width * 0.98)]
        body = hsv[int(height * 0.31) : int(height * 0.78), int(width * 0.02) : int(width * 0.98)]
        warning = hsv[int(height * 0.60) : int(height * 0.68), int(width * 0.02) : int(width * 0.98)]
        confirm_zone = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.50) : int(width * 0.95)]
        cancel_zone = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.05) : int(width * 0.50)]
        if (
            header.size == 0
            or body.size == 0
            or warning.size == 0
            or confirm_zone.size == 0
            or cancel_zone.size == 0
        ):
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        body_graywhite = np.mean((body[:, :, 1] < 90) & (body[:, :, 2] > 120))
        warning_red_orange = np.mean(
            (
                ((warning[:, :, 0] <= 12) | ((warning[:, :, 0] >= 170) & (warning[:, :, 0] <= 179)))
                | ((warning[:, :, 0] >= 5) & (warning[:, :, 0] <= 30))
            )
            & (warning[:, :, 1] > 70)
            & (warning[:, :, 2] > 100)
        )
        confirm_green = np.mean(
            (confirm_zone[:, :, 0] >= 38)
            & (confirm_zone[:, :, 0] <= 92)
            & (confirm_zone[:, :, 1] > 70)
            & (confirm_zone[:, :, 2] > 80)
        )
        cancel_white = np.mean((cancel_zone[:, :, 1] < 70) & (cancel_zone[:, :, 2] > 175))

        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.50 <= cx <= width * 0.95):
                continue
            if not (height * 0.66 <= cy <= height * 0.78):
                continue
            if not (width * 0.30 <= w <= width * 0.55):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if (
            header_green > 0.45
            and body_graywhite > 0.75
            and warning_red_orange > 0.045
            and confirm_green > 0.25
            and cancel_white > 0.80
            and len(candidates) == 1
        ):
            _area, x, y = candidates[0]
            return x, y
        return None

    @staticmethod
    def _finish_training_unused_points_confirm_button(
        perception: dict[str, Any],
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if not all(
            token in normalized
            for token in (
                "养成结束确认",
                "即将完成养成",
                "是否确认执行",
                "剩余技能点数",
                "未使用的技能点数将作废",
                "取消",
                "完成",
            )
        ):
            return None
        if perception.get("screen") != "modal_confirm" and not SafeAdvancePlanner._looks_like_confirm_modal_panel(img):
            return None
        height, width = img.shape[:2]
        if (width, height) != (720, 1280):
            return None
        candidates: list[tuple[int, int]] = []
        for button in perception.get("buttons", []):
            if not isinstance(button, dict) or button.get("kind") != "primary_green":
                continue
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if (
                width * 0.50 <= cx <= width * 0.95
                and height * 0.66 <= cy <= height * 0.78
                and width * 0.30 <= w <= width * 0.55
                and height * 0.04 <= h <= height * 0.09
            ):
                candidates.append((int(cx), int(cy)))
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _goal_remaining_text_continue_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        # Tesseract may drop the leading "距离下一个" or confuse the single
        # character "剩".  Require the stable goal/countdown/unit pattern;
        # the fullscreen button geometry remains the second independent gate.
        goal_countdown = re.search(r"目标还[^0-9]{0,2}[0-9]{1,3}回合", normalized)
        if goal_countdown is None:
            return None
        return SafeAdvancePlanner._fullscreen_continue_button(perception, img)

    @classmethod
    def _looks_like_pending_training_result_log(
        cls,
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
        *,
        route_policy: SafeAdvanceRoutePolicy | None,
    ) -> bool:
        if (
            route_policy is None
            or not route_policy.constrained
            or route_policy.source != "pending"
            or route_policy.pending_action_id not in {
                "training.execute", "safe_advance.rest_confirm",
                "safe_advance.rest_outing_confirm", "safe_advance.bottom_skip_toggle",
                "wait:bottom_skip_double_enabled",
            }
            or re.fullmatch(r"[0-9a-fA-F]{64}", str(route_policy.action_source_sha256 or "")) is None
            or not route_policy.allows_observed_state("dialogue")
        ):
            return False
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return False
        lines = str(screen_text.get("normalized_text", "")).splitlines()
        if lines[:1] != ["训练"] or not any(
            re.fullmatch(r"获得了.+的碎片", line) for line in lines
        ):
            return False
        # A result log, not the shortened-version button or an ordinary cut-in.
        # Keep the real skip/wait pending identity after each successful step.
        return bool(
            cls._looks_like_training_top_hud(img)
            and cls._looks_like_training_result_dialogue(img)
            and cls._looks_like_large_bottom_dialogue(img)
            and cls._looks_like_training_animation_controls(img)
            and not cls._looks_like_training_home_controls(img)
            and not cls._looks_like_confirm_modal_panel(img)
            and not detect_event_option_layout(img)["points"]
            and perception.get("screen") not in {"modal_confirm", "event_choice", "training_menu"}
            and not TrainingMenuPrechecker().check(evidence["image"]).accepted
        )

    @staticmethod
    def _training_result_summary_continue_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        date_text = "月上半月" in normalized or "月下半月" in normalized
        if not (
            "高级" in normalized
            and date_text
            and "知识的碎片" in normalized
            and "跳过" in normalized
            and "缩短版" in normalized
        ):
            return None
        return (
            SafeAdvancePlanner._goal_achieved_continue_button(perception, img)
            or SafeAdvancePlanner._fullscreen_continue_button(perception, img)
        )

    @staticmethod
    def _swbc_skill_shortage_result_continue_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        upper = normalized.upper()
        common_result = (
            any(marker in upper for marker in ("SWBC", "SWEC", "SWSC"))
            and "技能" in normalized
            and "中山" in normalized
            and "草地" in normalized
            and "1600" in normalized
        )
        shortage_intro = "获得技能" in normalized and "人气第" in normalized
        ranked_reward = all(
            term in normalized
            for term in ("第1名", "粉丝数", "下一步")
        )
        if not (common_result and (shortage_intro or ranked_reward)):
            return None
        return SafeAdvancePlanner._fullscreen_continue_button(perception, img)

    @staticmethod
    def _swbc_ranked_reward_continue_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        upper = normalized.upper()
        if not (
            any(marker in upper for marker in ("SWBC", "SWEC", "SWSC"))
            and (
                "\u7c89\u4e1d\u6570" in normalized
                or any(
                    term in normalized
                    for term in ("\u4e0b\u4e00\u6b65", "\u4e0b=\u6b65")
                )
            )
            and "\u8349\u5730" in normalized
            and re.search(r"(?:1|2|3)\d{3}", normalized) is not None
        ):
            return None
        return SafeAdvancePlanner._fullscreen_continue_button(perception, img)

    @staticmethod
    def _swbc_growth_summary_continue_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        upper = normalized.upper()
        schedule_overview_variant = (
            "GROWUPRACE" in upper
            and ("SUPERW" in upper or "SWBC" in upper)
        )
        if schedule_overview_variant:
            return SafeAdvancePlanner._fullscreen_continue_button(perception, img)
        obscured_header_variant = (
            "GROWUPRACE" in upper
            and ("SWBC" in upper or "\u521d\u7ea7" in normalized)
            and "\u7ecf\u5178\u7ea7" in normalized
            and "\u9ad8\u7ea7" in normalized
            and "12\u6708\u4e0b\u534a\u6708\u7ed3\u675f\u65f6" in normalized
            and "\u80dc\u5229" in normalized
        )
        if obscured_header_variant:
            return SafeAdvancePlanner._fullscreen_continue_button(perception, img)
        current_variant = all(
            marker in normalized
            for marker in (
                "初级",
                "经典级",
                "高级",
                "12月下半月结束时",
            )
        )
        if "SUPERWBC" in upper and current_variant:
            return SafeAdvancePlanner._fullscreen_continue_button(perception, img)
        if not (
            "SUPERWBC" in upper
            and "GROWUPRACE" in upper
            and "初级" in normalized
            and "经典级" in normalized
            and "高级" in normalized
            and "12月下半月结束时" in normalized
            and "胜利" in normalized
        ):
            return None
        return SafeAdvancePlanner._fullscreen_continue_button(perception, img)

    @staticmethod
    def _looks_like_final_training_rating(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
        *, rating_layout_only: bool = False,
    ) -> bool:
        if SafeAdvancePlanner._looks_like_training_top_hud(img):
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split()) if isinstance(screen_text, dict) else ""
        # A still-active career can resemble the rating button/badge during
        # training animation. Layout-only must not bypass its own HUD evidence.
        if ("体力" in normalized and "回合" in normalized
                and any(term in normalized for term in ("还剩", "剩余"))
                and re.search(r"(?:初级|经典|高级)(?:\d{1,2}月|出道前)", normalized)):
            return False
        # An event's green scenery/buttons can satisfy the rating palette.
        # Use its actual controls/header, independently of the character art.
        from core.auto_trainer.event_options import event_option_rows_ready
        if ("选项效果" in normalized and any(header in normalized for header in
                ("养成优俊少女事件","养成赛马娘事件","协助卡事件","支援卡事件","剧本事件","主线事件"))):
            return False
        if rating_layout_only:
            return rating_continue_point(img) is not None and not event_option_rows_ready(img)
        if not isinstance(screen_text, dict):
            return False
        has_rating_terms = "等级" in normalized and "评分" in normalized
        has_score = re.search(r"\d{1,3}[,，]\d{3}", normalized) is not None
        if not (has_rating_terms or has_score) and not rating_layout_only:
            return False
        if event_option_rows_ready(img):
            return False
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center = hsv[
            int(height * 0.14) : int(height * 0.53),
            int(width * 0.16) : int(width * 0.84),
        ]
        bottom = hsv[
            int(height * 0.86) : int(height * 0.97),
            int(width * 0.24) : int(width * 0.76),
        ]
        if center.size == 0 or bottom.size == 0:
            return False
        pink = float(
            np.mean(
                (center[:, :, 0] >= 160)
                & (center[:, :, 0] <= 179)
                & (center[:, :, 1] > 65)
                & (center[:, :, 2] > 120)
            )
        )
        bronze_or_orange = float(
            np.mean(
                (center[:, :, 0] >= 5)
                & (center[:, :, 0] <= 35)
                & (center[:, :, 1] > 65)
                & (center[:, :, 2] > 100)
            )
        )
        green = float(
            np.mean(
                (bottom[:, :, 0] >= 35)
                & (bottom[:, :, 0] <= 95)
                & (bottom[:, :, 1] > 75)
                & (bottom[:, :, 2] > 95)
            )
        )
        legacy_rating = pink > 0.035 and (has_rating_terms or has_score)
        a_grade_rating = bronze_or_orange > 0.15 and has_rating_terms
        # The badge palette changes at higher tiers (UF is purple/cyan).
        # Its letters, numeric score and colour are descriptive, not gates.
        rating_page = (has_rating_terms and "养成等级" in normalized and "下一页" in normalized
            and float(np.mean((center[:, :, 1] > 65) & (center[:, :, 2] > 100))) > .25)
        return (legacy_rating or a_grade_rating or rating_page) and green > 0.10

    @staticmethod
    def _inheritance_factor_result_confirm_button(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        if "因子" not in normalized:
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left = hsv[
            int(height * 0.87) : int(height * 0.98),
            int(width * 0.04) : int(width * 0.49),
        ]
        right = hsv[
            int(height * 0.87) : int(height * 0.98),
            int(width * 0.50) : int(width * 0.96),
        ]
        if left.size == 0 or right.size == 0:
            return None
        left_white = float(
            np.mean((left[:, :, 1] < 70) & (left[:, :, 2] > 165))
        )
        right_green = float(
            np.mean(
                (right[:, :, 0] >= 35)
                & (right[:, :, 0] <= 95)
                & (right[:, :, 1] > 75)
                & (right[:, :, 2] > 90)
            )
        )
        if left_white < 0.25 or right_green < 0.18:
            return None
        return int(width * 0.714), int(height * 0.92)

    @staticmethod
    def _inheritance_factor_second_confirmation_text_verified(
        screen_text: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        lines = tuple(
            "".join(line.split())
            for line in str(screen_text.get("visible_text", "")).splitlines()
            if line.strip()
        )
        normalized = "".join(lines)
        return bool(
            screen_text.get("dangerous") is False
            and not screen_text.get("resource_terms")
            and lines
            and lines[0] == "确定因子确认"
            and lines[-2:] == ("取消", "确定")
            and "是否确定获得该因子" in normalized
            and "确定后将无法重新获得因子" in normalized
        )

    @staticmethod
    def _looks_like_event_point_reward(
        screen_text: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        return bool(
            "\u6d3b\u52a8\u79ef\u5206" in normalized
            and (
                "\u6d3b\u52a8\u52a0\u6210\u603b\u8ba1" in normalized
                or "\u6e38\u620f\u8fdb\u5ea6" in normalized
            )
        )

    def _post_career_participation_prompt_action(
        self,
        img: np.ndarray,
        screen_text: dict[str, Any] | None,
    ) -> tuple[int, int] | None:
        """Close the post-career ``要报名参加比赛吗?`` prompt.

        The prompt offers team-arena / event / daily / exhibition entries.
        Decline by tapping the bottom close control; point recorded from the
        2026-08-10 run (generic fullscreen continue).
        """

        normalized = ""
        if isinstance(screen_text, dict):
            normalized = "".join(
                str(screen_text.get("normalized_text", "")).split()
            )
        if "报名参加比赛" not in normalized:
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        return (
            int(round(width * 360 / 720)),
            int(round(height * 1080 / 1280)),
        )

    @staticmethod
    def _team_formation_page_close_button(
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        """Return the top-left back control of the full team-formation page.

        The JJC team page appears after a career completes; the user only
        wants it closed.  The top-left arrow is the standard back control.
        """

        height, width = img.shape[:2]
        if width > height:
            return None
        return int(round(width * 30 / 720)), int(round(height * 25 / 1280))

    def _team_formation_completion_prompt_close_button(
        self,
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        if not (
            "\u56e2\u961f\u7f16\u6210" in normalized
            and (
                "\u5173\u95ed" in normalized
                or "\u524d\u5f80\u4e3b\u9875" in normalized
            )
            and (
                "\u5b8c\u6210\u517b\u6210" in normalized
                or "\u7f16\u5165\u961f\u4f0d" in normalized
            )
        ):
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left = hsv[
            int(height * 0.60) : int(height * 0.70),
            int(width * 0.05) : int(width * 0.48),
        ]
        right = hsv[
            int(height * 0.60) : int(height * 0.70),
            int(width * 0.52) : int(width * 0.95),
        ]
        if left.size == 0 or right.size == 0:
            return None
        left_white = float(
            np.mean((left[:, :, 1] < 65) & (left[:, :, 2] > 170))
        )
        right_green = float(
            np.mean(
                (right[:, :, 0] >= 35)
                & (right[:, :, 0] <= 95)
                & (right[:, :, 1] > 70)
                & (right[:, :, 2] > 80)
            )
        )
        if left_white < 0.75 or right_green < 0.25:
            return None
        return int(width * 0.28), int(height * 0.652)

    @staticmethod
    def _activity_story_unlock_close_button(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        if not (
            "\u6d3b\u52a8\u5267\u60c5\u89e3\u9501" in normalized
            and (
                "\u5173\u95ed" in normalized
                or "\u73b0\u53ef\u89e3\u9501\u6d3b\u52a8\u5267\u60c5" in normalized
            )
        ):
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        close_button = hsv[
            int(height * 0.60) : int(height * 0.70),
            int(width * 0.28) : int(width * 0.72),
        ]
        if close_button.size == 0:
            return None
        button_white = float(
            np.mean(
                (close_button[:, :, 1] < 65)
                & (close_button[:, :, 2] > 170)
            )
        )
        if button_white < 0.75:
            return None
        return int(width * 0.50), int(height * 0.652)

    @staticmethod
    def _looks_like_event_reward_overlay(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            y0: float,
            y1: float,
            x0: float,
            x1: float,
            predicate: Any,
        ) -> float:
            region = hsv[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            return float(np.mean(predicate(region))) if region.size else 0.0

        gold = lambda r: (
            (r[:, :, 0] >= 10)
            & (r[:, :, 0] <= 38)
            & (r[:, :, 1] > 100)
            & (r[:, :, 2] > 140)
        )
        pink = lambda r: (
            ((r[:, :, 0] <= 10) | (r[:, :, 0] >= 165))
            & (r[:, :, 1] > 35)
            & (r[:, :, 2] > 130)
        )
        red = lambda r: (
            ((r[:, :, 0] <= 10) | (r[:, :, 0] >= 170))
            & (r[:, :, 1] > 120)
            & (r[:, :, 2] > 100)
        )
        green = lambda r: (
            (r[:, :, 0] >= 35)
            & (r[:, :, 0] <= 95)
            & (r[:, :, 1] > 75)
            & (r[:, :, 2] > 95)
        )
        return bool(
            ratio(0.03, 0.16, 0.18, 0.82, gold) > 0.10
            and ratio(0.34, 0.51, 0.30, 0.70, pink) > 0.08
            and ratio(0.64, 0.90, 0.15, 0.85, red) > 0.18
            and ratio(0.88, 0.97, 0.24, 0.76, green) > 0.35
        )

    @staticmethod
    def _looks_like_final_career_summary(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        if SafeAdvancePlanner._looks_like_training_top_hud(img):
            return False
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        if bool(
            "下一页" in normalized
            and any(
                term in normalized
                for term in ("主要获胜经历", "总成绩", "天王奖")
            )
        ):
            return True
        if re.search(r"\d{1,3}[,，]\d{3}", normalized) is None:
            return False
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        body = hsv[
            0 : int(height * 0.84),
            0:width,
        ]
        bottom = hsv[
            int(height * 0.86) : int(height * 0.97),
            int(width * 0.24) : int(width * 0.76),
        ]
        blue = float(
            np.mean(
                (body[:, :, 0] >= 95)
                & (body[:, :, 0] <= 130)
                & (body[:, :, 1] > 70)
                & (body[:, :, 2] > 100)
            )
        )
        green = float(
            np.mean(
                (bottom[:, :, 0] >= 35)
                & (bottom[:, :, 0] <= 95)
                & (bottom[:, :, 1] > 75)
                & (bottom[:, :, 2] > 95)
            )
        )
        return blue > 0.12 and green > 0.10

    @classmethod
    def _goal_failure_summary_continue_button(
        cls,
        screen_text: dict[str, Any] | None,
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        # The explicit failure title also survives when OCR misses the small
        # badge and score. Neither number is needed for the free Continue,
        # and navigation does not supply a count to the goal ledger.
        if "失败" not in normalized and "目标未达成" not in normalized:
            return None
        if not any(
            marker in normalized
            for marker in (
                "目标未达成",
                "参加目标级别比赛",
                "推荐选择与优俊少女适应性相符的比赛",
            )
        ):
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        return cls._fullscreen_continue_button(perception, img)

    @staticmethod
    def _final_settlement_bottom_green_button(
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        x0, x1 = int(width * 0.15), int(width * 0.85)
        y0, y1 = int(height * 0.80), int(height * 0.98)
        roi = hsv[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        green = (
            (roi[:, :, 0] >= 35)
            & (roi[:, :, 0] <= 95)
            & (roi[:, :, 1] > 75)
            & (roi[:, :, 2] > 95)
        ).astype(np.uint8)
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(green)
        candidates: list[tuple[int, int, int]] = []
        for index in range(1, count):
            x, y, box_width, box_height, area = (
                int(value) for value in stats[index]
            )
            absolute_x = x0 + x
            absolute_y = y0 + y
            if not int(width * 0.14) <= absolute_x <= int(width * 0.35):
                continue
            if not int(width * 0.35) <= box_width <= int(width * 0.72):
                continue
            if not int(height * 0.05) <= box_height <= int(height * 0.10):
                continue
            if not int(height * 0.80) <= absolute_y <= int(height * 0.92):
                continue
            if area < int(width * height * 0.02):
                continue
            candidates.append(
                (
                    area,
                    absolute_x + box_width // 2,
                    absolute_y + box_height // 2,
                )
            )
        if not candidates:
            return None
        _area, center_x, center_y = max(candidates)
        return center_x, center_y

    @staticmethod
    def _looks_like_final_career_reward(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        if re.search(r"\d{2,4}/\d{2,4}", normalized) is None:
            return False
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        body = hsv[0 : int(height * 0.84), 0:width]
        bottom = hsv[
            int(height * 0.86) : int(height * 0.97),
            int(width * 0.24) : int(width * 0.76),
        ]
        blue = float(
            np.mean(
                (body[:, :, 0] >= 95)
                & (body[:, :, 0] <= 130)
                & (body[:, :, 1] > 45)
                & (body[:, :, 2] > 100)
            )
        )
        green = float(
            np.mean(
                (bottom[:, :, 0] >= 35)
                & (bottom[:, :, 0] <= 95)
                & (bottom[:, :, 1] > 75)
                & (bottom[:, :, 2] > 95)
            )
        )
        return blue > 0.18 and green > 0.10

    @staticmethod
    def _looks_like_factor_research_slot_upgrade(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            y0: float,
            y1: float,
            x0: float,
            x1: float,
            predicate: Any,
        ) -> float:
            region = hsv[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            return float(np.mean(predicate(region))) if region.size else 0.0

        navy = lambda r: (
            (r[:, :, 0] >= 100)
            & (r[:, :, 0] <= 135)
            & (r[:, :, 1] > 40)
            & (r[:, :, 2] > 45)
            & (r[:, :, 2] < 210)
        )
        cyan = lambda r: (
            (r[:, :, 0] >= 80)
            & (r[:, :, 0] <= 105)
            & (r[:, :, 1] > 80)
            & (r[:, :, 2] > 120)
        )
        yellow = lambda r: (
            (r[:, :, 0] >= 18)
            & (r[:, :, 0] <= 38)
            & (r[:, :, 1] > 80)
            & (r[:, :, 2] > 140)
        )
        purple = lambda r: (
            (r[:, :, 0] >= 135)
            & (r[:, :, 0] <= 170)
            & (r[:, :, 1] > 80)
            & (r[:, :, 2] > 100)
        )
        green = lambda r: (
            (r[:, :, 0] >= 38)
            & (r[:, :, 0] <= 92)
            & (r[:, :, 1] > 70)
            & (r[:, :, 2] > 80)
        )
        return bool(
            ratio(0.00, 0.82, 0.00, 1.00, navy) > 0.60
            and ratio(0.07, 0.28, 0.18, 0.82, cyan) > 0.18
            and ratio(0.25, 0.36, 0.15, 0.85, yellow) > 0.10
            and ratio(0.34, 0.53, 0.30, 0.70, purple) > 0.07
            and ratio(0.82, 0.94, 0.22, 0.78, green) > 0.25
        )

    @staticmethod
    def _looks_like_final_support_reward(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(
            y0: float,
            y1: float,
            x0: float,
            x1: float,
            predicate: Any,
        ) -> float:
            region = hsv[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            return float(np.mean(predicate(region))) if region.size else 0.0

        green = lambda r: (
            (r[:, :, 0] >= 38)
            & (r[:, :, 0] <= 92)
            & (r[:, :, 1] > 70)
            & (r[:, :, 2] > 80)
        )
        white = lambda r: (r[:, :, 1] < 65) & (r[:, :, 2] > 170)
        colorful_slots: list[float] = []
        for x0, x1 in (
            (0.04, 0.21),
            (0.22, 0.40),
            (0.41, 0.59),
            (0.60, 0.78),
            (0.79, 0.96),
        ):
            slot = hsv[
                int(height * 0.19) : int(height * 0.36),
                int(width * x0) : int(width * x1),
            ]
            colorful_slots.append(
                float(np.mean((slot[:, :, 1] > 80) & (slot[:, :, 2] > 90)))
                if slot.size
                else 0.0
            )
        has_reward_count = re.search(r"[xX×]\d{3,}", normalized) is not None
        return bool(
            min(colorful_slots) > 0.30
            and ratio(0.02, 0.10, 0.20, 0.80, green) > 0.12
            and ratio(0.10, 0.15, 0.02, 0.98, green) > 0.28
            and ratio(0.38, 0.42, 0.02, 0.98, green) > 0.35
            and ratio(0.48, 0.52, 0.02, 0.98, green) > 0.20
            and ratio(0.52, 0.86, 0.02, 0.98, white) > 0.90
            and ratio(0.88, 0.97, 0.25, 0.75, green) > 0.45
            and (has_reward_count or "," in normalized)
        )
