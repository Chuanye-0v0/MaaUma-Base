from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.auto_trainer.terminal_skill_handoff import (
    TerminalSkillHandoffError,
    load_terminal_skill_handoff_artifact,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TYPE = "account_cycle_autonomous_training.v1"
_DEVICE_ENDPOINT = "127.0.0.1:16448"
_ALLOWED_EXTERNAL_ROOT = Path(
    r"E:\uma\.worktrees\account-cycle-card-desktop-e\runs\skill_takeover_20260914"
)
_ARTIFACT_NAMES = {
    "finish_ready_observation",
    "reopen_receipt",
    "source_report",
    "parent_plan",
    "child_plan",
    "promotion_review",
    "promotion_evidence",
    "selection_checkpoint",
    "selection_ledger",
    "selection_ledger_frame",
    "final_confirmation_evidence",
    "closure_contract",
    "top_frame",
    "current_frame",
    "final_confirmation_intent",
    "final_report",
    "success_report",
    "return_report",
}


class ExternalTerminalSkillHandoffError(ValueError):
    pass


@dataclass(frozen=True)
class ExternalTerminalSkillHandoffBinding:
    run_session_id: str
    sidecar_path: str
    sidecar_sha256: str
    source_type: str
    external_evidence_roots: tuple[str, ...]
    expected_remaining_skill_points: int
    purchased_item_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_terminal_skill_handoff_path": self.sidecar_path,
            "external_terminal_skill_handoff_sha256": self.sidecar_sha256,
            "external_terminal_skill_source_type": self.source_type,
            "external_terminal_skill_evidence_roots": list(
                self.external_evidence_roots
            ),
            "external_terminal_skill_expected_remaining_points": (
                self.expected_remaining_skill_points
            ),
            "external_terminal_skill_purchased_item_ids": list(
                self.purchased_item_ids
            ),
        }


def load_external_terminal_skill_handoff(
    sidecar_path: Path,
    *,
    expected_sidecar_sha256: str,
    allowed_output_root: Path,
    expected_run_session_id: str,
    consumed_terminal_handoff_path: Path,
    expected_consumed_terminal_handoff_sha256: str,
    allowed_external_root: Path = _ALLOWED_EXTERNAL_ROOT,
) -> ExternalTerminalSkillHandoffBinding:
    output_root = allowed_output_root.resolve(strict=True)
    sidecar = _resolve_file_inside(
        sidecar_path,
        output_root,
        "external_terminal_skill_sidecar_outside_output_root",
    )
    _require_sha(sidecar, expected_sidecar_sha256, "external_terminal_skill_sidecar_sha_mismatch")
    payload = _read_json(sidecar, "external_terminal_skill_sidecar_invalid")
    required = {
        "schema_version",
        "source_type",
        "run_session_id",
        "device_endpoint",
        "external_evidence_roots",
        "consumed_terminal_handoff",
        "artifacts",
        "expected_remaining_skill_points",
        "purchased_item_ids",
    }
    if set(payload) != required:
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_sidecar_schema_invalid")
    if (
        payload.get("schema_version") != "uma.external_terminal_skill_handoff.v1"
        or payload.get("source_type") != _SOURCE_TYPE
        or payload.get("run_session_id") != expected_run_session_id
        or payload.get("device_endpoint") != _DEVICE_ENDPOINT
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_sidecar_identity_invalid")

    handoff_ref = payload.get("consumed_terminal_handoff")
    if not _is_ref(handoff_ref):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_handoff_reference_invalid")
    handoff_path = _resolve_file_inside(
        Path(handoff_ref["path"]), output_root,
        "external_terminal_skill_handoff_outside_output_root",
    )
    expected_handoff = consumed_terminal_handoff_path.resolve(strict=True)
    if (
        handoff_path != expected_handoff
        or handoff_ref["sha256"] != expected_consumed_terminal_handoff_sha256
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_handoff_mismatch")
    load_terminal_skill_handoff_artifact(
        handoff_path,
        expected_artifact_sha256=expected_consumed_terminal_handoff_sha256,
        allowed_root=output_root,
    )

    external_base = allowed_external_root.resolve(strict=True)
    raw_roots = payload.get("external_evidence_roots")
    if not isinstance(raw_roots, list) or not 1 <= len(raw_roots) <= 4:
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_evidence_roots_invalid")
    external_roots: list[Path] = []
    for raw_root in raw_roots:
        if not isinstance(raw_root, str) or not raw_root:
            raise ExternalTerminalSkillHandoffError("external_terminal_skill_evidence_roots_invalid")
        root = Path(raw_root).resolve(strict=True)
        try:
            relative_root = root.relative_to(external_base)
        except ValueError as exc:
            raise ExternalTerminalSkillHandoffError(
                "external_terminal_skill_evidence_root_outside_allowlist"
            ) from exc
        if not relative_root.parts or not root.is_dir() or root in external_roots:
            raise ExternalTerminalSkillHandoffError(
                "external_terminal_skill_evidence_root_not_specific"
            )
        external_roots.append(root)

    refs = payload.get("artifacts")
    if not isinstance(refs, dict) or set(refs) != _ARTIFACT_NAMES:
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_artifacts_invalid")
    paths: dict[str, Path] = {}
    values: dict[str, dict[str, Any]] = {}
    for name, ref in refs.items():
        if not _is_ref(ref):
            raise ExternalTerminalSkillHandoffError(
                f"external_terminal_skill_artifact_reference_invalid:{name}"
            )
        path = Path(ref["path"]).resolve(strict=True)
        if not path.is_file() or not any(_is_inside(path, root) for root in external_roots):
            raise ExternalTerminalSkillHandoffError(
                f"external_terminal_skill_artifact_outside_root:{name}"
            )
        _require_sha(path, ref["sha256"], f"external_terminal_skill_artifact_sha_mismatch:{name}")
        paths[name] = path
        if path.suffix.lower() == ".json":
            values[name] = _read_json(
                path, f"external_terminal_skill_artifact_invalid:{name}"
            )

    expected_remaining, item_ids = _validate_native_chain(
        paths, values, external_roots=tuple(external_roots)
    )
    if (
        payload.get("expected_remaining_skill_points") != expected_remaining
        or payload.get("purchased_item_ids") != list(item_ids)
    ):
        raise ExternalTerminalSkillHandoffError(
            "external_terminal_skill_summary_mismatch"
        )
    return ExternalTerminalSkillHandoffBinding(
        run_session_id=expected_run_session_id,
        sidecar_path=str(sidecar),
        sidecar_sha256=expected_sidecar_sha256,
        source_type=_SOURCE_TYPE,
        external_evidence_roots=tuple(str(root) for root in external_roots),
        expected_remaining_skill_points=expected_remaining,
        purchased_item_ids=item_ids,
    )


def _validate_native_chain(
    paths: Mapping[str, Path],
    values: Mapping[str, Mapping[str, Any]],
    *,
    external_roots: tuple[Path, ...],
) -> tuple[int, tuple[str, ...]]:
    plan = values.get("child_plan", {})
    parent = values.get("parent_plan", {})
    review = values.get("promotion_review", {})
    evidence = values.get("promotion_evidence", {})
    final_evidence = values.get("final_confirmation_evidence", {})
    contract = values.get("closure_contract", {})
    finish_ready = values.get("finish_ready_observation", {})
    reopen = values.get("reopen_receipt", {})
    final_intent = values.get("final_confirmation_intent", {})
    final = values.get("final_report", {})
    success = values.get("success_report", {})
    returned = values.get("return_report", {})
    purchases = plan.get("purchases")
    plan_input = plan.get("input")
    policy = plan.get("policy")
    authorization = plan.get("final_authorization")
    if (
        plan.get("schema_version") != "uma.skill_purchase_plan.v1"
        or plan.get("mode") != "live"
        or plan.get("execution_allowed") is not True
        or plan.get("multi_skill_live_gate") != "current_run_confirmation_closed"
        or not isinstance(purchases, list)
        or not 1 <= len(purchases) <= 6
        or not isinstance(plan_input, Mapping)
        or not isinstance(policy, Mapping)
        or policy.get("currency_scope") != "skill_points_only"
        or policy.get("dangerous_resource_flags") != []
        or not isinstance(authorization, Mapping)
        or authorization.get("schema_version") != "uma.skill_final_authorization.v1"
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_plan_invalid")
    initial = plan_input.get("current_skill_points")
    spent = plan.get("spent_skill_points")
    remaining = plan.get("expected_remaining_skill_points")
    ids: list[str] = []
    costs: list[int] = []
    cumulative = 0
    for index, purchase in enumerate(purchases, 1):
        if not isinstance(purchase, Mapping):
            raise ExternalTerminalSkillHandoffError("external_terminal_skill_purchase_invalid")
        item_id = purchase.get("item_id")
        cost = purchase.get("observed_final_cost")
        if (
            not isinstance(item_id, str) or not item_id
            or type(cost) is not int or cost <= 0
            or purchase.get("order") != index
        ):
            raise ExternalTerminalSkillHandoffError("external_terminal_skill_purchase_invalid")
        cumulative += cost
        if (
            purchase.get("expected_selected_total_after") != cumulative
            or type(initial) is not int
            or purchase.get("expected_skill_points_after") != initial - cumulative
        ):
            raise ExternalTerminalSkillHandoffError("external_terminal_skill_economics_invalid")
        ids.append(item_id)
        costs.append(cost)
    if (
        len(ids) != len(set(ids))
        or type(initial) is not int or initial < 0
        or type(spent) is not int or spent != sum(costs)
        or type(remaining) is not int or remaining < 0 or initial - spent != remaining
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_economics_invalid")

    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    if (
        plan_input.get("source_report_sha256") != hashes["source_report"]
        or review.get("source_report_sha256") != hashes["source_report"]
        or evidence.get("source_report_sha256") != hashes["source_report"]
        or authorization.get("parent_plan_sha256") != hashes["parent_plan"]
        or authorization.get("final_evidence_sha256") != hashes["final_confirmation_evidence"]
        or final_evidence.get("parent_plan_sha256") != hashes["parent_plan"]
        or final_evidence.get("promotion_review_sha256") != hashes["promotion_review"]
        or final_evidence.get("promotion_evidence_sha256") != hashes["promotion_evidence"]
        or final_evidence.get("selection_checkpoint_sha256") != hashes["selection_checkpoint"]
        or final_evidence.get("selection_ledger_sha256") != hashes["selection_ledger"]
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_native_binding_invalid")
    if (
        contract.get("schema_version") != "uma.skill_multi_page_confirmation_contract.v1"
        or contract.get("final_confirmation_allowed") is not True
        or contract.get("plan_sha256") != hashes["child_plan"]
        or contract.get("top_frame_sha256") != hashes["top_frame"]
        or contract.get("selection_checkpoint_sha256") != hashes["selection_checkpoint"]
        or contract.get("expected_remaining_skill_points") != remaining
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_closure_invalid")
    if (
        finish_ready.get("page") != "finish_ready"
        or finish_ready.get("endpoint") != _DEVICE_ENDPOINT
        or finish_ready.get("current_skill_points") != remaining
        or reopen.get("action_id") != "open_skills"
        or reopen.get("performed") is not True
        or reopen.get("endpoint") != _DEVICE_ENDPOINT
        or not isinstance(reopen.get("observation"), Mapping)
        or reopen["observation"].get("page") != "skills"
        or reopen["observation"].get("endpoint") != _DEVICE_ENDPOINT
        or reopen["observation"].get("current_skill_points") != remaining
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_intent_invalid")
    if (
        final_intent.get("schema_version") != "uma.skill_batch_final_confirmation_session.v1"
        or final_intent.get("status") != "consumed_before_device_connect"
        or final_intent.get("action_id") != "skill_purchase.final_confirm"
        or final_intent.get("plan_id") != plan.get("plan_id")
        or final_intent.get("plan_sha256") != hashes["child_plan"]
        or final_intent.get("closure_contract_sha256") != hashes["closure_contract"]
        or final_intent.get("top_frame_sha256") != hashes["top_frame"]
        or final_intent.get("selection_checkpoint_sha256") != hashes["selection_checkpoint"]
        or final_intent.get("expected_remaining_skill_points") != remaining
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_final_intent_invalid")

    multi = len(ids) > 1
    if (
        final.get("schema_version") != "uma.autonomous_final_skill_confirmation.v1"
        or final.get("mode") != "live" or final.get("execute") is not True
        or final.get("stop_reason") != ("batch_final_confirmation_probe_complete" if multi else "final_confirmation_probe_complete")
        or final.get("final_confirmation_performed") is not True
        or final.get("actions") != ["skill_purchase.final_confirm"]
        or final.get("device_action_count") != 1
        or final.get("plan_id") != plan.get("plan_id")
        or Path(str(final.get("promotion_plan_path", ""))).resolve() != paths["child_plan"]
        or final.get("promotion_plan_sha256") != hashes["child_plan"]
        or final.get("closure_contract_sha256") != hashes["closure_contract"]
        or final.get("live_session_intent_sha256") != hashes["final_confirmation_intent"]
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_final_report_invalid")
    if (
        success.get("schema_version") != "uma.autonomous_skill_success_close.v1"
        or success.get("mode") != "live" or success.get("execute") is not True
        or success.get("stop_reason") != ("batch_close_success_probe_complete" if multi else "close_success_probe_complete")
        or success.get("close_success_performed") is not True
        or success.get("actions") != ["skill_purchase.close_success"]
        or success.get("device_action_count") != 1
        or success.get("plan_id") != plan.get("plan_id")
        or success.get("child_plan_sha256") != hashes["child_plan"]
        or success.get("final_confirmation_report_sha256") != hashes["final_report"]
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_success_report_invalid")
    if (
        returned.get("schema_version") != "uma.autonomous_return_after_learning.v1"
        or returned.get("mode") != "live" or returned.get("execute") is not True
        or returned.get("stop_reason") != ("batch_return_after_purchase_probe_complete" if multi else "return_after_purchase_probe_complete")
        or returned.get("return_performed") is not True
        or returned.get("actions") != ["skill_purchase.return_after_purchase"]
        or returned.get("device_action_count") != 1
        or returned.get("plan_id") != plan.get("plan_id")
        or returned.get("child_plan_sha256") != hashes["child_plan"]
        or returned.get("final_confirmation_report_sha256") != hashes["final_report"]
        or returned.get("success_close_report_sha256") != hashes["success_report"]
        or returned.get("device_endpoint") != _DEVICE_ENDPOINT
        or returned.get("verified_current_skill_points") != remaining
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_return_report_invalid")
    observation = returned.get("pre_return_observation")
    source_sha = returned.get("source_frame_sha256")
    if (
        not isinstance(observation, Mapping)
        or observation.get("current_skill_points") != remaining
        or observation.get("frame_sha256") != source_sha
        or returned.get("verified_skill_points_frame_sha256") != source_sha
        or not _is_sha(source_sha)
        or observation.get("selected_item_ids") != []
        or observation.get("selected_total") != 0
        or observation.get("green_confirm_control") is not None
    ):
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_return_observation_invalid")
    semantics = observation.get("acquired_semantics")
    observed_ids = observation.get("observed_acquired_item_ids")
    if semantics == "fresh_visible_rows":
        valid_acquired = (
            observation.get("purchase_terminal_state") == "acquired"
            and observed_ids == ids
        )
    elif semantics == "purchase_receipts_and_targets_absent":
        valid_acquired = (
            observation.get("verified_return_targets_absent") is True
            and isinstance(observed_ids, list)
        )
    else:
        valid_acquired = False
    if not valid_acquired:
        raise ExternalTerminalSkillHandoffError("external_terminal_skill_acquired_semantics_invalid")
    _validate_report_frames(
        paths,
        values,
        ("final_report", "success_report", "return_report"),
        external_roots=external_roots,
    )
    return remaining, tuple(ids)


def _validate_report_frames(
    paths: Mapping[str, Path],
    values: Mapping[str, Mapping[str, Any]],
    names: tuple[str, ...],
    *,
    external_roots: tuple[Path, ...],
) -> None:
    # Each frame is independently constrained by the artifact root through its
    # report path's common external evidence tree, then source/post hashes are
    # required to name actual captured bytes when supplied.
    for name in names:
        report = values[name]
        frames = report.get("captured_frames")
        if not isinstance(frames, list) or len(frames) < 2:
            raise ExternalTerminalSkillHandoffError(f"external_terminal_skill_frames_invalid:{name}")
        resolved: list[Path] = []
        for raw in frames:
            if not isinstance(raw, str) or not raw:
                raise ExternalTerminalSkillHandoffError(f"external_terminal_skill_frames_invalid:{name}")
            path = Path(raw).resolve(strict=True)
            if not any(_is_inside(path, root) for root in external_roots):
                raise ExternalTerminalSkillHandoffError(
                    f"external_terminal_skill_frame_outside_root:{name}"
                )
            resolved.append(path)
        frame_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in resolved]
        for key in ("source_frame_sha256", "post_frame_sha256"):
            value = report.get(key)
            if value is not None and value not in frame_hashes:
                raise ExternalTerminalSkillHandoffError(
                    f"external_terminal_skill_frame_sha_mismatch:{name}"
                )


def _resolve_file_inside(path: Path, root: Path, code: str) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ExternalTerminalSkillHandoffError(code) from exc
    if not resolved.is_file():
        raise ExternalTerminalSkillHandoffError(code)
    return resolved


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalTerminalSkillHandoffError(code) from exc
    if not isinstance(value, dict):
        raise ExternalTerminalSkillHandoffError(code)
    return value


def _require_sha(path: Path, expected: Any, code: str) -> None:
    if not _is_sha(expected) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ExternalTerminalSkillHandoffError(code)


def _is_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"path", "sha256"}
        and isinstance(value.get("path"), str)
        and bool(value["path"])
        and _is_sha(value.get("sha256"))
    )


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


__all__ = [
    "ExternalTerminalSkillHandoffBinding",
    "ExternalTerminalSkillHandoffError",
    "load_external_terminal_skill_handoff",
]
