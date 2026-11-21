"""Single resolver for real-device rainbow (friendship-training) evidence.

Real CN captures never report ``training.friendship is True``: across six 2.8.x careers 1,325 training
pages were False (635) or None (690); 229 of the None pages carry the strict eligible-partner evidence
already used by forecast_metadata / stamina_lesson_quality / recovery timing (same-type support with a
max/orange bond in that training). Call sites that read ``candidate.friendship is True`` directly treat
all of those as non-rainbow. This module gives them the same evidence rule, behind an explicit switch.

The upgrade is evidence, not an observation: callers keep their own labels/basis strings.
"""
from __future__ import annotations

from typing import Any


def effective_friendship(candidate: Any) -> bool | None:
    """True/False when observed; True for eligible-partner evidence; otherwise None (unknown)."""
    explicit = getattr(candidate, "friendship", None)
    if explicit is not None:
        return bool(explicit)
    try:
        from core.auto_trainer.larc_recovery_timing import _friendship_evidence

        evidence = _friendship_evidence(candidate)
    except Exception:  # missing participant view or malformed rows stay unknown
        return None
    return True if evidence == "eligible_partner_observed" else None


def friendship_basis(candidate: Any) -> str:
    explicit = getattr(candidate, "friendship", None)
    if explicit is not None:
        return "observed"
    return "eligible_partner_estimate" if effective_friendship(candidate) else "unknown"


def gates_enabled(goal: Any) -> bool:
    if goal is None:
        return False
    try:
        from core.auto_trainer.larc_strategy_extension import active_provider
        from core.auto_trainer.larc_strategy_params import params_for_goal

        return active_provider().friendship_evidence_gates_enabled(params_for_goal(goal))
    except Exception:
        return False


def friendship_for_gate(candidate: Any, goal: Any) -> bool | None:
    """Raw field unless the goal enables evidence-aware rainbow gates."""
    if gates_enabled(goal):
        return effective_friendship(candidate)
    return getattr(candidate, "friendship", None)
