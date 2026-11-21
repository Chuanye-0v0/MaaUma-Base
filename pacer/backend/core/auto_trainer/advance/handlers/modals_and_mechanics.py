"""Grand Masters mechanics, wisdom fragment dialogs, tutorials, and modal buttons."""
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


class ModalsAndMechanicsHandlerMixin:
    """Scenario mechanics, wisdom dialogs, skill back controls, and safe modals."""

    @staticmethod
    def _looks_like_grand_masters_fragment_animation(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center = hsv[int(height * 0.14) : int(height * 0.32), int(width * 0.32) : int(width * 0.68)]
        background = hsv[int(height * 0.36) : int(height * 0.85), :]
        bottom_controls = hsv[int(height * 0.90) : int(height * 0.99), int(width * 0.20) : width]
        if center.size == 0 or background.size == 0 or bottom_controls.size == 0:
            return False
        center_bright = np.mean(center[:, :, 2] > 180)
        center_saturated = np.mean((center[:, :, 1] > 80) & (center[:, :, 2] > 100))
        center_red = np.mean(
            ((center[:, :, 0] <= 10) | (center[:, :, 0] >= 170))
            & (center[:, :, 1] > 60)
            & (center[:, :, 2] > 120)
        )
        center_white = np.mean((center[:, :, 1] < 65) & (center[:, :, 2] > 170))
        background_white = np.mean((background[:, :, 1] < 65) & (background[:, :, 2] > 170))
        bottom_dark = np.mean(bottom_controls[:, :, 2] < 90)
        if (
            center_bright > 0.45
            and center_saturated > 0.30
            and center_red > 0.18
            and center_white > 0.10
            and background_white < 0.04
            and bottom_dark > 0.35
        ):
            return True

        knowledge_table = hsv[int(height * 0.38) : int(height * 0.78), int(width * 0.05) : int(width * 0.95)]
        top_band = hsv[int(height * 0.16) : int(height * 0.29), : int(width * 0.80)]
        if knowledge_table.size == 0 or top_band.size == 0:
            return False
        knowledge_yellow = np.mean(
            (knowledge_table[:, :, 0] >= 15)
            & (knowledge_table[:, :, 0] <= 45)
            & (knowledge_table[:, :, 1] > 70)
            & (knowledge_table[:, :, 2] > 110)
        )
        knowledge_blue = np.mean(
            (knowledge_table[:, :, 0] >= 95)
            & (knowledge_table[:, :, 0] <= 130)
            & (knowledge_table[:, :, 1] > 60)
            & (knowledge_table[:, :, 2] > 80)
        )
        knowledge_white = np.mean((knowledge_table[:, :, 1] < 65) & (knowledge_table[:, :, 2] > 170))
        full_dark = np.mean(hsv[:, :, 2] < 120)
        top_green = np.mean(
            (top_band[:, :, 0] >= 38)
            & (top_band[:, :, 0] <= 92)
            & (top_band[:, :, 1] > 70)
            & (top_band[:, :, 2] > 80)
        )
        return bool(
            full_dark > 0.50
            and bottom_dark > 0.45
            and top_green > 0.25
            and knowledge_yellow > 0.18
            and knowledge_blue > 0.06
            and knowledge_white < 0.08
        )

    @staticmethod
    def _looks_like_grand_masters_wisdom_learn_animation(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        middle = hsv[int(height * 0.52) : int(height * 0.76), int(width * 0.10) : int(width * 0.90)]
        text_band = hsv[int(height * 0.62) : int(height * 0.82), int(width * 0.05) : int(width * 0.95)]
        bottom = hsv[int(height * 0.88) : height, :]
        center = hsv[int(height * 0.16) : int(height * 0.65), int(width * 0.18) : int(width * 0.82)]
        if middle.size == 0 or text_band.size == 0 or bottom.size == 0 or center.size == 0:
            return False
        middle_gold = np.mean(
            (middle[:, :, 0] >= 12)
            & (middle[:, :, 0] <= 40)
            & (middle[:, :, 1] > 70)
            & (middle[:, :, 2] > 130)
        )
        text_gold = np.mean(
            (text_band[:, :, 0] >= 12)
            & (text_band[:, :, 0] <= 40)
            & (text_band[:, :, 1] > 65)
            & (text_band[:, :, 2] > 120)
        )
        text_white = np.mean((text_band[:, :, 1] < 65) & (text_band[:, :, 2] > 185))
        bottom_controls = np.mean((bottom[:, :, 1] < 85) & (bottom[:, :, 2] > 120))
        character_foreground = np.mean(
            ((center[:, :, 0] < 25) | (center[:, :, 0] > 150))
            & (center[:, :, 1] > 35)
            & (center[:, :, 2] > 70)
        )
        return bool(
            middle_gold > 0.30
            and text_gold > 0.28
            and text_white > 0.12
            and character_foreground > 0.32
            and bottom_controls < 0.35
        )

    @staticmethod
    def _looks_like_unreadable_resource_modal(img: np.ndarray) -> bool:
        height, width = img.shape[:2]
        if width > height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def ratio(y0: float, y1: float, x0: float, x1: float, mask: np.ndarray) -> float:
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

        blur_variance = cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        legacy_blurred_modal = bool(
            blur_variance < 12.0
            and ratio(0.23, 0.30, 0.02, 0.98, green) > 0.45
            and ratio(0.31, 0.77, 0.02, 0.98, white) > 0.60
            and ratio(0.68, 0.77, 0.08, 0.48, white) > 0.55
            and ratio(0.68, 0.77, 0.52, 0.92, green) > 0.45
        )
        # Portrait final-confirmation cards use a bright green header and a
        # white cancel/green confirm pair near the bottom.  This is deliberately
        # a stop-only visual signature; it does not authorize either button.
        final_confirmation_modal = bool(
            ratio(0.02, 0.11, 0.03, 0.97, green) > 0.45
            and ratio(0.11, 0.84, 0.03, 0.97, white) > 0.65
            and ratio(0.88, 0.98, 0.05, 0.49, white) > 0.80
            and ratio(0.88, 0.98, 0.51, 0.95, green) > 0.35
        )
        return legacy_blurred_modal or final_confirmation_modal

    @staticmethod
    def _looks_like_gur_race_confirmation_text(
        screen_text: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(screen_text, dict) or screen_text.get("dangerous") is True:
            return False
        lines = {
            line.strip()
            for line in str(screen_text.get("normalized_text", "")).splitlines()
            if line.strip()
        }
        normalized = "".join(
            str(screen_text.get("normalized_text", "")).split()
        )
        upper = normalized.upper()
        return bool(
            "赛事详情" in normalized
            and "GROWUPRACE" in upper
            and "是否参加比赛" in normalized
            and "取消" in lines
            and "参赛" in lines
            and re.search(r"举办(?:初级|经典级|高级)12月下半月", normalized)
        )

    @staticmethod
    def _looks_like_wbc_race_confirmation(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> bool:
        if not isinstance(screen_text, dict) or screen_text.get("dangerous") is True:
            return False
        upper = "".join(
            str(screen_text.get("normalized_text", "")).upper().split()
        )
        return bool(
            "WBC" in upper
            and "MILE" in upper
            and SafeAdvancePlanner._looks_like_unreadable_resource_modal(img)
        )

    def _skill_evolution_action(
        self,
        img: np.ndarray,
        screen_text: dict[str, Any] | None,
    ) -> tuple[str, tuple[int, int]] | None:
        """Drive the post-career skill-evolution selection page.

        The selection page (技能进化选择) offers 不进化 / 进化 for each
        evolvable skill, then a confirm dialog (技能进化确认).  Points were
        recorded from the 2026-08-10 run; the evolve path is verified live,
        the no-evolve radio position is provisional.
        """

        normalized = ""
        if isinstance(screen_text, dict):
            normalized = "".join(
                str(screen_text.get("normalized_text", "")).split()
            )
        height, width = img.shape[:2]
        if width > height:
            return None
        scale_x = width / 720.0
        scale_y = height / 1280.0
        if "技能进化选择" in normalized and "请选择进化技能" in normalized:
            if not self._pending_skill_evolution_confirm:
                point = (
                    (174, 604)
                    if self.skill_evolution_choice == "evolve"
                    else (167, 468)
                )
                self._pending_skill_evolution_confirm = True
                return (
                    "select",
                    (
                        int(round(point[0] * scale_x)),
                        int(round(point[1] * scale_y)),
                    ),
                )
            self._pending_skill_evolution_confirm = False
            return (
                "confirm_selection",
                (
                    int(round(518 * scale_x)),
                    int(round(1181 * scale_y)),
                ),
            )
        if "技能进化确认" in normalized and "是否确定进化技能" in normalized:
            self._pending_skill_evolution_confirm = False
            return (
                "confirm_dialog",
                (
                    int(round(518 * scale_x)),
                    int(round(1182 * scale_y)),
                ),
            )
        self._pending_skill_evolution_confirm = False
        return None

    def _grand_masters_tutorial_continue_button(
        self,
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        candidate = SafeAdvancePlanner._fullscreen_continue_button(perception, img)
        if candidate is None or candidate[1] < int(height * 0.90):
            return None

        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        white = (hsv[:, :, 1] < 55) & (hsv[:, :, 2] > 175)

        button_green = float(np.mean(green[1165:1250, 215:510]))
        carousel_green = float(np.mean(green[1142:1166, 300:430]))
        description_white = float(np.mean(white[970:1140, 8:712]))
        if (
            button_green > 0.75
            and carousel_green > 0.75
            and description_white > 0.75
        ):
            return int(candidate[0]), int(candidate[1])
        return None

    @staticmethod
    def _grand_masters_tutorial_topic_menu_evidence(
        img: np.ndarray,
    ) -> dict[str, Any]:
        height, width = img.shape[:2]
        if width > height:
            return {"present": False, "reason": "landscape"}

        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        white = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 175)
        dark_text = (hsv[:, :, 1] > 30) & (hsv[:, :, 2] < 135)

        header_green = float(np.mean(green[210:290, 15:565]))
        option_rows = (343, 455, 567, 680, 792)
        option_white = [
            float(np.mean(white[y + 12 : y + 65, 35:650]))
            for y in option_rows
        ]
        option_dark_text = [
            float(np.mean(dark_text[y + 20 : y + 60, 65:330]))
            for y in option_rows
        ]
        skip_level = SpeedTogglePlanner._bottom_skip_level(img)
        candidate = bool(
            header_green >= 0.55
            and min(option_white[:4]) >= 0.78
            and skip_level in {"single", "double"}
        )
        fifth_row_confirmed = bool(
            option_white[4] >= 0.88
            and 0.05 <= option_dark_text[4] <= 0.18
            and min(option_dark_text[:4]) >= 0.15
        )
        return {
            "candidate": candidate,
            "present": bool(candidate and fifth_row_confirmed),
            "header_green": round(header_green, 4),
            "option_white": [round(value, 4) for value in option_white],
            "option_dark_text": [round(value, 4) for value in option_dark_text],
            "fifth_row_confirmed": fifth_row_confirmed,
            "skip_level": skip_level,
        }

    @staticmethod
    def _grand_masters_tutorial_offer_evidence(
        img: np.ndarray,
    ) -> dict[str, Any]:
        height, width = img.shape[:2]
        if width > height:
            return {"candidate": False, "reason": "landscape"}

        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        white = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 175)
        dark_text = (hsv[:, :, 1] > 30) & (hsv[:, :, 2] < 135)
        header_green = float(np.mean(green[210:290, 15:565]))
        option_rows = (680, 792)
        option_white = [
            float(np.mean(white[y + 12 : y + 65, 35:650]))
            for y in option_rows
        ]
        option_dark_text = [
            float(np.mean(dark_text[y + 20 : y + 60, 65:330]))
            for y in option_rows
        ]
        option_layout = detect_event_option_layout(img)
        option_count = int(option_layout["option_count"])
        skip_level = SpeedTogglePlanner._bottom_skip_level(img)
        candidate = bool(
            header_green >= 0.55
            and min(option_white) >= 0.85
            and min(option_dark_text) >= 0.10
            and max(option_dark_text) <= 0.18
            and option_count == 2
            and skip_level in {"single", "double"}
        )
        bottom = option_layout["points"].get("bottom")
        return {
            "candidate": candidate,
            "header_green": round(header_green, 4),
            "option_white": [round(value, 4) for value in option_white],
            "option_dark_text": [round(value, 4) for value in option_dark_text],
            "option_count": option_count,
            "bottom_point": list(bottom) if bottom is not None else None,
            "skip_level": skip_level,
        }

    @staticmethod
    def _grand_masters_tutorial_offer_text_confirmed(
        screen_text: dict[str, Any] | None,
    ) -> bool:
        if screen_text is None:
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        decline_term = "不听取说明"
        has_decline = decline_term in normalized
        has_independent_accept = "听取说明" in normalized.replace(decline_term, "")
        return has_decline and has_independent_accept

    @staticmethod
    def _grand_masters_tutorial_topic_text_confirmed(
        screen_text: dict[str, Any] | None,
    ) -> bool:
        if screen_text is None:
            return False
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        required = (
            "了解养成目标和特殊比赛",
            "了解知识的碎片",
            "了解知识的结晶",
            "了解女神的智慧",
        )
        return all(term in normalized for term in required)

    @staticmethod
    def _grand_masters_tutorial_finish_button(
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        normalized = cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        white = (hsv[:, :, 1] < 55) & (hsv[:, :, 2] > 175)

        center_white = float(np.mean(white[1165:1250, 218:503]))
        right_white = float(np.mean(white[1165:1250, 535:705]))
        description_white = float(np.mean(white[970:1140, 8:712]))
        carousel_green = float(np.mean(green[1142:1166, 300:430]))
        if (
            center_white > 0.75
            and right_white > 0.75
            and description_white > 0.80
            and carousel_green > 0.75
        ):
            return int(round(width * 0.50)), int(round(height * 0.943))
        return None

    @staticmethod
    def _classify_wisdom_fragment_dialog(
        img: np.ndarray,
    ) -> WisdomFragmentDialogResult:
        """Recognize the exact Grand Masters no-fragment confirmation."""

        height, width = img.shape[:2]
        normalized = SafeAdvancePlanner._normalize_wisdom_fragment_frame(img)
        if normalized is None:
            return WisdomFragmentDialogResult(WisdomFragmentDialogState.NO_MATCH)
        template_load = SafeAdvancePlanner._load_wisdom_fragment_dialog_template()
        if template_load.error is not None or template_load.template is None:
            return WisdomFragmentDialogResult(
                WisdomFragmentDialogState.ASSET_ERROR,
                asset_error=template_load.error or "template_load_failed",
            )
        template = template_load.template

        dialog_x, dialog_y, dialog_width, dialog_height = WISDOM_FRAGMENT_DIALOG_ROI
        candidate = normalized[
            dialog_y : dialog_y + dialog_height,
            dialog_x : dialog_x + dialog_width,
        ]
        if candidate.shape != template.shape:
            return WisdomFragmentDialogResult(WisdomFragmentDialogState.NO_MATCH)

        for roi, gray_threshold, edge_threshold, color_distance_threshold in (
            WISDOM_FRAGMENT_DIALOG_ANCHORS.values()
        ):
            if not SafeAdvancePlanner._wisdom_fragment_anchor_matches(
                candidate,
                template,
                roi,
                gray_threshold=gray_threshold,
                edge_threshold=edge_threshold,
                color_distance_threshold=color_distance_threshold,
            ):
                return WisdomFragmentDialogResult(WisdomFragmentDialogState.NO_MATCH)
        return WisdomFragmentDialogResult(
            WisdomFragmentDialogState.MATCH,
            tap=(int(round(width * (520 / 720))), int(round(height * (835 / 1280)))),
        )

    @staticmethod
    def _normalize_wisdom_fragment_frame(img: np.ndarray) -> np.ndarray | None:
        height, width = img.shape[:2]
        if (width, height) == (720, 1280):
            return img
        if (width, height) == (1440, 2560):
            return cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
        return None

    @staticmethod
    def _load_wisdom_fragment_dialog_template() -> _WisdomFragmentTemplateLoad:
        manifest_path = sa_attr('WISDOM_FRAGMENT_DIALOG_MANIFEST_PATH', WISDOM_FRAGMENT_DIALOG_MANIFEST_PATH)
        template_path = sa_attr('WISDOM_FRAGMENT_DIALOG_TEMPLATE_PATH', WISDOM_FRAGMENT_DIALOG_TEMPLATE_PATH)
        if not manifest_path.is_file():
            return _WisdomFragmentTemplateLoad(None, "manifest_missing")
        try:
            manifest_payload = manifest_path.read_bytes()
        except OSError:
            return _WisdomFragmentTemplateLoad(None, "manifest_read_failed")
        try:
            manifest = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _WisdomFragmentTemplateLoad(None, "manifest_invalid")
        if not isinstance(manifest, dict):
            return _WisdomFragmentTemplateLoad(None, "manifest_mismatch")

        expected_anchors = {
            name: list(spec[0]) for name, spec in WISDOM_FRAGMENT_DIALOG_ANCHORS.items()
        }
        source = manifest.get("source")
        template_entry = manifest.get("template")
        manifest_matches = (
            manifest.get("schema_version") == "uma.safe_advance_template.v1"
            and manifest.get("canonical_size") == [720, 1280]
            and isinstance(source, dict)
            and source.get("path") == WISDOM_FRAGMENT_DIALOG_SOURCE_PATH
            and source.get("sha256") == WISDOM_FRAGMENT_DIALOG_SOURCE_SHA256
            and source.get("size") == [720, 1280]
            and isinstance(template_entry, dict)
            and template_entry.get("path") == template_path.name
            and template_entry.get("sha256") == sa_attr('WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256', WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256)
            and template_entry.get("crop_roi_xywh") == list(WISDOM_FRAGMENT_DIALOG_ROI)
            and template_entry.get("anchors_xywh_relative_to_crop") == expected_anchors
        )
        if not manifest_matches:
            return _WisdomFragmentTemplateLoad(None, "manifest_mismatch")
        if not template_path.is_file():
            return _WisdomFragmentTemplateLoad(None, "template_missing")
        try:
            payload = template_path.read_bytes()
        except OSError:
            return _WisdomFragmentTemplateLoad(None, "template_read_failed")
        if hashlib.sha256(payload).hexdigest().upper() != sa_attr('WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256', WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256):
            return _WisdomFragmentTemplateLoad(None, "template_sha_mismatch")
        try:
            template = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        except cv2.error:
            return _WisdomFragmentTemplateLoad(None, "template_decode_failed")
        if template is None or template.shape != (530, 712, 3):
            return _WisdomFragmentTemplateLoad(None, "template_decode_failed")
        return _WisdomFragmentTemplateLoad(template)

    @staticmethod
    def _looks_like_wisdom_fragment_candidate(img: np.ndarray) -> bool:
        """Coarsely identify this modal family without consulting production assets."""

        normalized = SafeAdvancePlanner._normalize_wisdom_fragment_frame(img)
        if normalized is None:
            return False
        hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        white = (hsv[:, :, 1] < 70) & (hsv[:, :, 2] > 170)
        red = (
            ((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 170))
            & (hsv[:, :, 1] > 65)
            & (hsv[:, :, 2] > 100)
        )

        def ratio(mask: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> float:
            region = mask[y0:y1, x0:x1]
            return float(np.mean(region)) if region.size else 0.0

        return bool(
            ratio(green, 377, 439, 4, 716) > 0.75
            and ratio(white, 439, 735, 20, 700) > 0.90
            and ratio(red, 740, 785, 120, 600) > 0.05
            and ratio(white, 794, 871, 55, 344) > 0.70
            and ratio(green, 794, 871, 376, 664) > 0.75
        )

    @staticmethod
    def _wisdom_fragment_anchor_matches(
        candidate: np.ndarray,
        template: np.ndarray,
        roi: tuple[int, int, int, int],
        *,
        gray_threshold: float,
        edge_threshold: float,
        color_distance_threshold: float,
    ) -> bool:
        x, y, width, height = roi
        candidate_anchor = candidate[y : y + height, x : x + width]
        template_anchor = template[y : y + height, x : x + width]
        if candidate_anchor.shape != template_anchor.shape or candidate_anchor.size == 0:
            return False

        candidate_gray = cv2.cvtColor(candidate_anchor, cv2.COLOR_BGR2GRAY)
        template_gray = cv2.cvtColor(template_anchor, cv2.COLOR_BGR2GRAY)
        gray_score = float(
            cv2.matchTemplate(candidate_gray, template_gray, cv2.TM_CCOEFF_NORMED)[0, 0]
        )
        candidate_edges = cv2.Canny(candidate_gray, 60, 140)
        template_edges = cv2.Canny(template_gray, 60, 140)
        edge_score = float(
            cv2.matchTemplate(candidate_edges, template_edges, cv2.TM_CCOEFF_NORMED)[0, 0]
        )
        color_distance = float(
            np.mean(
                np.abs(candidate_anchor.astype(np.int16) - template_anchor.astype(np.int16))
            )
            / 255.0
        )
        return bool(
            np.isfinite(gray_score)
            and np.isfinite(edge_score)
            and gray_score >= gray_threshold
            and edge_score >= edge_threshold
            and color_distance <= color_distance_threshold
        )

    @staticmethod
    def _looks_like_forbidden_spend_modal(img: np.ndarray) -> bool:
        """Fail closed on spend icons or values inside this dialog's otherwise blank body."""

        normalized = SafeAdvancePlanner._normalize_wisdom_fragment_frame(img)
        if normalized is None or not SafeAdvancePlanner._looks_like_wisdom_fragment_candidate(img):
            return False

        # Currency, carrot, clock, TP, and shop badges all introduce saturated icon pixels
        # or dark value glyphs into an area that is blank white in the exact warning dialog.
        body = normalized[450:730, 40:680]
        hsv = cv2.cvtColor(body, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(body, cv2.COLOR_BGR2GRAY)
        icon_mask = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 70)).astype(np.uint8)
        value_mask = (gray < 190).astype(np.uint8)

        def largest_component(mask: np.ndarray) -> int:
            count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
            if count <= 1:
                return 0
            return int(np.max(stats[1:, cv2.CC_STAT_AREA]))

        return bool(largest_component(icon_mask) >= 40 or largest_component(value_mask) >= 18)

    @staticmethod
    def _learn_wisdom_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.02) : int(height * 0.10), int(width * 0.01) : int(width * 0.99)]
        body = hsv[int(height * 0.10) : int(height * 0.86), int(width * 0.01) : int(width * 0.99)]
        if header.size == 0 or body.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        body_yellow_or_blue = np.mean(
            (
                ((body[:, :, 0] >= 20) & (body[:, :, 0] <= 38))
                | ((body[:, :, 0] >= 85) & (body[:, :, 0] <= 125))
            )
            & (body[:, :, 1] > 60)
            & (body[:, :, 2] > 90)
        )
        body_red = np.mean(
            ((body[:, :, 0] <= 8) | (body[:, :, 0] >= 170))
            & (body[:, :, 1] > 60)
            & (body[:, :, 2] > 90)
        )
        body_yellow = np.mean(
            (body[:, :, 0] >= 20)
            & (body[:, :, 0] <= 38)
            & (body[:, :, 1] > 60)
            & (body[:, :, 2] > 90)
        )
        knowledge_graph_colours = bool(
            body_red > 0.01
            and (body_yellow > 0.04 or body_red > 0.04)
        )
        foreground_confirm = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.50) : int(width * 0.95)]
        foreground_cancel = hsv[int(height * 0.66) : int(height * 0.78), int(width * 0.05) : int(width * 0.50)]
        foreground_body = hsv[int(height * 0.60) : int(height * 0.82), int(width * 0.08) : int(width * 0.92)]
        foreground_confirm_green = (
            np.mean(
                (foreground_confirm[:, :, 0] >= 38)
                & (foreground_confirm[:, :, 0] <= 92)
                & (foreground_confirm[:, :, 1] > 70)
                & (foreground_confirm[:, :, 2] > 80)
            )
            if foreground_confirm.size
            else 0.0
        )
        foreground_cancel_white = (
            np.mean((foreground_cancel[:, :, 1] < 70) & (foreground_cancel[:, :, 2] > 175))
            if foreground_cancel.size
            else 0.0
        )
        foreground_body_graywhite = (
            np.mean((foreground_body[:, :, 1] < 90) & (foreground_body[:, :, 2] > 120))
            if foreground_body.size
            else 0.0
        )
        candidates: list[tuple[float, int, int]] = []
        foreground_candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.45 <= cx <= width * 0.88):
                continue
            if not (width * 0.30 <= w <= width * 0.55):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            area = float(button.get("area", 0))
            if height * 0.66 <= cy <= height * 0.78:
                foreground_candidates.append((area, int(cx), int(cy)))
            if height * 0.86 <= cy <= height * 0.98:
                candidates.append((area, int(cx), int(cy)))
        if (
            header_green > 0.35
            and foreground_confirm_green > 0.28
            and foreground_cancel_white > 0.75
            and foreground_body_graywhite > 0.80
            and knowledge_graph_colours
        ):
            if foreground_candidates:
                _area, x, y = max(foreground_candidates, key=lambda item: item[0])
                return x, y
            return int(width * 0.72), int(height * 0.717)
        if (
            header_green > 0.35
            and body_yellow_or_blue > 0.08
            and knowledge_graph_colours
            and len(candidates) == 1
        ):
            _area, x, y = candidates[0]
            return x, y
        return None

    @staticmethod
    def _knowledge_table_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.02) : int(height * 0.10), int(width * 0.01) : int(width * 0.99)]
        toast = hsv[int(height * 0.47) : int(height * 0.54), :]
        close = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.05) : int(width * 0.50)]
        right_button = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.50) : int(width * 0.95)]
        if header.size == 0 or toast.size == 0 or close.size == 0 or right_button.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        toast_dark = np.mean(toast[:, :, 2] < 100)
        close_white = np.mean((close[:, :, 1] < 65) & (close[:, :, 2] > 170))
        right_button_value_mean = float(np.mean(right_button[:, :, 2]))
        if header_green > 0.35 and close_white > 0.75 and (
            toast_dark > 0.30 or right_button_value_mean < 200.0
        ):
            return int(width * 0.28), int(height * 0.923)
        return None

    @staticmethod
    def _available_knowledge_table_close_button(
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.02) : int(height * 0.10),
            int(width * 0.01) : int(width * 0.99),
        ]
        close = hsv[
            int(height * 0.88) : int(height * 0.98),
            int(width * 0.05) : int(width * 0.50),
        ]
        right_button = hsv[
            int(height * 0.88) : int(height * 0.98),
            int(width * 0.50) : int(width * 0.95),
        ]
        if header.size == 0 or close.size == 0 or right_button.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        close_white = np.mean(
            (close[:, :, 1] < 65)
            & (close[:, :, 2] > 170)
        )
        right_green = np.mean(
            (right_button[:, :, 0] >= 38)
            & (right_button[:, :, 0] <= 92)
            & (right_button[:, :, 1] > 70)
            & (right_button[:, :, 2] > 80)
        )
        if header_green > 0.35 and close_white > 0.75 and right_green > 0.30:
            return int(width * 0.28), int(height * 0.923)
        return None

    @staticmethod
    def _grand_masters_effects_close_button(
        img: np.ndarray,
        screen_text: dict[str, Any] | None,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if not isinstance(screen_text, dict):
            return None
        normalized_text = "".join(
            str(
                screen_text.get("normalized_text")
                or screen_text.get("visible_text")
                or ""
            ).split()
        )
        if (
            "\u5973\u795e\u7684\u667a\u6167\u6548\u679c\u672a\u751f\u6548" not in normalized_text
            or "\u77e5\u8bc6\u7684\u7ed3\u6676\u6548\u679c" not in normalized_text
        ):
            return None

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.02) : int(height * 0.08),
            int(width * 0.01) : int(width * 0.99),
        ]
        section = hsv[
            int(height * 0.08) : int(height * 0.11),
            int(width * 0.02) : int(width * 0.98),
        ]
        body = hsv[
            int(height * 0.30) : int(height * 0.88),
            int(width * 0.04) : int(width * 0.96),
        ]
        close = hsv[
            int(height * 0.89) : int(height * 0.96),
            int(width * 0.30) : int(width * 0.70),
        ]
        if header.size == 0 or section.size == 0 or body.size == 0 or close.size == 0:
            return None

        def green_ratio(region: np.ndarray) -> float:
            return float(
                np.mean(
                    (region[:, :, 0] >= 38)
                    & (region[:, :, 0] <= 92)
                    & (region[:, :, 1] > 70)
                    & (region[:, :, 2] > 80)
                )
            )

        header_green = green_ratio(header)
        section_green = green_ratio(section)
        body_white = float(np.mean((body[:, :, 1] < 65) & (body[:, :, 2] > 170)))
        body_orange = float(
            np.mean(
                (body[:, :, 0] >= 5)
                & (body[:, :, 0] <= 35)
                & (body[:, :, 1] > 60)
                & (body[:, :, 2] > 100)
            )
        )
        close_white = float(np.mean((close[:, :, 1] < 65) & (close[:, :, 2] > 170)))
        if (
            header_green > 0.45
            and section_green > 0.35
            and body_white > 0.70
            and body_orange > 0.03
            and close_white > 0.80
        ):
            return int(width * 0.50), int(height * 0.923)
        return None

    @staticmethod
    def _race_reservation_list_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.02) : int(height * 0.10), int(width * 0.01) : int(width * 0.99)]
        tabs = hsv[int(height * 0.09) : int(height * 0.15), int(width * 0.01) : int(width * 0.99)]
        grid = hsv[int(height * 0.16) : int(height * 0.86), int(width * 0.02) : int(width * 0.98)]
        close = hsv[int(height * 0.88) : int(height * 0.98), int(width * 0.25) : int(width * 0.75)]
        if header.size == 0 or tabs.size == 0 or grid.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        tabs_white = np.mean((tabs[:, :, 1] < 70) & (tabs[:, :, 2] > 170))
        grid_gray = np.mean((grid[:, :, 1] < 55) & (grid[:, :, 2] > 95) & (grid[:, :, 2] < 195))
        grid_green = np.mean(
            (grid[:, :, 0] >= 38)
            & (grid[:, :, 0] <= 92)
            & (grid[:, :, 1] > 70)
            & (grid[:, :, 2] > 80)
        )
        close_white = np.mean((close[:, :, 1] < 65) & (close[:, :, 2] > 170))
        close_green = np.mean(
            (close[:, :, 0] >= 38)
            & (close[:, :, 0] <= 92)
            & (close[:, :, 1] > 70)
            & (close[:, :, 2] > 80)
        )
        if (
            header_green > 0.30
            and tabs_white > 0.45
            and grid_gray > 0.24
            and grid_green > 0.012
            and close_white > 0.82
            and close_green < 0.08
        ):
            return int(width * 0.50), int(height * 0.923)
        return None

    @staticmethod
    def _wisdom_learning_prompt_confirm_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        knowledge_upgrade = "提升女神的知识等级" in normalized
        fragment_blocked = "无法获得知识的碎片" in normalized
        already_acquired_state = "已获得智慧的状态下" in normalized
        wisdom_acquired = "已获得的智慧" in normalized or (
            "习得" in normalized and knowledge_upgrade
        )
        if not (
            wisdom_acquired
            and fragment_blocked
            and (knowledge_upgrade or already_acquired_state)
        ):
            return None
        return SafeAdvancePlanner._safe_green_confirm_button(perception, img)

    @staticmethod
    def _rest_outing_confirm_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if "休息&外出确认" not in normalized:
            return None
        return SafeAdvancePlanner._safe_green_confirm_button(perception, img)

    @staticmethod
    def _rest_confirm_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if "休息确认" not in normalized:
            return None
        return SafeAdvancePlanner._safe_green_confirm_button(
            perception, img, allow_obscured_background=True,
        )

    @staticmethod
    def _goddess_wisdom_acquisition_confirm_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        """Recognize the current goddess-wisdom acquisition confirmation.

        This runs before the generic unreadable-resource stop, but it is still
        fail-closed: a title/card OCR anchor and the existing strict green
        foreground-button geometry must both be present.
        """

        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict) or screen_text.get("dangerous"):
            return None
        if screen_text.get("errors") not in (None, (), []):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        title_anchor = (
            "\u5973\u795e" in normalized
            and "\u667a\u6167" in normalized
            and ("\u4e60\u5f97" in normalized or "\u83b7\u5f97" in normalized)
        )
        dialog_anchor = (
            "\u786e\u8ba4" in normalized
            or "\u5173\u95ed" in normalized
        )
        question_anchor = (
            "\u662f\u5426\u4e60\u5f97\u5973\u795e\u7684\u667a\u6167" in normalized
            and "\u53d1\u52a8\u6548\u679c" in normalized
        )
        if not (title_anchor and (dialog_anchor or question_anchor)):
            return None
        if perception.get("screen") != "modal_confirm":
            return None
        strict_button = SafeAdvancePlanner._safe_green_confirm_button(perception, img)
        if strict_button is not None:
            return strict_button
        return SafeAdvancePlanner._goddess_wisdom_foreground_learn_button(perception, img)

    @staticmethod
    def _goddess_wisdom_foreground_learn_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        """Select the lower learn button on the two-button wisdom modal.

        Some live layouts place the modal's green title band outside the
        legacy single-button ROI.  We only use this fallback after the
        goddess-specific OCR anchors and ``modal_confirm`` classification
        have passed, and require a matched upper/lower pair of detected
        foreground buttons rather than guessing a coordinate.
        """

        if perception.get("screen") != "modal_confirm":
            return None
        height, width = img.shape[:2]
        candidates: list[tuple[int, int, int, int, float]] = []
        for button in perception.get("buttons", []):
            if button.get("kind") != "primary_green":
                continue
            try:
                cx, cy = (int(button["center"][0]), int(button["center"][1]))
                _x, _y, bw, bh = (int(value) for value in button["bbox"])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if not (
                width * 0.34 <= cx <= width * 0.78
                and width * 0.25 <= bw <= width * 0.65
                and height * 0.03 <= bh <= height * 0.10
            ):
                continue
            if not (height * 0.62 <= cy <= height * 0.97):
                continue
            candidates.append((cx, cy, bw, bh, float(button.get("area", 0))))
        if len(candidates) < 2:
            return None
        candidates.sort(key=lambda item: item[1])
        upper, lower = candidates[-2], candidates[-1]
        if lower[1] - upper[1] < height * 0.10:
            return None
        if abs(lower[0] - upper[0]) > width * 0.08:
            return None
        if abs(lower[2] - upper[2]) > width * 0.12:
            return None
        return lower[0], lower[1]

    @staticmethod
    def _additional_data_download_confirm_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict) or screen_text.get("dangerous"):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        if not (
            "数据下载" in normalized
            and "下载附加数据" in normalized
            and re.search(r"\d+(?:\.\d+)?MB", normalized, flags=re.IGNORECASE)
        ):
            return None
        return SafeAdvancePlanner._safe_green_confirm_button(perception, img)

    @staticmethod
    def _safe_green_confirm_button(
        perception: dict[str, Any],
        img: np.ndarray,
        *,
        allow_obscured_background: bool = False,
    ) -> tuple[int, int] | None:
        allowed_screens = {"modal_confirm"}
        if allow_obscured_background:
            allowed_screens.update({"game_home", "portrait_ui", "unknown"})
        if perception.get("screen") not in allowed_screens:
            return None
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[int(height * 0.29) : int(height * 0.36), int(width * 0.02) : int(width * 0.98)]
        # The 720x1280 goddess confirmation is vertically higher than the
        # legacy modal layout.  Keep the legacy band, but accept the narrow
        # upper band only as a second visual proof; button geometry remains
        # the decisive foreground guard.
        alternate_header = hsv[int(height * 0.18) : int(height * 0.33), int(width * 0.02) : int(width * 0.98)]
        body = hsv[int(height * 0.34) : int(height * 0.70), int(width * 0.02) : int(width * 0.98)]
        if header.size == 0 or body.size == 0:
            return None
        header_green = max(
            np.mean(
                (header[:, :, 0] >= 38)
                & (header[:, :, 0] <= 92)
                & (header[:, :, 1] > 70)
                & (header[:, :, 2] > 80)
            ),
            np.mean(
                (alternate_header[:, :, 0] >= 38)
                & (alternate_header[:, :, 0] <= 92)
                & (alternate_header[:, :, 1] > 70)
                & (alternate_header[:, :, 2] > 80)
            ),
        )
        body_white = np.mean((body[:, :, 1] < 70) & (body[:, :, 2] > 175))
        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if button.get("kind") != "primary_green":
                continue
            if not (width * 0.28 <= cx <= width * 0.72):
                continue
            if not (height * 0.58 <= cy <= height * 0.74):
                continue
            if not (width * 0.30 <= w <= width * 0.55):
                continue
            if not (height * 0.04 <= h <= height * 0.09):
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if header_green <= 0.25 or body_white <= 0.55 or len(candidates) != 1:
            return None
        _area, x, y = candidates[0]
        return x, y

    @staticmethod
    def _fullscreen_continue_button(
        perception: dict[str, Any],
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        x0, x1 = int(width * 0.15), int(width * 0.85)
        y0, y1 = int(height * 0.84), int(height * 0.98)
        crop = img[y0:y1, x0:x1]
        if crop.size:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(
                hsv,
                np.array([38, 70, 80], dtype=np.uint8),
                np.array([92, 255, 255], dtype=np.uint8),
            )
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contour_candidates: list[tuple[float, int, int]] = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                area = cv2.contourArea(contour)
                if not (width * 0.25 <= w <= width * 0.65):
                    continue
                if not (height * 0.035 <= h <= height * 0.12):
                    continue
                contour_candidates.append((float(area), x0 + x + w // 2, y0 + y + h // 2))
            if contour_candidates:
                _score, x, y = max(contour_candidates, key=lambda item: item[0])
                return int(x), int(y)

        candidates: list[tuple[float, int, int]] = []
        for button in perception.get("buttons", []):
            cx, cy = button.get("center", [0, 0])
            _x, _y, w, h = button.get("bbox", [0, 0, 0, 0])
            if cy < height * 0.78:
                continue
            if not (width * 0.28 <= cx <= width * 0.72):
                continue
            if w < width * 0.25 or h < height * 0.04:
                continue
            candidates.append((float(button.get("area", 0)), int(cx), int(cy)))
        if not candidates:
            return None
        _score, x, y = max(candidates, key=lambda item: item[0])
        return x, y

    @staticmethod
    def _race_recommendation_top_confirm_button(
        evidence: dict[str, Any],
        perception: dict[str, Any],
        img: np.ndarray,
        *, current_setting_only: bool = False,
    ) -> tuple[int, int] | None:
        screen_text = evidence.get("screen_text")
        if not isinstance(screen_text, dict):
            return None
        normalized = "".join(str(screen_text.get("normalized_text", "")).split())
        required_text = ("赛事推荐功能", "定位舞台中心路线", "知名优俊少女路线",
                         "孤高自我之旅路线", "今后不再弹出该提示")
        if not ("赛事推荐" in normalized if current_setting_only else
                all(marker in normalized for marker in required_text)):
            return None

        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        if current_setting_only:
            white = (hsv[:, :, 1] < 75) & (hsv[:, :, 2] > 170)
            if any(float(np.mean(white[int(height*a):int(height*b),int(width*.05):int(width*.95)])) < .65
                    for a,b in ((.20,.36),(.40,.55),(.59,.74))):
                return None
            confirm = SafeAdvancePlanner._fullscreen_continue_button(perception, img)
            return confirm if confirm is not None and confirm[1] >= int(height*.88) else None
        corner_rois = (
            (0.021, 0.184, 0.090, 0.230),
            (0.910, 0.184, 0.979, 0.230),
            (0.021, 0.340, 0.090, 0.391),
            (0.910, 0.340, 0.979, 0.391),
        )
        corner_green: list[float] = []
        for x0, y0, x1, y1 in corner_rois:
            roi = hsv[
                int(height * y0) : int(height * y1),
                int(width * x0) : int(width * x1),
            ]
            if roi.size == 0:
                return None
            corner_green.append(
                float(
                    np.mean(
                        (roi[:, :, 0] >= 38)
                        & (roi[:, :, 0] <= 92)
                        & (roi[:, :, 1] > 70)
                        & (roi[:, :, 2] > 80)
                    )
                )
            )
        if any(ratio <= 0.07 for ratio in corner_green):
            return None

        confirm = SafeAdvancePlanner._fullscreen_continue_button(perception, img)
        if confirm is None or confirm[1] < int(height * 0.88):
            return None
        return confirm

    @staticmethod
    def _skill_acquisition_back_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        def crop_ratio(
            x0: float,
            x1: float,
            y0: float,
            y1: float,
            mask: np.ndarray,
        ) -> float:
            x_start, x_end = int(width * x0), int(width * x1)
            y_start, y_end = int(height * y0), int(height * y1)
            roi = mask[y_start:y_end, x_start:x_end]
            if roi.size == 0:
                return 0.0
            return float(np.mean(roi))

        green = (
            (hsv[:, :, 0] >= 38)
            & (hsv[:, :, 0] <= 92)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 80)
        )
        orange = (
            (hsv[:, :, 0] >= 5)
            & (hsv[:, :, 0] <= 35)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 100)
        )
        white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170)
        gray = (hsv[:, :, 1] < 50) & (hsv[:, :, 2] > 80) & (hsv[:, :, 2] < 220)
        purple = (
            (hsv[:, :, 0] >= 125)
            & (hsv[:, :, 0] <= 165)
            & (hsv[:, :, 1] > 20)
            & (hsv[:, :, 2] > 40)
        )

        skill_bar_green = crop_ratio(0.28, 0.97, 0.31, 0.39, green)
        skill_bar_white = crop_ratio(0.28, 0.97, 0.31, 0.39, white)
        list_white = crop_ratio(0.02, 0.98, 0.40, 0.80, white)
        list_purple = crop_ratio(0.02, 0.98, 0.40, 0.80, purple)
        plus_column_orange = crop_ratio(0.70, 0.96, 0.52, 0.80, orange)
        plus_column_gray = crop_ratio(0.70, 0.96, 0.52, 0.80, gray)
        confirm_green = crop_ratio(0.25, 0.75, 0.80, 0.92, green)
        back_white = crop_ratio(0.00, 0.25, 0.90, 1.00, white)

        if (
            skill_bar_green > 0.04
            and skill_bar_white > 0.45
            and list_white > 0.55
            and list_purple > 0.04
            and plus_column_orange > 0.10
            and plus_column_gray > 0.18
            and confirm_green > 0.25
            and back_white > 0.60
        ):
            return SafeAdvancePlanner._skill_acquisition_back_control(img)
        return None

    @staticmethod
    def _skill_acquisition_back_control(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        x0, x1 = 0, int(width * 0.25)
        y0, y1 = int(height * 0.86), int(height * 0.98)
        roi = hsv[y0:y1, x0:x1]
        if roi.size == 0:
            return None
        white = ((roi[:, :, 1] < 45) & (roi[:, :, 2] > 190)).astype(np.uint8)
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(white)
        candidates: list[tuple[int, int, int]] = []
        for index in range(1, count):
            x, y, box_width, box_height, area = (
                int(value) for value in stats[index]
            )
            absolute_x = x0 + x
            absolute_y = y0 + y
            if not int(width * 0.01) <= absolute_x <= int(width * 0.08):
                continue
            if not int(width * 0.12) <= box_width <= int(width * 0.20):
                continue
            if not int(height * 0.04) <= box_height <= int(height * 0.07):
                continue
            if not int(height * 0.88) <= absolute_y <= int(height * 0.94):
                continue
            if area < int(width * height * 0.005):
                continue
            candidates.append(
                (
                    area,
                    absolute_x + box_width // 2,
                    absolute_y + box_height // 2,
                )
            )
        if len(candidates) != 1:
            return None
        _area, center_x, center_y = candidates[0]
        return center_x, center_y

    @staticmethod
    def _grand_masters_crystal_reward_decision(
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> dict[str, Any]:
        decision = SafeAdvancePlanner._advance_bottom_dialogue(evidence, img)
        decision["reason"] = "grand_masters_crystal_reward"
        decision["route_state"] = "scenario_reward_continue"
        return decision

    @staticmethod
    def _looks_like_healthy_game_advisory(
        screen_text: dict[str, Any] | None,
        img: np.ndarray,
    ) -> bool:
        if not isinstance(screen_text, dict):
            return False
        normalized_text = str(screen_text.get("normalized_text", ""))
        if "游戏忠告" not in normalized_text:
            return False
        height, width = img.shape[:2]
        if width >= height:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        center = hsv[
            int(height * 0.15) : int(height * 0.90),
            int(width * 0.05) : int(width * 0.95),
        ]
        if center.size == 0:
            return False
        blue = np.mean(
            (center[:, :, 0] >= 90)
            & (center[:, :, 0] <= 135)
            & (center[:, :, 1] > 60)
            & (center[:, :, 2] > 80)
        )
        return bool(blue > 0.55)
