"""RaceRoute constants, policy, session state, and helper predicates."""
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


RACE_ROUTE_CONFIRM_TOKEN = "UG_PARENT_RACE_ROUTE_SANDBOX"
RACE_SCENE_WAIT_REASONS = frozenset({"race_scene_running", "landscape_race_skip_enabled"})
RESULT_TRANSITION_RECAPTURE_LIMIT = 3
RESULT_TRANSITION_RECAPTURE_DELAY_SECONDS = 0.35
RACE_ENTRY_POST_OBSERVATION_RETRIES = 2
RACE_LIST_GRADE_REOBSERVATION_RETRIES = 2
RACE_READ_ONLY_REOBSERVATION_DELAY_SECONDS = 0.2
_RACE_ACCOUNT_CONFLICT_TEXT = ('其他设备登录','别处登录')
SUPPORTED_RACE_ROUTE_SCREEN_SIZES = {
    (720, 1280),
    (1440, 2560),
    (1280, 720),
    (2560, 1440),
}


def _current_clock_retry(image, text, *, allow_stock_reobserve=False):
    """Re-read only a known full-page stock field, without relaxing its value."""
    from core.auto_trainer.race_clock_retry import read_retry, _full_page
    if not allow_stock_reobserve or image is None:
        return read_retry(image, text)
    normalized = cv2.resize(image, (720, 1280)) if image.shape[:2] != (1280, 720) else image
    if not _full_page(normalized):
        return read_retry(image, text)
    from core.auto_trainer.career_goal_perception import _GOAL_REGION_READER
    reread = []
    def current_field(crop):
        value, confidence = _GOAL_REGION_READER.read_line(crop)
        value = re.sub(r"\s+", "", value)
        # These are exactly read_retry's full-page stock crops, not rank/text
        # fallback. A second reading must agree with the first numeric value.
        if (crop.shape[:2] not in {(55, 73), (55, 76)} or confidence >= .9
                or confidence < .85 or not re.fullmatch(r"[0-9]{1,5}", value)):
            return value, confidence
        inner, inner_confidence = _GOAL_REGION_READER.read_line(crop[6:-6, 4:-4])
        inner = re.sub(r"\s+", "", inner)
        if inner == value and inner_confidence >= .95:
            reread.append(True)
            return inner, inner_confidence
        return value, confidence
    proof = read_retry(image, text, reader=current_field)
    if proof is not None and reread:
        proof['stock_read_mode'] = 'same_frame_numeric_agreement'
    return proof


def is_race_scene_wait_result(result: dict[str, Any]) -> bool:
    """Return whether a completed route step is a budgeted race-scene wait."""

    decision = result.get("decision")
    return bool(
        result.get("performed")
        and isinstance(decision, dict)
        and decision.get("action") == "wait"
        and decision.get("route_state") == "race_scene"
        and decision.get("reason") in RACE_SCENE_WAIT_REASONS
    )


def larc_exchange_entry_released(decision: dict[str, Any]) -> bool:
    """The current recognized successor ends an exchange's confirmation ownership."""
    return decision.get("reason") in {
        "view_race_result", "go_to_race_from_detail", "confirm_race_playback",
        "race_scene_start_or_continue", "skip_or_advance_race_scene", "race_result_continue",
        "race_scene_running", "race_scene:skip_not_allowed", "race_result:continue_not_allowed",
        "race_preview:view_result_locked", "race_preview:advance_not_allowed",
        "ready_main_menu", "ready_training_menu", "race_day:enter_not_allowed",
    }


@dataclass(frozen=True)
class RaceRoutePolicy:
    scenario: str = "grand_masters"
    allow_enter_race: bool = False
    allow_enter_race_from_main_menu: bool = False
    allow_fan_goal_recommended_race: bool = False
    allow_larc_custom_scheduled_race: bool = False
    custom_target_race: dict[str, Any] | None = None
    allow_first_win_rescue: bool = False
    allow_select_visible_g1: bool = False
    allow_scan_race_list_for_g1: bool = False
    allow_leave_race_list: bool = False
    return_fan_shortfall_to_home: bool = False
    allow_leave_unverified_g1_race_list: bool = False
    allow_leave_race_detail: bool = False
    allow_confirm_race: bool = False
    allow_larc_unupgraded_entry: bool = False
    allow_quick_result: bool = False
    allow_skip_race_scene: bool = False
    allow_result_continue: bool = False
    allow_clock_retry: bool = False
    clock_retry_remaining: int | None = None
    clock_retry_unresolved: bool = False
    allow_debut_retry_entry: bool = False
    allow_result_retry_entry: bool = False
    allow_locked_race_entry: bool = False
    post_entry_only: bool = False
    desired_run_style: str | None = None
    allow_grand_masters_final_race_confirm: bool = False
    allow_cancel_unverified_grand_masters_final: bool = False
    visible_text: str | None = None
    wbc_only: bool = False
    inspection_only: bool = False
    count_reserved_only: bool = False
    count_result_persisted: bool = False
    _confirmed_live_ordinary_route: bool = False

    def ordinary_observed_control_scope(self):
        if self.inspection_only or self.wbc_only or self.count_reserved_only:
            return False
        forward = (self.allow_enter_race or self.allow_confirm_race
                   or self.allow_locked_race_entry or self.allow_quick_result
                   or self.allow_skip_race_scene or self.allow_result_continue or self.post_entry_only)
        requested = (self.return_fan_shortfall_to_home
                     or any(getattr(self, name) for name in self.__dataclass_fields__
                            if name.startswith("allow_")))
        # Navigation/cancellation/retry-only is not a race takeover.
        return not requested or forward

    def for_authorized_continuation(self, *, mode, execute, confirm_token):
        if (mode == "live" and execute and confirm_token == RACE_ROUTE_CONFIRM_TOKEN
                and self.ordinary_observed_control_scope()):
            return replace(self, _confirmed_live_ordinary_route=True)
        return self


@dataclass
class RaceRouteSession:
    """Explicit, single-use proofs staged by preceding race-route frames."""

    planned_race_ids: tuple[str, ...] = ()
    schedule_packet_sha256: str | None = None
    scheduled_entry_consumed: bool = False
    pending_target_race: dict[str, Any] | None = None
    pending_locked_target_race: dict[str, Any] | None = None
    pending_scenario_final_race: dict[str, Any] | None = None
    pending_fan_goal_recommendation: dict[str, Any] | None = None
    pending_fan_goal_shortfall_selected: dict[str, Any] | None = None
    pending_custom_scheduled_race: dict[str, Any] | None = None
    pending_wbc_hub_race: dict[str, Any] | None = None
    pending_gur_race: dict[str, Any] | None = None
    pending_larc_exchange: dict[str, Any] | None = None
    race_list_scan_viewports: tuple[str, ...] = ()
    wbc_entry_confirmed: bool = False
    inspection_phase: str = "new"
    inspection_source_sha256: str | None = None
    count_phase: str = "new"
    count_resume_preview_title: tuple[str, str] | None = None
    count_resume_watch_pending: bool = False
    count_resume_restart_title: tuple[str, str] | None = None
    result_transition_pending: bool = False
    clock_retries_used: int = 0
    last_clock_retry_remaining: int | None = None
    clock_retry_entry_pending: bool = False

    def __post_init__(self) -> None:
        has_ids = bool(self.planned_race_ids)
        has_packet = self.schedule_packet_sha256 is not None
        if has_ids != has_packet:
            raise ValueError("scheduled_race_binding_incomplete")
        if (
            type(self.planned_race_ids) is not tuple
            or any(
                type(race_id) is not str or not race_id
                for race_id in self.planned_race_ids
            )
            or len(set(self.planned_race_ids)) != len(self.planned_race_ids)
        ):
            raise ValueError("planned_race_ids_invalid")
        if has_packet and (
            type(self.schedule_packet_sha256) is not str
            or re.fullmatch(
                r"[0-9a-f]{64}",
                self.schedule_packet_sha256,
            )
            is None
        ):
            raise ValueError("schedule_packet_sha256_invalid")
        if type(self.scheduled_entry_consumed) is not bool:
            raise ValueError("scheduled_entry_consumed_invalid")
        if type(self.result_transition_pending) is not bool:
            raise ValueError("result_transition_pending_invalid")
        if type(self.clock_retries_used) is not int or not 0 <= self.clock_retries_used <= 5:
            raise ValueError("clock_retries_used_invalid")
        if type(self.clock_retry_entry_pending) is not bool:
            raise ValueError("clock_retry_entry_pending_invalid")
        if self.last_clock_retry_remaining is not None and (
                type(self.last_clock_retry_remaining) is not int or not 1 <= self.last_clock_retry_remaining <= 9):
            raise ValueError("last_clock_retry_remaining_invalid")
        if self.scheduled_entry_consumed and not has_packet:
            raise ValueError("scheduled_entry_consumed_without_binding")
        if (
            type(self.race_list_scan_viewports) is not tuple
            or len(self.race_list_scan_viewports) > 2
            or len(set(self.race_list_scan_viewports))
            != len(self.race_list_scan_viewports)
            or any(
                type(value) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in self.race_list_scan_viewports
            )
        ):
            raise ValueError("race_list_scan_viewports_invalid")

    @property
    def scheduled_binding_active(self) -> bool:
        return bool(self.planned_race_ids)

    def stage_race_list_scan(self, decision: dict[str, Any]) -> None:
        if self.scheduled_binding_active:
            raise ValueError("scheduled_race_grade_only_scan_forbidden")
        if decision.get("action") != "swipe":
            raise ValueError("race_list_scan_decision_required")
        gesture = decision.get("gesture")
        scan = (
            decision.get("evidence", {}).get("race_list_scan")
            if isinstance(decision.get("evidence"), dict)
            else None
        )
        if (
            not isinstance(gesture, dict)
            or gesture.get("coordinate_name")
            != "race_list.scan_for_visible_g1"
            or not isinstance(scan, dict)
        ):
            raise ValueError("race_list_scan_evidence_required")
        viewport_sha256 = scan.get("viewport_sha256")
        if (
            type(viewport_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", viewport_sha256) is None
        ):
            raise ValueError("race_list_scan_viewport_sha_invalid")
        if viewport_sha256 in self.race_list_scan_viewports:
            raise ValueError("race_list_scan_viewport_repeated")
        if len(self.race_list_scan_viewports) >= 2:
            raise ValueError("race_list_scan_limit_reached")
        if scan.get("scan_index") != len(self.race_list_scan_viewports) + 1:
            raise ValueError("race_list_scan_index_mismatch")
        self.race_list_scan_viewports += (viewport_sha256,)

    def stage_target_race_entry(self, decision: dict[str, Any]) -> None:
        if (
            self.pending_larc_exchange is not None
            or self.pending_scenario_final_race is not None
            or self.pending_fan_goal_recommendation is not None
            or self.pending_fan_goal_shortfall_selected is not None
            or self.pending_wbc_hub_race is not None
            or self.pending_gur_race is not None
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("target_race_verification")
        maiden = (isinstance(verification, dict) and verification.get("grade") == "MAIDEN"
                  and verification.get("source") == "race_list.current_first_win_requirement"
                  and decision.get("reason") == "enter_selected_first_win_race")
        if not isinstance(verification, dict) or (verification.get("grade") != "G1" and not maiden):
            raise ValueError("target_race_verification_required")
        if self.pending_locked_target_race is not None:
            evidence = decision.get("evidence")
            prior_locked = (
                evidence.get("prior_locked_target_verification")
                if isinstance(evidence, dict)
                else None
            )
            if prior_locked != self.pending_locked_target_race:
                raise ValueError("locked_target_upgrade_verification_required")
        if self.scheduled_binding_active:
            if self.scheduled_entry_consumed:
                raise ValueError("scheduled_race_session_consumed")
            if verification.get("race_id") not in self.planned_race_ids:
                raise ValueError("selected_race_not_planned")
            if (
                verification.get("schedule_packet_sha256")
                != self.schedule_packet_sha256
            ):
                raise ValueError("schedule_packet_sha256_mismatch")
        self.pending_locked_target_race = None
        self.pending_target_race = dict(verification)

    def stage_larc_exchange_entry(self, decision: dict[str, Any]) -> None:
        """Retain the current exchange across its optional confirmation dialogs."""
        proof = decision.get("larc_exchange_verification")
        if not isinstance(proof, dict) or proof.get("race_kind") != "larc_exchange":
            raise ValueError("larc_exchange_verification_required")
        if any(value is not None for value in (
            self.pending_target_race, self.pending_scenario_final_race,
            self.pending_fan_goal_recommendation, self.pending_fan_goal_shortfall_selected,
            self.pending_wbc_hub_race, self.pending_gur_race,
        )) or self.scheduled_binding_active:
            raise ValueError("ambiguous_race_verification")
        if self.pending_larc_exchange == proof:
            return  # Another dialog of this same exchange, not another entry.
        if self.pending_larc_exchange is not None:
            raise ValueError("larc_exchange_verification_changed")
        source = Path(str((decision.get("evidence") or {}).get("image", "")))
        if (proof.get("source") != "larc_exchange.current_detail"
                or decision.get("reason") != "confirm_race_entry"
                or (decision.get("tap") or {}).get("coordinate_name") != "race_confirm.primary"
                or not source.is_file()
                or proof.get("source_image_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()):
            raise ValueError("larc_exchange_detail_required")
        self.pending_larc_exchange = dict(proof)

    def target_race_verification(self) -> dict[str, Any] | None:
        return dict(self.pending_target_race) if self.pending_target_race is not None else None

    def consume_target_race_entry(self) -> None:
        self.pending_target_race = None
        self.pending_locked_target_race = None
        if self.scheduled_binding_active:
            self.scheduled_entry_consumed = True

    def stage_locked_target_race_entry(self, decision: dict[str, Any]) -> None:
        if (
            self.pending_larc_exchange is not None
            or self.pending_target_race is not None
            or self.pending_scenario_final_race is not None
            or self.pending_fan_goal_recommendation is not None
            or self.pending_fan_goal_shortfall_selected is not None
            or self.pending_wbc_hub_race is not None
            or self.pending_gur_race is not None
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("locked_target_verification")
        route_state = decision.get("route_state") or decision.get("state")
        tap = decision.get("tap")
        evidence = decision.get("evidence")
        image_path = (
            Path(str(evidence.get("image")))
            if isinstance(evidence, dict) and evidence.get("image")
            else None
        )
        actual_sha256 = None
        if image_path is not None and image_path.is_file():
            actual_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        main_menu_contract = isinstance(verification, dict) and all(
            (
                decision.get("reason") == "enter_race_from_race_day",
                route_state == "race_day_main_menu",
                isinstance(tap, dict),
                tap.get("coordinate_name") == "race_day.practice",
                verification.get("source") == "race_day_main_menu.locked_target",
                verification.get("action_id") == "race_day.practice",
                verification.get("single_use") is True,
                verification.get("source_image_sha256") == actual_sha256,
            )
        )
        race_list_contract = isinstance(verification, dict) and all(
            (
                decision.get("reason") == "enter_locked_target_race_from_list",
                route_state == "race_list",
                isinstance(tap, dict),
                tap.get("coordinate_name") == "race_list.primary_entry",
                verification.get("source") == "race_list.same_frame_locked_target",
                verification.get("action_id") == "race_list.primary_entry",
                verification.get("grade") in {"G2", "G3", "OPEN"},
                verification.get("single_use") is True,
                verification.get("source_image_sha256") == actual_sha256,
            )
        )
        if not (main_menu_contract or race_list_contract):
            raise ValueError("locked_target_verification_required")
        self.pending_locked_target_race = dict(verification)

    def locked_target_race_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_locked_target_race)
            if self.pending_locked_target_race is not None
            else None
        )

    def stage_scenario_final_race_entry(self, decision: dict[str, Any]) -> None:
        if (
            self.pending_larc_exchange is not None
            or self.pending_target_race is not None
            or self.pending_locked_target_race is not None
            or self.pending_fan_goal_recommendation is not None
            or self.pending_fan_goal_shortfall_selected is not None
            or self.pending_wbc_hub_race is not None
            or self.pending_gur_race is not None
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("scenario_final_verification")
        route_state = decision.get("route_state") or decision.get("state")
        if not isinstance(verification, dict) or not all(
            (
                decision.get("reason") == "enter_race_from_race_day",
                route_state == "race_day_main_menu",
                verification.get("source") == "grand_masters_final_race_day",
                verification.get("scenario") == "grand_masters",
                verification.get("cup") == "glory_goddess",
                verification.get("single_use") is True,
            )
        ):
            raise ValueError("scenario_final_verification_required")
        self.pending_scenario_final_race = dict(verification)

    def scenario_final_race_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_scenario_final_race)
            if self.pending_scenario_final_race is not None
            else None
        )

    def consume_scenario_final_race_entry(self) -> None:
        self.pending_scenario_final_race = None

    def stage_fan_goal_recommendation_entry(self, decision: dict[str, Any]) -> None:
        if (
            self.pending_target_race is not None
            or self.pending_locked_target_race is not None
            or self.pending_scenario_final_race is not None
            or self.pending_fan_goal_shortfall_selected is not None
            or self.pending_wbc_hub_race is not None
            or self.pending_gur_race is not None
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("fan_goal_recommendation_verification")
        route_state = decision.get("route_state") or decision.get("state")
        tap = decision.get("tap")
        evidence = decision.get("evidence")
        image_path = (
            Path(str(evidence.get("image")))
            if isinstance(evidence, dict) and evidence.get("image")
            else None
        )
        actual_sha256 = None
        if image_path is not None and image_path.is_file():
            actual_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if not isinstance(verification, dict) or not all(
            (
                decision.get("reason") == "enter_fan_goal_recommended_race",
                route_state == "race_list",
                isinstance(tap, dict),
                tap.get("coordinate_name") == "race_list.fan_goal_recommended_entry",
                verification.get("source") == "race_list.fan_goal_recommended_card",
                verification.get("goal_kind") == "fans",
                verification.get("goal_target") == 3000,
                verification.get("recommendation_rank") == 1,
                isinstance(verification.get("positive_fan_reward"), int),
                verification.get("positive_fan_reward", 0) > 0,
                verification.get("action_id") == "race_list.fan_goal_recommended_entry",
                verification.get("single_use") is True,
                verification.get("source_image_sha256") == actual_sha256,
            )
        ):
            raise ValueError("fan_goal_recommendation_verification_required")
        self.pending_fan_goal_recommendation = dict(verification)

    def fan_goal_recommendation_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_fan_goal_recommendation)
            if self.pending_fan_goal_recommendation is not None
            else None
        )

    def consume_fan_goal_recommendation_entry(self) -> None:
        self.pending_fan_goal_recommendation = None

    def stage_fan_goal_shortfall_selected_entry(
        self,
        decision: dict[str, Any],
    ) -> None:
        if self.scheduled_binding_active:
            raise ValueError("scheduled_race_binding_active")
        if any(
            pending is not None
            for pending in (
                self.pending_target_race,
                self.pending_locked_target_race,
                self.pending_scenario_final_race,
                self.pending_fan_goal_recommendation,
                self.pending_fan_goal_shortfall_selected,
                self.pending_wbc_hub_race,
                self.pending_gur_race,
            )
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("fan_goal_shortfall_selected_verification")
        route_state = decision.get("route_state") or decision.get("state")
        tap = decision.get("tap")
        evidence = decision.get("evidence")
        if (
            not isinstance(verification, dict)
            or decision.get("reason") != "enter_fan_goal_shortfall_selected_race"
            or route_state != "race_list"
            or not isinstance(tap, dict)
            or tap.get("coordinate_name")
            != "race_list.fan_goal_shortfall_selected_entry"
            or not isinstance(evidence, dict)
        ):
            raise ValueError("shortfall_selected_verification_required")
        image_path = (
            Path(str(evidence.get("image")))
            if evidence.get("image")
            else None
        )
        if image_path is None or not image_path.is_file():
            raise ValueError("shortfall_selected_verification_required")
        actual_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        race_name = verification.get("race_name")
        shortfall = verification.get("fan_shortfall")
        reward = verification.get("fan_reward")
        stars = verification.get("gold_star_surface_distance_evidence")
        source_sha256 = verification.get("source_image_sha256")
        if (
            verification.get("source")
            != "race_list.fan_goal_shortfall_selected_card"
            or verification.get("action_id")
            != "race_list.fan_goal_shortfall_selected_entry"
            or verification.get("single_use") is not True
            or type(race_name) is not str
            or not race_name
            or verification.get("grade") not in {"G1", "G2", "G3"}
            or type(shortfall) is not int
            or shortfall <= 0
            or type(reward) is not int
            or reward < shortfall
            or type(stars) is not list
            or stars
            not in (
                ["草地", "短距离"],
                ["草地", "英里"],
                ["草地", "中距离"],
                ["草地", "长距离"],
            )
            or type(source_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
            or source_sha256 != actual_sha256
        ):
            raise ValueError("shortfall_selected_verification_required")
        self.pending_fan_goal_shortfall_selected = dict(verification)

    def fan_goal_shortfall_selected_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_fan_goal_shortfall_selected)
            if self.pending_fan_goal_shortfall_selected is not None
            else None
        )

    def consume_fan_goal_shortfall_selected_entry(self) -> None:
        self.pending_fan_goal_shortfall_selected = None

    def stage_wbc_hub_race_entry(self, decision: dict[str, Any]) -> None:
        if any(
            pending is not None
            for pending in (
                self.pending_target_race,
                self.pending_locked_target_race,
                self.pending_scenario_final_race,
                self.pending_fan_goal_recommendation,
                self.pending_fan_goal_shortfall_selected,
                self.pending_gur_race,
            )
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("wbc_hub_verification")
        route_state = decision.get("route_state") or decision.get("state")
        tap = decision.get("tap")
        evidence = decision.get("evidence")
        image_path = (
            Path(str(evidence.get("image")))
            if isinstance(evidence, dict) and evidence.get("image")
            else None
        )
        actual_sha256 = None
        if image_path is not None and image_path.is_file():
            actual_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if not isinstance(verification, dict) or not all(
            (
                decision.get("reason") == "enter_race_from_wbc_hub",
                route_state == "wbc_race_hub",
                isinstance(tap, dict),
                tap.get("coordinate_name") == "wbc_hub.race",
                verification.get("source") == "wbc_race_hub",
                verification.get("scenario") == "grand_masters",
                verification.get("action_id") == "wbc_hub.race",
                verification.get("single_use") is True,
                verification.get("source_image_sha256") == actual_sha256,
            )
        ):
            raise ValueError("wbc_hub_verification_required")
        self.pending_wbc_hub_race = dict(verification)

    def wbc_hub_race_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_wbc_hub_race)
            if self.pending_wbc_hub_race is not None
            else None
        )

    def consume_wbc_hub_race_entry(self) -> None:
        self.pending_wbc_hub_race = None

    def stage_gur_race_entry(self, decision: dict[str, Any]) -> None:
        if any(
            pending is not None
            for pending in (
                self.pending_target_race,
                self.pending_locked_target_race,
                self.pending_scenario_final_race,
                self.pending_fan_goal_recommendation,
                self.pending_fan_goal_shortfall_selected,
                self.pending_wbc_hub_race,
            )
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("gur_race_verification")
        route_state = decision.get("route_state") or decision.get("state")
        tap = decision.get("tap")
        evidence = decision.get("evidence")
        image_path = (
            Path(str(evidence.get("image")))
            if isinstance(evidence, dict) and evidence.get("image")
            else None
        )
        actual_sha256 = None
        if image_path is not None and image_path.is_file():
            actual_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if not isinstance(verification, dict) or not all(
            (
                decision.get("reason") == "enter_gur_race",
                route_state == "gur_race_entry",
                isinstance(tap, dict),
                tap.get("coordinate_name") == "gur_race.primary_entry",
                verification.get("source") == "gur_race_entry.structured_frame",
                verification.get("scenario") == "grand_masters",
                verification.get("action_id") == "gur_race.primary_entry",
                verification.get("single_use") is True,
                verification.get("source_image_sha256") == actual_sha256,
            )
        ):
            raise ValueError("gur_race_verification_required")
        self.pending_gur_race = dict(verification)

    def gur_race_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_gur_race)
            if self.pending_gur_race is not None
            else None
        )

    def consume_gur_race_entry(self) -> None:
        self.pending_gur_race = None

    def stage_custom_scheduled_race_entry(self, decision: dict[str, Any]) -> None:
        if self.scheduled_binding_active or any(
            pending is not None
            for pending in (
                self.pending_target_race,
                self.pending_locked_target_race,
                self.pending_scenario_final_race,
                self.pending_fan_goal_recommendation,
                self.pending_fan_goal_shortfall_selected,
                self.pending_custom_scheduled_race,
                self.pending_wbc_hub_race,
                self.pending_gur_race,
            )
        ):
            raise ValueError("ambiguous_race_verification")
        verification = decision.get("custom_scheduled_race_verification")
        route_state = decision.get("route_state") or decision.get("state")
        tap = decision.get("tap")
        evidence = decision.get("evidence")
        image_path = (
            Path(str(evidence.get("image")))
            if isinstance(evidence, dict) and evidence.get("image")
            else None
        )
        actual_sha256 = (
            hashlib.sha256(image_path.read_bytes()).hexdigest()
            if image_path is not None and image_path.is_file()
            else None
        )
        if not isinstance(verification, dict) or not all(
            (
                decision.get("reason") == "enter_custom_scheduled_race",
                route_state == "race_list",
                isinstance(tap, dict),
                tap.get("coordinate_name") == "race_list.primary_entry",
                verification.get("source") == "race_list.custom_scheduled_selected_card",
                verification.get("action_id") == "race_list.primary_entry",
                isinstance(verification.get("race_id") or verification.get("id"), str) and bool(verification.get("race_id") or verification.get("id")),
                verification.get("single_use") is True,
                verification.get("source_image_sha256") == actual_sha256,
            )
        ):
            raise ValueError("custom_scheduled_race_verification_required")
        self.pending_custom_scheduled_race = dict(verification)

    def custom_scheduled_race_verification(self) -> dict[str, Any] | None:
        return (
            dict(self.pending_custom_scheduled_race)
            if self.pending_custom_scheduled_race is not None
            else None
        )

    def consume_custom_scheduled_race_entry(self) -> None:
        self.pending_custom_scheduled_race = None
