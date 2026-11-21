"""Same-frame, non-executable runtime projection for event option scoring."""

from __future__ import annotations

from core.auto_trainer.career_supervisor_event_identity_shadow import (
    EventIdentityShadowReport,
)
from core.auto_trainer.career_supervisor_event_local_decision import (
    EventLocalDecisionBinding,
    EventLocalDecisionError,
    EventLocalDecisionPolicy,
    build_event_local_decision_shadow,
)
from core.auto_trainer.career_supervisor_turn import TurnAuthorityBundle


EVENT_LOCAL_RUNTIME_SCHEMA = "uma.event_local_decision_runtime_shadow_slot.v1"
EVENT_LOCAL_RUNTIME_POLICY_NAME = "offline_equal_unit_weights_v1"
_OFFLINE_EQUAL_UNIT_POLICY = EventLocalDecisionPolicy(
    weights=(
        ("bond", 1),
        ("energy", 1),
        ("guts", 1),
        ("max_energy", 1),
        ("mood", 1),
        ("power", 1),
        ("skill_points", 1),
        ("speed", 1),
        ("stamina", 1),
        ("wit", 1),
    )
)


def build_event_local_decision_runtime_shadow(
    *,
    authority: TurnAuthorityBundle | None,
    identity_shadow: EventIdentityShadowReport | None,
) -> dict[str, object]:
    """Build a diagnostic-only local event score or a stable abstention."""

    if not isinstance(authority, TurnAuthorityBundle):
        return _abstain("event_local_authority_unavailable")
    if not isinstance(identity_shadow, EventIdentityShadowReport):
        return _abstain("event_local_identity_unavailable")
    if identity_shadow.status != "available":
        reason = next(
            iter(identity_shadow.abstain_reasons),
            "event_local_identity_unavailable",
        )
        return _abstain(reason)

    identity = identity_shadow.identity
    audit = identity_shadow.audit
    summary = identity_shadow.summary
    if (
        identity is None
        or audit is None
        or summary is None
        or identity.status != "identified"
        or audit.match_status != "matched"
        or audit.matched_event_id is None
        or summary.branch_complete is not True
    ):
        return _abstain("event_local_identity_incomplete")
    if not identity_shadow.effect_candidates:
        return _abstain("event_local_candidates_incomplete")
    if not _binding_matches(
        authority=authority,
        identity_shadow=identity_shadow,
    ):
        return _abstain("event_local_binding_mismatch")

    try:
        decision = build_event_local_decision_shadow(
            binding=EventLocalDecisionBinding(
                authority_bundle_sha256=authority.bundle_sha256,
                source_frame_sha256=identity_shadow.source_frame_sha256,
                event_page_sha256=identity_shadow.event_page_sha256,
                dataset_sha256=summary.dataset_sha256,
            ),
            candidates=identity_shadow.effect_candidates,
            option_count=summary.option_count,
            policy=_OFFLINE_EQUAL_UNIT_POLICY,
        )
    except (EventLocalDecisionError, ValueError):
        return _abstain("event_local_contract_error")
    if decision.status != "available":
        return _abstain(
            decision.fallback_reason or "event_local_decision_unavailable"
        )
    return {
        **_base_slot(),
        "status": "available",
        "abstain_reason": None,
        "decision": decision.to_dict(),
    }


def _binding_matches(
    *,
    authority: TurnAuthorityBundle,
    identity_shadow: EventIdentityShadowReport,
) -> bool:
    identity = identity_shadow.identity
    audit = identity_shadow.audit
    summary = identity_shadow.summary
    if identity is None or audit is None or summary is None:
        return False
    return (
        identity_shadow.source_frame_sha256 == authority.source_frame_sha256
        and identity_shadow.event_page_sha256 == authority.source_frame_sha256
        and identity.authority_bundle_sha256 == authority.bundle_sha256
        and identity.run_session_id == authority.run_session_id
        and identity.generation == authority.generation
        and identity.source_frame_sha256 == identity_shadow.source_frame_sha256
        and identity.event_page_sha256 == identity_shadow.event_page_sha256
        and identity.dataset_sha256 == summary.dataset_sha256
        and audit.authority_bundle_sha256 == authority.bundle_sha256
        and audit.run_session_id == authority.run_session_id
        and audit.generation == authority.generation
        and audit.source_frame_sha256 == identity_shadow.source_frame_sha256
        and audit.event_page_sha256 == identity_shadow.event_page_sha256
        and audit.dataset_sha256 == summary.dataset_sha256
        and audit.matched_event_id == summary.event_id
        and all(
            candidate.dataset_sha256 == summary.dataset_sha256
            and candidate.event_id == summary.event_id
            for candidate in identity_shadow.effect_candidates
        )
    )


def _base_slot() -> dict[str, object]:
    return {
        "schema_version": EVENT_LOCAL_RUNTIME_SCHEMA,
        "policy_name": EVENT_LOCAL_RUNTIME_POLICY_NAME,
        "action_eligible": False,
        "usable_for_action": False,
        "execution_allowed": False,
    }


def _abstain(reason: str) -> dict[str, object]:
    return {
        **_base_slot(),
        "status": "abstain",
        "abstain_reason": reason,
    }
