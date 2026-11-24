"""Story dialogue, skip toggles, movie fast-forward, and event option helpers."""
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


class DialogueAndEventsHandlerMixin:
    """Dialogue advancement, skip-level toggles, and single-option event handlers."""

    @staticmethod
    def _bottom_right_skip_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        region = hsv[int(height * 0.88) : int(height * 0.985), int(width * 0.82) : int(width * 0.99)]
        core = hsv[int(height * 0.91) : int(height * 0.975), int(width * 0.86) : int(width * 0.97)]
        if region.size == 0 or core.size == 0:
            return None
        region_white = np.mean((region[:, :, 1] < 65) & (region[:, :, 2] > 170))
        core_orange = np.mean(
            (core[:, :, 0] >= 5)
            & (core[:, :, 0] <= 25)
            & (core[:, :, 1] > 80)
            & (core[:, :, 2] > 120)
        )
        if region_white > 0.70 and core_orange > 0.06:
            return int(width * 0.91), int(height * 0.95)
        return None

    @staticmethod
    def _career_opening_movie_skip_button(img: np.ndarray) -> tuple[int,int] | None:
        return (SafeAdvancePlanner._story_bottom_right_fast_forward_button(img)
            or SafeAdvancePlanner._career_opening_closeup_fast_forward_button(img)
            or SafeAdvancePlanner._career_opening_subtitle_fast_forward_button(img))

    @staticmethod
    def _career_opening_subtitle_fast_forward_button(img: np.ndarray) -> tuple[int,int] | None:
        """Opening captions over bright scenery, with their real round Skip control."""
        height, width = img.shape[:2]
        if (width, height) != (720, 1280) or SpeedTogglePlanner._bottom_skip_state(img) in {'on','off'}:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        core = hsv[1184:1241,630:680]
        separator = hsv[1020:1027,20:350]
        body = hsv[1035:1160,50:600]
        white = np.mean((core[:,:,1]<65)&(core[:,:,2]>170))
        orange = np.mean((core[:,:,0]>=5)&(core[:,:,0]<=25)&(core[:,:,1]>80)&(core[:,:,2]>120))
        line = np.mean((separator[:,:,1]<65)&(separator[:,:,2]>180))
        caption = np.mean((body[:,:,1]<65)&(body[:,:,2]>175))
        if white>.60 and orange>.15 and line>.60 and .025<caption<.25:
            return (655,1215)
        return None

    @staticmethod
    def _story_bottom_right_fast_forward_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if SpeedTogglePlanner._bottom_skip_state(img) in {"on", "off"}:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        core = hsv[int(height * 0.925) : int(height * 0.970), int(width * 0.875) : int(width * 0.945)]
        dialogue = hsv[int(height * 0.78) : int(height * 0.95), int(width * 0.05) : int(width * 0.95)]
        full = hsv
        if core.size == 0 or dialogue.size == 0:
            return None
        core_white = np.mean((core[:, :, 1] < 65) & (core[:, :, 2] > 170))
        core_orange = np.mean(
            (core[:, :, 0] >= 5)
            & (core[:, :, 0] <= 25)
            & (core[:, :, 1] > 80)
            & (core[:, :, 2] > 120)
        )
        dialogue_dark = np.mean(dialogue[:, :, 2] < 90)
        dialogue_white = np.mean((dialogue[:, :, 1] < 65) & (dialogue[:, :, 2] > 175))
        full_brown = np.mean(
            (full[:, :, 0] >= 5)
            & (full[:, :, 0] <= 30)
            & (full[:, :, 1] > 50)
            & (full[:, :, 2] > 80)
        )
        full_dark = np.mean(full[:, :, 2] < 90)
        if (
            core_white > 0.60
            and core_orange > 0.15
            and dialogue_dark > 0.30
            and dialogue_white < 0.20
            and (full_brown > 0.25 or full_dark > 0.18)
        ):
            return int(width * 0.91), int(height * 0.95)
        return None

    @staticmethod
    def _career_opening_closeup_fast_forward_button(
        img: np.ndarray,
    ) -> tuple[int, int] | None:
        """Recognize the UI-free opening close-up before score-page heuristics."""

        height, width = img.shape[:2]
        if (width, height) != (720, 1280):
            return None
        if SpeedTogglePlanner._bottom_skip_state(img) in {"on", "off"}:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        core = hsv[
            int(height * 0.925) : int(height * 0.970),
            int(width * 0.875) : int(width * 0.945),
        ]
        body = img[
            int(height * 0.78) : int(height * 0.95),
            int(width * 0.05) : int(width * 0.95),
        ]
        if core.size == 0 or body.size == 0:
            return None
        core_white = float(
            np.mean((core[:, :, 1] < 65) & (core[:, :, 2] > 170))
        )
        core_orange = float(
            np.mean(
                (core[:, :, 0] >= 5)
                & (core[:, :, 0] <= 25)
                & (core[:, :, 1] > 80)
                & (core[:, :, 2] > 120)
            )
        )
        body_hsv = cv2.cvtColor(body, cv2.COLOR_BGR2HSV)
        body_white = float(
            np.mean(
                (body_hsv[:, :, 1] < 65)
                & (body_hsv[:, :, 2] > 175)
            )
        )
        body_edge = float(
            np.mean(
                cv2.Canny(
                    cv2.cvtColor(body, cv2.COLOR_BGR2GRAY),
                    80,
                    160,
                )
                > 0
            )
        )
        if (
            core_white > 0.60
            and core_orange > 0.15
            and body_white < 0.08
            and body_edge < 0.015
        ):
            return (655, 1215)
        return None

    @staticmethod
    def _race_story_fast_forward_button(
        img: np.ndarray,
        *,
        allow_with_bottom_skip: bool = False,
    ) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        if not allow_with_bottom_skip and SpeedTogglePlanner._bottom_skip_state(img) in {"on", "off"}:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        fast = hsv[int(height * 0.90) : int(height * 0.985), int(width * 0.70) : int(width * 0.84)]
        if fast.size == 0:
            return None
        white = np.mean((fast[:, :, 1] < 65) & (fast[:, :, 2] > 170))
        orange = np.mean(
            (fast[:, :, 0] >= 5)
            & (fast[:, :, 0] <= 25)
            & (fast[:, :, 1] > 80)
            & (fast[:, :, 2] > 120)
        )
        if white > 0.25 and orange > 0.04:
            return int(width * 0.77), int(height * 0.95)
        return None

    @staticmethod
    def _race_story_fast_forward_decision(
        evidence: dict[str, Any],
        point: tuple[int, int],
    ) -> dict[str, Any]:
        return {
            "action": "tap",
            "reason": "skip_race_story_scene",
            "tap": {
                "coordinate_name": "safe_advance.race_story_fast_forward",
                "x": point[0],
                "y": point[1],
                "scaled": False,
            },
            "evidence": evidence,
        }

    @staticmethod
    def _landscape_race_menu_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width <= height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.02) : int(height * 0.11),
            int(width * 0.22) : int(width * 0.78),
        ]
        panel = hsv[
            int(height * 0.11) : int(height * 0.98),
            int(width * 0.22) : int(width * 0.78),
        ]
        close = hsv[
            int(height * 0.82) : int(height * 0.95),
            int(width * 0.38) : int(width * 0.62),
        ]
        if header.size == 0 or panel.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 35)
            & (header[:, :, 0] <= 95)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        panel_white = np.mean((panel[:, :, 1] < 60) & (panel[:, :, 2] > 180))
        close_white = np.mean((close[:, :, 1] < 70) & (close[:, :, 2] > 170))
        close_brown = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 30)
            & (close[:, :, 1] > 45)
            & (close[:, :, 2] > 70)
        )
        if header_green > 0.45 and panel_white > 0.70 and close_white > 0.55 and close_brown > 0.01:
            return int(width * 0.50), int(height * 0.89)
        return None

    @staticmethod
    def _race_playback_menu_close_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        header = hsv[
            int(height * 0.23) : int(height * 0.31),
            int(width * 0.01) : int(width * 0.99),
        ]
        panel = hsv[
            int(height * 0.31) : int(height * 0.76),
            int(width * 0.03) : int(width * 0.97),
        ]
        menu_text = hsv[
            int(height * 0.31) : int(height * 0.52),
            int(width * 0.05) : int(width * 0.35),
        ]
        close = hsv[
            int(height * 0.68) : int(height * 0.78),
            int(width * 0.25) : int(width * 0.75),
        ]
        if header.size == 0 or panel.size == 0 or menu_text.size == 0 or close.size == 0:
            return None
        header_green = np.mean(
            (header[:, :, 0] >= 38)
            & (header[:, :, 0] <= 92)
            & (header[:, :, 1] > 70)
            & (header[:, :, 2] > 80)
        )
        panel_white = np.mean((panel[:, :, 1] < 70) & (panel[:, :, 2] > 170))
        menu_brown = np.mean(
            (menu_text[:, :, 0] >= 5)
            & (menu_text[:, :, 0] <= 30)
            & (menu_text[:, :, 1] > 50)
            & (menu_text[:, :, 2] > 70)
        )
        menu_orange = np.mean(
            (menu_text[:, :, 0] >= 5)
            & (menu_text[:, :, 0] <= 30)
            & (menu_text[:, :, 1] > 80)
            & (menu_text[:, :, 2] > 120)
        )
        menu_red = np.mean(
            (((menu_text[:, :, 0] <= 8) | (menu_text[:, :, 0] >= 170)))
            & (menu_text[:, :, 1] > 70)
            & (menu_text[:, :, 2] > 100)
        )
        close_white = np.mean((close[:, :, 1] < 70) & (close[:, :, 2] > 170))
        close_brown = np.mean(
            (close[:, :, 0] >= 5)
            & (close[:, :, 0] <= 30)
            & (close[:, :, 1] > 50)
            & (close[:, :, 2] > 70)
        )
        close_green = np.mean(
            (close[:, :, 0] >= 38)
            & (close[:, :, 0] <= 92)
            & (close[:, :, 1] > 70)
            & (close[:, :, 2] > 80)
        )
        if (
            header_green > 0.35
            and panel_white > 0.75
            and menu_brown > 0.045
            and menu_orange > 0.035
            and menu_red < 0.08
            and close_white > 0.70
            and close_brown > 0.01
            and close_green < 0.15
        ):
            return int(width * 0.50), int(height * 0.718)
        return None

    @staticmethod
    def _current_vr_opening_fast_forward_button(
        img: np.ndarray,
        screen_text: str,
    ) -> tuple[int, int] | None:
        """Recognize the current full-screen VR opening with its exact dialogue."""

        height, width = img.shape[:2]
        if (width, height) != (720, 1280):
            return None
        if SpeedTogglePlanner._bottom_skip_state(img) in {"on", "off"}:
            return None
        compact = re.sub(r"\s+", "", str(screen_text))
        if not all(term in compact for term in ("里见皇冠", "VR世界", "老师们", "陪伴大家")):
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        core = hsv[1184:1242, 630:681]
        if core.size == 0:
            return None
        core_white = float(
            np.mean((core[:, :, 1] < 65) & (core[:, :, 2] > 170))
        )
        core_orange = float(
            np.mean(
                (core[:, :, 0] >= 5)
                & (core[:, :, 0] <= 25)
                & (core[:, :, 1] > 80)
                & (core[:, :, 2] > 120)
            )
        )
        if core_white > 0.60 and core_orange > 0.15:
            return (655, 1215)
        return None

    @staticmethod
    def _single_green_event_option_button(img: np.ndarray) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        option_layout = detect_event_option_layout(img)
        if int(option_layout.get("option_count", 0)) != 1:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        option = hsv[int(height * 0.59) : int(height * 0.69), int(width * 0.02) : int(width * 0.98)]
        lower_dialogue = hsv[int(height * 0.70) : int(height * 0.90), int(width * 0.02) : int(width * 0.98)]
        right_edge = hsv[
            int(height * 0.60) : int(height * 0.68),
            int(width * 0.88) : int(width * 0.98),
        ]
        if option.size == 0 or lower_dialogue.size == 0:
            return None
        green = np.mean(
            (option[:, :, 0] >= 38)
            & (option[:, :, 0] <= 92)
            & (option[:, :, 1] > 70)
            & (option[:, :, 2] > 80)
        )
        white = np.mean((option[:, :, 1] < 70) & (option[:, :, 2] > 175))
        brown = np.mean(
            (option[:, :, 0] >= 5)
            & (option[:, :, 0] <= 30)
            & (option[:, :, 1] > 50)
            & (option[:, :, 2] > 80)
        )
        right_green = 0.0
        if right_edge.size:
            right_green = np.mean(
                (right_edge[:, :, 0] >= 38)
                & (right_edge[:, :, 0] <= 92)
                & (right_edge[:, :, 1] > 70)
                & (right_edge[:, :, 2] > 80)
            )
        lower_white = np.mean((lower_dialogue[:, :, 1] < 70) & (lower_dialogue[:, :, 2] > 175))
        if (
            green > 0.05
            and white > 0.35
            and right_green > 0.25
            and brown > 0.008
            and lower_white < 0.35
        ):
            return int(width * 0.50), int(height * 0.648)
        return None

    @staticmethod
    def _advance_dialogue(
        evidence: dict[str, Any],
        *,
        point: tuple[int, int] = DIALOGUE_NEXT_POINT,
        scaled: bool = True,
        coordinate_name: str = "safe_advance.dialogue_next",
    ) -> dict[str, Any]:
        return {
            "action": "tap",
            "reason": "advance_dialogue",
            "tap": {
                "coordinate_name": coordinate_name,
                "x": point[0],
                "y": point[1],
                "scaled": scaled,
            },
            "evidence": evidence,
        }

    @staticmethod
    def _advance_bottom_dialogue(
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> dict[str, Any]:
        height, width = img.shape[:2]
        return SafeAdvancePlanner._advance_dialogue(
            evidence,
            point=(int(width * 0.87), int(height * 0.84)),
            scaled=False,
            coordinate_name="safe_advance.dialogue_box",
        )

    @staticmethod
    def _skip_bottom_dialogue(
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> dict[str, Any]:
        skip_level = SpeedTogglePlanner._bottom_skip_level(img)
        evidence = dict(evidence)
        evidence["bottom_skip_level"] = skip_level
        if skip_level in {"off", "single"}:
            return SafeAdvancePlanner._enable_bottom_skip_toggle(evidence, img)
        if skip_level == "double":
            return {
                "action": "wait",
                "reason": "bottom_skip_double_enabled",
                "route_state": "auto_skip_wait",
                "evidence": evidence,
            }
        return SafeAdvancePlanner._ordinary_skip_fallback(evidence,img)

    @staticmethod
    def _ordinary_skip_fallback(evidence, img):
        """Skip acceleration is optional; ordinary semantic controls still own input."""
        from core.auto_trainer.advance.executor import SafeAdvanceExecutor

        evidence={**evidence,'optional_skip':'unavailable'}
        if ((SafeAdvancePlanner._looks_like_main_training_menu(img)
                and SafeAdvancePlanner._looks_like_training_home_controls(img))
                or SafeAdvanceExecutor._career_home_controls_ready(img)):
            return {**handoff('ready_main_menu',evidence=evidence),'route_state':'main_menu'}
        if (not event_option_rows_ready(img) and not SafeAdvancePlanner._looks_like_confirm_modal_panel(img)
                and SafeAdvancePlanner._looks_like_large_bottom_dialogue(img)
                and (SafeAdvancePlanner._looks_like_dialogue_continue_arrow(img)
                    or SafeAdvancePlanner._looks_like_training_story_dialogue(img)
                    or SafeAdvancePlanner._looks_like_green_nameplate_bottom_dialogue(img)
                    or SafeAdvancePlanner._looks_like_colored_nameplate_bottom_dialogue(img))):
            return SafeAdvancePlanner._advance_bottom_dialogue(evidence,img)
        return {'action':'wait','reason':'bottom_skip_state_unconfirmed','route_state':'auto_skip_wait','evidence':evidence}

    @staticmethod
    def _choose_default_event_option(
        option_points: dict[str, tuple[int, int]],
        option_count: int,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        choice = "bottom" if option_count >= 4 and "bottom" in option_points else "top"
        x, y = option_points[choice]
        return {
            "action": "tap",
            "reason": f"choose_event_{choice}",
            "route_state": "event_choice",
            "tap": {
                "coordinate_name": f"event_choice.{choice}",
                "x": int(x),
                "y": int(y),
                "scaled": False,
            },
            "evidence": evidence,
        }

    @staticmethod
    def _enable_bottom_skip_toggle(
        evidence: dict[str, Any],
        img: np.ndarray,
    ) -> dict[str, Any]:
        height, width = img.shape[:2]
        return {
            "action": "tap",
            "reason": "enable_bottom_skip_toggle",
            "tap": {
                "coordinate_name": "safe_advance.bottom_skip_toggle",
                "x": int(width * 0.358),
                "y": int(height * 0.975),
                "scaled": False,
            },
            "evidence": evidence,
        }
