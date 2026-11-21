from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any

from core.auto_trainer.condition_context import condition_allows_clinic
from core.auto_trainer.config import UGParentGoal
from core.auto_trainer.rejection_policy import discount_soft_score, is_hard_rejected
from core.auto_trainer.state import (
    STAT_KEYS,
    TRAINING_TYPES,
    TrainingCandidate,
    TrainingTurnState,
)


@dataclass(frozen=True)
class StrategyResult:
    action: str
    target: str | None
    confidence: float
    reason: str
    risk_flags: list[str] = field(default_factory=list)
    scored_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GrandMastersUGParentStrategy:
    """Grand Masters strategy for UG+ parent dry-run decisions."""

    FAILURE_LIMITS = {"low": 15, "medium": 30, "high": 45}
    GROUP_CARD_FRIENDSHIP_TRAINING_VALUE = 0.0
    PASSION_ZONE_CONDITION_ID = "ancestral_guidance_passion_zone"
    PASSION_ZONE_GROUP_CARD_ID = "30137"
    SURVIVAL_FLOORS = {
        "early": {"speed": 260, "stamina": 190, "power": 240, "guts": 170, "wisdom": 220},
        "mid": {"speed": 400, "stamina": 360, "power": 380, "guts": 260, "wisdom": 320},
        "late": {"speed": 650, "stamina": 500, "power": 600, "guts": 350, "wisdom": 450},
    }
    POOR_MOODS = {"bad", "very_bad", "poor", "awful", "不佳", "极差"}

    def __init__(self, goal: UGParentGoal):
        self.goal = goal

    def evaluate(self, state: TrainingTurnState) -> StrategyResult:
        if state.screen != "training_menu":
            return StrategyResult(
                action="stop",
                target=None,
                confidence=0.0,
                reason=f"Unsupported screen: {state.screen}",
                risk_flags=["unsupported_screen"],
                scored_candidates=[],
            )
        if state.errors:
            return StrategyResult(
                action="stop",
                target=None,
                confidence=0.0,
                reason="State contains hard errors",
                risk_flags=["state_errors_present"],
                scored_candidates=[],
            )

        scored = [
            self._score_candidate(state, candidate)
            for candidate in state.training_options
        ]
        clinic_result = self._clinic_result_if_indicated(state, scored)
        if clinic_result is not None:
            return clinic_result
        eligible = [item for item in scored if not is_hard_rejected(item)]
        eligible.sort(key=lambda item: (-item["score"], item["action_id"]))

        if not eligible:
            recovery = self._recovery_result_if_available(state, scored)
            if recovery is not None:
                return recovery
            return StrategyResult(
                action="stop",
                target=None,
                confidence=0.0,
                reason="No eligible training candidate",
                risk_flags=["no_eligible_training_candidate"],
                scored_candidates=sorted(scored, key=lambda item: item["action_id"]),
            )

        best = eligible[0]
        recovery = self._recovery_result_if_available(state, scored)
        if recovery is not None and (
            self._recovery_overrides_training(recovery) or best["score"] <= 0
        ):
            return recovery

        second = eligible[1]["score"] if len(eligible) > 1 else 0.0
        margin = best["score"] - second
        confidence = self._confidence(
            best_score=best["score"],
            second_score=second,
            source_confidence=best["source_confidence"],
        )
        if self._uses_fallback_stat_priority(best):
            confidence = min(confidence, 0.55)
        if "support_identity_unverified" in best["risk_flags"]:
            confidence = min(confidence, 0.65)

        return StrategyResult(
            action="train",
            target=best["training_type"],
            confidence=confidence,
            reason=(
                f"Selected {best['training_type']} with score "
                f"{round(best['score'], 2)} and margin {round(margin, 2)}"
            ),
            risk_flags=list(best["risk_flags"]),
            scored_candidates=sorted(
                scored, key=lambda item: (-item["score"], item["action_id"])
            ),
        )

    def _score_candidate(
        self, state: TrainingTurnState, candidate: TrainingCandidate
    ) -> dict[str, Any]:
        soft_flags: list[str] = []
        hard_flags: list[str] = []
        source_confidence = self._safe_unit_interval(candidate.confidence)
        group_card_friendship_training = (
            self._group_card_friendship_training_evidence(state, candidate)
        )
        soft_flags.extend(group_card_friendship_training["risk_flags"])

        if self._confidence_invalid(candidate):
            soft_flags.append("source_confidence_invalid")

        raw = candidate.raw_data
        if (candidate.training_type not in TRAINING_TYPES
                or raw.get("action", "train") != "train" or raw.get("allowed") is False
                or raw.get("eligible") is False or is_hard_rejected(raw)):
            hard_flags.append("action_not_authorized")

        failure_limit = self.FAILURE_LIMITS[self.goal.risk_tolerance]
        if candidate.failure_rate is None or self._failure_rate_invalid(candidate):
            soft_flags.append("failure_rate_unreadable")
        elif candidate.failure_rate > failure_limit:
            hard_flags.append("failure_rate_exceeds_limit")

        if self._has_unknown_field(candidate, {"stats_gain_partial"}):
            soft_flags.append("stats_gain_partial")

        primary_stat_gain_blocked = self._primary_stat_gain_blocked(candidate)
        if primary_stat_gain_blocked:
            soft_flags.append("primary_stat_gain_blocked")
        if self._has_unverified_support_evidence(candidate):
            soft_flags.append("support_identity_unverified")

        survival_floor_value = (
            0.0
            if primary_stat_gain_blocked
            else self._survival_floor_value(state, candidate)
        )
        risk_flags = soft_flags + hard_flags
        if "failure_rate_exceeds_limit" in hard_flags:
            risk_flags.append(f"failure_rate_exceeds_{self.goal.risk_tolerance}_risk")
        if survival_floor_value > 0:
            risk_flags.append(f"survival_floor:{candidate.training_type}")

        components = {
            "immediate_stat_gain_value": self._immediate_gain_value(candidate),
            "target_gap_value": self._target_gap_value(state, candidate),
            "fallback_stat_priority_value": (
                0.0
                if primary_stat_gain_blocked
                else self._fallback_stat_priority_value(state, candidate)
            ),
            "survival_floor_value": survival_floor_value,
            "support_value": self._support_value(candidate),
            "group_card_friendship_training_value": group_card_friendship_training[
                "value"
            ],
            "friendship_value": self._friendship_value(
                state, candidate, primary_stat_gain_blocked
            ),
            "hint_and_skill_value": self._hint_and_skill_value(candidate),
            "fragment_value": self._fragment_value(state, candidate),
            "vitality_penalty": self._vitality_penalty(state, candidate),
            "failure_rate_penalty": self._failure_rate_penalty(candidate),
            "uncertainty_penalty": self._uncertainty_penalty(candidate),
        }
        score = sum(components.values())
        if primary_stat_gain_blocked:
            # Secondary rewards alone do not make a blocked primary gain positive.
            score = min(score, 0.0)
        if hard_flags:
            score = min(score, -100.0)
        else:
            discounted = discount_soft_score(score, bool(soft_flags))
            if discounted != score:
                components["soft_risk_adjustment"] = discounted - score
            score = discounted

        return {
            "action_id": candidate.action_id,
            "training_type": candidate.training_type,
            "score": round(score, 4),
            "components": {
                key: round(value, 4) for key, value in components.items()
            },
            "risk_flags": risk_flags,
            "rejected": bool(hard_flags),
            "hard_rejected": bool(hard_flags),
            "soft_risk": bool(soft_flags),
            "hard_flags": hard_flags,
            "soft_flags": soft_flags,
            "source_confidence": source_confidence,
            "evidence": {
                "group_card_friendship_training": group_card_friendship_training[
                    "evidence"
                ],
            },
            "raw_candidate": candidate.to_dict(),
        }

    def _clinic_result_if_indicated(
        self,
        state: TrainingTurnState,
        scored: list[dict[str, Any]],
    ) -> StrategyResult | None:
        if not self.goal.execution.allow_clinic:
            return None

        current_speed = state.current_stats.get("speed")
        target_speed = self.goal.target_stats.get("speed")
        if (
            not isinstance(current_speed, int)
            or isinstance(current_speed, bool)
            or not isinstance(target_speed, int)
            or isinstance(target_speed, bool)
            or current_speed >= target_speed
        ):
            return None

        if condition_allows_clinic(
            state.scenario_state.get("condition_assessment", {})
        ):
            return StrategyResult(
                action="clinic",
                target=None,
                confidence=1.0,
                reason=(
                    "Clinic: explicit condition chubby_tendency from condition probe "
                    f"blocks speed training gain while speed {current_speed} is below "
                    f"target {target_speed}"
                ),
                risk_flags=[
                    "clinic_explicit_condition_source",
                    "clinic_explicit_chubby_tendency",
                    "clinic_explicit_speed_gain_blocked",
                    "clinic_speed_below_target",
                ],
                scored_candidates=sorted(
                    scored, key=lambda item: (-item["score"], item["action_id"])
                ),
            )

        return None

    def _recovery_result_if_available(
        self,
        state: TrainingTurnState,
        scored: list[dict[str, Any]],
    ) -> StrategyResult | None:
        recovery_reason = self._rest_recovery_reason(state, scored)
        if recovery_reason is None:
            return None

        risk_flags = [recovery_reason, "no_eligible_training_candidate"]
        rest_candidate = {
            "action_id": "recover:rest",
            "action": "rest",
            "target": None,
            "score": 0.0,
            "components": {
                "vitality_recovery_value": (
                    round(max(0.0, 40.0 - float(state.vitality)), 4)
                    if state.vitality is not None
                    else 0.0
                ),
                "failed_training_avoidance_value": 20.0,
            },
            "risk_flags": [recovery_reason],
            "rejected": False,
            "source_confidence": 1.0,
            "raw_candidate": {
                "action": "rest",
                "reason": recovery_reason,
            },
        }
        candidates = sorted(scored + [rest_candidate], key=lambda item: item["action_id"])
        return StrategyResult(
            action="rest",
            target=None,
            confidence=1.0,
            reason="No eligible training candidate; rest to recover",
            risk_flags=risk_flags,
            scored_candidates=candidates,
        )

    def _rest_recovery_reason(
        self, state: TrainingTurnState, scored: list[dict[str, Any]]
    ) -> str | None:
        if state.vitality is not None:
            if state.vitality < 20:
                return "low_vitality_recovery"
            if state.vitality < 35 and self._has_poor_mood(state):
                return "low_vitality_bad_mood_recovery"

        if self._all_known_training_tabs_are_too_risky(scored):
            return "unsafe_failure_rate_recovery"

        if state.vitality is not None:
            if state.vitality < 40:
                if (
                    self._known_training_tabs_are_safe_now(scored)
                    and self._best_eligible_training_score(scored) > 0
                ):
                    return None
                return "low_vitality_recovery"
            if state.vitality < 55 and self._best_eligible_training_score(scored) <= 0:
                return "weak_training_value_recovery"
            return None

        if self._best_eligible_training_score(scored) <= 0:
            return "weak_training_value_recovery"
        if self._all_scored_candidates_are_too_risky(scored):
            return "unsafe_failure_rate_recovery"
        return None

    def _has_poor_mood(self, state: TrainingTurnState) -> bool:
        mood = state.scenario_state.get("mood")
        if mood is None:
            return False
        return str(mood).strip().lower() in self.POOR_MOODS

    def _recovery_overrides_training(self, recovery: StrategyResult) -> bool:
        hard_reasons = {
            "low_vitality_recovery",
            "low_vitality_bad_mood_recovery",
            "unsafe_failure_rate_recovery",
        }
        return any(reason in hard_reasons for reason in recovery.risk_flags)

    @staticmethod
    def _best_eligible_training_score(scored: list[dict[str, Any]]) -> float:
        scores = [
            float(item.get("score", 0.0) or 0.0)
            for item in scored
            if item.get("training_type") in TRAINING_TYPES and not is_hard_rejected(item)
        ]
        if not scores:
            return float("-inf")
        return max(scores)

    def _known_training_tabs_are_safe_now(self, scored: list[dict[str, Any]]) -> bool:
        known_failure_tabs = [
            item
            for item in scored
            if item.get("training_type") in TRAINING_TYPES
            and item.get("raw_candidate", {}).get("failure_rate") is not None
        ]
        if len(known_failure_tabs) < 3:
            return False
        safe_limit = min(5, self.FAILURE_LIMITS[self.goal.risk_tolerance])
        return all(
            float(item.get("raw_candidate", {}).get("failure_rate", safe_limit + 1))
            <= safe_limit
            for item in known_failure_tabs
        )

    def _all_known_training_tabs_are_too_risky(
        self, scored: list[dict[str, Any]]
    ) -> bool:
        failure_flag = f"failure_rate_exceeds_{self.goal.risk_tolerance}_risk"
        known_failure_tabs = [
            item
            for item in scored
            if item.get("training_type") in TRAINING_TYPES
            and item.get("raw_candidate", {}).get("failure_rate") is not None
        ]
        if len(known_failure_tabs) < 3:
            return False
        return all(failure_flag in item["risk_flags"] for item in known_failure_tabs)

    def _all_scored_candidates_are_too_risky(
        self, scored: list[dict[str, Any]]
    ) -> bool:
        failure_flag = f"failure_rate_exceeds_{self.goal.risk_tolerance}_risk"
        return bool(scored) and all(failure_flag in item["risk_flags"] for item in scored)

    def _phase(self, turn: int | None) -> str:
        if turn is None:
            return "unknown"
        if turn <= 24:
            return "early"
        if turn <= 48:
            return "mid"
        return "late"

    def _immediate_gain_value(self, candidate: TrainingCandidate) -> float:
        return float(sum(self._reliable_stats_gains(candidate).values())) * 1.4

    def _target_gap_value(
        self, state: TrainingTurnState, candidate: TrainingCandidate
    ) -> float:
        value = 0.0
        for stat, gain in self._reliable_stats_gains(candidate).items():
            if stat == "skill_pt":
                continue
            target = self.goal.target_stats.get(stat)
            current = state.current_stats.get(stat)
            if target is None or current is None:
                continue
            gap_ratio = max(0.0, (target - current) / max(target, 1))
            over_target = current >= target
            value += gain * (2.2 * gap_ratio)
            if over_target:
                value -= gain * 1.2
        return value

    def _fallback_stat_priority_value(
        self, state: TrainingTurnState, candidate: TrainingCandidate
    ) -> float:
        """Small fallback when stat gain OCR is unavailable.

        This is deliberately weaker than real gain scoring. It prevents a
        high-vitality turn with missing OCR from being decided only by parser
        confidence or failure rate.
        """
        if self._stats_gain_reliable(candidate):
            return 0.0
        stat = candidate.training_type
        if stat not in STAT_KEYS or stat == "skill_pt":
            return 0.0
        target = self.goal.target_stats.get(stat)
        current = state.current_stats.get(stat)
        if target is None or current is None or target <= 0:
            return 0.0
        gap_ratio = max(0.0, (target - current) / target)
        value = gap_ratio * 10.0
        if stat in self.goal.factor_priority:
            value += 4.0
        if self._phase(state.turn) == "late" and current < target:
            value *= 1.15
        return min(14.0, value) * self._unreliable_gain_confidence(candidate)

    def _survival_floor_value(
        self, state: TrainingTurnState, candidate: TrainingCandidate
    ) -> float:
        if candidate.training_type not in TRAINING_TYPES:
            return 0.0
        floors = self._survival_floors_for_state(state)
        floor = floors.get(candidate.training_type)
        current = state.current_stats.get(candidate.training_type)
        if floor is None or current is None or floor <= 0 or current >= floor:
            return 0.0

        deficit_ratios: dict[str, float] = {}
        for stat, stat_floor in floors.items():
            stat_current = state.current_stats.get(stat)
            if stat_current is None or stat_floor <= 0 or stat_current >= stat_floor:
                continue
            deficit_ratios[stat] = (stat_floor - stat_current) / stat_floor
        if not deficit_ratios:
            return 0.0

        ratio = deficit_ratios.get(candidate.training_type, 0.0)
        if ratio <= 0:
            return 0.0
        top_ratio = max(deficit_ratios.values())
        value = ratio * 45.0
        if ratio >= top_ratio - 0.015:
            value += 18.0
        if self._is_goal_race_preparation_window(state):
            value *= 1.2
        return min(40.0, value)

    def _survival_floors_for_state(
        self, state: TrainingTurnState
    ) -> dict[str, int]:
        phase = self._phase(state.turn)
        floors = dict(self.SURVIVAL_FLOORS.get(phase, self.SURVIVAL_FLOORS["mid"]))
        period_text = str(state.scenario_state.get("period_text", "") or "")
        if period_text.startswith("classic_year_") and any(
            token in period_text for token in ("09_", "10_", "11_", "12_")
        ):
            floors.update(self.SURVIVAL_FLOORS["mid"])
        return floors

    def _is_goal_race_preparation_window(self, state: TrainingTurnState) -> bool:
        period_text = str(state.scenario_state.get("period_text", "") or "")
        if any(token in period_text for token in ("11_", "12_")):
            return True
        return state.turn is not None and 36 <= state.turn <= 48

    def _support_value(self, candidate: TrainingCandidate) -> float:
        effective_count = self._effective_support_count(candidate)
        if effective_count is not None:
            return min(24.0, float(effective_count) * 6.0)
        return 0.0

    def _group_card_friendship_training_evidence(
        self,
        state: TrainingTurnState,
        candidate: TrainingCandidate,
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "eligible": False,
            "condition_enabled": False,
            "support_slot_index": None,
            "identity_status": None,
            "is_effective_support": None,
            "reason": "condition_not_explicitly_enabled",
        }
        if not self._has_explicit_passion_zone_condition(state):
            return {"value": 0.0, "evidence": evidence, "risk_flags": []}

        evidence["condition_enabled"] = True
        support_slots = candidate.raw_data.get("support_slots")
        if not isinstance(support_slots, list):
            evidence["reason"] = "matching_group_card_not_on_training"
            return {"value": 0.0, "evidence": evidence, "risk_flags": []}

        identity_unverified = False
        matched_slot_evidence: dict[str, Any] | None = None
        for index, slot in enumerate(support_slots):
            if not isinstance(slot, dict):
                continue
            if slot.get("matched_card_id") != self.PASSION_ZONE_GROUP_CARD_ID:
                continue

            slot_evidence = {
                "support_slot_index": index,
                "identity_status": (
                    slot.get("identity_status")
                    if isinstance(slot.get("identity_status"), str)
                    else None
                ),
                "is_effective_support": (
                    slot.get("is_effective_support")
                    if isinstance(slot.get("is_effective_support"), bool)
                    else None
                ),
            }
            matched_slot_evidence = slot_evidence
            if (
                slot_evidence["identity_status"] in {"known", "verified"}
                and slot.get("is_effective_support") is True
            ):
                evidence.update(slot_evidence)
                evidence["eligible"] = True
                evidence["reason"] = "qualified_group_card_support_slot"
                return {
                    "value": self.GROUP_CARD_FRIENDSHIP_TRAINING_VALUE,
                    "evidence": evidence,
                    "risk_flags": [],
                }
            if slot_evidence["identity_status"] not in {"known", "verified"}:
                identity_unverified = True

        if matched_slot_evidence is not None:
            evidence.update(matched_slot_evidence)
            if identity_unverified:
                evidence["reason"] = "group_card_identity_unverified"
                return {
                    "value": 0.0,
                    "evidence": evidence,
                    "risk_flags": [
                        "group_card_friendship_support_identity_unverified"
                    ],
                }
            evidence["reason"] = "group_card_not_effective_support"
            return {"value": 0.0, "evidence": evidence, "risk_flags": []}

        evidence["reason"] = "matching_group_card_not_on_training"
        return {"value": 0.0, "evidence": evidence, "risk_flags": []}

    def _has_explicit_passion_zone_condition(self, state: TrainingTurnState) -> bool:
        scenario_state = state.scenario_state
        metadata = scenario_state.get("condition_context")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("source"), dict):
            return False
        conditions = scenario_state.get("conditions")
        if not isinstance(conditions, list):
            return False
        return any(
            isinstance(condition, dict)
            and condition.get("condition_id") == self.PASSION_ZONE_CONDITION_ID
            and isinstance(condition.get("effects"), dict)
            and condition["effects"].get(
                "group_card_friendship_training_enabled"
            )
            is True
            for condition in conditions
        )

    def _friendship_value(
        self,
        state: TrainingTurnState,
        candidate: TrainingCandidate,
        primary_stat_gain_blocked: bool = False,
    ) -> float:
        if primary_stat_gain_blocked or not candidate.friendship:
            return 0.0
        phase = self._phase(state.turn)
        base = 28.0
        if phase == "early":
            base = 22.0
        elif phase == "late":
            base = 34.0
        if candidate.training_type != "wisdom":
            base += 8.0
        return base

    def _hint_and_skill_value(self, candidate: TrainingCandidate) -> float:
        hint_value = 0.0
        if (
            candidate.hint_count is not None
            and 0 <= candidate.hint_count <= 6
            and not self._has_unknown_field(
                candidate,
                {"hint_detection_unreliable", "hint_count_unreliable"},
            )
        ):
            hint_value = float(candidate.hint_count) * 5.0
        skill_value = float(self._reliable_stats_gains(candidate).get("skill_pt", 0)) * 1.2
        return hint_value + skill_value

    def _fragment_value(
        self, state: TrainingTurnState, candidate: TrainingCandidate
    ) -> float:
        if candidate.fragment_available is False:
            return 0.0
        if candidate.fragment_state in {"full", "effect_active"}:
            return 0.0
        fragment_count = int(state.scenario_state.get("fragment_count", 0) or 0)
        if fragment_count >= 8:
            return 0.0
        fragments = self._candidate_fragments(candidate)
        if not candidate.fragment_color and not fragments:
            return 0.0
        phase = self._phase(state.turn)
        base = 3.0 if phase == "early" else 7.0
        candidate_fragment_count = max(
            [int(fragment.get("count", 1) or 1) for fragment in fragments] or [1]
        )
        if candidate_fragment_count > 1:
            base += float(candidate_fragment_count - 1) * 3.0
        if fragment_count in {0, 4, 7}:
            base += 4.0
        if candidate.friendship and candidate.training_type != "wisdom":
            base += 4.0
        return base

    @staticmethod
    def _candidate_fragments(candidate: TrainingCandidate) -> list[dict[str, Any]]:
        if candidate.fragments:
            return candidate.fragments
        grand_masters = candidate.scenario_mechanics.get("grand_masters")
        if isinstance(grand_masters, dict) and isinstance(grand_masters.get("fragments"), list):
            return [
                item
                for item in grand_masters["fragments"]
                if isinstance(item, dict)
            ]
        return []

    def _vitality_penalty(
        self, state: TrainingTurnState, candidate: TrainingCandidate
    ) -> float:
        if state.vitality is None:
            return 0.0
        if state.vitality < 25:
            return -28.0
        if state.vitality < 40:
            return -10.0
        return 0.0

    def _failure_rate_penalty(self, candidate: TrainingCandidate) -> float:
        if (
            candidate.failure_rate is None
            or self._failure_rate_invalid(candidate)
            or candidate.failure_rate <= 0
        ):
            return 0.0
        return -float(candidate.failure_rate) * 0.8

    @staticmethod
    def _stats_gain_reliable(candidate: TrainingCandidate) -> bool:
        return bool(GrandMastersUGParentStrategy._reliable_stats_gains(candidate))

    @staticmethod
    def _reliable_stats_gains(candidate: TrainingCandidate) -> dict[str, int]:
        """Return strict per-field gains without promoting audit-only totals."""
        unreliable_fields = {
            "local_template_incomplete",
            "local_ocr_value_unreliable",
            "stats_gain_unreliable",
        }
        if GrandMastersUGParentStrategy._has_unknown_field(candidate, unreliable_fields):
            return {}

        gain_breakdown = candidate.raw_data.get("gain_breakdown")
        gain_breakdown = gain_breakdown if isinstance(gain_breakdown, dict) else {}
        reliable_gains: dict[str, int] = {}
        for stat, gain in candidate.stats_gain.items():
            if gain <= 0:
                continue
            field_evidence = gain_breakdown.get(stat)
            if (
                not isinstance(field_evidence, dict)
                or field_evidence.get("reliable") is not True
            ):
                continue
            reliable_gains[stat] = gain
        return reliable_gains

    @staticmethod
    def _primary_stat_gain_blocked(candidate: TrainingCandidate) -> bool:
        stat = candidate.training_type
        direct_gain = candidate.stats_gain.get(stat)
        if (
            isinstance(direct_gain, (int, float))
            and not isinstance(direct_gain, bool)
            and direct_gain <= 0
        ):
            return True

        gain_breakdown = candidate.raw_data.get("gain_breakdown")
        if not isinstance(gain_breakdown, dict):
            return False
        primary_breakdown = gain_breakdown.get(stat)
        if not isinstance(primary_breakdown, dict):
            return False
        total_gain = primary_breakdown.get("total_gain")
        return (
            isinstance(total_gain, (int, float))
            and not isinstance(total_gain, bool)
            and total_gain <= 0
        )

    @staticmethod
    def _unreliable_gain_confidence(candidate: TrainingCandidate) -> float:
        confidence = GrandMastersUGParentStrategy._safe_unit_interval(
            candidate.confidence
        )
        return confidence * 0.5

    @staticmethod
    def _effective_support_count(candidate: TrainingCandidate) -> int | None:
        value = candidate.raw_data.get("effective_support_count")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
            return value
        return None

    def _has_unverified_support_evidence(self, candidate: TrainingCandidate) -> bool:
        return self._effective_support_count(candidate) is None

    @staticmethod
    def _has_unknown_field(
        candidate: TrainingCandidate,
        fields: set[str],
    ) -> bool:
        unknown_fields = candidate.raw_data.get("unknown_fields", [])
        if not isinstance(unknown_fields, list):
            return False
        return any(field in fields for field in unknown_fields)

    @staticmethod
    def _uses_fallback_stat_priority(scored_candidate: dict[str, Any]) -> bool:
        components = scored_candidate.get("components", {})
        if not isinstance(components, dict):
            return False
        fallback = float(components.get("fallback_stat_priority_value", 0.0) or 0.0)
        immediate = float(components.get("immediate_stat_gain_value", 0.0) or 0.0)
        target_gap = float(components.get("target_gap_value", 0.0) or 0.0)
        return fallback > 0.0 and immediate == 0.0 and target_gap == 0.0

    def _uncertainty_penalty(self, candidate: TrainingCandidate) -> float:
        confidence = self._safe_unit_interval(candidate.confidence)
        return -(1.0 - confidence) * 20.0

    @staticmethod
    def _safe_unit_interval(value: Any) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return 0.0
        parsed = float(value)
        if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
            return 0.0
        return parsed

    def _confidence_invalid(self, candidate: TrainingCandidate) -> bool:
        if self._has_unknown_field(candidate, {"confidence_invalid"}):
            return True
        value = candidate.confidence
        return (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        )

    def _failure_rate_invalid(self, candidate: TrainingCandidate) -> bool:
        if self._has_unknown_field(candidate, {"failure_rate_invalid"}):
            return True
        value = candidate.failure_rate
        if value is None:
            return False
        return (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 100.0
        )

    def _confidence(
        self, best_score: float, second_score: float, source_confidence: float
    ) -> float:
        source_confidence = self._safe_unit_interval(source_confidence)
        if best_score <= 0:
            return 0.2
        if second_score <= 0:
            margin_factor = 0.2
        else:
            margin_factor = min(
                0.35, max(0.0, (best_score - second_score) / max(best_score, 1.0))
            )
        return round(
            max(0.0, min(1.0, 0.45 + margin_factor + source_confidence * 0.35)),
            2,
        )
