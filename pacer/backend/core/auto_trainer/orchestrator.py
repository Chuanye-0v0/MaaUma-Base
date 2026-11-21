from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from core.auto_trainer.career_goal_ledger import (
    CareerGoalLedger,
    CareerGoalLedgerError,
    CareerGoalLedgerStore,
)
from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.event_choice_route import EventChoiceExecutor, EventChoicePolicy
from core.auto_trainer.executor import TrainingActionExecutor
from core.auto_trainer.live_loop import UGParentTrainingLoop
from core.auto_trainer.race_route import (
    RaceRouteExecutor,
    RaceRoutePlanner,
    RaceRoutePolicy,
    RaceRouteSession,
    is_race_scene_wait_result,
)
from core.auto_trainer.race_calendar import RaceCalendar, RaceCalendarError, career_global_turn
from core.auto_trainer.race_plan import RacePlan
from core.auto_trainer.race_scheduler import (
    OpportunityConstrainedRaceScheduler,
    RaceEligibilityEvidence,
    RaceScheduleDecision,
    RaceSchedulerError,
    RaceSchedulingState,
    race_schedule_decision_to_dict,
)
from core.auto_trainer.safe_advance import SafeAdvanceExecutor
from core.auto_trainer.skip_session import RunSkipSession
from core.auto_trainer.route_transition_session import RouteTransitionSession
from core.auto_trainer.skill_handoff import (
    SkillHandoffResumeAuthorization,
    SkillHandoffSession,
)
from core.auto_trainer.state import TrainingTurnState
from core.coordinates import CoordinateConfig
from core.screen_text import analyze_screen_text


def _scenario_mandatory_race_ids(scenario_state: dict[str, Any]) -> tuple[str, ...]:
    mandatory = scenario_state.get("scenario_mandatory_race_ids", ())
    if type(mandatory) is list:
        mandatory = tuple(mandatory)
    if (
        type(mandatory) is not tuple
        or any(type(item) is not str or not item for item in mandatory)
        or len(set(mandatory)) != len(mandatory)
    ):
        raise RaceSchedulerError("scenario_mandatory_invalid")
    return mandatory


FINAL_PHASE_TRAINING_SCREENS = {"final_phase_training_menu", "final_phase_training_tab"}
FINAL_PHASE_MAIN_SCREENS = {"final_phase_main_menu"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _fan_count_from_state(scenario_state: dict[str, Any]) -> int | None:
    value = scenario_state.get("fan_count")
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise RaceSchedulerError("fan_count_invalid")
    return value


def _race_eligibility_from_state(
    scenario_state: dict[str, Any],
) -> dict[str, RaceEligibilityEvidence]:
    raw = scenario_state.get("race_eligibility", {})
    if type(raw) is not dict:
        raise RaceSchedulerError("eligibility_mapping_invalid")
    result: dict[str, RaceEligibilityEvidence] = {}
    expected_keys = {
        "aptitude_ok",
        "condition_ok",
        "consecutive_race_ok",
        "evidence_sha256",
    }
    for race_id, row in raw.items():
        if (
            type(race_id) is not str
            or type(row) is not dict
            or set(row) != expected_keys
        ):
            raise RaceSchedulerError("eligibility_mapping_invalid")
        result[race_id] = RaceEligibilityEvidence(
            race_id=race_id,
            aptitude_ok=row["aptitude_ok"],
            condition_ok=row["condition_ok"],
            consecutive_race_ok=row["consecutive_race_ok"],
            evidence_sha256=row["evidence_sha256"],
        )
    return result


@dataclass(frozen=True)
class AutonomousOrchestratorPolicy:
    execute_training_confirm: bool = False
    training_confirm_token: str | None = None
    execute_safe_advance: bool = False
    safe_advance_confirm_token: str | None = None
    allow_choose_event: bool = False
    execute_event_choice: bool = False
    event_choice_confirm_token: str | None = None
    event_choice: str = "top"
    allow_enter_race: bool = False
    allow_enter_race_from_main_menu: bool = False
    allow_leave_race_list: bool = False
    allow_select_visible_g1: bool = False
    allow_scan_race_list_for_g1: bool = False
    allow_confirm_race: bool = False
    allow_quick_result: bool = False
    allow_skip_race_scene: bool = False
    allow_result_continue: bool = False
    allow_locked_race_entry: bool = False
    execute_race_route: bool = False
    race_route_confirm_token: str | None = None
    race_scene_wait_limit: int = 25
    allow_team_formation_confirm: bool = False
    resync_route_transition: bool = False
    skill_handoff_authorization: SkillHandoffResumeAuthorization | None = None
    skill_handoff_session: SkillHandoffSession | None = None
    visible_text: str | None = None


class DefaultOrchestratorRoutes:
    def __init__(
        self,
        *,
        goal: UGParentGoal,
        run_dir: Path,
        adb_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.goal = goal
        self.run_dir = Path(run_dir)
        self.adb_factory = adb_factory
        self.training_loop = UGParentTrainingLoop(
            goal=goal,
            run_dir=run_dir,
            executor=TrainingActionExecutor(adb_factory=adb_factory),
        )
        self.safe_executor = SafeAdvanceExecutor(adb_factory=adb_factory)
        self.skip_session_path = self.run_dir / "run_skip_session.json"
        self.skip_session = (
            RunSkipSession.load(
                self.skip_session_path,
                expected_run_session_id=self.run_dir.name,
            )
            if self.skip_session_path.exists()
            else RunSkipSession.new(self.run_dir.name)
        )
        self.transition_session_path = self.run_dir / "run_route_transition.json"
        self.transition_session = (
            RouteTransitionSession.load(
                self.transition_session_path,
                expected_run_session_id=self.run_dir.name,
            )
            if self.transition_session_path.exists()
            else RouteTransitionSession.new(self.run_dir.name)
        )
        self.event_executor = EventChoiceExecutor(adb_factory=adb_factory)
        self.race_executor = RaceRouteExecutor(adb_factory=adb_factory)

    def run_training_turn(self, state: TrainingTurnState, **kwargs: Any) -> dict[str, Any]:
        return self.training_loop.run_turn(state, **kwargs)

    def run_safe_once(self, image_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        kwargs["skip_session"] = self.skip_session
        kwargs["transition_session"] = self.transition_session
        result = self.safe_executor.run_once(screenshot_path=image_path, **kwargs)
        result["skip_session"] = self.skip_session.to_dict()
        if kwargs.get("mode") == "live":
            result["route_transition"] = self.transition_session.to_dict()
            self.skip_session.save(self.skip_session_path)
            self.transition_session.save(self.transition_session_path)
        else:
            result.setdefault("route_transition", self.transition_session.to_dict())
        return result

    def run_event_once(self, image_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self.event_executor.run_once(screenshot_path=image_path, **kwargs)

    def run_race_route(self, image_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        max_steps = int(kwargs.pop("max_steps", 1) or 1)
        race_scene_wait_limit = int(kwargs.pop("race_scene_wait_limit", 25) or 0)
        screenshot_dir = Path(kwargs.pop("screenshot_dir"))
        mode = str(kwargs.get("mode", "dry-run"))
        planned_race_ids = kwargs.pop("planned_race_ids", ())
        schedule_packet_sha256 = kwargs.pop("schedule_packet_sha256", None)
        step_reports: list[dict[str, Any]] = []
        performed = 0
        stop_reason = "max_steps_reached"
        race_scene_waits = 0
        session = RaceRouteSession(
            planned_race_ids=planned_race_ids,
            schedule_packet_sha256=schedule_packet_sha256,
        )

        for step in range(1, max_steps + 1):
            step_path = (
                Path(image_path)
                if step == 1 and image_path is not None
                else screenshot_dir / f"race_route_step_{step:03d}.png"
            )
            result = self.race_executor.run_once(
                screenshot_path=step_path,
                session=session,
                **kwargs,
            )
            step_reports.append({"step": step, "result": result})
            if is_race_scene_wait_result(result):
                race_scene_waits += 1
            else:
                race_scene_waits = 0
            if result.get("performed"):
                performed += 1
                if race_scene_wait_limit > 0 and race_scene_waits >= race_scene_wait_limit:
                    stop_reason = "race_scene:wait_limit_reached"
                    break
                continue
            stop_reason = str(result.get("reason", "stopped"))
            break
            # In dry-run, tap plans return performed=False/dry_run, so the route
            # stops after planning one safe action instead of looping on a static image.
        else:
            stop_reason = "max_steps_reached"

        return {
            "status": "completed" if stop_reason == "max_steps_reached" else "stopped",
            "performed_steps": performed,
            "requested_steps": max_steps,
            "stop_reason": stop_reason,
            "step_reports": step_reports,
            "mode": mode,
        }


class UGParentAutonomousOrchestrator:
    def __init__(
        self,
        *,
        goal: UGParentGoal,
        run_dir: str | Path,
        routes: Any | None = None,
        screen_detector: Callable[..., str] | None = None,
        adb_factory: Callable[[], Any] | None = None,
        race_calendar: RaceCalendar | None = None,
        race_plan: RacePlan | None = None,
        goal_ledger_store: CareerGoalLedgerStore | None = None,
        race_scheduler: OpportunityConstrainedRaceScheduler | None = None,
        career_goal_ledger_path: str | Path | None = None,
    ) -> None:
        self.goal = goal
        self.run_dir = Path(run_dir)
        self.routes = routes or DefaultOrchestratorRoutes(
            goal=goal,
            run_dir=self.run_dir,
            adb_factory=adb_factory,
        )
        self.screen_detector = screen_detector or self._detect_screen
        self.race_calendar = race_calendar
        self.race_plan = race_plan
        self.goal_ledger_store = goal_ledger_store
        self.race_scheduler = race_scheduler
        self.career_goal_ledger_path = (
            Path(career_goal_ledger_path)
            if career_goal_ledger_path is not None
            else self.run_dir / "career_goal_ledger.json"
        )

    @property
    def race_scheduling_enabled(self) -> bool:
        configured = (
            self.race_calendar,
            self.race_plan,
            self.goal_ledger_store,
            self.race_scheduler,
        )
        if all(item is None for item in configured):
            return False
        if any(item is None for item in configured):
            raise RaceSchedulerError("race_scheduler_dependencies_incomplete")
        return True

    def requires_main_menu_schedule(self, *, career_state: TrainingTurnState | None = None) -> bool:
        if not self.race_scheduling_enabled:
            return False
        mandatory = _scenario_mandatory_race_ids(
            career_state.scenario_state if career_state is not None else {}
        )
        ledger_path = self.career_goal_ledger_path
        ledger = (
            self.goal_ledger_store.load(ledger_path, expected_run_session_id=self.run_dir.name)
            if ledger_path.exists() else CareerGoalLedger.empty(self.run_dir.name)
        )
        return bool(
            any(goal.goal_kind == "graded_race_rank_count" for goal in ledger.active_goals())
            or any(item.executable for item in self.race_plan.directives)
            or mandatory
        )

    def _calendar_turn_from_anchor(self, career_state: TrainingTurnState, source_sha: str) -> int:
        anchor = career_state.scenario_state.get("career_calendar_anchor")
        if not isinstance(anchor, dict) or anchor.get("status") != "verified" or anchor.get("basis") != "same_frame_main_menu_header":
            raise RaceSchedulerError("race_schedule_calendar_unbound")
        if anchor.get("source_frame_sha256") != source_sha:
            raise RaceSchedulerError("race_schedule_calendar_source_mismatch")
        time = anchor.get("career_time")
        if not isinstance(time, dict):
            raise RaceSchedulerError("race_schedule_calendar_unbound")
        try:
            turn = career_global_turn(
                stage=time.get("stage"), year_index=time.get("year_index"),
                month=time.get("month"), half=time.get("half"),
            )
        except (RaceCalendarError, TypeError) as exc:
            raise RaceSchedulerError("race_schedule_calendar_unbound") from exc
        remaining = anchor.get("remaining_turns")
        evidence = anchor.get("evidence")
        remaining_evidence = evidence.get("remaining_field_evidence") if isinstance(evidence, dict) else None
        race_id = anchor.get("target_race_id")
        entry = self.race_calendar.get(race_id) if type(race_id) is str else None
        if (
            type(time.get("global_turn")) is not int or time["global_turn"] != turn
            or type(remaining) is not int or not 0 <= remaining <= 99
            or not isinstance(remaining_evidence, dict)
            or type(remaining_evidence.get("value")) is not int
            or remaining_evidence["value"] != remaining
            or anchor.get("goal_kind") != "named_race_rank"
            or type(anchor.get("required_rank")) is not int or anchor["required_rank"] <= 0
            or entry is None or entry.schedule_status != "verified" or entry.career_time is None
            or entry.grade != anchor.get("grade")
            or type(anchor.get("target_calendar_turn")) is not int
            or anchor["target_calendar_turn"] != entry.career_time.global_turn
            or turn + remaining != entry.career_time.global_turn
        ):
            raise RaceSchedulerError("race_schedule_calendar_unbound")
        return turn

    def plan_ready_main_menu(
        self,
        *,
        career_state: TrainingTurnState,
        source_frame_sha256: str,
    ) -> RaceScheduleDecision:
        if not self.race_scheduling_enabled:
            raise RaceSchedulerError("race_scheduler_not_configured")
        if not isinstance(career_state, TrainingTurnState):
            raise RaceSchedulerError("training_turn_state_required")
        if (
            type(source_frame_sha256) is not str
            or _SHA256_RE.fullmatch(source_frame_sha256) is None
        ):
            raise RaceSchedulerError("race_schedule_source_sha_invalid")
        scenario_state = career_state.scenario_state
        if scenario_state.get("source_frame_sha256") != source_frame_sha256:
            raise RaceSchedulerError("race_schedule_source_sha_mismatch")
        if career_state.turn is None or career_state.turn <= 0:
            raise RaceSchedulerError("race_schedule_turn_unknown")
        eligibility = _race_eligibility_from_state(scenario_state)
        mandatory = _scenario_mandatory_race_ids(scenario_state)
        ledger_path = self.career_goal_ledger_path
        ledger = (
            self.goal_ledger_store.load(
                ledger_path,
                expected_run_session_id=self.run_dir.name,
            )
            if ledger_path.exists()
            else CareerGoalLedger.empty(self.run_dir.name)
        )
        count_goals = tuple(goal for goal in ledger.active_goals()
                            if goal.goal_kind == "graded_race_rank_count")
        if count_goals:
            if any(goal.progress_basis != "verified_lower_bound" or goal.deadline_turn is None
                   for goal in count_goals):
                raise RaceSchedulerError("career_goal_calendar_unbound")
            anchor = scenario_state.get("career_calendar_anchor")
            validated = self.goal_ledger_store.bind_count_goal_deadline(
                ledger, anchor=anchor, observed_turn=career_state.turn,
                source_frame_sha256=source_frame_sha256,
                expected_run_session_id=self.run_dir.name,
            )
            if validated.status != "no_change":
                raise RaceSchedulerError("career_goal_calendar_unbound")
            calendar_turn = anchor["career_time"]["global_turn"]
        else:
            calendar_turn = self._calendar_turn_from_anchor(career_state, source_frame_sha256)
        return self.race_scheduler.get_schedule_candidates(
            career_state=RaceSchedulingState(
                current_turn=calendar_turn,
                observed_runner_turn=career_state.turn,
                fan_count=_fan_count_from_state(scenario_state),
                eligibility_by_race_id=eligibility,
            ),
            ledger=ledger,
            calendar=self.race_calendar,
            race_plan=self.race_plan,
            scenario_mandatory=mandatory,
        )

    def run(
        self,
        *,
        initial_state: TrainingTurnState | None = None,
        screenshot_path: str | Path | None = None,
        screen_hint: str | None = None,
        sidecar_path: str | Path | None = None,
        policy: AutonomousOrchestratorPolicy | None = None,
        mode: str = "dry-run",
        max_turns: int = 1,
        max_route_steps: int = 1,
        min_decision_confidence: float = 0.80,
    ) -> dict[str, Any]:
        policy = policy or AutonomousOrchestratorPolicy()
        report = self._base_report(
            mode=mode,
            max_turns=max_turns,
            min_decision_confidence=min_decision_confidence,
            screenshot_path=screenshot_path,
            sidecar_path=sidecar_path,
        )
        if max_turns <= 0:
            return self._finish(report, status="stopped", stop_reason="max_turns_must_be_positive")

        if policy.visible_text is not None:
            screen_text = analyze_screen_text(policy.visible_text)
            if screen_text.get("dangerous"):
                report["steps"].append(
                    {
                        "step": 1,
                        "tool": "screen_text_safety",
                        "screen": "resource_prompt",
                        "action": None,
                        "reason": "resource_prompt",
                        "screenshot_path": (
                            str(screenshot_path)
                            if screenshot_path is not None
                            else None
                        ),
                        "result": {"screen_text": screen_text},
                    }
                )
                return self._finish(
                    report,
                    status="stopped",
                    stop_reason="resource_prompt",
                    next_recommendation="manual_review_required",
                )

        screen = self._screen_for(
            initial_state=initial_state,
            screenshot_path=screenshot_path,
            screen_hint=screen_hint,
            visible_text=policy.visible_text,
        )
        if self._is_dangerous_screen(screen):
            return self._finish(report, status="stopped", stop_reason=screen)

        if screen in FINAL_PHASE_TRAINING_SCREENS:
            if initial_state is not None and initial_state.training_options:
                return self._run_training(
                    report,
                    state=replace(initial_state, screen="training_menu"),
                    policy=policy,
                    mode=mode,
                    max_turns=max_turns,
                    min_decision_confidence=min_decision_confidence,
                    sidecar_path=sidecar_path,
                    screen_label="final_phase_training_menu",
                    completed_stop_reason="final_phase_turn_completed",
                )
            return self._run_final_phase_policy(
                report,
                screen=screen,
                state=initial_state,
                screenshot_path=screenshot_path,
            )

        if screen in FINAL_PHASE_MAIN_SCREENS:
            return self._run_final_phase_policy(
                report,
                screen=screen,
                state=initial_state,
                screenshot_path=screenshot_path,
            )

        if screen == "training_menu":
            if initial_state is None:
                return self._finish(
                    report,
                    status="stopped",
                    stop_reason="training_state_required",
                    next_recommendation="capture_five_training_tabs",
                )
            return self._run_training(
                report,
                state=initial_state,
                policy=policy,
                mode=mode,
                max_turns=max_turns,
                min_decision_confidence=min_decision_confidence,
                sidecar_path=sidecar_path,
            )

        if (
            screen == "ready_main_menu"
            and initial_state is not None
            and self.race_scheduling_enabled
        ):
            if screenshot_path is None:
                return self._finish(
                    report,
                    status="stopped",
                    stop_reason="screenshot_required",
                )
            image_path = Path(screenshot_path)
            try:
                source_frame_sha256 = hashlib.sha256(
                    image_path.read_bytes()
                ).hexdigest()
                schedule = (
                    self.plan_ready_main_menu(
                        career_state=initial_state, source_frame_sha256=source_frame_sha256,
                    )
                    if self.requires_main_menu_schedule(career_state=initial_state) else None
                )
            except CareerGoalLedgerError:
                return self._finish(
                    report,
                    status="stopped",
                    stop_reason="career_goal_ledger_invalid",
                )
            except (OSError, RaceSchedulerError) as exc:
                return self._finish(
                    report,
                    status="stopped",
                    stop_reason=str(exc),
                )
            if schedule is None:
                return self._run_safe_advance(
                    report, image_path=image_path, policy=policy, mode=mode,
                    career_state=initial_state,
                )
            schedule_payload = race_schedule_decision_to_dict(schedule)
            report["race_schedule"] = schedule_payload
            report["steps"].append(
                {
                    "step": len(report["steps"]) + 1,
                    "tool": "race_schedule",
                    "screen": "ready_main_menu",
                    "action": (
                        schedule.candidates[0].action_kind
                        if schedule.candidates
                        else None
                    ),
                    "reason": schedule.status,
                    "screenshot_path": str(image_path),
                    "source_frame_sha256": source_frame_sha256,
                    "result": schedule_payload,
                }
            )
            if schedule.status == "career_goal_schedule_unfulfillable":
                return self._finish(
                    report,
                    status="stopped",
                    stop_reason=schedule.status,
                )
            scheduled_races = tuple(
                candidate.race_id
                for candidate in schedule.candidates
                if candidate.action_kind == "enter_race"
                and candidate.priority in {"hard", "must"}
                and candidate.race_id is not None
            )
            if scheduled_races:
                return self._run_race_route(
                    report,
                    image_path=image_path,
                    policy=policy,
                    mode=mode,
                    max_route_steps=max_route_steps,
                    planned_race_ids=scheduled_races,
                    schedule_packet_sha256=schedule.packet_sha256,
                )

        if screenshot_path is None:
            return self._finish(report, status="stopped", stop_reason="screenshot_required")

        image_path = Path(screenshot_path)
        if screen in {
            "ready_main_menu",
            "main_menu",
            "post_race_dialogue",
            "skill_acquisition",
            "story",
        }:
            return self._run_safe_advance(
                report,
                image_path=image_path,
                policy=policy,
                mode=mode,
                career_state=initial_state,
            )

        if screen == "event_choice":
            return self._run_event_choice(report, image_path=image_path, policy=policy, mode=mode)

        if screen in {"race_list", "race_detail", "race_scene", "race_result"}:
            return self._run_race_route(
                report,
                image_path=image_path,
                policy=policy,
                mode=mode,
                max_route_steps=max_route_steps,
            )

        return self._finish(
            report,
            status="stopped",
            stop_reason="unknown_screen",
            next_recommendation="manual_review_required",
        )

    def _run_training(
        self,
        report: dict[str, Any],
        *,
        state: TrainingTurnState,
        policy: AutonomousOrchestratorPolicy,
        mode: str,
        max_turns: int,
        min_decision_confidence: float,
        sidecar_path: str | Path | None,
        screen_label: str = "training_menu",
        completed_stop_reason: str = "turn_completed",
    ) -> dict[str, Any]:
        result = self.routes.run_training_turn(
            state,
            mode=mode,
            execute_training_confirm=policy.execute_training_confirm,
            confirm_token=policy.training_confirm_token,
            max_turns=max_turns,
            min_decision_confidence=min_decision_confidence,
        )
        execution = result.get("execution", {}) if isinstance(result, dict) else {}
        decision = result.get("decision", {}) if isinstance(result, dict) else {}
        reason = str(execution.get("reason") or decision.get("reason") or "turn_completed")
        performed = bool(execution.get("performed"))
        report["steps"].append(
            {
                "step": len(report["steps"]) + 1,
                "tool": "training_loop",
                "screen": screen_label,
                "action": decision.get("action"),
                "decision": decision,
                "execution": execution,
                "reason": reason,
                "sidecar_path": str(sidecar_path) if sidecar_path is not None else None,
            }
        )
        if performed:
            report["turns_completed"] = 1
            return self._finish(report, status="completed", stop_reason=completed_stop_reason)
        return self._finish(
            report,
            status="stopped",
            stop_reason=reason,
            next_recommendation=self._recommendation_for_stop(reason),
        )

    def _run_final_phase_policy(
        self,
        report: dict[str, Any],
        *,
        screen: str,
        state: TrainingTurnState | None,
        screenshot_path: str | Path | None,
    ) -> dict[str, Any]:
        decision = self._final_phase_decision(screen=screen, state=state)
        report["steps"].append(
            {
                "step": len(report["steps"]) + 1,
                "tool": "final_phase_policy",
                "screen": screen,
                "action": decision["action"],
                "target": decision.get("target"),
                "reason": decision["reason"],
                "tap_plan": decision["tap_plan"],
                "screenshot_path": str(screenshot_path) if screenshot_path is not None else None,
                "evidence": decision["evidence"],
            }
        )
        return self._finish(
            report,
            status="stopped",
            stop_reason="final_phase_action_planned",
            next_recommendation="review_planned_final_phase_action",
        )

    def _final_phase_decision(
        self,
        *,
        screen: str,
        state: TrainingTurnState | None,
    ) -> dict[str, Any]:
        vitality = state.vitality if state is not None else None
        if vitality is not None and vitality < 35:
            return {
                "action": "rest",
                "target": None,
                "reason": "final_phase_low_vitality_rest",
                "tap_plan": self._final_phase_rest_tap_plan(screen),
                "evidence": {"vitality": vitality},
            }

        if screen in FINAL_PHASE_MAIN_SCREENS:
            return {
                "action": "enter_training",
                "target": None,
                "reason": "final_phase_enter_training_from_main_menu",
                "tap_plan": [self._tap_record("main_menu", "training")],
                "evidence": {"vitality": vitality},
            }

        target = self._best_final_phase_target(state)
        return {
            "action": "train",
            "target": target,
            "reason": "final_phase_training_menu_conservative_train",
            "tap_plan": [
                self._tap_record("training_menu", target),
                self._tap_record("training_menu", f"{target}_decide"),
            ],
            "evidence": {"vitality": vitality, "fallback_target": target},
        }

    def _final_phase_rest_tap_plan(self, screen: str) -> list[dict[str, Any]]:
        if screen in FINAL_PHASE_TRAINING_SCREENS:
            return [
                self._tap_record("training_menu", "back"),
                self._tap_record("main_menu", "rest"),
            ]
        return [self._tap_record("main_menu", "rest")]

    def _best_final_phase_target(self, state: TrainingTurnState | None) -> str:
        if state is None or not state.training_options:
            return "wisdom"
        scored = [
            (candidate.total_gain(), candidate.confidence, candidate.training_type)
            for candidate in state.training_options
            if candidate.failure_rate is None or candidate.failure_rate <= 30
        ]
        if not scored:
            return "wisdom"
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return scored[0][2]

    @staticmethod
    def _tap_record(section: str, name: str) -> dict[str, Any]:
        x, y = CoordinateConfig().point(section, name)
        return {
            "coordinate_name": f"{section}.{name}",
            "x": x,
            "y": y,
            "scaled": True,
        }

    def _run_safe_advance(
        self,
        report: dict[str, Any],
        *,
        image_path: Path,
        policy: AutonomousOrchestratorPolicy,
        mode: str,
        recovery: bool = False,
        career_state: TrainingTurnState | None = None,
    ) -> dict[str, Any]:
        safe_kwargs: dict[str, Any] = {
            "mode": mode,
            "execute": policy.execute_safe_advance,
            "confirm_token": policy.safe_advance_confirm_token,
            "allow_team_formation_confirm": (
                policy.allow_team_formation_confirm
            ),
            "skill_handoff_authorization": (
                policy.skill_handoff_authorization
            ),
            "skill_handoff_session": policy.skill_handoff_session,
            "visible_text": (
                policy.visible_text if mode == "dry-run" else None
            ),
            "transition_resync": policy.resync_route_transition,
        }
        if (
            career_state is not None
            and career_state.turn is not None
            and career_state.turn > 0
            and self.goal_ledger_store is not None
        ):
            safe_kwargs.update(
                {
                    "career_goal_observed_turn": career_state.turn,
                    "career_goal_ledger_path": self.career_goal_ledger_path,
                    "career_goal_run_session_id": self.run_dir.name,
                    "career_goal_ledger_store": self.goal_ledger_store,
                }
            )
        result = self.routes.run_safe_once(image_path, **safe_kwargs)
        reason = str(result.get("reason", "stopped"))
        if reason == "skill_acquisition_handoff" and isinstance(
            result.get("skill_handoff"), dict
        ):
            report["skill_handoff"] = result["skill_handoff"]
        self._append_tool_step(
            report,
            tool="safe_advance",
            screen="post_race_dialogue" if recovery else "main_menu",
            image_path=image_path,
            result=result,
            reason=reason,
        )
        recommendation = "rerun_after_safe_recovery" if recovery and self._planned_tap(result) else None
        return self._finish(
            report,
            status="stopped",
            stop_reason=reason,
            next_recommendation=recommendation or self._recommendation_for_stop(reason),
        )

    def _run_event_choice(
        self,
        report: dict[str, Any],
        *,
        image_path: Path,
        policy: AutonomousOrchestratorPolicy,
        mode: str,
        recovery: bool = False,
    ) -> dict[str, Any]:
        result = self.routes.run_event_once(
            image_path,
            mode=mode,
            execute=policy.execute_event_choice,
            confirm_token=policy.event_choice_confirm_token,
            policy=EventChoicePolicy(
                allow_choose_event=policy.allow_choose_event,
                choice=policy.event_choice,
            ),
        )
        reason = str(result.get("reason", "stopped"))
        self._append_tool_step(
            report,
            tool="event_choice_route",
            screen="event_choice",
            image_path=image_path,
            result=result,
            reason=reason,
        )
        recommendation = "rerun_after_event_choice" if recovery and self._planned_tap(result) else None
        return self._finish(
            report,
            status="stopped",
            stop_reason=reason,
            next_recommendation=recommendation or self._recommendation_for_stop(reason),
        )

    def _run_race_route(
        self,
        report: dict[str, Any],
        *,
        image_path: Path,
        policy: AutonomousOrchestratorPolicy,
        mode: str,
        max_route_steps: int,
        planned_race_ids: tuple[str, ...] = (),
        schedule_packet_sha256: str | None = None,
    ) -> dict[str, Any]:
        route_report = self.routes.run_race_route(
            image_path,
            mode=mode,
            execute=policy.execute_race_route,
            confirm_token=policy.race_route_confirm_token,
            policy=RaceRoutePolicy(
                allow_enter_race=policy.allow_enter_race,
                allow_enter_race_from_main_menu=policy.allow_enter_race_from_main_menu,
                allow_leave_race_list=policy.allow_leave_race_list,
                allow_select_visible_g1=policy.allow_select_visible_g1,
                allow_scan_race_list_for_g1=(
                    policy.allow_scan_race_list_for_g1
                ),
                allow_confirm_race=policy.allow_confirm_race,
                allow_quick_result=policy.allow_quick_result,
                allow_skip_race_scene=policy.allow_skip_race_scene,
                allow_result_continue=policy.allow_result_continue,
                allow_locked_race_entry=policy.allow_locked_race_entry,
                visible_text=policy.visible_text,
            ),
            screenshot_dir=self.run_dir / "race_route",
            max_steps=max_route_steps,
            race_scene_wait_limit=policy.race_scene_wait_limit,
            planned_race_ids=planned_race_ids,
            schedule_packet_sha256=schedule_packet_sha256,
        )
        stop_reason = str(route_report.get("stop_reason", "stopped"))
        report["steps"].append(
            {
                "step": len(report["steps"]) + 1,
                "tool": "race_route",
                "screen": "race_route",
                "action": None,
                "reason": stop_reason,
                "screenshot_path": str(image_path),
                "result": route_report,
            }
        )
        if stop_reason == "max_steps_reached":
            recovery_screen = self._screen_for(
                initial_state=None,
                screenshot_path=image_path,
                screen_hint=None,
                visible_text=policy.visible_text,
            )
            if recovery_screen == "event_choice":
                self._run_event_choice(
                    report,
                    image_path=image_path,
                    policy=policy,
                    mode=mode,
                    recovery=True,
                )
            else:
                recovery_report = self._run_safe_advance(
                    report,
                    image_path=image_path,
                    policy=policy,
                    mode=mode,
                    recovery=True,
                )
                if recovery_report.get("stop_reason") == "skill_acquisition_handoff":
                    return recovery_report
            return self._finish(
                report,
                status="stopped",
                stop_reason="race_route:max_steps_reached",
                next_recommendation=report.get("next_recommendation") or "manual_review_required",
            )
        return self._finish(
            report,
            status="stopped",
            stop_reason=stop_reason,
            next_recommendation=self._recommendation_for_stop(stop_reason),
        )

    def _screen_for(
        self,
        *,
        initial_state: TrainingTurnState | None,
        screenshot_path: str | Path | None,
        screen_hint: str | None,
        visible_text: str | None,
    ) -> str:
        if initial_state is not None:
            return self._normalize_screen(initial_state.screen)
        if screen_hint:
            return self._normalize_screen(screen_hint)
        return self._normalize_screen(
            self.screen_detector(
                screenshot_path=screenshot_path,
                visible_text=visible_text,
            )
        )

    @staticmethod
    def _detect_screen(**kwargs: Any) -> str:
        screenshot_path = kwargs.get("screenshot_path")
        if screenshot_path is None:
            return "unknown"
        decision = RaceRoutePlanner().plan(
            screenshot_path,
            policy=RaceRoutePolicy(visible_text=kwargs.get("visible_text")),
        )
        reason = str(decision.get("reason", "unknown"))
        route_state = str(decision.get("route_state", ""))
        if reason == "skill_acquisition_handoff" or route_state == "skill_acquisition":
            return "skill_acquisition"
        if reason == "ready_training_menu":
            return (
                "final_phase_training_menu"
                if UGParentAutonomousOrchestrator._visible_text_is_final_phase(
                    kwargs.get("visible_text")
                )
                else "training_menu"
            )
        if reason == "ready_main_menu":
            return (
                "final_phase_main_menu"
                if UGParentAutonomousOrchestrator._visible_text_is_final_phase(
                    kwargs.get("visible_text")
                )
                else "ready_main_menu"
            )
        if reason in {"event_choice", "event_choice:choose_not_allowed"}:
            if UGParentAutonomousOrchestrator._training_menu_precheck_accepts(
                screenshot_path
            ):
                return (
                    "final_phase_training_menu"
                    if UGParentAutonomousOrchestrator._visible_text_is_final_phase(
                        kwargs.get("visible_text")
                    )
                    else "training_menu"
                )
            return "event_choice"
        if reason in {"post_race_dialogue", "advance_dialogue"}:
            return "post_race_dialogue"
        if "resource_prompt" in reason:
            return "resource_prompt"
        if route_state in {"race_list", "race_detail", "race_scene", "race_result"}:
            return route_state
        if reason.startswith("race_list"):
            return "race_list"
        if reason.startswith("race_detail"):
            return "race_detail"
        if reason.startswith("race_scene"):
            return "race_scene"
        if reason.startswith("race_result"):
            return "race_result"
        return "unknown"

    @staticmethod
    def _normalize_screen(screen: str | None) -> str:
        value = str(screen or "unknown").strip().lower()
        aliases = {
            "main": "main_menu",
            "ready": "ready_main_menu",
            "ready_training_menu": "training_menu",
            "final-training-menu": "final_phase_training_menu",
            "final_training_menu": "final_phase_training_menu",
            "final_phase_training": "final_phase_training_menu",
            "final_phase_tab": "final_phase_training_tab",
            "final-main-menu": "final_phase_main_menu",
            "final_main_menu": "final_phase_main_menu",
            "race_day": "race_list",
            "race_day_main_menu": "race_list",
            "goal_route": "race_list",
            "story_dialogue": "post_race_dialogue",
            "race_story": "post_race_dialogue",
        }
        return aliases.get(value, value)

    @staticmethod
    def _is_dangerous_screen(screen: str) -> bool:
        danger_tokens = (
            "resource",
            "shop",
            "gacha",
            "payment",
            "purchase",
            "clock",
            "jewel",
            "tp",
            "rp",
            "carrot",
            "paid",
        )
        return any(token in screen for token in danger_tokens)

    @staticmethod
    def _planned_tap(result: dict[str, Any]) -> bool:
        decision = result.get("decision")
        return isinstance(decision, dict) and decision.get("action") == "tap"

    def _append_tool_step(
        self,
        report: dict[str, Any],
        *,
        tool: str,
        screen: str,
        image_path: Path,
        result: dict[str, Any],
        reason: str,
    ) -> None:
        decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
        report["steps"].append(
            {
                "step": len(report["steps"]) + 1,
                "tool": tool,
                "screen": screen,
                "action": decision.get("action"),
                "reason": reason,
                "screenshot_path": str(image_path),
                "decision": decision,
                "result": result,
            }
        )

    def _base_report(
        self,
        *,
        mode: str,
        max_turns: int,
        min_decision_confidence: float,
        screenshot_path: str | Path | None,
        sidecar_path: str | Path | None,
    ) -> dict[str, Any]:
        return {
            "is_valid": True,
            "status": "running",
            "run_dir": str(self.run_dir),
            "mode": mode,
            "turns_requested": int(max_turns),
            "turns_completed": 0,
            "min_decision_confidence": float(min_decision_confidence),
            "screenshot_path": str(screenshot_path) if screenshot_path is not None else None,
            "sidecar_path": str(sidecar_path) if sidecar_path is not None else None,
            "steps": [],
            "stop_reason": None,
            "next_recommendation": None,
        }

    def _finish(
        self,
        report: dict[str, Any],
        *,
        status: str,
        stop_reason: str,
        next_recommendation: str | None = None,
    ) -> dict[str, Any]:
        report["status"] = status
        report["stop_reason"] = stop_reason
        if next_recommendation is not None:
            report["next_recommendation"] = next_recommendation
        elif report.get("next_recommendation") is None:
            report["next_recommendation"] = self._recommendation_for_stop(stop_reason)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "autonomous_run_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report

    @staticmethod
    def _recommendation_for_stop(reason: str) -> str:
        if reason in {"turn_completed"}:
            return "rerun_next_turn"
        if reason == "skill_acquisition_handoff":
            return "run_skill_acquisition_handoff"
        if reason == "final_phase_turn_completed":
            return "rerun_final_phase_until_ending"
        if reason == "final_phase_action_planned":
            return "review_planned_final_phase_action"
        if reason in {"dry_run", "execute", "confirm_token"}:
            return "review_planned_action_before_live_execute"
        if "confidence" in reason:
            return "manual_review_training_decision"
        if "choose_not_allowed" in reason:
            return "rerun_with_allow_choose_event_if_intended"
        if "not_allowed" in reason or "confirm_not_allowed" in reason:
            return "rerun_with_explicit_allow_flag_if_intended"
        return "manual_review_required"

    @staticmethod
    def _visible_text_is_final_phase(visible_text: Any) -> bool:
        text = str(visible_text or "")
        return any(token in text for token in ("还剩", "结束", "荣耀女神杯"))

    @staticmethod
    def _training_menu_precheck_accepts(screenshot_path: Any) -> bool:
        if screenshot_path is None:
            return False
        try:
            from core.training_menu_precheck import TrainingMenuPrechecker

            return bool(TrainingMenuPrechecker().check(screenshot_path).accepted)
        except Exception:
            return False
