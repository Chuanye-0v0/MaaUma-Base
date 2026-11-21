"""Global home, title screen, login rewards, and career setup handlers."""
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


class SetupAndHomeHandlerMixin:
    """Global home, title start, announcements, and trainee/deck setup handlers."""

    @staticmethod
    def is_post_career_global_home(img: np.ndarray | None) -> bool:
        """Confirm the unobscured home, shared with entry and settlement."""
        return is_global_home(img)

    @staticmethod
    def _recruit_return_home_point(
        image_path: Path,
        img: np.ndarray,
        screen_text: dict[str, Any],
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if (width, height) != (720, 1280):
            return None
        if set(screen_text.get("resource_terms", ())) != {"招募"}:
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if not all(
            phrase in normalized
            for phrase in (
                "招募1次",
                "招募10次",
            )
        ):
            return None
        try:
            tokens = tuple(TesseractTsvTokenReader().read(image_path.read_bytes()))
        except Exception:
            return None
        joined_tokens = "".join(token.text for token in tokens)
        if not all(
            phrase in joined_tokens
            for phrase in ("今日仍余招募机会", "招募10次")
        ):
            return None
        nav_terms_complete = all(
            term in {token.text for token in tokens}
            for term in ("强化", "剧情", "赛事")
        )
        home_tokens = [token for token in tokens if token.text == "主页"]
        if len(home_tokens) > 1:
            return None
        if not home_tokens:
            if not SafeAdvancePlanner._frozen_recruit_home_nav_matches(img):
                return None
            return (360, 1250)
        if not nav_terms_complete:
            return None
        if home_tokens[0].bbox is None:
            return None
        x, y, box_width, box_height = home_tokens[0].bbox
        if not (
            box_width > 0
            and box_height > 0
            and 320 <= x
            and 1180 <= y
            and x + box_width <= 410
            and y + box_height <= 1275
        ):
            return None
        center = (x + box_width // 2, y + box_height // 2)
        if not (330 <= center[0] <= 390 and 1230 <= center[1] <= 1270):
            return None
        return center

    @staticmethod
    def _frozen_recruit_home_nav_matches(img: np.ndarray) -> bool:
        path = sa_attr('RECRUIT_HOME_NAV_REFERENCE_PATH', RECRUIT_HOME_NAV_REFERENCE_PATH)
        try:
            payload = path.read_bytes()
        except OSError:
            return False
        if hashlib.sha256(payload).hexdigest() != sa_attr('RECRUIT_HOME_NAV_REFERENCE_SHA256', RECRUIT_HOME_NAV_REFERENCE_SHA256):
            return False
        reference = cv2.imdecode(
            np.frombuffer(payload, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if reference is None or reference.shape != img.shape:
            return False

        def similarity(box: tuple[int, int, int, int]) -> tuple[float, float]:
            x0, y0, x1, y1 = box
            current_roi = img[y0:y1, x0:x1]
            reference_roi = reference[y0:y1, x0:x1]
            expected_shape = (y1 - y0, x1 - x0, 3)
            if (
                current_roi.shape != expected_shape
                or reference_roi.shape != expected_shape
            ):
                return (-1.0, 0.0)
            correlation = float(
                cv2.matchTemplate(
                    cv2.cvtColor(current_roi, cv2.COLOR_BGR2GRAY),
                    cv2.cvtColor(reference_roi, cv2.COLOR_BGR2GRAY),
                    cv2.TM_CCOEFF_NORMED,
                )[0, 0]
            )
            exact_ratio = float(
                np.mean(np.all(current_roi == reference_roi, axis=2))
            )
            return correlation, exact_ratio

        home = similarity((285, 1195, 435, 1270))
        if not (home[0] >= 0.99 and home[1] >= 0.90):
            return False

        recruit_roi = img[1195:1270, 580:710]
        recruit_hsv = cv2.cvtColor(recruit_roi, cv2.COLOR_BGR2HSV)
        recruit_selected_ratio = float(
            np.mean(
                (recruit_hsv[:, :, 0] >= 140)
                & (recruit_hsv[:, :, 0] <= 175)
                & (recruit_hsv[:, :, 1] >= 100)
                & (recruit_hsv[:, :, 2] >= 120)
            )
        )
        if not (0.25 <= recruit_selected_ratio <= 0.38):
            return False

        story = similarity((140, 1195, 285, 1270))
        race = similarity((435, 1195, 580, 1270))
        aggregate = similarity((140, 1195, 580, 1270))
        secondary_matches = sum(
            (
                story[0] >= 0.93 and story[1] >= 0.85,
                race[0] >= 0.93 and race[1] >= 0.90,
                aggregate[0] >= 0.93 and aggregate[1] >= 0.90,
            )
        )
        return secondary_matches >= 2

    @staticmethod
    def _trainee_select_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        title = hsv[int(height * 0.04) : int(height * 0.15), 0 : int(width * 0.42)]
        card_area = hsv[int(height * 0.42) : int(height * 0.78), int(width * 0.02) : int(width * 0.98)]
        continue_button = hsv[int(height * 0.80) : int(height * 0.89), int(width * 0.25) : int(width * 0.75)]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if title.size == 0 or card_area.size == 0 or continue_button.size == 0 or bottom_nav.size == 0:
            return None

        title_dark = np.mean(title[:, :, 2] < 90)
        cards_white = np.mean((card_area[:, :, 1] < 65) & (card_area[:, :, 2] > 175))
        cards_orange = np.mean(
            (card_area[:, :, 0] >= 5)
            & (card_area[:, :, 0] <= 38)
            & (card_area[:, :, 1] > 70)
            & (card_area[:, :, 2] > 100)
        )
        continue_green = np.mean(
            (continue_button[:, :, 0] >= 38)
            & (continue_button[:, :, 0] <= 92)
            & (continue_button[:, :, 1] > 70)
            & (continue_button[:, :, 2] > 80)
        )
        continue_white = np.mean((continue_button[:, :, 1] < 65) & (continue_button[:, :, 2] > 175))
        nav_white = np.mean((bottom_nav[:, :, 1] < 65) & (bottom_nav[:, :, 2] > 175))
        nav_green = np.mean(
            (bottom_nav[:, :, 0] >= 38)
            & (bottom_nav[:, :, 0] <= 92)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 80)
        )

        if not (
            title_dark > 0.20
            and cards_white > 0.56
            and 0.09 < cards_orange < 0.25
            and 0.35 < continue_green < 0.70
            and continue_white > 0.35
            and nav_white > 0.45
            and nav_green < 0.18
        ):
            return None

        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.35 <= cx <= width * 0.65):
                continue
            if not (height * 0.80 <= cy <= height * 0.89):
                continue
            if not (width * 0.25 <= w <= width * 0.50):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if candidates:
            _area, x, y = max(candidates, key=lambda item: item[0])
            return x, y
        return int(width * 0.50), int(height * 0.842)

    @staticmethod
    def _inheritance_select_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top = hsv[int(height * 0.02) : int(height * 0.20), :]
        parent_cards = hsv[int(height * 0.50) : int(height * 0.74), int(width * 0.02) : int(width * 0.98)]
        continue_button = hsv[int(height * 0.78) : int(height * 0.89), int(width * 0.25) : int(width * 0.75)]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if top.size == 0 or parent_cards.size == 0 or continue_button.size == 0 or bottom_nav.size == 0:
            return None

        top_dark = np.mean(top[:, :, 2] < 90)
        top_blue = np.mean(
            (top[:, :, 0] >= 85)
            & (top[:, :, 0] <= 125)
            & (top[:, :, 1] > 60)
            & (top[:, :, 2] > 90)
        )
        parent_white = np.mean((parent_cards[:, :, 1] < 65) & (parent_cards[:, :, 2] > 175))
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
        nav_white = np.mean((bottom_nav[:, :, 1] < 65) & (bottom_nav[:, :, 2] > 175))

        if not (
            top_dark > 0.40
            and top_blue < 0.12
            and parent_white > 0.70
            and 0.25 < continue_green < 0.60
            and continue_white > 0.40
            and nav_green < 0.09
            and nav_white > 0.50
        ):
            return None

        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.35 <= cx <= width * 0.65):
                continue
            if not (height * 0.78 <= cy <= height * 0.89):
                continue
            if not (width * 0.25 <= w <= width * 0.50):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if candidates:
            _area, x, y = max(candidates, key=lambda item: item[0])
            return x, y
        return int(width * 0.50), int(height * 0.842)

    @staticmethod
    def _looks_like_partial_inheritance_select_page(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top = hsv[int(height * 0.02) : int(height * 0.20), :]
        parent_cards = hsv[
            int(height * 0.50) : int(height * 0.74),
            int(width * 0.02) : int(width * 0.98),
        ]
        if top.size == 0 or parent_cards.size == 0:
            return False
        top_dark = np.mean(top[:, :, 2] < 90)
        parent_white = np.mean(
            (parent_cards[:, :, 1] < 65) & (parent_cards[:, :, 2] > 175)
        )
        return bool(top_dark > 0.40 or parent_white > 0.70)

    @staticmethod
    def _inheritance_auto_select_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top = hsv[int(height * 0.02) : int(height * 0.20), :]
        left_plus = hsv[int(height * 0.55) : int(height * 0.67), int(width * 0.07) : int(width * 0.25)]
        right_plus = hsv[int(height * 0.55) : int(height * 0.67), int(width * 0.55) : int(width * 0.73)]
        auto_button = hsv[int(height * 0.72) : int(height * 0.79), int(width * 0.50) : int(width * 0.94)]
        continue_button = hsv[int(height * 0.78) : int(height * 0.89), int(width * 0.25) : int(width * 0.75)]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if (
            top.size == 0
            or left_plus.size == 0
            or right_plus.size == 0
            or auto_button.size == 0
            or continue_button.size == 0
            or bottom_nav.size == 0
        ):
            return None

        top_dark = np.mean(top[:, :, 2] < 90)
        left_empty = SafeAdvancePlanner._empty_inheritance_slot_score(left_plus)
        right_empty = SafeAdvancePlanner._empty_inheritance_slot_score(right_plus)
        auto_green = np.mean(
            (auto_button[:, :, 0] >= 38)
            & (auto_button[:, :, 0] <= 92)
            & (auto_button[:, :, 1] > 70)
            & (auto_button[:, :, 2] > 80)
        )
        continue_green = np.mean(
            (continue_button[:, :, 0] >= 38)
            & (continue_button[:, :, 0] <= 92)
            & (continue_button[:, :, 1] > 70)
            & (continue_button[:, :, 2] > 80)
        )
        nav_green = np.mean(
            (bottom_nav[:, :, 0] >= 38)
            & (bottom_nav[:, :, 0] <= 92)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 80)
        )
        if not (
            top_dark > 0.40
            and left_empty > 0.75
            and right_empty > 0.75
            and auto_green > 0.25
            and continue_green > 0.25
            and nav_green < 0.09
        ):
            return None
        return int(width * 0.72), int(height * 0.756)

    @staticmethod
    def _empty_inheritance_slot_score(region: np.ndarray) -> float:
        if region.size == 0:
            return 0.0
        white = np.mean((region[:, :, 1] < 65) & (region[:, :, 2] > 175))
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        saturated = np.mean((region[:, :, 1] > 70) & (region[:, :, 2] > 90))
        return float(min(1.0, white * 0.85 + green * 5.0 - saturated * 0.35))

    @staticmethod
    def _friend_support_first_card_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.025) : int(height * 0.085), int(width * 0.01) : int(width * 0.99)]
        first_row = hsv[int(height * 0.17) : int(height * 0.29), int(width * 0.03) : int(width * 0.95)]
        card_left = hsv[int(height * 0.18) : int(height * 0.28), int(width * 0.04) : int(width * 0.20)]
        follow_col = hsv[int(height * 0.16) : int(height * 0.82), int(width * 0.70) : int(width * 0.96)]
        bottom_close = hsv[int(height * 0.89) : int(height * 0.98), int(width * 0.25) : int(width * 0.75)]
        if (
            header.size == 0
            or first_row.size == 0
            or card_left.size == 0
            or follow_col.size == 0
            or bottom_close.size == 0
        ):
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        first_row_white = np.mean((first_row[:, :, 1] < 65) & (first_row[:, :, 2] > 175))
        card_saturated = np.mean((card_left[:, :, 1] > 70) & (card_left[:, :, 2] > 90))
        follow_pink = np.mean(
            (follow_col[:, :, 0] >= 135)
            & (follow_col[:, :, 0] <= 175)
            & (follow_col[:, :, 1] > 60)
            & (follow_col[:, :, 2] > 90)
        )
        close_white = np.mean((bottom_close[:, :, 1] < 65) & (bottom_close[:, :, 2] > 175))
        if (
            header_green > 0.55
            and first_row_white > 0.70
            and card_saturated > 0.35
            and follow_pink > 0.05
            and close_white > 0.80
        ):
            return int(width * 0.122), int(height * 0.230)
        return None

    @staticmethod
    def _support_deck_empty_friend_slot_button(img: np.ndarray) -> tuple[int, int] | None:
        if not SafeAdvancePlanner._looks_like_support_deck_page(img):
            return None
        height, width = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        friend_slot = hsv[int(height * 0.42) : int(height * 0.64), int(width * 0.66) : int(width * 0.95)]
        if friend_slot.size == 0:
            return None
        slot_white = np.mean((friend_slot[:, :, 1] < 65) & (friend_slot[:, :, 2] > 175))
        slot_pink = np.mean(
            (friend_slot[:, :, 0] >= 135)
            & (friend_slot[:, :, 0] <= 175)
            & (friend_slot[:, :, 1] > 60)
            & (friend_slot[:, :, 2] > 90)
        )
        slot_green = np.mean(
            (friend_slot[:, :, 0] >= 38)
            & (friend_slot[:, :, 0] <= 92)
            & (friend_slot[:, :, 1] > 70)
            & (friend_slot[:, :, 2] > 80)
        )
        if slot_white > 0.45 and slot_pink > 0.08 and slot_green > 0.012:
            return int(width * 0.797), int(height * 0.531)
        return None

    @staticmethod
    def _support_deck_start_confirm_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if (
            SafeAdvancePlanner._knowledge_table_close_button(img) is not None
            or SafeAdvancePlanner._learn_wisdom_button(perception, img) is not None
        ):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.02) : int(height * 0.08), int(width * 0.01) : int(width * 0.99)]
        body = hsv[int(height * 0.08) : int(height * 0.96), int(width * 0.01) : int(width * 0.99)]
        warning = hsv[int(height * 0.78) : int(height * 0.90), int(width * 0.05) : int(width * 0.95)]
        confirm_zone = hsv[int(height * 0.89) : int(height * 0.97), int(width * 0.50) : int(width * 0.95)]
        cancel_zone = hsv[int(height * 0.89) : int(height * 0.97), int(width * 0.05) : int(width * 0.50)]
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
        warning_orange = np.mean(
            (warning[:, :, 0] >= 5)
            & (warning[:, :, 0] <= 35)
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
        cancel_brown = np.mean(
            (cancel_zone[:, :, 0] >= 5)
            & (cancel_zone[:, :, 0] <= 30)
            & (cancel_zone[:, :, 1] > 45)
            & (cancel_zone[:, :, 2] > 70)
        )
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.50 <= cx <= width * 0.95):
                continue
            if not (height * 0.89 <= cy <= height * 0.97):
                continue
            if not (width * 0.30 <= w <= width * 0.55):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if (
            header_green > 0.35
            and body_graywhite > 0.50
            and warning_orange > 0.035
            and confirm_green > 0.35
            and cancel_white > 0.45
            and cancel_brown > 0.01
            and len(candidates) == 1
        ):
            _area, x, y = candidates[0]
            return x, y
        return None

    @staticmethod
    def _looks_like_support_deck_page(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.16) : int(height * 0.22), int(width * 0.04) : int(width * 0.96)]
        card_grid = hsv[int(height * 0.22) : int(height * 0.64), int(width * 0.04) : int(width * 0.96)]
        type_icons = hsv[int(height * 0.62) : int(height * 0.70), int(width * 0.08) : int(width * 0.92)]
        auto_button = hsv[int(height * 0.69) : int(height * 0.76), int(width * 0.48) : int(width * 0.94)]
        start_button = hsv[int(height * 0.80) : int(height * 0.89), int(width * 0.25) : int(width * 0.75)]
        if (
            header.size == 0
            or card_grid.size == 0
            or type_icons.size == 0
            or auto_button.size == 0
            or start_button.size == 0
        ):
            return False

        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        header_white = np.mean((header[:, :, 1] < 65) & (header[:, :, 2] > 175))
        card_saturated = np.mean((card_grid[:, :, 1] > 70) & (card_grid[:, :, 2] > 90))
        card_pink = np.mean(
            (card_grid[:, :, 0] >= 135)
            & (card_grid[:, :, 0] <= 175)
            & (card_grid[:, :, 1] > 60)
            & (card_grid[:, :, 2] > 90)
        )
        icon_white = np.mean((type_icons[:, :, 1] < 65) & (type_icons[:, :, 2] > 175))
        auto_green = np.mean(
            (auto_button[:, :, 0] >= 38)
            & (auto_button[:, :, 0] <= 92)
            & (auto_button[:, :, 1] > 70)
            & (auto_button[:, :, 2] > 80)
        )
        start_green = np.mean(
            (start_button[:, :, 0] >= 38)
            & (start_button[:, :, 0] <= 92)
            & (start_button[:, :, 1] > 70)
            & (start_button[:, :, 2] > 80)
        )
        start_orange = np.mean(
            (start_button[:, :, 0] >= 5)
            & (start_button[:, :, 0] <= 38)
            & (start_button[:, :, 1] > 70)
            & (start_button[:, :, 2] > 100)
        )
        start_white = np.mean((start_button[:, :, 1] < 65) & (start_button[:, :, 2] > 175))
        return bool(
            header_green > 0.25
            and header_white > 0.25
            and card_saturated > 0.35
            and card_pink > 0.08
            and icon_white > 0.55
            and auto_green > 0.40
            and start_green > 0.50
            and start_orange > 0.15
            and start_white < 0.20
        )

    @staticmethod
    def _looks_like_scenario_select_page(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top_left = hsv[int(height * 0.10) : int(height * 0.18), 0 : int(width * 0.35)]
        center_hero = hsv[int(height * 0.18) : int(height * 0.78), int(width * 0.05) : int(width * 0.95)]
        continue_button = hsv[int(height * 0.80) : int(height * 0.90), int(width * 0.25) : int(width * 0.75)]
        bottom_nav = hsv[int(height * 0.90) : height, :]
        if top_left.size == 0 or center_hero.size == 0 or continue_button.size == 0 or bottom_nav.size == 0:
            return False
        top_blue = np.mean(
            (top_left[:, :, 0] >= 85)
            & (top_left[:, :, 0] <= 125)
            & (top_left[:, :, 1] > 60)
            & (top_left[:, :, 2] > 90)
        )
        top_purple = np.mean(
            (top_left[:, :, 0] >= 120)
            & (top_left[:, :, 0] <= 155)
            & (top_left[:, :, 1] > 30)
            & (top_left[:, :, 2] > 70)
        )
        hero_saturated = np.mean((center_hero[:, :, 1] > 70) & (center_hero[:, :, 2] > 90))
        continue_green = np.mean(
            (continue_button[:, :, 0] >= 38)
            & (continue_button[:, :, 0] <= 92)
            & (continue_button[:, :, 1] > 70)
            & (continue_button[:, :, 2] > 80)
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
            top_blue > 0.18
            and top_purple > 0.18
            and hero_saturated > 0.42
            and continue_green > 0.55
            and nav_green > 0.25
            and nav_blue > 0.05
        )

    @staticmethod
    def _title_start_point(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        upper = hsv[int(height * 0.02) : int(height * 0.45), :]
        center = hsv[int(height * 0.40) : int(height * 0.80), int(width * 0.10) : int(width * 0.90)]
        start_bar = hsv[int(height * 0.80) : int(height * 0.90), int(width * 0.10) : int(width * 0.90)]
        if upper.size == 0 or center.size == 0 or start_bar.size == 0:
            return None
        full_blue = np.mean(
            (hsv[:, :, 0] >= 85)
            & (hsv[:, :, 0] <= 115)
            & (hsv[:, :, 1] > 50)
            & (hsv[:, :, 2] > 100)
        )
        upper_blue = np.mean(
            (upper[:, :, 0] >= 85)
            & (upper[:, :, 0] <= 115)
            & (upper[:, :, 1] > 50)
            & (upper[:, :, 2] > 100)
        )
        center_saturated = np.mean((center[:, :, 1] > 70) & (center[:, :, 2] > 100))
        center_pink = np.mean(
            (center[:, :, 0] >= 140)
            & (center[:, :, 0] <= 175)
            & (center[:, :, 1] > 60)
            & (center[:, :, 2] > 90)
        )
        center_yellow = np.mean(
            (center[:, :, 0] >= 20)
            & (center[:, :, 0] <= 38)
            & (center[:, :, 1] > 70)
            & (center[:, :, 2] > 100)
        )
        start_white = np.mean((start_bar[:, :, 1] < 65) & (start_bar[:, :, 2] > 170))
        if full_blue > 0.40 and upper_blue > 0.55 and center_saturated > 0.35 and start_white > 0.25:
            return int(width * 0.50), int(height * 0.85)
        full_white = np.mean((hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170))
        upper_white = np.mean((upper[:, :, 1] < 65) & (upper[:, :, 2] > 170))
        if (
            full_white > 0.75
            and upper_white > 0.80
            and center_pink > 0.05
            and center_yellow > 0.02
            and start_white > 0.50
        ):
            return int(width * 0.50), int(height * 0.85)
        return None

    @staticmethod
    def _cn_title_start_point(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        very_top = hsv[0 : int(height * 0.12), :]
        logo = hsv[
            int(height * 0.48) : int(height * 0.78),
            int(width * 0.12) : int(width * 0.88),
        ]
        start_bar = hsv[
            int(height * 0.80) : int(height * 0.90),
            int(width * 0.10) : int(width * 0.90),
        ]
        if very_top.size == 0 or logo.size == 0 or start_bar.size == 0:
            return None
        very_top_blue = np.mean(
            (very_top[:, :, 0] >= 85)
            & (very_top[:, :, 0] <= 115)
            & (very_top[:, :, 1] > 50)
            & (very_top[:, :, 2] > 100)
        )
        logo_pink = np.mean(
            (logo[:, :, 0] >= 140)
            & (logo[:, :, 0] <= 175)
            & (logo[:, :, 1] > 60)
            & (logo[:, :, 2] > 90)
        )
        logo_yellow = np.mean(
            (logo[:, :, 0] >= 20)
            & (logo[:, :, 0] <= 38)
            & (logo[:, :, 1] > 70)
            & (logo[:, :, 2] > 100)
        )
        logo_orange = np.mean(
            (logo[:, :, 0] >= 5)
            & (logo[:, :, 0] <= 35)
            & (logo[:, :, 1] > 60)
            & (logo[:, :, 2] > 100)
        )
        start_pink = np.mean(
            (start_bar[:, :, 0] >= 140)
            & (start_bar[:, :, 0] <= 175)
            & (start_bar[:, :, 1] > 60)
            & (start_bar[:, :, 2] > 90)
        )
        start_white = np.mean((start_bar[:, :, 1] < 65) & (start_bar[:, :, 2] > 170))
        legal = hsv[int(height * 0.90) : int(height * 0.99), int(width * 0.02) : int(width * 0.98)]
        legal_white = np.mean((legal[:, :, 1] < 65) & (legal[:, :, 2] > 170)) if legal.size else 0.0
        legal_orange = (
            np.mean(
                (legal[:, :, 0] >= 5)
                & (legal[:, :, 0] <= 35)
                & (legal[:, :, 1] > 60)
                & (legal[:, :, 2] > 100)
            )
            if legal.size
            else 0.0
        )
        if (
            very_top_blue > 0.45
            and logo_pink > 0.15
            and logo_yellow > 0.03
            and start_pink > 0.18
            and start_white > 0.20
        ):
            return int(width * 0.50), int(height * 0.85)
        if (
            logo_pink > 0.08
            and logo_yellow > 0.03
            and logo_orange > 0.12
            and start_white > 0.45
            and legal_white > 0.55
            and legal_orange > 0.08
        ):
            return int(width * 0.50), int(height * 0.85)
        return None

    @staticmethod
    def _current_cn_title_start_point(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        logo_bgr = img[
            int(height * 0.57) : int(height * 0.81),
            int(width * 0.18) : int(width * 0.82),
        ]
        menu_bgr = img[
            int(height * 0.89) : int(height * 0.965),
            int(width * 0.86) : int(width * 0.99),
        ]
        if logo_bgr.size == 0 or menu_bgr.size == 0:
            return None
        logo_bgr = cv2.resize(logo_bgr, (460, 305), interpolation=cv2.INTER_AREA)
        menu_bgr = cv2.resize(menu_bgr, (90, 95), interpolation=cv2.INTER_AREA)
        logo = cv2.cvtColor(logo_bgr, cv2.COLOR_BGR2HSV)
        menu = cv2.cvtColor(menu_bgr, cv2.COLOR_BGR2HSV)
        logo_pink = np.mean(
            (logo[:, :, 0] >= 140)
            & (logo[:, :, 0] <= 175)
            & (logo[:, :, 1] > 60)
            & (logo[:, :, 2] > 90)
        )
        logo_yellow = np.mean(
            (logo[:, :, 0] >= 20)
            & (logo[:, :, 0] <= 38)
            & (logo[:, :, 1] > 70)
            & (logo[:, :, 2] > 100)
        )
        logo_white = np.mean((logo[:, :, 1] < 65) & (logo[:, :, 2] > 170))
        logo_edges = cv2.Canny(cv2.cvtColor(logo_bgr, cv2.COLOR_BGR2GRAY), 80, 160)
        logo_edge_ratio = np.mean(logo_edges > 0)
        menu_white = np.mean((menu[:, :, 1] < 65) & (menu[:, :, 2] > 170))
        menu_brown = np.mean(
            (menu[:, :, 0] >= 5)
            & (menu[:, :, 0] <= 30)
            & (menu[:, :, 1] > 50)
            & (menu[:, :, 2] > 80)
        )
        menu_blue = np.mean(
            (menu[:, :, 0] >= 90)
            & (menu[:, :, 0] <= 135)
            & (menu[:, :, 1] > 30)
            & (menu[:, :, 2] > 50)
        )
        if (
            logo_pink > 0.16
            and 0.06 < logo_yellow < 0.115
            and logo_white > 0.30
            and logo_edge_ratio > 0.11
            and menu_white > 0.40
            and menu_brown > 0.05
            and menu_blue > 0.025
        ):
            return int(width * 0.50), int(height * 0.85)
        return None

    @staticmethod
    def _looks_like_startup_progress(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        progress_line = hsv[int(height * 0.872) : int(height * 0.895), int(width * 0.16) : int(width * 0.84)]
        if progress_line.size == 0:
            return False
        bottom_action = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.40) : int(width * 0.98)]
        bottom_green = np.mean(
            (bottom_action[:, :, 0] >= 38)
            & (bottom_action[:, :, 0] <= 92)
            & (bottom_action[:, :, 1] > 70)
            & (bottom_action[:, :, 2] > 80)
        )
        if bottom_green > 0.10:
            return False
        progress_dark = np.mean(progress_line[:, :, 2] < 100)
        full_dark = np.mean(hsv[:, :, 2] < 100)
        full_blue = np.mean(
            (hsv[:, :, 0] >= 85)
            & (hsv[:, :, 0] <= 115)
            & (hsv[:, :, 1] > 50)
            & (hsv[:, :, 2] > 100)
        )
        top_notice = hsv[int(height * 0.07) : int(height * 0.15), int(width * 0.03) : int(width * 0.97)]
        notice_white = np.mean((top_notice[:, :, 1] < 65) & (top_notice[:, :, 2] > 170)) if top_notice.size else 0.0
        return bool(progress_dark > 0.10 and (full_blue > 0.35 or full_dark > 0.70 or notice_white > 0.60))

    @staticmethod
    def _received_login_reward_skip_point(
        img: np.ndarray,
        perception: dict[str, Any],
        screen_text: dict[str, Any],
    ) -> tuple[int, int] | None:
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        received_text_present = "获得了" in normalized
        if not screen_text.get("resource_terms") and not received_text_present:
            return None
        forbidden_terms = (
            "购买",
            "使用",
            "消费",
            "消耗",
            "支付",
            "补充",
            "恢复",
            "不足",
            "商店",
            "招募",
            "重试",
            "继续吗",
        )
        if any(term in normalized for term in forbidden_terms):
            return None
        if perception.get("buttons"):
            return None
        skip = SafeAdvancePlanner._bottom_right_skip_button(img)
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.03) : int(height * 0.18),
            int(width * 0.12) : int(width * 0.88),
        ]
        if header.size == 0:
            return None
        header_gold = np.mean(
            (header[:, :, 0] >= 10)
            & (header[:, :, 0] <= 38)
            & (header[:, :, 1] > 80)
            & (header[:, :, 2] > 140)
        )
        header_blue = np.mean(
            (header[:, :, 0] >= 85)
            & (header[:, :, 0] <= 125)
            & (header[:, :, 1] > 40)
            & (header[:, :, 2] > 100)
        )
        header_washed_out = np.mean(
            (header[:, :, 1] < 65) & (header[:, :, 2] > 170)
        )
        header_saturated = np.mean(
            (header[:, :, 1] > 80) & (header[:, :, 2] > 100)
        )
        anniversary_reward_animation = bool(
            received_text_present
            and skip is not None
            and header_washed_out > 0.75
            and header_saturated < 0.12
        )
        if (header_gold < 0.12 or header_blue < 0.35) and not anniversary_reward_animation:
            return None
        if skip is None and received_text_present:
            region = hsv[
                int(height * 0.88) : int(height * 0.985),
                int(width * 0.82) : int(width * 0.99),
            ]
            core = hsv[
                int(height * 0.91) : int(height * 0.975),
                int(width * 0.86) : int(width * 0.97),
            ]
            compact_white = np.mean(
                (region[:, :, 1] < 65) & (region[:, :, 2] > 170)
            )
            core_orange = np.mean(
                (core[:, :, 0] >= 5)
                & (core[:, :, 0] <= 25)
                & (core[:, :, 1] > 80)
                & (core[:, :, 2] > 120)
            )
            if compact_white > 0.15 and core_orange > 0.06:
                skip = (int(width * 0.91), int(height * 0.95))
        if skip is None:
            return None
        if not received_text_present:
            reward_panel = hsv[
                int(height * 0.45) : int(height * 0.76),
                int(width * 0.20) : int(width * 0.80),
            ]
            if reward_panel.size == 0:
                return None
            panel_white = np.mean(
                (reward_panel[:, :, 1] < 65)
                & (reward_panel[:, :, 2] > 175)
            )
            panel_color = np.mean(
                (reward_panel[:, :, 1] > 80)
                & (reward_panel[:, :, 2] > 100)
            )
            if panel_white < 0.55 or panel_color < 0.15:
                return None
        return skip

    @staticmethod
    def _training_pass_reset_close_button(
        screen_text: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        required_terms = (
            "训练通行证",
            "统计期间即将重置",
            "高级通行证",
            "购买",
            "关闭",
            "前往训练通行证",
        )
        if not all(term in normalized for term in required_terms):
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.30) : int(height * 0.35),
            int(width * 0.02) : int(width * 0.98),
        ]
        panel = hsv[
            int(height * 0.35) : int(height * 0.70),
            int(width * 0.02) : int(width * 0.98),
        ]
        left = hsv[
            int(height * 0.62) : int(height * 0.69),
            int(width * 0.08) : int(width * 0.48),
        ]
        right = hsv[
            int(height * 0.62) : int(height * 0.69),
            int(width * 0.52) : int(width * 0.92),
        ]
        if any(region.size == 0 for region in (header, panel, left, right)):
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        panel_white = np.mean((panel[:, :, 1] < 70) & (panel[:, :, 2] > 170))
        left_white = np.mean((left[:, :, 1] < 70) & (left[:, :, 2] > 170))
        left_brown = np.mean(
            (left[:, :, 0] >= 5)
            & (left[:, :, 0] <= 30)
            & (left[:, :, 1] > 50)
            & (left[:, :, 2] > 70)
        )
        right_green = np.mean(
            (right[:, :, 0] >= 38)
            & (right[:, :, 0] <= 92)
            & (right[:, :, 1] > 70)
            & (right[:, :, 2] > 80)
        )
        if not (
            header_green > 0.65
            and panel_white > 0.75
            and left_white > 0.75
            and left_brown > 0.01
            and right_green > 0.55
        ):
            return None
        return int(width * 0.27), int(height * 0.655)

    @staticmethod
    def _login_walk_reward_continue_point(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict) or screen_text.get("dangerous"):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        required_terms = (
            "活动",
            "登录散步奖励",
            "可以立刻和优俊少女外出散步了",
            "每天可通过登录散步奖励积蓄1次散步槽",
            "点击",
        )
        if not all(term in normalized for term in required_terms):
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center = hsv[
            int(height * 0.35) : int(height * 0.60),
            int(width * 0.25) : int(width * 0.75),
        ]
        bottom = hsv[
            int(height * 0.78) : int(height * 0.88),
            int(width * 0.25) : int(width * 0.75),
        ]
        if center.size == 0 or bottom.size == 0:
            return None
        center_cyan = np.mean(
            (center[:, :, 0] >= 85)
            & (center[:, :, 0] <= 110)
            & (center[:, :, 1] > 80)
            & (center[:, :, 2] > 120)
        )
        center_gold = np.mean(
            (center[:, :, 0] >= 10)
            & (center[:, :, 0] <= 35)
            & (center[:, :, 1] > 80)
            & (center[:, :, 2] > 120)
        )
        bottom_dark = np.mean(bottom[:, :, 2] < 100)
        if center_cyan < 0.03 or center_gold < 0.12 or bottom_dark < 0.45:
            return None
        return int(width * 0.50), int(height * 0.83)

    @staticmethod
    def _startup_promo_skip_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        # The ordinary in-career story shell keeps the persistent bottom
        # skip control visible.  Its right-side record/menu buttons can mimic
        # the promo's isolated bottom-right skip geometry, but must never be
        # tapped as a startup promo.  With double skip already enabled the
        # correct action is to wait for automatic story progression.
        if SpeedTogglePlanner._bottom_skip_level(img) in {"single", "double"}:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[
            int(height * 0.86) : int(height * 0.98),
            int(width * 0.82) : int(width * 0.99),
        ]
        top = hsv[0 : int(height * 0.12), :]
        middle = hsv[int(height * 0.15) : int(height * 0.80), :]
        center = hsv[
            int(height * 0.30) : int(height * 0.62),
            int(width * 0.12) : int(width * 0.88),
        ]
        if region.size == 0 or top.size == 0 or middle.size == 0 or center.size == 0:
            return None
        top_yellow = np.mean(
            (top[:, :, 0] >= 18)
            & (top[:, :, 0] <= 38)
            & (top[:, :, 1] > 60)
            & (top[:, :, 2] > 130)
        )
        top_red = np.mean(
            ((top[:, :, 0] <= 8) | (top[:, :, 0] >= 170))
            & (top[:, :, 1] > 70)
            & (top[:, :, 2] > 120)
        )
        middle_saturated = np.mean((middle[:, :, 1] > 80) & (middle[:, :, 2] > 100))
        center_yellow = np.mean(
            (center[:, :, 0] >= 18)
            & (center[:, :, 0] <= 38)
            & (center[:, :, 1] > 60)
            & (center[:, :, 2] > 130)
        )
        white = np.mean((region[:, :, 1] < 65) & (region[:, :, 2] > 170))
        orange = np.mean(
            (region[:, :, 0] >= 5)
            & (region[:, :, 0] <= 30)
            & (region[:, :, 1] > 55)
            & (region[:, :, 2] > 90)
        )
        green = np.mean(
            (region[:, :, 0] >= 38)
            & (region[:, :, 0] <= 92)
            & (region[:, :, 1] > 70)
            & (region[:, :, 2] > 80)
        )
        promo_visual = (top_yellow > 0.04 and middle_saturated > 0.35) or center_yellow > 0.06
        skip_button = (white > 0.30 and orange > 0.05) or (white > 0.10 and orange > 0.20)
        if top_red < 0.04 and promo_visual and skip_button and green < 0.05:
            return int(width * 0.92), int(height * 0.93)
        return None

    @staticmethod
    def _menu_panel_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.23) : int(height * 0.29),
            int(width * 0.01) : int(width * 0.99),
        ]
        panel = hsv[
            int(height * 0.30) : int(height * 0.78),
            int(width * 0.01) : int(width * 0.99),
        ]
        close = hsv[
            int(height * 0.72) : int(height * 0.80),
            int(width * 0.28) : int(width * 0.72),
        ]
        if header.size == 0 or panel.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 35)
            & (header[:, :, 0] <= 105)
            & (header[:, :, 1] > 30)
            & (header[:, :, 2] > 100)
        )
        header_dark = np.mean(header[:, :, 2] < 80)
        panel_green = np.mean(
            (panel[:, :, 0] >= 35)
            & (panel[:, :, 0] <= 105)
            & (panel[:, :, 1] > 30)
            & (panel[:, :, 2] > 100)
        )
        panel_white = np.mean((panel[:, :, 1] < 70) & (panel[:, :, 2] > 170))
        close_white = np.mean((close[:, :, 1] < 70) & (close[:, :, 2] > 170))
        close_brown = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 30)
            & (close[:, :, 1] > 50)
            & (close[:, :, 2] > 70)
        )
        close_blue = np.mean(
            (close[:, :, 0] >= 85)
            & (close[:, :, 0] <= 125)
            & (close[:, :, 1] > 50)
            & (close[:, :, 2] > 90)
        )
        if (
            header_green > 0.18
            and header_dark < 0.05
            and panel_green < 0.10
            and panel_white > 0.45
            and close_white > 0.65
            and close_brown > 0.01
        ):
            return int(width * 0.50), int(height * 0.718)
        return None

    @staticmethod
    def _announcement_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.03) : int(height * 0.08), int(width * 0.01) : int(width * 0.99)]
        body = hsv[int(height * 0.14) : int(height * 0.86), int(width * 0.01) : int(width * 0.99)]
        close = hsv[int(height * 0.89) : int(height * 0.965), int(width * 0.28) : int(width * 0.72)]
        if header.size == 0 or body.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        body_graywhite = np.mean((body[:, :, 1] < 90) & (body[:, :, 2] > 120))
        close_white = np.mean((close[:, :, 1] < 65) & (close[:, :, 2] > 170))
        close_brown = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 30)
            & (close[:, :, 1] > 50)
            & (close[:, :, 2] > 80)
        )
        close_orange = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 25)
            & (close[:, :, 1] > 80)
            & (close[:, :, 2] > 120)
        )
        has_close_text = (close_white > 0.35 and close_orange > 0.15) or (close_white > 0.80 and close_brown > 0.015)
        if header_green > 0.10 and body_graywhite > 0.45 and has_close_text:
            return int(width * 0.50), int(height * 0.92)
        return None

    @staticmethod
    def _trainee_detail_close_button(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if not isinstance(screen_text, dict):
            return None
        lines = tuple(
            "".join(line.split())
            for line in str(screen_text.get("visible_text", "")).splitlines()
            if line.strip()
        )
        if not (
            screen_text.get("dangerous") is False
            and not screen_text.get("resource_terms")
            and lines
            and lines[0] == "优俊少女详情"
            and lines[-1] == "关闭"
            and all(
                marker in lines
                for marker in (
                    "场地适应性",
                    "距离适应性",
                    "跑法适应性",
                    "养成信息",
                    "技能",
                )
            )
        ):
            return None
        return SafeAdvancePlanner._announcement_close_button(img)

    @staticmethod
    def _connection_conflict_return_title_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.30) : int(height * 0.35), int(width * 0.02) : int(width * 0.98)]
        body = hsv[int(height * 0.35) : int(height * 0.70), int(width * 0.02) : int(width * 0.98)]
        button = hsv[int(height * 0.61) : int(height * 0.69), int(width * 0.25) : int(width * 0.75)]
        if header.size == 0 or body.size == 0 or button.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        body_white = np.mean((body[:, :, 1] < 65) & (body[:, :, 2] > 170))
        button_white = np.mean((button[:, :, 1] < 65) & (button[:, :, 2] > 170))
        button_brown = np.mean(
            (button[:, :, 0] >= 5)
            & (button[:, :, 0] <= 30)
            & (button[:, :, 1] > 50)
            & (button[:, :, 2] > 80)
        )
        button_green = np.mean(
            (button[:, :, 0] >= 38)
            & (button[:, :, 0] <= 92)
            & (button[:, :, 1] > 70)
            & (button[:, :, 2] > 80)
        )
        if (
            header_green > 0.55
            and body_white > 0.85
            and button_white > 0.75
            and button_brown > 0.04
            and button_green < 0.05
        ):
            # L'Arc's supporter-points award shares this green/white modal
            # frame. Its foreground blue Pt badge plus large orange amount
            # identifies a reward, not the plain connection message. Abstain
            # here only; the existing page owner still decides any close tap.
            badge = hsv[int(height * 0.40) : int(height * 0.46), int(width * 0.28) : int(width * 0.43)]
            amount = hsv[int(height * 0.46) : int(height * 0.55), int(width * 0.25) : int(width * 0.77)]
            blue_pt = np.mean((badge[:, :, 0] > 92) & (badge[:, :, 0] < 125)
                              & (badge[:, :, 1] > 110) & (badge[:, :, 2] > 100))
            orange_amount = np.mean((amount[:, :, 0] > 5) & (amount[:, :, 0] < 35)
                                    & (amount[:, :, 1] > 75) & (amount[:, :, 2] > 180))
            if blue_pt > 0.02 and orange_amount > 0.04:
                return None
            return int(width * 0.50), int(height * 0.652)
        return None

    @staticmethod
    def _game_home_training_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        return global_home_portal_point(img)

    @staticmethod
    def _continue_training_modal_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        if perception.get("screen") != "game_home":
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.23) : int(height * 0.31), int(width * 0.01) : int(width * 0.99)]
        body = hsv[int(height * 0.31) : int(height * 0.78), int(width * 0.01) : int(width * 0.99)]
        if header.size == 0 or body.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        body_graywhite = np.mean((body[:, :, 1] < 90) & (body[:, :, 2] > 120))
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
        if header_green > 0.30 and body_graywhite > 0.55 and len(candidates) == 1:
            _area, x, y = candidates[0]
            return x, y
        return None

    @staticmethod
    def _continue_training_modal_text_verified(
        screen_text: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized = str(screen_text.get("normalized_text", ""))
        lines = {line.strip() for line in normalized.splitlines() if line.strip()}
        return (
            "继续养成" in normalized
            and "删除数据" in normalized
            and "取消" in lines
            and "继续" in lines
        )
