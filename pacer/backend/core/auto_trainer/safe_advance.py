"""Backward-compatible facade for the modular SafeAdvance state machine (`core.auto_trainer.advance`)."""
from __future__ import annotations

import cv2
import numpy as np

from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_ledger import (
    CareerGoalLedger,
    CareerGoalLedgerError,
    CareerGoalLedgerStore,
    build_count_goal_completion_receipt,
    build_named_goal_completion_receipt,
)
from core.auto_trainer.career_goal_perception import (
    CareerGoalPerceptionError,
    career_goal_observation_to_dict,
    is_goal_achievement_title,
    match_main_menu_count_goal,
    match_main_menu_named_goal,
    perceive_goal_page,
)
from core.auto_trainer.career_result_layouts import (
    diagonal_goal_band,
    exchange_history_continue_point,
    rating_continue_point,
)
from core.auto_trainer.career_start_perception import CareerStartPerceptor
from core.auto_trainer.career_start_safety import TesseractTsvTokenReader
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver, FreshFrameEvidence
from core.auto_trainer.event_options import (
    detect_event_option_layout,
    event_option_rows_ready,
    is_carrot_event_narrative,
    single_story_event_option_point,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate, FrameSafetyAnalyzer
from core.auto_trainer.global_home_layout import global_home_portal_point, is_global_home
from core.auto_trainer.larc_adaptation import (
    LarcAdaptationSession,
    plan_larc_adaptation,
    plan_larc_adaptation_entry,
    plan_larc_adaptation_return,
)
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_page,
    is_larc_race_day,
    plan_larc_explanation,
    plan_larc_ss_entry,
    plan_larc_ss_preview,
)
from core.auto_trainer.larc_race_day_context import read_larc_race_day_context
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from core.auto_trainer.route_transition_session import (
    POST_ACTION_ROUTE_SUCCESSORS,
    RouteTransitionError,
    RouteTransitionSession,
    RouteTransitionStatus,
)
from core.auto_trainer.runtime_control import controlled_sleep, reobserving_step
from core.auto_trainer.safe_advance_transition import (
    SafeAdvanceRoutePolicy,
    action_allowed_from_state,
    expected_next_states_for_decision,
    is_global_route_interrupt,
    observed_route_state,
)
from core.auto_trainer.screen_graph import ScreenGraph, classify_landscape_race_screen
from core.auto_trainer.skip_session import RunSkipSession, SkipSessionError, SkipSessionState
from core.auto_trainer.skill_handoff import (
    SkillAcquisitionHandoffSafetyError,
    SkillHandoffResumeAuthorization,
    SkillHandoffSession,
    validate_resume_authorization,
    validate_resume_session,
)
from core.auto_trainer.speed_toggle import SpeedTogglePlanner
from core.coordinates import CoordinateConfig, CoordinateScaler
from core.frame_io import read_image_with_retry
from core.grand_masters_overlay import detect_grand_masters_crystal_reward_overlay
from core.main_menu_state import HIGH_CONFIDENCE, analyze_main_menu_state
from core.perception import Perception
from core.screen_text import analyze_screen_text, fuse_perception_with_text
from core.skill_acquisition_parser import inspect_skill_acquisition_page
from core.training_menu_precheck import TrainingMenuPrechecker, TrainingMenuPrecheckResult

from core.auto_trainer.advance import (
    ADVANCE_CONFIRM_TOKEN,
    DIALOGUE_NEXT_POINT,
    GentleTransitionWaitPolicy,
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
    SafeAdvanceClassifierMixin,
    SafeAdvanceExecutor,
    SafeAdvancePlanner,
    SafeAdvanceTransitionMixin,
    SpecialPageWaitBudget,
    TransitionBudget,
    WISDOM_FRAGMENT_DIALOG_ANCHORS,
    WISDOM_FRAGMENT_DIALOG_MANIFEST_PATH,
    WISDOM_FRAGMENT_DIALOG_ROI,
    WISDOM_FRAGMENT_DIALOG_SOURCE_PATH,
    WISDOM_FRAGMENT_DIALOG_SOURCE_SHA256,
    WISDOM_FRAGMENT_DIALOG_TEMPLATE_PATH,
    WISDOM_FRAGMENT_DIALOG_TEMPLATE_SHA256,
    WisdomFragmentDialogResult,
    WisdomFragmentDialogState,
    _GLOBAL_HOME_PASSIVE_RESOURCE_TERMS,
    _OPTIONAL_NAVIGATION_PAIRS,
    _UNBOUND_GOAL_SUMMARY_ACTION_IDS,
    _UNBOUND_GOAL_SUMMARY_ACTION_PAIRS,
    _WisdomFragmentTemplateLoad,
    _career_end_skill_points_from_visible_text,
    _unused_skill_points_from_visible_text,
    evaluate_gentle_transition_wait,
    post_wait_navigation_reobserve,
)
