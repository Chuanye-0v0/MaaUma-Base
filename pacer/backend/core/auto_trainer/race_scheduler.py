"""Deterministic, opportunity-constrained career race scheduling."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from core.auto_trainer.career_goal_ledger import (
    CareerGoal,
    CareerGoalLedger,
    CareerGoalLedgerError,
    CareerGoalLedgerStore,
)
from core.auto_trainer.race_calendar import (
    RaceCalendar,
    RaceCalendarEntry,
)
from core.auto_trainer.race_plan import RacePlan


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class RaceSchedulerError(ValueError):
    """Raised when scheduler inputs cannot produce a safe packet."""


@dataclass(frozen=True)
class RaceEligibilityEvidence:
    race_id: str
    aptitude_ok: bool | None
    condition_ok: bool | None
    consecutive_race_ok: bool | None
    evidence_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.race_id, "eligibility_race_id_invalid")
        for value in (
            self.aptitude_ok,
            self.condition_ok,
            self.consecutive_race_ok,
        ):
            if value is not None and type(value) is not bool:
                raise RaceSchedulerError("eligibility_flag_invalid")
        _require_sha(self.evidence_sha256, "eligibility_sha_invalid")

    @property
    def formally_eligible(self) -> bool:
        return (
            self.aptitude_ok is True
            and self.condition_ok is True
            and self.consecutive_race_ok is True
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "race_id": self.race_id,
            "aptitude_ok": self.aptitude_ok,
            "condition_ok": self.condition_ok,
            "consecutive_race_ok": self.consecutive_race_ok,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True)
class CountRaceEligibilityEvidence:
    """Source-bound four-part eligibility proof supplied by the UI producer."""

    run_session_id: str
    calendar_turn: int
    race_id: str
    source_path: str
    source_frame_sha256: str
    aptitude_ok: bool | None
    condition_ok: bool | None
    consecutive_race_ok: bool | None
    entry_conditions_met: bool | None
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_text(self.run_session_id, "eligibility_run_session_id_invalid")
        if type(self.calendar_turn) is not int or self.calendar_turn <= 0:
            raise RaceSchedulerError("eligibility_calendar_turn_invalid")
        _require_text(self.race_id, "eligibility_race_id_invalid")
        _require_text(self.source_path, "eligibility_source_path_invalid")
        _require_sha(self.source_frame_sha256, "eligibility_source_sha_invalid")
        for value in (
            self.aptitude_ok,
            self.condition_ok,
            self.consecutive_race_ok,
            self.entry_conditions_met,
        ):
            if value is not None and type(value) is not bool:
                raise RaceSchedulerError("eligibility_flag_invalid")
        if not isinstance(self.evidence, Mapping) or not self.evidence:
            raise RaceSchedulerError("eligibility_evidence_invalid")
        frozen = _freeze_json(self.evidence)
        if not isinstance(frozen, Mapping) or not frozen:
            raise RaceSchedulerError("eligibility_evidence_invalid")
        object.__setattr__(self, "evidence", frozen)

    @property
    def formally_eligible(self) -> bool:
        return all(
            value is True
            for value in (
                self.aptitude_ok,
                self.condition_ok,
                self.consecutive_race_ok,
                self.entry_conditions_met,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_session_id": self.run_session_id,
            "calendar_turn": self.calendar_turn,
            "race_id": self.race_id,
            "source_path": self.source_path,
            "source_frame_sha256": self.source_frame_sha256,
            "aptitude_ok": self.aptitude_ok,
            "condition_ok": self.condition_ok,
            "consecutive_race_ok": self.consecutive_race_ok,
            "entry_conditions_met": self.entry_conditions_met,
            "evidence": _thaw_json(self.evidence),
        }


@dataclass(frozen=True)
class RaceSchedulingState:
    current_turn: int
    observed_runner_turn: int
    fan_count: int | None
    eligibility_by_race_id: Mapping[str, RaceEligibilityEvidence]

    def __post_init__(self) -> None:
        if type(self.current_turn) is not int or self.current_turn <= 0:
            raise RaceSchedulerError("current_turn_invalid")
        if type(self.observed_runner_turn) is not int or self.observed_runner_turn <= 0:
            raise RaceSchedulerError("observed_runner_turn_invalid")
        if self.fan_count is not None and (
            type(self.fan_count) is not int or self.fan_count < 0
        ):
            raise RaceSchedulerError("fan_count_invalid")
        if not isinstance(self.eligibility_by_race_id, Mapping):
            raise RaceSchedulerError("eligibility_mapping_invalid")
        normalized: dict[str, RaceEligibilityEvidence] = {}
        for race_id, evidence in self.eligibility_by_race_id.items():
            if (
                type(race_id) is not str
                or not isinstance(evidence, RaceEligibilityEvidence)
                or evidence.race_id != race_id
                or race_id in normalized
            ):
                raise RaceSchedulerError("eligibility_mapping_invalid")
            normalized[race_id] = evidence
        object.__setattr__(
            self,
            "eligibility_by_race_id",
            MappingProxyType(normalized),
        )


@dataclass(frozen=True)
class CandidateAction:
    action_id: str
    action_kind: Literal["enter_race", "defer_to_local_policy", "stop"]
    race_id: str | None
    priority: Literal["hard", "must", "prefer", "none"]
    source_goal_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence_sha256: str


@dataclass(frozen=True)
class RaceScheduleDecision:
    candidates: tuple[CandidateAction, ...]
    required_remaining: Mapping[str, int]
    verified_opportunity_race_ids: Mapping[str, tuple[str, ...]]
    status: str
    packet_sha256: str


@dataclass(frozen=True)
class CountRaceReservationPlan:
    reservation: Mapping[str, object]
    decision: RaceScheduleDecision

    def __post_init__(self) -> None:
        if not isinstance(self.reservation, Mapping):
            raise RaceSchedulerError("count_reservation_invalid")
        object.__setattr__(self, "reservation", _freeze_json(self.reservation))

    def reservation_dict(self) -> dict[str, object]:
        value = _thaw_json(self.reservation)
        if type(value) is not dict:
            raise RaceSchedulerError("count_reservation_invalid")
        return value

    def to_dict(self) -> dict[str, object]:
        reservation = self.reservation_dict()
        return {
            "schema_version": "race_schedule_decision.v1",
            "run_session_id": reservation["run_session_id"],
            "turn_index": reservation["observed_runner_turn"],
            "source_frame_sha256": reservation["source_frame_sha256"],
            "ledger_revision": reservation["ledger_revision"],
            "ledger_view_sha256": reservation["ledger_view_sha256"],
            "decision": race_schedule_decision_to_dict(self.decision),
            "count_reservation": reservation,
        }


class OpportunityConstrainedRaceScheduler:
    def get_schedule_candidates(
        self,
        *,
        career_state: RaceSchedulingState,
        ledger: CareerGoalLedger,
        calendar: RaceCalendar,
        race_plan: RacePlan,
        scenario_mandatory: tuple[str, ...],
    ) -> RaceScheduleDecision:
        if not isinstance(career_state, RaceSchedulingState):
            raise RaceSchedulerError("career_state_required")
        if not isinstance(calendar, RaceCalendar):
            raise RaceSchedulerError("race_calendar_required")
        if not isinstance(race_plan, RacePlan):
            raise RaceSchedulerError("race_plan_required")
        if (
            type(scenario_mandatory) is not tuple
            or any(type(item) is not str or not item for item in scenario_mandatory)
            or len(set(scenario_mandatory)) != len(scenario_mandatory)
        ):
            raise RaceSchedulerError("scenario_mandatory_invalid")
        CareerGoalLedgerStore().verify_hash_chain(ledger)
        if career_state.observed_runner_turn < ledger.observed_turn:
            raise RaceSchedulerError("runner_turn_before_ledger")

        lower_bound_goals = tuple(
            goal for goal in ledger.active_goals()
            if goal.goal_kind == "graded_race_rank_count"
            and goal.progress_basis == "verified_lower_bound"
        )
        if lower_bound_goals:
            if len(lower_bound_goals) != 1 or len(ledger.active_goals()) != 1:
                raise RaceSchedulerError("count_goal_active_conflict")
            goal = lower_bound_goals[0]
            if goal.deadline_turn is None:
                raise RaceSchedulerError("count_goal_calendar_unbound")
            if career_state.current_turn > goal.deadline_turn:
                raise RaceSchedulerError("count_goal_deadline_passed")
            if any(entry.grade == goal.grade and entry.career_time is None
                   for entry in calendar.entries):
                raise RaceSchedulerError("count_goal_calendar_unverified")
            requirement_verified = goal.completed_count >= goal.required_count
            current_grade_entries = tuple(
                entry for entry in calendar.query_turn(
                    career_state.current_turn, formal_only=False,
                )
                if entry.grade == goal.grade
            )
            consecutive_race_blocked = bool(current_grade_entries) and all(
                (evidence := career_state.eligibility_by_race_id.get(entry.race_id)) is not None
                and evidence.consecutive_race_ok is False
                for entry in current_grade_entries
            )
            if (
                not requirement_verified
                and current_grade_entries
                and not consecutive_race_blocked
            ):
                raise RaceSchedulerError("count_goal_eligibility_required")
            if scenario_mandatory:
                raise RaceSchedulerError("count_goal_other_race_obligation_pending")
            for race_id in {item.race_id for item in race_plan.directives}:
                resolution = race_plan.resolve(race_id, hard_goal=False, scenario_mandatory=False)
                if resolution.executable and resolution.directive == "must_run":
                    entry = calendar.get(race_id)
                    if (entry is None or entry.career_time is None
                        or entry.career_time.global_turn == career_state.current_turn):
                        raise RaceSchedulerError("count_goal_other_race_obligation_pending")
            # These empty maps mean not computed, never exact zero progress or
            # a claim that the configured calendar exhausts the live schedule.
            input_packet = _input_packet(
                career_state=career_state, ledger=ledger, calendar=calendar,
                race_plan=race_plan, scenario_mandatory=scenario_mandatory,
                required_remaining={}, opportunities={},
            )
            input_sha = _canonical_sha(input_packet)
            candidate = _candidate(
                input_sha=input_sha, action_kind="defer_to_local_policy",
                race_id=None, priority="none", source_goal_ids=(goal.goal_instance_id,),
                reasons=("count_goal_requirement_verified_waiting_system"
                         if requirement_verified else "count_grade_race_consecutive_blocked"
                         if consecutive_race_blocked else "no_current_count_grade_race",),
            )
            return _decision(
                candidates=(candidate,), required_remaining={}, opportunities={},
                status="defer_to_local_policy", input_sha=input_sha,
            )

        active_goals = tuple(
            goal
            for goal in ledger.active_goals()
            # Named goals follow the natural mandatory career route. Their
            # relative countdown is not a verified calendar scheduling window.
            if goal.goal_kind == "graded_race_rank_count"
            and ledger.required_remaining(goal.goal_instance_id) > 0
        )
        required_remaining = {
            goal.goal_instance_id: ledger.required_remaining(
                goal.goal_instance_id
            )
            for goal in active_goals
        }
        opportunities: dict[str, tuple[str, ...]] = {}
        hard_goal_ids_by_race: dict[str, set[str]] = {}
        goal_ids_by_current_race: dict[str, set[str]] = {}
        unfulfillable_goal_ids: list[str] = []
        can_verify_opportunities = (
            career_state.fan_count is not None
            and bool(career_state.eligibility_by_race_id)
        )
        for goal in active_goals:
            window = (
                ()
                if career_state.current_turn > goal.deadline_turn
                else calendar.query_window(
                    career_state.current_turn,
                    goal.deadline_turn,
                    formal_only=True,
                )
            )
            eligible = tuple(
                sorted(
                    (
                        entry
                        for entry in window
                        if _goal_matches_race(goal, entry)
                        and _entry_is_formally_eligible(entry, career_state)
                    ),
                    key=lambda item: (
                        item.career_time.global_turn
                        if item.career_time is not None
                        else 10**9,
                        item.race_id,
                    ),
                )
            )
            unique_ids = tuple(
                dict.fromkeys(entry.race_id for entry in eligible)
            )
            opportunities[goal.goal_instance_id] = unique_ids
            required = required_remaining[goal.goal_instance_id]
            # A career turn can consume at most one race action. Multiple
            # eligible races on the same turn are alternatives, not multiple
            # opportunities toward a count-based goal.
            opportunity_turn_count = len(
                {
                    entry.career_time.global_turn
                    for entry in eligible
                    if entry.career_time is not None
                }
            )
            if opportunity_turn_count < required and can_verify_opportunities:
                unfulfillable_goal_ids.append(goal.goal_instance_id)
            force_now = opportunity_turn_count <= (
                required + race_plan.reserve_opportunities
            )
            for entry in eligible:
                if (
                    entry.career_time is not None
                    and entry.career_time.global_turn == career_state.current_turn
                ):
                    goal_ids_by_current_race.setdefault(
                        entry.race_id,
                        set(),
                    ).add(goal.goal_instance_id)
                    if force_now:
                        hard_goal_ids_by_race.setdefault(
                            entry.race_id,
                            set(),
                        ).add(goal.goal_instance_id)

        input_packet = _input_packet(
            career_state=career_state,
            ledger=ledger,
            calendar=calendar,
            race_plan=race_plan,
            scenario_mandatory=scenario_mandatory,
            required_remaining=required_remaining,
            opportunities=opportunities,
        )
        input_sha = _canonical_sha(input_packet)
        if unfulfillable_goal_ids and not race_plan.allow_goal_failure:
            candidate = _candidate(
                input_sha=input_sha,
                action_kind="stop",
                race_id=None,
                priority="hard",
                source_goal_ids=tuple(sorted(unfulfillable_goal_ids)),
                reasons=("career_goal_schedule_unfulfillable",),
            )
            return _decision(
                candidates=(candidate,),
                required_remaining=required_remaining,
                opportunities=opportunities,
                status="career_goal_schedule_unfulfillable",
                input_sha=input_sha,
            )

        current_entries = tuple(
            sorted(
                (
                    entry
                    for entry in calendar.query_turn(
                        career_state.current_turn,
                        formal_only=True,
                    )
                    if _entry_is_formally_eligible(entry, career_state)
                ),
                key=lambda item: item.race_id,
            )
        )
        scenario_set = set(scenario_mandatory)
        ignored_goal_ids = (
            set(unfulfillable_goal_ids)
            if race_plan.allow_goal_failure
            else set()
        )
        race_candidates: list[CandidateAction] = []
        for entry in current_entries:
            hard_goal_ids = (
                hard_goal_ids_by_race.get(entry.race_id, set())
                - ignored_goal_ids
            )
            all_goal_ids = (
                goal_ids_by_current_race.get(entry.race_id, set())
                - ignored_goal_ids
            )
            scenario_required = entry.race_id in scenario_set
            user_resolution = race_plan.resolve(
                entry.race_id,
                hard_goal=False,
                scenario_mandatory=False,
            )
            reasons: list[str] = []
            if hard_goal_ids:
                reasons.append("career_goal_force_window")
            if scenario_required:
                reasons.append("scenario_mandatory")
            if user_resolution.directive == "must_run":
                reasons.append("user_must_run")
            elif user_resolution.directive == "prefer_run":
                reasons.append("user_prefer_run")
            elif user_resolution.directive == "avoid_run":
                if hard_goal_ids:
                    reasons.append(
                        "user_avoid_overridden_by_career_hard_goal"
                    )
                elif scenario_required:
                    reasons.append(
                        "user_avoid_overridden_by_scenario_mandatory"
                    )
                else:
                    continue

            if hard_goal_ids:
                priority: Literal["hard", "must", "prefer", "none"] = "hard"
            elif scenario_required or user_resolution.directive == "must_run":
                priority = "must"
            elif user_resolution.directive == "prefer_run":
                priority = "prefer"
            elif all_goal_ids:
                priority = "none"
                reasons.append("career_goal_optional_opportunity")
            else:
                continue
            race_candidates.append(
                _candidate(
                    input_sha=input_sha,
                    action_kind="enter_race",
                    race_id=entry.race_id,
                    priority=priority,
                    source_goal_ids=tuple(sorted(all_goal_ids)),
                    reasons=tuple(reasons),
                )
            )

        hard = tuple(
            candidate
            for candidate in race_candidates
            if candidate.priority == "hard"
        )
        must = tuple(
            candidate
            for candidate in race_candidates
            if candidate.priority == "must"
        )
        if hard:
            return _decision(
                candidates=hard,
                required_remaining=required_remaining,
                opportunities=opportunities,
                status="hard_race_candidate",
                input_sha=input_sha,
            )
        if must:
            return _decision(
                candidates=must,
                required_remaining=required_remaining,
                opportunities=opportunities,
                status="must_race_candidate",
                input_sha=input_sha,
            )

        defer_reasons = (
            ("goal_failure_allowed_by_plan",)
            if unfulfillable_goal_ids
            else ("local_policy_retains_primary_action_choice",)
        )
        defer = _candidate(
            input_sha=input_sha,
            action_kind="defer_to_local_policy",
            race_id=None,
            priority="none",
            source_goal_ids=tuple(sorted(unfulfillable_goal_ids)),
            reasons=defer_reasons,
        )
        if unfulfillable_goal_ids:
            return _decision(
                candidates=(defer,),
                required_remaining=required_remaining,
                opportunities=opportunities,
                status="goal_failure_allowed",
                input_sha=input_sha,
            )
        if race_candidates:
            return _decision(
                candidates=(
                    defer,
                    *tuple(
                        sorted(
                            race_candidates,
                            key=lambda item: (
                                {"prefer": 0, "none": 1}[item.priority],
                                item.race_id or "",
                            ),
                        )
                    ),
                ),
                required_remaining=required_remaining,
                opportunities=opportunities,
                status="race_candidates_available",
                input_sha=input_sha,
            )
        status = "no_active_goal" if not active_goals else "defer_to_local_policy"
        return _decision(
            candidates=(defer,),
            required_remaining=required_remaining,
            opportunities=opportunities,
            status=status,
            input_sha=input_sha,
        )


def build_count_race_reservation(
    *,
    ledger: CareerGoalLedger,
    calendar: RaceCalendar,
    main_anchor: Mapping[str, object],
    main_source_path: str | Path,
    observed_runner_turn: int,
    eligibility: CountRaceEligibilityEvidence,
) -> CountRaceReservationPlan:
    """Build one positive reservation; callers must re-observe after its race."""

    if not isinstance(ledger, CareerGoalLedger):
        raise RaceSchedulerError("career_goal_ledger_required")
    if not isinstance(calendar, RaceCalendar):
        raise RaceSchedulerError("race_calendar_required")
    if not isinstance(main_anchor, Mapping):
        raise RaceSchedulerError("count_reservation_main_anchor_invalid")
    if type(observed_runner_turn) is not int or observed_runner_turn <= 0:
        raise RaceSchedulerError("observed_runner_turn_invalid")
    if not isinstance(eligibility, CountRaceEligibilityEvidence):
        raise RaceSchedulerError("count_reservation_eligibility_required")

    store = CareerGoalLedgerStore()
    store.verify_hash_chain(ledger)
    if observed_runner_turn < ledger.observed_turn:
        raise RaceSchedulerError("runner_turn_before_ledger")
    source_sha = main_anchor.get("source_frame_sha256")
    _require_sha(source_sha, "count_reservation_main_source_invalid")
    source_path = _verified_source_file(
        main_source_path,
        source_sha,
        "count_reservation_main_source_invalid",
    )
    try:
        binding = store.bind_count_goal_deadline(
            ledger,
            anchor=main_anchor,
            observed_turn=observed_runner_turn,
            source_frame_sha256=source_sha,
            expected_run_session_id=ledger.run_session_id,
        )
    except CareerGoalLedgerError as exc:
        raise RaceSchedulerError("count_reservation_main_anchor_invalid") from exc
    if binding.status != "no_change" or binding.ledger.to_dict() != ledger.to_dict():
        raise RaceSchedulerError("count_reservation_main_anchor_invalid")

    active = ledger.active_goals()
    if len(active) != 1:
        raise RaceSchedulerError("count_goal_active_conflict")
    goal = active[0]
    if (
        goal.goal_kind != "graded_race_rank_count"
        or goal.progress_basis != "verified_lower_bound"
        or goal.deadline_turn is None
    ):
        raise RaceSchedulerError("count_goal_active_conflict")
    if goal.required_count - goal.completed_count <= 0:
        raise RaceSchedulerError("count_goal_fresh_observation_required")

    time = main_anchor.get("career_time")
    if not isinstance(time, Mapping) or type(time.get("global_turn")) is not int:
        raise RaceSchedulerError("count_reservation_main_anchor_invalid")
    calendar_turn = time["global_turn"]
    if calendar_turn > goal.deadline_turn:
        raise RaceSchedulerError("count_goal_deadline_passed")
    if any(
        entry.grade == goal.grade and entry.career_time is None
        for entry in calendar.entries
    ):
        raise RaceSchedulerError("count_reservation_calendar_unverified")
    current = tuple(
        entry
        for entry in calendar.query_turn(calendar_turn, formal_only=False)
        if entry.grade == goal.grade and entry.race_id == eligibility.race_id
    )
    if len(current) != 1:
        raise RaceSchedulerError("count_reservation_candidate_not_unique")
    entry = current[0]
    if (
        entry.schedule_status != "verified"
        or entry.career_time is None
        or entry.career_time.global_turn != calendar_turn
    ):
        raise RaceSchedulerError("count_reservation_calendar_unverified")

    if (
        eligibility.run_session_id != ledger.run_session_id
        or eligibility.calendar_turn != calendar_turn
        or eligibility.race_id != entry.race_id
    ):
        raise RaceSchedulerError("count_reservation_eligibility_mismatch")
    if not eligibility.formally_eligible:
        raise RaceSchedulerError("count_reservation_eligibility_unverified")
    _verified_source_file(
        eligibility.source_path,
        eligibility.source_frame_sha256,
        "count_reservation_eligibility_source_invalid",
    )

    reservation = {
        "schema_version": "count_race_reservation.v1",
        "run_session_id": ledger.run_session_id,
        "observed_runner_turn": observed_runner_turn,
        "calendar_turn": calendar_turn,
        "goal_instance_id": goal.goal_instance_id,
        "race_id": entry.race_id,
        "grade": entry.grade,
        "deadline_turn": goal.deadline_turn,
        "ledger_revision": ledger.revision,
        "ledger_view_sha256": ledger.current_view_sha256,
        "source_path": str(source_path),
        "source_frame_sha256": source_sha,
        "eligibility": eligibility.to_dict(),
    }
    input_sha = _canonical_sha(reservation)
    candidate = _candidate(
        input_sha=input_sha,
        action_kind="enter_race",
        race_id=entry.race_id,
        priority="none",
        source_goal_ids=(goal.goal_instance_id,),
        reasons=("count_goal_single_race_reservation",),
    )
    decision = _decision(
        candidates=(candidate,),
        required_remaining={},
        opportunities={},
        status="count_race_reserved",
        input_sha=input_sha,
    )
    return CountRaceReservationPlan(reservation=reservation, decision=decision)


def race_schedule_decision_to_dict(
    decision: RaceScheduleDecision,
) -> dict[str, object]:
    if not isinstance(decision, RaceScheduleDecision):
        raise RaceSchedulerError("race_schedule_decision_required")
    return {
        "status": decision.status,
        "packet_sha256": decision.packet_sha256,
        "candidates": [
            {
                "action_id": candidate.action_id,
                "action_kind": candidate.action_kind,
                "race_id": candidate.race_id,
                "priority": candidate.priority,
                "source_goal_ids": list(candidate.source_goal_ids),
                "reasons": list(candidate.reasons),
                "evidence_sha256": candidate.evidence_sha256,
            }
            for candidate in decision.candidates
        ],
        "required_remaining": dict(decision.required_remaining),
        "verified_opportunity_race_ids": {
            key: list(value)
            for key, value in decision.verified_opportunity_race_ids.items()
        },
    }


def _entry_is_formally_eligible(
    entry: RaceCalendarEntry,
    state: RaceSchedulingState,
) -> bool:
    if entry.schedule_status != "verified" or entry.career_time is None:
        return False
    evidence = state.eligibility_by_race_id.get(entry.race_id)
    if evidence is None or not evidence.formally_eligible:
        return False
    required_fans = entry.entry_requirements.get("fan_count")
    return (
        type(required_fans) is int
        and state.fan_count is not None
        and state.fan_count >= required_fans
    )


def _goal_matches_race(
    goal: CareerGoal,
    entry: RaceCalendarEntry,
) -> bool:
    return (
        goal.goal_kind == "graded_race_rank_count"
        and goal.grade == entry.grade
    )


def _candidate(
    *,
    input_sha: str,
    action_kind: Literal["enter_race", "defer_to_local_policy", "stop"],
    race_id: str | None,
    priority: Literal["hard", "must", "prefer", "none"],
    source_goal_ids: tuple[str, ...],
    reasons: tuple[str, ...],
) -> CandidateAction:
    action_id = (
        f"enter_race:{race_id}"
        if action_kind == "enter_race"
        else (
            "defer_to_local_policy"
            if action_kind == "defer_to_local_policy"
            else "stop:career_goal_schedule_unfulfillable"
        )
    )
    evidence_sha = _canonical_sha(
        {
            "input_sha256": input_sha,
            "action_id": action_id,
            "action_kind": action_kind,
            "race_id": race_id,
            "priority": priority,
            "source_goal_ids": list(source_goal_ids),
            "reasons": list(reasons),
        }
    )
    return CandidateAction(
        action_id=action_id,
        action_kind=action_kind,
        race_id=race_id,
        priority=priority,
        source_goal_ids=source_goal_ids,
        reasons=reasons,
        evidence_sha256=evidence_sha,
    )


def _decision(
    *,
    candidates: tuple[CandidateAction, ...],
    required_remaining: Mapping[str, int],
    opportunities: Mapping[str, tuple[str, ...]],
    status: str,
    input_sha: str,
) -> RaceScheduleDecision:
    packet_sha = _canonical_sha(
        {
            "input_sha256": input_sha,
            "status": status,
            "candidates": [
                {
                    "action_id": item.action_id,
                    "action_kind": item.action_kind,
                    "race_id": item.race_id,
                    "priority": item.priority,
                    "source_goal_ids": list(item.source_goal_ids),
                    "reasons": list(item.reasons),
                    "evidence_sha256": item.evidence_sha256,
                }
                for item in candidates
            ],
            "required_remaining": dict(sorted(required_remaining.items())),
            "verified_opportunity_race_ids": {
                key: list(value)
                for key, value in sorted(opportunities.items())
            },
        }
    )
    return RaceScheduleDecision(
        candidates=candidates,
        required_remaining=MappingProxyType(
            dict(sorted(required_remaining.items()))
        ),
        verified_opportunity_race_ids=MappingProxyType(
            {
                key: tuple(value)
                for key, value in sorted(opportunities.items())
            }
        ),
        status=status,
        packet_sha256=packet_sha,
    )


def _input_packet(
    *,
    career_state: RaceSchedulingState,
    ledger: CareerGoalLedger,
    calendar: RaceCalendar,
    race_plan: RacePlan,
    scenario_mandatory: tuple[str, ...],
    required_remaining: Mapping[str, int],
    opportunities: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    return {
        "schema_version": "race_schedule_input.v1",
        "current_turn": career_state.current_turn,
        "observed_runner_turn": career_state.observed_runner_turn,
        "fan_count": career_state.fan_count,
        "eligibility": {
            key: value.to_dict()
            for key, value in sorted(
                career_state.eligibility_by_race_id.items()
            )
        },
        "ledger": {
            "run_session_id": ledger.run_session_id,
            "revision": ledger.revision,
            "observed_turn": ledger.observed_turn,
            "current_view_sha256": ledger.current_view_sha256,
        },
        "calendar": _calendar_identity(calendar),
        "race_plan": _plan_identity(race_plan),
        "scenario_mandatory": sorted(scenario_mandatory),
        "required_remaining": dict(sorted(required_remaining.items())),
        "verified_opportunity_race_ids": {
            key: list(value)
            for key, value in sorted(opportunities.items())
        },
    }


def _calendar_identity(calendar: RaceCalendar) -> dict[str, object]:
    return {
        "calendar_id": calendar.calendar_id,
        "calendar_sha256": calendar.calendar_sha256,
        "apk_database_sha256": calendar.apk_database_sha256,
        "entries": [
            {
                "race_id": entry.race_id,
                "turn": (
                    entry.career_time.global_turn
                    if entry.career_time is not None
                    else None
                ),
                "grade": entry.grade,
                "schedule_status": entry.schedule_status,
                "fan_count": entry.entry_requirements.get("fan_count"),
                "apk_metadata_sha256": entry.apk_metadata_sha256,
            }
            for entry in calendar.entries
        ],
    }


def _plan_identity(plan: RacePlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "reserve_opportunities": plan.reserve_opportunities,
        "allow_goal_failure": plan.allow_goal_failure,
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
            for item in plan.directives
        ],
    }


def _verified_source_file(
    value: str | Path,
    expected_sha256: str,
    reason: str,
) -> Path:
    _require_sha(expected_sha256, reason)
    try:
        path = Path(value)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError) as exc:
        raise RaceSchedulerError(reason) from exc
    if digest != expected_sha256:
        raise RaceSchedulerError(reason)
    return path


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise RaceSchedulerError("eligibility_evidence_invalid")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) in {list, tuple}:
        return tuple(_freeze_json(item) for item in value)
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise RaceSchedulerError("eligibility_evidence_invalid")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_text(value: object, reason: str) -> None:
    if type(value) is not str or not value:
        raise RaceSchedulerError(reason)


def _require_sha(value: object, reason: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise RaceSchedulerError(reason)
