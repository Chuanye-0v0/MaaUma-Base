from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.condition_context import inspect_formally_bound_condition
from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.pipeline_timing import timed_stage
from core.auto_trainer.grand_masters import GrandMastersUGParentStrategy
from core.auto_trainer.larc_opening import LarcOpeningStrategy
from core.auto_trainer.perception_reliability import PerceptionDecisionView
from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle
from core.auto_trainer.rejection_policy import is_hard_rejected
from core.auto_trainer.state import (
    STAT_KEYS,
    TRAINING_TYPES,
    TrainingCandidate,
    TrainingTurnState,
)


DEFAULT_AI_REVIEW_MIN_CONFIDENCE = 0.6
NUMERIC_BLIND_FALLBACK_MODE = "numeric_blind_fallback"
NUMERIC_BLIND_SAFE_FAILURE_RATE = 5.0
NUMERIC_BLIND_BASIC_STATS = {"speed", "stamina", "power"}
NUMERIC_BLIND_SURVIVAL_FLOOR_WEIGHT = 25.0
NUMERIC_BLIND_TARGET_GAP_WEIGHT = 25.0
NUMERIC_BLIND_SAFE_SKILL_PT_CAP = 10.0
NUMERIC_BLIND_SAFE_VITALITY_CAP = 10.0
NUMERIC_BLIND_TERMINAL_STATE_ERRORS = frozenset({
    "decode_failed", "unsupported_layout", "connection_conflict", "account_mismatch",
    "run_session_mismatch", "capture_context_turn_mismatch", "resource_prompt",
    "payment_pending", "purchase_outcome_unknown",
    "ready_main_menu_consensus_source_sha256_not_independent",
    "ready_main_menu_consensus_screen_or_layout_mismatch",
    "ready_main_menu_consensus_remaining_turns_conflict",
})


def numeric_blind_terminal_state_errors(errors):
    return [error for error in errors if error.partition(":")[0] in NUMERIC_BLIND_TERMINAL_STATE_ERRORS]


class DecisionEngine:
    """Scenario-aware dry-run decision engine."""

    def __init__(self, goal: UGParentGoal):
        self.goal = goal
        if goal.scenario == "grand_masters":
            self.strategy = GrandMastersUGParentStrategy(goal)
        elif goal.scenario == "larc":
            self.strategy = LarcOpeningStrategy(goal)
        else:
            raise ValueError(f"unsupported_scenario:{goal.scenario}")

    @timed_stage('decision_scoring')
    def decide(
        self,
        state: TrainingTurnState,
        ai_review_json: str | None = None,
        expected_packet_sha256: str | None = None,
        min_ai_review_confidence: float = DEFAULT_AI_REVIEW_MIN_CONFIDENCE,
        *,
        perception_decision_view: PerceptionDecisionView | None = None,
        perception_authority: TurnAuthorityBundle | None = None,
        perception_turn_id: str | None = None,
        perception_source_frame_sha256_by_tab: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        decision_state = self._normal_mode_decision_state(
            state,
            perception_decision_view=perception_decision_view,
            perception_authority=perception_authority,
            perception_turn_id=perception_turn_id,
            perception_source_frame_sha256_by_tab=(
                perception_source_frame_sha256_by_tab
            ),
        )
        result = self.strategy.evaluate(decision_state).to_dict()
        result["perception_option_fields"] = "decision_safe_only"
        result["ai_review"] = None
        if ai_review_json:
            review = parse_ai_review(
                ai_review_json,
                self._eligible_candidate_ids(result),
                expected_packet_sha256,
            )
            return self.apply_validated_ai_review(
                result,
                review,
                min_ai_review_confidence=min_ai_review_confidence,
            )
        return result

    def _normal_mode_decision_state(
        self,
        state: TrainingTurnState,
        *,
        perception_decision_view: PerceptionDecisionView | None,
        perception_authority: TurnAuthorityBundle | None,
        perception_turn_id: str | None,
        perception_source_frame_sha256_by_tab: Mapping[str, str] | None,
    ) -> TrainingTurnState:
        """Remove shadow perception fields before deterministic scoring.

        Training gains and failure rate keep their existing reliability checks.
        Support counts may re-enter only through the same turn/SHA-bound
        decision view used by numeric-blind mode.  Bond and fragment semantics
        currently have no production authorization, so their score inputs stay
        unavailable even when raw parser output contains a candidate value.
        """

        sanitized: list[TrainingCandidate] = []
        for candidate in state.training_options:
            usage = self._numeric_blind_field_usage(
                candidate,
                perception_decision_view=perception_decision_view,
                perception_authority=perception_authority,
                perception_turn_id=perception_turn_id,
                perception_source_frame_sha256_by_tab=(
                    perception_source_frame_sha256_by_tab
                ),
            )
            prefix = f"training.{candidate.training_type}"
            effective_support_count = self._numeric_blind_safe_integer(
                usage[f"{prefix}.support.effective_count"]
            )
            hint_count = self._numeric_blind_safe_integer(
                usage[f"{prefix}.support.hint_count"]
            )

            raw_data = dict(candidate.raw_data)
            raw_data["effective_support_count"] = effective_support_count
            raw_data["hint_count"] = hint_count
            raw_data["friendship"] = None
            raw_data["support_slots"] = []
            raw_data["fragment_color"] = None
            raw_data["fragment_stat"] = None
            raw_data["fragment_count"] = None
            raw_data["fragments"] = []
            raw_data["perception_decision_field_usage"] = {
                key: value
                for key, value in usage.items()
                if key
                in {
                    f"{prefix}.support.effective_count",
                    f"{prefix}.support.hint_count",
                }
            }

            scenario_mechanics = dict(candidate.scenario_mechanics)
            grand_masters = scenario_mechanics.get("grand_masters")
            if isinstance(grand_masters, dict):
                grand_masters = dict(grand_masters)
                grand_masters["fragments"] = []
                scenario_mechanics["grand_masters"] = grand_masters

            sanitized.append(
                replace(
                    candidate,
                    hint_count=hint_count,
                    friendship=None,
                    fragment_color=None,
                    fragment_stat=None,
                    fragment_count=None,
                    fragments=[],
                    scenario_mechanics=scenario_mechanics,
                    raw_data=raw_data,
                )
            )
        return replace(state, training_options=sanitized)

    def decide_numeric_blind_fallback(
        self,
        state: TrainingTurnState,
        *,
        formal_condition_binding_key: str | bytes | None = None,
        condition_turn_root: str | Path | None = None,
        expected_condition_turn_id: str | None = None,
        perception_decision_view: PerceptionDecisionView | None = None,
        perception_authority: TurnAuthorityBundle | None = None,
        perception_turn_id: str | None = None,
        perception_source_frame_sha256_by_tab: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.goal.scenario != "grand_masters":
            return self._numeric_blind_result(
                action="stop",
                target=None,
                risk_flags=["numeric_blind_scenario_not_supported"],
                scored_candidates=[],
                evidence={"outcome": "stop", "scenario": self.goal.scenario},
            )
        condition_observation = self._numeric_blind_condition_observation(
            state,
            formal_condition_binding_key=formal_condition_binding_key,
            condition_turn_root=condition_turn_root,
            expected_condition_turn_id=expected_condition_turn_id,
        )
        terminal_errors = numeric_blind_terminal_state_errors(state.errors)
        if state.screen != "training_menu" or terminal_errors:
            return self._numeric_blind_result(
                action="stop",
                target=None,
                risk_flags=["numeric_blind_state_not_eligible"],
                scored_candidates=[],
                evidence={
                    "outcome": "stop",
                    "condition_observation": condition_observation,
                    "terminal_state_errors": terminal_errors,
                },
            )
        if state.errors:
            return self._numeric_blind_result(
                action="reobserve",
                target=None,
                risk_flags=["numeric_blind_state_refresh_required"],
                scored_candidates=[],
                evidence={"outcome": "reobserve", "state_errors": list(state.errors)},
            )
        if condition_observation["valid"] is True:
            condition_id = str(condition_observation.get("condition_id") or "negative")
            return self._numeric_blind_result(
                action="clinic",
                target=None,
                risk_flags=[
                    "clinic_explicit_condition_source",
                    f"clinic_explicit_{condition_id}",
                    "clinic_explicit_disease_condition",
                ],
                scored_candidates=[],
                evidence={
                    "outcome": "clinic",
                    "trigger": f"explicit_{condition_id}",
                    "condition_observation": condition_observation,
                },
            )

        stats = self._numeric_blind_structured_stats(state)
        if stats is None:
            return self._numeric_blind_result(
                action="rest",
                target=None,
                risk_flags=["numeric_blind_structured_stats_unavailable"],
                scored_candidates=[],
                evidence={
                    "outcome": "rest",
                    "safe_failure_rate_max": NUMERIC_BLIND_SAFE_FAILURE_RATE,
                    "condition_observation": condition_observation,
                },
            )

        scored = [
            self._score_numeric_blind_candidate(
                state,
                candidate,
                stats,
                perception_decision_view=perception_decision_view,
                perception_authority=perception_authority,
                perception_turn_id=perception_turn_id,
                perception_source_frame_sha256_by_tab=(
                    perception_source_frame_sha256_by_tab
                ),
            )
            for candidate in state.training_options
        ]
        scored.sort(key=lambda item: (-item["score"], item["action_id"]))
        eligible = [item for item in scored if not is_hard_rejected(item)
                    and item["eligible"] is True and item.get("allowed") is not False]
        if not eligible:
            return self._numeric_blind_result(
                action="rest",
                target=None,
                risk_flags=["numeric_blind_no_safe_training_candidate"],
                scored_candidates=scored,
                evidence={
                    "outcome": "rest",
                    "safe_failure_rate_max": NUMERIC_BLIND_SAFE_FAILURE_RATE,
                    "condition_observation": condition_observation,
                },
            )

        selected = eligible[0]
        return self._numeric_blind_result(
            action="train",
            target=selected["training_type"],
            risk_flags=["numeric_blind_fallback"],
            scored_candidates=scored,
            evidence={
                "outcome": "train",
                "selected_action_id": selected["action_id"],
                "safe_failure_rate_max": NUMERIC_BLIND_SAFE_FAILURE_RATE,
                "selected_score": selected["score"],
                "selected_components": dict(selected["components"]),
                "condition_observation": condition_observation,
            },
        )

    @staticmethod
    def _numeric_blind_result(
        *,
        action: str,
        target: str | None,
        risk_flags: list[str],
        scored_candidates: list[dict[str, Any]],
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        audit_evidence = dict(evidence)
        audit_evidence.setdefault(
            "input_usage", {"untrusted_training_numbers": "not_consumed"}
        )
        audit_evidence.setdefault(
            "scoring_policy",
            {
                "survival_floor_role": "normalized_structured_stat_gap",
                "perception_option_fields": "decision_safe_only",
            },
        )
        return {
            "action": action,
            "target": target,
            "confidence": 1.0,
            "reason": NUMERIC_BLIND_FALLBACK_MODE,
            "mode": NUMERIC_BLIND_FALLBACK_MODE,
            "decision_policy": NUMERIC_BLIND_FALLBACK_MODE,
            "risk_flags": risk_flags,
            "scored_candidates": scored_candidates,
            "numeric_blind_fallback": audit_evidence,
            "ai_review": None,
        }

    @staticmethod
    def _numeric_blind_condition_observation(
        state: TrainingTurnState,
        *,
        formal_condition_binding_key: str | bytes | None,
        condition_turn_root: str | Path | None,
        expected_condition_turn_id: str | None,
    ) -> dict[str, Any]:
        observations = []
        for condition_id, effects in (
            ("chubby_tendency", {"speed_training_gain_blocked": True}),
            (
                "insomnia_tendency",
                {"random_vitality_loss_after_training": True},
            ),
            ("migraine", {"mood_gain_blocked": True}),
        ):
            observation = inspect_formally_bound_condition(
                state.scenario_state,
                expected_turn=state.turn,
                condition_id=condition_id,
                effects=effects,
                formal_binding_key=formal_condition_binding_key,
                required_turn_root=condition_turn_root,
                expected_turn_id=expected_condition_turn_id,
            )
            if observation["valid"] is True:
                return observation
            observations.append(observation)
        return observations[0]

    def _numeric_blind_structured_stats(
        self,
        state: TrainingTurnState,
    ) -> dict[str, tuple[int, int]] | None:
        stats: dict[str, tuple[int, int]] = {}
        for training_type in TRAINING_TYPES:
            current = state.current_stats.get(training_type)
            target = self.goal.target_stats.get(training_type)
            if (
                not isinstance(current, int)
                or isinstance(current, bool)
                or current < 0
                or not isinstance(target, int)
                or isinstance(target, bool)
                or target <= 0
            ):
                return None
            stats[training_type] = (current, target)
        return stats

    def _score_numeric_blind_candidate(
        self,
        state: TrainingTurnState,
        candidate: TrainingCandidate,
        stats: dict[str, tuple[int, int]],
        *,
        perception_decision_view: PerceptionDecisionView | None,
        perception_authority: TurnAuthorityBundle | None,
        perception_turn_id: str | None,
        perception_source_frame_sha256_by_tab: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        risk_flags = self._numeric_blind_candidate_rejections(candidate)
        failure_rate_evidence = self._numeric_blind_failure_rate_evidence(candidate)
        failure_rate = failure_rate_evidence["value"]
        if failure_rate_evidence["reliable"] is not True:
            risk_flags.append("failure_rate_not_reliable")
            reason = failure_rate_evidence["reason"]
            if reason != "failure_rate_not_reliable":
                risk_flags.append(reason)
        elif failure_rate > NUMERIC_BLIND_SAFE_FAILURE_RATE:
            risk_flags.append("failure_rate_above_numeric_blind_safety_limit")

        components: dict[str, float] = {
            "survival_floor_value": 0.0,
            "target_gap_value": 0.0,
            "decision_safe_gain_value": 0.0,
            "decision_safe_skill_pt_value": 0.0,
            "effective_support_value": 0.0,
            "reliable_hint_value": 0.0,
            "decision_safe_vitality_value": 0.0,
        }
        field_usage = self._numeric_blind_field_usage(
            candidate,
            perception_decision_view=perception_decision_view,
            perception_authority=perception_authority,
            perception_turn_id=perception_turn_id,
            perception_source_frame_sha256_by_tab=(
                perception_source_frame_sha256_by_tab
            ),
        )
        effective_support_count = None
        hint_count = None
        if not risk_flags:
            training_type = candidate.training_type
            current, target = stats[training_type]
            floor = self._numeric_blind_basic_floor(state, training_type)
            if floor is not None and current < floor:
                components["survival_floor_value"] = (
                    float(floor - current)
                    / float(floor)
                    * NUMERIC_BLIND_SURVIVAL_FLOOR_WEIGHT
                )
            components["target_gap_value"] = (
                float(max(0, target - current))
                / float(target)
                * NUMERIC_BLIND_TARGET_GAP_WEIGHT
            )
            safe_stat_gain = sum(
                max(
                    0.0,
                    self._numeric_blind_safe_number(
                        field_usage[f"training.{training_type}.training_gain.{stat}"]
                    )
                    or 0.0,
                )
                for stat in TRAINING_TYPES
            )
            components["decision_safe_gain_value"] = safe_stat_gain
            safe_skill_pt = self._numeric_blind_safe_number(
                field_usage[
                    f"training.{training_type}.training_gain.skill_pt"
                ]
            )
            components["decision_safe_skill_pt_value"] = min(
                max(0.0, safe_skill_pt or 0.0) / 2.0,
                NUMERIC_BLIND_SAFE_SKILL_PT_CAP,
            )
            effective_support_count = self._numeric_blind_safe_integer(
                field_usage[
                    f"training.{training_type}.support.effective_count"
                ]
            )
            if effective_support_count is not None and 0 <= effective_support_count <= 6:
                components["effective_support_value"] = float(
                    effective_support_count
                ) * 4.0
            hint_count = self._numeric_blind_safe_integer(
                field_usage[f"training.{training_type}.support.hint_count"]
            )
            if hint_count is not None and 0 <= hint_count <= 6:
                components["reliable_hint_value"] = float(hint_count) * 3.0
            vitality_delta = self._numeric_blind_safe_number(
                field_usage[f"training.{training_type}.vitality_delta"]
            )
            if vitality_delta is not None:
                components["decision_safe_vitality_value"] = max(
                    -NUMERIC_BLIND_SAFE_VITALITY_CAP,
                    min(
                        vitality_delta * 0.25,
                        NUMERIC_BLIND_SAFE_VITALITY_CAP,
                    ),
                )

        score = round(sum(components.values()), 4) if not risk_flags else 0.0
        return {
            "action_id": candidate.action_id,
            "action": "train",
            "training_type": candidate.training_type,
            "score": score,
            "components": {key: round(value, 4) for key, value in components.items()},
            "risk_flags": risk_flags,
            "rejected": bool(risk_flags),
            "hard_rejected": bool(risk_flags),
            "eligible": not risk_flags,
            "evidence": {
                "failure_rate": (
                    failure_rate
                    if failure_rate_evidence["reliable"] is True
                    else None
                ),
                "failure_rate_reliable": failure_rate_evidence["reliable"],
                "failure_rate_reason": failure_rate_evidence["reason"],
                "failure_rate_observation": failure_rate_evidence,
                "safe_failure_rate_max": NUMERIC_BLIND_SAFE_FAILURE_RATE,
                "effective_support_count": effective_support_count,
                "hint_count": hint_count,
                "field_usage": field_usage,
            },
        }

    @staticmethod
    def _numeric_blind_safe_number(field: Mapping[str, Any]) -> float | None:
        value = field.get("value")
        if (
            field.get("status") != "decision_safe"
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            return None
        try:
            parsed = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _numeric_blind_safe_integer(field: Mapping[str, Any]) -> int | None:
        value = field.get("value")
        if field.get("status") != "decision_safe" or type(value) is not int:
            return None
        return value

    @staticmethod
    def _numeric_blind_field_payload(
        *,
        path: str,
        status: str,
        value: Any,
        source_frame_sha256: str | None,
        evidence_schema: str | None,
        gate_id: str | None,
        reasons: tuple[str, ...] | list[str],
    ) -> dict[str, Any]:
        return {
            "path": path,
            "status": status,
            "value": value,
            "source_frame_sha256": source_frame_sha256,
            "evidence_schema": evidence_schema,
            "gate_id": gate_id,
            "reasons": list(reasons),
        }

    def _numeric_blind_field_usage(
        self,
        candidate: TrainingCandidate,
        *,
        perception_decision_view: PerceptionDecisionView | None,
        perception_authority: TurnAuthorityBundle | None,
        perception_turn_id: str | None,
        perception_source_frame_sha256_by_tab: Mapping[str, str] | None,
    ) -> dict[str, dict[str, Any]]:
        tab = candidate.training_type
        fields = (
            *(f"training_gain.{stat}" for stat in STAT_KEYS),
            "support.effective_count",
            "support.hint_count",
            "vitality_delta",
        )
        source_sha = (
            perception_source_frame_sha256_by_tab.get(tab)
            if type(perception_source_frame_sha256_by_tab) is dict
            else None
        )
        unavailable_reason = None
        if type(perception_decision_view) is not PerceptionDecisionView:
            unavailable_reason = "perception_view_unavailable"
        elif type(perception_authority) is not TurnAuthorityBundle:
            unavailable_reason = "perception_authority_unavailable"
        elif not isinstance(perception_turn_id, str):
            unavailable_reason = "perception_turn_unavailable"
        elif not isinstance(source_sha, str):
            unavailable_reason = "perception_source_frame_unavailable"
        usage: dict[str, dict[str, Any]] = {}
        for field in fields:
            path = f"training.{tab}.{field}"
            if unavailable_reason is not None:
                usage[path] = self._numeric_blind_field_payload(
                    path=path,
                    status="unknown",
                    value=None,
                    source_frame_sha256=source_sha,
                    evidence_schema=None,
                    gate_id=None,
                    reasons=(unavailable_reason,),
                )
                continue
            observation = perception_decision_view.training_field(
                tab,
                field,
                authority=perception_authority,
                expected_turn_id=perception_turn_id,
                expected_source_frame_sha256=source_sha,
            )
            usage[path] = self._numeric_blind_field_payload(
                path=observation.path,
                status=observation.status,
                value=observation.decision_value,
                source_frame_sha256=observation.source_frame_sha256,
                evidence_schema=observation.evidence_schema,
                gate_id=observation.gate_id,
                reasons=observation.reasons,
            )
        return usage

    def _numeric_blind_basic_floor(
        self,
        state: TrainingTurnState,
        training_type: str,
    ) -> int | None:
        if training_type not in NUMERIC_BLIND_BASIC_STATS:
            return None
        if state.turn is None or state.turn > 48:
            phase = "late"
        elif state.turn <= 24:
            phase = "early"
        else:
            phase = "mid"
        return self.strategy.SURVIVAL_FLOORS[phase][training_type]

    @staticmethod
    def _numeric_blind_candidate_rejections(
        candidate: TrainingCandidate,
    ) -> list[str]:
        raw = candidate.raw_data
        rejected: list[str] = []
        action = raw.get("action", "train")
        if not isinstance(action, str) or action != "train":
            rejected.append("candidate_action_not_allowed")
        strict_optional_fields = {
            "allowed": True,
            "eligible": True,
            "rejected": False,
            "hard_rejected": False,
        }
        for field, expected in strict_optional_fields.items():
            if field in raw and raw[field] is not expected:
                rejected.append(f"candidate_{field}_not_allowed")
        return rejected

    @staticmethod
    def _numeric_blind_failure_rate_evidence(
        candidate: TrainingCandidate,
    ) -> dict[str, Any]:
        value = candidate.failure_rate
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 100.0
        ):
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_value_invalid",
            }
        parsed_value = float(value)
        unknown_fields = candidate.raw_data.get("unknown_fields", [])
        if not isinstance(unknown_fields, list) or any(
            field in unknown_fields
            for field in (
                "failure_rate",
                "failure_rate_invalid",
                "failure_rate_unreliable",
            )
        ):
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_unknown_or_unreliable",
                "top_level_value": parsed_value,
            }
        if "field_reliability" not in candidate.raw_data:
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_reliability_evidence_missing",
                "top_level_value": parsed_value,
            }
        field_reliability = candidate.raw_data["field_reliability"]
        if (
            not isinstance(field_reliability, dict)
            or field_reliability.get("failure_rate") != "reliable"
        ):
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_reliability_evidence_invalid",
                "top_level_value": parsed_value,
            }
        if "failure_rate_ocr" not in candidate.raw_data:
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_ocr_evidence_missing",
                "top_level_value": parsed_value,
            }
        failure_rate_ocr = candidate.raw_data["failure_rate_ocr"]
        if (
            not isinstance(failure_rate_ocr, dict)
            or failure_rate_ocr.get("status") != "accepted"
        ):
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_ocr_evidence_invalid",
                "top_level_value": parsed_value,
            }
        ocr_value = failure_rate_ocr.get("value")
        if (
            isinstance(ocr_value, bool)
            or not isinstance(ocr_value, (int, float))
            or not math.isfinite(float(ocr_value))
            or not 0.0 <= float(ocr_value) <= 100.0
        ):
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_ocr_value_invalid",
                "top_level_value": parsed_value,
            }
        parsed_ocr_value = float(ocr_value)
        if not math.isclose(
            parsed_value,
            parsed_ocr_value,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            return {
                "value": None,
                "reliable": False,
                "reason": "failure_rate_ocr_contradiction",
                "top_level_value": parsed_value,
                "ocr_value": parsed_ocr_value,
                "consistency_tolerance": 1e-6,
            }
        return {
            "value": parsed_value,
            "reliable": True,
            "reason": "accepted",
            "top_level_value": parsed_value,
            "ocr_value": parsed_ocr_value,
            "consistency_tolerance": 1e-6,
        }

    @staticmethod
    def _eligible_candidate_ids(decision: dict[str, Any]) -> set[str]:
        return {
            item["action_id"]
            for item in decision.get("scored_candidates", [])
            if isinstance(item, dict)
            and isinstance(item.get("action_id"), str)
            and item.get("eligible", True)
            and item.get("allowed") is not False
            and not is_hard_rejected(item)
        }

    def apply_validated_ai_review(
        self,
        deterministic_decision: dict[str, Any],
        review: dict[str, Any],
        *,
        min_ai_review_confidence: float = DEFAULT_AI_REVIEW_MIN_CONFIDENCE,
    ) -> dict[str, Any]:
        result = dict(deterministic_decision)
        applied_review = dict(review)
        applied_review["errors"] = list(review.get("errors", []))
        applied_review["applied"] = False
        result["ai_review"] = applied_review

        if (
            not applied_review.get("valid")
            or applied_review.get("veto")
            or result.get("action") != "train"
        ):
            return result

        selected = applied_review.get("selected_action_id")
        if selected not in self._eligible_candidate_ids(result):
            applied_review["valid"] = False
            if "selected_action_not_in_candidates" not in applied_review["errors"]:
                applied_review["errors"].append("selected_action_not_in_candidates")
            return result

        if float(applied_review.get("confidence", 0.0)) < float(
            min_ai_review_confidence
        ):
            applied_review["valid"] = False
            applied_review["errors"].append(
                "ai_review_confidence_below_threshold"
            )
            return result

        target = selected.split(":", 1)[1]
        result["target"] = target
        result["reason"] = (
            f"AI tactical review selected {selected}: {applied_review['reason']}"
        )
        result["confidence"] = min(
            float(result.get("confidence", 0.0)),
            float(applied_review["confidence"]),
        )
        result["risk_flags"] = list(result.get("risk_flags", [])) + list(
            applied_review.get("risk_flags", [])
        )
        applied_review["applied"] = True
        return result


def parse_ai_review(
    raw: str,
    candidate_ids: set[str],
    expected_packet_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception as exc:
        return {
            "valid": False,
            "errors": [f"json_parse_error:{type(exc).__name__}:{exc}"],
            "selected_action_id": None,
            "confidence": 0.0,
            "reason": "",
            "risk_flags": [],
            "veto": False,
        }

    errors: list[str] = []
    if not isinstance(data, dict):
        errors.append(f"root_not_object:{type(data).__name__}")
        data = {}

    if expected_packet_sha256 is not None:
        packet_sha256 = data.get("decision_packet_sha256")
        if packet_sha256 is None:
            errors.append("decision_packet_hash_missing")
        elif not isinstance(packet_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", packet_sha256
        ):
            errors.append("decision_packet_hash_invalid")
        elif packet_sha256 != expected_packet_sha256:
            errors.append("decision_packet_hash_mismatch")

    selected = data.get("selected_action_id")
    if selected not in candidate_ids:
        errors.append("selected_action_not_in_candidates")

    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        errors.append("confidence_not_number")
        parsed_confidence = 0.0
    elif not math.isfinite(confidence):
        errors.append("confidence_not_finite")
        parsed_confidence = 0.0
    elif not 0.0 <= confidence <= 1.0:
        errors.append("confidence_out_of_range")
        parsed_confidence = 0.0
    else:
        parsed_confidence = float(confidence)

    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("reason_required")
        reason = ""

    risk_flags = data.get("risk_flags", [])
    if not isinstance(risk_flags, list) or not all(
        isinstance(item, str) for item in risk_flags
    ):
        errors.append("risk_flags_must_be_string_list")
        risk_flags = []

    veto = data.get("veto", False)
    if not isinstance(veto, bool):
        errors.append("veto_must_be_bool")
        veto = False

    return {
        "valid": not errors,
        "errors": errors,
        "selected_action_id": selected if isinstance(selected, str) else None,
        "confidence": parsed_confidence,
        "reason": reason,
        "risk_flags": list(risk_flags),
        "veto": veto,
    }
