"""SafeAdvanceExecutor with staged preflight, goal ledger, safety gate, and post-tap freeze."""
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
from types import SimpleNamespace
from core.auto_trainer.transition_wait import TransitionBudget
from ._compat import (
    FrameSafetyAnalyzer,
    Perception,
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


class SafeAdvanceExecutor:
    def __init__(
        self,
        *,
        adb_factory: Callable[[], Any] | None = None,
        planner: SafeAdvancePlanner | None = None,
        safety_analyzer: FrameSafetyAnalyzer | None = None,
        safety_gate: FormalActionSafetyGate | None = None,
        sleep_fn: Callable[[float], None] = controlled_sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adb_factory = adb_factory or self._default_adb_factory
        self.planner = planner or SafeAdvancePlanner()
        self.safety_analyzer = safety_analyzer or FrameSafetyAnalyzer()
        self.safety_gate = safety_gate or FormalActionSafetyGate()
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self._default_gentle_budget = TransitionBudget(clock=monotonic_fn)

    @staticmethod
    def _expected_clinic_cancel_decision(
        screenshot_path: Path,
        *,
        expected_action_id: str | None,
    ) -> dict[str, Any] | None:
        if expected_action_id != "safe_advance.clinic_confirm_cancel":
            return None
        try:
            from core.auto_trainer.executor import (
                CLINIC_CONFIRM_TEMPLATE_SHA256,
                analyze_clinic_confirm_modal,
            )

            verification = analyze_clinic_confirm_modal(screenshot_path)
            if (
                verification.get("accepted") is not True
                or verification.get("reason")
                != "clinic_confirm_template_verified"
            ):
                return None
            coordinates = CoordinateConfig()
            cancel = coordinates.require_verified("clinic_confirm", "cancel")
            if (
                cancel["verification"].get("source_sha256")
                != CLINIC_CONFIRM_TEMPLATE_SHA256
            ):
                return None
            image = cv2.imread(str(screenshot_path))
            if image is None:
                return None
            point = CoordinateScaler(
                coordinates.reference_size,
                (image.shape[1], image.shape[0]),
            ).scale_point(int(cancel["x"]), int(cancel["y"]))
        except (KeyError, OSError, TypeError, ValueError):
            return None
        return {
            "action": "tap",
            "reason": "cancel_clinic_confirmation",
            "route_state": "modal",
            "tap": {
                "coordinate_name": "safe_advance.clinic_confirm_cancel",
                "x": point[0],
                "y": point[1],
                "scaled": False,
            },
            "evidence": {"clinic_confirm": verification},
        }

    @staticmethod
    def _scope_global_home_training_navigation_packet(
        packet: Any,
        decision: dict[str, Any],
        screenshot_path: Path,
        *,
        expected_action_id: str | None,
    ) -> Any:
        """Ignore only fixed global-home status and navigation labels.

        These labels are not resource prompts for the independently detected
        training button, but the raw full-frame OCR correctly remains
        dangerous.  Produce a same-frame scoped packet only when every route
        and geometry binding below is exact; all other resource terms keep the
        original fail-closed packet.
        """

        tap = decision.get("tap")
        if (
            expected_action_id != "safe_advance.home_training"
            or decision.get("action") != "tap"
            or decision.get("reason") != "enter_training_from_home"
            or not isinstance(tap, dict)
            or tap.get("coordinate_name") != "safe_advance.home_training"
            or packet.text_status != "ok"
            or packet.errors
            or not packet.resource_terms
            or not set(packet.resource_terms).issubset(
                _GLOBAL_HOME_PASSIVE_RESOURCE_TERMS
            )
        ):
            return packet
        try:
            image_bytes = screenshot_path.read_bytes()
            if hashlib.sha256(image_bytes).hexdigest() != packet.source_image_sha256:
                return packet
            image = cv2.imread(str(screenshot_path))
            if image is None:
                return packet
            perception = Perception().analyze_image(image)
            training_point = SafeAdvancePlanner()._game_home_training_button(
                perception,
                image,
            )
            if training_point is None:
                return packet
            if (tap.get("x"), tap.get("y")) != training_point:
                return packet
        except (OSError, TypeError, ValueError):
            return packet
        return replace(packet, resource_terms=(), dangerous=False)

    @staticmethod
    def _loading_wait_wisdom_resume(
        report_path: str | Path, report_sha256: str, session: RouteTransitionSession,
        decision: dict[str, Any], fresh_sha256: str,
    ) -> tuple[RouteTransitionSession, dict[str, Any]]:
        """Amend one recorded wait contract, never execute or synthesize a wait."""
        error = "loading_wait_resume_evidence_invalid"
        raw = Path(report_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != report_sha256:
            raise RouteTransitionError(error)
        wrapper = json.loads(raw)
        report = wrapper["report"]
        steps = report["step_reports"]
        if wrapper.get("code") != 0 or len(steps) != 3 or report.get("stop_reason") != "route_state_out_of_scope":
            raise RouteTransitionError(error)
        wait, denied = steps[1]["result"], steps[2]["result"]
        legacy = ("dialogue", "event_choice", "loading", "main_menu", "race_list", "race_scene", "training_menu", "wbc_race_hub")
        before = session.to_dict()
        source = wait["frame_safety"]["source_image_sha256"]
        if (session.status != RouteTransitionStatus.TRACKING or session.current_state != "loading"
            or session.last_action_id != "wait:loading" or session.mismatch_state is not None
            or tuple(session.expected_next_states) != legacy
            or session.action_source_sha256 != source or session.observation_sha256 != source
            or any(value != before for value in (wait.get("route_transition"), denied.get("route_transition"), report.get("route_transition")))
            or wait.get("performed") is not True or wait.get("tap_performed") is True
            or wait.get("reason") != "loading" or wait["decision"].get("action") != "wait"
            or wait["decision"].get("reason") != "loading" or wait["decision"].get("tap") is not None
            or wait["frame_safety"].get("dangerous") or wait["frame_safety"].get("resource_terms")
            or wait["safety_verdict"] != {"action_id": None, "allowed": True, "reason": "wait", "source_image_sha256": source}
            or hashlib.sha256(Path(steps[1]["screenshot_path"]).read_bytes()).hexdigest() != source
            or denied.get("performed") is not False or denied.get("reason") != "route_state_out_of_scope"
            or fresh_sha256 == source):
            raise RouteTransitionError(error)
        for candidate in (denied["decision"], decision):
            if (candidate.get("action") != "tap" or candidate.get("reason") != "confirm_wisdom_learning_prompt"
                or candidate.get("tap", {}).get("coordinate_name") != "safe_advance.wisdom_learning_prompt_confirm"
                or observed_route_state(candidate) != "modal" or not action_allowed_from_state("modal", candidate)):
                raise RouteTransitionError("loading_wait_resume_wisdom_required")
        candidate = copy.deepcopy(session)
        candidate.commit_action("wait:loading", source_sha256=source, expected_next_states=(*legacy, "modal"))
        return candidate, {"kind": "contract_amendment", "new_tap": False, "new_wait": False,
            "action_id": "wait:loading", "source_sha256": source,
            "source_report": str(Path(report_path).resolve()), "source_report_sha256": report_sha256,
            "old_version": session.transition_version, "new_version": candidate.transition_version,
            "old_expected_next_states": list(session.expected_next_states),
            "new_expected_next_states": list(candidate.expected_next_states)}

    @reobserving_step
    def _reread_unreadable_resource_prompt(self, path, packet, decision, planner_kwargs):
        readings = []
        initial_sha = packet.source_image_sha256
        for _ in range(2):
            try:
                fresh = self.safety_analyzer.analyze(path)
            except (OSError, ValueError):
                readings.append({'status': 'read_failed'})
                continue
            if fresh.source_image_sha256 != initial_sha:
                readings.append({'status': 'source_changed'})
                break
            if fresh.dangerous or fresh.resource_terms:
                readings.append({'status': 'resource_terms_observed'})
                break
            readings.append({'status': fresh.text_status})
            if (fresh.text_status != 'ok' or fresh.errors or fresh.visible_text == packet.visible_text):
                continue
            resolved = self.planner.plan(path, **{**planner_kwargs, 'visible_text': fresh.visible_text})
            # A reread can hand off a passive page. It cannot authorize any input.
            if ((resolved.get('action') in {'stop', 'wait'} or is_route_handoff(resolved)) and not resolved.get('tap')
                    and resolved.get('reason') != 'resource_prompt_unreadable'):
                decision, packet = resolved, fresh
                break
        decision.setdefault('evidence', {})['resource_prompt_reread'] = readings
        return decision, packet

    def run_once(
        self,
        *,
        mode: str,
        execute: bool,
        confirm_token: str | None,
        scenario: str = "grand_masters",
        goal_completion_observe_only: bool = False,
        screenshot_path: str | Path | None = None,
        initial_frame: Any | None = None,
        wait_seconds: float = 0.0,
        settle_seconds: float = 0.0,
        post_wait_freezes: bool = True,
        post_wait_freeze_poll_seconds: float = POST_WAIT_FREEZE_POLL_SECONDS,
        post_wait_freeze_timeout_seconds: float = POST_WAIT_FREEZE_TIMEOUT_SECONDS,
        post_wait_freeze_stable_frames: int = POST_WAIT_FREEZE_STABLE_FRAMES,
        post_wait_freeze_max_frame_diff: float = POST_WAIT_FREEZE_MAX_FRAME_DIFF,
        allow_team_formation_confirm: bool = False,
        allow_return_main_menu_from_training: bool = False,
        allow_larc_ss: bool = False,
        allow_larc_first_arc_skill_inspection: bool = False,
        allow_larc_final_arc_skill_inspection: bool = False,
        larc_ss_preview_only: bool = False,
        larc_ss_preview_decider: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        larc_supporter_goal: dict[str, Any] | None = None,
        larc_adaptation_session: LarcAdaptationSession | None = None,
        larc_member_roster: Any | None = None,
        larc_member_roster_path: str | Path | None = None,
        larc_observed_turn: int | None = None,
        allow_enter_training_from_global_home: bool = False,
        allow_career_end_finish_after_skill_handoff: bool = False,
        expected_career_end_skill_points: int | None = None,
        expected_unused_skill_points: int | None = None,
        allow_finish_training_with_unused_skill_points: bool = False,
        allow_startup_promo_skip: bool = False,
        visible_text: str | None = None,
        expected_action_id: str | None = None,
        skill_handoff_authorization: SkillHandoffResumeAuthorization | None = None,
        skill_handoff_session: SkillHandoffSession | None = None,
        skip_session: RunSkipSession | None = None,
        transition_session: RouteTransitionSession | None = None,
        route_bootstrap_state: str | None = None,
        transition_resync: bool = False,
        loading_wait_resume_report: str | Path | None = None,
        loading_wait_resume_report_sha256: str | None = None,
        loading_wait_resume_session_path: str | Path | None = None,
        capture_observation_report: str | Path | None = None,
        capture_observation_report_sha256: str | None = None,
        capture_observation_invocation: str | Path | None = None,
        capture_observation_invocation_sha256: str | None = None,
        capture_observation_session_path: str | Path | None = None,
        special_page_wait_budget: SpecialPageWaitBudget | None = None,
        control_driver: CooperativeRunnerDriver | None = None,
        career_goal_observed_turn: int | None = None,
        career_goal_ledger_path: str | Path | None = None,
        career_goal_run_session_id: str | None = None,
        career_goal_ledger_store: CareerGoalLedgerStore | None = None,
        allow_unbound_goal_summary_continue: bool = False,
        allow_gentle_transition_wait: bool = False,
        gentle_transition_budget: TransitionBudget | None = None,
    ) -> dict[str, Any]:
        _ctx = SimpleNamespace(**{k: v for k, v in locals().items() if k != 'self'})
        for step in (
            self._run_stage_validate_and_paths,
            self._run_stage_capture_and_safety_read,
            self._run_stage_goal_ledger_and_handoff_args,
            self._run_stage_plan_and_safety_gate,
        ):
            early = step(_ctx)
            if early is not None:
                return early
        return self._run_stage_execute_and_post_freeze(_ctx)

    def _run_stage_validate_and_paths(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        scenario = _ctx.scenario
        screenshot_path = _ctx.screenshot_path
        post_wait_freezes = _ctx.post_wait_freezes
        post_wait_freeze_poll_seconds = _ctx.post_wait_freeze_poll_seconds
        post_wait_freeze_timeout_seconds = _ctx.post_wait_freeze_timeout_seconds
        post_wait_freeze_stable_frames = _ctx.post_wait_freeze_stable_frames
        post_wait_freeze_max_frame_diff = _ctx.post_wait_freeze_max_frame_diff
        larc_adaptation_session = _ctx.larc_adaptation_session
        larc_member_roster = _ctx.larc_member_roster
        larc_member_roster_path = _ctx.larc_member_roster_path
        larc_observed_turn = _ctx.larc_observed_turn
        allow_enter_training_from_global_home = _ctx.allow_enter_training_from_global_home
        allow_career_end_finish_after_skill_handoff = _ctx.allow_career_end_finish_after_skill_handoff
        expected_career_end_skill_points = _ctx.expected_career_end_skill_points
        expected_unused_skill_points = _ctx.expected_unused_skill_points
        allow_finish_training_with_unused_skill_points = _ctx.allow_finish_training_with_unused_skill_points
        allow_startup_promo_skip = _ctx.allow_startup_promo_skip
        expected_action_id = _ctx.expected_action_id
        skip_session = _ctx.skip_session
        transition_session = _ctx.transition_session
        route_bootstrap_state = _ctx.route_bootstrap_state
        transition_resync = _ctx.transition_resync
        loading_wait_resume_report = _ctx.loading_wait_resume_report
        loading_wait_resume_report_sha256 = _ctx.loading_wait_resume_report_sha256
        loading_wait_resume_session_path = _ctx.loading_wait_resume_session_path
        control_driver = _ctx.control_driver
        allow_unbound_goal_summary_continue = _ctx.allow_unbound_goal_summary_continue
        if any(value is not None for value in (
            larc_member_roster, larc_member_roster_path, larc_observed_turn,
        )) and (
            larc_member_roster is None or scenario != "larc"
            or type(larc_observed_turn) is not int or larc_observed_turn < 0
            or (mode == "live" and (
                larc_member_roster_path is None or transition_session is None
                or transition_session.run_session_id != larc_member_roster.run_session_id
            ))
        ):
            return {"performed": False, "reason": "larc_ss_observation_context_invalid", "decision": {}}
        if larc_adaptation_session is not None and (
            scenario != "larc" or transition_resync
            or (mode == "live" and (
                larc_adaptation_session.path is None or transition_session is None
                or transition_session.run_session_id != larc_adaptation_session.run_session_id
            ))
        ):
            return {"performed": False, "reason": "larc_adaptation_context_invalid", "decision": {}}
        if loading_wait_resume_report is not None or loading_wait_resume_report_sha256 is not None:
            if (not loading_wait_resume_report or not loading_wait_resume_report_sha256
                or transition_session is None or loading_wait_resume_session_path is None
                or transition_resync or route_bootstrap_state is not None):
                return {"performed": False, "reason": "loading_wait_resume_context_invalid", "decision": {}}
        self.validate_post_wait_freeze_options(
            poll_seconds=post_wait_freeze_poll_seconds,
            timeout_seconds=post_wait_freeze_timeout_seconds,
            stable_frames=post_wait_freeze_stable_frames,
            max_frame_diff=post_wait_freeze_max_frame_diff,
        )
        if (
            allow_career_end_finish_after_skill_handoff
            and expected_action_id != "safe_advance.career_end_finish"
        ):
            return {
                "performed": False,
                "reason": "career_end_finish_expected_action_required",
                "decision": {},
            }
        if expected_career_end_skill_points is not None and (
            type(expected_career_end_skill_points) is not int
            or expected_career_end_skill_points < 0
            or not allow_career_end_finish_after_skill_handoff
            or expected_action_id != "safe_advance.career_end_finish"
        ):
            return {
                "performed": False,
                "reason": "career_end_skill_points_context_invalid",
                "decision": {},
            }
        if expected_unused_skill_points is not None and (
            type(expected_unused_skill_points) is not int
            or expected_unused_skill_points <= 0
            or expected_action_id != "safe_advance.finish_training"
            or transition_session is None
            or transition_session.current_state != "modal"
            or transition_session.last_action_id != "safe_advance.career_end_finish"
            or "modal" not in transition_session.expected_next_states
            or transition_resync
            or route_bootstrap_state is not None
        ):
            return {
                "performed": False,
                "reason": "finish_training_unused_points_context_invalid",
                "decision": {},
            }
        if (
            allow_enter_training_from_global_home
            and expected_action_id != "safe_advance.home_training"
        ):
            return {
                "performed": False,
                "reason": "home_training_expected_action_required",
                "decision": {},
            }
        if (
            allow_startup_promo_skip
            and expected_action_id != "safe_advance.bottom_right_skip"
        ):
            return {
                "performed": False,
                "reason": "startup_promo_skip_expected_action_required",
                "decision": {},
            }
        if (
            allow_unbound_goal_summary_continue
            and expected_action_id not in _UNBOUND_GOAL_SUMMARY_ACTION_IDS
        ):
            return {
                "performed": False,
                "reason": "unbound_goal_summary_expected_action_required",
                "decision": {},
            }
        if allow_unbound_goal_summary_continue and not post_wait_freezes:
            return {
                "performed": False,
                "reason": "unbound_goal_summary_post_wait_freeze_required",
                "decision": {},
            }
        if allow_unbound_goal_summary_continue and (
            skip_session is not None
            or transition_session is not None
            or transition_resync
        ):
            return {
                "performed": False,
                "reason": "unbound_goal_summary_state_forbidden",
                "decision": {},
            }
        screenshot_path = Path(screenshot_path) if screenshot_path is not None else (
            Path(tempfile.gettempdir()) / "uma_safe_advance" / "screen.png"
        )
        post_tap_screenshot_path = screenshot_path.with_name(
            f"{screenshot_path.stem}.post_tap{screenshot_path.suffix or '.png'}"
        )

        if control_driver is not None:
            control_driver.reach("before_capture")
        _ctx.screenshot_path = screenshot_path
        _ctx.post_tap_screenshot_path = post_tap_screenshot_path
        return None

    def _run_stage_capture_and_safety_read(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        scenario = _ctx.scenario
        screenshot_path = _ctx.screenshot_path
        initial_frame = _ctx.initial_frame
        allow_larc_ss = _ctx.allow_larc_ss
        larc_ss_preview_only = _ctx.larc_ss_preview_only
        larc_adaptation_session = _ctx.larc_adaptation_session
        allow_enter_training_from_global_home = _ctx.allow_enter_training_from_global_home
        visible_text = _ctx.visible_text
        expected_action_id = _ctx.expected_action_id
        skip_session = _ctx.skip_session
        transition_session = _ctx.transition_session
        route_bootstrap_state = _ctx.route_bootstrap_state
        career_goal_observed_turn = _ctx.career_goal_observed_turn
        career_goal_ledger_path = _ctx.career_goal_ledger_path
        career_goal_run_session_id = _ctx.career_goal_run_session_id
        career_goal_ledger_store = _ctx.career_goal_ledger_store
        allow_unbound_goal_summary_continue = _ctx.allow_unbound_goal_summary_continue
        reader_started=time.monotonic()
        reader_timings={}
        adb = self.adb_factory()
        if mode == "live":
            if not adb.connect():
                return {"performed": False, "reason": "adb_connect_failed", "decision": {}}
            size = tuple(adb.get_screen_size())
            if size not in SUPPORTED_ADVANCE_SCREEN_SIZES:
                return {"performed": False, "reason": f"unsupported_screen_size:{size}", "decision": {}}
            reader_timings['connection_size_seconds']=round(time.monotonic()-reader_started,4)
            reader_started=time.monotonic()
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            if initial_frame is not None:
                adb.save_frame(str(screenshot_path), initial_frame)
            else:
                adb.screenshot(str(screenshot_path))
        elif mode != "dry-run":
            return {"performed": False, "reason": "mode", "decision": {}}

        reader_timings['capture_seconds']=round(time.monotonic()-reader_started,4) if mode=='live' else 0.
        reader_started=time.monotonic()
        settings_frame=cv2.imread(str(screenshot_path)) if scenario=="larc" and skip_session is not None else None
        settings_verified=settings_frame is not None and SafeAdvancePlanner._shortened_event_settings_action(settings_frame) is not None
        cache=getattr(self.safety_analyzer,"_larc_shorten_text_cache",None)
        settings_text=(cache[1] if settings_verified and cache and cache[0]==skip_session.run_session_id
            and transition_session is not None and transition_session.current_state=="modal"
            and transition_session.last_action_id in {"safe_advance.bottom_skip_toggle","safe_advance.shorten_all_events",
                "safe_advance.shorten_unread_toggle","wait:shorten_settings_skip_loading"} else None)
        movie_frame=cv2.imread(str(screenshot_path)) if scenario=="larc" and transition_session is not None else None
        movie_bound=(movie_frame is not None and SafeAdvancePlanner._career_opening_movie_skip_button(movie_frame) is not None
            and ((route_bootstrap_state=="career-opening")
                or transition_session.last_action_id in {"safe_advance.larc_opening_movie_skip","wait:larc_opening_movie_pending"}))
        ss_text=None
        ss_frame=None
        ss_text_source=None
        if scenario=='larc' and mode=='live' and transition_session is not None and (allow_larc_ss or larc_ss_preview_only):
            from core.auto_trainer.larc_ss_text_cache import cached_text
            ss_frame=movie_frame if movie_frame is not None else cv2.imread(str(screenshot_path))
            ss_text=cached_text(self.safety_analyzer,ss_frame,transition_session.run_session_id)
            if ss_text is not None:ss_text_source='larc_ss_unchanged_ui_cache'
            else:
                from core.auto_trainer.larc_preview_text import default_local_reader,ss_preview_text,ss_entry_text
                if default_local_reader(self.safety_analyzer):
                    ss_text=ss_preview_text(ss_frame)
                    if ss_text is not None:ss_text_source='larc_ss_preview_regions'
                    else:
                        ss_text=ss_entry_text(ss_frame)
                        if ss_text is not None:ss_text_source='larc_ss_entry_regions'
        adapt_text = None
        adapt_text_source = None
        if (scenario == 'larc' and mode == 'live' and larc_adaptation_session is not None
                and getattr(larc_adaptation_session, 'stage', None) != 'awaiting_result'
                and ss_text is None and settings_text is None and not movie_bound):
            from core.auto_trainer.larc_preview_text import default_local_reader, adaptation_grid_text
            if default_local_reader(self.safety_analyzer):
                adapt_frame = ss_frame if ss_frame is not None else (
                    movie_frame if movie_frame is not None else (
                        settings_frame if settings_frame is not None else cv2.imread(str(screenshot_path))))
                adapt_text = adaptation_grid_text(adapt_frame)
                if adapt_text is not None:
                    adapt_text_source = 'larc_adaptation_grid_regions'
        raw_packet = self.safety_analyzer.analyze(
            screenshot_path,
            supplied_text=visible_text if mode == "dry-run" else "" if movie_bound else settings_text if settings_text is not None else ss_text if ss_text is not None else adapt_text,
        )
        if adapt_text is not None and adapt_text_source is not None:
            raw_packet = replace(raw_packet, text_source=adapt_text_source)
        if ss_frame is not None:
            from core.auto_trainer.larc_ss_text_cache import remember_text
            if ss_text is not None:
                raw_packet=replace(raw_packet,text_source=ss_text_source)
            remember_text(self.safety_analyzer,ss_frame,transition_session.run_session_id,raw_packet)
        if mode=="live" and movie_bound:
            raw_packet=replace(raw_packet,text_source="larc_opening_movie_visual")
        if mode=="live" and settings_text is not None:
            raw_packet=replace(raw_packet,text_source="larc_shorten_settings_cache")
        if scenario=="larc" and skip_session is not None:
            if (settings_verified and raw_packet.text_status=="ok" and not raw_packet.errors
                    and not raw_packet.dangerous and not raw_packet.resource_terms and "缩短事件设置" in raw_packet.visible_text):
                self.safety_analyzer._larc_shorten_text_cache=(skip_session.run_session_id,raw_packet.visible_text)
            else:self.safety_analyzer._larc_shorten_text_cache=None
        reader_timings['text_seconds']=round(time.monotonic()-reader_started,4)
        reader_timings['text_source']=raw_packet.text_source
        skip_level: str | None = None
        if skip_session is not None:
            skip_frame = cv2.imread(str(screenshot_path))
            if skip_frame is None:
                return {
                    "performed": False,
                    "reason": "skip_session_observation:image_read",
                    "decision": {},
                    "frame_safety": raw_packet.to_dict(),
                    "skip_session": skip_session.to_dict(),
                }
            skip_level = SpeedTogglePlanner._bottom_skip_level(skip_frame)
            try:
                skip_session.observe(
                    skip_level,
                    source_sha256=raw_packet.source_image_sha256,
                )
            except SkipSessionError as exc:
                return {
                    "performed": False,
                    "reason": f"skip_session_observation:{exc}",
                    "decision": {},
                    "frame_safety": raw_packet.to_dict(),
                    "skip_session": skip_session.to_dict(),
                }
        packet = raw_packet
        career_start_destination = None
        passive_route_terms = {"TP", "RP", "招募"}
        if (
            raw_packet.text_status == "ok"
            and not raw_packet.errors
            and set(raw_packet.resource_terms).issubset(passive_route_terms)
            and expected_action_id == "safe_advance.scenario_continue"
        ):
            try:
                image_bytes = screenshot_path.read_bytes()
                destination, destination_safety = (
                    CareerStartPerceptor().analyze_destination(image_bytes)
                )
                actual_sha256 = hashlib.sha256(image_bytes).hexdigest()
                if (
                    raw_packet.source_image_sha256 == actual_sha256
                    and destination.page_state == "scenario_select_ready"
                    and destination.scenario_id == "grand_masters"
                    and destination.selected
                    and _post_binding_valid(
                        destination,
                        destination_safety,
                        actual_sha256,
                    )
                ):
                    packet = destination_safety.scoped_packet
                    career_start_destination = destination
            except Exception:
                packet = raw_packet
                career_start_destination = None
        # OCR box ordering alone does not bind the remaining-turns banner to a
        # particular goal card.  Keep goal persistence shadow-only until a
        # card/line binding proof exists.
        goal_text_order_verified = False
        planner_visible_text = packet.visible_text
        if (
            allow_enter_training_from_global_home
            and expected_action_id == "safe_advance.home_training"
            and packet.text_status == "ok"
            and not packet.errors
            and packet.resource_terms
            and set(packet.resource_terms).issubset(
                _GLOBAL_HOME_PASSIVE_RESOURCE_TERMS
            )
        ):
            planner_visible_text = None
        planner_kwargs: dict[str, Any] = {"visible_text": planner_visible_text}
        if scenario != "grand_masters":
            planner_kwargs["scenario"] = scenario
        if career_start_destination is not None:
            planner_kwargs["authenticated_scenario_select"] = True
        goal_config_values = (
            career_goal_observed_turn,
            career_goal_ledger_path,
            career_goal_run_session_id,
            career_goal_ledger_store,
        )
        goal_config_supplied = any(value is not None for value in goal_config_values)
        if allow_unbound_goal_summary_continue and goal_config_supplied:
            return {
                "performed": False,
                "reason": "unbound_goal_summary_goal_config_forbidden",
                "decision": {},
                "frame_safety": packet.to_dict(),
            }
        if goal_config_supplied:
            planner_kwargs["career_goal_text_order_verified"] = (
                goal_text_order_verified
            )
        if goal_config_supplied and (
            type(career_goal_observed_turn) is not int
            or career_goal_observed_turn <= 0
            or career_goal_ledger_path is None
            or type(career_goal_run_session_id) is not str
            or not career_goal_run_session_id
            or not isinstance(career_goal_ledger_store, CareerGoalLedgerStore)
        ):
            return {
                "performed": False,
                "reason": "career_goal_persistence_config_invalid",
                "decision": {},
                "frame_safety": packet.to_dict(),
            }
        _ctx.adb = adb
        _ctx.reader_timings = reader_timings
        _ctx.raw_packet = raw_packet
        _ctx.packet = packet
        _ctx.skip_level = skip_level
        _ctx.career_start_destination = career_start_destination
        _ctx.goal_text_order_verified = goal_text_order_verified
        _ctx.planner_kwargs = planner_kwargs
        _ctx.goal_config_supplied = goal_config_supplied
        return None

    def _run_stage_goal_ledger_and_handoff_args(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        execute = _ctx.execute
        scenario = _ctx.scenario
        goal_completion_observe_only = _ctx.goal_completion_observe_only
        screenshot_path = _ctx.screenshot_path
        allow_return_main_menu_from_training = _ctx.allow_return_main_menu_from_training
        allow_larc_ss = _ctx.allow_larc_ss
        allow_larc_first_arc_skill_inspection = _ctx.allow_larc_first_arc_skill_inspection
        allow_larc_final_arc_skill_inspection = _ctx.allow_larc_final_arc_skill_inspection
        larc_ss_preview_only = _ctx.larc_ss_preview_only
        larc_ss_preview_decider = _ctx.larc_ss_preview_decider
        larc_supporter_goal = _ctx.larc_supporter_goal
        larc_adaptation_session = _ctx.larc_adaptation_session
        larc_member_roster = _ctx.larc_member_roster
        larc_observed_turn = _ctx.larc_observed_turn
        allow_enter_training_from_global_home = _ctx.allow_enter_training_from_global_home
        allow_career_end_finish_after_skill_handoff = _ctx.allow_career_end_finish_after_skill_handoff
        expected_unused_skill_points = _ctx.expected_unused_skill_points
        allow_finish_training_with_unused_skill_points = _ctx.allow_finish_training_with_unused_skill_points
        allow_startup_promo_skip = _ctx.allow_startup_promo_skip
        expected_action_id = _ctx.expected_action_id
        skill_handoff_authorization = _ctx.skill_handoff_authorization
        skill_handoff_session = _ctx.skill_handoff_session
        transition_session = _ctx.transition_session
        career_goal_observed_turn = _ctx.career_goal_observed_turn
        career_goal_ledger_path = _ctx.career_goal_ledger_path
        career_goal_run_session_id = _ctx.career_goal_run_session_id
        career_goal_ledger_store = _ctx.career_goal_ledger_store
        allow_unbound_goal_summary_continue = _ctx.allow_unbound_goal_summary_continue
        packet = _ctx.packet
        planner_kwargs = _ctx.planner_kwargs
        goal_config_supplied = _ctx.goal_config_supplied
        goal_text_order_verified = _ctx.goal_text_order_verified
        connection_conflict_frame = cv2.imread(str(screenshot_path))
        connection_conflict_interrupt = bool(
            connection_conflict_frame is not None
            and SafeAdvancePlanner._connection_conflict_return_title_button(
                connection_conflict_frame
            )
            is not None
        )
        goal_completion = None
        goal_animation_pending = None
        completion_text = analyze_screen_text(packet.visible_text)["normalized_text"]
        completion_lines = completion_text.splitlines()
        # A top-of-screen mission toast may precede the goal card in OCR order.
        # Parse the card's exact title/body section while the safety checks
        # below still inspect the complete original packet and source frame.
        goal_title_index = next((index for index, line in enumerate(completion_lines)
            if is_goal_achievement_title(line, include_career_complete=True)), None)
        if goal_title_index is not None and goal_title_index > 0:
            goal_section = completion_lines[goal_title_index:]
            section_structure = (len(goal_section) == 2 or len(goal_section) == 3
                and goal_section[2] in {"完成!", "继续"})
            if section_structure and connection_conflict_frame is not None:
                section_text = "\n".join(goal_section)
                section_perception = fuse_perception_with_text(
                    Perception().analyze_image(connection_conflict_frame), analyze_screen_text(section_text))
                # The ordinary training HUD also says "目标达成". Only a
                # real completion-card layout may discard a preceding toast;
                # the HUD must remain available to the training/SS reader.
                section_layout = (SafeAdvancePlanner._goal_achieved_continue_button(
                    section_perception, connection_conflict_frame, goal_layout_only=True) is not None
                    or SafeAdvancePlanner._looks_like_goal_achievement_animation(
                        section_perception, connection_conflict_frame, goal_layout_only=True))
                if section_layout:
                    completion_lines, completion_text = goal_section, section_text
        all_goals_confirmation = None
        optional_goal_tracking = goal_completion_observe_only and scenario == "larc"
        if optional_goal_tracking:
            # The game owns goal completion. In ordinary L'Arc execution the
            # goal ledger is a report, not permission to press a free Continue.
            card = fuse_perception_with_text(Perception().analyze_image(connection_conflict_frame),
                analyze_screen_text(completion_text)) if connection_conflict_frame is not None else {}
            card_point = (SafeAdvancePlanner._goal_achieved_continue_button(card,
                connection_conflict_frame, goal_layout_only=True)
                if connection_conflict_frame is not None else None)
            if (card_point is not None and packet.text_status == "ok" and not packet.errors
                    and not packet.dangerous and not packet.resource_terms):
                goal_completion = {"status":"game_ui_verified" if mode == "live" and execute else "preview",
                    "run_session_id":(transition_session.run_session_id if transition_session else career_goal_run_session_id),
                    "source_frame_sha256":packet.source_image_sha256,"layout_verified":True,
                    "ledger_written":False,"basis":"game_reported_goal_card"}
                if completion_lines == ["养成目标达成!", "已达成所有目标!", "继续"]:
                    all_goals_confirmation = dict(goal_completion)
            completion_lines = []  # No target-name/ledger reconciliation gates.
        if not connection_conflict_interrupt and completion_lines == ["养成目标达成!", "已达成所有目标!", "继续"]:
            if not goal_config_supplied:
                return {"performed": False, "reason": "career_goal_persistence_config_required",
                        "decision": {}, "frame_safety": packet.to_dict()}
            try:
                if (packet.text_status != "ok" or packet.errors or packet.dangerous or packet.resource_terms
                    or hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256
                    or connection_conflict_frame is None or transition_session is None
                    or transition_session.run_session_id != career_goal_run_session_id):
                    raise CareerGoalLedgerError("all_goals_confirmation_source_invalid")
                goal_perception = fuse_perception_with_text(
                    Perception().analyze_image(connection_conflict_frame), analyze_screen_text(completion_text))
                continue_point = SafeAdvancePlanner._goal_achieved_continue_button(goal_perception, connection_conflict_frame)
                if continue_point is None:
                    raise CareerGoalLedgerError("all_goals_confirmation_layout_invalid")
                ledger = career_goal_ledger_store.load(Path(career_goal_ledger_path),
                    expected_run_session_id=career_goal_run_session_id)
                builtin_all_goals = (
                    scenario == "larc" and not ledger.goals and ledger.revision == 0
                    and transition_session.current_state == "dialogue"
                    and transition_session.last_action_id == "safe_advance.goal_achievement_continue"
                    and "dialogue" in transition_session.expected_next_states
                    and transition_session.action_source_sha256 is not None
                    and transition_session.action_source_sha256 != packet.source_image_sha256
                )
                if not builtin_all_goals and (not ledger.goals or ledger.active_goals()
                        or any(goal.status != "completed" for goal in ledger.goals)):
                    raise CareerGoalLedgerError("all_goals_confirmation_incomplete")
                all_goals_confirmation = {
                    "status": ("builtin_verified" if builtin_all_goals else "verified")
                        if mode == "live" and execute is True else "preview",
                    "run_session_id": ledger.run_session_id,
                    "source_frame_sha256": packet.source_image_sha256,
                    "normalized_text_sha256": hashlib.sha256(completion_text.encode("utf-8")).hexdigest(),
                    "ledger_revision": ledger.revision, "ledger_view_sha256": ledger.current_view_sha256,
                    "completed_goal_ids": [goal.goal_instance_id for goal in ledger.goals],
                    "layout_verified": True, "ledger_written": False,
                }
            except (CareerGoalLedgerError, OSError):
                return {"performed": False, "reason": "career_all_goals_confirmation_failed",
                        "decision": {}, "frame_safety": packet.to_dict()}
        if (not connection_conflict_interrupt and len(completion_lines) >= 2
            and is_goal_achievement_title(completion_lines[0])):
            if not goal_config_supplied:
                return {"performed": False, "reason": "career_goal_persistence_config_required",
                        "decision": {}, "frame_safety": packet.to_dict()}
            try:
                animation_without_marker = len(completion_lines) == 2
                if (packet.text_status != "ok" or packet.errors or packet.dangerous or packet.resource_terms
                    or hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256
                    or not (animation_without_marker or (
                        len(completion_lines) == 3 and completion_lines[2] in {"完成!", "继续"}))
                    or connection_conflict_frame is None):
                    raise CareerGoalLedgerError("goal_completion_source_invalid")
                goal_perception = fuse_perception_with_text(
                    Perception().analyze_image(connection_conflict_frame), analyze_screen_text(completion_text))
                continue_variant = len(completion_lines) == 3 and completion_lines[2] == "继续"
                layout_verified = (
                    SafeAdvancePlanner._goal_achieved_continue_button(goal_perception, connection_conflict_frame) is not None
                    if continue_variant else SafeAdvancePlanner._looks_like_goal_achievement_animation(goal_perception, connection_conflict_frame)
                )
                contract = match_main_menu_named_goal(completion_lines[1])
                count_contract = match_main_menu_count_goal(completion_lines[1]) if contract is None else None
                if count_contract is not None:
                    contract = count_contract
                builtin_target_id = TARGET_IDS.get(completion_lines[1])
                fan_goal = re.fullmatch(r"获得([1-9][0-9]*)名粉丝", completion_lines[1])
                if builtin_target_id is None and fan_goal is not None:
                    builtin_target_id = f"obtain_{fan_goal.group(1)}_fans"
                if builtin_target_id is None and scenario == "larc":
                    supporter_goal = re.fullmatch(r"获得([1-9][0-9]*)点协助者积分", completion_lines[1])
                    if supporter_goal is not None:
                        builtin_target_id = f"larc_obtain_{supporter_goal.group(1)}_supporter_points"
                    elif completion_lines[1] in LARC_SYSTEM_GOAL_IDS:
                        builtin_target_id = LARC_SYSTEM_GOAL_IDS[completion_lines[1]]
                if (
                    not layout_verified
                    and animation_without_marker
                    and (contract is not None or builtin_target_id is not None)
                ):
                    # Title/known target may render before the stamp and continue
                    # control. Do not write completion or stop the whole career.
                    return {
                        "performed": False,
                        "tap_performed": False,
                        "reason": "goal_achievement_loading",
                        "decision": {
                            "action": "wait",
                            "reason": "goal_achievement_loading",
                            "route_state": "loading",
                            "evidence": {
                                "image": str(screenshot_path),
                                "goal_completion": "waiting_for_stable_layout",
                            },
                        },
                        "frame_safety": packet.to_dict(),
                    }
                if not layout_verified or (
                    contract is None and builtin_target_id is None
                ):
                    raise CareerGoalLedgerError("goal_completion_layout_or_contract_invalid")
                ledger_path = Path(career_goal_ledger_path)
                ledger = career_goal_ledger_store.load(ledger_path, expected_run_session_id=career_goal_run_session_id)
                if builtin_target_id is not None:
                    if contract is not None or ledger.goals or ledger.revision != 0:
                        raise CareerGoalLedgerError("builtin_goal_ledger_conflict")
                    completion_evidence = {
                        "source_frame_sha256": packet.source_image_sha256,
                        "normalized_text_sha256": hashlib.sha256(
                            completion_text.encode("utf-8")
                        ).hexdigest(),
                        "target_id": builtin_target_id,
                        "layout_verified": True,
                        "layout": (
                            "goal_achievement_continue"
                            if continue_variant
                            else "goal_achievement_animation"
                        ),
                        "ledger_revision": ledger.revision,
                        "ledger_view_sha256": ledger.current_view_sha256,
                        "ledger_written": False,
                    }
                    if animation_without_marker:
                        goal_animation_pending = {
                            **completion_evidence,
                            "status": "waiting_for_completion_marker",
                        }
                    else:
                        goal_completion = {
                            **completion_evidence,
                            "status": (
                                "builtin_verified"
                                if mode == "live" and execute is True
                                else "preview"
                            ),
                        }
                else:
                    completion_event_kind = "named_goal_completion"
                    completion_builder = build_named_goal_completion_receipt
                    record_completion = career_goal_ledger_store.record_named_goal_completion
                    if count_contract is not None:
                        matching = tuple(goal for goal in ledger.goals
                            if goal.goal_kind == "graded_race_rank_count" and goal.grade == contract["grade"]
                            and goal.required_rank == contract["required_rank"] and goal.required_count == contract["required_count"]
                            and goal.progress_basis == "verified_lower_bound" and goal.deadline_turn is not None
                            and goal.completed_count >= goal.required_count)
                        completion_event_kind = "count_goal_completion"
                        completion_builder = build_count_goal_completion_receipt
                        record_completion = career_goal_ledger_store.record_count_goal_completion
                    else:
                        candidates = tuple(contract.get("candidate_race_ids", ()))
                        matching = tuple(goal for goal in ledger.goals
                            if goal.goal_kind == "named_race_rank" and goal.grade == contract["grade"]
                            and goal.required_rank == contract["required_rank"] and goal.required_count == 1
                            and goal.candidate_race_ids == candidates and goal.target_race_id is not None
                            and (goal.target_race_id in candidates if candidates else goal.target_race_id == contract["target_race_id"]))
                    if len(matching) != 1:
                        raise CareerGoalLedgerError("goal_completion_goal_ambiguous")
                    goal = matching[0]
                    completion_evidence = {"source_frame_sha256": packet.source_image_sha256,
                        "normalized_text_sha256": hashlib.sha256(completion_text.encode("utf-8")).hexdigest(),
                        "goal_instance_id": goal.goal_instance_id, "layout_verified": True,
                        "layout": "goal_achievement_continue" if continue_variant else "goal_achievement_animation"}
                    if animation_without_marker:
                        goal_animation_pending = {
                            **completion_evidence,
                            "status": "waiting_for_completion_marker",
                            "ledger_revision": ledger.revision,
                        }
                    elif goal.status == "completed":
                        goal_completion = completion_evidence
                        completion_event_kinds = {completion_event_kind}
                        if count_contract is not None:
                            completion_event_kinds.add("count_goal_main_menu_completion")
                        prior = next((revision for revision in ledger.revisions
                            if revision["event_kind"] in completion_event_kinds
                            and revision["source_sha256"] in goal.evidence_refs
                            and any(item["goal_instance_id"] == goal.goal_instance_id and item["status"] == "completed"
                                    for item in revision["view"]["goals"])), None)
                        if prior is None:
                            raise CareerGoalLedgerError("goal_completion_prior_receipt_missing")
                        goal_completion.update(status="already_persisted", receipt_sha256=prior["source_sha256"],
                                               ledger_revision=ledger.revision)
                    else:
                        goal_completion = completion_evidence
                        receipt = completion_builder(run_session_id=career_goal_run_session_id,
                            observed_turn=career_goal_observed_turn, active_goal=goal, source_path=screenshot_path,
                            source_frame_sha256=packet.source_image_sha256, visible_text=completion_text,
                            allow_continue_variant=continue_variant)
                        merge = record_completion(ledger, receipt)
                        goal_completion.update(status="preview", receipt=receipt.to_dict(), receipt_sha256=receipt.receipt_sha256,
                                               ledger_revision=merge.ledger.revision)
                        if mode == "live" and execute is True:
                            if merge.status == "updated":
                                career_goal_ledger_store.write_atomic(ledger_path, merge.ledger)
                            persisted = career_goal_ledger_store.load(ledger_path, expected_run_session_id=career_goal_run_session_id)
                            if persisted.to_dict() != merge.ledger.to_dict():
                                raise CareerGoalLedgerError("goal_completion_readback_mismatch")
                            goal_completion["status"] = "persisted"
                    if goal_completion is not None and (mode != "live" or execute is not True):
                        goal_completion["status"] = "preview"
            except (CareerGoalLedgerError, OSError) as exc:
                return {"performed": False, "reason": "career_goal_completion_failed",
                        "error": str(exc), "decision": {}, "frame_safety": packet.to_dict()}
        goal_observation = None
        if (not optional_goal_tracking and not connection_conflict_interrupt and goal_completion is None
            and goal_animation_pending is None and all_goals_confirmation is None):
            try:
                goal_observation = perceive_goal_page(
                    screenshot_path,
                    visible_text=packet.visible_text,
                    source_frame_sha256=packet.source_image_sha256,
                    observed_turn=(
                        career_goal_observed_turn if goal_config_supplied else 1
                    ),
                    text_order_verified=goal_text_order_verified,
                )
            except CareerGoalPerceptionError:
                return {
                    "performed": False,
                    "reason": "career_goal_observation_invalid",
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                }
        goal_summary_exception: dict[str, Any] | None = None
        if allow_unbound_goal_summary_continue:
            if goal_observation is None:
                return {
                    "performed": False,
                    "reason": "unbound_goal_summary_not_eligible",
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                }
            layout = goal_observation.field_evidence.get("layout", {})
            layout_verified = (
                isinstance(layout, Mapping) and layout.get("verified") is True
            )
            matched_reason = (
                goal_observation.abstain_reasons[0]
                if len(goal_observation.abstain_reasons) == 1
                else None
            )
            goal_summary_exception = {
                "matched_reason": matched_reason,
                "observation_sha256": goal_observation.observation_sha256,
                "source_image_sha256": packet.source_image_sha256,
                "layout_verified": layout_verified,
                "expected_action_id": expected_action_id,
                "actual_action_id": None,
                "expected_reason": next(
                    (
                        reason
                        for action_id, reason in _UNBOUND_GOAL_SUMMARY_ACTION_PAIRS
                        if action_id == expected_action_id
                    ),
                    None,
                ),
                "actual_reason": None,
                "ledger_written": False,
            }
            if not (
                packet.text_status == "ok"
                and not packet.errors
                and not packet.dangerous
                and not packet.resource_terms
                and goal_observation.screen == "goal_achievement_result"
                and layout_verified
                and goal_observation.source_frame_sha256
                == packet.source_image_sha256
                and matched_reason
                in {
                    "goal_current_row_unsupported",
                    "goal_text_order_unverified",
                    "goal_remaining_unbound_multi_goal_summary",
                }
                and not goal_observation.is_complete
            ):
                return {
                    "performed": False,
                    "reason": "unbound_goal_summary_not_eligible",
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                    "goal_summary_exception": goal_summary_exception,
                }
        if goal_observation is not None and goal_observation.screen != "unknown":
            if not goal_config_supplied:
                if not allow_unbound_goal_summary_continue:
                    return {
                        "performed": False,
                        "reason": "career_goal_persistence_config_required",
                        "decision": {},
                        "frame_safety": packet.to_dict(),
                    }
            if goal_config_supplied:
                planner_kwargs["career_goal_observed_turn"] = (
                    career_goal_observed_turn
                )
            if goal_observation.is_content_complete and mode == "live":
                ledger_path = Path(career_goal_ledger_path)
                try:
                    ledger = (
                        career_goal_ledger_store.load(
                            ledger_path,
                            expected_run_session_id=career_goal_run_session_id,
                        )
                        if ledger_path.exists()
                        else CareerGoalLedger.empty(
                            career_goal_run_session_id
                        )
                    )
                    merge = career_goal_ledger_store.merge_observation(
                        ledger,
                        goal_observation,
                    )
                    if merge.status == "updated":
                        career_goal_ledger_store.write_atomic(
                            ledger_path,
                            merge.ledger,
                        )
                    persisted = career_goal_ledger_store.load(
                        ledger_path,
                        expected_run_session_id=career_goal_run_session_id,
                    )
                except (CareerGoalLedgerError, OSError):
                    return {
                        "performed": False,
                        "reason": "career_goal_persistence_failed",
                        "decision": {},
                        "frame_safety": packet.to_dict(),
                    }
                observation_sha256 = goal_observation.observation_sha256
                bound_to_ledger = any(
                    revision.get("source_sha256") == observation_sha256
                    for revision in persisted.revisions
                ) or any(
                    goal.source_packet_sha256 == observation_sha256
                    or observation_sha256 in goal.evidence_refs
                    for goal in persisted.goals
                )
                if persisted.revision <= 0 or not bound_to_ledger:
                    return {
                        "performed": False,
                        "reason": "career_goal_persistence_failed",
                        "decision": {},
                        "frame_safety": packet.to_dict(),
                    }
                planner_kwargs[
                    "persisted_career_goal_observation_sha256"
                ] = observation_sha256
                planner_kwargs[
                    "persisted_career_goal_ledger_revision"
                ] = persisted.revision
        resume_requested = (
            skill_handoff_authorization is not None or skill_handoff_session is not None
        )
        if resume_requested:
            if (
                skill_handoff_authorization is None
                or skill_handoff_session is None
            ):
                return {
                    "performed": False,
                    "reason": "skill_handoff_resume_authorization:incomplete",
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                }
            try:
                resume_report = validate_resume_authorization(
                    skill_handoff_authorization
                )
            except SkillAcquisitionHandoffSafetyError as exc:
                return {
                    "performed": False,
                    "reason": f"skill_handoff_resume_authorization:{exc}",
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                }
            try:
                validate_resume_session(
                    skill_handoff_session,
                    skill_handoff_authorization,
                    resume_report,
                )
            except SkillAcquisitionHandoffSafetyError as exc:
                reason = str(exc)
                return {
                    "performed": False,
                    "reason": (
                        "skill_handoff_resume_already_consumed"
                        if reason == "already_consumed"
                        else f"skill_handoff_resume_session:{reason}"
                    ),
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                }
            planner_kwargs["allow_resume_after_skill_handoff"] = True
        if allow_return_main_menu_from_training:
            planner_kwargs["allow_return_main_menu_from_training"] = True
        if allow_larc_ss:
            planner_kwargs["allow_larc_ss"] = True
        if allow_larc_first_arc_skill_inspection:
            planner_kwargs["allow_larc_first_arc_skill_inspection"] = True
        if allow_larc_final_arc_skill_inspection:
            planner_kwargs["allow_larc_final_arc_skill_inspection"] = True
        if larc_ss_preview_only:
            planner_kwargs["larc_ss_preview_only"] = True
        if larc_ss_preview_decider is not None:
            def decide_bound_ss_preview(preview):
                if larc_member_roster is None:
                    return larc_ss_preview_decider(preview)
                try:
                    # The packet already carries run-local member bindings.
                    # Compare the same representation, without mutating the
                    # live roster before the original safety/save boundary.
                    roster_view = copy.deepcopy(larc_member_roster)
                    bound = dict(preview, participants_observation=roster_view.observe_ss_preview(
                        cv2.imread(str(screenshot_path)), preview["participants_observation"],
                        source={"run_session_id": roster_view.run_session_id,
                                "turn_index": larc_observed_turn,
                                "absolute_path": str(screenshot_path.resolve()),
                                "source_sha256": packet.source_image_sha256},
                    ))
                except (ValueError, OSError, KeyError) as exc:
                    return {"selected": False, "reason": f"larc_ss_observation:{exc}"}
                return larc_ss_preview_decider(bound)

            planner_kwargs["larc_ss_preview_decider"] = decide_bound_ss_preview
        if larc_supporter_goal is not None:
            planner_kwargs["larc_supporter_goal"] = larc_supporter_goal
        if larc_adaptation_session is not None:
            planner_kwargs["larc_adaptation_session"] = (
                copy.deepcopy(larc_adaptation_session) if mode == "dry-run" else larc_adaptation_session
            )
            adaptation_view = planner_kwargs["larc_adaptation_session"]
            if (adaptation_view.stage == "selecting" and adaptation_view.plan
                    and transition_session is not None
                    and transition_session.run_session_id == adaptation_view.run_session_id
                    and transition_session.current_state in {"modal", "loading"}
                    and transition_session.last_action_id == "safe_advance.larc_adaptation_review"):
                # Older sessions lack selection_ready. Their recorded review
                # already followed this plan's fixed-slot/selected-cost check.
                adaptation_view.selection_ready = True
        if allow_enter_training_from_global_home:
            planner_kwargs["allow_enter_training_from_global_home"] = True
        if allow_career_end_finish_after_skill_handoff:
            planner_kwargs["allow_career_end_finish_after_skill_handoff"] = True
        if expected_unused_skill_points is not None or allow_finish_training_with_unused_skill_points:
            planner_kwargs["allow_finish_training_with_unused_skill_points"] = True
        if expected_action_id == "safe_advance.recruit_return_home":
            planner_kwargs["allow_recruit_return_home"] = True
        if expected_action_id == "safe_advance.knowledge_hold_close":
            planner_kwargs["prefer_scenario_hold"] = True
        _ctx.goal_completion = goal_completion
        _ctx.goal_animation_pending = goal_animation_pending
        _ctx.all_goals_confirmation = all_goals_confirmation
        _ctx.optional_goal_tracking = optional_goal_tracking
        _ctx.goal_summary_exception = goal_summary_exception
        _ctx.resume_requested = resume_requested
        return None

    def _run_stage_plan_and_safety_gate(self, _ctx: SimpleNamespace) -> dict[str, Any] | None:
        mode = _ctx.mode
        execute = _ctx.execute
        confirm_token = _ctx.confirm_token
        scenario = _ctx.scenario
        screenshot_path = _ctx.screenshot_path
        allow_team_formation_confirm = _ctx.allow_team_formation_confirm
        allow_return_main_menu_from_training = _ctx.allow_return_main_menu_from_training
        larc_member_roster = _ctx.larc_member_roster
        larc_member_roster_path = _ctx.larc_member_roster_path
        larc_observed_turn = _ctx.larc_observed_turn
        expected_career_end_skill_points = _ctx.expected_career_end_skill_points
        expected_unused_skill_points = _ctx.expected_unused_skill_points
        allow_startup_promo_skip = _ctx.allow_startup_promo_skip
        expected_action_id = _ctx.expected_action_id
        skip_session = _ctx.skip_session
        transition_session = _ctx.transition_session
        route_bootstrap_state = _ctx.route_bootstrap_state
        transition_resync = _ctx.transition_resync
        loading_wait_resume_report = _ctx.loading_wait_resume_report
        loading_wait_resume_report_sha256 = _ctx.loading_wait_resume_report_sha256
        capture_observation_report = _ctx.capture_observation_report
        capture_observation_report_sha256 = _ctx.capture_observation_report_sha256
        capture_observation_invocation = _ctx.capture_observation_invocation
        capture_observation_invocation_sha256 = _ctx.capture_observation_invocation_sha256
        capture_observation_session_path = _ctx.capture_observation_session_path
        special_page_wait_budget = _ctx.special_page_wait_budget
        control_driver = _ctx.control_driver
        reader_timings = _ctx.reader_timings
        raw_packet = _ctx.raw_packet
        packet = _ctx.packet
        skip_level = _ctx.skip_level
        career_start_destination = _ctx.career_start_destination
        planner_kwargs = _ctx.planner_kwargs
        goal_completion = _ctx.goal_completion
        goal_animation_pending = _ctx.goal_animation_pending
        all_goals_confirmation = _ctx.all_goals_confirmation
        optional_goal_tracking = _ctx.optional_goal_tracking
        goal_summary_exception = _ctx.goal_summary_exception
        resume_requested = _ctx.resume_requested
        route_policy = None
        capture_resume_candidate = None
        capture_handoff = None
        if capture_observation_report is not None:
            try:
                if (mode != "live" or execute is not True or transition_session is None
                    or capture_observation_session_path is None or transition_resync or route_bootstrap_state
                    or loading_wait_resume_report or not allow_return_main_menu_from_training
                    or expected_action_id != "safe_advance.training_menu_back"):
                    raise RouteTransitionError("capture_handoff_context_invalid")
                from core.auto_trainer.capture_observation_handoff import prepare_capture_observation_handoff
                capture_resume_candidate, capture_handoff = prepare_capture_observation_handoff(
                    report_path=capture_observation_report, report_sha256=capture_observation_report_sha256,
                    invocation_path=capture_observation_invocation, invocation_sha256=capture_observation_invocation_sha256,
                    session=transition_session, image_path=screenshot_path, packet=packet)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                return {"performed": False, "reason": f"capture_observation_handoff:{exc}",
                        "decision": {}, "frame_safety": packet.to_dict()}
        state_constrained = (
            transition_session is not None or route_bootstrap_state is not None
        )
        if state_constrained:
            bootstrap_resync = bool(
                transition_resync and route_bootstrap_state is not None
            )
            route_policy = SafeAdvanceRoutePolicy.resolve(
                current_state=(
                    (capture_resume_candidate or transition_session).current_state
                    if transition_session is not None and not bootstrap_resync
                    else None
                ),
                expected_next_states=(
                    transition_session.expected_next_states
                    if transition_session is not None and not bootstrap_resync
                    else ()
                ),
                bootstrap_state=(route_bootstrap_state or None),
                last_action_id=(
                    transition_session.last_action_id
                    if transition_session is not None and not bootstrap_resync
                    else None
                ),
                action_source_sha256=(
                    transition_session.action_source_sha256
                    if transition_session is not None and not bootstrap_resync
                    else None
                ),
            )
            if not route_policy.allowed_observed_states:
                return {
                    "performed": False,
                    "reason": "route_context_missing",
                    "decision": {},
                    "frame_safety": packet.to_dict(),
                    "route_policy": {
                        "allowed_observed_states": [],
                        "source": route_policy.source,
                    },
                }
            planner_kwargs["route_policy"] = route_policy
        if control_driver is not None:
            control_driver.reach("before_decision")
        reader_started=time.monotonic()
        if not packet.dangerous and not packet.resource_terms and (_ctx.allow_gentle_transition_wait or _ctx.gentle_transition_budget is not None):
            planner_kwargs['allow_gentle_transition_wait'] = True
            planner_kwargs['gentle_transition_budget'] = (
                _ctx.gentle_transition_budget
                if _ctx.gentle_transition_budget is not None
                else self._default_gentle_budget
            )
        decision = self.planner.plan(screenshot_path, **planner_kwargs)
        if (
            _ctx.gentle_transition_budget is None
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
        if (mode == 'live' and scenario == 'larc' and decision.get('action') == 'stop'
                and decision.get('reason') == 'resource_prompt_unreadable'
                and not packet.dangerous and not packet.resource_terms):
            decision, packet = self._reread_unreadable_resource_prompt(
                screenshot_path, packet, decision, planner_kwargs)
        reader_timings['planning_seconds']=round(time.monotonic()-reader_started,4)
        if (skip_session is not None and (
                (decision.get('tap') or {}).get('coordinate_name')=='safe_advance.bottom_skip_toggle'
                and not skip_session.may_toggle(str(skip_level or 'absent'))
                or decision.get('reason')=='shorten_settings_skip_loading')):
            skip_session.defer(skip_session.deferred_reason or 'optional_control_unconfirmed')
            frame=cv2.imread(str(screenshot_path));evidence=decision.get('evidence') or {}
            normal_settings=(self.planner._common_shorten_settings_decision(frame,evidence)
                if decision.get('reason') in {'enable_shorten_settings_skip_toggle','shorten_settings_skip_loading'} else None)
            decision=normal_settings or SafeAdvancePlanner._ordinary_skip_fallback(evidence,frame)
        clinic_cancel = self._expected_clinic_cancel_decision(
            screenshot_path,
            expected_action_id=expected_action_id,
        )
        if clinic_cancel is not None:
            decision = clinic_cancel
        if decision.get("action") == "tap" and decision.get("reason") == "continue_goal_achievement_result":
            allowed_completion_statuses = (
                {"persisted", "already_persisted", "builtin_verified"}
                if mode == "live" and execute is True else {"preview"}
            )
            all_goals_allowed = (
                all_goals_confirmation is not None
                and decision.get("tap", {}).get("coordinate_name") == "safe_advance.goal_achievement_continue"
            )
            if not optional_goal_tracking and not all_goals_allowed and (goal_completion is None or goal_completion.get("status") not in allowed_completion_statuses):
                return {
                    "performed": False, "reason": "career_goal_completion_required",
                    "decision": decision, "frame_safety": packet.to_dict(),
                }
        special_page_wait = (
            special_page_wait_budget.observe(
                decision,
                source_image_sha256=packet.source_image_sha256,
            )
            if special_page_wait_budget is not None
            else None
        )
        if special_page_wait is not None and special_page_wait_budget.exhausted:
            return {
                "performed": False,
                "reason": "special_page_recovery_exhausted",
                "decision": {
                    "action": "stop",
                    "reason": "special_page_recovery_exhausted",
                    "route_state": "fail_closed",
                },
                "special_page_wait": special_page_wait,
                "frame_safety": packet.to_dict(),
            }
        decision_tap = decision.get("tap")
        decision_action_id = (
            decision_tap.get("coordinate_name")
            if isinstance(decision_tap, dict)
            else None
        )
        if goal_summary_exception is not None:
            goal_summary_exception["actual_action_id"] = decision_action_id
            decision_reason = decision.get("reason")
            goal_summary_exception["actual_reason"] = (
                decision_reason if isinstance(decision_reason, str) else None
            )
        home_training_navigation_scoped = False
        scoped_home_training_packet = (
            self._scope_global_home_training_navigation_packet(
                packet,
                decision,
                screenshot_path,
                expected_action_id=expected_action_id,
            )
        )
        if scoped_home_training_packet is not packet:
            packet = scoped_home_training_packet
            home_training_navigation_scoped = True
        if (
            decision_action_id == "safe_advance.scenario_continue"
            and career_start_destination is None
        ):
            return {
                "performed": False,
                "reason": "scenario_scope_required",
                "decision": {
                    "action": "stop",
                    "reason": "scenario_scope_required",
                },
                "frame_safety": packet.to_dict(),
            }
        if resume_requested and decision.get("reason") != "resume_after_skill_acquisition_handoff":
            return {
                "performed": False,
                "reason": "skill_handoff_resume_page_mismatch",
                "decision": decision,
                "frame_safety": packet.to_dict(),
            }
        if allow_team_formation_confirm:
            decision = self._team_formation_confirm_decision(decision, screenshot_path)
        loading_resume_candidate = None
        loading_contract_amendment = None
        if loading_wait_resume_report is not None:
            try:
                loading_resume_candidate, loading_contract_amendment = self._loading_wait_wisdom_resume(
                    loading_wait_resume_report, loading_wait_resume_report_sha256, transition_session,
                    decision, packet.source_image_sha256)
                route_policy = SafeAdvanceRoutePolicy.resolve(
                    current_state=loading_resume_candidate.current_state,
                    expected_next_states=loading_resume_candidate.expected_next_states, bootstrap_state=None,
                    last_action_id=loading_resume_candidate.last_action_id,
                    action_source_sha256=loading_resume_candidate.action_source_sha256)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                return {"performed": False, "reason": f"loading_wait_resume:{exc}",
                    "decision": decision, "frame_safety": packet.to_dict()}
        route_policy_evidence = None
        global_route_interrupt = False
        larc_read_only_wait = (scenario == "larc" and decision.get("action") == "wait"
            and not any(decision.get(key) for key in ("tap", "gesture", "keyevent"))
            and not packet.dangerous)
        larc_current_page_handoff = (scenario == "larc" and not packet.dangerous
            and ((is_route_handoff(decision) and decision.get("reason") in {"ready_main_menu", "ready_training_menu",
                "race_day_main_menu", "event_choice", "skill_acquisition_handoff", "career_end_confirmation",
                "terminal_rating_verified"}) or (decision.get("action") == "stop"
                and decision.get("reason") == "ready_larc_ss_preview")))
        if route_policy is not None:
            observed_state = observed_route_state(decision)
            global_route_interrupt = is_global_route_interrupt(decision)
            route_policy_evidence = {
                "allowed_observed_states": list(route_policy.allowed_observed_states),
                "source": route_policy.source,
                "observed_state": observed_state,
                "source_image_sha256": packet.source_image_sha256,
                "pending_action_id": route_policy.pending_action_id,
                "action_source_sha256": route_policy.action_source_sha256,
            }
            if not (
                route_policy.allows_observed_state(observed_state)
                or global_route_interrupt
                or larc_read_only_wait or larc_current_page_handoff
            ):
                return {
                    "performed": False,
                    "reason": "route_state_out_of_scope",
                    "decision": decision,
                    "frame_safety": packet.to_dict(),
                    "route_policy": route_policy_evidence,
                }
            if (
                decision.get("action") in {"tap", "wait"}
                and not global_route_interrupt
                and not larc_read_only_wait
                and not action_allowed_from_state(observed_state, decision)
            ):
                return {
                    "performed": False,
                    "reason": "route_action_out_of_scope",
                    "decision": decision,
                    "frame_safety": packet.to_dict(),
                    "route_policy": route_policy_evidence,
                }
        transition_view = None
        transition_expected_states: tuple[str, ...] = ()
        record_global_transition = False
        staged_live_tap = False
        if transition_session is not None:
            record_global_transition = (
                global_route_interrupt
                and decision.get("action") in {"tap", "wait"}
                and (
                    decision.get("action") == "tap"
                    or transition_session.status == RouteTransitionStatus.BOOTSTRAP
                )
            )
            staged_live_tap = (
                mode == "live"
                and decision.get("action") == "tap"
                and (not global_route_interrupt or record_global_transition)
            )
            transition_view = (
                copy.deepcopy(capture_resume_candidate or loading_resume_candidate or transition_session)
                if mode == "dry-run" or staged_live_tap or (goal_completion is not None and goal_completion["status"] == "preview")
                else transition_session
            )
            if (not global_route_interrupt or record_global_transition) and not larc_read_only_wait:
                try:
                    keep_ss_event_owner = (not transition_resync and larc_current_page_handoff
                        and observed_state == "training_menu"
                        and transition_view.last_action_id == "safe_advance.larc_ss_confirm"
                        and "event_choice" in transition_view.expected_next_states)
                    # The SS preview can linger after its confirmed input.
                    # This read-only handoff is not completion of its delayed
                    # event transition and cannot authorize another SS input.
                    if not keep_ss_event_owner:
                        optional_skip_observation=(transition_view.last_action_id=='safe_advance.bottom_skip_toggle'
                            and observed_state in transition_view.expected_next_states)
                        transition_view.observe(
                            observed_state,
                            source_sha256=packet.source_image_sha256,
                            resync=bool(transition_resync or record_global_transition or larc_current_page_handoff
                                or optional_skip_observation),
                        )
                except RouteTransitionError as exc:
                    return {
                        "performed": False,
                        "reason": str(exc),
                        "decision": decision,
                        "frame_safety": packet.to_dict(),
                        "route_transition": transition_view.to_dict(),
                    }
                transition_expected_states = expected_next_states_for_decision(decision)
                if decision.get("action") in {"tap", "wait"} and not transition_expected_states:
                    return {
                        "performed": False,
                        "reason": "transition_contract_missing",
                        "decision": decision,
                        "frame_safety": packet.to_dict(),
                        "route_transition": transition_view.to_dict(),
                    }
        safety_verdict = self.safety_gate.evaluate(packet, decision)
        safety_evidence = {
            "frame_safety": packet.to_dict(),
            "safety_verdict": safety_verdict.to_dict(),
            "reader_timing":reader_timings,
        }
        if all_goals_confirmation is not None:
            safety_evidence["career_all_goals_confirmation"] = all_goals_confirmation
        if goal_animation_pending is not None:
            safety_evidence["career_goal_animation_pending"] = goal_animation_pending
        if goal_completion is not None:
            safety_evidence["career_goal_completion"] = goal_completion
            if goal_completion["status"] == "preview":
                return {"performed": False,
                        "reason": "career_goal_completion_preview" if safety_verdict.allowed else "action_safety_gate",
                        "decision": decision, **safety_evidence}
        if goal_summary_exception is not None:
            safety_evidence["goal_summary_exception"] = goal_summary_exception
        if special_page_wait is not None:
            safety_evidence["special_page_wait"] = special_page_wait
        if route_policy_evidence is not None:
            safety_evidence["route_policy"] = route_policy_evidence
            if larc_read_only_wait:
                safety_evidence["route_policy"]["read_only_wait_keeps_previous_owner"] = True
            if larc_current_page_handoff:
                safety_evidence["route_policy"]["current_page_handoff_not_history_gate"] = True
        if transition_view is not None:
            transition_evidence = (
                transition_session
                if staged_live_tap and transition_session is not None
                else transition_view
            )
            safety_evidence["route_transition"] = transition_evidence.to_dict()
        if career_start_destination is not None:
            safety_evidence["raw_frame_safety"] = raw_packet.to_dict()
            safety_evidence["career_start_destination"] = (
                career_start_destination.to_dict()
            )
        if home_training_navigation_scoped:
            safety_evidence["raw_frame_safety"] = raw_packet.to_dict()
            safety_evidence["home_training_navigation_scope"] = {
                "action_id": "safe_advance.home_training",
                "ignored_fixed_navigation_terms": sorted(
                    set(raw_packet.resource_terms)
                ),
                "same_frame_sha256": packet.source_image_sha256,
                "global_home_geometry_verified": True,
                "training_button_geometry_verified": True,
            }
        if not safety_verdict.allowed:
            return {
                "performed": False,
                "reason": "action_safety_gate",
                "decision": decision,
                **safety_evidence,
            }
        ss_evidence = decision.get("evidence", {}).get("larc_ss", {})
        if larc_member_roster is not None and "participants_observation" in ss_evidence:
            try:
                source = {
                    "run_session_id": larc_member_roster.run_session_id,
                    "turn_index": larc_observed_turn,
                    "absolute_path": str(screenshot_path.resolve()),
                    "source_sha256": packet.source_image_sha256,
                }
                persist = mode == "live" and execute and confirm_token == ADVANCE_CONFIRM_TOKEN
                roster_view = larc_member_roster if persist else copy.deepcopy(larc_member_roster)
                ss_evidence["participants_observation"] = roster_view.observe_ss_preview(
                    cv2.imread(str(screenshot_path)), ss_evidence["participants_observation"], source=source,
                )
                if persist:
                    roster_view.save(larc_member_roster_path)
            except (ValueError, OSError) as exc:
                return {"performed": False, "reason": f"larc_ss_observation:{exc}",
                        "decision": decision, **safety_evidence}
        if (
            mode == "live"
            and decision.get("reason") == "skip_startup_promo"
            and not allow_startup_promo_skip
        ):
            return {
                "performed": False,
                "reason": "startup_promo_skip_requires_handoff",
                "decision": decision,
                **safety_evidence,
            }
        if goal_summary_exception is not None and (
            decision.get("action") != "tap"
            or (
                decision_action_id,
                goal_summary_exception["actual_reason"],
            )
            not in _UNBOUND_GOAL_SUMMARY_ACTION_PAIRS
            or (
                decision_action_id,
                goal_summary_exception["actual_reason"],
            )
            != (
                expected_action_id,
                goal_summary_exception["expected_reason"],
            )
        ):
            return {
                "performed": False,
                "reason": "unbound_goal_summary_action_pair_mismatch",
                "expected_action_id": expected_action_id,
                "actual_action_id": decision_action_id,
                "decision": decision,
                **safety_evidence,
            }
        if expected_action_id is not None:
            tap = decision.get("tap")
            actual_action_id = (
                tap.get("coordinate_name") if isinstance(tap, dict) else None
            )
            if decision.get("action") != "tap" or actual_action_id != expected_action_id:
                return {
                    "performed": False,
                    "reason": "expected_action_mismatch",
                    "expected_action_id": expected_action_id,
                    "actual_action_id": actual_action_id,
                    "decision": decision,
                    **safety_evidence,
                }
        if expected_career_end_skill_points is not None:
            actual_skill_points = _career_end_skill_points_from_visible_text(
                packet.visible_text
            )
            if actual_skill_points is None:
                return {
                    "performed": False,
                    "reason": "career_end_skill_points_unverified",
                    "decision": decision,
                    **safety_evidence,
                }
            safety_evidence["external_terminal_skill_points"] = {
                "expected": expected_career_end_skill_points,
                "actual": actual_skill_points,
                "source_frame_sha256": packet.source_image_sha256,
            }
            if actual_skill_points != expected_career_end_skill_points:
                return {
                    "performed": False,
                    "reason": "career_end_skill_points_mismatch",
                    "decision": decision,
                    **safety_evidence,
                }
        if expected_unused_skill_points is not None:
            actual_unused_points = _unused_skill_points_from_visible_text(
                packet.visible_text
            )
            if actual_unused_points is None:
                return {
                    "performed": False,
                    "reason": "finish_training_unused_points_unverified",
                    "decision": decision,
                    **safety_evidence,
                }
            safety_evidence["finish_training_unused_skill_points"] = {
                "expected": expected_unused_skill_points,
                "actual": actual_unused_points,
                "source_frame_sha256": packet.source_image_sha256,
            }
            if actual_unused_points != expected_unused_skill_points:
                return {
                    "performed": False,
                    "reason": "finish_training_unused_points_mismatch",
                    "decision": decision,
                    **safety_evidence,
                }
        _ctx.packet = packet
        _ctx.decision = decision
        _ctx.capture_handoff = capture_handoff
        _ctx.loading_contract_amendment = loading_contract_amendment
        _ctx.global_route_interrupt = global_route_interrupt
        _ctx.larc_read_only_wait = larc_read_only_wait
        _ctx.transition_view = transition_view
        _ctx.transition_expected_states = transition_expected_states
        _ctx.record_global_transition = record_global_transition
        _ctx.staged_live_tap = staged_live_tap
        _ctx.safety_verdict = safety_verdict
        _ctx.safety_evidence = safety_evidence
        return None

    def _run_stage_execute_and_post_freeze(self, _ctx: SimpleNamespace) -> dict[str, Any]:
        mode = _ctx.mode
        execute = _ctx.execute
        confirm_token = _ctx.confirm_token
        scenario = _ctx.scenario
        screenshot_path = _ctx.screenshot_path
        post_tap_screenshot_path = _ctx.post_tap_screenshot_path
        wait_seconds = _ctx.wait_seconds
        settle_seconds = _ctx.settle_seconds
        post_wait_freezes = _ctx.post_wait_freezes
        post_wait_freeze_poll_seconds = _ctx.post_wait_freeze_poll_seconds
        post_wait_freeze_timeout_seconds = _ctx.post_wait_freeze_timeout_seconds
        post_wait_freeze_stable_frames = _ctx.post_wait_freeze_stable_frames
        post_wait_freeze_max_frame_diff = _ctx.post_wait_freeze_max_frame_diff
        larc_adaptation_session = _ctx.larc_adaptation_session
        larc_member_roster = _ctx.larc_member_roster
        larc_member_roster_path = _ctx.larc_member_roster_path
        larc_observed_turn = _ctx.larc_observed_turn
        skill_handoff_authorization = _ctx.skill_handoff_authorization
        skill_handoff_session = _ctx.skill_handoff_session
        skip_session = _ctx.skip_session
        transition_session = _ctx.transition_session
        loading_wait_resume_report = _ctx.loading_wait_resume_report
        loading_wait_resume_report_sha256 = _ctx.loading_wait_resume_report_sha256
        loading_wait_resume_session_path = _ctx.loading_wait_resume_session_path
        capture_observation_report_sha256 = _ctx.capture_observation_report_sha256
        capture_observation_session_path = _ctx.capture_observation_session_path
        control_driver = _ctx.control_driver
        allow_unbound_goal_summary_continue = _ctx.allow_unbound_goal_summary_continue
        adb = _ctx.adb
        packet = _ctx.packet
        decision = _ctx.decision
        resume_requested = _ctx.resume_requested
        capture_handoff = _ctx.capture_handoff
        loading_contract_amendment = _ctx.loading_contract_amendment
        global_route_interrupt = _ctx.global_route_interrupt
        larc_read_only_wait = _ctx.larc_read_only_wait
        transition_view = _ctx.transition_view
        transition_expected_states = _ctx.transition_expected_states
        record_global_transition = _ctx.record_global_transition
        staged_live_tap = _ctx.staged_live_tap
        safety_verdict = _ctx.safety_verdict
        safety_evidence = _ctx.safety_evidence
        if decision.get("action") == "wait":
            if mode == "dry-run":
                return {
                    "performed": False,
                    "reason": "dry_run",
                    "decision": decision,
                    **safety_evidence,
                }
            if transition_view is not None and (
                not global_route_interrupt or record_global_transition
            ) and not larc_read_only_wait:
                transition_action_id = f"wait:{decision.get('reason', 'wait')}"
                if (
                    decision.get("reason") == "training_action_animation_wait"
                    and (transition_view.last_action_id
                        in {"training.execute", "safe_advance.rest_confirm", "safe_advance.rest_outing_confirm"}
                        or (scenario == "larc" and transition_view.last_action_id in {
                            "safe_advance.larc_ss_confirm", "wait:bottom_skip_double_enabled"}))
                ):
                    transition_action_id = transition_view.last_action_id
                if (scenario == "larc" and decision.get("reason") in {"loading", "training_action_animation_wait"}
                        and transition_view.last_action_id in {
                            "safe_advance.larc_ss_confirm", "wait:larc_ss_animation"}):
                    # The owner is still SS, but this is a wait, not another
                    # confirmation input. Identical loading frames are normal.
                    transition_action_id = "wait:larc_ss_animation"
                try:
                    transition_view.commit_action(
                        transition_action_id,
                        expected_next_states=transition_expected_states,
                        source_sha256=packet.source_image_sha256,
                    )
                except RouteTransitionError as exc:
                    return {
                        "performed": False,
                        "reason": f"route_transition_commit:{exc}",
                        "decision": decision,
                        **safety_evidence,
                    }
                safety_evidence["route_transition"] = transition_view.to_dict()
            if scenario == "larc" and getattr(adb, "supports_in_memory_capture", False) is True:
                safety_evidence["home_readiness_wait"] = self._wait_for_career_home(adb, wait_seconds)
            else:
                self.sleep_fn(max(0.0, float(wait_seconds)))
            return {
                "performed": True,
                "reason": str(decision.get("reason", "wait")),
                "decision": decision,
                **safety_evidence,
            }
        if decision.get("action") != "tap":
            result = {
                "performed": False,
                "reason": str(decision.get("reason", "stop")),
                "decision": decision,
                **safety_evidence,
            }
            if decision.get("reason") == "larc_adaptation_verified" and larc_adaptation_session is not None:
                result["larc_adaptation_receipt"] = decision["evidence"]["larc_adaptation"]["receipt"]
                if mode == "live" and execute:
                    larc_adaptation_session.save()
            if decision.get("reason") == "skill_acquisition_handoff":
                result["skill_handoff"] = {
                    "schema_version": "uma.skill_acquisition_handoff.v1",
                    "page_state": "skill_acquisition",
                    "source_image_path": str(screenshot_path),
                    "source_image_sha256": packet.source_image_sha256,
                    "frame_safety": packet.to_dict(),
                    "purchase_executed": False,
                }
            return result
        if mode == "dry-run":
            return {
                "performed": False,
                "reason": "dry_run",
                "decision": decision,
                **safety_evidence,
            }
        if not execute:
            return {
                "performed": False,
                "reason": "execute",
                "decision": decision,
                **safety_evidence,
            }
        if confirm_token != ADVANCE_CONFIRM_TOKEN:
            return {
                "performed": False,
                "reason": "confirm_token",
                "decision": decision,
                **safety_evidence,
            }

        if hashlib.sha256(screenshot_path.read_bytes()).hexdigest() != packet.source_image_sha256:
            return {
                "performed": False,
                "reason": "frame_safety_sha_mismatch",
                "decision": decision,
                **safety_evidence,
            }

        if resume_requested and not skill_handoff_session.consume(skill_handoff_authorization):
            return {
                "performed": False,
                "reason": "skill_handoff_resume_already_consumed",
                "decision": decision,
                **safety_evidence,
            }

        tap = decision["tap"]
        is_skip_toggle = (
            decision.get("reason") == "enable_bottom_skip_toggle"
            or tap.get("coordinate_name") == "safe_advance.bottom_skip_toggle"
        )
        if is_skip_toggle and skip_session is None:
            return {
                "performed": False,
                "reason": "skip_session_required_for_toggle",
                "decision": decision,
                **safety_evidence,
            }
        if skip_session is not None and is_skip_toggle:
            observed_level = str(skip_session.last_observed_level or "absent")
            if not skip_session.may_toggle(observed_level):
                return {
                    "performed": False,
                    "reason": "skip_session_toggle_not_allowed",
                    "decision": decision,
                    "skip_session": skip_session.to_dict(),
                    **safety_evidence,
                }
        if transition_view is not None and (
            not global_route_interrupt or record_global_transition
        ):
            try:
                transition_view.commit_action(
                    str(tap.get("coordinate_name") or decision.get("reason") or "tap"),
                    expected_next_states=transition_expected_states,
                    source_sha256=packet.source_image_sha256,
                )
            except RouteTransitionError as exc:
                return {
                    "performed": False,
                    "reason": f"route_transition_commit:{exc}",
                    "decision": decision,
                    **safety_evidence,
                }
            if not staged_live_tap:
                safety_evidence["route_transition"] = transition_view.to_dict()
        if control_driver is not None:
            control_driver.reach("before_tap")
        if capture_handoff is not None:
            try:
                if (decision.get("reason") != "return_main_menu_from_training"
                    or tap.get("coordinate_name") != "safe_advance.training_menu_back"
                    or RouteTransitionSession.load(capture_observation_session_path,
                        expected_run_session_id=transition_session.run_session_id).to_dict() != capture_handoff["old_session"]
                    or any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha
                        for path, sha in capture_handoff["source_files"].items())):
                    raise RouteTransitionError("capture_handoff_preflight_changed")
                attempt = Path(capture_observation_session_path).parent / (
                    f"{transition_session.run_session_id}.capture_observation_{capture_observation_report_sha256}.attempt.json")
                with attempt.open("x", encoding="utf-8") as stream:
                    json.dump({"kind": "capture_observation_return_attempt", "handoff": capture_handoff,
                        "decision": decision, "safety_verdict": safety_verdict.to_dict()}, stream, ensure_ascii=False)
                capture_handoff["attempt_path"] = str(attempt)
                safety_evidence["capture_observation_attempt"] = str(attempt)
            except (OSError, RouteTransitionError) as exc:
                return {"performed": False, "reason": f"capture_observation_handoff:{exc}",
                        "decision": decision, **safety_evidence}
        if loading_contract_amendment is not None:
            try:
                latest = RouteTransitionSession.load(loading_wait_resume_session_path,
                    expected_run_session_id=transition_session.run_session_id)
                if (latest.to_dict() != transition_session.to_dict()
                    or hashlib.sha256(Path(loading_wait_resume_report).read_bytes()).hexdigest() != loading_wait_resume_report_sha256):
                    raise RouteTransitionError("loading_wait_resume_source_changed")
            except (OSError, RouteTransitionError) as exc:
                return {"performed": False, "reason": str(exc), "decision": decision, **safety_evidence}
        adaptation_tap = str(tap.get("coordinate_name", "")).startswith("safe_advance.larc_adaptation_")
        if adaptation_tap:
            result_close = (
                decision.get("reason") == "larc_adaptation_result_close"
                and tap.get("coordinate_name") == "safe_advance.larc_adaptation_result_close"
            )
            # Closing the current result notice spends no points and does not
            # need a historical purchase. Selection/submission still has an owner.
            if larc_adaptation_session is None and not result_close:
                return {"performed": False, "reason": "larc_adaptation_session_required", "decision": decision,
                        **safety_evidence}
            if tap["coordinate_name"] == "safe_advance.larc_adaptation_upgrade":
                # Save the attempt before the point-spend tap; an unknown outcome must not repeat it.
                larc_adaptation_session.stage = "submission_pending"
                larc_adaptation_session.submitted_source_sha256 = packet.source_image_sha256
            if larc_adaptation_session is not None:
                larc_adaptation_session.save()
        try:
            from core.execution_frames import tap_without_settle
            tap_succeeded = tap_without_settle(adb,
                tap["x"], tap["y"], scaled=bool(tap.get("scaled", True)),
            )
        except Exception:
            if loading_contract_amendment is None and capture_handoff is None:
                raise
            return {"performed": False, "reason": "adb_tap_outcome_unknown",
                "decision": decision, **safety_evidence}
        if tap_succeeded is not True:
            return {
                "performed": False,
                "reason": "adb_tap_failed",
                "decision": decision,
                **safety_evidence,
            }
        if adaptation_tap and tap["coordinate_name"] == "safe_advance.larc_adaptation_upgrade":
            larc_adaptation_session.mark_submitted(packet.source_image_sha256)
            larc_adaptation_session.save()
        if adaptation_tap and tap["coordinate_name"] == "safe_advance.larc_adaptation_return":
            larc_adaptation_session.returned = True
            larc_adaptation_session.save()
        if staged_live_tap and transition_session is not None:
            (
                transition_session.status,
                transition_session.current_state,
                transition_session.last_action_id,
                transition_session.expected_next_states,
                transition_session.observation_sha256,
                transition_session.action_source_sha256,
                transition_session.mismatch_state,
                transition_session.transition_version,
            ) = (
                transition_view.status,
                transition_view.current_state,
                transition_view.last_action_id,
                transition_view.expected_next_states,
                transition_view.observation_sha256,
                transition_view.action_source_sha256,
                transition_view.mismatch_state,
                transition_view.transition_version,
            )
            safety_evidence["route_transition"] = transition_session.to_dict()
            if loading_contract_amendment is not None:
                safety_evidence["contract_amendment"] = loading_contract_amendment
            if capture_handoff is not None:
                safety_evidence["capture_observation_handoff"] = capture_handoff

        if larc_member_roster is not None and tap.get("coordinate_name") == "safe_advance.larc_ss_confirm":
            receipt = {
                "run_session_id": larc_member_roster.run_session_id,
                "turn_index": larc_observed_turn, "action_id": tap["coordinate_name"],
                "tap_performed": True, "source_sha256": packet.source_image_sha256,
            }
            try:
                larc_member_roster.record_ss_execution(receipt)
                larc_member_roster.save(larc_member_roster_path)
                safety_evidence["larc_ss_execution_receipt"] = {"status": "persisted", "receipt": receipt}
            except (ValueError, OSError) as exc:
                return {"performed": False, "tap_performed": True,
                        "reason": "larc_ss_receipt_persistence_failed", "decision": decision,
                        "larc_ss_execution_receipt": {"status": "persistence_failed", "receipt": receipt,
                                                       "error": str(exc)}, **safety_evidence}

        control_frame_completed = False

        def complete_control_frame(*, capture: bool) -> None:
            nonlocal control_frame_completed
            if control_driver is None or control_frame_completed:
                return
            if capture:
                adb.screenshot(str(post_tap_screenshot_path))
            post_bytes = post_tap_screenshot_path.read_bytes()
            if not post_bytes:
                raise RuntimeError("safe_advance_post_frame_empty")
            control_driver.after_fresh_frame(FreshFrameEvidence(
                path=str(post_tap_screenshot_path),
                sha256=hashlib.sha256(post_bytes).hexdigest(),
                captured_at=time.time(),
            ))
            control_frame_completed = True

        def post_tap_frame_evidence() -> dict[str, str] | None:
            try:
                post_bytes = post_tap_screenshot_path.read_bytes()
            except OSError:
                return None
            if not post_bytes:
                return None
            return {
                "path": str(post_tap_screenshot_path),
                "sha256": hashlib.sha256(post_bytes).hexdigest(),
            }

        if skip_session is not None and is_skip_toggle:
            try:
                skip_session.mark_toggle_performed(
                    source_sha256=packet.source_image_sha256,
                )
            except SkipSessionError as exc:
                complete_control_frame(capture=True)
                return {
                    "performed": False,
                    "tap_performed": True,
                    "reason": f"skip_session_toggle_record:{exc}",
                    "decision": decision,
                    "skip_session": skip_session.to_dict(),
                    **safety_evidence,
                }
        sleep_seconds = self._settle_seconds_for_decision(decision, settle_seconds)
        adaptation_ready = None
        if adaptation_tap and getattr(adb, "supports_in_memory_capture", False) is True:
            from core.auto_trainer.larc_control_wait import adaptation_controls, controls_changed
            before_adaptation = cv2.imread(str(screenshot_path))
            adaptation_ready = lambda image: controls_changed(before_adaptation, image) and adaptation_controls(
                image, allow_home=tap["coordinate_name"] == "safe_advance.larc_adaptation_return")
            sleep_seconds = 0
        elif (scenario == "larc" and decision.get("reason") == "larc_ss_open"
                and getattr(adb, "supports_in_memory_capture", False) is True):
            from core.auto_trainer.larc_control_wait import ss_preview_controls
            adaptation_ready = ss_preview_controls
            sleep_seconds = 0
        elif (scenario=="larc" and decision.get("reason")=="skip_larc_opening_movie"
                and getattr(adb,"supports_in_memory_capture",False) is True):
            # One movie skip, followed only by fresh-frame observation.
            adaptation_ready=lambda image: SafeAdvancePlanner._career_opening_movie_skip_button(image) is None
            sleep_seconds=0
        elif (scenario == "larc" and skip_session is not None and is_skip_toggle
                and getattr(adb, "supports_in_memory_capture", False) is True):
            # Tap feedback/animation can temporarily hide the arrows. Reuse
            # the bounded control wait, accepting only observed advancement;
            # a stable absent frame or the old single level is not confirmation.
            advanced_levels=("single","double") if skip_session.operation_previous_level=="off" else ("double",)
            adaptation_ready=lambda image: SpeedTogglePlanner._bottom_skip_level(image) in advanced_levels
            sleep_seconds = 0
        elif (scenario == "larc" and decision.get("reason") in {"select_shorten_all_events","uncheck_do_not_shorten_unread"}
                and getattr(adb,"supports_in_memory_capture",False) is True):
            before_settings=SafeAdvancePlanner._shortened_event_settings_action(cv2.imread(str(screenshot_path)))
            def settings_ready(image):
                after=SafeAdvancePlanner._shortened_event_settings_action(image)
                return after is not None and after[0]!=before_settings[0]
            adaptation_ready=settings_ready
            sleep_seconds=0
        if sleep_seconds > 0:
            self.sleep_fn(sleep_seconds)
        try:
            post_wait_freeze = self._wait_for_post_tap_freeze(
                adb,
                post_tap_screenshot_path,
                enabled=post_wait_freezes,
                poll_seconds=post_wait_freeze_poll_seconds,
                timeout_seconds=post_wait_freeze_timeout_seconds,
                required_stable_frames=post_wait_freeze_stable_frames,
                max_frame_diff=post_wait_freeze_max_frame_diff,
                stop_on_home=scenario == "larc" and getattr(adb, "supports_in_memory_capture", False) is True,
                semantic_pages=scenario == 'larc',
                **({"ready_predicate": adaptation_ready} if adaptation_ready is not None else {}),
            )
        except Exception as exc:
            navigation=(tap.get('coordinate_name'),decision.get('reason')) in _OPTIONAL_NAVIGATION_PAIRS
            if not navigation and not allow_unbound_goal_summary_continue and "larc_ss_execution_receipt" not in safety_evidence:
                raise
            if skip_session is not None and is_skip_toggle:skip_session.defer('post_wait_capture_unavailable')
            return {
                "performed": False,
                "tap_performed": True,
                "reason": f"post_wait_freeze_capture:{type(exc).__name__}",
                "decision": decision,
                "post_wait_freeze": {"status": "capture_error"},
                **safety_evidence,
            }
        complete_control_frame(capture=not post_wait_freezes)
        if post_wait_freeze["status"] not in {"stable", "disabled"}:
            if skip_session is not None and is_skip_toggle:skip_session.defer('post_wait_'+post_wait_freeze['status'])
            result = {
                "performed": False,
                "tap_performed": True,
                "reason": f"post_wait_freeze_{post_wait_freeze['status']}",
                "decision": decision,
                "post_wait_freeze": post_wait_freeze,
                **safety_evidence,
            }
            post_frame = post_tap_frame_evidence()
            if post_frame is not None:
                result["post_tap_frame"] = post_frame
            return result
        if skip_session is not None and is_skip_toggle:
            try:
                post_tap_bytes = post_tap_screenshot_path.read_bytes()
                post_tap_sha256 = hashlib.sha256(post_tap_bytes).hexdigest()
                post_tap_img = cv2.imread(str(post_tap_screenshot_path))
                if post_tap_img is None:
                    raise SkipSessionError("post_tap_image_read")
                post_tap_level = SpeedTogglePlanner._bottom_skip_level(post_tap_img)
                skip_session.observe(
                    post_tap_level,
                    source_sha256=post_tap_sha256,
                )
            except (OSError, SkipSessionError) as exc:
                skip_session.defer('post_tap_level_unreadable')
                return {
                    "performed": False,
                    "tap_performed": True,
                    "reason": "optional_skip_unconfirmed",
                    "decision": decision,
                    "post_wait_freeze": post_wait_freeze,
                    "skip_session": skip_session.to_dict(),
                    **safety_evidence,
                }
            if skip_session.state == SkipSessionState.ENABLED:
                reason = "enable_bottom_skip_toggle"
            elif skip_session.can_continue_operation:
                reason = "skip_session_progressed_pending"
            else:
                skip_session.defer('post_tap_level_unconfirmed')
                return {
                    "performed": False,
                    "tap_performed": True,
                    "reason": "optional_skip_unconfirmed",
                    "decision": decision,
                    "post_wait_freeze": post_wait_freeze,
                    "skip_session": skip_session.to_dict(),
                    **safety_evidence,
                }
        post_tap_confirmation = decision.get("post_tap_confirmation")
        if post_tap_confirmation == "career_complete_global_home":
            adb.screenshot(str(post_tap_screenshot_path))
            post_tap_image = cv2.imread(str(post_tap_screenshot_path))
            reason = (
                "career_complete"
                if SafeAdvancePlanner.is_post_career_global_home(post_tap_image)
                else "terminal_confirmation_unknown"
            )
        elif not (skip_session is not None and is_skip_toggle):
            reason = str(decision["reason"])
        result = {
            "performed": True,
            "tap_performed": True,
            "reason": reason,
            "decision": decision,
            "post_wait_freeze": post_wait_freeze,
            **safety_evidence,
        }
        if skip_session is not None:
            result["skip_session"] = skip_session.to_dict()
        post_frame = post_tap_frame_evidence()
        if post_frame is not None:
            result["post_tap_frame"] = post_frame
        return result

    def _wait_for_post_tap_freeze(
        self,
        adb: Any,
        screenshot_path: Path,
        *,
        enabled: bool,
        poll_seconds: float,
        timeout_seconds: float,
        required_stable_frames: int,
        max_frame_diff: float,
        stop_on_home: bool = False,
        ready_predicate: Callable[[np.ndarray], bool] | None = None,
        semantic_pages: bool = False,
    ) -> dict[str, Any]:
        poll_interval = min(.1, float(poll_seconds)) if ready_predicate is not None else float(poll_seconds)
        timeout = float(timeout_seconds)
        required_frames = int(required_stable_frames)
        max_difference = float(max_frame_diff)
        use_memory = getattr(adb, "supports_in_memory_capture", False) is True
        latest_frame: np.ndarray | None = None

        def finish(**kwargs):
            if use_memory and latest_frame is not None:
                adb.save_frame(screenshot_path, latest_frame)
            return self._post_wait_freeze_result(**kwargs)

        if not enabled:
            return finish(
                enabled=False,
                status="disabled",
                poll_count=0,
                stable_frames=0,
                required_stable_frames=required_frames,
                max_frame_diff=max_difference,
                poll_interval_seconds=poll_interval,
                timeout_seconds=timeout,
                elapsed_seconds=0.0,
                normalized_differences=[],
                difference_count=0,
                difference_min=None,
                difference_max=None,
                difference_last=None,
            )

        started_at = self.monotonic_fn()
        deadline = started_at + timeout
        if use_memory and ready_predicate is not None:
            from core.execution_frames import wait_for_frame
            observation = wait_for_frame(adb, ready_predicate, timeout=timeout,
                interval=poll_interval, clock=self.monotonic_fn, sleep=self.sleep_fn)
            latest_frame = observation.frame
            if observation.error is None:
                return finish(enabled=True,
                    status="stable" if observation.value else
                        "frame_unavailable" if observation.reason == "frame_unavailable" else "timeout",
                    readiness_basis="larc_controls" if observation.value else None,
                    poll_count=observation.polls, stable_frames=0,
                    required_stable_frames=required_frames, max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval, timeout_seconds=timeout,
                    elapsed_seconds=observation.seconds, normalized_differences=[],
                    difference_count=0, difference_min=None, difference_max=None, difference_last=None,
                    capture_latencies=observation.capture_latencies)
            # Native capture failed: use the existing file fallback within the
            # original deadline, not a second full wait budget.
            use_memory, latest_frame = False, None
            ready_predicate = None
        normalized_differences: list[float] = []
        difference_count = 0
        difference_min: float | None = None
        difference_max: float | None = None
        difference_last: float | None = None
        poll_count = 0
        stable_frames = 0
        previous_frame: np.ndarray | None = None
        capture_latencies: list[float] = []
        max_poll_count = max(required_frames, math.ceil(timeout / poll_interval) + 1)
        while True:
            capture_started_at = self.monotonic_fn()
            if capture_started_at >= deadline:
                return finish(
                    enabled=True,
                    status="timeout",
                    poll_count=poll_count,
                    stable_frames=stable_frames,
                    required_stable_frames=required_frames,
                    max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval,
                    timeout_seconds=timeout,
                    elapsed_seconds=max(0.0, capture_started_at - started_at),
                    normalized_differences=normalized_differences,
                    difference_count=difference_count,
                    difference_min=difference_min,
                    difference_max=difference_max,
                    difference_last=difference_last,
                    capture_latencies=capture_latencies,
                )
            if poll_count:
                if capture_started_at + max(capture_latencies) >= deadline:
                    return finish(
                        enabled=True,
                        status="insufficient_budget",
                        poll_count=poll_count,
                        stable_frames=stable_frames,
                        required_stable_frames=required_frames,
                        max_frame_diff=max_difference,
                        poll_interval_seconds=poll_interval,
                        timeout_seconds=timeout,
                        elapsed_seconds=max(0.0, capture_started_at - started_at),
                        normalized_differences=normalized_differences,
                        difference_count=difference_count,
                        difference_min=difference_min,
                        difference_max=difference_max,
                        difference_last=difference_last,
                        capture_latencies=capture_latencies,
                    )
            if use_memory:
                try:
                    current_frame = adb.read_frame(deadline=deadline)
                except (OSError, RuntimeError, AttributeError):
                    use_memory = False
                    latest_frame = None
                    self._capture_screenshot(
                        adb, screenshot_path, deadline=deadline,
                        timeout_seconds=deadline - capture_started_at,
                    )
                    current_frame = cv2.imread(str(screenshot_path), cv2.IMREAD_COLOR)
            else:
                self._capture_screenshot(
                    adb, screenshot_path, deadline=deadline,
                    timeout_seconds=deadline - capture_started_at,
                )
                current_frame = cv2.imread(str(screenshot_path), cv2.IMREAD_COLOR)
            poll_count += 1
            if use_memory and current_frame is not None:
                latest_frame = current_frame
            now = self.monotonic_fn()
            capture_latencies.append(max(0.0, now - capture_started_at))
            if current_frame is None:
                return finish(
                    enabled=True,
                    status="timeout" if now >= deadline else "frame_unavailable",
                    poll_count=poll_count,
                    stable_frames=stable_frames,
                    required_stable_frames=required_frames,
                    max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval,
                    timeout_seconds=timeout,
                    elapsed_seconds=max(0.0, now - started_at),
                    normalized_differences=normalized_differences,
                    difference_count=difference_count,
                    difference_min=difference_min,
                    difference_max=difference_max,
                    difference_last=difference_last,
                    capture_latencies=capture_latencies,
                )

            if previous_frame is None:
                stable_frames = 1
            else:
                difference = self._normalized_frame_difference(previous_frame, current_frame)
                difference_count += 1
                difference_min = difference if difference_min is None else min(difference_min, difference)
                difference_max = difference if difference_max is None else max(difference_max, difference)
                difference_last = difference
                if len(normalized_differences) >= POST_WAIT_FREEZE_DIFF_HISTORY_LIMIT:
                    normalized_differences.pop(0)
                normalized_differences.append(difference)
                stable_frames = stable_frames + 1 if difference <= max_difference else 1
            if now >= deadline:
                return finish(
                    enabled=True,
                    status="timeout",
                    poll_count=poll_count,
                    stable_frames=stable_frames,
                    required_stable_frames=required_frames,
                    max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval,
                    timeout_seconds=timeout,
                    elapsed_seconds=max(0.0, now - started_at),
                    normalized_differences=normalized_differences,
                    difference_count=difference_count,
                    difference_min=difference_min,
                    difference_max=difference_max,
                    difference_last=difference_last,
                    capture_latencies=capture_latencies,
                )
            home_ready = stop_on_home and self._career_home_controls_ready(current_frame)
            semantic_ready = self._post_tap_page_ready(current_frame) if semantic_pages and ready_predicate is None else None
            if stable_frames >= required_frames or home_ready or semantic_ready:
                return finish(
                    enabled=True,
                    status="stable",
                    readiness_basis=semantic_ready or ("career_home_controls" if home_ready else "frame_difference"),
                    poll_count=poll_count,
                    stable_frames=stable_frames,
                    required_stable_frames=required_frames,
                    max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval,
                    timeout_seconds=timeout,
                    elapsed_seconds=max(0.0, now - started_at),
                    normalized_differences=normalized_differences,
                    difference_count=difference_count,
                    difference_min=difference_min,
                    difference_max=difference_max,
                    difference_last=difference_last,
                    capture_latencies=capture_latencies,
                )
            remaining_captures = required_frames - stable_frames
            predicted_capture_seconds = max(capture_latencies)
            minimum_completion_seconds = remaining_captures * (poll_interval + predicted_capture_seconds)
            if now + minimum_completion_seconds >= deadline:
                return finish(
                    enabled=True,
                    status="insufficient_budget",
                    poll_count=poll_count,
                    stable_frames=stable_frames,
                    required_stable_frames=required_frames,
                    max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval,
                    timeout_seconds=timeout,
                    elapsed_seconds=max(0.0, now - started_at),
                    normalized_differences=normalized_differences,
                    difference_count=difference_count,
                    difference_min=difference_min,
                    difference_max=difference_max,
                    difference_last=difference_last,
                    capture_latencies=capture_latencies,
                )
            if poll_count >= max_poll_count:
                return finish(
                    enabled=True,
                    status="poll_limit",
                    poll_count=poll_count,
                    stable_frames=stable_frames,
                    required_stable_frames=required_frames,
                    max_frame_diff=max_difference,
                    poll_interval_seconds=poll_interval,
                    timeout_seconds=timeout,
                    elapsed_seconds=max(0.0, now - started_at),
                    normalized_differences=normalized_differences,
                    difference_count=difference_count,
                    difference_min=difference_min,
                    difference_max=difference_max,
                    difference_last=difference_last,
                    capture_latencies=capture_latencies,
                )
            previous_frame = current_frame
            self.sleep_fn(poll_interval)

    @staticmethod
    def _post_tap_page_ready(frame):
        # Identity/control readiness, not character art, sparkles or arrow animation.
        if SafeAdvanceExecutor._career_home_controls_ready(frame):return 'career_home_controls'
        if event_option_rows_ready(frame):return 'event_choice_controls'
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp_enough = float(cv2.Laplacian(gray, cv2.CV_64F).var()) >= 25.0
        if (not SafeAdvancePlanner._looks_like_confirm_modal_panel(frame)
                and SafeAdvancePlanner._looks_like_large_bottom_dialogue(frame)
                and (SafeAdvancePlanner._looks_like_dialogue_continue_arrow(frame)
                    or SafeAdvancePlanner._looks_like_training_story_dialogue(frame)
                    or SafeAdvancePlanner._looks_like_green_nameplate_bottom_dialogue(frame)
                    or (sharp_enough and SafeAdvancePlanner._looks_like_training_animation_controls(frame)))):
            return 'dialogue_controls'
        return None

    @staticmethod
    def _capture_screenshot(
        adb: Any,
        screenshot_path: Path,
        *,
        deadline: float,
        timeout_seconds: float,
    ) -> Any:
        screenshot = adb.screenshot
        try:
            parameters = tuple(inspect.signature(screenshot).parameters.values())
        except (TypeError, ValueError):
            return screenshot(str(screenshot_path))
        accepts_deadline = any(parameter.name == "deadline" for parameter in parameters)
        if accepts_deadline:
            return screenshot(str(screenshot_path), deadline=deadline)
        accepts_timeout = any(
            parameter.name == "timeout" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_timeout:
            return screenshot(str(screenshot_path), timeout=max(0.0, timeout_seconds))
        return screenshot(str(screenshot_path))

    @staticmethod
    def _career_home_controls_ready(image: np.ndarray) -> bool:
        # This only ends polling. The normal planner still checks the next action.
        from core.main_menu_state import CANONICAL_SIZE, SUPPORTED_SIZES, _detect_main_menu, _digit_mask
        from core.auto_trainer.event_options import event_option_rows_ready

        if image is None or image.ndim != 3 or image.shape[1::-1] not in SUPPORTED_SIZES:
            return False
        if image.shape[1::-1] != CANONICAL_SIZE:
            image = cv2.resize(image, CANONICAL_SIZE, interpolation=cv2.INTER_AREA)
        controls_ready = bool(
            _detect_main_menu(image, scenario="larc")["value"] == "main_menu"
            and SafeAdvancePlanner._looks_like_training_home_controls(image)
            and not SafeAdvancePlanner._looks_like_confirm_modal_panel(image)
            and not event_option_rows_ready(image)
        )
        if not controls_ready:
            return False
        # The remaining-turn number briefly enlarges across its normal slot.
        # Wait for that UI animation, without reading the number or comparing
        # attributes/rewards. An unreadable but normally sized slot is not a gate.
        mask = _digit_mask(image[68:119, 22:98], "remaining_blue")
        _, _, components, _ = cv2.connectedComponentsWithStats(mask)
        return not any(
            width >= 15 and height >= 48 and area >= 200
            for _, _, width, height, area in components[1:]
        )

    def _wait_for_career_home(self, adb, wait_seconds: float) -> dict[str, Any]:
        """Observe cheap native frames during an existing wait; never tap or run OCR."""
        duration = max(0.0, float(wait_seconds))
        started = self.monotonic_fn()
        deadline = started + duration
        polls = 0
        ready = False
        # The count also bounds polling with synthetic clocks in offline callers.
        for _ in range(math.ceil(duration / 0.1)):
            if self.monotonic_fn() >= deadline:
                break
            try:
                frame = adb.read_frame(deadline=deadline)
            except (OSError, RuntimeError, AttributeError):
                break
            polls += 1
            if self._career_home_controls_ready(frame):
                ready = True
                break
            remaining = deadline - self.monotonic_fn()
            if remaining > 0:
                self.sleep_fn(min(0.1, remaining))
        if not ready:
            remaining = deadline - self.monotonic_fn()
            if remaining > 0:
                self.sleep_fn(remaining)
        return {"ready": ready, "poll_count": polls,
                "elapsed_seconds": max(0.0, self.monotonic_fn() - started)}

    @staticmethod
    def _normalized_frame_difference(previous_frame: np.ndarray, current_frame: np.ndarray) -> float:
        if previous_frame.shape != current_frame.shape:
            return 1.0
        if (
            previous_frame.ndim == 3
            and current_frame.ndim == 3
            and SafeAdvancePlanner._looks_like_training_top_hud(previous_frame)
            and SafeAdvancePlanner._looks_like_training_top_hud(current_frame)
        ):
            width, height = POST_WAIT_TRAINING_HUD_REFERENCE_SIZE
            previous_normalized = cv2.resize(
                previous_frame,
                (width, height),
                interpolation=cv2.INTER_AREA,
            )
            current_normalized = cv2.resize(
                current_frame,
                (width, height),
                interpolation=cv2.INTER_AREA,
            )
            difference_sum = 0.0
            value_count = 0
            for x1, y1, x2, y2 in POST_WAIT_TRAINING_HUD_STABILITY_ROIS:
                previous_roi = previous_normalized[y1:y2, x1:x2]
                current_roi = current_normalized[y1:y2, x1:x2]
                difference_sum += float(
                    np.abs(
                        previous_roi.astype(np.int16)
                        - current_roi.astype(np.int16)
                    ).sum()
                )
                value_count += int(previous_roi.size)
            if value_count:
                return difference_sum / float(value_count * 255)
        return float(
            np.mean(np.abs(previous_frame.astype(np.int16) - current_frame.astype(np.int16))) / 255.0
        )

    @staticmethod
    def _post_wait_freeze_result(
        *,
        enabled: bool,
        status: str,
        poll_count: int,
        stable_frames: int,
        required_stable_frames: int,
        max_frame_diff: float,
        poll_interval_seconds: float,
        timeout_seconds: float,
        elapsed_seconds: float,
        normalized_differences: list[float],
        difference_count: int,
        difference_min: float | None,
        difference_max: float | None,
        difference_last: float | None,
        capture_latencies: list[float] | None = None,
        readiness_basis: str | None = None,
    ) -> dict[str, Any]:
        latency_values = list(capture_latencies or [])
        wall_clock_elapsed_seconds = elapsed_seconds
        return {
            "enabled": enabled,
            "status": status,
            **({"readiness_basis": readiness_basis} if readiness_basis else {}),
            "poll_count": poll_count,
            "stable_frames": stable_frames,
            "required_stable_frames": required_stable_frames,
            "max_frame_diff": max_frame_diff,
            "poll_interval_seconds": poll_interval_seconds,
            "timeout_seconds": timeout_seconds,
            "elapsed_seconds": wall_clock_elapsed_seconds,
            "wall_clock_elapsed_seconds": wall_clock_elapsed_seconds,
            "capture_latency_seconds": latency_values,
            "capture_latency_summary": {
                "count": len(latency_values),
                "last": latency_values[-1] if latency_values else None,
                "min": min(latency_values) if latency_values else None,
                "max": max(latency_values) if latency_values else None,
                "total": sum(latency_values),
            },
            "differences": list(normalized_differences),
            "differences_truncated": difference_count > len(normalized_differences),
            "difference_summary": {
                "count": difference_count,
                "last": difference_last,
                "min": difference_min,
                "max": difference_max,
            },
        }

    @staticmethod
    def validate_post_wait_freeze_options(
        *,
        poll_seconds: float,
        timeout_seconds: float,
        stable_frames: int,
        max_frame_diff: float,
    ) -> None:
        for name, value in (
            ("post_wait_freeze_poll_seconds", poll_seconds),
            ("post_wait_freeze_timeout_seconds", timeout_seconds),
            ("post_wait_freeze_max_frame_diff", max_frame_diff),
        ):
            try:
                numeric_value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name}_must_be_finite") from exc
            if not math.isfinite(numeric_value):
                raise ValueError(f"{name}_must_be_finite")

        if float(poll_seconds) <= 0:
            raise ValueError("post_wait_freeze_poll_seconds_must_be_positive")
        if float(timeout_seconds) <= 0:
            raise ValueError("post_wait_freeze_timeout_seconds_must_be_positive")
        if not 0.0 <= float(max_frame_diff) <= 1.0:
            raise ValueError("post_wait_freeze_max_frame_diff_must_be_between_0_and_1")
        if isinstance(stable_frames, bool) or not isinstance(stable_frames, int) or stable_frames < 2:
            raise ValueError("post_wait_freeze_stable_frames_must_be_integer_at_least_2")

    @staticmethod
    def _team_formation_confirm_decision(
        decision: dict[str, Any],
        screenshot_path: Path,
    ) -> dict[str, Any]:
        reason = str(decision.get("reason", ""))
        route_state = str(decision.get("route_state", ""))
        if decision.get("action") != "stop":
            return decision
        if reason not in {"team_formation", "team_formation:confirm_not_allowed"} and route_state != "team_formation":
            return decision
        img = cv2.imread(str(screenshot_path))
        if img is None:
            return decision
        height, width = img.shape[:2]
        x, y = CoordinateScaler(
            reference_size=(720, 1280),
            current_size=(width, height),
        ).scale_point(360, 1077)
        converted = dict(decision)
        converted["action"] = "tap"
        converted["reason"] = "team_formation_confirm"
        converted["route_state"] = "team_formation"
        converted["tap"] = {
            "coordinate_name": "safe_advance.team_formation_confirm",
            "x": int(x),
            "y": int(y),
            "scaled": False,
        }
        return converted

    @staticmethod
    def _settle_seconds_for_decision(decision: dict[str, Any], settle_seconds: float) -> float:
        base = max(0.0, float(settle_seconds))
        if str(decision.get("reason", "")) in LONG_SETTLE_REASONS:
            return max(base, START_TRAINING_SETTLE_SECONDS)
        return base

    @staticmethod
    def _default_adb_factory() -> Any:
        from core.adb import ADB

        return ADB()
