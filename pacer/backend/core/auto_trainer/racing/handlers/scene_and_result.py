"""Race scene skip, result continuation, and core decision builders."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from core.frame_io import read_image_with_retry
from core.auto_trainer.action_table import ActionTable
from core.auto_trainer.career_goal_perception import is_goal_achievement_title
from core.auto_trainer.larc_pages import (
    LARC_SYSTEM_GOAL_IDS,
    annotate_larc_race_page,
    larc_race_transition_kind,
    plan_larc_explanation,
)
from core.auto_trainer.frame_safety import FormalActionSafetyGate
from core.auto_trainer.runtime_control import reobserving_step, controlled_sleep
from core.auto_trainer.race_clock_retry import _ordinary_result_blocked, retry_prompt_visible
from core.auto_trainer.route_handoff import handoff, is_route_handoff
from .._compat import (
    FrameSafetyAnalyzer,
    LocalRapidOCRScreenTextReader,
    Perception,
    RaceRouteExecutor,
    RaceRoutePlanner,
    SafeAdvancePlanner,
    TrainingMenuPrechecker,
    _current_clock_retry,
    analyze_screen_text,
    classify_landscape_race_screen,
    fuse_perception_with_text,
    match_main_menu_named_goal,
    rr_attr,
)
from ..models import (
    RACE_ENTRY_POST_OBSERVATION_RETRIES,
    RACE_LIST_GRADE_REOBSERVATION_RETRIES,
    RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS,
    RACE_ROUTE_CONFIRM_TOKEN,
    RACE_SCENE_WAIT_REASONS,
    RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS,
    RESULT_TRANSITION_RECAPTURE_LIMIT,
    SUPPORTED_RACE_ROUTE_SCREEN_SIZES,
    RaceRoutePolicy,
    RaceRouteSession,
    _RACE_ACCOUNT_CONFLICT_TEXT,
    is_race_scene_wait_result,
    larc_exchange_entry_released,
)


class SceneAndResultHandlerMixin:
    """Race scene skip/advance, result continuation, and tap/stop builders."""

    @staticmethod
    def _tap(
        reason: str,
        route_state: str,
        coordinate_name: str,
        point: tuple[int, int],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "action": "tap",
            "reason": reason,
            "route_state": route_state,
            "tap": {
                "coordinate_name": coordinate_name,
                "x": int(point[0]),
                "y": int(point[1]),
                "scaled": False,
            },
            "evidence": evidence,
        }

    def _configured_decision(
        self,
        state: str,
        img: np.ndarray,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.action_table.decision("race_route", state, img, evidence=evidence)

    @staticmethod
    def _inspection_context_error(policy: RaceRoutePolicy, session: RaceRouteSession | None) -> str | None:
        if policy.wbc_only or any(getattr(policy, name) for name in policy.__dataclass_fields__ if name.startswith("allow_")):
            return "race_inspection:unrelated_capability"
        if (session is None or session.inspection_phase not in {"new", "opened", "confirmed", "returned"}
            or session.planned_race_ids or session.schedule_packet_sha256 is not None
            or session.scheduled_entry_consumed or session.wbc_entry_confirmed
            or any(getattr(session, name) is not None for name in (
                "pending_target_race", "pending_locked_target_race", "pending_scenario_final_race",
                "pending_fan_goal_recommendation", "pending_fan_goal_shortfall_selected",
                "pending_wbc_hub_race", "pending_gur_race"))):
            return "race_inspection:session_conflict"
        return None

    @staticmethod
    def _bind_observed_free_control(evidence, action_id):
        evidence["observed_free_race_control"] = {
            "action_id": action_id,
            "source_image_sha256": hashlib.sha256(Path(evidence["image"]).read_bytes()).hexdigest(),
        }

    def _verified_result_notice(self, policy, observed, decision, evidence):
        if policy.allow_result_continue:
            return decision
        if observed:
            decision.setdefault("route_state", "race_result")
            self._bind_observed_free_control(evidence, decision["tap"]["coordinate_name"])
            return decision
        if policy._confirmed_live_ordinary_route:
            return decision
        return self._stop("race_result:continue_not_allowed", evidence=evidence)

    def _portrait_scene_skip(self, policy, observed, img, explicit_point, evidence):
        if policy.allow_skip_race_scene:
            return self._skip_observed_scene(policy, explicit_point, evidence)
        if observed and img.shape[:2] in {(1280, 720), (2560, 1440)}:
            height, width = img.shape[:2]
            canonical = img if width == 720 else cv2.resize(img, (720, 1280), interpolation=cv2.INTER_AREA)
            controls = self._count_portrait_watch_controls(canonical, None, require_camera_label=False)
            point = controls.get("skip_point") if controls else None
            if point is not None:
                return self._skip_observed_scene(policy,
                    (round(point[0]*width/720), round(point[1]*height/1280)), evidence)
        return self._stop("race_scene:skip_not_allowed", evidence=evidence)

    def _continue_observed_result(self, policy, observed, point, evidence):
        if not policy.allow_result_continue and (not observed or point is None):
            return self._stop("race_result:continue_not_allowed", evidence=evidence)
        if not policy.allow_result_continue:
            self._bind_observed_free_control(evidence, "race_result.continue")
        return self._tap("race_result_continue", "race_result", "race_result.continue", tuple(point), evidence)

    def _skip_observed_scene(self, policy, point, evidence):
        if not policy.allow_skip_race_scene:
            self._bind_observed_free_control(evidence, "race_scene.skip_or_advance")
        return self._tap("skip_or_advance_race_scene", "race_scene", "race_scene.skip_or_advance",
                         tuple(point), evidence)

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
