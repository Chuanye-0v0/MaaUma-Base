"""Backward-compatible facade for the modular RaceRoute state machine (`core.auto_trainer.racing`)."""
from __future__ import annotations

import cv2
import numpy as np

from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_perception import is_goal_achievement_title, match_main_menu_named_goal
from core.auto_trainer.frame_safety import (
    FormalActionSafetyGate,
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
)
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_race_page,
    larc_race_transition_kind,
    plan_larc_explanation,
)
from core.auto_trainer.race_clock_retry import _ordinary_result_blocked, retry_prompt_visible
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from core.auto_trainer.runtime_control import controlled_sleep, reobserving_step
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.auto_trainer.screen_graph import classify_landscape_race_screen
from core.auto_trainer.transition_wait import (
    GentleTransitionWaitPolicy,
    TransitionBudget,
    evaluate_gentle_transition_wait,
)
from core.frame_io import read_image_with_retry
from core.perception import Perception
from core.screen_text import analyze_screen_text, fuse_perception_with_text
from core.training_menu_precheck import TrainingMenuPrechecker

from core.auto_trainer.racing import (
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RACE_LIST_GRADE_REOBSERVATION_RETRIES,
    RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS,
    RACE_ROUTE_CONFIRM_TOKEN,
    RACE_SCENE_WAIT_REASONS,
    RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS,
    RESULT_TRANSITION_RECAPTURE_LIMIT,
    RaceClassifierMixin,
    RaceRouteExecutor,
    RaceRoutePlanner,
    RaceRoutePolicy,
    RaceRouteSession,
    SUPPORTED_RACE_ROUTE_SCREEN_SIZES,
    _RACE_ACCOUNT_CONFLICT_TEXT,
    _current_clock_retry,
    is_race_scene_wait_result,
    larc_exchange_entry_released,
)
