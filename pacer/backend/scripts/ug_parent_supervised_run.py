from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import ADB_ENDPOINT, ADB_PORT  # noqa: E402

_EVENT_KNOWLEDGE_SCHEMA_SHA256 = (
    "d486d5226f0e2cde6c85fe3530266fdec68ad250350baceba45a821b1172be22"
)
_EVENT_KNOWLEDGE_DATASET_SHA256 = (
    "dba68c9b45fa0dd45c64b76a479849554c08bc3b5b09baa5fcc89bb53b075371"
)
_EVENT_KNOWLEDGE_REGISTRY_SHA256 = (
    "078ef3893ce64d97053b39a587540dd6b0cc971458c60be0fdf6a96f8c43d83f"
)

from scripts.ug_parent_event_choice_route import main as event_choice_main
from scripts.ug_parent_event_choice_route import parse_args as parse_event_choice_args
from scripts.ug_parent_event_choice_route import run as run_event_choice
from scripts.ug_parent_condition_probe import main as condition_probe_main
from scripts.ug_parent_one_turn_pipeline import main as one_turn_main
from scripts.ug_parent_race_route import main as race_route_main
from scripts import ug_parent_race_route as count_race_cli
from scripts.ug_parent_safe_advance import main as safe_advance_main
from scripts.ug_parent_safe_advance import parse_args as parse_safe_advance_args
from scripts.ug_parent_safe_advance import run as run_safe_advance
from scripts.ug_parent_speed_toggle import main as speed_toggle_main
from core.auto_trainer.condition_context import (
    attach_formal_condition_binding,
    load_condition_assessment,
    load_explicit_condition_context,
)
from core.auto_trainer.condition_authority import ConditionAuthorityCache
from core.auto_trainer.route_handoff import is_route_handoff, is_route_handoff_result
from core.auto_trainer.condition_gate import (
    ConditionProbePolicy,
    apply_event_chain_risk,
    build_condition_quick_snapshot,
    decide_condition_probe,
)
from core.auto_trainer.condition_tracker import ConditionProbeReason, ConditionStatus, ConditionTracker
from core.auto_trainer.condition_tracker_store import ConditionTrackerStore
from core.auto_trainer.decision import numeric_blind_terminal_state_errors
from core.auto_trainer.dashboard_runner import CooperativeRunnerDriver
from core.auto_trainer.dashboard_v2 import record_dashboard_turn
from core.auto_trainer.next_run_truth_capture_supervisor import (
    archive_supervised_turn_truth_capture,
)
from core.auto_trainer.post_action_truth_capture import (
    archive_post_action_truth_capture,
)
from core.auto_trainer.career_supervisor_event_identity_shadow import (
    EventIdentityShadowReport,
)
from core.auto_trainer.career_supervisor_event_identity_perception import (
    EventIdentityPerceptionRequest,
    produce_event_identity_shadow_report,
)
from core.auto_trainer.career_supervisor_event_local_runtime import (
    build_event_local_decision_runtime_shadow,
)
from core.auto_trainer.career_supervisor_event_context import (
    EventOwnerContextAuthority,
    EventOwnerContextError,
    build_trainee_event_owner_context_from_prior,
)
from core.auto_trainer.career_supervisor_prior_event_context import (
    PriorTurnEventContextAuthority,
    PriorTurnEventContextError,
    build_prior_turn_event_context_authority,
)
from core.auto_trainer.career_supervisor_turn import (
    TurnAuthorityBundle,
    build_turn_authority_bundle,
)
from core.auto_trainer.run_trainee_identity_authority import (
    RunTraineeIdentityAuthority,
    RunTraineeIdentityError,
)
from core.auto_trainer.deepseek_two_run_trial import (
    DeepSeekTwoRunTrialError,
    validate_deepseek_two_run_trial,
)
from core.auto_trainer.career_goal_ledger import (
    CareerGoalLedger,
    CareerGoalLedgerError,
    CareerGoalLedgerStore,
    build_count_goal_main_menu_completion_receipt,
)
from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.orchestrator import UGParentAutonomousOrchestrator
from core.auto_trainer.race_calendar import RaceCalendar
from core.auto_trainer.race_plan import RacePlan
from core.auto_trainer.race_scheduler import (
    OpportunityConstrainedRaceScheduler,
    RaceScheduleDecision,
    RaceSchedulerError,
    race_schedule_decision_to_dict,
    build_count_race_reservation,
)
from core.auto_trainer.scheduled_race_progress import (
    ScheduledRaceProgressError,
    inspect_scheduled_race_progress,
)
from core.auto_trainer.terminal_skill_handoff import (
    TerminalSkillHandoffError,
    inspect_terminal_skill_handoff,
    load_terminal_skill_handoff_artifact,
)
from core.auto_trainer.terminal_skill_lifecycle import (
    TerminalSkillLifecycleError,
    load_terminal_skill_lifecycle,
)
from core.auto_trainer.route_transition_session import (
    POST_ACTION_ROUTE_SUCCESSORS,
    RouteTransitionError,
    RouteTransitionSession,
    RouteTransitionStatus,
    record_external_action_receipt,
)
from core.auto_trainer.external_terminal_skill_handoff import (
    ExternalTerminalSkillHandoffError,
    load_external_terminal_skill_handoff,
)
from core.auto_trainer.state import TRAINING_TYPES, TrainingTurnState
from core.auto_trainer.progressive_route_action_port import DryRunProgressiveActionPort
from core.auto_trainer.progressive_route_graph import ProgressiveRouteGraph
from core.auto_trainer.progressive_route_packet import load_transition_packet
from core.auto_trainer.progressive_route_recognizer import ScopedRouteRecognizer
from core.auto_trainer.progressive_route_runner import (
    ProgressiveRouteRunner,
    TransitionStore,
)
from core.auto_trainer.progressive_route_runtime import (
    CapturedRouteFrame,
    ProgressiveRouteRuntime,
)
from core.auto_trainer.progressive_route_session import ProgressiveRouteSession
from core.auto_trainer.screen_graph import ScreenGraph
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state


@dataclass(frozen=True)
class _CareerRaceRuntime:
    run_dir: Path
    orchestrator: UGParentAutonomousOrchestrator
    ledger_path: Path


    def requires_main_menu_schedule(self, *, state: TrainingTurnState | None = None) -> bool:
        return self.orchestrator.requires_main_menu_schedule(career_state=state)

    def bind_pending_goal(
        self, *, schedule_state: tuple[TrainingTurnState, str | None] | None,
        provider_source: dict[str, Any] | None, turn_index: int,
        recover_from_main_menu: bool = False,
    ) -> str | None:
        store = self.orchestrator.goal_ledger_store
        ledger = store.load(self.ledger_path, expected_run_session_id=self.run_dir.name)
        count_goals = [goal for goal in ledger.active_goals()
                       if goal.goal_kind == "graded_race_rank_count" and goal.progress_basis == "verified_lower_bound"]
        pending = bool(ledger.pending_named_goals())
        if not count_goals and not pending and not recover_from_main_menu:
            return
        unbound_reason = "career_goal_count_calendar_unbound" if count_goals else "career_goal_occurrence_unbound"
        if schedule_state is None or provider_source is None:
            raise CareerGoalLedgerError(unbound_reason)
        state, source_sha = schedule_state
        if (state.turn != turn_index or source_sha is None
            or provider_source.get("sha256") != source_sha
            or state.scenario_state.get("source_frame_sha256") != source_sha):
            raise CareerGoalLedgerError(unbound_reason)
        anchor = state.scenario_state.get("career_calendar_anchor")
        marker = anchor.get("goal_achieved_marker") if isinstance(anchor, dict) else None
        if count_goals and isinstance(marker, dict) and marker.get("present") is True:
            receipt = build_count_goal_main_menu_completion_receipt(
                ledger=ledger,
                anchor=anchor,
                run_session_id=self.run_dir.name,
                observed_turn=turn_index,
                source_path=provider_source.get("path"),
                source_frame_sha256=source_sha,
            )
            merged = store.record_count_goal_main_menu_completion(ledger, receipt)
            if merged.status == "updated":
                store.write_atomic(self.ledger_path, merged.ledger)
            loaded = store.load(self.ledger_path, expected_run_session_id=self.run_dir.name)
            if loaded.current_view_sha256 != merged.ledger.current_view_sha256:
                raise CareerGoalLedgerError("count_goal_main_menu_completion_readback_failed")
            ledger = loaded
            count_goals = [goal for goal in ledger.active_goals()
                           if goal.goal_kind == "graded_race_rank_count"
                           and goal.progress_basis == "verified_lower_bound"]
            if not count_goals:
                return
        if count_goals:
            merged = store.bind_count_goal_deadline(ledger,
                anchor=anchor,
                observed_turn=turn_index, source_frame_sha256=source_sha,
                expected_run_session_id=self.run_dir.name)
            if merged.status == "updated":
                store.write_atomic(self.ledger_path, merged.ledger)
            loaded = store.load(self.ledger_path, expected_run_session_id=self.run_dir.name)
            if (loaded.pending_count_goals() or loaded.current_view_sha256 != merged.ledger.current_view_sha256):
                raise CareerGoalLedgerError("career_goal_binding_readback_failed")
            return "count_calendar_bound_reservation_pending"
        merge = store.bind_named_goal_occurrence if pending else store.recover_named_goal_from_main_menu
        merged = merge(
            ledger, anchor=state.scenario_state.get("career_calendar_anchor"),
            observed_turn=turn_index, source_frame_sha256=source_sha,
            expected_run_session_id=self.run_dir.name, race_calendar=self.orchestrator.race_calendar,
        )
        if merged.status == "updated":
            store.write_atomic(self.ledger_path, merged.ledger)
        loaded = store.load(self.ledger_path, expected_run_session_id=self.run_dir.name)
        if (loaded.pending_named_goals()
            or loaded.current_view_sha256 != merged.ledger.current_view_sha256):
            raise CareerGoalLedgerError("career_goal_binding_readback_failed")

    def plan_and_persist(
        self,
        *,
        state: TrainingTurnState,
        source_frame_sha256: str,
        turn_index: int,
    ) -> tuple[RaceScheduleDecision, Path]:
        store = self.orchestrator.goal_ledger_store
        if store is None:
            raise RaceSchedulerError("race_scheduler_dependencies_incomplete")
        ledger = store.load(
            self.ledger_path,
            expected_run_session_id=self.run_dir.name,
        )
        decision = self.orchestrator.plan_ready_main_menu(
            career_state=state,
            source_frame_sha256=source_frame_sha256,
        )
        destination = (
            self.run_dir
            / "race_schedule_decisions"
            / f"turn_{turn_index:03d}.json"
        )
        _write_canonical_json_atomic(
            destination,
            {
                "schema_version": "race_schedule_decision.v1",
                "run_session_id": self.run_dir.name,
                "turn_index": turn_index,
                "source_frame_sha256": source_frame_sha256,
                "ledger_path": str(self.ledger_path),
                "ledger_revision": ledger.revision,
                "ledger_view_sha256": ledger.current_view_sha256,
                "decision": race_schedule_decision_to_dict(decision),
            },
        )
        return decision, destination


@dataclass(frozen=True)
class EventIdentityShadowArtifact:
    """A same-turn artifact envelope for a non-executable event shadow report."""

    run_session_id: str
    generation: int
    global_action_turn: int
    source_frame_sha256: str
    event_page_sha256: str
    artifact_path: Path
    artifact_sha256: str
    shadow: EventIdentityShadowReport


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Supervised UG parent training runner. Formal live execution currently uses "
            "only the deterministic local policy; static and command advisors are "
            "dry-run-only."
        )
    )
    parser.add_argument("--goal-config", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--run-session-id", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--consumed-terminal-handoff")
    parser.add_argument("--expected-consumed-terminal-handoff-sha256")
    parser.add_argument("--terminal-skill-lifecycle-root")
    parser.add_argument("--expected-terminal-skill-lifecycle-root-sha256")
    parser.add_argument("--external-terminal-skill-handoff")
    parser.add_argument("--expected-external-terminal-skill-handoff-sha256")
    parser.add_argument("--allow-career-end-finish-after-skill-handoff", action="store_true")
    parser.add_argument("--dashboard-run-dir")
    parser.add_argument(
        "--career-goal-ledger-json",
        help=(
            "Persistent run-bound career goal ledger. Defaults to the current "
            "output root; set this when rotating evidence output directories."
        ),
    )
    parser.add_argument(
        "--skip-session-json",
        help="Persistent run-bound skip state used when rotating evidence output directories.",
    )
    parser.add_argument(
        "--route-transition-json",
        help="Persistent run-bound route transition state used when rotating evidence output directories.",
    )
    parser.add_argument("--dashboard-display-title", default="当前大师杯育成")
    parser.add_argument("--dashboard-display-subtitle", default="进行中")
    parser.add_argument(
        "--race-calendar-json",
        default=str(ROOT / "data" / "race_calendar" / "cn_standard_v1.json"),
    )
    parser.add_argument("--race-plan-json")
    parser.add_argument("--recover-career-goal-from-main-menu", action="store_true",
                        help="Recover the current named goal only from this run's fresh main-menu provider.")
    state_context = parser.add_mutually_exclusive_group(required=True)
    state_context.add_argument("--state-context-json")
    state_context.add_argument(
        "--state-context-pattern",
        help="Per-turn context path pattern, for example state_context_turn{turn_index}.json.",
    )
    state_context.add_argument("--state-context-provider", choices=["main-menu-png"])
    parser.add_argument("--start-turn-index", type=int, default=1)
    parser.add_argument("--max-training-turns", type=int, required=True)
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument(
        "--route-engine",
        choices=["legacy", "progressive"],
        default="legacy",
    )
    parser.add_argument("--progressive-run-id")
    parser.add_argument("--progressive-session")
    parser.add_argument("--progressive-transition-packet")
    parser.add_argument("--progressive-frame-trace")
    parser.add_argument("--progressive-frame-trace-sha256")
    parser.add_argument("--progressive-max-directives", type=int, default=2)
    parser.add_argument(
        "--progressive-recovery",
        choices=["disabled"],
        default="disabled",
    )
    parser.add_argument(
        "--start-screen",
        choices=["main-menu", "training-menu"],
        default="training-menu",
    )
    parser.add_argument("--adb-port", type=int)
    parser.add_argument("--capture-live", action="store_true")
    parser.add_argument("--capture-execute", action="store_true")
    parser.add_argument("--capture-confirm-token")
    parser.add_argument("--collect-larc-ss-information", action="store_true")
    parser.add_argument(
        "--enable-explicit-condition-probe",
        action="store_true",
        help="Formal-live only: require a verified condition-details probe before each turn.",
    )
    parser.add_argument(
        "--condition-probe-policy",
        choices=[policy.value for policy in ConditionProbePolicy],
    )
    parser.add_argument("--condition-audit-interval", type=int, default=6)
    parser.add_argument("--condition-probe-confirm-token")
    parser.add_argument(
        "--condition-triggered-recovery",
        action="store_true",
        help=(
            "One action only: re-probe when triggered authority cache is missing; "
            "disabled by default."
        ),
    )
    parser.add_argument("--execute-training-confirm", action="store_true")
    parser.add_argument("--training-confirm-token")
    parser.add_argument(
        "--ai-review-json",
        help="Static advisor JSON for dry-run only; live mode rejects it.",
    )
    parser.add_argument(
        "--ai-review-command",
        help=(
            "Dry-run-only protocol/replay advisor command; live mode rejects it until "
            "a sandboxed provider is available."
        ),
    )
    parser.add_argument("--ai-review-command-timeout-seconds", type=int, default=60)
    parser.add_argument(
        "--structured-advisor-mode",
        choices=["off", "observer", "advisor"],
        default="off",
    )
    parser.add_argument(
        "--structured-advisor-command-arg",
        action="append",
        default=[],
    )
    parser.add_argument("--structured-advisor-model-id")
    parser.add_argument(
        "--structured-advisor-invalid-policy",
        choices=["local_fallback", "stop"],
        default="local_fallback",
    )
    parser.add_argument(
        "--structured-advisor-timeout-seconds",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--structured-advisor-ttl-ms",
        type=int,
        default=30_000,
    )
    parser.add_argument("--deepseek-two-run-trial-contract")
    parser.add_argument(
        "--deepseek-two-run-trial-role",
        choices=["observer", "advisor"],
    )
    parser.add_argument("--min-live-sidecar-confidence", type=float, default=0.65)
    parser.add_argument("--min-decision-confidence", type=float, default=0.0)
    parser.add_argument("--allow-partial-live-sidecar", action="store_true")
    parser.add_argument(
        "--numeric-blind-fallback",
        action="store_true",
        help="Use the local numeric-blind fallback policy for this process run.",
    )
    parser.add_argument("--advance-between-turns", action="store_true")
    parser.add_argument("--advance-after-final-action", action="store_true")
    parser.add_argument("--advance-execute", action="store_true")
    parser.add_argument("--advance-confirm-token")
    parser.add_argument("--advance-post-wait-freeze-poll-seconds", type=float)
    parser.add_argument("--advance-post-wait-freeze-timeout-seconds", type=float)
    parser.add_argument("--advance-post-wait-freeze-stable-frames", type=int)
    parser.add_argument("--advance-post-wait-freeze-max-frame-diff", type=float)
    parser.add_argument("--allow-terminal-skill-entry", action="store_true")
    parser.add_argument("--terminal-skill-entry-settle-seconds", type=float, default=1.0)
    parser.add_argument("--allow-team-formation-confirm", action="store_true")
    parser.add_argument("--allow-return-main-menu-from-training", action="store_true")
    parser.add_argument("--allow-startup-promo-skip", action="store_true")
    parser.add_argument(
        "--allow-unbound-goal-summary-continue",
        action="store_true",
        help=(
            "Allow one revalidated, exact-action goal-summary continue when "
            "structured next-goal binding abstains."
        ),
    )
    parser.add_argument("--advance-max-steps", type=int, default=12)
    parser.add_argument("--race-route-between-turns", action="store_true")
    parser.add_argument("--race-route-execute", action="store_true")
    parser.add_argument("--race-route-confirm-token")
    parser.add_argument("--race-route-max-steps", type=int, default=30)
    parser.add_argument("--inspect-current-count-races", action="store_true",
                        help="Inspect the current bound-count race list and return; never train or enter a race.")
    parser.add_argument("--inspection-resume-report")
    parser.add_argument("--inspection-resume-report-sha256")
    parser.add_argument("--count-race-previous-training-report")
    parser.add_argument("--count-race-previous-invocation")
    parser.add_argument("--count-race-post-training-route-report")
    parser.add_argument("--count-race-zero-action-report")
    parser.add_argument("--count-race-inspection-report")
    parser.add_argument("--count-race-history-json", help="Original prior count result and return-to-main evidence paths.")
    parser.add_argument("--count-race-resume-report")
    parser.add_argument("--count-race-resume-report-sha256")
    parser.add_argument("--count-race-restart-evidence")
    parser.add_argument("--count-race-restart-evidence-sha256")
    parser.add_argument("--race-scene-wait-limit", type=int, default=5)
    parser.add_argument("--allow-enter-race", action="store_true")
    parser.add_argument("--allow-enter-race-from-main-menu", action="store_true")
    parser.add_argument("--allow-fan-goal-recommended-race", action="store_true")
    parser.add_argument("--allow-select-visible-g1", action="store_true")
    parser.add_argument("--allow-scan-race-list-for-g1", action="store_true")
    parser.add_argument("--allow-leave-race-list", action="store_true")
    parser.add_argument("--allow-leave-race-detail", action="store_true")
    parser.add_argument("--allow-confirm-race", action="store_true")
    parser.add_argument("--allow-quick-result", action="store_true")
    parser.add_argument("--allow-skip-race-scene", action="store_true")
    parser.add_argument("--allow-result-continue", action="store_true")
    parser.add_argument("--allow-locked-race-entry", action="store_true")
    parser.add_argument(
        "--allow-grand-masters-final-race-confirm",
        action="store_true",
    )
    parser.add_argument("--event-choice-between-turns", action="store_true")
    parser.add_argument("--event-choice-execute", action="store_true")
    parser.add_argument("--event-choice-confirm-token")
    parser.add_argument("--event-choice-max-steps", type=int, default=1)
    parser.add_argument("--event-formal-generation", type=int, default=1)
    parser.add_argument("--run-trainee-authority-json")
    parser.add_argument("--allow-choose-event", action="store_true")
    parser.add_argument("--event-choice-selection", choices=["top", "bottom"], default="top")
    parser.add_argument("--speed-toggle-between-turns", action="store_true")
    parser.add_argument("--speed-toggle-execute", action="store_true")
    parser.add_argument("--speed-toggle-confirm-token")
    parser.add_argument("--route-max-cycles", type=int, default=12)
    parser.add_argument("--route-before-first-turn", action="store_true")
    parser.add_argument("--verify-ready-after-advance", action="store_true")
    parser.add_argument("--ready-recheck-delay-seconds", type=float, default=0.0)
    parser.add_argument("--post-action-skip-taps", type=int, default=0)
    parser.add_argument("--settle-seconds", type=float, default=0.9)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def _career_goal_ledger_path(args: argparse.Namespace) -> Path:
    configured = getattr(args, "career_goal_ledger_json", None)
    if configured:
        return Path(configured)
    return Path(args.output_root) / args.run_session_id / "career_goal_ledger.json"


def _skip_session_path(args: argparse.Namespace) -> Path:
    configured = getattr(args, "skip_session_json", None)
    if configured:
        return Path(configured)
    return Path(args.output_root) / f"{args.run_session_id}.skip_session.json"


def _route_transition_path(args: argparse.Namespace) -> Path:
    configured = getattr(args, "route_transition_json", None)
    if configured:
        return Path(configured)
    return Path(args.output_root) / f"{args.run_session_id}.route_transition.json"


def _prepare_career_race_runtime(
    args: argparse.Namespace,
) -> _CareerRaceRuntime:
    calendar = RaceCalendar.load(args.race_calendar_json)
    race_plan = (
        RacePlan.load(args.race_plan_json)
        if args.race_plan_json
        else RacePlan.from_dict(
            {
                "schema_version": "race_plan.v1",
                "plan_id": "read_only_default",
                "reserve_opportunities": 1,
                "allow_goal_failure": False,
                "directives": [],
            }
        )
    )
    run_dir = Path(args.output_root) / args.run_session_id
    ledger_path = _career_goal_ledger_path(args)
    store = CareerGoalLedgerStore()
    if ledger_path.exists():
        store.load(
            ledger_path,
            expected_run_session_id=args.run_session_id,
        )
    else:
        store.write_atomic(
            ledger_path,
            CareerGoalLedger.empty(args.run_session_id),
        )
    _write_canonical_json_atomic(
        run_dir / "race_calendar_manifest.json",
        {
            "schema_version": "race_calendar_manifest.v1",
            "run_session_id": args.run_session_id,
            "calendar_id": calendar.calendar_id,
            "calendar_sha256": calendar.calendar_sha256,
            "apk_database_sha256": calendar.apk_database_sha256,
            "entry_count": len(calendar.entries),
        },
    )
    _write_canonical_json_atomic(
        run_dir / "race_plan_snapshot.json",
        {
            "schema_version": race_plan.schema_version,
            "run_session_id": args.run_session_id,
            "plan_id": race_plan.plan_id,
            "reserve_opportunities": race_plan.reserve_opportunities,
            "allow_goal_failure": race_plan.allow_goal_failure,
            "directives": [
                {
                    "race_id": item.race_id,
                    "directive": item.directive,
                    "priority": item.priority,
                    "source": item.source,
                    "enabled": item.enabled,
                    "outcome_requirement": (
                        dict(item.outcome_requirement)
                        if item.outcome_requirement is not None
                        else None
                    ),
                }
                for item in race_plan.directives
            ],
        },
    )
    orchestrator = UGParentAutonomousOrchestrator(
        goal=UGParentGoal.default(),
        run_dir=run_dir,
        routes=SimpleNamespace(),
        race_calendar=calendar,
        race_plan=race_plan,
        goal_ledger_store=store,
        race_scheduler=OpportunityConstrainedRaceScheduler(),
        career_goal_ledger_path=ledger_path,
    )
    return _CareerRaceRuntime(
        run_dir=run_dir,
        orchestrator=orchestrator,
        ledger_path=ledger_path,
    )


def _write_canonical_json_atomic(path: Path, payload: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise OSError("career_race_artifact_conflict")
        return
    temporary = destination.with_name(
        f".{destination.name}.{secrets.token_hex(8)}.tmp"
    )
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
        if destination.read_bytes() != encoded:
            raise OSError("career_race_artifact_atomic_verify_failed")
    finally:
        temporary.unlink(missing_ok=True)


class _NumericBlindProviderRefresh(ValueError):
    pass


def _race_schedule_state_for_turn(
    *,
    turn_index: int,
    turn_root: Path,
    context_path: Path | None,
    provider_source: dict[str, Any] | None,
    race_calendar: RaceCalendar,
    allow_observation_refresh: bool = False,
) -> tuple[TrainingTurnState, str | None] | None:
    if provider_source is not None:
        source_path = provider_source.get("path")
        source_sha256 = provider_source.get("sha256")
        if (
            type(source_path) is not str
            or type(source_sha256) is not str
            or hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
            != source_sha256
        ):
            raise ValueError("race_schedule_provider_source_invalid")
        provider_report = provide_main_menu_state(
            source_path,
            turn_root / "race_schedule_state_context.json",
            turn_index=turn_index,
            consensus_png_paths=provider_source.get("consensus_paths", ()),
            race_calendar=race_calendar,
        )
        raw_state = provider_report.get("state_context")
        if not isinstance(raw_state, dict):
            analyzer = provider_report.get("analyzer") or {}
            errors = analyzer.get("errors")
            if (allow_observation_refresh and provider_report.get("valid_for_training") is False
                    and isinstance(errors, list) and all(isinstance(error, str) for error in errors)
                    and not numeric_blind_terminal_state_errors(errors)):
                raise _NumericBlindProviderRefresh("race_schedule_provider_observation_unreadable")
            raise ValueError("race_schedule_provider_state_invalid")
    elif context_path is not None:
        payload = json.loads(context_path.read_text(encoding="utf-8-sig"))
        raw_state = payload.get("state_context") if isinstance(payload, dict) else None
        if not isinstance(raw_state, dict):
            raw_state = payload
        if not isinstance(raw_state, dict):
            raise ValueError("race_schedule_state_context_invalid")
        scenario = raw_state.get("scenario_state")
        source_sha256 = (
            scenario.get("source_frame_sha256")
            if isinstance(scenario, dict)
            else None
        )
        if (
            type(source_sha256) is not str
            or len(source_sha256) != 64
            or any(char not in "0123456789abcdef" for char in source_sha256)
        ):
            # Preserve the state for mandatory/count rejection; missing source
            # evidence must not erase scheduling demand and fall into training.
            source_sha256 = None
    else:
        return None
    normalized = dict(raw_state)
    normalized["screen"] = "ready_main_menu"
    scenario_state = normalized.get("scenario_state")
    scenario_state = (
        dict(scenario_state)
        if isinstance(scenario_state, dict)
        else {}
    )
    scenario_state["source_frame_sha256"] = source_sha256
    if provider_source is None:
        # Manual state context is not a fresh route capture, even if it copies
        # a previously verified header anchor.
        scenario_state.pop("career_calendar_anchor", None)
    normalized["scenario_state"] = scenario_state
    return TrainingTurnState.from_dict(normalized), source_sha256


def _race_schedule_before_training(
    args, *, turn_index, turn_root, context_path, provider_source, seen_sources,
    race_calendar, control_driver=None,
):
    """Refresh the bound main-menu observation before scheduling or collecting tabs."""
    state_kwargs = dict(turn_index=turn_index, turn_root=turn_root, context_path=context_path,
                        race_calendar=race_calendar)
    allow_refresh = args.mode == "live" and getattr(args, "numeric_blind_fallback", False)
    try:
        state = _race_schedule_state_for_turn(
            provider_source=provider_source, allow_observation_refresh=allow_refresh, **state_kwargs,
        )
    except _NumericBlindProviderRefresh:
        source = _capture_condition_resync_source(
            args, provider_source, turn_root / "numeric_blind_provider_refresh", control_driver=control_driver,
        )
        original = provider_source["path"], provider_source["sha256"]
        if any(path == source["path"] or sha == source["sha256"]
               for path, sha in seen_sources if (path, sha) != original):
            raise ValueError("numeric_blind_provider_refresh_source_reused")
        state = _race_schedule_state_for_turn(provider_source=source, **state_kwargs)
        seen_sources.add((source["path"], source["sha256"]))
        return state, source
    return state, provider_source


def _fan_shortfall_handoff_due(
    schedule_state: tuple[TrainingTurnState, str | None] | None,
) -> bool:
    if schedule_state is None:
        return False
    state, source_sha256 = schedule_state
    anchor = state.scenario_state.get("career_calendar_anchor")
    if (
        not isinstance(anchor, dict)
        or anchor.get("status") != "verified"
        or anchor.get("source_frame_sha256") != source_sha256
        or anchor.get("goal_kind") != "fan_count"
        or type(anchor.get("required_fans")) is not int
        or anchor["required_fans"] <= 0
        or type(anchor.get("remaining_turns")) is not int
        or not 1 <= anchor["remaining_turns"] <= 6
    ):
        return False
    marker = anchor.get("goal_achieved_marker")
    if not isinstance(marker, dict) or marker.get("source_frame_sha256") != source_sha256:
        return False
    if marker.get("verified") is True and marker.get("present") is True:
        return False
    if (
        marker.get("verified") is True
        and marker.get("present") is False
        and marker.get("reason") == "fan_shortfall_verified"
        and type(marker.get("remaining_fans")) is int
        and marker["remaining_fans"] > 0
    ):
        return True
    color = marker.get("color_evidence")
    return bool(
        marker.get("verified") is False
        and marker.get("present") is False
        and marker.get("reason") == "goal_marker_orange_unverified"
        and isinstance(color, dict)
        and type(color.get("orange_pixels")) is int
        and color["orange_pixels"] < 100
        and isinstance(color.get("orange_ratio"), (int, float))
        and not isinstance(color.get("orange_ratio"), bool)
        and float(color["orange_ratio"]) < 0.05
    )


def _fan_shortfall_entry_progress(
    race_report: dict[str, Any], *, allowed_grades: tuple[str, ...] = ("G2", "G3"),
) -> dict[str, Any]:
    progress = {
        "schema_version": "uma.fan_shortfall_race_progress.v1",
        "entry_consumed": False,
        "confirmation_step": None,
        "reason": "fan_shortfall_confirmation_missing",
    }
    steps = race_report.get("step_reports") if isinstance(race_report, dict) else None
    if not isinstance(steps, list):
        return progress
    valid: list[int] = []
    saw_candidate = False
    for row in steps:
        if not isinstance(row, dict) or not isinstance(row.get("result"), dict):
            continue
        step = row.get("step")
        result = row["result"]
        decision = result.get("decision")
        if not isinstance(decision, dict) or decision.get(
            "consume_fan_goal_shortfall_selected_verification"
        ) is not True:
            continue
        saw_candidate = True
        evidence = decision.get("evidence")
        proof = (
            evidence.get("fan_goal_shortfall_selected_verification")
            if isinstance(evidence, dict)
            else None
        )
        tap = decision.get("tap")
        safety = result.get("safety_verdict")
        stars = (
            proof.get("gold_star_surface_distance_evidence")
            if isinstance(proof, dict)
            else None
        )
        if (
            type(step) is int
            and step > 0
            and result.get("performed") is True
            and result.get("tap_performed") is True
            and result.get("reason") == "confirm_fan_goal_shortfall_selected_race"
            and decision.get("action") == "tap"
            and decision.get("reason") == result["reason"]
            and isinstance(tap, dict)
            and tap.get("coordinate_name")
            == "race_confirm.fan_goal_shortfall_selected"
            and isinstance(safety, dict)
            and safety.get("allowed") is True
            and safety.get("action_id")
            == "race_confirm.fan_goal_shortfall_selected"
            and isinstance(proof, dict)
            and proof.get("source") == "race_list.fan_goal_shortfall_selected_card"
            and proof.get("action_id")
            == "race_list.fan_goal_shortfall_selected_entry"
            and proof.get("single_use") is True
            and proof.get("confirmation_source")
            == "race_detail_confirm.foreground_positioned_ocr"
            and proof.get("grade") in allowed_grades
            and type(proof.get("fan_shortfall")) is int
            and proof["fan_shortfall"] > 0
            and type(proof.get("fan_reward")) is int
            and proof["fan_reward"] >= proof["fan_shortfall"]
            and isinstance(stars, list)
            and len(stars) == 2
            and stars[0] == "草地"
            and stars[1] in {"短距离", "英里", "中距离", "长距离"}
        ):
            valid.append(step)
    if len(valid) == 1:
        progress.update(
            entry_consumed=True,
            confirmation_step=valid[0],
            reason="fan_shortfall_entry_consumed",
        )
    elif len(valid) > 1:
        progress["reason"] = "fan_shortfall_confirmation_ambiguous"
    elif saw_candidate:
        progress["reason"] = "fan_shortfall_confirmation_unverified"
    return progress


def _fan_shortfall_route_authorized(args: argparse.Namespace) -> bool:
    return bool(
        args.race_route_between_turns
        and args.allow_enter_race
        and args.allow_enter_race_from_main_menu
        and args.allow_fan_goal_recommended_race
        and args.allow_confirm_race
    )


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


def _reject_existing_reparse_points(path: Path) -> None:
    raw = Path(path).absolute()
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current = current / part
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ValueError("terminal_skill_resume_path_inspection_failed") from exc
        if _is_reparse_point(value):
            raise ValueError("terminal_skill_resume_reparse_point_rejected")


def _load_consumed_terminal_skill_binding(
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    external_values = (
        args.external_terminal_skill_handoff,
        args.expected_external_terminal_skill_handoff_sha256,
    )
    values = (
        args.consumed_terminal_handoff,
        args.expected_consumed_terminal_handoff_sha256,
        args.terminal_skill_lifecycle_root,
        args.expected_terminal_skill_lifecycle_root_sha256,
    )
    if not any(value is not None for value in values + external_values):
        return None
    if any(value is not None for value in external_values):
        if (
            not all(
                isinstance(value, str) and value
                for value in (
                    args.consumed_terminal_handoff,
                    args.expected_consumed_terminal_handoff_sha256,
                    *external_values,
                )
            )
            or any(
                value is not None
                for value in (
                    args.terminal_skill_lifecycle_root,
                    args.expected_terminal_skill_lifecycle_root_sha256,
                )
            )
        ):
            raise ValueError("external_terminal_skill_resume_binding_incomplete")
        return load_external_terminal_skill_handoff(
            Path(args.external_terminal_skill_handoff),
            expected_sidecar_sha256=(
                args.expected_external_terminal_skill_handoff_sha256
            ),
            allowed_output_root=Path(args.output_root),
            expected_run_session_id=args.run_session_id,
            consumed_terminal_handoff_path=Path(args.consumed_terminal_handoff),
            expected_consumed_terminal_handoff_sha256=(
                args.expected_consumed_terminal_handoff_sha256
            ),
        ).to_dict()
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("terminal_skill_resume_binding_incomplete")
    raw_output_root = Path(args.output_root)
    _reject_existing_reparse_points(raw_output_root)
    output_root = raw_output_root.resolve(strict=True)

    def inside_output(value: str, *, code: str) -> Path:
        raw_path = Path(value)
        _reject_existing_reparse_points(raw_path)
        path = raw_path.resolve(strict=True)
        try:
            path.relative_to(output_root)
        except ValueError as exc:
            raise ValueError(code) from exc
        if not path.is_file():
            raise ValueError(code)
        return path

    handoff_path = inside_output(
        args.consumed_terminal_handoff,
        code="consumed_terminal_handoff_outside_output_root",
    )
    lifecycle_root = inside_output(
        args.terminal_skill_lifecycle_root,
        code="terminal_skill_lifecycle_root_outside_output_root",
    )
    for expected_sha256, path, code in (
        (
            args.expected_consumed_terminal_handoff_sha256,
            handoff_path,
            "consumed_terminal_handoff_sha_mismatch",
        ),
        (
            args.expected_terminal_skill_lifecycle_root_sha256,
            lifecycle_root,
            "terminal_skill_lifecycle_root_sha_mismatch",
        ),
    ):
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
        ):
            raise ValueError(code)
    if lifecycle_root.name != "terminal_skill_lifecycle.json":
        raise ValueError("terminal_skill_lifecycle_root_not_canonical")
    load_terminal_skill_handoff_artifact(
        handoff_path,
        expected_artifact_sha256=args.expected_consumed_terminal_handoff_sha256,
        allowed_root=output_root,
    )
    lifecycle = load_terminal_skill_lifecycle(
        allowed_run_root=output_root,
        lifecycle_dir=lifecycle_root.parent,
        run_session_id=args.run_session_id,
    )
    terminal_receipt = lifecycle_root.parent / "receipt-007-return_after_purchase.json"
    if not terminal_receipt.is_file():
        # The fully validated direct-final chain has no close action/receipt.
        terminal_receipt = lifecycle_root.parent / "receipt-006-return_after_purchase.json"
    if not terminal_receipt.is_file():
        raise ValueError("terminal_skill_lifecycle_incomplete")
    handoff_value = lifecycle.get("handoff_report_path")
    if not isinstance(handoff_value, str):
        raise ValueError("terminal_skill_lifecycle_handoff_invalid")
    bound_handoff = Path(handoff_value)
    if not bound_handoff.is_absolute():
        bound_handoff = output_root / bound_handoff
    if (
        bound_handoff.resolve(strict=True) != handoff_path
        or lifecycle.get("handoff_report_sha256")
        != args.expected_consumed_terminal_handoff_sha256
    ):
        raise ValueError("terminal_skill_lifecycle_handoff_mismatch")
    return {
        "consumed_terminal_handoff_path": str(handoff_path),
        "consumed_terminal_handoff_sha256": (
            args.expected_consumed_terminal_handoff_sha256
        ),
        "consumed_terminal_skill_lifecycle_root_path": str(lifecycle_root),
        "consumed_terminal_skill_lifecycle_root_sha256": (
            args.expected_terminal_skill_lifecycle_root_sha256
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    try:
        report, code = run_supervised(args)
    except Exception as exc:
        return _print(
            {"is_valid": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"},
            indent=indent,
            code=2,
        )
    advisor_report = {"source": _advisor_source(args)}
    contract_sha256 = getattr(
        args,
        "deepseek_two_run_trial_contract_sha256",
        None,
    )
    if contract_sha256:
        advisor_report["trial"] = {
            "trial_id": args.deepseek_two_run_trial_id,
            "role": args.deepseek_two_run_trial_role,
            "contract_sha256": contract_sha256,
        }
    report["advisor"] = advisor_report
    return _print(report, indent=indent, code=code)


def run_supervised(
    args: argparse.Namespace,
    *,
    control_driver: CooperativeRunnerDriver | None = None,
) -> tuple[dict[str, Any], int]:
    if (
        not isinstance(args.start_turn_index, int)
        or isinstance(args.start_turn_index, bool)
        or args.start_turn_index < 1
    ):
        raise ValueError("start_turn_index_must_be_positive")
    if args.max_training_turns <= 0:
        raise ValueError("max_training_turns_must_be_positive")
    if args.inspect_current_count_races:
        return _run_current_count_race_inspection(args)
    if (args.count_race_resume_report or args.count_race_resume_report_sha256
        or getattr(args, "count_race_restart_evidence", None) or getattr(args, "count_race_restart_evidence_sha256", None)):
        return _resume_count_reserved_race(args)
    if args.event_formal_generation <= 0:
        raise ValueError("event_formal_generation_must_be_positive")
    try:
        consumed_terminal_binding = _load_consumed_terminal_skill_binding(args)
        if args.allow_career_end_finish_after_skill_handoff and (
            consumed_terminal_binding is None
            or not (args.route_before_first_turn or _uses_state_context_provider(args))
        ):
            raise ValueError("career_end_finish_requires_consumed_terminal_resume")
    except (
        OSError,
        ValueError,
        TerminalSkillHandoffError,
        TerminalSkillLifecycleError,
        ExternalTerminalSkillHandoffError,
    ) as exc:
        return (
            {
                "is_valid": False,
                "status": "stopped",
                "run_session_id": args.run_session_id,
                "completed_training_turns": 0,
                "requested_training_turns": args.max_training_turns,
                "stop_reason": "terminal_skill_resume_binding_invalid",
                "error": str(exc),
                "turn_reports": [],
            },
            2,
        )
    _validate_ai_review_source(args)
    _validate_state_context_configuration(args)
    _validate_explicit_condition_probe(args)
    try:
        run_trainee_authority = (
            _load_run_trainee_identity_authority(
                Path(args.run_trainee_authority_json),
                expected_run_session_id=args.run_session_id,
            )
            if args.run_trainee_authority_json
            else None
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RunTraineeIdentityError, ValueError) as exc:
        return (
            {
                "is_valid": False,
                "status": "stopped",
                "run_session_id": args.run_session_id,
                "completed_training_turns": 0,
                "requested_training_turns": args.max_training_turns,
                "stop_reason": "run_trainee_authority_invalid",
                "error": str(exc),
                "turn_reports": [],
            },
            2,
        )
    condition_policy = _resolved_condition_probe_policy(args)
    if args.route_engine == "progressive":
        if args.mode == "live":
            return _triggered_condition_failure(
                args, "progressive_condition_gate_unavailable"
            )
        progressive_report, progressive_code = _run_progressive_supervised(args)
        _add_action_accounting(
            progressive_report,
            requested=args.max_training_turns,
            completed=int(progressive_report.get("completed_training_turns") or 0),
        )
        return progressive_report, progressive_code

    tracker: ConditionTracker | None = None
    tracker_store: ConditionTrackerStore | None = None
    authority_cache: ConditionAuthorityCache | None = None
    tracker_recovery_reason: str | None = None
    if condition_policy is ConditionProbePolicy.TRIGGERED:
        if not args.dashboard_run_dir:
            return _triggered_condition_failure(args, "triggered_condition_tracker_store_required")
        dashboard_run_dir = Path(args.dashboard_run_dir)
        runtime_root = dashboard_run_dir / ".runtime"
        tracker_store = ConditionTrackerStore(runtime_root / "condition_tracker.v1.json")
        authority_cache = ConditionAuthorityCache(dashboard_run_dir)
        loaded = tracker_store.load(
            expected_run_id=dashboard_run_dir.name,
            current_turn=args.start_turn_index,
            audit_interval=args.condition_audit_interval,
        )
        tracker = loaded.tracker
        tracker_recovery_reason = loaded.recovery_reason

    output_root = Path(args.output_root)
    try:
        career_race_runtime = _prepare_career_race_runtime(args)
    except (CareerGoalLedgerError, OSError, ValueError) as exc:
        return (
            {
                "is_valid": False,
                "status": "stopped",
                "run_session_id": args.run_session_id,
                "completed_training_turns": 0,
                "requested_training_turns": args.max_training_turns,
                "stop_reason": "career_race_runtime_invalid",
                "error": str(exc),
                "turn_reports": [],
            },
            2,
        )
    turn_reports: list[dict[str, Any]] = []
    completed_training_turns = 0
    completed_race_turns = 0
    completed_action_turns = 0
    stop_reason = "max_training_turns_reached"
    exit_code = 0
    pre_route_report: dict[str, Any] | None = None
    provider_mode = _uses_state_context_provider(args)
    bootstrap_start_screen = args.start_screen
    provider_boundary = "main-menu" if provider_mode else None
    current_start_screen = bootstrap_start_screen
    first_turn_index = args.start_turn_index
    resolved_context_paths = (
        {}
        if provider_mode
        else {first_turn_index: _resolve_state_context_path(args, first_turn_index)}
    )
    provider_sources: set[tuple[str, str]] = set()
    current_provider_source: dict[str, str] | None = None
    control_boundary: str | None = None

    route_transition_path = _route_transition_path(args)
    if not route_transition_path.exists():
        RouteTransitionSession.new(args.run_session_id).save(route_transition_path)
    route_transition = RouteTransitionSession.load(
        route_transition_path,
        expected_run_session_id=args.run_session_id,
    )
    resume_pending_transition = bool(
        route_transition.status == RouteTransitionStatus.TRACKING
        and route_transition.expected_next_states
    )

    if args.route_before_first_turn or provider_mode:
        pre_route_report = {
            "turn_index": args.start_turn_index - 1,
            "turn_id": f"{args.run_session_id}_pre_route",
        }
        def pre_route_condition_race_guard(
            race_stop: str, advance_report: dict[str, Any]
        ) -> tuple[bool, str]:
            nonlocal tracker_recovery_reason
            if not _requires_critical_race_authority(race_stop):
                return True, ""
            if condition_policy is not ConditionProbePolicy.TRIGGERED:
                return True, ""
            assert tracker is not None and tracker_store is not None and authority_cache is not None
            try:
                race_source = _provider_source_from_route_step(advance_report)
                authority = _prepare_condition_turn_authority(
                    args=args,
                    tracker=tracker,
                    tracker_store=tracker_store,
                    authority_cache=authority_cache,
                    current_provider_source=race_source,
                    turn_root=output_root / f"{args.run_session_id}_pre_route" / "race_condition_gate",
                    turn_id=f"{args.run_session_id}_pre_route_race_gate",
                    turn_index=args.start_turn_index,
                    critical_race=True,
                    recovery_reason=tracker_recovery_reason,
                    control_driver=control_driver,
                )
            except (_ConditionGateFailure, ValueError):
                return False, "condition_probe_required_before_race_route"
            tracker_recovery_reason = None
            # This is a pre-action probe.  It is intentionally not committed
            # until a training action has actually completed.
            _write_condition_turn_summary(
                output_root / f"{args.run_session_id}_pre_route", authority.summary
            )
            return True, ""

        route_ok, route_stop_reason, route_exit_code = _route_between_turns(
            args,
            output_root / f"{args.run_session_id}_pre_route",
            pre_route_report,
            current_turn_index=args.start_turn_index,
            transition_resync=not resume_pending_transition,
            bootstrap_state=(
                None
                if resume_pending_transition
                else (
                    "modal"
                    if consumed_terminal_binding is not None
                    else bootstrap_start_screen
                )
            ),
            consumed_terminal_binding=consumed_terminal_binding,
            control_driver=control_driver,
            condition_race_guard=(
                pre_route_condition_race_guard
                if condition_policy is ConditionProbePolicy.TRIGGERED
                else None
            ),
        )
        if route_ok and route_stop_reason in TERMINAL_ROUTE_STOPS:
            if condition_policy is ConditionProbePolicy.TRIGGERED:
                assert tracker is not None and tracker_store is not None
                _apply_pre_route_condition_risk_and_persist(
                    args=args,
                    tracker=tracker,
                    tracker_store=tracker_store,
                    pre_route_report=pre_route_report,
                )
            early_report = {
                    "is_valid": True,
                    "status": "completed",
                    "run_session_id": args.run_session_id,
                    "completed_training_turns": 0,
                    "requested_training_turns": args.max_training_turns,
                    "stop_reason": route_stop_reason,
                    "pre_route": pre_route_report,
                    "turn_reports": turn_reports,
                }
            _add_action_accounting(
                early_report,
                requested=args.max_training_turns,
                completed=0,
            )
            if consumed_terminal_binding is not None:
                early_report.update(consumed_terminal_binding)
            return early_report, 0
        if not route_ok:
            if condition_policy is ConditionProbePolicy.TRIGGERED:
                assert tracker is not None and tracker_store is not None
                _apply_pre_route_condition_risk_and_persist(
                    args=args,
                    tracker=tracker,
                    tracker_store=tracker_store,
                    pre_route_report=pre_route_report,
                )
            failure_code = route_exit_code if route_exit_code != 0 else 2
            early_report = {
                    "is_valid": False,
                    "status": "stopped",
                    "run_session_id": args.run_session_id,
                    "completed_training_turns": 0,
                    "requested_training_turns": args.max_training_turns,
                    "stop_reason": route_stop_reason,
                    "pre_route": pre_route_report,
                    "turn_reports": turn_reports,
                }
            _add_action_accounting(
                early_report,
                requested=args.max_training_turns,
                completed=0,
            )
            return early_report, failure_code
        next_start_screen = _start_screen_from_route_stop(route_stop_reason)
        if next_start_screen is not None:
            current_start_screen = next_start_screen
        if provider_mode:
            required_stop = f"ready_{provider_boundary.replace('-', '_')}"
            if route_stop_reason != required_stop:
                return _provider_source_failure(
                    args,
                    turn_reports,
                    pre_route_report,
                    "state_context_provider_requires_ready_main_menu",
                )
            try:
                current_provider_source = _fresh_provider_source(
                    pre_route_report,
                    provider_sources,
                )
            except ValueError as exc:
                return _provider_source_failure(
                    args,
                    turn_reports,
                    pre_route_report,
                    str(exc),
                )
        if condition_policy is ConditionProbePolicy.TRIGGERED:
            assert tracker is not None and tracker_store is not None
            _apply_pre_route_condition_risk_and_persist(
                args=args,
                tracker=tracker,
                tracker_store=tracker_store,
                pre_route_report=pre_route_report,
            )

    training_parser = None
    for offset in range(args.max_training_turns):
        turn_index = args.start_turn_index + offset
        turn_id = f"{args.run_session_id}_turn_{turn_index:03d}"
        turn_root = output_root / turn_id
        prior_roots = (turn_root, *turn_root.glob("route_steps/*_one_turn_attempt"))
        if any((root / "decision").exists() for root in prior_roots):
            stop_reason, exit_code = "one_turn_retry_execution_evidence_present", 2
            turn_reports.append({"turn_index": turn_index, "turn_id": turn_id, "code": 2,
                                 "report": {"is_valid": False, "status": stop_reason}})
            break
        if turn_root.exists() and any(turn_root.iterdir()):
            turn_root = _next_route_artifact_dir(turn_root, {}, "one_turn_attempt")
            turn_root.mkdir(parents=True, exist_ok=False)
        if provider_mode:
            if current_provider_source is None:
                return _provider_source_failure(
                    args,
                    turn_reports,
                    pre_route_report,
                    "state_context_provider_source_missing",
                )
            context_path = None
        else:
            try:
                context_path = resolved_context_paths.pop(turn_index, None)
                if context_path is None:
                    context_path = _resolve_state_context_path(args, turn_index)
            except ValueError as exc:
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 2,
                        "state_context_source_path": _state_context_path_for_turn(args, turn_index),
                        "report": {
                            "is_valid": False,
                            "status": "state_context_invalid",
                            "error": str(exc),
                        },
                    }
                )
                stop_reason = "state_context_invalid"
                exit_code = 2
                break
        if not provider_mode and context_path is None:
            turn_reports.append(
                {
                    "turn_index": turn_index,
                    "turn_id": turn_id,
                    "code": 2,
                    "state_context_source_path": _state_context_path_for_turn(args, turn_index),
                    "report": {
                        "is_valid": False,
                        "status": "state_context_invalid",
                        "error": str(exc),
                    },
                }
            )
            stop_reason = "state_context_invalid"
            exit_code = 2
            break
        condition_report_path: Path | None = None
        condition_probe_report: dict[str, Any] | None = None
        condition_binding_key: str | None = None
        condition_gate_summary: dict[str, Any] | None = None
        if condition_policy is ConditionProbePolicy.ALWAYS:
            condition_binding_key = secrets.token_hex(32)
            condition_report_path, condition_probe_report, condition_probe_code = (
                _run_explicit_condition_probe(
                    args,
                    turn_root,
                    turn_id=turn_id,
                    turn_index=turn_index,
                    formal_condition_binding_key=condition_binding_key,
                )
            )
            if condition_probe_code != 0 or condition_report_path is None:
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 2,
                        "condition_probe": condition_probe_report,
                        "report": {
                            "is_valid": False,
                            "status": "condition_probe_failed",
                        },
                    }
                )
                stop_reason = "condition_probe_failed"
                exit_code = 2
                break
        elif condition_policy is ConditionProbePolicy.TRIGGERED:
            assert tracker is not None and tracker_store is not None and authority_cache is not None
            assert current_provider_source is not None
            try:
                authority = _prepare_condition_turn_authority(
                    args=args,
                    tracker=tracker,
                    tracker_store=tracker_store,
                    authority_cache=authority_cache,
                    current_provider_source=current_provider_source,
                    turn_root=turn_root,
                    turn_id=turn_id,
                    turn_index=turn_index,
                    critical_race=False,
                    recovery_reason=tracker_recovery_reason,
                    control_driver=control_driver,
                )
            except _ConditionGateFailure as exc:
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 2,
                        "condition_gate": {"status": str(exc)},
                        "report": {"is_valid": False, "status": str(exc)},
                    }
                )
                stop_reason = str(exc)
                exit_code = 2
                break
            condition_report_path = authority.report_path
            condition_binding_key = authority.binding_key
            condition_gate_summary = authority.summary
            prior_provider_path = current_provider_source["path"]
            current_provider_source = authority.provider_source
            if current_provider_source["path"] != prior_provider_path:
                provider_sources.add(
                    (current_provider_source["path"], current_provider_source["sha256"])
                )
            tracker_recovery_reason = None
        race_schedule_summary: dict[str, Any] | None = None
        schedule_state: tuple[TrainingTurnState, str | None] | None = None
        fan_shortfall_probe: dict[str, Any] | None = None
        count_requires_schedule = False
        if current_start_screen == "main-menu":
            try:
                schedule_state, current_provider_source = _race_schedule_before_training(
                    args,
                    turn_index=turn_index,
                    turn_root=turn_root,
                    context_path=context_path,
                    provider_source=current_provider_source,
                    seen_sources=provider_sources,
                    race_calendar=career_race_runtime.orchestrator.race_calendar,
                    control_driver=control_driver,
                )
                binding_stop = career_race_runtime.bind_pending_goal(
                    schedule_state=schedule_state, provider_source=current_provider_source,
                    turn_index=turn_index,
                    recover_from_main_menu=args.recover_career_goal_from_main_menu,
                )
                count_requires_schedule = binding_stop == "count_calendar_bound_reservation_pending"
                if binding_stop is not None and (
                    not count_requires_schedule
                    or not career_race_runtime.orchestrator.race_scheduling_enabled
                ):
                    turn_reports.append({
                        "turn_index": turn_index, "turn_id": turn_id, "code": 0,
                        "report": {"is_valid": True, "status": binding_stop},
                    })
                    stop_reason = binding_stop
                    break
                if count_requires_schedule or career_race_runtime.requires_main_menu_schedule(
                    state=schedule_state[0] if schedule_state is not None else None,
                ):
                    if schedule_state is None:
                        raise RaceSchedulerError("race_schedule_calendar_unbound")
                    state, source_frame_sha256 = schedule_state
                    if source_frame_sha256 is None:
                        raise RaceSchedulerError("race_schedule_calendar_unbound")
                    count_active = (career_race_runtime.orchestrator.goal_ledger_store.load(
                        career_race_runtime.ledger_path,
                        expected_run_session_id=career_race_runtime.run_dir.name).active_goals()
                        if count_requires_schedule else ())
                    count_requirement_verified = (len(count_active) == 1
                        and count_active[0].goal_kind == "graded_race_rank_count"
                        and count_active[0].progress_basis == "verified_lower_bound"
                        and count_active[0].completed_count >= count_active[0].required_count)
                    count_current = count_requires_schedule and not count_requirement_verified and any(
                        entry.grade == "G1" for entry in career_race_runtime.orchestrator.race_calendar.query_turn(
                            state.scenario_state["career_calendar_anchor"]["career_time"]["global_turn"], formal_only=False))
                    policy_defer = False
                    consecutive_defer = False
                    previous_turn_root = _count_race_previous_turn_root(args, turn_index)
                    previous_race = (
                        _count_race_previous_entry_evidence(args, turn_index)
                        if count_current and previous_turn_root is None
                        else None
                    )
                    if previous_race is not None:
                        current_races = tuple(
                            entry.race_id
                            for entry in career_race_runtime.orchestrator.race_calendar.query_turn(
                                state.scenario_state["career_calendar_anchor"]["career_time"]["global_turn"],
                                formal_only=False,
                            )
                            if entry.grade == "G1"
                        )
                        proof_path = turn_root / "count_consecutive_race_block.json"
                        _write_canonical_json_atomic(proof_path, {
                            "schema_version": "count_consecutive_race_block.v1",
                            "run_session_id": args.run_session_id,
                            "previous_runner_turn": turn_index - 1,
                            "current_runner_turn": turn_index,
                            "current_race_ids": list(current_races),
                            "previous_race_report": previous_race,
                        })
                        proof_sha = hashlib.sha256(proof_path.read_bytes()).hexdigest()
                        state_payload = state.to_dict()
                        state_payload["scenario_state"]["race_eligibility"] = {
                            race_id: {
                                "aptitude_ok": None,
                                "condition_ok": None,
                                "consecutive_race_ok": False,
                                "evidence_sha256": proof_sha,
                            }
                            for race_id in current_races
                        }
                        state = TrainingTurnState.from_dict(state_payload)
                        consecutive_defer = True
                    if count_current and (
                        args.count_race_inspection_report
                        or args.count_race_history_json
                        or previous_turn_root is not None
                    ):
                        schedule_decision, schedule_path = _prepare_count_race_reservation(
                            args, turn_root, career_race_runtime, turn_index=turn_index,
                            condition_report_path=condition_report_path,
                            condition_binding_key=condition_binding_key,
                            previous_turn_root=previous_turn_root,
                        )
                        if args.count_race_history_json and schedule_decision.status == "defer_to_local_policy":
                            persisted = json.loads(schedule_path.read_text(encoding="utf-8"))
                            proof = persisted["count_race_policy"]
                            if _count_policy_defer_decision(proof) != schedule_decision:
                                raise RaceSchedulerError("count_goal_current_schedule_persistence_mismatch")
                            current_provider_source = dict(proof["main_source"])
                            schedule_state = _race_schedule_state_for_turn(turn_index=turn_index,
                                turn_root=turn_root, context_path=context_path, provider_source=current_provider_source,
                                race_calendar=career_race_runtime.orchestrator.race_calendar)
                            if career_race_runtime.bind_pending_goal(schedule_state=schedule_state,
                                provider_source=current_provider_source, turn_index=turn_index) != "count_calendar_bound_reservation_pending":
                                raise RaceSchedulerError("count_goal_current_schedule_required")
                            state, source_frame_sha256 = schedule_state
                            provider_sources.add((current_provider_source["path"], current_provider_source["sha256"]))
                            policy_defer = True
                    else:
                        schedule_decision, schedule_path = career_race_runtime.plan_and_persist(
                            state=state,
                            source_frame_sha256=source_frame_sha256,
                            turn_index=turn_index,
                        )
                    if count_requires_schedule and (not isinstance(schedule_decision, RaceScheduleDecision)
                                                    or schedule_decision.status != "count_race_reserved"):
                        store = career_race_runtime.orchestrator.goal_ledger_store
                        ledger = store.load(career_race_runtime.ledger_path,
                                            expected_run_session_id=career_race_runtime.run_dir.name)
                        active = ledger.active_goals()
                        if (not isinstance(schedule_decision, RaceScheduleDecision)
                            or schedule_decision.status != "defer_to_local_policy"
                            or len(schedule_decision.candidates) != 1
                            or schedule_decision.required_remaining
                            or schedule_decision.verified_opportunity_race_ids
                            or len(active) != 1 or active[0].goal_kind != "graded_race_rank_count"
                            or active[0].progress_basis != "verified_lower_bound"
                            or active[0].deadline_turn is None):
                            raise RaceSchedulerError("count_goal_current_schedule_required")
                        candidate = schedule_decision.candidates[0]
                        if (candidate.action_kind != "defer_to_local_policy" or candidate.race_id is not None
                             or candidate.priority != "none" or candidate.reasons != (
                                 "count_goal_requirement_verified_waiting_system"
                                 if active[0].completed_count >= active[0].required_count else
                                 "count_grade_race_consecutive_blocked" if consecutive_defer else
                                 "count_race_policy_defer" if policy_defer else "no_current_count_grade_race",)
                            or candidate.source_goal_ids != (active[0].goal_instance_id,)):
                            raise RaceSchedulerError("count_goal_current_schedule_required")
                        persisted = json.loads(schedule_path.read_text(encoding="utf-8"))
                        if persisted != {
                            "schema_version": "race_schedule_decision.v1",
                            "run_session_id": career_race_runtime.run_dir.name,
                            "turn_index": turn_index, "source_frame_sha256": source_frame_sha256,
                            "ledger_path": str(career_race_runtime.ledger_path),
                            "ledger_revision": ledger.revision, "ledger_view_sha256": ledger.current_view_sha256,
                            "decision": race_schedule_decision_to_dict(schedule_decision),
                            **({"count_race_policy": proof} if policy_defer else {}),
                        }:
                            raise RaceSchedulerError("count_goal_current_schedule_persistence_mismatch")
                    race_schedule_summary = {
                        "path": str(schedule_path),
                        **race_schedule_decision_to_dict(schedule_decision),
                    }
            except (
                CareerGoalLedgerError,
                OSError,
                RaceSchedulerError,
                UnicodeError,
                ValueError,
            ) as exc:
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 2,
                        "race_schedule": {
                            "status": "race_schedule_invalid",
                            "error": str(exc),
                        },
                        "report": {
                            "is_valid": False,
                            "status": "race_schedule_invalid",
                        },
                    }
                )
                stop_reason = "race_schedule_invalid"
                exit_code = 2
                break
        if _fan_shortfall_handoff_due(schedule_state):
            fan_entry = {
                "turn_index": turn_index,
                "turn_id": turn_id,
                "code": 0,
                "route_steps": [],
                "fan_shortfall_handoff": {
                    "status": "due",
                    "max_remaining_turns": 6,
                },
            }
            if not _fan_shortfall_route_authorized(args):
                fan_entry["code"] = 2
                fan_entry["report"] = {
                    "is_valid": False,
                    "status": "fan_shortfall_race_route_not_authorized",
                }
                turn_reports.append(fan_entry)
                stop_reason = "fan_shortfall_race_route_not_authorized"
                exit_code = 2
                break
            if condition_policy is ConditionProbePolicy.TRIGGERED:
                assert tracker is not None and tracker_store is not None and authority_cache is not None
                assert current_provider_source is not None
                try:
                    authority = _prepare_condition_turn_authority(
                        args=args,
                        tracker=tracker,
                        tracker_store=tracker_store,
                        authority_cache=authority_cache,
                        current_provider_source=current_provider_source,
                        turn_root=turn_root / "fan_race_condition_gate",
                        turn_id=f"{turn_id}_fan_race_gate",
                        turn_index=turn_index,
                        critical_race=True,
                        recovery_reason=None,
                        control_driver=control_driver,
                    )
                except _ConditionGateFailure as exc:
                    fan_entry["code"] = 2
                    fan_entry["report"] = {
                        "is_valid": False,
                        "status": str(exc),
                    }
                    turn_reports.append(fan_entry)
                    stop_reason = str(exc)
                    exit_code = 2
                    break
                condition_report_path = authority.report_path
                condition_binding_key = authority.binding_key
                condition_gate_summary = authority.summary
                prior_provider_path = current_provider_source["path"]
                current_provider_source = authority.provider_source
                if current_provider_source["path"] != prior_provider_path:
                    provider_sources.add(
                        (current_provider_source["path"], current_provider_source["sha256"])
                    )
                fan_entry["condition_gate"] = condition_gate_summary
            route_ok, route_stop, route_code = _run_race_route_step(
                args,
                turn_root,
                fan_entry,
                fan_shortfall_only=True,
            )
            fan_entry["code"] = route_code
            fan_entry["report"] = {
                "is_valid": route_ok,
                "status": "fan_shortfall_race_routed",
                "stop_reason": route_stop,
            }
            race_report = fan_entry.get("race_route", {}).get("report", {})
            fan_progress = _fan_shortfall_entry_progress(race_report)
            fan_entry["fan_shortfall_progress"] = fan_progress
            if not route_ok:
                turn_reports.append(fan_entry)
                stop_reason = route_stop
                exit_code = route_code if route_code != 0 else 2
                break
            if fan_progress["entry_consumed"]:
                completed_race_turns += 1
                completed_action_turns += 1
                if condition_policy is ConditionProbePolicy.TRIGGERED:
                    assert tracker is not None and tracker_store is not None
                    _save_condition_tracker_after_action(
                        args=args,
                        tracker=tracker,
                        tracker_store=tracker_store,
                        turn_index=turn_index,
                        summary=condition_gate_summary,
                    )
                continuation_ok, continuation_stop, continuation_code = _route_between_turns(
                    args,
                    turn_root,
                    fan_entry,
                    current_turn_index=turn_index,
                    transition_resync=False,
                    control_driver=control_driver,
                    condition_race_guard=lambda _race_stop, _report: (
                        False,
                        "fan_shortfall_continuation_race_boundary",
                    ),
                )
                fan_entry["report"] = {
                    "is_valid": continuation_ok,
                    "status": "fan_shortfall_race_action_completed",
                    "stop_reason": continuation_stop,
                }
                turn_reports.append(fan_entry)
                if not continuation_ok:
                    stop_reason = continuation_stop
                    exit_code = continuation_code if continuation_code != 0 else 2
                    break
                if continuation_stop in TERMINAL_ROUTE_STOPS | TERMINAL_HANDOFF_ROUTE_STOPS:
                    stop_reason = continuation_stop
                    break
                next_start_screen = _start_screen_from_route_stop(continuation_stop)
                if next_start_screen is not None:
                    current_start_screen = next_start_screen
                if provider_mode:
                    if continuation_stop != "ready_main_menu":
                        stop_reason = "state_context_provider_requires_ready_main_menu"
                        exit_code = 2
                        break
                    try:
                        current_provider_source = _fresh_provider_source(
                            fan_entry,
                            provider_sources,
                        )
                    except ValueError as exc:
                        stop_reason = str(exc)
                        exit_code = 2
                        break
                if condition_policy is ConditionProbePolicy.TRIGGERED:
                    assert tracker is not None and tracker_store is not None
                    _apply_condition_route_risk_and_persist(
                        args=args,
                        tracker=tracker,
                        tracker_store=tracker_store,
                        turn_index=turn_index,
                        turn_root=turn_root,
                        turn_entry=fan_entry,
                        summary=condition_gate_summary,
                    )
                _record_turn_truth_capture(turn_root, fan_entry)
                continue
            if route_stop != "ready_main_menu":
                fan_entry["code"] = 2
                fan_entry["report"] = {
                    "is_valid": False,
                    "status": fan_progress["reason"],
                    "stop_reason": route_stop,
                }
                turn_reports.append(fan_entry)
                stop_reason = fan_progress["reason"]
                exit_code = 2
                break
            if provider_mode:
                try:
                    current_provider_source = _fresh_provider_source(
                        fan_entry,
                        provider_sources,
                    )
                except ValueError as exc:
                    turn_reports.append(fan_entry)
                    stop_reason = str(exc)
                    exit_code = 2
                    break
            fan_shortfall_probe = fan_entry
        if race_schedule_summary is not None:
            if (
                race_schedule_summary["status"]
                == "career_goal_schedule_unfulfillable"
            ):
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 0,
                        "race_schedule": race_schedule_summary,
                        "report": {
                            "is_valid": True,
                            "status": "career_goal_schedule_unfulfillable",
                        },
                    }
                )
                stop_reason = "career_goal_schedule_unfulfillable"
                break
            planned_race_ids = tuple(
                candidate["race_id"]
                for candidate in race_schedule_summary["candidates"]
                if candidate["action_kind"] == "enter_race"
                and (candidate["priority"] in {"hard", "must"} or race_schedule_summary["status"] == "count_race_reserved")
                and isinstance(candidate.get("race_id"), str)
            )
            if planned_race_ids:
                turn_entry = {
                    "turn_index": turn_index,
                    "turn_id": turn_id,
                    "code": 0,
                    "race_schedule": race_schedule_summary,
                    "route_steps": [],
                }
                if race_schedule_summary["status"] == "count_race_reserved":
                    route_ok, route_stop, route_code = _run_count_reserved_race(
                        args, turn_root, turn_entry, schedule_path=Path(race_schedule_summary["path"]),
                        runtime=career_race_runtime, turn_index=turn_index)
                    turn_entry["code"] = route_code
                    turn_entry["report"] = {"is_valid": route_ok, "status": "count_race_routed", "stop_reason": route_stop}
                    if turn_entry.get("count_entry_confirmed") is True:
                        completed_race_turns += 1
                        completed_action_turns += 1
                    turn_reports.append(turn_entry)
                    stop_reason, exit_code = route_stop, route_code
                    break
                route_ok, route_stop, route_code = _run_race_route_step(
                    args,
                    turn_root,
                    turn_entry,
                    planned_race_ids=planned_race_ids,
                    schedule_packet_sha256=race_schedule_summary[
                        "packet_sha256"
                    ],
                    suppress_result_continue=True,
                )
                turn_entry["code"] = route_code
                turn_entry["report"] = {
                    "is_valid": route_ok,
                    "status": "race_schedule_routed",
                    "stop_reason": route_stop,
                }
                race_report = turn_entry.get("race_route", {}).get("report", {})
                try:
                    race_progress = inspect_scheduled_race_progress(
                        race_report,
                        planned_race_ids=planned_race_ids,
                        schedule_packet_sha256=race_schedule_summary[
                            "packet_sha256"
                        ],
                    )
                except ScheduledRaceProgressError as exc:
                    race_progress = None
                    turn_entry["scheduled_race_progress"] = {
                        "schema_version": "uma.scheduled_race_progress.v1",
                        "entry_consumed": False,
                        "race_id": None,
                        "confirmation_step": None,
                        "reason": str(exc),
                    }
                else:
                    turn_entry["scheduled_race_progress"] = race_progress.to_dict()
                if race_progress is None or not race_progress.entry_consumed:
                    turn_reports.append(turn_entry)
                    stop_reason = route_stop
                    exit_code = route_code
                    break

                completed_race_turns += 1
                completed_action_turns += 1
                continuation_ok, continuation_stop, continuation_code = (
                    _route_between_turns(
                        args,
                        turn_root,
                        turn_entry,
                        current_turn_index=turn_index,
                        transition_resync=True,
                        control_driver=control_driver,
                        condition_race_guard=lambda _race_stop, _report: (
                            False,
                            "scheduled_race_continuation_race_boundary",
                        ),
                    )
                )
                turn_entry["report"] = {
                    "is_valid": continuation_ok,
                    "status": "scheduled_race_action_completed",
                    "stop_reason": continuation_stop,
                }
                turn_reports.append(turn_entry)
                if not continuation_ok:
                    stop_reason = continuation_stop
                    exit_code = continuation_code if continuation_code != 0 else 2
                    break
                if continuation_stop in TERMINAL_ROUTE_STOPS:
                    stop_reason = continuation_stop
                    break
                if continuation_stop in TERMINAL_HANDOFF_ROUTE_STOPS:
                    stop_reason = continuation_stop
                    break
                next_start_screen = _start_screen_from_route_stop(
                    continuation_stop
                )
                if next_start_screen is not None:
                    current_start_screen = next_start_screen
                if provider_mode:
                    if continuation_stop != "ready_main_menu":
                        stop_reason = (
                            "state_context_provider_requires_ready_main_menu"
                        )
                        exit_code = 2
                        break
                    try:
                        current_provider_source = _fresh_provider_source(
                            turn_entry,
                            provider_sources,
                        )
                    except ValueError as exc:
                        stop_reason = str(exc)
                        exit_code = 2
                        break
                _record_turn_truth_capture(turn_root, turn_entry)
                continue
        pending_reason = None
        if current_start_screen != "main-menu":
            pending_reason = (
                "career_goal_recovery_requires_main_menu" if args.recover_career_goal_from_main_menu
                else _pending_career_goal_stop_reason(args)
            )
        if pending_reason is not None:
            turn_reports.append({"turn_index": turn_index, "turn_id": turn_id, "code": 2,
                                 "report": {"is_valid": False, "status": pending_reason}})
            stop_reason, exit_code = pending_reason, 2
            break
        if current_start_screen == "main-menu" and args.allow_terminal_skill_entry:
            skill_entry = _maybe_run_terminal_skill_entry(
                args,
                turn_root,
                context_path,
                current_provider_source,
                turn_index=turn_index,
            )
            if skill_entry == "handoff":
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 0,
                        "terminal_skill_entry": {"status": "handoff_ready"},
                        "report": {
                            "is_valid": True,
                            "status": "skill_acquisition_handoff",
                        },
                    }
                )
                stop_reason = "skill_acquisition_handoff"
                break
            if skill_entry == "unverified":
                turn_reports.append(
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_id,
                        "code": 2,
                        "terminal_skill_entry": {"status": "unverified"},
                        "report": {
                            "is_valid": False,
                            "status": "terminal_skill_entry_unverified",
                        },
                    }
                )
                stop_reason = "terminal_skill_entry_unverified"
                exit_code = 2
                break
        if training_parser is None:
            from core.training_parser import TrainingParser

            training_parser = TrainingParser(
                use_mimo=False, recognition_mode="lightweight",
                larc_roster_path=output_root / args.run_session_id / "larc_member_roster.json",
            )
        one_turn_report, one_turn_code = _invoke_one_turn(
            _one_turn_argv(
                args,
                turn_index,
                turn_id,
                turn_root,
                current_start_screen,
                context_path,
                current_provider_source["path"] if current_provider_source else None,
                current_provider_source.get("consensus_paths", ())
                if current_provider_source
                else (),
                condition_report_path,
            ),
            formal_condition_binding_key=condition_binding_key,
            control_driver=control_driver,
            training_parser=training_parser,
        )
        turn_entry = {
                "turn_index": turn_index,
                "turn_id": turn_id,
                "code": one_turn_code,
                "state_context_source_path": str(context_path) if context_path else None,
                "report": one_turn_report,
            }
        if fan_shortfall_probe is not None:
            turn_entry["fan_shortfall_probe"] = fan_shortfall_probe
        if condition_probe_report is not None:
            turn_entry["condition_probe"] = condition_probe_report
        if condition_gate_summary is not None:
            turn_entry["condition_gate"] = condition_gate_summary
        if current_provider_source is not None:
            turn_entry["state_context_provider"] = dict(current_provider_source)
            artifacts = one_turn_report.get("artifacts")
            if isinstance(artifacts, dict):
                for key in (
                    "state_context_provider_path",
                    "state_context_snapshot_path",
                    "source_screenshot_path",
                    "source_screenshot_sha256",
                ):
                    if isinstance(artifacts.get(key), str):
                        turn_entry["state_context_provider"][key] = artifacts[key]
        if race_schedule_summary is not None:
            turn_entry["race_schedule"] = race_schedule_summary
        turn_reports.append(turn_entry)

        if one_turn_code != 0 or not one_turn_report.get("is_valid"):
            _record_turn_truth_capture(turn_root, turn_entry)
            stop_reason = str(one_turn_report.get("status", "one_turn_failed"))
            exit_code = 2
            break
        if one_turn_report.get("status") != "completed":
            _record_turn_truth_capture(turn_root, turn_entry)
            stop_reason = str(one_turn_report.get("status", "one_turn_not_completed"))
            if stop_reason == "control_stopped":
                boundary = one_turn_report.get("control_boundary")
                if isinstance(boundary, str):
                    control_boundary = boundary
            break

        live_loop = one_turn_report.get("stages", {}).get("live_loop", {})
        execution = live_loop.get("execution", {}) if isinstance(live_loop, dict) else {}
        if not execution.get("performed"):
            _record_turn_truth_capture(turn_root, turn_entry)
            stop_reason = str(execution.get("reason", "training_not_performed"))
            break
        is_training_action = (
            isinstance(live_loop, dict)
            and isinstance(live_loop.get("decision"), dict)
            and live_loop["decision"].get("action") == "train"
        )
        is_rest_action = (
            isinstance(live_loop, dict)
            and isinstance(live_loop.get("decision"), dict)
            and live_loop["decision"].get("action") == "rest"
        )
        is_clinic_action = (
            isinstance(live_loop, dict)
            and isinstance(live_loop.get("decision"), dict)
            and live_loop["decision"].get("action") == "clinic"
        )
        transition_resync_after_action = True
        if is_training_action:
            try:
                turn_entry["training_route_receipt"] = _record_training_route_receipt(
                    args=args,
                    execution=execution,
                )
            except (OSError, RouteTransitionError) as exc:
                _record_turn_truth_capture(turn_root, turn_entry)
                stop_reason = f"training_route_receipt:{exc}"
                exit_code = 2
                break
            transition_resync_after_action = False
        elif is_rest_action:
            try:
                turn_entry["rest_route_receipt"] = _record_rest_route_receipt(
                    args=args,
                    execution=execution,
                )
            except (OSError, RouteTransitionError) as exc:
                _record_turn_truth_capture(turn_root, turn_entry)
                stop_reason = f"rest_route_receipt:{exc}"
                exit_code = 2
                break
            transition_resync_after_action = False
        prior_event_context = _attach_prior_event_context_shadow(
            turn_entry=turn_entry,
            run_session_id=args.run_session_id,
            global_action_turn=turn_index,
            turn_root=turn_root,
            one_turn_report=one_turn_report,
        )
        if condition_policy is ConditionProbePolicy.TRIGGERED:
            assert tracker is not None and tracker_store is not None
            if is_clinic_action:
                tracker.mark_dirty()
                if condition_gate_summary is not None:
                    condition_gate_summary["dirty"] = True
                    _write_condition_turn_summary(turn_root, condition_gate_summary)
            _save_condition_tracker_after_action(
                args=args,
                tracker=tracker,
                tracker_store=tracker_store,
                turn_index=turn_index,
                summary=condition_gate_summary,
            )
        completed_training_turns += 1
        completed_action_turns += 1
        should_route_after_action = (provider_mode and offset < args.max_training_turns - 1) or (
            args.advance_between_turns and (
            offset < args.max_training_turns - 1 or args.advance_after_final_action
            )
        )
        if should_route_after_action:
            def condition_race_guard(
                race_stop: str, advance_report: dict[str, Any]
            ) -> tuple[bool, str]:
                nonlocal condition_gate_summary
                if not _requires_critical_race_authority(race_stop):
                    return True, ""
                assert tracker is not None and tracker_store is not None and authority_cache is not None
                assert current_provider_source is not None
                try:
                    race_source = _provider_source_from_route_step(advance_report)
                except ValueError:
                    return False, "condition_probe_required_before_race_route"
                try:
                    authority = _prepare_condition_turn_authority(
                        args=args,
                        tracker=tracker,
                        tracker_store=tracker_store,
                        authority_cache=authority_cache,
                        current_provider_source=race_source,
                        turn_root=turn_root / "race_condition_gate",
                        turn_id=f"{turn_id}_race_gate",
                        turn_index=turn_index,
                        critical_race=True,
                        recovery_reason=None,
                        control_driver=control_driver,
                    )
                except _ConditionGateFailure as exc:
                    return False, str(exc)
                condition_gate_summary = authority.summary
                turn_reports[-1]["condition_gate"] = condition_gate_summary
                _write_condition_turn_summary(turn_root, condition_gate_summary)
                _save_condition_tracker_after_action(
                    args=args,
                    tracker=tracker,
                    tracker_store=tracker_store,
                    turn_index=turn_index,
                    summary=condition_gate_summary,
                )
                return True, ""

            route_ok, route_stop_reason, route_exit_code = _route_between_turns(
                args,
                turn_root,
                turn_reports[-1],
                current_turn_index=turn_index,
                transition_resync=transition_resync_after_action,
                control_driver=control_driver,
                condition_race_guard=(
                    condition_race_guard
                    if condition_policy is ConditionProbePolicy.TRIGGERED
                    else None
                ),
                prior_event_context=prior_event_context,
                run_trainee_authority=run_trainee_authority,
            )
            if not route_ok:
                _record_turn_truth_capture(turn_root, turn_entry)
                if condition_policy is ConditionProbePolicy.TRIGGERED:
                    assert tracker is not None and tracker_store is not None
                    _apply_condition_route_risk_and_persist(
                        args=args,
                        tracker=tracker,
                        tracker_store=tracker_store,
                        turn_index=turn_index,
                        turn_root=turn_root,
                        turn_entry=turn_reports[-1],
                        summary=condition_gate_summary,
                    )
                stop_reason = route_stop_reason
                exit_code = route_exit_code if route_exit_code != 0 else 2
                break
            if route_stop_reason in TERMINAL_ROUTE_STOPS:
                _record_turn_truth_capture(turn_root, turn_entry)
                if condition_policy is ConditionProbePolicy.TRIGGERED:
                    assert tracker is not None and tracker_store is not None
                    _apply_condition_route_risk_and_persist(
                        args=args,
                        tracker=tracker,
                        tracker_store=tracker_store,
                        turn_index=turn_index,
                        turn_root=turn_root,
                        turn_entry=turn_reports[-1],
                        summary=condition_gate_summary,
                    )
                stop_reason = route_stop_reason
                break
            if route_stop_reason in TERMINAL_HANDOFF_ROUTE_STOPS:
                _record_turn_truth_capture(turn_root, turn_entry)
                stop_reason = route_stop_reason
                break
            next_start_screen = _start_screen_from_route_stop(route_stop_reason)
            if next_start_screen is not None:
                current_start_screen = next_start_screen
            if provider_mode:
                if route_stop_reason != "ready_main_menu":
                    _record_turn_truth_capture(turn_root, turn_entry)
                    stop_reason = "state_context_provider_requires_ready_main_menu"
                    exit_code = 2
                    break
            if (
                condition_policy is ConditionProbePolicy.TRIGGERED
                and route_stop_reason == "ready_main_menu"
            ):
                assert tracker is not None and tracker_store is not None
                _apply_condition_route_risk_and_persist(
                    args=args,
                    tracker=tracker,
                    tracker_store=tracker_store,
                    turn_index=turn_index,
                    turn_root=turn_root,
                    turn_entry=turn_reports[-1],
                    summary=condition_gate_summary,
                )
            if provider_mode:
                try:
                    current_provider_source = _fresh_provider_source(
                        turn_reports[-1],
                        provider_sources,
                    )
                except ValueError as exc:
                    _record_turn_truth_capture(turn_root, turn_entry)
                    stop_reason = str(exc)
                    exit_code = 2
                    break
        _record_turn_truth_capture(turn_root, turn_entry)
        if args.dashboard_run_dir:
            try:
                turn_reports[-1]["dashboard_record"] = record_dashboard_turn(
                    Path(args.dashboard_run_dir),
                    turn_root,
                    turn_index=turn_index,
                    workspace_root=ROOT,
                    display_title=args.dashboard_display_title,
                    display_subtitle=args.dashboard_display_subtitle,
                )
            except (OSError, ValueError) as exc:
                turn_reports[-1]["dashboard_record_error"] = str(exc)
                # Dashboard archival is best-effort: a stale packet conflict
                # or missing archive must never halt training progress.
                continue
    else:
        stop_reason = "max_training_turns_reached"

    completed_stop_reasons = {"max_training_turns_reached", *TERMINAL_ROUTE_STOPS}
    report = {
        "is_valid": exit_code == 0,
        "status": (
            "control_stopped"
            if stop_reason == "control_stopped"
            else "completed"
            if stop_reason in completed_stop_reasons
            else "stopped"
        ),
        "run_session_id": args.run_session_id,
        "completed_training_turns": completed_training_turns,
        "completed_race_turns": completed_race_turns,
        "completed_action_turns": completed_action_turns,
        "requested_training_turns": args.max_training_turns,
        "stop_reason": stop_reason,
        "turn_reports": turn_reports,
    }
    if pre_route_report is not None:
        report["pre_route"] = pre_route_report
    for terminal_entry in reversed(turn_reports):
        terminal_handoff = terminal_entry.get("terminal_skill_handoff")
        if isinstance(terminal_handoff, dict):
            report["terminal_skill_handoff"] = terminal_handoff
            break
    if control_boundary is not None:
        report["control_boundary"] = control_boundary
    if (
        consumed_terminal_binding is not None
        and exit_code == 0
        and report["is_valid"] is True
    ):
        report.update(consumed_terminal_binding)
    _add_action_accounting(
        report,
        requested=args.max_training_turns,
        completed=completed_action_turns,
    )
    return (report, exit_code)


def _run_progressive_supervised(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int]:
    base: dict[str, Any] = {
        "run_session_id": args.run_session_id,
        "route_engine": "progressive",
        "requested_training_turns": args.max_training_turns,
        "completed_training_turns": 0,
        "performed": False,
    }
    if args.mode != "dry-run":
        return (
            {
                **base,
                "is_valid": False,
                "status": "stopped",
                "stop_reason": "progressive_live_not_authorized",
            },
            2,
        )
    try:
        session, store = _validate_progressive_resume_inputs(args)
        if args.progressive_max_directives <= 0:
            raise ValueError("progressive_max_directives_must_be_positive")
        progressive_runner = _build_progressive_dry_run_runner(
            args,
            session=session,
            store=store,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return (
            {
                **base,
                "is_valid": False,
                "status": "stopped",
                "stop_reason": f"progressive_resume_invalid:{exc}",
            },
            2,
        )
    try:
        result = progressive_runner.run(args.progressive_max_directives)
        session.save(args.progressive_session)
        load_transition_packet(
            store.packet_path,
            session=session,
            expected_run_session_id=args.run_session_id,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return (
            {
                **base,
                "is_valid": False,
                "status": "stopped",
                "stop_reason": f"progressive_runner_failed:{exc}",
            },
            2,
        )
    if not isinstance(result, str):
        return (
            {
                **base,
                "is_valid": False,
                "status": "stopped",
                "stop_reason": "progressive_runner_result_invalid",
            },
            2,
        )
    completed = result in {"career_complete", "career_complete_main"}
    bounded = result == "max_directives_exhausted"
    valid_stop = result.startswith(
        ("explicit_probe:", "collect_scenario:", "handoff:", "controlled_stop:")
    )
    return (
        {
            **base,
            "is_valid": completed or bounded or valid_stop,
            "status": "completed" if completed or bounded else "stopped",
            "stop_reason": result,
        },
        0 if completed or bounded or valid_stop else 2,
    )


def _validate_progressive_resume_inputs(
    args: argparse.Namespace,
) -> tuple[ProgressiveRouteSession, TransitionStore]:
    if not args.progressive_run_id:
        raise ValueError("progressive_run_id_required")
    if not args.progressive_session:
        raise ValueError("progressive_session_required")
    if not args.progressive_transition_packet:
        raise ValueError("progressive_transition_packet_required")
    if not args.progressive_frame_trace:
        raise ValueError("progressive_frame_trace_required")
    if not args.progressive_frame_trace_sha256:
        raise ValueError("progressive_frame_trace_sha256_required")
    session = ProgressiveRouteSession.load(args.progressive_session)
    if session.run_session_id != args.run_session_id:
        raise ValueError("progressive_session_run_session_mismatch")
    packet_path = Path(args.progressive_transition_packet).resolve(strict=True)
    store = TransitionStore(
        packet_path.parent.parent,
        run_id=args.progressive_run_id,
        session_id=args.run_session_id,
    )
    if store.packet_path.resolve(strict=True) != packet_path:
        raise ValueError("progressive_transition_packet_store_mismatch")
    store.load_last_directive_sequence(session)
    load_transition_packet(
        packet_path,
        session=session,
        expected_run_session_id=args.run_session_id,
    )
    return session, store


class _StructuredTraceFrameSource:
    _FIELDS = frozenset(
        {
            "path",
            "source_sha256",
            "captured_at",
            "stable",
            "period_key",
            "calendar_ordinal",
            "perception",
        }
    )

    def __init__(self, path: Path, expected_sha256: str) -> None:
        payload = path.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256:
            raise ValueError("progressive_frame_trace_sha256_mismatch")
        self._frames: list[CapturedRouteFrame] = []
        for line in payload.decode("utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict) or set(raw) != self._FIELDS:
                raise ValueError("progressive_frame_trace_record_invalid")
            self._frames.append(CapturedRouteFrame(**raw))
        if not self._frames:
            raise ValueError("progressive_frame_trace_empty")

    def capture(
        self,
        candidate_detector_ids: tuple[str, ...],
    ) -> CapturedRouteFrame:
        if not candidate_detector_ids:
            raise ValueError("progressive_candidate_detector_ids_empty")
        if not self._frames:
            raise ValueError("progressive_frame_trace_exhausted")
        return self._frames.pop(0)


class _OfflineProgressiveDriver:
    def __init__(self, *, run_id: str, session_id: str) -> None:
        self.port = SimpleNamespace(run_id=run_id, session_id=session_id)

    def reach(self, boundary: str) -> None:
        if not boundary:
            raise ValueError("progressive_boundary_empty")

    def after_fresh_frame(self, evidence: object) -> None:
        if getattr(evidence, "sha256", None) is None:
            raise ValueError("progressive_fresh_frame_evidence_invalid")


def _build_progressive_dry_run_runner(
    args: argparse.Namespace,
    *,
    session: ProgressiveRouteSession,
    store: TransitionStore,
) -> ProgressiveRouteRunner:
    graph = ProgressiveRouteGraph.load()
    runtime = ProgressiveRouteRuntime(
        graph,
        session,
        ScopedRouteRecognizer(graph, ScreenGraph.load()),
    )
    source = _StructuredTraceFrameSource(
        Path(args.progressive_frame_trace),
        args.progressive_frame_trace_sha256,
    )
    return ProgressiveRouteRunner(
        runtime=runtime,
        frame_source=source,
        action_port=DryRunProgressiveActionPort(),
        driver=_OfflineProgressiveDriver(
            run_id=args.progressive_run_id,
            session_id=args.run_session_id,
        ),
        transition_store=store,
        wait=lambda _seconds: None,
    )


def _one_turn_argv(
    args: argparse.Namespace,
    turn_index: int,
    turn_id: str,
    turn_root: Path,
    start_screen: str,
    state_context_path: Path | None,
    main_menu_screenshot_path: str | None = None,
    main_menu_consensus_screenshot_paths: tuple[str, ...] | list[str] = (),
    condition_report_path: Path | None = None,
) -> list[str]:
    argv = [
        "--goal-config",
        str(Path(args.goal_config)),
        "--output-root",
        str(turn_root / "capture"),
        "--actual-group-id",
        turn_id,
        "--run-session-id",
        args.run_session_id,
        "--turn-index",
        str(turn_index),
        "--source-state-key",
        turn_id,
        "--operator-id",
        args.operator_id,
        "--run-dir",
        str(turn_root / "decision"),
        "--mode",
        args.mode,
        "--max-turns",
        "1",
        "--start-screen",
        start_screen,
        "--settle-seconds",
        str(args.settle_seconds),
        "--min-live-sidecar-confidence",
        str(args.min_live_sidecar_confidence),
        "--min-decision-confidence",
        str(args.min_decision_confidence),
        "--post-action-skip-taps",
        str(args.post_action_skip_taps),
    ]
    if args.state_context_provider:
        argv.extend(
            [
                "--state-context-provider",
                args.state_context_provider,
                "--main-menu-screenshot-path",
                str(main_menu_screenshot_path),
            ]
        )
        for consensus_path in main_menu_consensus_screenshot_paths:
            argv.extend(["--main-menu-consensus-screenshot-path", consensus_path])
    else:
        argv.extend(["--state-context-json", str(state_context_path)])
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.capture_live:
        argv.append("--capture-live")
    if args.capture_execute:
        argv.append("--capture-execute")
    if args.capture_confirm_token:
        argv.extend(["--capture-confirm-token", args.capture_confirm_token])
    if getattr(args, "collect_larc_ss_information", False):
        argv.append("--collect-larc-ss-information")
    if args.execute_training_confirm:
        argv.append("--execute-training-confirm")
    if args.training_confirm_token:
        argv.extend(["--training-confirm-token", args.training_confirm_token])
    if condition_report_path is not None:
        argv.extend(["--condition-report-json", str(condition_report_path)])
        argv.append("--require-explicit-condition-probe")
    if args.allow_partial_live_sidecar:
        argv.append("--allow-partial-live-sidecar")
    if getattr(args, "numeric_blind_fallback", False) is True:
        argv.append("--numeric-blind-fallback")
    if args.ai_review_json:
        argv.extend(["--ai-review-json", str(Path(args.ai_review_json))])
    if args.ai_review_command:
        argv.extend(["--ai-review-command", args.ai_review_command])
        argv.extend(
            [
                "--ai-review-command-timeout-seconds",
                str(args.ai_review_command_timeout_seconds),
            ]
        )
    structured_mode = getattr(
        args,
        "structured_advisor_mode",
        "off",
    )
    if structured_mode != "off":
        argv.extend(
            ["--structured-advisor-mode", structured_mode]
        )
        for command_arg in getattr(
            args,
            "structured_advisor_command_arg",
            [],
        ):
            argv.extend(
                ["--structured-advisor-command-arg", command_arg]
            )
        argv.extend(
            [
                "--structured-advisor-model-id",
                str(args.structured_advisor_model_id),
                "--structured-advisor-invalid-policy",
                str(args.structured_advisor_invalid_policy),
                "--structured-advisor-timeout-seconds",
                str(args.structured_advisor_timeout_seconds),
                "--structured-advisor-ttl-ms",
                str(args.structured_advisor_ttl_ms),
            ]
        )
    if args.pretty:
        argv.append("--pretty")
    return argv


def _validate_ai_review_source(args: argparse.Namespace) -> None:
    if args.mode == "live":
        if args.ai_review_json:
            raise ValueError("ai_review_static_json_forbidden_in_live")
        if args.ai_review_command:
            raise ValueError("ai_review_command_not_sandboxed_for_live")
    if args.ai_review_json and args.ai_review_command:
        raise ValueError("ai_review_source_conflict")
    structured_mode = getattr(
        args,
        "structured_advisor_mode",
        "off",
    )
    trial_contract = getattr(
        args,
        "deepseek_two_run_trial_contract",
        None,
    )
    trial_role = getattr(
        args,
        "deepseek_two_run_trial_role",
        None,
    )
    if bool(trial_contract) != bool(trial_role):
        if trial_contract:
            raise ValueError(
                "deepseek_two_run_trial_role_required"
            )
        raise ValueError(
            "deepseek_two_run_trial_contract_required"
        )
    structured_args = tuple(
        getattr(args, "structured_advisor_command_arg", [])
    )
    if structured_mode == "off":
        if trial_contract:
            raise ValueError(
                "deepseek_trial_requires_structured_advisor"
            )
        if structured_args or getattr(
            args,
            "structured_advisor_model_id",
            None,
        ):
            raise ValueError("structured_advisor_disabled_with_config")
    else:
        if args.mode == "live" and not trial_contract:
            raise ValueError(
                "deepseek_two_run_trial_contract_required"
            )
        if not structured_args:
            raise ValueError("structured_advisor_command_required")
        if not args.structured_advisor_model_id:
            raise ValueError("structured_advisor_model_id_required")
        if args.ai_review_json or args.ai_review_command:
            raise ValueError("legacy_and_structured_advisor_conflict")
        if args.state_context_provider != "main-menu-png":
            raise ValueError(
                "structured_advisor_requires_main_menu_provider"
            )
        if trial_contract:
            try:
                validated_trial = validate_deepseek_two_run_trial(
                    trial_contract,
                    role=trial_role,
                    run_session_id=args.run_session_id,
                    structured_advisor_mode=structured_mode,
                    structured_advisor_command=structured_args,
                    structured_advisor_model_id=(
                        args.structured_advisor_model_id
                    ),
                    structured_advisor_invalid_policy=(
                        args.structured_advisor_invalid_policy
                    ),
                    live=args.mode == "live",
                    adb_port=args.adb_port,
                    command_cwd=(
                        Path(structured_args[1]).parent.parent
                        if len(structured_args) == 2
                        else ROOT
                    ),
                )
            except DeepSeekTwoRunTrialError as exc:
                raise ValueError(str(exc)) from exc
            args.deepseek_two_run_trial_contract_sha256 = (
                validated_trial.contract_sha256
            )
            args.deepseek_two_run_trial_id = (
                validated_trial.trial_id
            )


def _resolved_condition_probe_policy(args: argparse.Namespace) -> ConditionProbePolicy:
    explicit = args.condition_probe_policy
    if args.enable_explicit_condition_probe:
        if explicit not in (None, ConditionProbePolicy.ALWAYS.value):
            raise ValueError("condition_probe_policy_conflict")
        return ConditionProbePolicy.ALWAYS
    return ConditionProbePolicy(explicit or ConditionProbePolicy.OFF.value)


def _validate_explicit_condition_probe(args: argparse.Namespace) -> None:
    if (
        not isinstance(args.condition_audit_interval, int)
        or isinstance(args.condition_audit_interval, bool)
        or not 1 <= args.condition_audit_interval <= 78
    ):
        raise ValueError("condition_audit_interval_invalid")
    policy = _resolved_condition_probe_policy(args)
    if args.condition_triggered_recovery:
        if policy is not ConditionProbePolicy.TRIGGERED:
            raise ValueError("condition_triggered_recovery_requires_triggered")
        if args.max_training_turns != 1:
            raise ValueError(
                "condition_triggered_recovery_max_training_turns_must_be_1"
            )
    if policy is ConditionProbePolicy.OFF:
        if args.mode == "live":
            raise ValueError("condition_probe_off_forbidden_in_live")
        return
    if args.mode != "live":
        raise ValueError("explicit_condition_probe_live_mode_required")
    if args.state_context_provider != "main-menu-png":
        raise ValueError("explicit_condition_probe_requires_main_menu_provider")
    if not args.condition_probe_confirm_token:
        raise ValueError("condition_probe_confirm_token_required")
    if args.adb_port != ADB_PORT:
        raise ValueError(f"condition_probe_adb_port_must_be_{ADB_PORT}")


def _triggered_condition_failure(
    args: argparse.Namespace, reason: str
) -> tuple[dict[str, Any], int]:
    report = {
        "is_valid": False,
        "status": "stopped",
        "run_session_id": args.run_session_id,
        "completed_training_turns": 0,
        "requested_training_turns": args.max_training_turns,
        "stop_reason": reason,
        "turn_reports": [],
    }
    _add_action_accounting(report, requested=args.max_training_turns, completed=0)
    return report, 2


@dataclass(frozen=True)
class _ConditionTurnAuthority:
    report_path: Path
    binding_key: str
    summary: dict[str, Any]
    provider_source: dict[str, Any]


class _ConditionGateFailure(RuntimeError):
    pass


def _is_race_day_main_menu(source_path: str) -> bool:
    """Detect a race-day main menu without touching the device."""

    try:
        from core.auto_trainer.safe_advance import SafeAdvancePlanner

        plan = SafeAdvancePlanner().plan(source_path)
    except Exception:
        return False
    return plan.get("reason") == "race_day_main_menu"


class _ConditionTurnCoordinator:
    """Materialize one current-turn condition authority without UI inference."""

    def __init__(
        self,
        *,
        args: argparse.Namespace,
        tracker: ConditionTracker,
        tracker_store: ConditionTrackerStore,
        authority_cache: ConditionAuthorityCache,
        resync_provider: Callable[[dict[str, Any], Path], dict[str, Any]] | None = None,
    ) -> None:
        self.args = args
        self.tracker = tracker
        self.tracker_store = tracker_store
        self.authority_cache = authority_cache
        self.resync_provider = resync_provider or (
            lambda source, destination: _capture_condition_resync_source(
                self.args, source, destination
            )
        )

    def prepare(
        self,
        *,
        current_provider_source: dict[str, Any],
        turn_root: Path,
        turn_id: str,
        turn_index: int,
        critical_race: bool,
        recovery_reason: str | None,
    ) -> _ConditionTurnAuthority:
        source_path = current_provider_source.get("path")
        source_sha256 = current_provider_source.get("sha256")
        if not isinstance(source_path, str) or not isinstance(source_sha256, str):
            raise _ConditionGateFailure("condition_quick_snapshot_unstable")
        try:
            snapshot = _parse_condition_quick_snapshot(
                current_provider_source,
                turn_root / "condition_gate" / "main_menu_state.provider.json",
                turn_index,
            )
        except (OSError, ValueError, RuntimeError):
            time.sleep(
                self.args.ready_recheck_delay_seconds
                if self.args.ready_recheck_delay_seconds > 0
                else 1
            )
            try:
                resynced_source = self.resync_provider(
                    current_provider_source,
                    turn_root / "condition_gate" / "resync_capture",
                )
                if resynced_source.get("path") == source_path:
                    raise ValueError("condition_resync_source_not_fresh")
                snapshot = _parse_condition_quick_snapshot(
                    resynced_source,
                    turn_root / "condition_gate" / "main_menu_state.provider.resync.json",
                    turn_index,
                )
                current_provider_source = resynced_source
                source_path = resynced_source["path"]
                source_sha256 = resynced_source["sha256"]
            except (OSError, ValueError, RuntimeError):
                snapshot = None
        if snapshot is None:
            self.tracker.mark_dirty()
            if _is_race_day_main_menu(source_path):
                # Race-day menus do not render the mood/vitality panel needed
                # for a quick snapshot.  A fresh probe would fail the same way,
                # so bypass the gate conservatively and let the race route run.
                summary = {
                    "schema_version": "condition_turn_summary.v1",
                    "policy": ConditionProbePolicy.TRIGGERED.value,
                    "status": "race_day_bypass",
                    "dirty": True,
                    "probe_performed": False,
                    "probe_reasons": [],
                    "last_explicit_probe_turn": self.tracker.last_explicit_probe_turn,
                    "next_periodic_due_turn": (
                        (self.tracker.last_explicit_probe_turn or 0)
                        + self.tracker.audit_interval
                        if self.tracker.audit_interval
                        else None
                    ),
                    "report_sha256": self.tracker.report_sha256,
                    "source_frame_sha256": self.tracker.source_frame_sha256,
                    "authority_id": None,
                    "provider_source_sha256": source_sha256,
                    "authority_rebound": False,
                    "recovery_reason": recovery_reason,
                }
                turn_root.mkdir(parents=True, exist_ok=True)
                _write_condition_turn_summary(turn_root, summary)
                return _ConditionTurnAuthority(
                    report_path=None,  # type: ignore[arg-type]
                    binding_key=None,  # type: ignore[arg-type]
                    summary=summary,
                    provider_source={
                        "path": source_path,
                        "sha256": source_sha256,
                        "consensus_paths": tuple(
                            current_provider_source.get("consensus_paths", ())
                        ),
                    },
                )
            if not critical_race:
                raise _ConditionGateFailure("condition_quick_snapshot_unstable")
        else:
            self.tracker.observe_main_menu(snapshot)
        decision = decide_condition_probe(
            ConditionProbePolicy.TRIGGERED,
            self.tracker,
            turn_index,
            critical_race,
        )
        binding_key = secrets.token_hex(32)
        probe_performed = decision.required
        authority_id: str | None = self.tracker.report_sha256

        def apply_fresh_probe() -> tuple[Path, str]:
            report_path, _probe_report, code = _run_explicit_condition_probe(
                self.args,
                turn_root,
                turn_id=turn_id,
                turn_index=turn_index,
                formal_condition_binding_key=binding_key,
            )
            if code != 0 or report_path is None:
                raise _ConditionGateFailure("condition_probe_failed")
            try:
                formal = load_explicit_condition_context(
                    report_path,
                    required_turn_root=turn_root,
                    formal_live=True,
                    formal_binding_key=binding_key,
                    expected_turn_id=turn_id,
                    expected_turn_index=turn_index,
                )
                source = formal["metadata"]["source"]["condition_source_sha256"]
                assessment = load_condition_assessment(
                    report_path=report_path,
                    artifact_path=report_path.parent / "ability_details.png",
                    expected_source_sha256=source,
                )
                if assessment["status"] == ConditionStatus.UNKNOWN.value:
                    raise ValueError("condition_assessment_unknown")
                self.tracker.apply_assessment(assessment, turn_index)
                cached = self.authority_cache.store_verified(
                    report_path=report_path,
                    artifact_path=report_path.parent / "ability_details.png",
                    assessment=assessment,
                )
            except (KeyError, OSError, ValueError) as exc:
                raise _ConditionGateFailure("condition_probe_failed") from exc
            return report_path, cached.authority_id

        if decision.required:
            report_path, authority_id = apply_fresh_probe()
        else:
            if (
                self.tracker.dirty
                or self.tracker.status is ConditionStatus.UNKNOWN
                or not self.tracker.report_sha256
            ):
                raise _ConditionGateFailure("condition_authority_unavailable")
            authority_id = self.tracker.report_sha256
            negative_recheck = (
                self.args.condition_triggered_recovery
                and self.tracker.status is ConditionStatus.NEGATIVE
                and self.tracker.last_explicit_probe_turn is not None
                and self.tracker.last_explicit_probe_turn < turn_index
            )
            if negative_recheck:
                try:
                    self.authority_cache.validate(authority_id)
                except ValueError as exc:
                    if str(exc) != "condition_authority_missing":
                        raise _ConditionGateFailure(
                            "condition_authority_unavailable"
                        ) from exc
                    recovery_reason = "condition_authority_missing"
                except OSError as exc:
                    raise _ConditionGateFailure(
                        "condition_authority_unavailable"
                    ) from exc
                else:
                    recovery_reason = "condition_negative_recheck"
                report_path, authority_id = apply_fresh_probe()
                probe_performed = True
            else:
                try:
                    materialized = self.authority_cache.materialize_formal_context(
                        authority_id=authority_id,
                        turn_root=turn_root,
                        turn_id=turn_id,
                        turn_index=turn_index,
                        binding_key=binding_key,
                    )
                    report_path = materialized.report_path
                except ValueError as exc:
                    if not (
                        self.args.condition_triggered_recovery
                        and str(exc) == "condition_authority_missing"
                    ):
                        raise _ConditionGateFailure(
                            "condition_authority_unavailable"
                        ) from exc
                    report_path, authority_id = apply_fresh_probe()
                    probe_performed = True
                    recovery_reason = "condition_authority_missing"
                except OSError as exc:
                    raise _ConditionGateFailure(
                        "condition_authority_unavailable"
                    ) from exc
        next_due = None
        if self.tracker.audit_interval:
            next_due = (self.tracker.last_explicit_probe_turn or 0) + self.tracker.audit_interval
        summary = {
            "schema_version": "condition_turn_summary.v1",
            "policy": ConditionProbePolicy.TRIGGERED.value,
            "status": self.tracker.status.value,
            "dirty": self.tracker.dirty,
            "probe_performed": probe_performed,
            "probe_reasons": list(decision.reasons),
            "last_explicit_probe_turn": self.tracker.last_explicit_probe_turn,
            "next_periodic_due_turn": next_due,
            "report_sha256": self.tracker.report_sha256,
            "source_frame_sha256": self.tracker.source_frame_sha256,
            "authority_id": authority_id,
            "provider_source_sha256": source_sha256,
            "authority_rebound": not probe_performed,
            "recovery_reason": recovery_reason,
        }
        turn_root.mkdir(parents=True, exist_ok=True)
        _write_condition_turn_summary(turn_root, summary)
        return _ConditionTurnAuthority(
            report_path=report_path,
            binding_key=binding_key,
            summary=summary,
            provider_source={
                "path": source_path,
                "sha256": source_sha256,
                "consensus_paths": tuple(
                    current_provider_source.get("consensus_paths", ())
                ),
            },
        )


def _prepare_condition_turn_authority(
    *,
    args: argparse.Namespace,
    tracker: ConditionTracker,
    tracker_store: ConditionTrackerStore,
    authority_cache: ConditionAuthorityCache,
    current_provider_source: dict[str, Any],
    turn_root: Path,
    turn_id: str,
    turn_index: int,
    critical_race: bool,
    recovery_reason: str | None,
    control_driver: CooperativeRunnerDriver | None = None,
) -> _ConditionTurnAuthority:
    return _ConditionTurnCoordinator(
        args=args,
        tracker=tracker,
        tracker_store=tracker_store,
        authority_cache=authority_cache,
        resync_provider=lambda source, destination: _capture_condition_resync_source(
            args,
            source,
            destination,
            control_driver=control_driver,
        ),
    ).prepare(
        current_provider_source=current_provider_source,
        turn_root=turn_root,
        turn_id=turn_id,
        turn_index=turn_index,
        critical_race=critical_race,
        recovery_reason=recovery_reason,
    )


def _save_condition_tracker_after_action(
    *,
    args: argparse.Namespace,
    tracker: ConditionTracker,
    tracker_store: ConditionTrackerStore,
    turn_index: int,
    summary: dict[str, Any] | None,
) -> None:
    reasons = ()
    if isinstance(summary, dict):
        raw_reasons = summary.get("probe_reasons", [])
        if isinstance(raw_reasons, list):
            reasons = tuple(ConditionProbeReason(reason) for reason in raw_reasons)
    tracker_store.save(
        run_id=Path(args.dashboard_run_dir).name,
        last_committed_turn=turn_index,
        tracker=tracker,
        probe_reasons=reasons,
    )


def _write_condition_turn_summary(turn_root: Path, summary: dict[str, Any]) -> None:
    """Atomically publish the summary consumed by dashboard archival."""
    turn_root.mkdir(parents=True, exist_ok=True)
    destination = turn_root / "condition_turn_summary.json"
    payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, destination)
        if destination.read_text(encoding="utf-8") != payload:
            raise OSError("condition_turn_summary_atomic_verify_failed")
    finally:
        temporary.unlink(missing_ok=True)


def _apply_condition_route_risk_and_persist(
    *,
    args: argparse.Namespace,
    tracker: ConditionTracker,
    tracker_store: ConditionTrackerStore,
    turn_index: int,
    turn_root: Path,
    turn_entry: dict[str, Any],
    summary: dict[str, Any] | None,
) -> None:
    """Persist conservative event-chain invalidation before dashboard archival."""
    if not apply_event_chain_risk(tracker, turn_entry.get("route_steps", [])):
        return
    if summary is not None:
        summary["dirty"] = True
        _write_condition_turn_summary(turn_root, summary)
    _save_condition_tracker_after_action(
        args=args,
        tracker=tracker,
        tracker_store=tracker_store,
        turn_index=turn_index,
        summary=summary,
    )


def _apply_pre_route_condition_risk_and_persist(
    *,
    args: argparse.Namespace,
    tracker: ConditionTracker,
    tracker_store: ConditionTrackerStore,
    pre_route_report: dict[str, Any],
) -> None:
    """Persist pre-action event risk against the last genuinely committed turn."""
    if not apply_event_chain_risk(tracker, pre_route_report.get("route_steps", [])):
        return
    prior_committed_turn = args.start_turn_index - 1
    if prior_committed_turn < 0:
        raise ValueError("condition_pre_route_prior_turn_invalid")
    _save_condition_tracker_after_action(
        args=args,
        tracker=tracker,
        tracker_store=tracker_store,
        turn_index=prior_committed_turn,
        summary=None,
    )


def _advisor_source(args: argparse.Namespace) -> str:
    if getattr(args, "structured_advisor_mode", "off") != "off":
        return f"structured_{args.structured_advisor_mode}"
    if args.ai_review_command:
        return "command"
    if args.ai_review_json:
        return "static_dry_run"
    return "none"


def _validate_state_context_configuration(args: argparse.Namespace) -> None:
    if args.mode == "live" and args.state_context_provider != "main-menu-png":
        raise ValueError("live_requires_main_menu_state_provider")
    if args.state_context_provider:
        return
    if args.state_context_json:
        if args.max_training_turns > 1:
            raise ValueError(
                "static_state_context_requires_max_training_turns_1: "
                "use --state-context-pattern with {turn_index}"
            )
        return

    pattern = str(args.state_context_pattern)
    try:
        parsed_fields = list(Formatter().parse(pattern))
    except ValueError as exc:
        raise ValueError(f"state_context_pattern_invalid:{exc}") from exc

    replacement_fields = [item for item in parsed_fields if item[1] is not None]
    if not replacement_fields:
        raise ValueError("state_context_pattern_requires_turn_index_placeholder")
    for _literal_text, field_name, format_spec, conversion in replacement_fields:
        if field_name != "turn_index":
            raise ValueError(f"state_context_pattern_invalid_field:{field_name}")
        if conversion is not None:
            raise ValueError("state_context_pattern_conversion_not_allowed")
        if format_spec:
            raise ValueError("state_context_pattern_format_spec_not_allowed")

    first_path = _normalized_state_context_pattern_path(pattern, args.start_turn_index)
    next_path = _normalized_state_context_pattern_path(pattern, args.start_turn_index + 1)
    if first_path == next_path:
        raise ValueError(
            "state_context_pattern_resolves_to_same_path:"
            f"{args.start_turn_index}:{args.start_turn_index + 1}:{first_path}"
        )


def _normalized_state_context_pattern_path(pattern: str, turn_index: int) -> Path:
    try:
        formatted = pattern.format(turn_index=turn_index)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"state_context_pattern_invalid:{exc}") from exc
    return Path(formatted).expanduser().resolve(strict=False)


def _state_context_path_for_turn(args: argparse.Namespace, turn_index: int) -> str:
    if args.state_context_pattern:
        try:
            return str(Path(str(args.state_context_pattern).format(turn_index=turn_index)))
        except (KeyError, ValueError) as exc:
            raise ValueError(f"state_context_pattern_invalid:{exc}") from exc
    return str(Path(args.state_context_json))


def _uses_state_context_provider(args: argparse.Namespace) -> bool:
    return args.state_context_provider == "main-menu-png"


def _ready_report_explicitly_rejects_provider_source(report: dict[str, Any]) -> bool:
    step_reports = report.get("step_reports")
    if not isinstance(step_reports, list) or not step_reports:
        return False
    last_step = step_reports[-1]
    if not isinstance(last_step, dict):
        return False
    result = last_step.get("result")
    decision = result.get("decision") if isinstance(result, dict) else None
    evidence = decision.get("evidence") if isinstance(decision, dict) else None
    atomic_state = (
        evidence.get("atomic_main_menu_state")
        if isinstance(evidence, dict)
        else None
    )
    return bool(
        isinstance(atomic_state, dict)
        and atomic_state.get("valid_for_training") is False
    )


def _fresh_provider_source(
    route_report: dict[str, Any],
    seen_sources: set[tuple[str, str]],
) -> dict[str, Any]:
    route_steps = route_report.get("route_steps")
    if not isinstance(route_steps, list):
        raise ValueError("state_context_provider_source_missing")
    ready_sources: list[tuple[str, str]] = []
    for step in reversed(route_steps):
        if not isinstance(step, dict):
            continue
        report = step.get("report")
        if not isinstance(report, dict) or report.get("stop_reason") != "ready_main_menu":
            continue
        if _ready_report_explicitly_rejects_provider_source(report):
            continue
        source_path = report.get("source_screenshot_path")
        if not isinstance(source_path, str) or not source_path:
            raise ValueError("state_context_provider_source_missing")
        path = Path(source_path)
        if not path.is_file():
            raise ValueError("state_context_provider_source_missing")
        source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        source = (str(path), source_sha256)
        if any(existing_path == source[0] for existing_path, _existing_sha in ready_sources):
            raise ValueError("state_context_provider_source_duplicate_path")
        if any(existing_sha == source[1] for _existing_path, existing_sha in ready_sources):
            raise ValueError("state_context_provider_source_duplicate_sha")
        if any(existing_path == source[0] for existing_path, _existing_sha in seen_sources):
            raise ValueError("state_context_provider_source_duplicate_path")
        if source in seen_sources:
            raise ValueError("state_context_provider_source_duplicate_path")
        if any(existing_sha == source_sha256 for _existing_path, existing_sha in seen_sources):
            raise ValueError("state_context_provider_source_duplicate_sha")
        ready_sources.append(source)
    if not ready_sources:
        raise ValueError("state_context_provider_source_missing")
    seen_sources.update(ready_sources)
    primary_path, primary_sha256 = ready_sources[0]
    return {
        "path": primary_path,
        "sha256": primary_sha256,
        "consensus_paths": [path for path, _sha256 in ready_sources[1:]],
    }


def _parse_condition_quick_snapshot(
    provider_source: dict[str, Any], provider_path: Path, turn_index: int
) -> Any:
    source_path = provider_source.get("path")
    source_sha256 = provider_source.get("sha256")
    if not isinstance(source_path, str) or not isinstance(source_sha256, str):
        raise ValueError("condition_quick_snapshot_invalid")
    provider_report = provide_main_menu_state(
        source_path,
        provider_path,
        turn_index=turn_index,
        consensus_png_paths=provider_source.get("consensus_paths", ()),
    )
    return build_condition_quick_snapshot(
        provider_report,
        expected_turn=turn_index,
        expected_source_sha256=source_sha256,
    )


def _capture_condition_resync_source(
    args: argparse.Namespace,
    prior_source: dict[str, Any],
    capture_root: Path,
    *,
    control_driver: CooperativeRunnerDriver | None = None,
) -> dict[str, Any]:
    """Take exactly one read-only capture; never reuse the previous source path."""
    argv = _safe_advance_argv(args, capture_root, transition_resync=True)
    max_steps_index = argv.index("--max-steps") + 1
    argv[max_steps_index] = "1"
    if "--execute" in argv:
        argv.remove("--execute")
    if "--confirm-token" in argv:
        token_index = argv.index("--confirm-token")
        del argv[token_index : token_index + 2]
    report, code = (
        _invoke_safe_advance(argv)
        if control_driver is None
        else _invoke_safe_advance(argv, control_driver=control_driver)
    )
    if (
        code != 0
        or report.get("is_valid") is not True
        or report.get("stop_reason") != "ready_main_menu"
        or report.get("performed_steps") != 0
    ):
        raise ValueError("condition_resync_capture_failed")
    source = _provider_source_from_route_step(report)
    if source["path"] == prior_source.get("path"):
        raise ValueError("condition_resync_source_not_fresh")
    return source


def _provider_source_failure(
    args: argparse.Namespace,
    turn_reports: list[dict[str, Any]],
    pre_route_report: dict[str, Any] | None,
    stop_reason: str,
) -> tuple[dict[str, Any], int]:
    report: dict[str, Any] = {
        "is_valid": False,
        "status": "stopped",
        "run_session_id": args.run_session_id,
        "completed_training_turns": 0,
        "requested_training_turns": args.max_training_turns,
        "stop_reason": stop_reason,
        "turn_reports": turn_reports,
    }
    if pre_route_report is not None:
        report["pre_route"] = pre_route_report
    _add_action_accounting(
        report,
        requested=args.max_training_turns,
        completed=0,
    )
    return report, 2


def _add_action_accounting(
    report: dict[str, Any],
    *,
    requested: int,
    completed: int,
) -> None:
    report["requested_action_turns"] = requested
    report["completed_action_turns"] = completed
    report["remaining_action_turns"] = max(0, requested - completed)
    evidence_sha256 = _last_evidence_sha256(report)
    if evidence_sha256 is not None:
        report["last_evidence_sha256"] = evidence_sha256


def _last_evidence_sha256(value: Any) -> str | None:
    candidates: list[str] = []
    evidence_keys = {
        "source_image_sha256",
        "source_screenshot_sha256",
        "frame_sha256",
        "turn_packet_sha256",
    }

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if (
                    key in evidence_keys
                    and isinstance(child, str)
                    and len(child) == 64
                    and all(char in "0123456789abcdef" for char in child)
                ):
                    candidates.append(child)
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return candidates[-1] if candidates else None


def _provider_source_from_route_step(report: dict[str, Any]) -> dict[str, Any]:
    """Bind a race-day probe to the route capture that detected that race."""
    source_path = report.get("source_screenshot_path")
    if (
        (not isinstance(source_path, str) or not source_path)
        and report.get("stop_reason") == "race_day_main_menu"
    ):
        step_reports = report.get("step_reports")
        if isinstance(step_reports, list) and step_reports:
            last_step = step_reports[-1]
            if isinstance(last_step, dict):
                source_path = last_step.get("screenshot_path")
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("state_context_provider_source_missing")
    path = Path(source_path)
    if not path.is_file():
        raise ValueError("state_context_provider_source_missing")
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "consensus_paths": (),
    }


def _resolve_state_context_path(args: argparse.Namespace, turn_index: int) -> Path:
    path = Path(_state_context_path_for_turn(args, turn_index))
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ValueError(f"state_context_missing_for_turn:{turn_index}:{path}") from exc
    except OSError as exc:
        raise ValueError(f"state_context_unreadable_for_turn:{turn_index}:{path}:{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"state_context_invalid_for_turn:{turn_index}:{path}:{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"state_context_root_not_object_for_turn:{turn_index}:{path}")
    return path


READY_ROUTE_STOPS = {"ready_main_menu", "ready_training_menu"}
TERMINAL_ROUTE_STOPS = {"team_formation", "career_complete"}
TERMINAL_HANDOFF_ROUTE_STOPS = {"skill_acquisition_handoff"}


def _requires_critical_race_authority(route_stop_reason: str) -> bool:
    return route_stop_reason == "race_day_main_menu"


def _start_screen_from_route_stop(route_stop_reason: str) -> str | None:
    if route_stop_reason == "ready_main_menu":
        return "main-menu"
    if route_stop_reason == "ready_training_menu":
        return "training-menu"
    return None


def _load_run_trainee_identity_authority(
    path: Path,
    *,
    expected_run_session_id: str,
) -> RunTraineeIdentityAuthority:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise
    authority = RunTraineeIdentityAuthority.from_dict(payload)
    if authority.run_session_id != expected_run_session_id:
        raise ValueError("run_trainee_authority_session_mismatch")
    return authority


def _attach_prior_event_context_shadow(
    *,
    turn_entry: dict[str, Any],
    run_session_id: str,
    global_action_turn: int,
    turn_root: Path,
    one_turn_report: dict[str, Any],
) -> PriorTurnEventContextAuthority | None:
    try:
        authority = build_prior_turn_event_context_authority(
            run_session_id=run_session_id,
            global_action_turn=global_action_turn,
            turn_root=turn_root,
            one_turn_report=one_turn_report,
        )
    except PriorTurnEventContextError as exc:
        turn_entry["prior_event_context_shadow"] = {
            "schema_version": "uma.prior_turn_event_context_shadow_slot.v1",
            "status": "abstain",
            "reason": exc.code,
            "action_eligible": False,
            "usable_for_action": False,
        }
        return None
    turn_entry["prior_event_context_shadow"] = authority.to_dict()
    return authority


def _route_between_turns(
    args: argparse.Namespace,
    turn_root: Path,
    turn_report: dict[str, Any],
    *,
    current_turn_index: int | None = None,
    transition_resync: bool = False,
    bootstrap_state: str | None = None,
    control_driver: CooperativeRunnerDriver | None = None,
    condition_race_guard: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
    prior_event_context: PriorTurnEventContextAuthority | None = None,
    run_trainee_authority: RunTraineeIdentityAuthority | None = None,
    consumed_terminal_binding: dict[str, Any] | None = None,
) -> tuple[bool, str, int]:
    if args.route_max_cycles <= 0:
        raise ValueError("route_max_cycles_must_be_positive")
    finish_after_skill = args.allow_career_end_finish_after_skill_handoff
    if finish_after_skill and consumed_terminal_binding is None:
        return False, "career_end_finish_requires_consumed_terminal_resume", 2
    turn_report["skip_session_owner"] = "safe_advance"
    resync_next_advance = transition_resync
    bootstrap_next_advance = bootstrap_state

    for _cycle in range(args.route_max_cycles):
        advance_transition_resync = resync_next_advance
        wbc_resync_handoff = False
        advance_dir = _next_route_artifact_dir(turn_root, turn_report, "advance")
        advance_argv = _safe_advance_argv(
            args,
            advance_dir,
            career_goal_observed_turn=current_turn_index,
            transition_resync=advance_transition_resync,
            bootstrap_state=bootstrap_next_advance,
            finish_after_skill_handoff=finish_after_skill and _cycle == 0,
            expected_career_end_skill_points=(
                consumed_terminal_binding.get(
                    "external_terminal_skill_expected_remaining_points"
                )
                if finish_after_skill
                and _cycle == 0
                and consumed_terminal_binding is not None
                else None
            ),
        )
        resync_next_advance = False
        bootstrap_next_advance = None
        advance_report, advance_code = (
            _invoke_safe_advance(advance_argv)
            if control_driver is None
            else _invoke_safe_advance(advance_argv, control_driver=control_driver)
        )
        _record_route_step(turn_report, "advance", advance_code, advance_report)
        if "advance" not in turn_report:
            turn_report["advance"] = {"code": advance_code, "report": advance_report}
        if advance_code != 0 or not advance_report.get("is_valid", False):
            return False, _safe_advance_failure_reason(advance_report), 2
        advance_stop = str(advance_report.get("stop_reason"))
        if advance_stop in READY_ROUTE_STOPS:
            if not args.verify_ready_after_advance:
                return True, advance_stop, 0
            if args.ready_recheck_delay_seconds > 0:
                time.sleep(args.ready_recheck_delay_seconds)
            verify_dir = _next_route_artifact_dir(turn_root, turn_report, "advance_ready_verify")
            verify_argv = _safe_advance_argv(
                args,
                verify_dir,
                career_goal_observed_turn=current_turn_index,
                transition_resync=advance_transition_resync,
            )
            verify_report, verify_code = (
                _invoke_safe_advance(verify_argv)
                if control_driver is None
                else _invoke_safe_advance(verify_argv, control_driver=control_driver)
            )
            _record_route_step(turn_report, "advance_ready_verify", verify_code, verify_report)
            if verify_code != 0 or not verify_report.get("is_valid", False):
                return False, _safe_advance_failure_reason(verify_report), 2
            verify_stop = str(verify_report.get("stop_reason"))
            if verify_stop in READY_ROUTE_STOPS:
                return True, verify_stop, 0
            advance_stop = verify_stop
            advance_report = verify_report
        # SafeAdvance may preserve its classifier namespace in the stop reason
        # (``unsupported_screen:event_choice``).  The route controller owns
        # the event-choice handoff, so normalize that equivalent reason before
        # dispatching instead of bouncing back to the training loop.
        if advance_stop == "unsupported_screen:event_choice":
            advance_stop = "event_choice"
        elif advance_stop == "unsupported_screen:race_participant_list":
            advance_stop = "race_list"
        elif advance_stop in {
            "route_state_out_of_scope",
            "route_action_out_of_scope",
        }:
            step_reports = advance_report.get("step_reports")
            last_result = (
                step_reports[-1].get("result")
                if isinstance(step_reports, list)
                and step_reports
                and isinstance(step_reports[-1], dict)
                else None
            )
            decision = (
                last_result.get("decision")
                if isinstance(last_result, dict)
                and last_result.get("reason") == advance_stop
                else None
            )
            tap = decision.get("tap") if isinstance(decision, dict) else None
            action_id = (
                tap.get("coordinate_name") if isinstance(tap, dict) else None
            )
            if (
                isinstance(decision, dict)
                and decision.get("route_state") == "event_choice"
            ):
                advance_stop = "event_choice"
            elif (
                isinstance(decision, dict)
                and decision.get("action") == "tap"
                and decision.get("reason") == "continue_race_result"
                and action_id == "safe_advance.race_result_continue"
                and isinstance(decision.get("evidence"), dict)
                and isinstance(
                    decision["evidence"].get("perception"),
                    dict,
                )
                and decision["evidence"]["perception"].get("screen")
                == "race_result_ready"
            ):
                advance_stop = "race_scene"
            elif (
                isinstance(decision, dict)
                and decision.get("action") == "stop"
                and decision.get("reason") == "wbc_race_hub"
                and decision.get("tap") is None
            ):
                # A restarted supervisor may bootstrap from the expected main
                # menu while the device is already at the WBC hub.  Use the
                # out-of-scope stop only to request one non-executing,
                # race-list-scoped recapture; dispatch still requires that
                # fresh report and its persisted same-frame route receipt.
                resync_dir = _next_route_artifact_dir(
                    turn_root,
                    turn_report,
                    "advance_wbc_resync",
                )
                resync_argv = _safe_advance_argv(
                    args,
                    resync_dir,
                    career_goal_observed_turn=current_turn_index,
                    transition_resync=True,
                    bootstrap_state="race-list",
                )
                resync_argv[resync_argv.index("--max-steps") + 1] = "1"
                if "--execute" in resync_argv:
                    resync_argv.remove("--execute")
                if "--confirm-token" in resync_argv:
                    token_index = resync_argv.index("--confirm-token")
                    del resync_argv[token_index : token_index + 2]
                resync_report, resync_code = (
                    _invoke_safe_advance(resync_argv)
                    if control_driver is None
                    else _invoke_safe_advance(
                        resync_argv,
                        control_driver=control_driver,
                    )
                )
                _record_route_step(
                    turn_report,
                    "advance_wbc_resync",
                    resync_code,
                    resync_report,
                )
                if resync_code != 0 or not resync_report.get("is_valid", False):
                    return False, _safe_advance_failure_reason(resync_report), 2
                if str(resync_report.get("stop_reason")) != "wbc_race_hub":
                    return False, "wbc_resync:page_mismatch", 2
                advance_report = resync_report
                advance_stop = "wbc_race_hub"
                wbc_resync_handoff = True
            elif (
                isinstance(decision, dict)
                and (is_route_handoff(decision, reason="race_list")
                     or (decision.get("action") == "stop" and decision.get("reason") == "race_detail"))
                and decision.get("tap") is None
                and isinstance(decision.get("evidence"), dict)
                and isinstance(
                    decision["evidence"].get("perception"),
                    dict,
                )
                and decision["evidence"]["perception"].get("screen")
                == "race_participant_list"
            ):
                advance_stop = "race_list"
            elif (
                isinstance(decision, dict)
                and (
                    decision.get("route_state")
                    in {
                        "race_list",
                        "race_detail",
                        "race_detail_confirm",
                        "race_menu",
                        "race_participant_list",
                    }
                    or (
                        decision.get("reason") == "go_to_race_modal"
                        and action_id == "safe_advance.go_to_race"
                    )
                )
            ):
                advance_stop = "race_list"
            elif (
                isinstance(decision, dict)
                and decision.get("action") == "wait"
                and decision.get("reason") == "bottom_skip_double_enabled"
                and decision.get("route_state") == "auto_skip_wait"
            ):
                time.sleep(max(0.0, float(args.settle_seconds)))
                continue
            elif (
                args.allow_unbound_goal_summary_continue
                and isinstance(decision, dict)
                and decision.get("action") == "stop"
                and decision.get("reason")
                == "career_goal_observation_required"
            ):
                exception_dir = _next_route_artifact_dir(
                    turn_root,
                    turn_report,
                    "unbound_goal_summary",
                )
                exception_argv = _unbound_goal_summary_argv(
                    args,
                    exception_dir,
                )
                exception_report, exception_code = (
                    _invoke_safe_advance(exception_argv)
                    if control_driver is None
                    else _invoke_safe_advance(
                        exception_argv,
                        control_driver=control_driver,
                    )
                )
                _record_route_step(
                    turn_report,
                    "unbound_goal_summary",
                    exception_code,
                    exception_report,
                )
                exception_steps = exception_report.get("step_reports")
                exception_result = (
                    exception_steps[-1].get("result")
                    if isinstance(exception_steps, list)
                    and exception_steps
                    and isinstance(exception_steps[-1], dict)
                    else None
                )
                exception_receipt = (
                    exception_result.get("goal_summary_exception")
                    if isinstance(exception_result, dict)
                    else None
                )
                if not (
                    exception_code == 0
                    and exception_report.get("is_valid") is True
                    and exception_report.get("stop_reason")
                    == "max_steps_reached"
                    and exception_report.get("performed_steps") == 1
                    and isinstance(exception_result, dict)
                    and exception_result.get("performed") is True
                    and isinstance(exception_receipt, dict)
                    and exception_receipt.get("matched_reason")
                    in {
                        "goal_text_order_unverified",
                        "goal_remaining_unbound_multi_goal_summary",
                    }
                    and exception_receipt.get("ledger_written") is False
                ):
                    return False, "unbound_goal_summary_exception_failed", 2
                resync_next_advance = True
                bootstrap_next_advance = "dialogue"
                continue
        if advance_stop == "career_complete":
            steps = advance_report.get("step_reports")
            result = steps[-1].get("result", {}) if isinstance(steps, list) and steps and isinstance(steps[-1], dict) else {}
            decision = result.get("decision", {})
            gate = result.get("safety_verdict", {})
            packet = result.get("frame_safety", {})
            post = result.get("post_tap_frame", {})
            transition = advance_report.get("route_transition", {})
            try:
                action_id = "safe_advance.support_reward_next_page"
                source = Path(decision["evidence"]["image"])
                source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
                post_sha = hashlib.sha256(Path(post["path"]).read_bytes()).hexdigest()
                verified = (
                    advance_report.get("status") == "stopped"
                    and result.get("performed") is True and result.get("tap_performed") is True
                    and result.get("reason") == "career_complete"
                    and decision.get("action") == "tap" and decision.get("reason") == "next_support_reward_page"
                    and decision["tap"].get("coordinate_name") == action_id
                    and decision.get("post_tap_confirmation") == "career_complete_global_home"
                    and gate.get("allowed") is True and gate.get("action_id") == action_id
                    and gate.get("source_image_sha256") == packet.get("source_image_sha256") == source_sha
                    and post.get("sha256") == post_sha and post_sha != source_sha
                    and transition.get("run_session_id") == args.run_session_id
                    and transition.get("last_action_id") == action_id
                    and transition.get("action_source_sha256") == source_sha
                )
            except (OSError, KeyError, TypeError, AttributeError):
                verified = False
            if not verified:
                return False, "career_complete_receipt_unverified", 2
            return True, "career_complete", 0
        if advance_stop in TERMINAL_ROUTE_STOPS:
            return True, advance_stop, 0
        if advance_stop in TERMINAL_HANDOFF_ROUTE_STOPS:
            try:
                handoff = inspect_terminal_skill_handoff(
                    advance_report,
                    allowed_root=turn_root,
                )
            except TerminalSkillHandoffError as exc:
                return False, f"terminal_skill_handoff:{exc.code}", 2
            handoff_payload = handoff.to_dict()
            handoff_path = turn_root / "terminal_skill_handoff.json"
            try:
                _write_canonical_json_atomic(handoff_path, handoff_payload)
            except OSError:
                return False, "terminal_skill_handoff:artifact_conflict", 2
            turn_report["terminal_skill_handoff"] = {
                **handoff_payload,
                "artifact_path": str(handoff_path.resolve()),
                "artifact_sha256": hashlib.sha256(
                    handoff_path.read_bytes()
                ).hexdigest(),
            }
            return True, advance_stop, 0
        if (
            advance_stop == "global_route_interrupt_performed"
            and int(advance_report.get("performed_steps") or 0) > 0
        ):
            transition = advance_report.get("route_transition")
            if not isinstance(transition, dict) or not isinstance(
                transition.get("last_action_id"), str
            ):
                return False, "global_route_interrupt_receipt_missing", 2
            step_reports = advance_report.get("step_reports")
            last_result = (
                step_reports[-1].get("result")
                if isinstance(step_reports, list)
                and step_reports
                and isinstance(step_reports[-1], dict)
                else None
            )
            decision = (
                last_result.get("decision")
                if isinstance(last_result, dict)
                and last_result.get("performed") is True
                else None
            )
            if not isinstance(decision, dict) or decision.get("action") not in {
                "tap",
                "wait",
            }:
                return False, "global_route_interrupt_receipt_missing", 2
            if decision.get("action") == "tap":
                tap = decision.get("tap")
                action_id = (
                    tap.get("coordinate_name") if isinstance(tap, dict) else None
                )
                if (
                    not isinstance(action_id, str)
                    or transition.get("last_action_id") != action_id
                ):
                    return False, "global_route_interrupt_receipt_mismatch", 2
                frame_safety = last_result.get("frame_safety")
                source_sha256 = (
                    frame_safety.get("source_image_sha256")
                    if isinstance(frame_safety, dict)
                    else None
                )
                if (
                    not isinstance(source_sha256, str)
                    or transition.get("action_source_sha256") != source_sha256
                ):
                    return False, "global_route_interrupt_receipt_sha_mismatch", 2
            else:
                route_policy = last_result.get("route_policy")
                preserved_pending = (
                    isinstance(route_policy, dict)
                    and route_policy.get("source") == "pending"
                    and decision.get("reason") == "loading"
                    and decision.get("state") == "connection_overlay_wait"
                    and decision.get("route_state") == "connection_overlay"
                    and transition.get("last_action_id")
                    == route_policy.get("pending_action_id")
                    and transition.get("action_source_sha256")
                    == route_policy.get("action_source_sha256")
                    and transition.get("expected_next_states")
                    == route_policy.get("allowed_observed_states")
                )
                if not preserved_pending:
                    return False, "global_route_interrupt_pending_mismatch", 2
            continue
        if advance_stop == "max_steps_reached" and int(advance_report.get("performed_steps") or 0) > 0:
            continue
        if advance_stop in {
            "race_list",
            "race_day_main_menu",
            "race_detail",
            "race_scene",
            "wbc_race_hub",
        }:
            if args.recover_career_goal_from_main_menu:
                return False, "career_goal_recovery_requires_main_menu", 2
            if not args.race_route_between_turns:
                return False, f"safe_advance:{advance_stop}", 0
            force_return_only = (
                advance_stop == "race_list"
                and _pending_career_goal_stop_reason(args)
                == "career_goal_occurrence_unbound"
            )
            wbc_confirmation_recovery = (
                _advance_report_is_wbc_confirmation_handoff(advance_report)
            )
            if (
                advance_stop == "race_day_main_menu"
                and args.mode == "live"
                and args.race_route_execute is True
            ):
                try:
                    turn_report["race_day_owner_preflight"] = (
                        _settle_race_day_owner_preflight(args, advance_report)
                    )
                except (OSError, RouteTransitionError) as exc:
                    return False, f"race_day_owner_preflight:{exc}", 2
            if (
                condition_race_guard is not None
                and not force_return_only
                and not wbc_confirmation_recovery
            ):
                race_authorized, race_reason = condition_race_guard(
                    advance_stop, advance_report
                )
                if not race_authorized:
                    return False, race_reason, 2
            wbc_only = advance_stop == "wbc_race_hub"
            if wbc_only and advance_transition_resync and not wbc_resync_handoff:
                return False, "wbc_dispatch:resync_forbidden", 2
            race_ok, race_stop, race_exit = _run_race_route_step(
                args,
                turn_root,
                turn_report,
                **(
                    {"wbc_only": True}
                    if wbc_only
                    else (
                        {"force_return_only": True}
                        if force_return_only
                        else (
                            {"allow_wbc_confirmation_recovery": True}
                            if wbc_confirmation_recovery
                            else {}
                        )
                    )
                ),
            )
            if not race_ok:
                return False, race_stop, race_exit
            if race_stop == "race_list_return_receipt_persisted":
                resync_next_advance = False
                continue
            if race_stop in {"ready_main_menu", "ready_training_menu"}:
                return True, race_stop, 0
            if race_stop == "event_choice":
                event_ok, event_stop, event_exit = _run_event_choice_step(
                    args,
                    turn_root,
                    turn_report,
                    control_driver=control_driver,
                    current_turn_index=current_turn_index,
                    prior_event_context=prior_event_context,
                    run_trainee_authority=run_trainee_authority,
                )
                if not event_ok:
                    return False, event_stop, event_exit
                if event_stop in {"ready_main_menu", "ready_training_menu"}:
                    return True, event_stop, 0
                resync_next_advance = False
                continue
            if race_stop in {
                "post_race_dialogue",
                "goal_achievement_loading",
                "goal_achievement_result",
                "resource_prompt",
            }:
                resync_next_advance = False
                continue
            return False, f"race_route:{race_stop}", 0
        if advance_stop == "event_choice":
            event_ok, event_stop, event_exit = _run_event_choice_step(
                args,
                turn_root,
                turn_report,
                control_driver=control_driver,
                current_turn_index=current_turn_index,
                prior_event_context=prior_event_context,
                run_trainee_authority=run_trainee_authority,
            )
            if not event_ok:
                return False, event_stop, event_exit
            if event_stop in {"ready_main_menu", "ready_training_menu"}:
                return True, event_stop, 0
            resync_next_advance = not _event_choice_receipt_persisted(turn_report)
            continue
        return False, f"safe_advance:{advance_stop}", 0

    return False, "route:max_cycles_reached", 0


def _advance_report_is_wbc_confirmation_handoff(
    report: dict[str, Any],
) -> bool:
    steps = report.get("step_reports")
    if not isinstance(steps, list) or not steps or not isinstance(steps[-1], dict):
        return False
    result = steps[-1].get("result")
    decision = result.get("decision") if isinstance(result, dict) else None
    if not isinstance(decision, dict) or (
        decision.get("action") != "stop"
        or decision.get("reason") != "race_detail"
        or decision.get("route_state") != "race_detail_confirm"
        or decision.get("tap") is not None
    ):
        return False
    evidence = decision.get("evidence")
    perception = evidence.get("perception") if isinstance(evidence, dict) else None
    screen_text = evidence.get("screen_text") if isinstance(evidence, dict) else None
    if (
        not isinstance(perception, dict)
        or perception.get("screen") != "race_detail_confirm"
        or not isinstance(screen_text, dict)
        or screen_text.get("dangerous") is not False
    ):
        return False
    normalized = "".join(str(screen_text.get("normalized_text", "")).upper().split())
    return "WBC" in normalized and "MILE" in normalized


def _run_current_count_race_inspection(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if (args.mode != "live" or args.max_training_turns != 1 or args.start_screen != "main-menu"
        or any(value for name, value in vars(args).items() if name.startswith("allow_"))
        or args.advance_execute or args.event_choice_execute or args.advance_after_final_action
        or args.recover_career_goal_from_main_menu):
        raise ValueError("race_inspection:unrelated_supervisor_capability")
    root = Path(args.output_root)
    turn_root = root / f"{args.run_session_id}_turn_{args.start_turn_index:03d}"
    report: dict[str, Any] = {"run_session_id": args.run_session_id, "route_steps": []}
    destination = _next_route_artifact_dir(turn_root, report, "count_race_inspection")
    argv = ["--mode", args.mode, "--inspection-only", "--max-steps", "6",
            "--screenshot-dir", str(destination),
            "--inspection-ledger-json", str(_career_goal_ledger_path(args)),
            "--inspection-calendar-json", str(args.race_calendar_json),
            "--inspection-runner-turn", str(args.start_turn_index),
            "--transition-session-json", str(_route_transition_path(args)),
            "--transition-run-session-id", args.run_session_id]
    if args.inspection_resume_report is not None:
        argv.extend(["--inspection-resume-report", args.inspection_resume_report])
    if args.inspection_resume_report_sha256 is not None:
        argv.extend(["--inspection-resume-report-sha256", args.inspection_resume_report_sha256])
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.race_route_execute:
        argv.append("--execute")
    if args.race_route_confirm_token is not None:
        argv.extend(["--confirm-token", args.race_route_confirm_token])
    child, code = _invoke_race_route(argv)
    _record_route_step(report, "count_race_inspection", code, child)
    report.update(is_valid=code == 0 and child.get("is_valid") is True,
                  status="stopped", stop_reason=child.get("stop_reason", child.get("error", "race_inspection_failed")),
                  completed_training_turns=0, completed_race_turns=0, turn_reports=[],
                  inspection=child)
    _add_action_accounting(report, requested=1, completed=0)
    return report, code


def _pending_career_goal_stop_reason(
    args: argparse.Namespace, *, allow_pending_count_for_wbc: bool = False,
) -> str | None:
    path = _career_goal_ledger_path(args)
    if not path.exists():
        return "career_goal_recovery_required" if args.recover_career_goal_from_main_menu else None
    try:
        ledger = CareerGoalLedgerStore().load(path, expected_run_session_id=args.run_session_id)
    except (CareerGoalLedgerError, OSError) as exc:
        return f"career_goal_ledger_invalid:{exc}"
    if any(goal.goal_kind == "graded_race_rank_count" and goal.progress_basis == "verified_lower_bound"
           and goal.deadline_turn is not None for goal in ledger.active_goals()):
        return "count_calendar_bound_reservation_pending"
    if ledger.pending_count_goals() and not allow_pending_count_for_wbc:
        return "career_goal_count_calendar_unbound"
    if ledger.pending_named_goals():
        return "career_goal_occurrence_unbound"
    if args.recover_career_goal_from_main_menu and not ledger.goals:
        return "career_goal_recovery_required"
    return None


def _count_policy_defer_decision(proof):
    from core.auto_trainer.race_scheduler import _candidate, _decision, _canonical_sha

    if proof["policy"]["action"] != "defer_to_local_policy":
        raise RaceSchedulerError("count_race:policy_defer_required")
    digest = _canonical_sha(proof)
    candidate = _candidate(input_sha=digest, action_kind="defer_to_local_policy", race_id=None,
        priority="none", source_goal_ids=(proof["goal_instance_id"],), reasons=("count_race_policy_defer",))
    return _decision(candidates=(candidate,), required_remaining={}, opportunities={},
        status="defer_to_local_policy", input_sha=digest)


def _count_race_previous_turn_root(args, turn_index):
    root = Path(args.output_root) / f"{args.run_session_id}_turn_{turn_index - 1:03d}"
    candidates = [root / "decision", *root.glob("route_steps/*_one_turn_attempt/decision")]
    required = (
        "turns/turn_000/state.json",
        "turns/turn_000/decision.json",
        "turns/turn_000/execution.json",
    )
    complete = [candidate for candidate in candidates if all((candidate / name).is_file() for name in required)]
    if (root / "race_schedule_state_context.json").is_file() and len(complete) == 1:
        return root
    return None


def _count_race_previous_entry_evidence(args, turn_index):
    root = Path(args.output_root) / f"{args.run_session_id}_turn_{turn_index - 1:03d}"
    verified = []
    for race_dir in root.glob("route_steps/*_one_turn_attempt/route_steps/*_count_reserved_race"):
        reports = sorted(
            race_dir.glob("report_*.json"),
            key=lambda path: int(path.stem.rsplit("_", 1)[-1]),
        )
        if not reports:
            continue
        report_path = reports[-1]
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        confirmations = [
            row["result"]
            for row in report.get("step_reports", [])
            if row.get("result", {}).get("performed") is True
            and row["result"].get("tap_performed") is True
            and row["result"].get("reason") == "confirm_race_entry"
            and row["result"].get("decision", {}).get("tap", {}).get("coordinate_name")
            == "race_confirm.primary"
        ]
        if (
            report.get("is_valid") is True
            and report.get("run_session_id") == args.run_session_id
            and report.get("observed_runner_turn") == turn_index - 1
            and len(report.get("planned_race_ids", [])) == 1
            and len(confirmations) == 1
        ):
            verified.append({
                "path": str(report_path.resolve()),
                "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                "race_id": report["planned_race_ids"][0],
            })
    return verified[0] if len(verified) == 1 else None


def _prepare_count_race_reservation(args, turn_root, runtime, *, turn_index,
                                    condition_report_path, condition_binding_key,
                                    previous_turn_root=None):
    from core.auto_trainer.count_race_card_evidence import (
        build_count_race_card_evidence, parse_count_race_card, validate_previous_count_race_history, _condition)
    from core.auto_trainer.count_race_policy import plan_count_race_continuation
    from core.auto_trainer.race_scheduler import CountRaceEligibilityEvidence
    from core.auto_trainer.orchestrator import _scenario_mandatory_race_ids

    if (args.mode != "live" or args.race_route_execute is not True
        or not args.count_race_history_json and previous_turn_root is None
        and not all(getattr(args, key) for key in (
            "count_race_previous_training_report", "count_race_previous_invocation",
            "count_race_zero_action_report", "count_race_inspection_report"))):
        raise RaceSchedulerError("count_race:current_evidence_paths_required")
    turn_id = f"{args.run_session_id}_turn_{turn_index:03d}"
    if condition_report_path is None:
        condition_binding_key = secrets.token_hex(32)
        condition_report_path, _, code = _run_explicit_condition_probe(args, turn_root,
            turn_id=turn_id, turn_index=turn_index, formal_condition_binding_key=condition_binding_key)
        if code != 0 or condition_report_path is None:
            raise RaceSchedulerError("count_race:condition_probe_failed")
    condition = load_explicit_condition_context(condition_report_path, required_turn_root=turn_root,
        formal_live=True, formal_binding_key=condition_binding_key, expected_turn_id=turn_id, expected_turn_index=turn_index)
    inspect_args = copy.copy(args)
    for name in vars(inspect_args):
        if name.startswith("allow_"):
            setattr(inspect_args, name, False)
    inspect_args.max_training_turns = 1
    inspect_args.advance_execute = inspect_args.event_choice_execute = inspect_args.advance_after_final_action = False
    inspect_args.recover_career_goal_from_main_menu = False
    inspect_args.inspection_resume_report = inspect_args.inspection_resume_report_sha256 = None
    inspected, code = _run_current_count_race_inspection(inspect_args)
    inspection_path = turn_root / "count_eligibility_inspection.json"
    if inspection_path.exists():
        try:
            latest = inspected["inspection"]["step_reports"][-1]["result"]
            inspection_root = Path(latest["decision"]["evidence"]["image"]).parent
            inspection_root.resolve().relative_to(turn_root.resolve())
        except (KeyError, IndexError, TypeError, ValueError):
            raise RaceSchedulerError("count_race:inspection_attempt_artifact_missing")
        inspection_path = inspection_root / "count_eligibility_inspection.json"
    _write_canonical_json_atomic(inspection_path, inspected)
    if code != 0 or inspected.get("is_valid") is not True:
        raise RaceSchedulerError("count_race:fresh_inspection_failed")
    child = inspected["inspection"]
    cards = [row["result"] for row in child["step_reports"]
             if row["result"].get("reason") == "leave_inspected_race_list" and row["result"].get("tap_performed") is True]
    if len(cards) != 1:
        raise RaceSchedulerError("count_race:unique_current_card_required")
    final = child["step_reports"][-1]["result"]
    main_path = Path(final["decision"]["evidence"]["image"])
    main_sha = hashlib.sha256(main_path.read_bytes()).hexdigest()
    observed = json.loads(main_path.with_suffix(".provider.json").read_text(encoding="utf-8"))
    anchor = observed["state_context"]["scenario_state"]["career_calendar_anchor"]
    if (observed.get("valid_for_training") is not True or main_sha != final["frame_safety"]["source_image_sha256"]
        or observed.get("source", {}).get("sha256") != main_sha or anchor.get("source_frame_sha256") != main_sha):
        raise RaceSchedulerError("count_race:fresh_main_required")
    calendar = runtime.orchestrator.race_calendar
    ledger = runtime.orchestrator.goal_ledger_store.load(runtime.ledger_path, expected_run_session_id=args.run_session_id)
    card = cards[0]
    if args.count_race_history_json:
        history = validate_previous_count_race_history(ledger, calendar=calendar, observed_runner_turn=turn_index,
            current_calendar_turn=child["calendar_turn"], paths=json.loads(Path(args.count_race_history_json).read_text(encoding="utf-8")))
        parsed = parse_count_race_card(card["decision"]["evidence"]["image"],
            source_frame_sha256=card["frame_safety"]["source_image_sha256"], calendar=calendar)
        active = ledger.active_goals()
        if (len(active) != 1 or active[0].goal_kind != "graded_race_rank_count"
            or active[0].progress_basis != "verified_lower_bound" or active[0].deadline_turn is None
            or parsed["calendar_turn"] != child["calendar_turn"]
            or parsed["calendar_turn"] != anchor["career_time"]["global_turn"]):
            raise RaceSchedulerError("count_race:policy_context_invalid")
        goal = active[0]
        binding = runtime.orchestrator.goal_ledger_store.bind_count_goal_deadline(ledger, anchor=anchor,
            observed_turn=turn_index, source_frame_sha256=main_sha, expected_run_session_id=args.run_session_id)
        if binding.status != "no_change":
            raise RaceSchedulerError("count_race:policy_context_invalid")
        if _scenario_mandatory_race_ids(observed["state_context"]["scenario_state"]):
            raise RaceSchedulerError("count_goal_other_race_obligation_pending")
        policy = plan_count_race_continuation(calendar=calendar, race_plan=runtime.orchestrator.race_plan,
            current_race_id=parsed["race_id"], current_turn=parsed["calendar_turn"], deadline_turn=goal.deadline_turn,
            required_upper_bound=goal.required_count - goal.completed_count,
            previous_action_kind=history["previous_action_kind"])
        facts = {"run_session_id": args.run_session_id, "observed_runner_turn": turn_index,
            "ledger_revision": ledger.revision, "ledger_view_sha256": ledger.current_view_sha256,
            "goal_instance_id": goal.goal_instance_id, "main_source": {"path": str(main_path), "sha256": main_sha},
            "card": parsed, "condition": _condition(condition, args.run_session_id, turn_index),
            "history": history, "policy": policy}
        if policy["action"] == "defer_to_local_policy":
            decision = _count_policy_defer_decision(facts)
            payload = {"schema_version": "race_schedule_decision.v1", "run_session_id": args.run_session_id,
                "turn_index": turn_index, "source_frame_sha256": main_sha, "ledger_path": str(runtime.ledger_path),
                "ledger_revision": ledger.revision, "ledger_view_sha256": ledger.current_view_sha256,
                "decision": race_schedule_decision_to_dict(decision), "count_race_policy": facts}
            destination = inspection_path.parent / "count_race_policy.json"
            _write_canonical_json_atomic(destination, payload)
            if json.loads(destination.read_text(encoding="utf-8")) != payload:
                raise RaceSchedulerError("count_race:policy_readback_failed")
            return decision, destination
        eligibility = CountRaceEligibilityEvidence(run_session_id=args.run_session_id,
            calendar_turn=parsed["calendar_turn"], race_id=parsed["race_id"], source_path=parsed["source_path"],
            source_frame_sha256=parsed["source_frame_sha256"], aptitude_ok=parsed["aptitude_ok"],
            entry_conditions_met=parsed["entry_conditions_met"], condition_ok=True,
            consecutive_race_ok=policy["action"] == "enter_race", evidence=facts)
    else:
        eligibility = build_count_race_card_evidence(card["decision"]["evidence"]["image"],
            source_frame_sha256=card["frame_safety"]["source_image_sha256"], run_session_id=args.run_session_id,
            observed_runner_turn=turn_index, expected_calendar_turn=child["calendar_turn"], calendar=calendar,
            validated_condition_context=condition, previous_training_report_path=args.count_race_previous_training_report,
            previous_invocation_path=args.count_race_previous_invocation, zero_action_report_path=args.count_race_zero_action_report,
            inspection_report_path=args.count_race_inspection_report,
            post_training_route_report_path=args.count_race_post_training_route_report,
            previous_turn_root=previous_turn_root)
    plan = build_count_race_reservation(ledger=ledger, calendar=calendar, main_anchor=anchor,
        main_source_path=main_path, observed_runner_turn=turn_index, eligibility=eligibility)
    destination = inspection_path.parent / "count_race_reservation.json"
    _write_canonical_json_atomic(destination, plan.to_dict())
    if json.loads(destination.read_text(encoding="utf-8")) != plan.to_dict():
        raise RaceSchedulerError("count_race:reservation_readback_failed")
    return plan.decision, destination


def _persist_count_race_result(runtime, *, schedule_path, report_path, report, run_session_id):
    from core.auto_trainer.race_result_rank_reader import RaceResultRankReader
    from core.auto_trainer.race_result_observation import build_count_race_result_receipt

    result = report["step_reports"][-1]["result"]
    source = result["decision"]["evidence"]["image"]
    source_sha = result["frame_safety"]["source_image_sha256"]
    rank = RaceResultRankReader().read(source, source_frame_sha256=source_sha)
    if rank.status != "accepted":
        raise RaceSchedulerError("count_race:rank_unverified")
    result["race_result"] = {"rank_evidence": rank.to_dict()}
    _write_canonical_json_atomic(report_path, report)
    calendar, store = runtime.orchestrator.race_calendar, runtime.orchestrator.goal_ledger_store
    receipt = build_count_race_result_receipt(source, source_frame_sha256=source_sha,
        schedule_path=schedule_path, race_report_path=report_path, calendar=calendar)
    ledger = store.load(runtime.ledger_path, expected_run_session_id=run_session_id)
    merged = store.record_count_race_result(ledger, receipt, calendar=calendar)
    if (merged.status, merged.reason) not in {("updated", "count_race_result_recorded"),
                                            ("no_change", "count_race_result_duplicate")}:
        raise RaceSchedulerError("count_race:result_not_recorded")
    store.write_atomic(runtime.ledger_path, merged.ledger)
    loaded = store.load(runtime.ledger_path, expected_run_session_id=run_session_id)
    if loaded.to_dict() != merged.ledger.to_dict():
        raise RaceSchedulerError("count_race:result_readback_failed")
    return {"status": merged.status, "reason": merged.reason, "receipt_sha256": receipt.receipt_sha256,
            "ledger_revision": loaded.revision, "ledger_view_sha256": loaded.current_view_sha256}


def _load_count_race_resume(args, runtime):
    from core.auto_trainer.race_route import RaceRouteSession
    from core.auto_trainer.race_scheduler import CountRaceEligibilityEvidence

    source = Path(args.count_race_resume_report)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if source_sha != args.count_race_resume_report_sha256:
        raise ValueError("count_resume:report_sha_mismatch")
    outer = json.loads(source.read_text(encoding="utf-8"))
    if getattr(args, "count_race_restart_evidence", None) or getattr(args, "count_race_restart_evidence_sha256", None):
        return _load_count_restart_resume(args, runtime, outer, source_sha)
    if outer.get("resumed_existing_race") is True:
        return _load_count_watch_resume(args, runtime, outer, source_sha)
    turn = outer["turn_reports"][-1]
    report = turn["race_route"]["report"]
    quick_pending = report.get("stop_reason") == "unsupported_screen:portrait_ui"
    last_path = Path(report["step_reports"][-1]["result"]["decision"]["evidence"]["image"])
    original_snapshot = last_path.parent / f"report_{len(report['step_reports']):03d}.json"
    if json.loads(original_snapshot.read_text(encoding="utf-8")) != report:
        raise ValueError("count_resume:stored_child_report_mismatch")
    schedule_path = Path(turn["race_schedule"]["path"])
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    reservation = schedule["count_reservation"]
    if (outer.get("run_session_id") != args.run_session_id or outer.get("status") != "stopped"
        or outer.get("is_valid") is not False or turn.get("code") != 2 or turn["race_route"].get("code") != 0
        or turn.get("turn_index") != args.start_turn_index or turn.get("count_entry_confirmed") is not True
        or report.get("run_session_id") != args.run_session_id
        or report.get("observed_runner_turn") != args.start_turn_index
        or report.get("stop_reason") not in {"race_preview:view_result_locked", "race_list:scheduled_session_consumed", "unsupported_screen:portrait_ui"}
        or report.get("is_valid") is not True or report.get("status") != "stopped"
        or report.get("planned_race_ids") != [reservation["race_id"]]
        or report.get("schedule_packet_sha256") != schedule["decision"]["packet_sha256"]
        or turn["race_schedule"].get("packet_sha256") != report["schedule_packet_sha256"]
        or "count_race_result" in turn or turn.get("count_owner_handoffs")):
        raise ValueError("count_resume:original_owner_invalid")
    child_id = report["race_route_session_id"]
    if not isinstance(child_id, str) or len(child_id) != 32 or any(c not in "0123456789abcdef" for c in child_id):
        raise ValueError("count_resume:child_identity_invalid")
    ledger = runtime.orchestrator.goal_ledger_store.load(runtime.ledger_path, expected_run_session_id=args.run_session_id)
    if reservation["ledger_revision"] != ledger.revision or reservation["ledger_view_sha256"] != ledger.current_view_sha256:
        raise ValueError("count_resume:ledger_changed_or_result_recorded")
    main = Path(reservation["source_path"])
    provider = json.loads(main.with_suffix(".provider.json").read_text(encoding="utf-8"))
    anchor = provider["state_context"]["scenario_state"]["career_calendar_anchor"]
    if provider["source"]["sha256"] != reservation["source_frame_sha256"]:
        raise ValueError("count_resume:main_source_mismatch")
    rebuilt = build_count_race_reservation(ledger=ledger, calendar=runtime.orchestrator.race_calendar,
        main_anchor=anchor, main_source_path=main, observed_runner_turn=args.start_turn_index,
        eligibility=CountRaceEligibilityEvidence(**reservation["eligibility"]))
    if rebuilt.to_dict() != schedule:
        raise ValueError("count_resume:reservation_changed")
    inspected = json.loads((schedule_path.parent / "count_eligibility_inspection.json").read_text(encoding="utf-8"))
    baseline = inspected["inspection"]["step_reports"][-1]["result"]["route_transition"]
    current = _load_race_owner_session(args)
    if (inspected.get("is_valid") is not True or inspected.get("run_session_id") != args.run_session_id
        or inspected["inspection"]["ledger_revision"] != ledger.revision
        or current.to_dict() != baseline or current.current_state != "main_menu"
        or current.observation_sha256 != reservation["source_frame_sha256"]):
        raise ValueError("count_resume:owner_baseline_changed")
    steps = report["step_reports"]
    if (not isinstance(steps, list) or len(steps) < (5 if quick_pending else 4)
        or [row.get("step") for row in steps] != list(range(1, len(steps) + 1))
        or type(report.get("performed_steps")) is not int
        or report["performed_steps"] != len(steps) - 1):
        raise ValueError("count_resume:step_sequence_invalid")
    identities = [("enter_race_from_main_menu", "ready_main_menu", "main_menu.race"),
                  ("enter_selected_target_race", "race_list", "race_list.primary_entry"),
                  ("confirm_race_entry", "race_detail_confirm", "race_confirm.primary")]
    for index, row in enumerate(steps):
        result = row["result"]
        decision, packet, gate = result["decision"], result["frame_safety"], result["safety_verdict"]
        path = Path(decision["evidence"]["image"])
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if (packet.get("source_image_sha256") != sha or gate.get("source_image_sha256") != sha
            or gate.get("allowed") is not True or packet.get("text_status") != "ok" or packet.get("errors")
            or packet.get("dangerous") is not False):
            raise ValueError("count_resume:source_or_gate_invalid")
        if index < 3:
            reason, state, action = identities[index]
            receipt = result.get("route_action_receipt_source") or {}
            expected_receipt = {"action_id": action, "coordinate_name": action, "action_state": state,
                                "source_sha256": sha}
            if (result.get("performed") is not True or result.get("tap_performed") is not True
                or result.get("reason") != reason or decision.get("action") != "tap"
                or decision.get("reason") != reason or decision.get("route_state") != state
                or decision.get("tap", {}).get("coordinate_name") != action or gate.get("action_id") != action
                or {key: receipt.get(key) for key in expected_receipt} != expected_receipt
                or Path(str(receipt.get("source_path", ""))).resolve() != path.resolve()):
                raise ValueError("count_resume:actual_entry_chain_invalid")
        elif quick_pending and index == len(steps) - 2:
            action = "race_detail.view_result"
            receipt = result.get("route_action_receipt_source") or {}
            expected_receipt = {"action_id": action, "coordinate_name": action,
                                "action_state": "race_preview", "source_sha256": sha}
            if (result.get("performed") is not True or result.get("tap_performed") is not True
                or result.get("reason") != "view_race_result" or decision.get("reason") != "view_race_result"
                or decision.get("action") != "tap" or decision.get("route_state") != "race_preview"
                or (decision.get("tap") or {}).get("coordinate_name") != action or gate.get("action_id") != action
                or decision.get("consume_target_race_verification") or decision.get("target_race_verification") is not None
                or {key: receipt.get(key) for key in expected_receipt} != expected_receipt
                or Path(str(receipt.get("source_path", ""))).resolve() != path.resolve()):
                raise ValueError("count_resume:actual_quick_result_invalid")
        elif index < len(steps) - 1:
            if (result.get("performed") is not True or result.get("tap_performed") is True
                or result.get("reason") != ("count_race_post_confirm_wait" if quick_pending else "loading")
                or decision.get("reason") != result.get("reason") or decision.get("action") != "wait"
                or decision.get("tap") is not None
                or decision.get("route_state") != ("race_list" if quick_pending else "loading") or gate.get("action_id") is not None):
                raise ValueError("count_resume:intervening_action_invalid")
        elif (result.get("performed") is not False or result.get("tap_performed") is True
              or result.get("reason") != report["stop_reason"] or decision.get("action") != "stop"
              or decision.get("reason") != report["stop_reason"] or gate.get("reason") != "non_executing_decision"
              or gate.get("action_id") is not None):
            raise ValueError("count_resume:terminal_invalid")
    session = RaceRouteSession(planned_race_ids=tuple(report["planned_race_ids"]),
        schedule_packet_sha256=report["schedule_packet_sha256"])
    entry = steps[1]["result"]["decision"]
    confirmation = steps[2]["result"]["decision"]
    if (entry["target_race_verification"]["source_image_sha256"] != steps[1]["result"]["frame_safety"]["source_image_sha256"]
        or confirmation.get("consume_target_race_verification") is not True
        or confirmation["evidence"].get("prior_target_race_verification") != entry["target_race_verification"]):
        raise ValueError("count_resume:confirmation_proof_mismatch")
    session.stage_target_race_entry(entry)
    session.consume_target_race_entry()
    session.count_phase = "confirmed"
    name = reservation["eligibility"]["evidence"]["card"]["race_name"]
    title = tuple("".join(line.split()) for line in steps[-1]["result"]["frame_safety"]["visible_text"].splitlines()[:2])
    if report["stop_reason"] == "race_preview:view_result_locked" and title != ("GI", name):
        raise ValueError("count_resume:preview_identity_invalid")
    if report["stop_reason"] == "race_list:scheduled_session_consumed" and steps[-1]["result"]["decision"].get("route_state") != "race_list":
        raise ValueError("count_resume:terminal_invalid")
    session.count_resume_preview_title = ("GI", name)
    if quick_pending:
        from core.auto_trainer.race_route import RaceRouteExecutor, RaceRoutePolicy

        session.count_resume_preview_title = None
        for row in steps[3:-2]:
            value = row["result"]
            if RaceRouteExecutor._count_scope_error(copy.deepcopy(value["decision"]), session,
                RaceRoutePolicy(count_reserved_only=True), value["frame_safety"]["source_image_sha256"]) is not None:
                raise ValueError("count_resume:post_confirm_wait_invalid")
        viewed = steps[-2]["result"]
        lines = tuple("".join(line.split()) for line in viewed["frame_safety"]["visible_text"].splitlines() if line.strip())
        # The recorded spring preview displays parentheses absent on its card.
        displayed_name = "天王奖（春）" if name == "天王奖春" else name
        session.count_resume_preview_title = ("GI", displayed_name)
        if (lines[:2] != ("GI", displayed_name) and lines[:1] != (displayed_name,)
            or RaceRouteExecutor._count_scope_error(copy.deepcopy(viewed["decision"]), session,
                RaceRoutePolicy(count_reserved_only=True, allow_quick_result=True,
                    visible_text=viewed["frame_safety"]["visible_text"]),
                viewed["frame_safety"]["source_image_sha256"]) is not None):
            raise ValueError("count_resume:recorded_quick_preview_invalid")
        session.count_resume_preview_title = None
    invocation = count_race_cli.CountRaceInvocation(args.run_session_id, args.start_turn_index, session,
        lambda _: (_ for _ in ()).throw(ValueError("count_resume:reentry_forbidden")), race_route_session_id=child_id)
    invocation.steps.extend(copy.deepcopy(steps))
    # Reconstruct the existing guards from actual performed history. A stop
    # does not erase tap signatures; intervening loading waits do.
    for row in steps:
        result = row["result"]
        if result.get("performed") is not True:
            continue
        signature = count_race_cli._tap_signature(result)
        if signature is None:
            invocation.previous_tap_signature, invocation.repeated_tap_count = None, 0
            invocation.recent_tap_signatures.clear()
        else:
            invocation.repeated_tap_count = invocation.repeated_tap_count + 1 if signature == invocation.previous_tap_signature else 1
            invocation.previous_tap_signature = signature
            invocation.recent_tap_signatures.append(signature)
            del invocation.recent_tap_signatures[:-18]
    return schedule_path, invocation, {"source_report": str(source.resolve()), "source_report_sha256": source_sha,
                                      "original_step_count": len(steps), "owner_baseline": baseline,
                                      **({"quick_result_pending": True} if quick_pending else {})}


def _load_count_watch_resume(args, runtime, outer, source_sha):
    import cv2
    from core.auto_trainer.race_route import RaceRouteExecutor, RaceRoutePlanner, RaceRoutePolicy

    turn = outer["turn_reports"][-1]
    report, prior = turn["race_route"]["report"], turn["count_resume"]
    origin = json.loads(Path(prior["source_report"]).read_text(encoding="utf-8"))
    if len(origin["turn_reports"][-1]["race_route"]["report"]["step_reports"]) >= len(report["step_reports"]):
        raise ValueError("count_watch_resume:parent_history_not_shorter")
    original_args = copy.copy(args)
    original_args.count_race_resume_report = prior["source_report"]
    original_args.count_race_resume_report_sha256 = prior["source_report_sha256"]
    restart = prior.get("restart_evidence", {}) if prior.get("restart") is True else {}
    original_args.count_race_restart_evidence = restart.get("path")
    original_args.count_race_restart_evidence_sha256 = restart.get("sha256")
    schedule_path, invocation, original_audit = _load_count_race_resume(original_args, runtime)
    steps, prefix = report["step_reports"], len(invocation.steps)
    continuing_watch = original_audit.get("watching") is True
    quick_continuation = original_audit.get("quick_result_pending") is True
    quick_denied = "count_race:preflight:quick_result_animation_unverified"
    result_pending = report.get("stop_reason") == "race_result:continue_not_allowed"
    expected_stop = "count_race:count_race:rank_unverified" if result_pending else report.get("stop_reason")
    if (outer.get("is_valid") is not False or outer.get("status") != "stopped"
        or outer.get("stop_reason") != expected_stop
        or (result_pending and turn.get("count_race_error") != "count_race:rank_unverified")
        or outer.get("run_session_id") != args.run_session_id or outer.get("new_entry_actions") != 0
        or turn.get("turn_index") != args.start_turn_index or turn.get("count_entry_confirmed") is not True
        or report.get("is_valid") is not True or report.get("status") != "stopped"
        or report.get("stop_reason") not in ({quick_denied} if quick_continuation else {
            "unsupported_screen:event_choice", "race_scene:wait_limit_reached", "race_result:continue_not_allowed"})
        or report.get("race_route_session_id") != invocation.race_route_session_id
        or report.get("run_session_id") != args.run_session_id or report.get("observed_runner_turn") != args.start_turn_index
        or report.get("planned_race_ids") != list(invocation.session.planned_race_ids)
        or report.get("schedule_packet_sha256") != invocation.session.schedule_packet_sha256
        or "count_race_result" in turn or turn.get("count_owner_handoffs")
        or (len(steps) != prefix + 2 if quick_continuation else len(steps) < prefix + (3 if result_pending else 1 if continuing_watch else 5))
        or steps[:prefix] != invocation.steps
        or prior.get("original_step_count") != prefix or prior.get("owner_baseline") != original_audit["owner_baseline"]
        or [row.get("step") for row in steps] != list(range(1, len(steps) + 1))
        or type(report.get("performed_steps")) is not int
        or report["performed_steps"] != sum(row["result"].get("performed") is True for row in steps)):
        raise ValueError("count_watch_resume:history_invalid")
    if report["stop_reason"] == "race_scene:wait_limit_reached" and not count_race_cli.is_race_scene_wait_result(steps[-1]["result"]):
        raise ValueError("count_watch_resume:terminal_invalid")
    final_path = Path(steps[-1]["result"]["decision"]["evidence"]["image"])
    if json.loads((final_path.parent / f"report_{len(steps):03d}.json").read_text(encoding="utf-8")) != report:
        raise ValueError("count_watch_resume:stored_report_mismatch")
    actual_taps = [row["result"] for row in (steps[prefix:-1] if quick_continuation else steps[prefix:])
                   if row["result"]["decision"].get("action") == "tap"]
    attempt = Path(prior["attempt_path"])
    if actual_taps:
        first_tap = actual_taps[0]
        expected_attempt = {**original_audit, "race_route_session_id": invocation.race_route_session_id,
            "first_tap": {"action_id": first_tap["decision"]["tap"]["coordinate_name"],
                "source_path": first_tap["decision"]["evidence"]["image"],
                "source_sha256": first_tap["frame_safety"]["source_image_sha256"],
                "frame_safety": first_tap["frame_safety"], "safety_verdict": first_tap["safety_verdict"]}}
        if json.loads(attempt.read_text(encoding="utf-8")) != expected_attempt:
            raise ValueError("count_watch_resume:first_go_attempt_mismatch")
        if (continuing_watch or result_pending) and RaceRouteExecutor._count_scope_error(copy.deepcopy(first_tap["decision"]), invocation.session,
            RaceRoutePolicy(count_reserved_only=True, allow_skip_race_scene=True, allow_quick_result=True,
                            visible_text=first_tap["frame_safety"]["visible_text"]),
            first_tap["frame_safety"]["source_image_sha256"]) is not None:
            raise ValueError("count_watch_resume:first_tap_scope_invalid")
    elif not continuing_watch or attempt.exists():
        raise ValueError("count_watch_resume:missing_actual_tap")
    if result_pending and (continuing_watch or len(actual_taps) != 2):
        raise ValueError("count_watch_resume:quick_result_chain_invalid")
    required = [("view_race_result", "race_preview", "race_detail.view_result")] if result_pending else [] if continuing_watch or quick_continuation else [("go_to_race_from_detail", "race_detail", "race_detail.go_to_race"),
                ("confirm_race_playback", "race_playback_confirm", "race_confirm.primary")]
    watch_actions = {("race_result_continue", "race_result", "race_result.continue")} if result_pending or quick_continuation else {("race_scene_start_or_continue", "race_scene", "race_scene.start_or_continue"),
                     ("skip_or_advance_race_scene", "race_scene", "race_scene.skip_or_advance")}
    seen = set()
    for offset, row in enumerate(steps[prefix:]):
        result = row["result"]
        decision, packet, gate = result["decision"], result["frame_safety"], result["safety_verdict"]
        path = Path(decision["evidence"]["image"])
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if (sha != packet.get("source_image_sha256") or sha != gate.get("source_image_sha256")
            or gate.get("allowed") is not True or packet.get("text_status") != "ok"
            or packet.get("errors") or packet.get("dangerous") is not False
            or decision.get("consume_target_race_verification") or decision.get("target_race_verification") is not None):
            raise ValueError("count_watch_resume:source_or_gate_invalid")
        if row is steps[-1] and report["stop_reason"] != "race_scene:wait_limit_reached":
            if quick_continuation:
                if (result.get("performed") is not False or result.get("tap_performed") is not None
                    or result.get("reason") != quick_denied or decision.get("action") != "tap"
                    or (decision.get("reason"), decision.get("route_state"), (decision.get("tap") or {}).get("coordinate_name"))
                        != ("race_result_continue", "race_result", "race_result.continue")
                    or gate.get("action_id") != "race_result.continue" or gate.get("reason") != "specific_action_allowed"
                    or result.get("route_action_receipt_source") is not None):
                    raise ValueError("count_watch_resume:preflight_denied_invalid")
                continue
            if (result.get("performed") is not False or decision.get("action") != "stop"
                or result.get("tap_performed") is True or decision.get("tap") is not None
                or result.get("reason") != report["stop_reason"] or decision.get("reason") != report["stop_reason"]
                or gate.get("action_id") is not None or gate.get("reason") != "non_executing_decision"):
                raise ValueError("count_watch_resume:terminal_invalid")
            if result_pending and not RaceRoutePlanner._looks_like_ranking_list_result(cv2.imread(str(path))):
                raise ValueError("count_watch_resume:rank_stop_layout_invalid")
            continue
        if result.get("performed") is not True:
            raise ValueError("count_watch_resume:unknown_outcome")
        if decision.get("action") == "wait" and offset >= len(required):
            if (result.get("tap_performed") is True or gate.get("action_id") is not None
                or (result.get("reason"), decision.get("route_state")) not in {("loading", "loading"), ("race_scene_running", "race_scene")}):
                raise ValueError("count_watch_resume:wait_invalid")
            continue
        action_id = (decision.get("tap") or {}).get("coordinate_name")
        identity = (decision.get("reason"), decision.get("route_state"), action_id)
        allowed = {required[offset]} if offset < len(required) else watch_actions
        receipt = result.get("route_action_receipt_source") or {}
        expected_receipt = {"action_id": action_id, "coordinate_name": action_id,
                            "action_state": decision["route_state"], "source_sha256": sha}
        if (decision.get("action") != "tap" or result.get("tap_performed") is not True
            or result.get("reason") != decision.get("reason") or identity not in allowed
            or gate.get("action_id") != action_id
            or {key: receipt.get(key) for key in expected_receipt} != expected_receipt
            or Path(str(receipt.get("source_path", ""))).resolve() != path.resolve()):
            raise ValueError("count_watch_resume:actual_watch_action_invalid")
        if (result_pending or quick_continuation) and offset >= len(required):
            img = cv2.imread(str(path))
            perception = decision["evidence"]["perception"]
            if (RaceRoutePlanner._looks_like_ranking_list_result(img)
                or not (
                    RaceRoutePlanner._looks_like_rank_overlay_result(perception, img)
                    or RaceRoutePlanner._looks_like_large_rank_result_animation(perception, img)
                )
                or (decision["tap"].get("x"), decision["tap"].get("y")) != RaceRoutePlanner._screen_continue_point(img)):
                raise ValueError("count_watch_resume:nonrank_animation_unverified")
        seen.add(identity)
    if not continuing_watch and not watch_actions.issubset(seen):
        raise ValueError("count_watch_resume:watch_chain_incomplete")
    invocation.steps[:] = copy.deepcopy(steps)
    invocation.session.count_phase = "confirmed" if result_pending or quick_continuation else "watching"
    invocation.session.count_resume_preview_title = None
    invocation.session.count_resume_restart_title = None
    invocation.session.count_resume_watch_pending = not (result_pending or quick_continuation)
    for row in steps[prefix:]:
        result = row["result"]
        invocation.race_scene_waits = invocation.race_scene_waits + 1 if count_race_cli.is_race_scene_wait_result(result) else 0
        if result.get("performed") is not True:
            continue
        signature = count_race_cli._tap_signature(result)
        if signature is None:
            invocation.previous_tap_signature, invocation.repeated_tap_count = None, 0
            invocation.recent_tap_signatures.clear()
        else:
            invocation.repeated_tap_count = invocation.repeated_tap_count + 1 if signature == invocation.previous_tap_signature else 1
            invocation.previous_tap_signature = signature
            invocation.recent_tap_signatures.append(signature)
            del invocation.recent_tap_signatures[:-18]
    return schedule_path, invocation, {"source_report": str(Path(args.count_race_resume_report).resolve()),
        "source_report_sha256": source_sha, "original_step_count": len(steps), "watching": not (result_pending or quick_continuation),
        **({"result_pending": True} if result_pending or quick_continuation else {}),
        **({"quick_result_pending": True} if quick_continuation else {}),
        "prior_attempt_path": str(attempt), "prior_attempt_sha256": hashlib.sha256(attempt.read_bytes()).hexdigest() if actual_taps else None,
        **({"watch_chain": True} if continuing_watch else {}),
        "owner_baseline": original_audit["owner_baseline"]}


def _load_count_restart_resume(args, runtime, outer, source_sha):
    turn = outer["turn_reports"][-1]
    report, prior = turn["race_route"]["report"], turn["count_resume"]
    parent = json.loads(Path(prior["source_report"]).read_text(encoding="utf-8"))
    if len(parent["turn_reports"][-1]["race_route"]["report"]["step_reports"]) >= len(report["step_reports"]):
        raise ValueError("count_restart:parent_history_not_shorter")
    previous_args = copy.copy(args)
    previous_args.count_race_restart_evidence = previous_args.count_race_restart_evidence_sha256 = None
    previous_args.count_race_resume_report = prior["source_report"]
    previous_args.count_race_resume_report_sha256 = prior["source_report_sha256"]
    schedule_path, invocation, previous_audit = _load_count_race_resume(previous_args, runtime)
    steps, prefix = report["step_reports"], len(invocation.steps)
    if (previous_audit.get("watching") is not True or previous_audit.get("restart")
        or outer.get("resumed_existing_race") is not True or outer.get("is_valid") is not False
        or outer.get("status") != "stopped" or outer.get("stop_reason") != "race_scene:wait_limit_reached"
        or outer.get("run_session_id") != args.run_session_id or outer.get("new_entry_actions") != 0
        or turn.get("turn_index") != args.start_turn_index or turn.get("count_entry_confirmed") is not True
        or turn["race_route"].get("code") != 0 or report.get("is_valid") is not True or report.get("status") != "stopped"
        or report.get("stop_reason") != outer["stop_reason"] or "count_race_result" in turn or turn.get("count_owner_handoffs")
        or report.get("race_route_session_id") != invocation.race_route_session_id
        or report.get("run_session_id") != args.run_session_id or report.get("observed_runner_turn") != args.start_turn_index
        or report.get("planned_race_ids") != list(invocation.session.planned_race_ids)
        or report.get("schedule_packet_sha256") != invocation.session.schedule_packet_sha256
        or steps[:prefix] != invocation.steps or len(steps) <= prefix
        or {key: value for key, value in prior.items() if key != "attempt_path"} != previous_audit
        or Path(prior["attempt_path"]).exists()
        or [row.get("step") for row in steps] != list(range(1, len(steps) + 1))
        or type(report.get("performed_steps")) is not int
        or report["performed_steps"] != sum(row["result"].get("performed") is True for row in steps)):
        raise ValueError("count_restart:wait_history_invalid")
    final_path = Path(steps[-1]["result"]["decision"]["evidence"]["image"])
    if json.loads((final_path.parent / f"report_{len(steps):03d}.json").read_text(encoding="utf-8")) != report:
        raise ValueError("count_restart:stored_report_mismatch")
    for row in steps[prefix:]:
        result = row["result"]
        decision, packet, gate = result["decision"], result["frame_safety"], result["safety_verdict"]
        sha = hashlib.sha256(Path(decision["evidence"]["image"]).read_bytes()).hexdigest()
        if (result.get("performed") is not True or result.get("tap_performed") is True
            or result.get("reason") != "race_scene_running" or decision.get("reason") != "race_scene_running"
            or decision.get("action") != "wait" or decision.get("route_state") != "race_scene" or decision.get("tap") is not None
            or result.get("route_action_receipt_source") is not None or decision.get("consume_target_race_verification")
            or sha != packet.get("source_image_sha256") or sha != gate.get("source_image_sha256")
            or packet.get("text_status") != "ok" or packet.get("dangerous") is not False or packet.get("errors")
            or gate.get("allowed") is not True or gate.get("action_id") is not None):
            raise ValueError("count_restart:wait_tail_unverified")
    evidence_path = Path(args.count_race_restart_evidence)
    if hashlib.sha256(evidence_path.read_bytes()).hexdigest() != args.count_race_restart_evidence_sha256:
        raise ValueError("count_restart:evidence_sha_mismatch")
    observed = json.loads(evidence_path.read_text(encoding="utf-8"))
    started_path, stopped_path = evidence_path.parent / "restart_started.json", evidence_path.parent / "restart.json"
    started = json.loads(started_path.read_text(encoding="utf-8"))
    stopped = json.loads(stopped_path.read_text(encoding="utf-8"))
    if (started.get("authorization") != "user_explicit_restart_long_stalled_game"
        or started.get("run_session_id") != args.run_session_id or started.get("device") != args.adb_port
        or started.get("package") != "com.bilibili.umamusu" or started.get("game_commands") != []
        or stopped != {**started, "game_commands": [{"command": "am force-stop", "ok": True, "returncode": 0}]}
        or observed.get("status") != "new_game_process_observed" or observed.get("device") != args.adb_port
        or type(observed.get("ui_taps")) is not int or observed["ui_taps"] != 0
        or observed.get("old_pid") != started.get("old_pid") or not str(started.get("old_pid", "")).isdigit()
        or not str(observed.get("new_pid", "")).isdigit() or observed["new_pid"] == observed["old_pid"]
        or hashlib.sha256(Path(observed["source_path"]).read_bytes()).hexdigest() != observed["frame_safety"]["source_image_sha256"]):
        raise ValueError("count_restart:process_evidence_invalid")
    for path in (runtime.ledger_path, _route_transition_path(args)):
        receipts = [value for key, value in observed["state_files"].items() if Path(key).name == path.name]
        if (len(receipts) != 1 or receipts[0].get("mtime_predates_restart") is not True
            or receipts[0].get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError("count_restart:state_changed")
    invocation.steps[:] = copy.deepcopy(steps)
    invocation.race_scene_waits += len(steps) - prefix
    invocation.previous_tap_signature, invocation.repeated_tap_count = None, 0
    invocation.recent_tap_signatures.clear()
    reservation = json.loads(schedule_path.read_text(encoding="utf-8"))["count_reservation"]
    invocation.session.count_resume_restart_title = (reservation["eligibility"]["evidence"]["card"]["race_name"], "GI")
    return schedule_path, invocation, {**previous_audit,
        "source_report": str(Path(args.count_race_resume_report).resolve()), "source_report_sha256": source_sha,
        "original_step_count": len(steps), "restart": True, "preserved_race_scene_waits": invocation.race_scene_waits,
        "restart_evidence": {"path": str(evidence_path.resolve()), "sha256": args.count_race_restart_evidence_sha256,
            "started_sha256": hashlib.sha256(started_path.read_bytes()).hexdigest(),
            "force_stop_sha256": hashlib.sha256(stopped_path.read_bytes()).hexdigest(),
            "old_pid": observed["old_pid"], "new_pid": observed["new_pid"]}}


def _resume_count_reserved_race(args):
    entry = {"turn_index": args.start_turn_index, "turn_id": f"{args.run_session_id}_turn_{args.start_turn_index:03d}",
             "route_steps": [], "count_confirmation_preexisting": True}
    outer = {"run_session_id": args.run_session_id, "resumed_existing_race": True, "completed_action_turns": 0,
             "completed_training_turns": 0, "new_entry_actions": 0, "turn_reports": [entry]}
    try:
        permitted = {"allow_confirm_race", "allow_quick_result", "allow_result_continue", "allow_skip_race_scene"}
        if (not args.count_race_resume_report or not args.count_race_resume_report_sha256
            or args.mode != "live" or args.race_route_execute is not True or args.max_training_turns != 1
            or any(value for name, value in vars(args).items() if name.startswith("allow_") and name not in permitted)
            or args.capture_execute or args.execute_training_confirm or args.inspect_current_count_races):
            raise ValueError("count_resume:unrelated_capability_or_context")
        runtime = SimpleNamespace(ledger_path=_career_goal_ledger_path(args),
            orchestrator=SimpleNamespace(goal_ledger_store=CareerGoalLedgerStore(), race_calendar=RaceCalendar.load(args.race_calendar_json)))
        schedule_path, invocation, audit = _load_count_race_resume(args, runtime)
        turn_root = Path(args.output_root) / entry["turn_id"]
        attempt_kind = "count_race_restart_resume" if audit.get("restart") else "count_race_watch_resume" if audit.get("watching") else "count_race_resume"
        attempt = turn_root / f"{attempt_kind}_{invocation.race_route_session_id}.attempt.json"
        if audit.get("watch_chain"):
            attempt = turn_root / f"count_watch_{audit['source_report_sha256']}.attempt.json"
        if audit.get("result_pending"):
            attempt = turn_root / f"count_result_{audit['source_report_sha256']}.attempt.json"
        if audit.get("quick_result_pending"):
            attempt = turn_root / f"count_quick_{audit['source_report_sha256']}.attempt.json"
        attempt_claimed = False
        def pre_tap(source):
            nonlocal attempt_claimed
            if audit.get("quick_result_pending"):
                if source.get("action_id") != "race_result.continue":
                    raise ValueError("quick_result_resume_action_out_of_scope")
                if not invocation.result_persisted:
                    import cv2
                    from core.auto_trainer.race_route import RaceRoutePlanner
                    from core.perception import Perception
                    from core.screen_text import analyze_screen_text, fuse_perception_with_text

                    image = cv2.imread(source["source_path"])
                    perception = fuse_perception_with_text(Perception().analyze_image(image),
                        analyze_screen_text(source["frame_safety"]["visible_text"])) if image is not None else {}
                    if (image is None or RaceRoutePlanner._looks_like_ranking_list_result(image)
                        or not (
                            RaceRoutePlanner._looks_like_rank_overlay_result(perception, image)
                            or RaceRoutePlanner._looks_like_large_rank_result_animation(perception, image)
                        )
                        or hashlib.sha256(Path(source["source_path"]).read_bytes()).hexdigest() != source["source_sha256"]):
                        raise ValueError("quick_result_animation_unverified")
            if audit.get("result_pending") and not invocation.result_persisted:
                raise ValueError("result_not_persisted")
            if audit.get("result_pending") and source.get("action_id") != "race_result.continue":
                raise ValueError("result_resume_action_out_of_scope")
            if (not audit.get("result_pending") and not audit.get("quick_result_pending")
                and not audit.get("watching") and invocation.session.count_resume_preview_title is None):
                return
            if not attempt_claimed and _load_race_owner_session(args).to_dict() != audit["owner_baseline"]:
                raise ValueError("owner_baseline_changed")
            current = runtime.orchestrator.goal_ledger_store.load(runtime.ledger_path, expected_run_session_id=args.run_session_id)
            expected = entry.get("count_race_result") or json.loads(schedule_path.read_text(encoding="utf-8"))["count_reservation"]
            if current.revision != expected["ledger_revision"] or current.current_view_sha256 != expected["ledger_view_sha256"]:
                raise ValueError("ledger_changed")
            if attempt_claimed:
                return
            payload = {**audit, "race_route_session_id": invocation.race_route_session_id, "first_tap": source,
                       **({"recorded_result": entry["count_race_result"]} if entry.get("count_race_result") else {})}
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
            attempt.parent.mkdir(parents=True, exist_ok=True)
            with attempt.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if attempt.read_text(encoding="utf-8") != encoded:
                raise ValueError("attempt_readback_mismatch")
            attempt_claimed = True
        invocation.pre_tap = pre_tap
        entry["count_resume"] = {**audit, "attempt_path": str(attempt)}
        ok, stop, code = _run_count_reserved_race(args, turn_root, entry, schedule_path=schedule_path,
            runtime=runtime, turn_index=args.start_turn_index, resume_invocation=invocation)
        outer.update(is_valid=ok, status="stopped", stop_reason=stop)
        return outer, code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        outer.update(is_valid=False, status="stopped", stop_reason="count_resume:validation_failed", error=str(exc))
        return outer, 2


def _run_count_reserved_race(args, turn_root, turn_report, *, schedule_path, runtime, turn_index, resume_invocation=None):
    from core.auto_trainer.count_race_card_evidence import parse_count_race_card
    from core.auto_trainer.race_route import RaceRouteSession

    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    reservation = schedule["count_reservation"]
    ledger = runtime.orchestrator.goal_ledger_store.load(runtime.ledger_path, expected_run_session_id=args.run_session_id)
    if (reservation["run_session_id"] != args.run_session_id or reservation["observed_runner_turn"] != turn_index
        or reservation["ledger_revision"] != ledger.revision or reservation["ledger_view_sha256"] != ledger.current_view_sha256):
        return False, "count_race:reservation_changed", 2
    race_dir = _next_route_artifact_dir(turn_root, turn_report, "count_reserved_race")
    def card_reader(path):
        card = parse_count_race_card(path, source_frame_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            calendar=runtime.orchestrator.race_calendar)
        if card["race_id"] != reservation["race_id"] or card["calendar_turn"] != reservation["calendar_turn"]:
            raise ValueError("reserved_card_mismatch")
        return card
    session = RaceRouteSession(planned_race_ids=(reservation["race_id"],),
        schedule_packet_sha256=schedule["decision"]["packet_sha256"])
    invocation = resume_invocation or count_race_cli.CountRaceInvocation(args.run_session_id, turn_index, session, card_reader)
    session = invocation.session
    argv = _race_route_argv(args, race_dir, planned_race_ids=session.planned_race_ids,
                           schedule_packet_sha256=session.schedule_packet_sha256,
                           count_reserved_only=True)
    child_args = count_race_cli.parse_args(argv)
    child_args.max_steps = 1
    owner_before = _load_race_owner_session(args)
    report = None
    dialogue_handoffs = 0
    try:
        for _ in range(args.race_route_max_steps):
            report, code = count_race_cli.run(child_args, count_invocation=invocation)
            snapshot = race_dir / f"report_{len(invocation.steps):03d}.json"
            _write_canonical_json_atomic(snapshot, report)
            turn_report["race_route"] = {"code": code, "report": report}
            turn_report["count_entry_confirmed"] = session.scheduled_entry_consumed
            if code != 0:
                return False, report.get("stop_reason", "count_race:owner_failed"), code
            last = report["step_reports"][-1]["result"]
            reason = last.get("reason")
            if reason == "race_result:continue_not_allowed" and session.scheduled_entry_consumed:
                turn_report["count_race_result"] = _persist_count_race_result(runtime, schedule_path=schedule_path,
                    report_path=race_dir / f"result_{len(invocation.steps):03d}.json", report=report, run_session_id=args.run_session_id)
                invocation.result_persisted = True
                continue
            if reason == "post_race_dialogue" or (invocation.result_persisted and reason in {
                "event_choice", "ready_main_menu", "ready_training_menu",
                "goal_achievement_loading", "goal_achievement_result",
            }):
                handoff = _record_race_owner_handoff(args, report, owner_before)
                turn_report.setdefault("count_owner_handoffs", []).append(handoff)
                if invocation.result_persisted:
                    return True, reason, 0
                if dialogue_handoffs >= args.route_max_cycles:
                    return False, "count_race:dialogue_cycle_limit", 2
                dialogue_handoffs += 1
                safe_dir = _next_route_artifact_dir(turn_root, turn_report, "count_race_dialogue")
                safe_report, safe_code = _invoke_safe_advance(_safe_advance_argv(
                    args, safe_dir, career_goal_observed_turn=turn_index, transition_resync=False))
                _record_route_step(turn_report, "advance", safe_code, safe_report)
                if safe_code != 0 or safe_report.get("is_valid") is not True:
                    return False, safe_report.get("stop_reason", "count_race:dialogue_failed"), 2
                owner_before = _load_race_owner_session(args)
                if (owner_before.current_state not in {"race_list", "race_scene"}
                    or safe_report.get("route_transition") != owner_before.to_dict()):
                    return False, "count_race:dialogue_race_handoff_required", 2
                continue
            if last.get("performed") is not True:
                return False, reason or "count_race:stopped", 2
            if report.get("stop_reason") != "max_steps_reached":
                return False, report["stop_reason"], 2
        return False, "count_race:step_limit_reached", 2
    except (OSError, ValueError) as exc:
        turn_report["count_race_error"] = str(exc)
        return False, f"count_race:{exc}", 2


def _run_race_route_step(
    args: argparse.Namespace,
    turn_root: Path,
    turn_report: dict[str, Any],
    *,
    planned_race_ids: tuple[str, ...] = (),
    schedule_packet_sha256: str | None = None,
    suppress_result_continue: bool = False,
    wbc_only: bool = False,
    fan_shortfall_only: bool = False,
    force_return_only: bool = False,
    allow_wbc_confirmation_recovery: bool = False,
) -> tuple[bool, str, int]:
    if fan_shortfall_only and (wbc_only or planned_race_ids or schedule_packet_sha256 is not None):
        return False, "fan_shortfall_dispatch:conflicting_binding", 2
    if force_return_only and (
        wbc_only
        or fan_shortfall_only
        or planned_race_ids
        or schedule_packet_sha256 is not None
        or allow_wbc_confirmation_recovery
    ):
        return False, "race_list_return:conflicting_binding", 2
    if allow_wbc_confirmation_recovery:
        route_steps = turn_report.get("route_steps")
        latest_report = (
            route_steps[-1].get("report")
            if isinstance(route_steps, list)
            and route_steps
            and isinstance(route_steps[-1], dict)
            else None
        )
        if (
            wbc_only
            or fan_shortfall_only
            or planned_race_ids
            or schedule_packet_sha256 is not None
            or not isinstance(latest_report, dict)
            or not _advance_report_is_wbc_confirmation_handoff(latest_report)
        ):
            return False, "wbc_confirmation_recovery:invalid_binding", 2
    wbc_owner = None
    if wbc_only:
        if args.recover_career_goal_from_main_menu or planned_race_ids or schedule_packet_sha256 is not None:
            return False, "wbc_dispatch:unrelated_binding_or_recovery", 2
        try:
            wbc_owner = _load_wbc_dispatch_session(args, turn_report)
        except (OSError, RouteTransitionError) as exc:
            return False, f"wbc_dispatch:{exc}", 2
    return_only = (
        force_return_only
        or (
            not wbc_only
            and args.allow_leave_race_list is True
            and args.allow_enter_race is False
            and args.allow_confirm_race is False
        )
    )
    pending_reason = _pending_career_goal_stop_reason(
        args,
        allow_pending_count_for_wbc=(
            wbc_only or allow_wbc_confirmation_recovery
        ),
    )
    if pending_reason is not None and not return_only:
        return False, pending_reason, 2
    if return_only:
        if args.allow_leave_race_list is not True:
            return False, "race_list_return_not_allowed", 2
        if args.mode != "live" or args.race_route_execute is not True:
            return False, "race_list_return_execution_required", 2
        if planned_race_ids or schedule_packet_sha256 is not None:
            return False, "race_list_return_planned_binding_forbidden", 2
        try:
            _load_race_list_return_session(args)
        except (OSError, RouteTransitionError) as exc:
            return False, f"race_list_return_preflight:{exc}", 2
    owner_before = None
    if not return_only and args.mode == "live" and args.race_route_execute is True:
        try:
            owner_before = wbc_owner or _load_race_owner_session(args)
        except (OSError, RouteTransitionError) as exc:
            return False, f"race_owner_preflight:{exc}", 2
    race_dir = _next_route_artifact_dir(turn_root, turn_report, "race_route")
    race_report, race_code = _invoke_race_route(
        _race_route_argv(
            args,
            race_dir,
            planned_race_ids=planned_race_ids,
            schedule_packet_sha256=schedule_packet_sha256,
            suppress_result_continue=suppress_result_continue,
            wbc_only=wbc_only,
            fan_shortfall_only=fan_shortfall_only,
            return_only=return_only,
        )
    )
    _record_route_step(turn_report, "race_route", race_code, race_report)
    if "race_route" not in turn_report:
        turn_report["race_route"] = {"code": race_code, "report": race_report}
    if race_code != 0 or not race_report.get("is_valid", False):
        return False, str(race_report.get("status", "race_route_failed")), 2
    if return_only:
        steps = race_report.get("step_reports")
        if (
            race_report.get("performed_steps") != 1
            or not isinstance(steps, list) or len(steps) != 1
            or not isinstance(steps[0], dict)
        ):
            return False, "race_list_return_single_step_required", 2
        try:
            receipt = _record_race_list_return_receipt(args, steps[0].get("result"))
        except (OSError, RouteTransitionError) as exc:
            return False, f"race_list_return_receipt:{exc}", 2
        turn_report["race_list_return_receipt"] = receipt
        return True, "race_list_return_receipt_persisted", 0
    if owner_before is not None and race_report.get("stop_reason") in {
        "post_race_dialogue", "event_choice", "ready_main_menu", "ready_training_menu",
        "goal_achievement_loading", "goal_achievement_result", "resource_prompt",
    }:
        try:
            turn_report["race_owner_handoff"] = _record_race_owner_handoff(args, race_report, owner_before)
        except (OSError, RouteTransitionError) as exc:
            return False, f"race_owner_handoff:{exc}", 2
    return True, str(race_report.get("stop_reason")), 0


def _load_wbc_dispatch_session(args: argparse.Namespace, turn_report: dict[str, Any]) -> RouteTransitionSession:
    route_steps = turn_report.get("route_steps")
    last = route_steps[-1] if isinstance(route_steps, list) and route_steps else None
    if (not isinstance(last, dict) or last.get("kind") not in {
            "advance", "advance_ready_verify", "advance_wbc_resync"}
        or last.get("code") != 0 or last.get("success") is not True):
        raise RouteTransitionError("safe_report_required")
    report = last.get("report")
    if (not isinstance(report, dict) or report.get("is_valid") is not True
        or report.get("status") != "stopped" or report.get("stop_reason") != "wbc_race_hub"):
        raise RouteTransitionError("safe_report_invalid")
    steps = report.get("step_reports")
    result = steps[-1].get("result") if isinstance(steps, list) and steps and isinstance(steps[-1], dict) else None
    if (not isinstance(result, dict) or result.get("performed") is not False
        or result.get("reason") != "wbc_race_hub" or result.get("tap_performed") is True):
        raise RouteTransitionError("safe_stop_required")
    decision, packet, gate = result.get("decision"), result.get("frame_safety"), result.get("safety_verdict")
    if (not isinstance(decision, dict) or decision.get("action") != "stop"
        or decision.get("reason") != "wbc_race_hub" or decision.get("tap") is not None
        or not isinstance(decision.get("evidence"), dict) or not isinstance(packet, dict)
        or not isinstance(gate, dict) or gate.get("allowed") is not True
        or gate.get("reason") != "non_executing_decision" or gate.get("action_id") is not None):
        raise RouteTransitionError("safe_gate_invalid")
    source = decision["evidence"].get("image")
    if not isinstance(source, (str, Path)) or not str(source):
        raise RouteTransitionError("safe_source_missing")
    raw = Path(source).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if not raw or packet.get("source_image_sha256") != sha or gate.get("source_image_sha256") != sha:
        raise RouteTransitionError("safe_source_sha_mismatch")
    session = _load_race_owner_session(args)
    policy = result.get("route_policy")
    pending_policy = (
        isinstance(policy, dict)
        and policy.get("source") == "pending"
        and isinstance(policy.get("allowed_observed_states"), (list, tuple))
        and "race_list" in policy.get("allowed_observed_states", [])
        and bool(policy.get("pending_action_id"))
        and policy.get("pending_action_id") == session.last_action_id
        and bool(policy.get("action_source_sha256"))
        and policy.get("action_source_sha256") != sha
    )
    explicit_resync_policy = (
        last.get("kind") == "advance_wbc_resync"
        and isinstance(policy, dict)
        and policy.get("source") == "bootstrap"
        and policy.get("allowed_observed_states") == ["race_list"]
        and policy.get("pending_action_id") is None
        and policy.get("action_source_sha256") is None
    )
    if (session.current_state != "race_list" or session.observation_sha256 != sha
        or report.get("route_transition") != session.to_dict()
        or result.get("route_transition") != session.to_dict()
        or not isinstance(policy, dict)
        or policy.get("observed_state") != "race_list" or policy.get("source_image_sha256") != sha
        or not (pending_policy or explicit_resync_policy)):
        raise RouteTransitionError("safe_observation_contract_invalid")
    return session


def _load_race_owner_session(args: argparse.Namespace) -> RouteTransitionSession:
    path = _route_transition_path(args)
    session = RouteTransitionSession.load(path, expected_run_session_id=args.run_session_id)
    if (session.status == RouteTransitionStatus.TRACKING and session.mismatch_state is None
            and session.to_dict() == getattr(args, "verified_race_dialogue_handoff_owner", None)):
        return session
    if (session.status != RouteTransitionStatus.TRACKING or session.mismatch_state is not None
        or session.expected_next_states or session.action_source_sha256 is not None):
        raise RouteTransitionError("race_owner_session_not_idle")
    return session


def _settle_race_day_owner_preflight(
    args: argparse.Namespace,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Consume only a verified delayed post-action contract at a race-day menu."""

    path = _route_transition_path(args)
    before = RouteTransitionSession.load(
        path,
        expected_run_session_id=args.run_session_id,
    )
    idle = (
        before.status == RouteTransitionStatus.TRACKING
        and before.mismatch_state is None
        and not before.expected_next_states
        and before.action_source_sha256 is None
    )
    if idle:
        if before.current_state != "main_menu":
            raise RouteTransitionError("race_day_idle_state_invalid")
        return {
            "status": "already_idle",
            "before": before.to_dict(),
            "after": before.to_dict(),
        }

    if (
        before.status != RouteTransitionStatus.TRACKING
        or before.mismatch_state is not None
        or before.current_state != "main_menu"
    ):
        raise RouteTransitionError("race_day_pending_session_invalid")
    if before.last_action_id not in {
        "training.execute",
        "safe_advance.rest_confirm",
        "safe_advance.rest_outing_confirm",
    } | {f"outing.satake_mei.{stage}" for stage in range(1, 6)}:
        raise RouteTransitionError("race_day_pending_action_invalid")
    if (
        before.expected_next_states != POST_ACTION_ROUTE_SUCCESSORS
        or before.action_source_sha256 is None
    ):
        raise RouteTransitionError("race_day_pending_contract_invalid")

    steps = report.get("step_reports")
    if (
        report.get("is_valid") is not True
        or report.get("status") != "stopped"
        or report.get("stop_reason") != "race_day_main_menu"
        or not isinstance(steps, list)
        or not steps
        or not isinstance(steps[-1], dict)
    ):
        raise RouteTransitionError("race_day_safe_report_invalid")
    performed = report.get("performed_steps")
    if (
        type(performed) is not int
        or performed < 0
        or performed
        != sum(
            isinstance(step, dict)
            and isinstance(step.get("result"), dict)
            and step["result"].get("performed") is True
            for step in steps
        )
    ):
        raise RouteTransitionError("race_day_safe_performed_count_invalid")

    last = steps[-1]
    result = last.get("result")
    if (
        not isinstance(result, dict)
        or result.get("performed") is not False
        or result.get("tap_performed") is True
        or result.get("reason") != "race_day_main_menu"
    ):
        raise RouteTransitionError("race_day_safe_stop_invalid")
    decision = result.get("decision")
    packet = result.get("frame_safety")
    verdict = result.get("safety_verdict")
    policy = result.get("route_policy")
    if (
        not isinstance(decision, dict)
        or not is_route_handoff(decision, reason="race_day_main_menu")
        or not isinstance(decision.get("evidence"), dict)
        or not isinstance(packet, dict)
        or packet.get("dangerous") is not False
        or packet.get("errors") not in ([], ())
        or not isinstance(verdict, dict)
        or verdict.get("allowed") is not True
        or verdict.get("reason") != "non_executing_decision"
        or verdict.get("action_id") is not None
    ):
        raise RouteTransitionError("race_day_safe_gate_invalid")
    source_value = decision["evidence"].get("image")
    if not isinstance(source_value, (str, Path)) or not str(source_value):
        raise RouteTransitionError("race_day_safe_source_missing")
    try:
        source = Path(source_value).resolve(strict=True)
        source_bytes = source.read_bytes()
    except OSError as exc:
        raise RouteTransitionError(f"race_day_safe_source:{exc}") from exc
    if not source_bytes:
        raise RouteTransitionError("race_day_safe_source_empty")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    screenshot_value = last.get("screenshot_path")
    try:
        screenshot = Path(screenshot_value).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise RouteTransitionError("race_day_safe_screenshot_invalid") from exc
    if (
        screenshot != source
        or packet.get("source_image_sha256") != source_sha
        or verdict.get("source_image_sha256") != source_sha
        or before.observation_sha256 != source_sha
        or before.action_source_sha256 == source_sha
    ):
        raise RouteTransitionError("race_day_safe_source_sha_mismatch")
    if not _is_race_day_main_menu(str(source)):
        from core.auto_trainer.safe_advance import SafeAdvancePlanner

        larc_page = SafeAdvancePlanner().plan(
            source, scenario="larc", visible_text=packet.get("visible_text"),
        )
        if not is_route_handoff(larc_page, reason="race_day_main_menu"):
            raise RouteTransitionError("race_day_safe_layout_invalid")
    if (
        not isinstance(policy, dict)
        or policy.get("source") != "pending"
        or policy.get("observed_state") != "main_menu"
        or policy.get("source_image_sha256") != source_sha
        or policy.get("allowed_observed_states")
        != list(POST_ACTION_ROUTE_SUCCESSORS)
        or policy.get("pending_action_id") != before.last_action_id
        or policy.get("action_source_sha256") != before.action_source_sha256
    ):
        raise RouteTransitionError("race_day_safe_policy_invalid")
    before_payload = before.to_dict()
    if (
        report.get("route_transition") != before_payload
        or result.get("route_transition") != before_payload
    ):
        raise RouteTransitionError("race_day_transition_evidence_mismatch")

    current = RouteTransitionSession.load(
        path,
        expected_run_session_id=args.run_session_id,
    )
    if current.to_dict() != before_payload:
        raise RouteTransitionError("race_day_owner_session_changed")
    candidate = copy.deepcopy(before)
    candidate.observe("main_menu", source_sha256=source_sha, resync=True)
    candidate.save(path)
    persisted = RouteTransitionSession.load(
        path,
        expected_run_session_id=args.run_session_id,
    )
    if persisted.to_dict() != candidate.to_dict():
        raise RouteTransitionError("race_day_owner_preflight_readback_mismatch")
    return {
        "status": "settled",
        "before": before_payload,
        "after": persisted.to_dict(),
        "report_sha256": _canonical_sha256(report),
        "source_image_sha256": source_sha,
    }


def _record_race_owner_handoff(
    args: argparse.Namespace, report: dict[str, Any], before: RouteTransitionSession,
) -> dict[str, Any]:
    stop = report.get("stop_reason")
    states = {"post_race_dialogue": "dialogue", "event_choice": "event_choice",
              "ready_main_menu": "main_menu", "ready_training_menu": "training_menu",
              "goal_achievement_loading": "loading", "goal_achievement_result": "dialogue",
              "race_day:enter_not_allowed": "main_menu", "resource_prompt": "resource_prompt"}
    steps = report.get("step_reports")
    if (report.get("is_valid") is not True or report.get("status") not in {"stopped", "handoff"} or stop not in states
        or not isinstance(steps, list) or not steps or not isinstance(steps[-1], dict)):
        raise RouteTransitionError("race_owner_terminal_report_invalid")
    performed = report.get("performed_steps")
    if (type(performed) is not int or performed < (0 if report.get('live_takeover') is True else 1)
        or performed != sum(isinstance(step, dict) and isinstance(step.get("result"), dict)
                            and step["result"].get("performed") is True for step in steps)):
        raise RouteTransitionError("race_owner_performed_count_invalid")
    result = steps[-1].get("result")
    if not isinstance(result, dict) or result.get("performed") is not False or result.get("reason") != stop:
        raise RouteTransitionError("race_owner_terminal_result_invalid")
    decision, packet, verdict = result.get("decision"), result.get("frame_safety"), result.get("safety_verdict")
    typed = report["status"] == "handoff"
    if typed:
        terminal = is_route_handoff_result(result) and report.get("handoff_target") == states[stop]
    else:
        terminal = (isinstance(decision, dict) and decision.get("action") == "stop"
                    and decision.get("reason") == stop)
    if (not terminal or not isinstance(decision.get("evidence"), dict) or not isinstance(packet, dict)
        or not isinstance(verdict, dict) or verdict.get("allowed") is not True
        or verdict.get("reason") != "non_executing_decision" or verdict.get("action_id") is not None
        or typed and (packet.get("dangerous") is True or packet.get("resource_terms"))):
        raise RouteTransitionError("race_owner_terminal_gate_invalid")
    source = decision["evidence"].get("image")
    if not isinstance(source, (str, Path)) or not str(source):
        raise RouteTransitionError("race_owner_terminal_source_missing")
    raw = Path(source).read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    if (not raw or packet.get("source_image_sha256") != source_sha
        or verdict.get("source_image_sha256") != source_sha):
        raise RouteTransitionError("race_owner_terminal_source_sha_mismatch")
    if stop == "resource_prompt":
        import cv2
        from core.auto_trainer.race_route import RaceRoutePlanner
        from core.auto_trainer.safe_advance import SafeAdvancePlanner
        from core.perception import Perception
        from core.screen_text import analyze_screen_text, fuse_perception_with_text

        image = cv2.imread(str(source))
        if (before.current_state != "main_menu" or len(steps) != 2 or performed != 1
            or decision.get("tap") is not None or packet.get("dangerous") is not True
            or image is None or SafeAdvancePlanner._clock_retry_cancel_decision(
                image, analyze_screen_text(packet.get("visible_text", "")), {}) is None):
            raise RouteTransitionError("race_owner_clock_cancel_terminal_invalid")
        prior = steps[0].get("result", {})
        prior_decision, prior_packet, prior_gate = (prior.get(key, {}) for key in ("decision", "frame_safety", "safety_verdict"))
        prior_source = prior_decision.get("evidence", {}).get("image")
        if (prior.get("performed") is not True or prior.get("tap_performed") is not True
            or prior.get("reason") != "race_result_continue"
            or prior_decision.get("action") != "tap" or prior_decision.get("reason") != "race_result_continue"
            or prior_decision.get("route_state") != "race_result"
            or prior_decision.get("tap") != {"coordinate_name": "race_result.continue", "x": 360, "y": 1062, "scaled": False}
            or not isinstance(prior_source, (str, Path))
            or prior_gate.get("allowed") is not True or prior_gate.get("action_id") != "race_result.continue"
            or prior_gate.get("reason") != "specific_action_allowed" or prior_packet.get("dangerous") is not False):
            raise RouteTransitionError("race_owner_clock_cancel_prior_invalid")
        prior_sha = hashlib.sha256(Path(prior_source).read_bytes()).hexdigest()
        prior_image = cv2.imread(str(prior_source))
        if (prior_sha == source_sha or prior_packet.get("source_image_sha256") != prior_sha
            or prior_gate.get("source_image_sha256") != prior_sha or prior_image is None):
            raise RouteTransitionError("race_owner_clock_cancel_prior_source_invalid")
        perception = fuse_perception_with_text(Perception().analyze_image(prior_image),
            analyze_screen_text(prior_packet.get("visible_text", "")))
        brown = RaceRoutePlanner._compact_brown_result_overlay(perception, prior_image)
        proof = prior_decision.get("evidence", {}).get("compact_brown_overlay_evidence")
        if brown is None or proof != {**brown, "source_image_sha256": prior_sha}:
            raise RouteTransitionError("race_owner_clock_cancel_prior_layout_invalid")
    if stop == "race_day:enter_not_allowed":
        import cv2
        from core.auto_trainer.safe_advance import SafeAdvancePlanner

        return_verified = SafeAdvancePlanner._looks_like_grand_masters_final_race_day_main_menu(cv2.imread(str(source)))
        if not return_verified:
            larc_return = SafeAdvancePlanner().plan(source, scenario="larc", visible_text=packet.get("visible_text", ""))
            return_verified = is_route_handoff(larc_return, reason="race_day_main_menu")
        if (before.current_state != "main_menu" or len(steps) != 2 or performed != 1
            or decision.get("tap") is not None
            or not return_verified):
            raise RouteTransitionError("race_owner_final_return_invalid")
        back = steps[0].get("result", {})
        back_decision, receipt = back.get("decision", {}), back.get("route_action_receipt_source", {})
        back_packet, back_gate = back.get("frame_safety", {}), back.get("safety_verdict", {})
        if (back.get("performed") is not True or back.get("tap_performed") is not True
            or back.get("reason") not in {"leave_selected_non_g1_race_list", "leave_unverified_g1_race_list"}
            or back_decision.get("action") != "tap" or back_decision.get("reason") != back.get("reason")
            or back_decision.get("route_state") != "race_list"
            or (back_decision.get("tap") or {}).get("coordinate_name") != "race_list.back"
            or receipt.get("action_id") != "race_list.back" or receipt.get("action_state") != "race_list"
            or receipt.get("coordinate_name") != "race_list.back"
            or not isinstance(receipt.get("source_path"), (str, Path))
            or not isinstance(back_decision.get("evidence", {}).get("image"), (str, Path))
            or Path(receipt["source_path"]).resolve() != Path(back_decision["evidence"]["image"]).resolve()
            or back_gate.get("allowed") is not True or back_gate.get("action_id") != "race_list.back"):
            raise RouteTransitionError("race_owner_final_return_receipt_invalid")
        back_sha = hashlib.sha256(Path(receipt["source_path"]).read_bytes()).hexdigest()
        if (back_sha == source_sha or receipt.get("source_sha256") != back_sha
            or back_packet.get("source_image_sha256") != back_sha or back_gate.get("source_image_sha256") != back_sha):
            raise RouteTransitionError("race_owner_final_return_source_mismatch")
    if _load_race_owner_session(args).to_dict() != before.to_dict():
        raise RouteTransitionError("race_owner_session_changed")
    candidate = copy.deepcopy(before)
    if stop == "resource_prompt":
        from dataclasses import replace
        candidate = replace(before, current_state="resource_prompt", observation_sha256=source_sha,
            transition_version=before.transition_version + 1)
        candidate._validate_invariants()
    else:
        candidate.observe(states[stop], source_sha256=source_sha, resync=True)
    if stop in {"post_race_dialogue", "goal_achievement_loading"}:
        candidate.commit_action("wait:race_route_owner_handoff",
            expected_next_states=("dialogue", "event_choice", "loading", "main_menu", "modal", "training_menu"),
            source_sha256=source_sha)
    path = _route_transition_path(args)
    candidate.save(path)
    persisted = RouteTransitionSession.load(path, expected_run_session_id=args.run_session_id)
    if persisted.to_dict() != candidate.to_dict():
        raise RouteTransitionError("race_owner_handoff_readback_mismatch")
    return {"before": before.to_dict(), "after": persisted.to_dict(),
            "report_sha256": _canonical_sha256(report), "terminal_source_sha256": source_sha,
            **({"observation_handoff": {"kind": "race_result_clock_cancel", "new_tap": False, "new_wait": False}}
               if stop == "resource_prompt" else {})}


def _load_race_list_return_session(args: argparse.Namespace) -> tuple[Path, RouteTransitionSession]:
    path = _route_transition_path(args)
    session = RouteTransitionSession.load(path, expected_run_session_id=args.run_session_id)
    if (
        session.status != RouteTransitionStatus.TRACKING
        or session.current_state != "race_list"
        or session.expected_next_states
        or session.action_source_sha256 is not None
        or session.mismatch_state is not None
    ):
        raise RouteTransitionError("race_list_return_session_not_idle")
    return path, session


def _record_race_list_return_receipt(args: argparse.Namespace, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("performed") is not True or result.get("tap_performed") is not True:
        raise RouteTransitionError("race_list_return_not_performed")
    decision = result.get("decision")
    source = result.get("route_action_receipt_source")
    if (
        result.get("reason") not in {
            "leave_selected_non_g1_race_list",
            "leave_unverified_g1_race_list",
        }
        or not isinstance(decision, dict) or decision.get("action") != "tap"
        or decision.get("reason") != result["reason"] or decision.get("route_state") != "race_list"
        or not isinstance(decision.get("tap"), dict)
        or decision["tap"].get("coordinate_name") != "race_list.back"
        or not isinstance(source, dict)
        or source.get("action_id") != "race_list.back" or source.get("action_state") != "race_list"
        or source.get("coordinate_name") != "race_list.back"
        or not isinstance(source.get("source_path"), (str, Path))
    ):
        raise RouteTransitionError("race_list_return_source_invalid")
    packet, verdict = result.get("frame_safety"), result.get("safety_verdict")
    actual_sha = hashlib.sha256(Path(source["source_path"]).read_bytes()).hexdigest()
    if (
        source.get("source_sha256") != actual_sha
        or not isinstance(packet, dict) or packet.get("source_image_sha256") != actual_sha
        or not isinstance(verdict, dict) or verdict.get("allowed") is not True
        or verdict.get("action_id") != "race_list.back"
        or verdict.get("source_image_sha256") != actual_sha
    ):
        raise RouteTransitionError("race_list_return_source_sha_mismatch")
    path, session = _load_race_list_return_session(args)
    session.observe("race_list", source_sha256=actual_sha)
    session.commit_action("race_list.back", expected_next_states=("loading", "main_menu"), source_sha256=actual_sha)
    session.save(path)
    return session.to_dict()


def _build_event_owner_context_shadow(
    *,
    turn_report: dict[str, Any],
    turn_authority: TurnAuthorityBundle,
    prior_event_context: PriorTurnEventContextAuthority | None,
    run_trainee_authority: RunTraineeIdentityAuthority | None,
) -> EventOwnerContextAuthority | None:
    reason: str | None = None
    owner_context: EventOwnerContextAuthority | None = None
    if prior_event_context is None:
        reason = "prior_event_context_unavailable"
    elif run_trainee_authority is None:
        reason = "run_trainee_identity_unavailable"
    else:
        try:
            owner_context = build_trainee_event_owner_context_from_prior(
                turn_authority=turn_authority,
                trainee_authority=run_trainee_authority,
                prior_context=prior_event_context,
            )
        except EventOwnerContextError as exc:
            reason = exc.code
    if owner_context is None:
        turn_report["event_owner_context_shadow"] = {
            "schema_version": "uma.event_owner_context_shadow_slot.v1",
            "status": "abstain",
            "reason": reason or "event_owner_context_unavailable",
            "action_eligible": False,
            "usable_for_action": False,
        }
        return None
    turn_report["event_owner_context_shadow"] = {
        "schema_version": "uma.event_owner_context_shadow_slot.v1",
        "status": "available",
        "authority": owner_context.to_dict(),
        "action_eligible": False,
        "usable_for_action": False,
    }
    return owner_context


def _run_event_choice_step(
    args: argparse.Namespace,
    turn_root: Path,
    turn_report: dict[str, Any],
    *,
    control_driver: CooperativeRunnerDriver | None = None,
    event_identity_shadow: EventIdentityShadowReport | None = None,
    turn_authority: TurnAuthorityBundle | None = None,
    event_identity_artifact: EventIdentityShadowArtifact | None = None,
    current_turn_index: int | None = None,
    prior_event_context: PriorTurnEventContextAuthority | None = None,
    run_trainee_authority: RunTraineeIdentityAuthority | None = None,
) -> tuple[bool, str, int]:
    if not args.event_choice_between_turns:
        return False, "event_choice:disabled", 0
    event_dir = _next_route_artifact_dir(turn_root, turn_report, "event_choice")
    event_argv = _event_choice_argv(
        args,
        event_dir,
        current_turn_index=current_turn_index,
    )
    event_report, event_code = (
        _invoke_event_choice(event_argv)
        if control_driver is None
        else _invoke_event_choice(event_argv, control_driver=control_driver)
    )
    _record_route_step(turn_report, "event_choice", event_code, event_report)
    formal_authority, formal_capture_reason = _event_turn_authority_from_capture(
        event_report=event_report,
        event_dir=event_dir,
        current_turn_index=current_turn_index,
    )
    if turn_authority is None and formal_authority is not None:
        turn_authority = formal_authority
    owner_context_authority = (
        _build_event_owner_context_shadow(
            turn_report=turn_report,
            turn_authority=formal_authority,
            prior_event_context=prior_event_context,
            run_trainee_authority=run_trainee_authority,
        )
        if formal_authority is not None
        else None
    )
    if (
        event_identity_artifact is None
        and event_identity_shadow is None
        and formal_authority is not None
    ):
        event_identity_shadow = _produce_formal_event_identity_shadow(
            authority=formal_authority,
            formal_capture=event_report.get("formal_capture"),
            artifact_path=event_dir / "event_choice_step_001.png",
            owner_context_authority=owner_context_authority,
        )
    verified_identity_shadow: EventIdentityShadowReport | None = None
    if event_identity_artifact is not None:
        shadow, abstain_reason = _verified_event_identity_shadow_artifact(
            turn_root=turn_root,
            authority=turn_authority,
            artifact=event_identity_artifact,
        )
        verified_identity_shadow = shadow
        shadow_payload = (
            _record_event_identity_shadow(turn_report, shadow)
            if shadow is not None
            else _event_choice_shadow_abstain(abstain_reason)
        )
    elif (
        isinstance(turn_authority, TurnAuthorityBundle)
        and isinstance(event_identity_shadow, EventIdentityShadowReport)
    ):
        artifact, abstain_reason = _event_identity_shadow_artifact_from_route(
            authority=turn_authority,
            event_dir=event_dir,
            event_report=event_report,
            shadow=event_identity_shadow,
        )
        if artifact is None:
            shadow_payload = _event_choice_shadow_abstain(abstain_reason)
        else:
            shadow, abstain_reason = _verified_event_identity_shadow_artifact(
                turn_root=turn_root,
                authority=turn_authority,
                artifact=artifact,
            )
            verified_identity_shadow = shadow
            shadow_payload = (
                _record_event_identity_shadow(turn_report, shadow)
                if shadow is not None
                else _event_choice_shadow_abstain(abstain_reason)
            )
    elif isinstance(event_identity_shadow, EventIdentityShadowReport):
        shadow_payload = _event_choice_shadow_abstain(
            "event_shadow_authority_unavailable"
        )
    elif formal_capture_reason is not None:
        shadow_payload = _event_choice_shadow_abstain(formal_capture_reason)
    else:
        shadow_payload = _event_choice_shadow_abstain(
            "reference_evidence_unavailable"
        )
    turn_report["event_choice_shadow"] = shadow_payload
    turn_report["event_local_decision_shadow"] = (
        build_event_local_decision_runtime_shadow(
            authority=(
                turn_authority
                if isinstance(turn_authority, TurnAuthorityBundle)
                else None
            ),
            identity_shadow=verified_identity_shadow,
        )
    )
    if "event_choice" not in turn_report:
        turn_report["event_choice"] = {"code": event_code, "report": event_report}
    if event_code != 0 or not event_report.get("is_valid", False):
        return False, str(event_report.get("status", "event_choice_failed")), 2
    event_stop = str(event_report.get("stop_reason"))
    if event_stop not in {"max_steps_reached", "ready_main_menu", "ready_training_menu"}:
        return False, f"event_choice:{event_stop}", 0
    return True, event_stop, 0


def _event_choice_receipt_persisted(turn_report: dict[str, Any]) -> bool:
    event_report = None
    route_steps = turn_report.get("route_steps")
    if isinstance(route_steps, list):
        for step in reversed(route_steps):
            if isinstance(step, dict) and step.get("kind") == "event_choice":
                event_report = step.get("report")
                break
    if event_report is None:
        event_step = turn_report.get("event_choice")
        if isinstance(event_step, dict):
            event_report = event_step.get("report")
    if not isinstance(event_report, dict):
        return False
    event_transition = event_report.get("route_transition")
    source_sha256 = (
        event_transition.get("action_source_sha256")
        if isinstance(event_transition, dict)
        else None
    )
    return (
        isinstance(event_transition, dict)
        and event_transition.get("status") == "tracking"
        and event_transition.get("current_state") == "event_choice"
        and event_transition.get("last_action_id") in {
            "event_choice.top",
            "event_choice.bottom",
            "event_choice.dialogue_next",
        }
        and type(source_sha256) is str
        and len(source_sha256) == 64
        and not any(char not in "0123456789abcdef" for char in source_sha256)
        and event_transition.get("expected_next_states")
        == list(POST_ACTION_ROUTE_SUCCESSORS)
    )


def _event_turn_authority_from_capture(
    *,
    event_report: dict[str, Any],
    event_dir: Path,
    current_turn_index: int | None,
) -> tuple[TurnAuthorityBundle | None, str | None]:
    capture = event_report.get("formal_capture")
    if capture is None:
        return None, None
    if not isinstance(capture, dict) or type(current_turn_index) is not int:
        return None, "event_shadow_formal_capture_invalid"
    required = {
        "schema_version",
        "run_session_id",
        "generation",
        "global_action_turn",
        "game_period",
        "page_state",
        "source_artifact_path",
        "source_frame_sha256",
        "capture_ttl_turns",
        "expires_after_global_action_turn",
        "capture_metadata_sha256",
    }
    if set(capture) != required:
        return None, "event_shadow_formal_capture_schema_invalid"
    metadata_sha256 = capture["capture_metadata_sha256"]
    material = {key: value for key, value in capture.items() if key != "capture_metadata_sha256"}
    if (
        not isinstance(metadata_sha256, str)
        or metadata_sha256 != _canonical_sha256(material)
        or capture["schema_version"] != "uma.event_choice_formal_capture.v1"
        or not isinstance(capture["run_session_id"], str)
        or not capture["run_session_id"]
        or type(capture["generation"]) is not int
        or capture["generation"] <= 0
        or capture["global_action_turn"] != current_turn_index
        or capture["game_period"] != "unknown"
        or capture["page_state"] != "event_choice"
        or capture["capture_ttl_turns"] != 0
        or capture["expires_after_global_action_turn"] != current_turn_index
        or not isinstance(capture["source_frame_sha256"], str)
        or len(capture["source_frame_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in capture["source_frame_sha256"])
        or not isinstance(capture["source_artifact_path"], str)
    ):
        return None, "event_shadow_formal_capture_invalid"
    try:
        artifact_path = Path(capture["source_artifact_path"]).resolve(strict=True)
        expected_path = (event_dir / "event_choice_step_001.png").resolve(strict=True)
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError:
        return None, "event_shadow_formal_capture_artifact_unreadable"
    if artifact_path != expected_path:
        return None, "event_shadow_formal_capture_path_mismatch"
    if artifact_sha256 != capture["source_frame_sha256"]:
        return None, "event_shadow_formal_capture_sha_mismatch"
    try:
        return (
            build_turn_authority_bundle(
                run_session_id=capture["run_session_id"],
                generation=capture["generation"],
                global_action_turn=capture["global_action_turn"],
                game_period=capture["game_period"],
                source_frame_sha256=capture["source_frame_sha256"],
                perception_packet_sha256=_canonical_sha256(material),
                condition_authority_sha256=None,
                goal_ledger_sha256=None,
                race_plan_sha256=None,
                training_packet_sha256=None,
                scenario_packet_sha256=None,
                unknown_fields=(
                    "condition_authority_sha256",
                    "goal_ledger_sha256",
                    "race_plan_sha256",
                    "scenario_packet_sha256",
                    "training_packet_sha256",
                ),
                hazard_flags=(),
            ),
            None,
        )
    except ValueError:
        return None, "event_shadow_formal_capture_authority_invalid"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _record_training_route_receipt(
    *,
    args: argparse.Namespace,
    execution: dict[str, Any],
) -> dict[str, Any]:
    if execution.get("performed") is not True or execution.get("reason") != "executed":
        raise RouteTransitionError("training_action_not_executed")
    target = execution.get("target")
    if target not in TRAINING_TYPES:
        raise RouteTransitionError("training_action_target_invalid")
    source = execution.get("route_action_receipt_source")
    if not isinstance(source, dict):
        raise RouteTransitionError("training_action_receipt_source_missing")
    expected_coordinate = f"training_menu.{target}_decide"
    if (
        source.get("action_id") != "training.execute"
        or source.get("action_state") != "training_menu"
        or source.get("coordinate_name") != expected_coordinate
    ):
        raise RouteTransitionError("training_action_receipt_source_invalid")
    source_path = source.get("source_path")
    if not isinstance(source_path, (str, Path)):
        raise RouteTransitionError("training_action_receipt_source_invalid")
    updated = record_external_action_receipt(
        _route_transition_path(args),
        expected_run_session_id=args.run_session_id,
        action_state="training_menu",
        action_id="training.execute",
        source_path=source_path,
        source_sha256=source.get("source_sha256"),
    )
    return updated.to_dict()


def _record_rest_route_receipt(
    *,
    args: argparse.Namespace,
    execution: dict[str, Any],
) -> dict[str, Any]:
    if (
        execution.get("performed") is not True
        or execution.get("reason") not in {"executed_rest_confirmed","executed_normal_outing"}
    ):
        raise RouteTransitionError("rest_action_not_confirmed")
    source = execution.get("route_action_receipt_source")
    if not isinstance(source, dict):
        raise RouteTransitionError("rest_action_receipt_source_missing")
    normal_outing=execution.get('reason')=='executed_normal_outing'
    recovery_kind='outing' if normal_outing else 'rest'
    direct = (execution.get('confirmation_mode') == 'disabled'
        and source.get('action_id') == recovery_kind+'.execute'
        and source.get('action_state') == 'main_menu'
        and source.get('coordinate_name') == 'main_menu.'+recovery_kind
        and execution.get('direct_start_observation', {}).get('screen') in {'main_menu','dialogue','event_choice'})
    modal = (source.get("action_id") in ({'safe_advance.normal_outing_confirm'} if normal_outing else
            {"safe_advance.rest_confirm", "safe_advance.rest_outing_confirm"})
        and source.get("action_state") == "modal"
        and source.get("coordinate_name") == source.get("action_id"))
    if not (direct or modal):
        raise RouteTransitionError("rest_action_receipt_source_invalid")
    source_path = source.get("source_path")
    if not isinstance(source_path, (str, Path)):
        raise RouteTransitionError("rest_action_receipt_source_invalid")
    updated = record_external_action_receipt(
        _route_transition_path(args),
        expected_run_session_id=args.run_session_id,
        action_state=source['action_state'],
        action_id=source["action_id"],
        source_path=source_path,
        source_sha256=source.get("source_sha256"),
    )
    return updated.to_dict()


def _record_turn_truth_capture(
    turn_root: Path,
    turn_entry: dict[str, Any],
) -> None:
    """Attach a best-effort, read-only truth archive result to the turn report."""

    if "truth_capture" not in turn_entry:
        turn_entry["truth_capture"] = archive_supervised_turn_truth_capture(
            turn_root=turn_root,
            turn_entry=turn_entry,
            turn_id=turn_entry.get("turn_id"),
            turn_index=turn_entry.get("turn_index"),
        )
    if "post_action_truth_capture" not in turn_entry:
        turn_entry["post_action_truth_capture"] = (
            archive_post_action_truth_capture(
                turn_root=turn_root,
                turn_entry=turn_entry,
                pre_capture=turn_entry["truth_capture"],
                turn_id=turn_entry.get("turn_id"),
                turn_index=turn_entry.get("turn_index"),
            )
        )


def _produce_formal_event_identity_shadow(
    *,
    authority: TurnAuthorityBundle,
    formal_capture: object,
    artifact_path: Path,
    owner_context_authority: EventOwnerContextAuthority | None = None,
) -> EventIdentityShadowReport:
    """Run the local-only perception producer for one verified event capture."""

    if not isinstance(formal_capture, dict):
        return EventIdentityShadowReport(
            status="abstain",
            source_frame_sha256=authority.source_frame_sha256,
            event_page_sha256=authority.source_frame_sha256,
            identity=None,
            audit=None,
            summary=None,
            abstain_reasons=("formal_capture_binding_invalid",),
            action_eligible=False,
        )
    try:
        return produce_event_identity_shadow_report(
            EventIdentityPerceptionRequest(
                authority=authority,
                formal_capture=formal_capture,
                artifact_path=artifact_path,
                artifact_root=artifact_path.parent,
                apk_root=ROOT / "data" / "apk_extracted",
                schema_path=(
                    ROOT
                    / "data"
                    / "event_knowledge"
                    / "schema"
                    / "event_knowledge.schema.json"
                ),
                dataset_path=(
                    ROOT
                    / "data"
                    / "event_knowledge"
                    / "staging"
                    / "current_deck_sample_v1.json"
                ),
                registry_path=(
                    ROOT
                    / "data"
                    / "event_knowledge"
                    / "staging"
                    / "cn_release_registry_v1.json"
                ),
                expected_schema_sha256=_EVENT_KNOWLEDGE_SCHEMA_SHA256,
                expected_dataset_sha256=_EVENT_KNOWLEDGE_DATASET_SHA256,
                expected_registry_sha256=_EVENT_KNOWLEDGE_REGISTRY_SHA256,
                owner_context_authority=owner_context_authority,
            )
        )
    except Exception:
        return EventIdentityShadowReport(
            status="abstain",
            source_frame_sha256=authority.source_frame_sha256,
            event_page_sha256=authority.source_frame_sha256,
            identity=None,
            audit=None,
            summary=None,
            abstain_reasons=("event_identity_perception_unavailable",),
            action_eligible=False,
        )


def _record_event_identity_shadow(
    turn_report: dict[str, Any],
    shadow: EventIdentityShadowReport,
) -> dict[str, object]:
    """Persist detailed diagnostics outside the dashboard's fixed v1 slot."""

    turn_report["event_identity_shadow"] = shadow.to_dict()
    if (
        shadow.status == "available"
        and shadow.identity is not None
        and shadow.identity.status == "identified"
    ):
        return {
            "schema_version": "uma.event_choice_shadow_slot.v1",
            "status": "identified",
            "abstain_reason": None,
            "action_eligible": False,
        }
    reason = next(
        (
            item
            for item in shadow.abstain_reasons
            if isinstance(item, str) and item
        ),
        "event_shadow_report_unavailable",
    )
    return _event_choice_shadow_abstain(reason)


def _event_identity_shadow_artifact_from_route(
    *,
    authority: TurnAuthorityBundle,
    event_dir: Path,
    event_report: dict[str, Any],
    shadow: EventIdentityShadowReport,
) -> tuple[EventIdentityShadowArtifact | None, str]:
    """Bind a shadow report to the first same-turn route capture only."""

    if event_report.get("is_valid") is not True:
        return None, "event_shadow_event_report_invalid"
    artifact_path = event_dir / "event_choice_step_001.png"
    return (
        EventIdentityShadowArtifact(
            run_session_id=authority.run_session_id,
            generation=authority.generation,
            global_action_turn=authority.global_action_turn,
            source_frame_sha256=authority.source_frame_sha256,
            event_page_sha256=authority.source_frame_sha256,
            artifact_path=artifact_path,
            artifact_sha256=authority.source_frame_sha256,
            shadow=shadow,
        ),
        "",
    )


def _verified_event_identity_shadow_artifact(
    *,
    turn_root: Path,
    authority: TurnAuthorityBundle | None,
    artifact: EventIdentityShadowArtifact | None,
) -> tuple[EventIdentityShadowReport | None, str]:
    """Return a report only when its artifact is bound to this authority."""

    if not isinstance(authority, TurnAuthorityBundle):
        return None, "event_shadow_authority_unavailable"
    if not isinstance(artifact, EventIdentityShadowArtifact):
        return None, "event_shadow_artifact_unavailable"
    if (
        artifact.run_session_id != authority.run_session_id
        or artifact.generation != authority.generation
        or artifact.global_action_turn != authority.global_action_turn
    ):
        return None, "event_shadow_authority_binding_mismatch"
    if (
        artifact.source_frame_sha256 != authority.source_frame_sha256
        or artifact.event_page_sha256 != authority.source_frame_sha256
        or artifact.artifact_sha256 != authority.source_frame_sha256
    ):
        return None, "event_shadow_frame_binding_mismatch"
    if not isinstance(artifact.artifact_path, Path):
        return None, "event_shadow_artifact_path_invalid"
    try:
        resolved_root = turn_root.resolve(strict=True)
        resolved_artifact = artifact.artifact_path.resolve(strict=True)
    except OSError:
        return None, "event_shadow_artifact_unreadable"
    if resolved_root != resolved_artifact and resolved_root not in resolved_artifact.parents:
        return None, "event_shadow_artifact_path_invalid"
    try:
        artifact_sha256 = hashlib.sha256(
            resolved_artifact.read_bytes()
        ).hexdigest()
    except OSError:
        return None, "event_shadow_artifact_unreadable"
    if artifact_sha256 != artifact.artifact_sha256:
        return None, "event_shadow_artifact_sha_mismatch"

    shadow = artifact.shadow
    if not isinstance(shadow, EventIdentityShadowReport):
        return None, "event_shadow_report_invalid"
    if (
        shadow.action_eligible is not False
        or shadow.source_frame_sha256 != authority.source_frame_sha256
        or shadow.event_page_sha256 != artifact.event_page_sha256
    ):
        return None, "event_shadow_report_binding_mismatch"
    if shadow.identity is not None and (
        shadow.identity.action_eligible is not False
        or shadow.identity.run_session_id != authority.run_session_id
        or shadow.identity.generation != authority.generation
        or shadow.identity.authority_bundle_sha256 != authority.bundle_sha256
        or shadow.identity.source_frame_sha256 != authority.source_frame_sha256
        or shadow.identity.event_page_sha256 != artifact.event_page_sha256
    ):
        return None, "event_shadow_identity_binding_mismatch"
    if shadow.audit is not None and (
        shadow.audit.action_eligible is not False
        or shadow.audit.run_session_id != authority.run_session_id
        or shadow.audit.generation != authority.generation
        or shadow.audit.authority_bundle_sha256 != authority.bundle_sha256
        or shadow.audit.source_frame_sha256 != authority.source_frame_sha256
        or shadow.audit.event_page_sha256 != artifact.event_page_sha256
    ):
        return None, "event_shadow_knowledge_binding_mismatch"
    if (
        shadow.identity is not None
        and shadow.audit is not None
        and shadow.identity.dataset_sha256 != shadow.audit.dataset_sha256
    ):
        return None, "event_shadow_dataset_binding_mismatch"
    return shadow, ""


def _event_choice_shadow_abstain(reason: str) -> dict[str, object]:
    return {
        "schema_version": "uma.event_choice_shadow_slot.v1",
        "status": "abstain",
        "abstain_reason": reason,
        "action_eligible": False,
    }


def _run_speed_toggle_step(
    args: argparse.Namespace,
    turn_root: Path,
    turn_report: dict[str, Any],
) -> tuple[bool, str, int]:
    speed_dir = _next_route_artifact_dir(turn_root, turn_report, "speed_toggle")
    speed_report, speed_code = _invoke_speed_toggle(_speed_toggle_argv(args, speed_dir))
    _record_route_step(turn_report, "speed_toggle", speed_code, speed_report)
    if "speed_toggle" not in turn_report:
        turn_report["speed_toggle"] = {"code": speed_code, "report": speed_report}
    if speed_code != 0 or not speed_report.get("is_valid", False):
        return False, str(speed_report.get("status", "speed_toggle_failed")), 2
    return True, str(speed_report.get("reason", "speed_toggle")), 0


def _record_route_step(
    turn_report: dict[str, Any],
    kind: str,
    code: int,
    report: dict[str, Any],
) -> None:
    steps = turn_report.setdefault("route_steps", [])
    if isinstance(steps, list):
        steps.append(
            {
                "kind": kind,
                "code": code,
                "success": code == 0 and report.get("is_valid") is True,
                "report": report,
            }
        )


def _next_route_artifact_dir(turn_root: Path, turn_report: dict[str, Any], kind: str) -> Path:
    route_steps = turn_report.get("route_steps")
    sequence = len(route_steps) + 1 if isinstance(route_steps, list) else 1
    route_steps_dir = turn_root / "route_steps"
    candidate = route_steps_dir / f"{sequence:03d}_{kind}"
    while candidate.exists():
        sequence += 1
        candidate = route_steps_dir / f"{sequence:03d}_{kind}"
    return candidate


def _safe_advance_failure_reason(report: dict[str, Any]) -> str:
    stop_reason = report.get("stop_reason")
    if isinstance(stop_reason, str) and stop_reason:
        return stop_reason
    return str(report.get("status", "safe_advance_failed"))


def _maybe_run_terminal_skill_entry(
    args: argparse.Namespace,
    turn_root: Path,
    context_path: Path | None,
    provider_source: dict[str, Any] | None,
    *,
    turn_index: int,
) -> str | None:
    """Visit the skill page for the terminal bulk-buy when skill_timing fires.

    Returns:
    - "handoff" when the skill page was reached and the safe-advance route
      produced a terminal skill handoff artifact;
    - "unverified" when the entry could not be confirmed (fail-closed);
    - None when the timing policy says this turn should train normally.
    """
    from core.auto_trainer.config import load_goal_config
    from core.auto_trainer.skill_timing import should_enter_terminal_skill_turn
    from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
    from core.adb import ADB
    from core.coordinates import DEFAULT_REFERENCE_SIZE, CoordinateScaler

    goal = load_goal_config(args.goal_config)
    if isinstance(provider_source, dict) and isinstance(
        provider_source.get("path"), str
    ):
        state_out = turn_root / "terminal_skill_entry_state_context.json"
        provider_report = provide_main_menu_state(
            provider_source["path"],
            state_out,
            turn_index=turn_index,
            consensus_png_paths=provider_source.get("consensus_paths", ()),
        )
        raw_state = provider_report.get("state_context")
        if not isinstance(raw_state, dict):
            return "unverified"
        state = raw_state
    elif context_path is not None:
        try:
            payload = json.loads(context_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "unverified"
        raw_state = payload.get("state_context") if isinstance(payload, dict) else None
        if not isinstance(raw_state, dict):
            raw_state = payload
        if not isinstance(raw_state, dict):
            return "unverified"
        state = raw_state
    else:
        return None
    if not should_enter_terminal_skill_turn(
        state, goal.skill_timing, total_turns=goal.total_turns
    ):
        state = _augment_terminal_skill_state_with_run_history(
            state, args, turn_index
        )
    if not should_enter_terminal_skill_turn(
        state, goal.skill_timing, total_turns=goal.total_turns
    ):
        return None
    adb = ADB(port=args.adb_port) if args.adb_port is not None else ADB()
    if not adb.connect():
        return "unverified"
    try:
        size = adb.get_screen_size() or (720, 1280)
        if (
            isinstance(provider_source, dict)
            and isinstance(provider_source.get("path"), str)
            and _is_race_day_main_menu(provider_source["path"])
        ):
            reference_point = (160, 1095)
        else:
            reference_point = (1200, 2000)
        x, y = CoordinateScaler(DEFAULT_REFERENCE_SIZE, size).scale_point(
            *reference_point
        )
        adb.tap(x, y, scaled=False)
    except Exception:
        return "unverified"
    finally:
        adb.disconnect()
    time.sleep(args.terminal_skill_entry_settle_seconds)
    entry_report: dict[str, Any] = {"route_steps": []}
    route_ok, route_stop, route_code = _route_between_turns(
        args,
        turn_root / "terminal_skill_entry",
        entry_report,
        transition_resync=True,
    )
    if route_ok and route_stop == "skill_acquisition_handoff":
        return "handoff"
    return "unverified"


def _augment_terminal_skill_state_with_run_history(
    state: dict[str, Any],
    args: argparse.Namespace,
    turn_index: int,
) -> dict[str, Any]:
    """Fill unreadable skill_pt from the most recent readable provider report."""

    fields = state.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        state = dict(state)
        state["fields"] = fields
    if isinstance(fields.get("skill_pt"), dict):
        current_pt = fields["skill_pt"].get("value")
    else:
        current_pt = None
    if isinstance(current_pt, int):
        return state
    last_pt = _last_known_skill_pt_from_run(args, turn_index)
    if last_pt is None:
        return state
    fields["skill_pt"] = {
        "value": last_pt,
        "abstained": False,
        "source": "run_history_fallback",
    }
    return state


def _last_known_skill_pt_from_run(
    args: argparse.Namespace, turn_index: int
) -> int | None:
    from core.auto_trainer.pacer_storage import _io, _rglob
    output_root = Path(args.output_root)
    if not _io(output_root).is_dir():
        return None
    candidates: list[tuple[int, int]] = []
    for report_path in _rglob(output_root, "main_menu_state.provider.json"):
        try:
            payload = json.loads(_io(report_path).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        context = payload.get("state_context")
        if not isinstance(context, dict):
            continue
        fields = context.get("fields")
        if not isinstance(fields, dict):
            continue
        pt_field = fields.get("skill_pt")
        pt = pt_field.get("value") if isinstance(pt_field, dict) else None
        observed_turn = context.get("turn_index")
        if (
            isinstance(pt, int)
            and isinstance(observed_turn, int)
            and observed_turn < turn_index
        ):
            candidates.append((observed_turn, pt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _safe_advance_argv(
    args: argparse.Namespace,
    screenshot_dir: Path,
    *,
    career_goal_observed_turn: int | None = None,
    transition_resync: bool = False,
    bootstrap_state: str | None = None,
    finish_after_skill_handoff: bool = False,
    expected_career_end_skill_points: int | None = None,
) -> list[str]:
    argv = [
        "--mode",
        args.mode,
        "--max-steps",
        "1" if finish_after_skill_handoff else str(args.advance_max_steps),
        "--screenshot-dir",
        str(screenshot_dir),
        "--run-session-id",
        args.run_session_id,
        "--skip-session-json",
        str(_skip_session_path(args)),
        "--transition-session-json",
        str(_route_transition_path(args)),
        "--settle-seconds",
        str(args.settle_seconds),
    ]
    if finish_after_skill_handoff:
        argv.extend(["--allow-career-end-finish-after-skill-handoff",
                     "--expected-action-id", "safe_advance.career_end_finish"])
        if expected_career_end_skill_points is not None:
            argv.extend(
                [
                    "--expected-career-end-skill-points",
                    str(expected_career_end_skill_points),
                ]
            )
    if transition_resync:
        argv.append("--transition-resync")
    if bootstrap_state is not None:
        argv.extend(["--route-bootstrap-state", bootstrap_state])
    if career_goal_observed_turn is not None:
        argv.extend(
            [
                "--career-goal-observed-turn",
                str(career_goal_observed_turn),
                "--career-goal-ledger-json",
                str(
                    _career_goal_ledger_path(args)
                ),
            ]
        )
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.advance_execute:
        argv.append("--execute")
    if args.advance_confirm_token:
        argv.extend(["--confirm-token", args.advance_confirm_token])
    freeze_args = (
        ("--post-wait-freeze-poll-seconds", args.advance_post_wait_freeze_poll_seconds),
        ("--post-wait-freeze-timeout-seconds", args.advance_post_wait_freeze_timeout_seconds),
        ("--post-wait-freeze-stable-frames", args.advance_post_wait_freeze_stable_frames),
        ("--post-wait-freeze-max-frame-diff", args.advance_post_wait_freeze_max_frame_diff),
    )
    for flag, value in freeze_args:
        if value is not None:
            argv.extend([flag, str(value)])
    if args.allow_team_formation_confirm:
        argv.append("--allow-team-formation-confirm")
    if args.allow_return_main_menu_from_training:
        argv.append("--allow-return-main-menu-from-training")
    if args.allow_startup_promo_skip:
        argv.append("--allow-startup-promo-skip")
    if args.pretty:
        argv.append("--pretty")
    return argv


def _unbound_goal_summary_argv(
    args: argparse.Namespace,
    screenshot_dir: Path,
) -> list[str]:
    argv = [
        "--mode",
        args.mode,
        "--max-steps",
        "1",
        "--screenshot-dir",
        str(screenshot_dir),
        "--expected-action-id",
        "safe_advance.goal_remaining_continue",
        "--allow-unbound-goal-summary-continue",
        "--settle-seconds",
        str(args.settle_seconds),
    ]
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.advance_execute:
        argv.append("--execute")
    if args.advance_confirm_token:
        argv.extend(["--confirm-token", args.advance_confirm_token])
    if args.pretty:
        argv.append("--pretty")
    return argv


def _race_route_argv(
    args: argparse.Namespace,
    screenshot_dir: Path,
    *,
    planned_race_ids: tuple[str, ...] = (),
    schedule_packet_sha256: str | None = None,
    suppress_result_continue: bool = False,
    wbc_only: bool = False,
    count_reserved_only: bool = False,
    fan_shortfall_only: bool = False,
    return_only: bool = False,
) -> list[str]:
    if wbc_only and (planned_race_ids or schedule_packet_sha256 is not None):
        raise ValueError("wbc_only:scheduled_binding_forbidden")
    if return_only and (
        wbc_only
        or count_reserved_only
        or fan_shortfall_only
        or planned_race_ids
        or schedule_packet_sha256 is not None
    ):
        raise ValueError("return_only:conflicting_binding")
    argv = [
        "--mode",
        args.mode,
        "--max-steps",
        "1" if return_only else str(args.race_route_max_steps),
        "--screenshot-dir",
        str(screenshot_dir),
        "--race-scene-wait-limit",
        str(args.race_scene_wait_limit),
    ]
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if wbc_only:
        argv.append("--wbc-only")
    if args.race_route_execute:
        argv.append("--execute")
    if args.race_route_confirm_token:
        argv.extend(["--confirm-token", args.race_route_confirm_token])
    if return_only:
        argv.extend(
            [
                "--allow-leave-race-list",
                "--allow-leave-unverified-g1-race-list",
            ]
        )
    if args.allow_enter_race and not return_only:
        argv.append("--allow-enter-race")
    if args.allow_enter_race_from_main_menu and not wbc_only and not return_only:
        argv.append("--allow-enter-race-from-main-menu")
    if args.allow_fan_goal_recommended_race and not wbc_only and not count_reserved_only and not return_only:
        argv.append("--allow-fan-goal-recommended-race")
    if args.allow_select_visible_g1 and not wbc_only and not count_reserved_only and not fan_shortfall_only and not return_only:
        argv.append("--allow-select-visible-g1")
    if args.allow_scan_race_list_for_g1 and not wbc_only and not count_reserved_only and not fan_shortfall_only and not return_only:
        argv.append("--allow-scan-race-list-for-g1")
    if args.allow_leave_race_list and not wbc_only and not count_reserved_only and not return_only:
        argv.append("--allow-leave-race-list")
    if (
        args.allow_leave_race_detail
        and not wbc_only
        and not count_reserved_only
        and not fan_shortfall_only
        and not return_only
    ):
        argv.append("--allow-leave-race-detail")
    if args.allow_confirm_race and not return_only:
        argv.append("--allow-confirm-race")
    if args.allow_quick_result and not return_only:
        argv.append("--allow-quick-result")
    if args.allow_skip_race_scene and not return_only:
        argv.append("--allow-skip-race-scene")
    if args.allow_result_continue and not suppress_result_continue and not return_only:
        argv.append("--allow-result-continue")
    if args.allow_locked_race_entry and not count_reserved_only and not fan_shortfall_only and not return_only:
        argv.append("--allow-locked-race-entry")
    if args.allow_grand_masters_final_race_confirm and not wbc_only and not count_reserved_only and not fan_shortfall_only and not return_only:
        argv.append("--allow-grand-masters-final-race-confirm")
    for race_id in planned_race_ids:
        argv.extend(["--planned-race-id", race_id])
    if schedule_packet_sha256 is not None:
        argv.extend(
            ["--schedule-packet-sha256", schedule_packet_sha256]
        )
    if args.pretty:
        argv.append("--pretty")
    return argv


def _event_choice_argv(
    args: argparse.Namespace,
    screenshot_dir: Path,
    *,
    current_turn_index: int | None = None,
) -> list[str]:
    argv = [
        "--mode",
        args.mode,
        "--max-steps",
        str(args.event_choice_max_steps),
        "--screenshot-dir",
        str(screenshot_dir),
        "--choice",
        args.event_choice_selection,
        "--transition-session-json",
        str(_route_transition_path(args)),
        "--transition-run-session-id",
        args.run_session_id,
    ]
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.event_choice_execute:
        argv.append("--execute")
    if args.event_choice_confirm_token:
        argv.extend(["--confirm-token", args.event_choice_confirm_token])
    if args.allow_choose_event:
        argv.append("--allow-choose-event")
    run_session_id = getattr(args, "run_session_id", None)
    generation = getattr(args, "event_formal_generation", None)
    if (
        isinstance(run_session_id, str)
        and run_session_id
        and type(generation) is int
        and generation > 0
        and type(current_turn_index) is int
        and current_turn_index >= 0
    ):
        argv.extend(
            [
                "--formal-run-session-id",
                run_session_id,
                "--formal-generation",
                str(generation),
                "--formal-global-action-turn",
                str(current_turn_index),
                "--formal-game-period",
                "unknown",
            ]
        )
    if args.pretty:
        argv.append("--pretty")
    return argv


def _speed_toggle_argv(args: argparse.Namespace, screenshot_dir: Path) -> list[str]:
    argv = [
        "--mode",
        args.mode,
        "--screenshot-path",
        str(screenshot_dir / "screen.png"),
    ]
    if args.adb_port is not None:
        argv.extend(["--adb-port", str(args.adb_port)])
    if args.speed_toggle_execute:
        argv.append("--execute")
    if args.speed_toggle_confirm_token:
        argv.extend(["--confirm-token", args.speed_toggle_confirm_token])
    if args.pretty:
        argv.append("--pretty")
    return argv


def _condition_probe_argv(args: argparse.Namespace, screenshot_dir: Path) -> list[str]:
    argv = [
        "--mode",
        "live",
        "--adb-port",
        str(ADB_PORT),
        "--execute",
        "--confirm-token",
        str(args.condition_probe_confirm_token),
        "--screenshot-dir",
        str(screenshot_dir),
        "--settle-seconds",
        str(args.settle_seconds),
    ]
    if args.pretty:
        argv.append("--pretty")
    return argv


def _run_explicit_condition_probe(
    args: argparse.Namespace,
    turn_root: Path,
    *,
    turn_id: str,
    turn_index: int,
    formal_condition_binding_key: str,
) -> tuple[Path | None, dict[str, Any], int]:
    probe_root = turn_root / "condition_probe"
    if probe_root.exists():
        probe_root = _next_route_artifact_dir(turn_root, {}, "condition_attempt") / "condition_probe"
    report, code = _invoke_condition_probe(_condition_probe_argv(args, probe_root))
    if code != 0 or not report.get("is_valid"):
        report_path = probe_root / "condition_probe.report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return None, report, 2
    report_path = probe_root / "condition_probe.report.json"
    try:
        report = attach_formal_condition_binding(
            report,
            turn_id=turn_id,
            turn_index=turn_index,
            artifact_path=probe_root / "ability_details.png",
            binding_key=formal_condition_binding_key,
        )
    except (OSError, ValueError) as exc:
        report["is_valid"] = False
        report["condition_context_error"] = str(exc)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return None, report, 2
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        load_explicit_condition_context(
            report_path,
            required_turn_root=turn_root,
            formal_live=True,
            formal_binding_key=formal_condition_binding_key,
            expected_turn_id=turn_id,
            expected_turn_index=turn_index,
        )
    except ValueError as exc:
        report["is_valid"] = False
        report["condition_context_error"] = str(exc)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return None, report, 2
    return report_path, report, 0


def _invoke_event_choice(
    argv: list[str],
    *,
    control_driver: CooperativeRunnerDriver | None = None,
) -> tuple[dict[str, Any], int]:
    if control_driver is not None:
        return run_event_choice(parse_event_choice_args(argv), control_driver=control_driver)
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = event_choice_main(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "status": "empty_event_choice_stdout"}, code
    return _load_json_from_stdout(raw, "event_choice"), code


def _invoke_condition_probe(argv: list[str]) -> tuple[dict[str, Any], int]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = condition_probe_main(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "status": "empty_condition_probe_stdout"}, code
    return _load_json_from_stdout(raw, "condition_probe"), code


def _invoke_speed_toggle(argv: list[str]) -> tuple[dict[str, Any], int]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = speed_toggle_main(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "status": "empty_speed_toggle_stdout"}, code
    return _load_json_from_stdout(raw, "speed_toggle"), code


def _invoke_race_route(argv: list[str]) -> tuple[dict[str, Any], int]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = race_route_main(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "status": "empty_race_route_stdout"}, code
    return _load_json_from_stdout(raw, "race_route"), code


def _invoke_safe_advance(
    argv: list[str],
    *,
    control_driver: CooperativeRunnerDriver | None = None,
) -> tuple[dict[str, Any], int]:
    if control_driver is not None:
        return run_safe_advance(parse_safe_advance_args(argv), control_driver=control_driver)
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = safe_advance_main(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "status": "empty_safe_advance_stdout"}, code
    return _load_json_from_stdout(raw, "safe_advance"), code


def _invoke_one_turn(
    argv: list[str],
    *,
    formal_condition_binding_key: str | None = None,
    control_driver: CooperativeRunnerDriver | None = None,
    training_parser: Any | None = None,
) -> tuple[dict[str, Any], int]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        kwargs: dict[str, Any] = {}
        if formal_condition_binding_key is not None:
            kwargs["formal_condition_binding_key"] = formal_condition_binding_key
        if control_driver is not None:
            kwargs["control_driver"] = control_driver
        if training_parser is not None:
            kwargs["training_parser"] = training_parser
        code = one_turn_main(argv, **kwargs) if kwargs else one_turn_main(argv)
    raw = stdout.getvalue().strip()
    if not raw:
        return {"is_valid": False, "status": "empty_one_turn_stdout"}, code
    return _load_json_from_stdout(raw, "one_turn"), code


def _load_json_from_stdout(raw: str, label: str) -> dict[str, Any]:
    start = raw.find("{")
    if start < 0:
        raise ValueError(f"{label}_stdout_missing_json_object")
    data = json.loads(raw[start:])
    if not isinstance(data, dict):
        raise ValueError(f"{label}_stdout_root_not_object")
    return data


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    output_encoding = getattr(sys.stdout, "encoding", None)
    if output_encoding:
        try:
            payload.encode(output_encoding)
        except (LookupError, UnicodeEncodeError):
            # PowerShell commonly exposes a GBK stdout on Chinese Windows.
            # Escape only when required so a valid report can never fail
            # after live work merely because OCR emitted an uncommon glyph.
            payload = json.dumps(data, ensure_ascii=True, indent=indent)
    print(payload)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
