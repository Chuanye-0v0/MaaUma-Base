from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import secrets
import threading
from typing import Any

from core.auto_trainer.ai_review import AITrainingReviewClient, review_training_decision
from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.decision import (
    DEFAULT_AI_REVIEW_MIN_CONFIDENCE,
    DecisionEngine,
)
from core.auto_trainer.dashboard_runner import ControlledStop, CooperativeRunnerDriver
from core.auto_trainer.executor import TrainingActionExecutor
from core.auto_trainer.frame_safety import FrameSafetyPacket
from core.auto_trainer.logging import RunLogger
from core.auto_trainer.state import TrainingTurnState
from core.auto_trainer.structured_action_gate import (
    StructuredExecutionGuard,
)
from core.auto_trainer.structured_advisor_runtime import (
    StructuredAdvisorRuntime,
)
from core.auto_trainer.structured_training_advisor import (
    StructuredTrainingAdvisorTurn,
    TrainingAdvisorMetadata,
    apply_structured_training_advisor,
)


class PreparedTurnError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreparedTrainingTurn:
    loop_binding_sha256: str
    turn_index: int
    state: TrainingTurnState
    state_snapshot: dict[str, Any]
    state_snapshot_sha256: str
    decision: dict[str, Any]
    decision_sha256: str
    ai_review: Any
    numeric_blind_enabled: bool
    structured_turn: StructuredTrainingAdvisorTurn | None
    structured_action_guard: Any | None
    structured_decision_packet_sha256: str | None
    structured_action_plan_sha256: str | None
    mode: str
    execute_training_confirm: bool
    confirm_token: str | None
    max_turns: int | None
    post_action_skip_taps: int
    min_decision_confidence: float
    require_selected_primary_gain_reliable: bool
    prepared_sha256: str


class UGParentTrainingLoop:
    def __init__(
        self,
        *,
        goal: UGParentGoal,
        run_dir: Path,
        executor: TrainingActionExecutor | Any | None = None,
        ai_client: AITrainingReviewClient | None = None,
        formal_condition_binding_key: str | bytes | None = None,
        condition_turn_root: str | Path | None = None,
        expected_condition_turn_id: str | None = None,
        structured_advisor_runtime: StructuredAdvisorRuntime | None = None,
        structured_advisor_metadata: TrainingAdvisorMetadata | None = None,
        structured_perception_frame_packet: FrameSafetyPacket | None = None,
        structured_frame_safety_analyzer: Any | None = None,
    ) -> None:
        self.goal = goal
        self.run_dir = Path(run_dir)
        self.executor = executor or TrainingActionExecutor(scenario=goal.scenario)
        self.ai_client = ai_client
        self.formal_condition_binding_key = formal_condition_binding_key
        self.condition_turn_root = condition_turn_root
        self.expected_condition_turn_id = expected_condition_turn_id
        if ai_client is not None and structured_advisor_runtime is not None:
            raise ValueError("legacy_and_structured_advisor_conflict")
        structured_values = (
            structured_advisor_metadata,
            structured_perception_frame_packet,
            structured_frame_safety_analyzer,
        )
        if structured_advisor_runtime is not None and any(
            value is None for value in structured_values
        ):
            raise ValueError("structured_advisor_context_incomplete")
        if structured_advisor_runtime is None and any(
            value is not None for value in structured_values
        ):
            raise ValueError("structured_advisor_runtime_required")
        self.structured_advisor_runtime = structured_advisor_runtime
        self.structured_advisor_metadata = structured_advisor_metadata
        self.structured_perception_frame_packet = (
            structured_perception_frame_packet
        )
        self.structured_frame_safety_analyzer = (
            structured_frame_safety_analyzer
        )
        self.logger = RunLogger(self.run_dir)
        self.engine = DecisionEngine(goal)
        self._turn_index = 0
        self._prepare_binding_sha256 = hashlib.sha256(
            secrets.token_bytes(32)
        ).hexdigest()
        self._consumed_prepared_sha256s: set[str] = set()
        self._execution_outcome_unknown = False
        self._execution_lock = threading.RLock()
        self.logger.write_run_config(goal, mode="loop")

    def run_turn(
        self,
        state: TrainingTurnState,
        *,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        post_action_skip_taps: int = 0,
        min_decision_confidence: float = 0.0,
        min_ai_review_confidence: float = DEFAULT_AI_REVIEW_MIN_CONFIDENCE,
        require_selected_primary_gain_reliable: bool = False,
        numeric_blind_fallback: bool = False,
        perception_decision_view=None,
        perception_authority=None,
        perception_turn_id: str | None = None,
        perception_source_frame_sha256_by_tab=None,
        control_driver: CooperativeRunnerDriver | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_turn(
            state,
            mode=mode,
            execute_training_confirm=execute_training_confirm,
            confirm_token=confirm_token,
            max_turns=max_turns,
            post_action_skip_taps=post_action_skip_taps,
            min_decision_confidence=min_decision_confidence,
            min_ai_review_confidence=min_ai_review_confidence,
            require_selected_primary_gain_reliable=(
                require_selected_primary_gain_reliable
            ),
            numeric_blind_fallback=numeric_blind_fallback,
            perception_decision_view=perception_decision_view,
            perception_authority=perception_authority,
            perception_turn_id=perception_turn_id,
            perception_source_frame_sha256_by_tab=(
                perception_source_frame_sha256_by_tab
            ),
            control_driver=control_driver,
        )
        if isinstance(prepared, dict):
            return prepared
        return self.execute_prepared_turn(
            prepared,
            control_driver=control_driver,
        )

    def prepare_turn(
        self,
        state: TrainingTurnState,
        *,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        post_action_skip_taps: int = 0,
        min_decision_confidence: float = 0.0,
        min_ai_review_confidence: float = (
            DEFAULT_AI_REVIEW_MIN_CONFIDENCE
        ),
        require_selected_primary_gain_reliable: bool = False,
        numeric_blind_fallback: bool = False,
        perception_decision_view=None,
        perception_authority=None,
        perception_turn_id: str | None = None,
        perception_source_frame_sha256_by_tab=None,
        control_driver: CooperativeRunnerDriver | None = None,
    ) -> PreparedTrainingTurn | dict[str, Any]:
        with self._execution_lock:
            if self._execution_outcome_unknown:
                raise PreparedTurnError(
                    "loop_execution_outcome_unknown"
                )
        if control_driver is not None:
            try:
                control_driver.reach("before_decision")
            except ControlledStop as exc:
                return {
                    "status": "control_stopped",
                    "control_boundary": exc.boundary,
                }
        ai_review = None
        from core.auto_trainer.larc_ai_runtime import current_larc_ai, apply_training_ai, LarcAIPause
        larc_ai=current_larc_ai()
        if larc_ai is not None and larc_ai.mode!='local' and (self.ai_client is not None or self.structured_advisor_runtime is not None):
            raise ValueError('larc_ai_and_structured_advisor_conflict')
        numeric_blind_enabled = numeric_blind_fallback is True
        perception_kwargs: dict[str, Any] = {}
        if perception_decision_view is not None:
            perception_kwargs = {
                "perception_decision_view": perception_decision_view,
                "perception_authority": perception_authority,
                "perception_turn_id": perception_turn_id,
                "perception_source_frame_sha256_by_tab": (
                    perception_source_frame_sha256_by_tab
                ),
            }
        if numeric_blind_enabled:
            decision = self.engine.decide_numeric_blind_fallback(
                state,
                formal_condition_binding_key=self.formal_condition_binding_key,
                condition_turn_root=self.condition_turn_root,
                expected_condition_turn_id=self.expected_condition_turn_id,
                **perception_kwargs,
            )
            if decision["action"] == "reobserve":
                return {
                    "status": "numeric_blind_state_refresh_required",
                    "decision": decision,
                    "execution": {
                        "performed": False, "reason": "numeric_blind_state_refresh_required", "tap_plan": [],
                    },
                }
        else:
            deterministic = self.engine.decide(
                state,
                **perception_kwargs,
            )
            decision = deterministic
            if self.ai_client is not None and deterministic.get("action") == "train":
                ai_review = review_training_decision(
                    state=state,
                    deterministic_decision=deterministic,
                    client=self.ai_client,
                )
                decision = self.engine.apply_validated_ai_review(
                    deterministic,
                    ai_review,
                    min_ai_review_confidence=min_ai_review_confidence,
                )
                ai_review = decision["ai_review"]

            decision = self._apply_selected_primary_gain_reliability_fallback(
                state=state,
                decision=decision,
                required=require_selected_primary_gain_reliable,
            )

        if not numeric_blind_enabled:
            try:
                decision=apply_training_ai(state,self.goal,decision,
                    require_primary_gain=require_selected_primary_gain_reliable)
            except LarcAIPause as exc:
                return {'status':'control_stopped','control_boundary':'larc_ai_pause','larc_ai':exc.result}

        structured_turn: StructuredTrainingAdvisorTurn | None = None
        structured_action_guard = None
        if self.structured_advisor_runtime is not None:
            structured_turn = apply_structured_training_advisor(
                state=state,
                deterministic_decision=decision,
                metadata=self.structured_advisor_metadata,
                runtime=self.structured_advisor_runtime,
            )
            decision = structured_turn.final_decision
            if structured_turn.plan is not None:
                structured_action_guard = StructuredExecutionGuard(
                    authority=structured_turn.authority,
                    decision_packet=structured_turn.decision_packet,
                    advisor_outcome=structured_turn.outcome,
                    plan=structured_turn.plan,
                    perception_frame_packet=(
                        self.structured_perception_frame_packet
                    ),
                    frame_safety_analyzer=(
                        self.structured_frame_safety_analyzer
                    ),
                )

        source_state_snapshot = state.to_dict()
        state_snapshot = dict(source_state_snapshot)
        if numeric_blind_enabled:
            state_snapshot["numeric_blind_input_usage"] = {
                "untrusted_training_numbers": "not_consumed"
            }
        structured_decision_sha = (
            structured_turn.decision_packet.decision_packet_sha256
            if structured_turn is not None
            else None
        )
        structured_plan_sha = (
            structured_turn.plan.plan_sha256
            if structured_turn is not None
            and structured_turn.plan is not None
            else None
        )
        effective_gain_requirement = (
            require_selected_primary_gain_reliable
            and not numeric_blind_enabled
        )
        material = {
            "loop_binding_sha256": self._prepare_binding_sha256,
            "turn_index": self._turn_index,
            "state_snapshot": state_snapshot,
            "decision": decision,
            "numeric_blind_enabled": numeric_blind_enabled,
            "structured_decision_packet_sha256": (
                structured_decision_sha
            ),
            "structured_action_plan_sha256": structured_plan_sha,
            "mode": mode,
            "execute_training_confirm": execute_training_confirm,
            "confirm_token": confirm_token,
            "max_turns": max_turns,
            "post_action_skip_taps": post_action_skip_taps,
            "min_decision_confidence": min_decision_confidence,
            "require_selected_primary_gain_reliable": (
                effective_gain_requirement
            ),
        }
        return PreparedTrainingTurn(
            loop_binding_sha256=self._prepare_binding_sha256,
            turn_index=self._turn_index,
            state=state,
            state_snapshot=state_snapshot,
            state_snapshot_sha256=_canonical_sha(
                source_state_snapshot
            ),
            decision=dict(decision),
            decision_sha256=_canonical_sha(decision),
            ai_review=ai_review,
            numeric_blind_enabled=numeric_blind_enabled,
            structured_turn=structured_turn,
            structured_action_guard=structured_action_guard,
            structured_decision_packet_sha256=(
                structured_decision_sha
            ),
            structured_action_plan_sha256=structured_plan_sha,
            mode=mode,
            execute_training_confirm=execute_training_confirm,
            confirm_token=confirm_token,
            max_turns=max_turns,
            post_action_skip_taps=post_action_skip_taps,
            min_decision_confidence=min_decision_confidence,
            require_selected_primary_gain_reliable=(
                effective_gain_requirement
            ),
            prepared_sha256=_canonical_sha(material),
        )

    def execute_prepared_turn(
        self,
        prepared: PreparedTrainingTurn,
        *,
        control_driver: CooperativeRunnerDriver | None = None,
    ) -> dict[str, Any]:
        with self._execution_lock:
            self._validate_prepared_turn(prepared)
            self._consumed_prepared_sha256s.add(
                prepared.prepared_sha256
            )
            try:
                execution = self._execute_decision(
                    state=prepared.state,
                    decision=prepared.decision,
                    turn_index=prepared.turn_index,
                    mode=prepared.mode,
                    execute_training_confirm=(
                        prepared.execute_training_confirm
                    ),
                    confirm_token=prepared.confirm_token,
                    max_turns=prepared.max_turns,
                    post_action_skip_taps=(
                        prepared.post_action_skip_taps
                    ),
                    min_decision_confidence=(
                        prepared.min_decision_confidence
                    ),
                    require_selected_primary_gain_reliable=(
                        prepared.require_selected_primary_gain_reliable
                    ),
                    control_driver=control_driver,
                    structured_action_guard=(
                        prepared.structured_action_guard
                    ),
                )
                self.logger.write_turn(
                    turn_index=prepared.turn_index,
                    state=prepared.state_snapshot,
                    candidates=prepared.decision.get(
                        "scored_candidates",
                        [],
                    ),
                    decision=prepared.decision,
                    execution=execution,
                    ai_review=prepared.ai_review,
                )
            except Exception:
                self._execution_outcome_unknown = True
                raise
            self._turn_index += 1
            return {
                "turn_index": prepared.turn_index,
                "state": prepared.state_snapshot,
                "decision": prepared.decision,
                "execution": execution,
            }

    def validate_prepared_turn(
        self,
        prepared: PreparedTrainingTurn,
    ) -> None:
        with self._execution_lock:
            self._validate_prepared_turn(prepared)

    def _validate_prepared_turn(
        self,
        prepared: PreparedTrainingTurn,
    ) -> None:
        if not isinstance(prepared, PreparedTrainingTurn):
            raise PreparedTurnError("prepared_turn_required")
        if prepared.loop_binding_sha256 != self._prepare_binding_sha256:
            raise PreparedTurnError("prepared_loop_binding_mismatch")
        if self._execution_outcome_unknown:
            raise PreparedTurnError("loop_execution_outcome_unknown")
        if (
            prepared.prepared_sha256
            in self._consumed_prepared_sha256s
        ):
            raise PreparedTurnError("prepared_turn_already_consumed")
        if prepared.turn_index != self._turn_index:
            raise PreparedTurnError("prepared_turn_index_mismatch")
        current_state_snapshot = prepared.state.to_dict()
        if (
            _canonical_sha(current_state_snapshot)
            != prepared.state_snapshot_sha256
        ):
            raise PreparedTurnError("prepared_state_snapshot_mismatch")
        if _canonical_sha(prepared.decision) != prepared.decision_sha256:
            raise PreparedTurnError("prepared_decision_mismatch")
        structured_turn = prepared.structured_turn
        if structured_turn is None:
            if (
                prepared.structured_action_guard is not None
                or prepared.structured_decision_packet_sha256 is not None
                or prepared.structured_action_plan_sha256 is not None
            ):
                raise PreparedTurnError(
                    "prepared_structured_binding_mismatch"
                )
        else:
            if (
                structured_turn.decision_packet.decision_packet_sha256
                != prepared.structured_decision_packet_sha256
            ):
                raise PreparedTurnError(
                    "prepared_structured_decision_mismatch"
                )
            plan_sha = (
                structured_turn.plan.plan_sha256
                if structured_turn.plan is not None
                else None
            )
            if plan_sha != prepared.structured_action_plan_sha256:
                raise PreparedTurnError(
                    "prepared_structured_plan_mismatch"
                )
            if (
                (structured_turn.plan is None)
                != (prepared.structured_action_guard is None)
            ):
                raise PreparedTurnError(
                    "prepared_structured_guard_mismatch"
                )
        material = {
            "loop_binding_sha256": prepared.loop_binding_sha256,
            "turn_index": prepared.turn_index,
            "state_snapshot": prepared.state_snapshot,
            "decision": prepared.decision,
            "numeric_blind_enabled": prepared.numeric_blind_enabled,
            "structured_decision_packet_sha256": (
                prepared.structured_decision_packet_sha256
            ),
            "structured_action_plan_sha256": (
                prepared.structured_action_plan_sha256
            ),
            "mode": prepared.mode,
            "execute_training_confirm": (
                prepared.execute_training_confirm
            ),
            "confirm_token": prepared.confirm_token,
            "max_turns": prepared.max_turns,
            "post_action_skip_taps": prepared.post_action_skip_taps,
            "min_decision_confidence": (
                prepared.min_decision_confidence
            ),
            "require_selected_primary_gain_reliable": (
                prepared.require_selected_primary_gain_reliable
            ),
        }
        if _canonical_sha(material) != prepared.prepared_sha256:
            raise PreparedTurnError("prepared_turn_sha_mismatch")

    @classmethod
    def _apply_selected_primary_gain_reliability_fallback(
        cls,
        *,
        state: TrainingTurnState,
        decision: dict[str, Any],
        required: bool,
    ) -> dict[str, Any]:
        if not required or decision.get("action") != "train":
            return decision
        original_target = decision.get("target")
        if (
            not isinstance(original_target, str)
            or cls._selected_primary_gain_is_reliable(state, original_target)
        ):
            return decision

        fallback_selection = cls._reliable_training_fallback_candidate(state, decision)
        if fallback_selection is None:
            return decision

        fallback, fallback_confidence = fallback_selection
        target = fallback["training_type"]
        original_confidence = cls._bounded_confidence(
            decision.get("confidence", 0.0)
        )
        result = dict(decision)
        result["target"] = target
        result["confidence"] = min(original_confidence, fallback_confidence)
        result["reason"] = (
            "Reliability fallback selected "
            f"{target} after {original_target} primary gain was unreliable"
        )
        result["risk_flags"] = list(decision.get("risk_flags", [])) + [
            "selected_primary_gain_unreliable_fallback"
        ]
        result["original_target"] = original_target
        result["reliability_fallback"] = {
            "reason": "selected_primary_gain_unreliable",
            "selected_action_id": fallback["action_id"],
            "original_selected_primary_gain": cls._selected_primary_gain_evidence(
                state,
                original_target,
            ),
            "selected_primary_gain": cls._selected_primary_gain_evidence(
                state,
                target,
            ),
        }
        return result

    @classmethod
    def _reliable_training_fallback_candidate(
        cls,
        state: TrainingTurnState,
        decision: dict[str, Any],
    ) -> tuple[dict[str, Any], float] | None:
        candidates = decision.get("scored_candidates", [])
        if not isinstance(candidates, list):
            return None
        best: tuple[float, int, dict[str, Any], float] | None = None
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            training_type = candidate.get("training_type")
            source_confidence = cls._valid_fallback_source_confidence(candidate)
            if (
                not isinstance(training_type, str)
                or candidate.get("action_id") != f"train:{training_type}"
                or candidate.get("rejected") is not False
                or not cls._optional_candidate_boolean_is(
                    candidate,
                    "hard_rejected",
                    False,
                )
                or not cls._optional_candidate_boolean_is(
                    candidate,
                    "eligible",
                    True,
                )
                or source_confidence is None
                or not cls._selected_primary_gain_is_reliable(state, training_type)
            ):
                continue
            raw_score = candidate.get("score")
            score = (
                float(raw_score)
                if isinstance(raw_score, (int, float))
                and not isinstance(raw_score, bool)
                and math.isfinite(float(raw_score))
                else float("-inf")
            )
            if best is None or score > best[0]:
                best = (score, index, candidate, source_confidence)
        if best is None:
            return None
        return best[2], best[3]

    @staticmethod
    def _optional_candidate_boolean_is(
        candidate: dict[str, Any],
        field: str,
        expected: bool,
    ) -> bool:
        return field not in candidate or candidate[field] is expected

    @staticmethod
    def _valid_fallback_source_confidence(
        candidate: dict[str, Any],
    ) -> float | None:
        value = candidate.get("source_confidence")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or not 0.0 < value <= 1.0:
            return None
        return value

    @staticmethod
    def _bounded_confidence(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        value = float(value)
        if not math.isfinite(value):
            return 0.0
        return max(0.0, min(1.0, value))

    def _execute_decision(
        self,
        *,
        state: TrainingTurnState,
        decision: dict[str, Any],
        turn_index: int,
        mode: str,
        execute_training_confirm: bool,
        confirm_token: str | None,
        max_turns: int | None,
        post_action_skip_taps: int,
        min_decision_confidence: float,
        require_selected_primary_gain_reliable: bool,
        control_driver: CooperativeRunnerDriver | None = None,
        structured_action_guard: Any | None = None,
    ) -> dict[str, Any]:
        action = decision.get("action")
        if action=="outing" and decision.get("target")=="normal":
            from core.auto_trainer.larc_charge_unlock import charge_unlock_window
            from core.auto_trainer.larc_friend_outing import bound_outing_packet
            if (not charge_unlock_window(state,self.goal) or bound_outing_packet(state) is not None
                    or float(decision.get('confidence',0))<min_decision_confidence):
                return {'performed':False,'reason':'normal_outing_opening_context_unverified','tap_plan':[]}
            return self.executor.execute_rest(mode=mode,recovery_kind='outing',
                execute_training_confirm=execute_training_confirm,confirm_token=confirm_token,max_turns=max_turns,
                control_driver=control_driver,structured_action_guard=structured_action_guard,
                safety_screenshot_dir=self.run_dir/'turns'/f'turn_{turn_index:03d}'/'normal_outing')
        if action == "outing":
            from core.auto_trainer.larc_friend_outing import bound_outing_packet
            packet = bound_outing_packet(state)
            if (packet is None or decision.get("target") != f"satake_mei:{packet['completed']+1}"
                    or float(decision.get("confidence", 0)) < min_decision_confidence):
                return {"performed": False, "reason": "mei_outing_decision_unverified", "tap_plan": []}
            return self.executor.execute_mei_outing(packet=packet, mode=mode,
                execute_training_confirm=execute_training_confirm, confirm_token=confirm_token,
                max_turns=max_turns, control_driver=control_driver,
                structured_action_guard=structured_action_guard,
                safety_screenshot_dir=self.run_dir / "turns" / f"turn_{turn_index:03d}" / "mei_outing")
        if action == "clinic":
            kwargs = {
                "mode": mode,
                "execute_training_confirm": execute_training_confirm,
                "confirm_token": confirm_token,
                "max_turns": max_turns,
                "allow_clinic": self.goal.execution.allow_clinic,
                "safety_screenshot_dir": (
                    self.run_dir / "turns" / f"turn_{turn_index:03d}" / "screenshots"
                ),
            }
            if control_driver is not None:
                kwargs["control_driver"] = control_driver
            if structured_action_guard is not None:
                kwargs["structured_action_guard"] = (
                    structured_action_guard
                )
            return self.executor.execute_clinic(**kwargs)
        if action == "rest":
            kwargs = {
                "mode": mode,
                "execute_training_confirm": execute_training_confirm,
                "confirm_token": confirm_token,
                "max_turns": max_turns,
                "safety_screenshot_dir": (
                    self.run_dir / "turns" / f"turn_{turn_index:03d}" / "screenshots"
                ),
            }
            if control_driver is not None:
                kwargs["control_driver"] = control_driver
            if structured_action_guard is not None:
                kwargs["structured_action_guard"] = (
                    structured_action_guard
                )
            return self.executor.execute_rest(**kwargs)
        if action != "train":
            return {
                "performed": False,
                "target": decision.get("target"),
                "reason": "decision_action_not_train",
                "tap_plan": [],
            }
        target = decision.get("target")
        if not isinstance(target, str):
            return {
                "performed": False,
                "target": None,
                "reason": "decision_target_missing",
                "tap_plan": [],
            }
        confidence = float(decision.get("confidence", 0.0) or 0.0)
        if confidence < min_decision_confidence:
            return {
                "performed": False,
                "target": target,
                "reason": "decision_confidence_below_threshold",
                "tap_plan": [],
                "decision_confidence": confidence,
                "required_confidence": min_decision_confidence,
            }
        if (
            require_selected_primary_gain_reliable
            and not self._selected_primary_gain_is_reliable(state, target)
        ):
            return {
                "performed": False,
                "target": target,
                "reason": "selected_primary_gain_unreliable",
                "tap_plan": [],
                "selected_primary_gain": self._selected_primary_gain_evidence(
                    state,
                    target,
                ),
            }
        kwargs = {
            "target": target,
            "mode": mode,
            "execute_training_confirm": execute_training_confirm,
            "confirm_token": confirm_token,
            "max_turns": max_turns,
            "post_action_skip_taps": post_action_skip_taps,
            "safety_screenshot_dir": (
                self.run_dir / "turns" / f"turn_{turn_index:03d}" / "screenshots"
            ),
        }
        if control_driver is not None:
            kwargs["control_driver"] = control_driver
        if structured_action_guard is not None:
            kwargs["structured_action_guard"] = (
                structured_action_guard
            )
        return self.executor.execute_training(**kwargs)

    @staticmethod
    def _is_capped_stat_zero_primary_gain(
        state: TrainingTurnState,
        candidate: Any,
        target: str,
        gain_breakdown: dict[str, Any],
        primary: dict[str, Any],
    ) -> bool:
        if target not in ("speed", "stamina", "power", "guts", "wisdom"):
            return False
        current_val = (
            state.current_stats.get(target)
            if isinstance(state.current_stats, dict)
            else None
        )
        if type(current_val) is not int or current_val < 1200:
            return False
        if (
            primary.get("total_gain", 0) != 0
            or primary.get("base_gain", 0) != 0
            or primary.get("fragment_or_scenario_gain", 0) != 0
        ):
            return False
        skill_pt = gain_breakdown.get("skill_pt")
        if not isinstance(skill_pt, dict) or skill_pt.get("reliable") is not True:
            return False
        skill_pt_gain = skill_pt.get("total_gain")
        if (
            not isinstance(skill_pt_gain, (int, float))
            or isinstance(skill_pt_gain, bool)
            or skill_pt_gain <= 0
        ):
            return False
        stats_gain_ocr = candidate.raw_data.get("stats_gain_ocr")
        columns = (
            stats_gain_ocr.get("columns")
            if isinstance(stats_gain_ocr, dict)
            else None
        )
        if not isinstance(columns, list):
            return False
        col = next(
            (
                item
                for item in columns
                if isinstance(item, dict) and item.get("attribute_id") == target
            ),
            None,
        )
        if not isinstance(col, dict):
            return False
        rows = col.get("rows")
        def _is_zero_or_absent_row(row: Any) -> bool:
            if not isinstance(row, dict):
                return False
            if row.get("occupancy") == "absent" and row.get("status") == "absent":
                return True
            val = row.get("value")
            return (
                row.get("status") == "accepted"
                and type(val) is int
                and val == 0
            )
        if (
            (
                col.get("reason") == "gain_rows_not_found"
                or col.get("status") in {"abstained", "accepted", "empty"}
            )
            and isinstance(rows, list)
            and len(rows) > 0
            and all(_is_zero_or_absent_row(r) for r in rows)
        ):
            return True
        return col.get("status") == "empty" and col.get("reason") == "no_components"

    @classmethod
    def _selected_primary_gain_evidence(
        cls,
        state: TrainingTurnState,
        target: str,
    ) -> dict[str, Any]:
        candidate = next(
            (
                option
                for option in state.training_options
                if option.training_type == target
            ),
            None,
        )
        if candidate is None:
            return {"present": False, "reliable": False, "total_gain": None}
        gain_breakdown = candidate.raw_data.get("gain_breakdown")
        primary = (
            gain_breakdown.get(target)
            if isinstance(gain_breakdown, dict)
            else None
        )
        if not isinstance(primary, dict):
            return {"present": False, "reliable": False, "total_gain": None}
        total_gain = primary.get("total_gain")
        is_positive_number = (
            isinstance(total_gain, (int, float))
            and not isinstance(total_gain, bool)
            and total_gain > 0
        )
        if not is_positive_number and cls._is_capped_stat_zero_primary_gain(
            state, candidate, target, gain_breakdown, primary
        ):
            return {
                "present": True,
                "reliable": True,
                "total_gain": 0,
                "capped_zero_gain": True,
            }
        return {
            "present": True,
            "reliable": primary.get("reliable") is True,
            "total_gain": total_gain if is_positive_number else None,
        }

    @classmethod
    def _selected_primary_gain_is_reliable(
        cls,
        state: TrainingTurnState,
        target: str,
    ) -> bool:
        evidence = cls._selected_primary_gain_evidence(state, target)
        return bool(
            evidence["present"]
            and evidence["reliable"]
            and (evidence["total_gain"] or evidence.get("capped_zero_gain") is True)
        )


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
