from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from core.skill_batch_purchase import (
    validate_skill_batch_plan,
    validate_skill_inventory_packet,
)
from core.skill_purchase_plan_promotion import validate_local_auto_skill_batch_inputs
from core.skill_batch_completion import load_batch_final_evidence


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROOT_NAME = "terminal_skill_lifecycle.json"
_ROOT_SCHEMA = "uma.terminal_skill_lifecycle.v1"
_RECEIPT_SCHEMA = "uma.terminal_skill_lifecycle_receipt.v1"
_IMPLEMENTED_PHASES = (
    "terminal_handoff",
    "capture_plan",
    "plan_promotion",
    "selection",
    "final_confirmation",
    "close_success",
    "return_after_purchase",
    "supervisor_resume",
)
_SAFE_RESOURCE_CHECKS = {
    "uses_skill_points_only": True,
    "uses_carrots": False,
    "uses_clocks": False,
    "uses_paid_currency": False,
}
_ATOMIC_TERMINAL_PHASES = {
    "final_confirmation": {
        "stage": "final-confirmation-probe",
        "action_id": "skill_purchase.final_confirm",
        "stop_reason": "final_confirmation_probe_complete",
    },
    "close_success": {
        "stage": "close-success-probe",
        "action_id": "skill_purchase.close_success",
        "stop_reason": "close_success_probe_complete",
    },
    "return_after_purchase": {
        "stage": "return-after-purchase-probe",
        "action_id": "skill_purchase.return_after_purchase",
        "stop_reason": "return_after_purchase_probe_complete",
    },
}


class TerminalSkillLifecycleError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _producer_canonical_sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_reparse_stat(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


def _reject_existing_reparse_points(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            value = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise TerminalSkillLifecycleError("path_inspection_failed") from exc
        if _is_reparse_stat(value):
            raise TerminalSkillLifecycleError("reparse_point_rejected")


def _require_inside_root(
    path: Path,
    *,
    allowed_run_root: Path,
    code: str,
    allow_missing_tail: bool = False,
) -> Path:
    root_raw = allowed_run_root.absolute()
    _reject_existing_reparse_points(root_raw)
    try:
        root = root_raw.resolve(strict=True)
    except OSError as exc:
        raise TerminalSkillLifecycleError("allowed_run_root_invalid") from exc
    if not root.is_dir():
        raise TerminalSkillLifecycleError("allowed_run_root_invalid")
    candidate_raw = path.absolute()
    _reject_existing_reparse_points(candidate_raw)
    try:
        candidate = candidate_raw.resolve(strict=not allow_missing_tail)
    except OSError as exc:
        raise TerminalSkillLifecycleError(code) from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TerminalSkillLifecycleError(code) from exc
    return candidate


def _require_sha256(value: str, *, code: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise TerminalSkillLifecycleError(code)


def _load_canonical_json(path: Path, *, invalid_code: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalSkillLifecycleError(invalid_code) from exc
    if not isinstance(payload, dict) or _canonical_json_bytes(payload) != raw:
        raise TerminalSkillLifecycleError(invalid_code)
    return payload


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _load_strict_json(path: Path, *, invalid_code: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_bytes().decode("utf-8-sig"), object_pairs_hook=_json_object
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise TerminalSkillLifecycleError(invalid_code) from exc
    if not isinstance(payload, dict):
        raise TerminalSkillLifecycleError(invalid_code)
    return payload


def _validate_handoff(payload: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "phase",
        "handoff_ready",
        "source_image_path",
        "source_image_sha256",
        "purchase_executed",
        "action_eligible",
        "next_required_phase",
    }
    if set(payload) != expected_keys:
        raise TerminalSkillLifecycleError("handoff_report_schema_invalid")
    if (
        payload.get("schema_version") != "uma.terminal_skill_handoff_progress.v1"
        or payload.get("phase") != "terminal_skill"
        or payload.get("handoff_ready") is not True
        or payload.get("purchase_executed") is not False
        or payload.get("action_eligible") is not False
        or payload.get("next_required_phase") != "full_page_capture_required"
        or not isinstance(payload.get("source_image_path"), str)
        or not payload["source_image_path"]
    ):
        raise TerminalSkillLifecycleError("handoff_report_state_invalid")
    _require_sha256(str(payload.get("source_image_sha256")), code="handoff_report_schema_invalid")


def _stored_file(
    value: Any,
    expected_sha: Any,
    *,
    allowed_run_root: Path,
    code: str,
) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise TerminalSkillLifecycleError(code)
    _require_sha256(expected_sha, code=code)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = allowed_run_root.absolute() / candidate
    path = _require_inside_root(candidate, allowed_run_root=allowed_run_root, code=code)
    if not path.is_file() or _sha256_file(path) != expected_sha:
        raise TerminalSkillLifecycleError(code)
    return path


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root.absolute()))


def _promotion_input(
    value: Mapping[str, Any] | None, key: str, *, code: str
) -> Any:
    if value is None or key not in value:
        raise TerminalSkillLifecycleError(code)
    return value[key]


def _validate_plan_promotion(
    purchase_plan: Mapping[str, Any],
    *,
    allowed_run_root: Path,
    state: Mapping[str, Any],
    inputs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    invalid = "plan_promotion_report_invalid"
    source = _stored_file(
        _promotion_input(inputs, "source_report_path", code=invalid),
        _promotion_input(inputs, "expected_source_report_sha256", code=invalid),
        allowed_run_root=allowed_run_root, code=invalid,
    )
    review = _stored_file(
        _promotion_input(inputs, "review_path", code=invalid),
        _promotion_input(inputs, "expected_review_sha256", code=invalid),
        allowed_run_root=allowed_run_root, code=invalid,
    )
    evidence = _stored_file(
        _promotion_input(inputs, "evidence_path", code=invalid),
        _promotion_input(inputs, "expected_evidence_sha256", code=invalid),
        allowed_run_root=allowed_run_root, code=invalid,
    )
    session_id = _promotion_input(inputs, "expected_skill_session_id", code=invalid)
    capture_batch_id = _promotion_input(inputs, "expected_capture_batch_id", code=invalid)
    if not isinstance(session_id, str) or not session_id or not isinstance(capture_batch_id, str) or not capture_batch_id:
        raise TerminalSkillLifecycleError(invalid)
    source_payload = _load_strict_json(source, invalid_code=invalid)
    evidence_payload = _load_strict_json(evidence, invalid_code=invalid)
    verdict = source_payload.get("verdict")
    inventory_packet = source_payload.get("inventory_packet")
    batch_plan = source_payload.get("batch_plan")
    local_auto_plan = evidence_payload.get("local_auto_batch_plan")
    capture_plan_sha = state.get("plan_sha256")
    single_page = isinstance(local_auto_plan, Mapping) and "single_page_confirmation" in local_auto_plan
    if local_auto_plan is not None:
        try:
            validate_local_auto_skill_batch_inputs(
                report=source_payload, build_context=evidence_payload.get("build_context", {}),
                batch_plan=local_auto_plan,
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise TerminalSkillLifecycleError(invalid) from exc
        batch_plan = local_auto_plan
        if single_page:
            from core.skill_batch_purchase import build_skill_batch_plan, select_local_skill_action_ids
            original = build_skill_batch_plan(inventory=inventory_packet,
                selected_action_ids=select_local_skill_action_ids(inventory_packet), decision_source="local_auto",
                skill_relations={item["skill_id"]: item.get("relations", {}).get("lower", []) for item in inventory_packet["candidates"]})
            if original["plan_sha256"] != capture_plan_sha:
                raise TerminalSkillLifecycleError(invalid)
    dry_run = source_payload.get("planner_dry_run")
    if (
        source_payload.get("schema_version")
        != "uma.fresh_skill_acquisition_dry_run.v1"
        or not isinstance(verdict, Mapping)
        or verdict.get("parser_verdict") != "PARSER_READY"
        or verdict.get("planner_verdict") != "PLANNER_READY"
        or verdict.get("capture_binding_accepted") is not True
        or (local_auto_plan is None and source_payload.get("capture_binding") != state.get("capture_binding"))
        or (local_auto_plan is not None and inventory_packet.get("capture_binding") != state.get("capture_binding"))
        or not isinstance(inventory_packet, Mapping)
        or inventory_packet.get("packet_sha256") != state.get("inventory_sha256")
        or not isinstance(batch_plan, Mapping)
        or (not single_page and batch_plan.get("plan_sha256") != capture_plan_sha)
        or batch_plan.get("inventory_packet_sha256") != state.get("inventory_sha256")
        or not isinstance(dry_run, Mapping)
        or dry_run.get("executed") is not True
        or dry_run.get("status") != "ok"
        or dry_run.get("trigger") != "final_build"
        or dry_run.get("risk_flags") != []
        or dry_run.get("resource_checks") != _SAFE_RESOURCE_CHECKS
    ):
        raise TerminalSkillLifecycleError(invalid)
    selected = batch_plan.get("selected_action_ids")
    if not isinstance(selected, list) or not selected or any(not isinstance(item, str) or not item for item in selected) or len(selected) != len(set(selected)):
        raise TerminalSkillLifecycleError(invalid)
    source_sha = _sha256_file(source)
    promotion = purchase_plan.get("execution_promotion")
    purchases = purchase_plan.get("purchases")
    policy = purchase_plan.get("policy")
    input_data = purchase_plan.get("input")
    if (
        purchase_plan.get("schema_version") != "uma.skill_purchase_plan.v1"
        or purchase_plan.get("mode") != "live"
        or purchase_plan.get("execution_allowed") is not True
        or not isinstance(policy, Mapping)
        or policy.get("currency_scope") != "skill_points_only"
        or policy.get("allow_fallback_skill") is not False
        or policy.get("allow_partial_confirmation") is not False
        or policy.get("dangerous_resource_flags") != []
        or not isinstance(input_data, Mapping)
        or input_data.get("source_report_sha256") != source_sha
        or not isinstance(promotion, Mapping)
        or not isinstance(purchases, list)
    ):
        raise TerminalSkillLifecycleError(invalid)
    if local_auto_plan is not None and (
        promotion.get("decision_source") != "local_auto"
        or promotion.get("inventory_packet_sha256") != state.get("inventory_sha256")
        or promotion.get("batch_plan_sha256") != batch_plan.get("plan_sha256")
        or (single_page and promotion.get("confirmation_row_limit") != 5)
    ):
        raise TerminalSkillLifecycleError(invalid)
    plan_id = purchase_plan.get("plan_id")
    if (
        not isinstance(plan_id, str) or not plan_id
        or promotion.get("plan_id") != plan_id
        or promotion.get("session_id") != session_id
        or promotion.get("fresh_capture_id") != capture_batch_id
        or promotion.get("current_run_scope_only") is not True
        or promotion.get("planned_selection_count") != len(purchases)
    ):
        raise TerminalSkillLifecycleError(invalid)
    item_ids = [item.get("item_id") if isinstance(item, Mapping) else None for item in purchases]
    orders = [item.get("order") if isinstance(item, Mapping) else None for item in purchases]
    spends = [item.get("observed_final_cost") if isinstance(item, Mapping) else None for item in purchases]
    initial_points = input_data.get("current_skill_points")
    if (
        item_ids != selected
        or len(item_ids) != len(set(item_ids))
        or orders != list(range(1, len(purchases) + 1))
        or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in spends)
        or not isinstance(initial_points, int)
        or isinstance(initial_points, bool)
        or purchase_plan.get("spent_skill_points") != sum(spends)
        or purchase_plan.get("expected_remaining_skill_points") != initial_points - sum(spends)
    ):
        raise TerminalSkillLifecycleError(invalid)
    selected_total = 0
    for purchase, spend in zip(purchases, spends, strict=True):
        if not isinstance(purchase, Mapping):
            raise TerminalSkillLifecycleError(invalid)
        selected_total += spend
        if (
            purchase.get("expected_selected_total_after") != selected_total
            or purchase.get("expected_skill_points_after") != initial_points - selected_total
        ):
            raise TerminalSkillLifecycleError(invalid)
    review_payload = _load_strict_json(review, invalid_code=invalid)
    reviewed_rows = review_payload.get("reviewed_rows")
    if (
        review_payload.get("schema_version") != "uma.skill_purchase_current_run_review.v1"
        or review_payload.get("plan_id") != plan_id
        or not isinstance(review_payload.get("sample_id"), str)
        or not review_payload["sample_id"]
        or review_payload.get("sample_id") != input_data.get("sample_id")
        or review_payload.get("sample_id") != promotion.get("sample_id")
        or review_payload.get("session_id") != session_id
        or review_payload.get("capture_batch_id") != capture_batch_id
        or not isinstance(reviewed_rows, Mapping)
        or not set(item_ids).issubset({row.get("item_id") for row in reviewed_rows.values() if isinstance(row, Mapping)})
    ):
        raise TerminalSkillLifecycleError(invalid)
    fingerprints = [
        purchase.get("source_row_fingerprint")
        if isinstance(purchase, Mapping)
        else None
        for purchase in purchases
    ]
    if (
        any(not isinstance(value, str) or not value for value in fingerprints)
        or len(fingerprints) != len(set(fingerprints))
        or set(reviewed_rows) != set(fingerprints)
    ):
        raise TerminalSkillLifecycleError(invalid)
    for purchase in purchases:
        if not isinstance(purchase, Mapping):
            raise TerminalSkillLifecycleError(invalid)
        row = reviewed_rows[purchase["source_row_fingerprint"]]
        binding = purchase.get("purchase_binding")
        if (
            not isinstance(row, Mapping)
            or not isinstance(binding, Mapping)
            or row.get("item_id") != purchase.get("item_id")
            or row.get("source_report_sha256") != source_sha
            or purchase.get("source_report_sha256") != source_sha
            or row.get("purchase_binding_sha256")
            != purchase.get("purchase_binding_sha256")
            or row.get("source_binding") != binding.get("source_binding")
            or row.get("observed_final_cost")
            != purchase.get("observed_final_cost")
            or row.get("expected_selected_total")
            != purchase.get("expected_selected_total_after")
            or row.get("expected_remaining_skill_points")
            != purchase.get("expected_skill_points_after")
            or binding.get("item_id") != purchase.get("item_id")
            or binding.get("source_row_fingerprint")
            != purchase.get("source_row_fingerprint")
            or binding.get("source_report_sha256") != source_sha
            or binding.get("observed_final_cost")
            != purchase.get("observed_final_cost")
            or binding.get("expected_selected_total_after")
            != purchase.get("expected_selected_total_after")
            or binding.get("expected_skill_points_after")
            != purchase.get("expected_skill_points_after")
            or purchase.get("purchase_binding_sha256")
            != _producer_canonical_sha(binding)
        ):
            raise TerminalSkillLifecycleError(invalid)
    evidence_payload = _load_strict_json(evidence, invalid_code=invalid)
    if (
        evidence_payload.get("schema_version")
        != "uma.skill_purchase_promotion_evidence.v2"
        or evidence_payload.get("plan_id") != plan_id
        or evidence_payload.get("source_report_sha256") != source_sha
        or not isinstance(evidence_payload.get("economics"), Mapping)
        or evidence_payload["economics"].get("initial_skill_points") != initial_points
        or evidence_payload["economics"].get("spend") != purchase_plan["spent_skill_points"]
        or evidence_payload["economics"].get("expected_remaining") != purchase_plan["expected_remaining_skill_points"]
        or evidence_payload["economics"].get("cost_basis") != "observed_final"
        or evidence_payload.get("resource_checks") != _SAFE_RESOURCE_CHECKS
    ):
        raise TerminalSkillLifecycleError(invalid)
    return {
        "purchase_plan_raw_sha256": _promotion_input(inputs, "report_raw_sha256", code=invalid),
        "purchase_plan_path": _promotion_input(inputs, "report_path", code=invalid),
        "review_raw_sha256": _sha256_file(review),
        "review_path": _relative(review, allowed_run_root),
        "plan_id": plan_id,
        "skill_session_id": session_id,
        "capture_batch_id": capture_batch_id,
        **({"capture_plan_sha256": capture_plan_sha, "plan_sha256": batch_plan["plan_sha256"]} if single_page else {}),
    }


def _receipt_name(sequence: int, phase: str) -> str:
    return f"receipt-{sequence:03d}-{phase}.json"


def _validate_atomic_terminal_phase(
    phase: str,
    report: Mapping[str, Any],
    *,
    allowed_run_root: Path,
    state: Mapping[str, Any],
    inputs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    invalid = f"{phase}_report_invalid"
    contract = _ATOMIC_TERMINAL_PHASES[phase]
    if phase == "final_confirmation" and report.get("schema_version") == "uma.skill_batch_final_confirmation_report.v1":
        try:
            native = load_batch_final_evidence(
                _stored_file(_promotion_input(inputs, "report_path", code=invalid),
                             _promotion_input(inputs, "report_raw_sha256", code=invalid),
                             allowed_run_root=allowed_run_root, code=invalid),
                root=allowed_run_root,
                plan_path=_stored_file(state.get("purchase_plan_path"), state.get("purchase_plan_raw_sha256"),
                                       allowed_run_root=allowed_run_root, code=invalid),
                review_path=_stored_file(state.get("review_path"), state.get("review_raw_sha256"),
                                        allowed_run_root=allowed_run_root, code=invalid),
            )
            receipt = _stored_file(_promotion_input(inputs, "atomic_session_receipt_path", code=invalid),
                                  _promotion_input(inputs, "expected_atomic_session_receipt_sha256", code=invalid),
                                  allowed_run_root=allowed_run_root, code=invalid)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise TerminalSkillLifecycleError(invalid) from exc
        if (receipt != Path(native["receipt_path"]) or native["plan_id"] != state.get("plan_id")
            or native["session_id"] != state.get("skill_session_id")
            or native["capture_batch_id"] != state.get("capture_batch_id")):
            raise TerminalSkillLifecycleError(invalid)
        return {"atomic_session_receipt_path": _relative(receipt, allowed_run_root),
                "atomic_session_receipt_sha256": _sha256_file(receipt),
                "final_confirmation_report_sha256": native["report_sha256"], "batch_final_confirmation": True,
                "confirmed_planned_item_ids": native["planned_item_ids"],
                "expected_remaining_skill_points": native["expected_remaining_skill_points"]}
    batch_phase = state.get("batch_final_confirmation") is True
    direct_return = batch_phase and phase == "return_after_purchase" and report.get("stage") == "batch-return-after-final-probe"
    if batch_phase and phase in {"close_success", "return_after_purchase"}:
        contract = {**contract, "stage": "batch-" + contract["stage"], "stop_reason": "batch_" + contract["stop_reason"]}
    if direct_return:
        contract = {**contract, "stage": "batch-return-after-final-probe", "stop_reason": "batch_return_after_final_probe_complete"}
        if state.get("close_success_report_sha256") is not None:
            raise TerminalSkillLifecycleError(invalid)
    expected_report_keys = {
        "is_valid", "stage", "mode", "performed", "action_id",
        "tap_count", "device_command_count", "plan_id",
        "live_session_receipt_sha256", "stop_reason", "actions",
        "captured_frames", "pre_frame_path", "pre_frame_sha256",
        "post_frame_path", "post_frame_sha256",
    }
    if phase == "final_confirmation":
        expected_report_keys.add("final_confirmation_performed")
    if phase == "return_after_purchase":
        expected_report_keys.update(
            {"final_confirmation_report_sha256", "close_success_performed", "direct_return_evidence"}
            if direct_return else {"final_confirmation_report_sha256", "success_report_sha256"}
        )
    elif batch_phase and phase == "close_success":
        expected_report_keys.add("final_confirmation_report_sha256")
    if set(report) != expected_report_keys:
        raise TerminalSkillLifecycleError(invalid)
    receipt_path = _stored_file(
        _promotion_input(inputs, "atomic_session_receipt_path", code=invalid),
        _promotion_input(
            inputs, "expected_atomic_session_receipt_sha256", code=invalid
        ),
        allowed_run_root=allowed_run_root,
        code=invalid,
    )
    receipt_sha256 = _sha256_file(receipt_path)
    pre_frame = _stored_file(
        report.get("pre_frame_path"),
        report.get("pre_frame_sha256"),
        allowed_run_root=allowed_run_root,
        code=invalid,
    )
    post_frame = _stored_file(
        report.get("post_frame_path"),
        report.get("post_frame_sha256"),
        allowed_run_root=allowed_run_root,
        code=invalid,
    )
    captured_frames = report.get("captured_frames")
    if not isinstance(captured_frames, list) or len(captured_frames) != 2:
        raise TerminalSkillLifecycleError(invalid)
    captured_pre = _stored_file(
        captured_frames[0], report.get("pre_frame_sha256"),
        allowed_run_root=allowed_run_root, code=invalid,
    )
    captured_post = _stored_file(
        captured_frames[1], report.get("post_frame_sha256"),
        allowed_run_root=allowed_run_root, code=invalid,
    )
    if captured_pre != pre_frame or captured_post != post_frame:
        raise TerminalSkillLifecycleError(invalid)
    if (
        report.get("mode") != "live"
        or report.get("is_valid") is not True
        or report.get("performed") is not True
        or report.get("stage") != contract["stage"]
        or report.get("action_id") != contract["action_id"]
        or report.get("actions") != [contract["action_id"]]
        or report.get("tap_count") != 1
        or report.get("stop_reason") != contract["stop_reason"]
        or report.get("plan_id") != state.get("plan_id")
        or report.get("live_session_receipt_sha256") != receipt_sha256
    ):
        raise TerminalSkillLifecycleError(invalid)
    receipt = _load_strict_json(receipt_path, invalid_code=invalid)
    expected_receipt_keys = {
        "schema_version", "status", "stage", "action_id", "plan_id",
        "plan_sha256", "review_sha256", "session_id", "capture_batch_id",
    }
    if phase == "return_after_purchase":
        expected_receipt_keys.update(
            {"final_confirmation_report_sha256", "close_success_performed"}
            if direct_return else {"final_confirmation_report_sha256", "success_report_sha256"}
        )
    elif batch_phase and phase == "close_success":
        expected_receipt_keys.add("final_confirmation_report_sha256")
    if (
        set(receipt) != expected_receipt_keys
        or receipt.get("schema_version")
        != "uma.skill_purchase_atomic_live_session.v1"
        or receipt.get("status") != "consumed_before_device_connect"
        or receipt.get("stage") != contract["stage"]
        or receipt.get("action_id") != contract["action_id"]
        or receipt.get("plan_id") != state.get("plan_id")
        or receipt.get("plan_sha256") != state.get("purchase_plan_raw_sha256")
        or receipt.get("review_sha256") != state.get("review_raw_sha256")
        or receipt.get("session_id") != state.get("skill_session_id")
        or receipt.get("capture_batch_id") != state.get("capture_batch_id")
    ):
        raise TerminalSkillLifecycleError(invalid)
    if phase == "final_confirmation":
        if report.get("final_confirmation_performed") is not True:
            raise TerminalSkillLifecycleError(invalid)
    if batch_phase and phase == "close_success" and (
        report.get("final_confirmation_report_sha256") != state.get("final_confirmation_report_sha256")
        or receipt.get("final_confirmation_report_sha256") != state.get("final_confirmation_report_sha256")
    ):
        raise TerminalSkillLifecycleError(invalid)
    if phase == "return_after_purchase":
        final_sha256 = state.get("final_confirmation_report_sha256")
        success_sha256 = state.get("close_success_report_sha256")
        if (
            report.get("final_confirmation_report_sha256") != final_sha256
            or report.get("success_report_sha256") != success_sha256
            or receipt.get("final_confirmation_report_sha256") != final_sha256
            or receipt.get("success_report_sha256") != success_sha256
        ):
            raise TerminalSkillLifecycleError(invalid)
        if direct_return and (
            report.get("close_success_performed") is not False
            or receipt.get("close_success_performed") is not False
            or report.get("direct_return_evidence") != {
                "source_frame_sha256": report["pre_frame_sha256"],
                "current_skill_points": state["expected_remaining_skill_points"],
                "planned_item_ids": state["confirmed_planned_item_ids"],
                "selected_item_ids": [], "selected_total": 0,
            }
            or type(report["direct_return_evidence"].get("current_skill_points")) is not int
            or type(report["direct_return_evidence"].get("selected_total")) is not int
        ):
            raise TerminalSkillLifecycleError(invalid)
    update = {
        "atomic_session_receipt_path": _relative(receipt_path, allowed_run_root),
        "atomic_session_receipt_sha256": receipt_sha256,
    }
    if direct_return:
        update["direct_final_return"] = True
    if phase == "final_confirmation":
        update["final_confirmation_report_sha256"] = _promotion_input(
            inputs, "report_raw_sha256", code=invalid
        )
    if phase == "close_success":
        update["close_success_report_sha256"] = _promotion_input(
            inputs, "report_raw_sha256", code=invalid
        )
    return update


def _validate_supervisor_resume(
    report: Mapping[str, Any],
    *,
    allowed_run_root: Path,
    lifecycle: Mapping[str, Any],
    lifecycle_root_path: Path | None,
) -> dict[str, Any]:
    required = {
        "consumed_terminal_handoff_path",
        "consumed_terminal_handoff_sha256",
        "consumed_terminal_skill_lifecycle_root_path",
        "consumed_terminal_skill_lifecycle_root_sha256",
    }
    if not required.issubset(report):
        raise TerminalSkillLifecycleError("supervisor_resume_binding_unavailable")
    invalid = "supervisor_resume_report_invalid"
    if lifecycle_root_path is None:
        raise TerminalSkillLifecycleError(invalid)
    if (
        report.get("is_valid") is not True
        or report.get("status") != "completed"
        or report.get("run_session_id") != lifecycle.get("run_session_id")
    ):
        raise TerminalSkillLifecycleError(invalid)
    handoff = _stored_file(
        report.get("consumed_terminal_handoff_path"),
        report.get("consumed_terminal_handoff_sha256"),
        allowed_run_root=allowed_run_root,
        code=invalid,
    )
    if (
        _relative(handoff, allowed_run_root)
        != lifecycle.get("handoff_report_path")
        or report.get("consumed_terminal_handoff_sha256")
        != lifecycle.get("handoff_report_sha256")
    ):
        raise TerminalSkillLifecycleError(invalid)
    lifecycle_root = _stored_file(
        report.get("consumed_terminal_skill_lifecycle_root_path"),
        report.get("consumed_terminal_skill_lifecycle_root_sha256"),
        allowed_run_root=allowed_run_root,
        code=invalid,
    )
    if (
        lifecycle_root != lifecycle_root_path
        or lifecycle_root.name != _ROOT_NAME
        or report.get("consumed_terminal_skill_lifecycle_root_sha256")
        != _sha256_file(lifecycle_root_path)
    ):
        raise TerminalSkillLifecycleError(invalid)
    return {}


def _validate_phase_report(
    phase: str,
    report: Mapping[str, Any],
    *,
    allowed_run_root: Path,
    lifecycle: Mapping[str, Any],
    state: Mapping[str, Any],
    checkpoint_path: Path | None,
    expected_checkpoint_sha256: str | None,
    promotion_inputs: Mapping[str, Any] | None = None,
    atomic_inputs: Mapping[str, Any] | None = None,
    lifecycle_root_path: Path | None = None,
) -> dict[str, Any]:
    if phase == "terminal_handoff":
        _validate_handoff(report)
        _stored_file(
            report.get("source_image_path"), report.get("source_image_sha256"),
            allowed_run_root=allowed_run_root, code="terminal_handoff_report_invalid"
        )
        return {}
    if phase == "capture_plan":
        invalid = "capture_plan_report_invalid"
        if (
            report.get("schema_version") != "uma.terminal_skill_prepare.v1"
            or report.get("is_valid") is not True
            or report.get("stop_reason") != "plan_ready"
            or report.get("performed") is not False
            or report.get("tap_count") != 0
            or report.get("selection_executed") is not False
            or report.get("purchase_executed") is not False
            or report.get("confirmation_opened") is not False
            or report.get("handoff_artifact_sha256") != lifecycle["handoff_report_sha256"]
        ):
            raise TerminalSkillLifecycleError(invalid)
        handoff = _stored_file(
            report.get("handoff_artifact_path"), report.get("handoff_artifact_sha256"),
            allowed_run_root=allowed_run_root, code=invalid
        )
        if _relative(handoff, allowed_run_root) != lifecycle["handoff_report_path"]:
            raise TerminalSkillLifecycleError(invalid)
        capture = _stored_file(report.get("capture_manifest_path"), report.get("capture_manifest_sha256"), allowed_run_root=allowed_run_root, code=invalid)
        inventory = _stored_file(report.get("inventory_path"), report.get("inventory_raw_sha256"), allowed_run_root=allowed_run_root, code=invalid)
        plan = _stored_file(report.get("plan_path"), report.get("plan_raw_sha256"), allowed_run_root=allowed_run_root, code=invalid)
        _require_sha256(report.get("inventory_sha256"), code=invalid)
        _require_sha256(report.get("plan_sha256"), code=invalid)
        inventory_payload = _load_strict_json(inventory, invalid_code=invalid)
        plan_payload = _load_strict_json(plan, invalid_code=invalid)
        inventory_valid, _ = validate_skill_inventory_packet(inventory_payload)
        plan_valid, _ = validate_skill_batch_plan(plan_payload, inventory_payload)
        capture_binding = inventory_payload.get("capture_binding")
        if (
            not inventory_valid
            or not plan_valid
            or inventory_payload.get("packet_sha256") != report["inventory_sha256"]
            or not isinstance(capture_binding, Mapping)
            or plan_payload.get("plan_sha256") != report["plan_sha256"]
            or plan_payload.get("inventory_packet_sha256")
            != inventory_payload.get("packet_sha256")
            or plan_payload.get("selected_action_ids")
            != report.get("selected_action_ids")
            or plan_payload.get("resource_checks") != _SAFE_RESOURCE_CHECKS
            or report.get("resource_checks") != _SAFE_RESOURCE_CHECKS
        ):
            raise TerminalSkillLifecycleError(invalid)
        return {
            "capture_manifest_sha256": _sha256_file(capture),
            "inventory_raw_sha256": _sha256_file(inventory),
            "plan_raw_sha256": _sha256_file(plan),
            "inventory_sha256": inventory_payload["packet_sha256"],
            "plan_sha256": plan_payload["plan_sha256"],
            "capture_binding": dict(capture_binding),
        }
    if phase == "plan_promotion":
        return _validate_plan_promotion(
            report,
            allowed_run_root=allowed_run_root,
            state=state,
            inputs=promotion_inputs,
        )
    if phase == "selection":
        invalid = "selection_report_invalid"
        if (
            report.get("schema_version") != "uma.skill_batch_cli_summary.v1"
            or report.get("phase") != "select"
            or report.get("mode") != "live"
            or report.get("is_valid") is not True
            or report.get("stop_reason") != "confirmation_opened"
            or report.get("performed") is not True
            or report.get("confirmation_opened") is not True
            or report.get("remaining_action_ids") != []
            or report.get("inventory_sha256") != state.get("inventory_sha256")
            or report.get("plan_sha256") != state.get("plan_sha256")
        ):
            raise TerminalSkillLifecycleError(invalid)
        if checkpoint_path is None or expected_checkpoint_sha256 is None:
            raise TerminalSkillLifecycleError("selection_checkpoint_required")
        checkpoint = _stored_file(checkpoint_path, expected_checkpoint_sha256, allowed_run_root=allowed_run_root, code="selection_checkpoint_invalid")
        payload = _load_strict_json(checkpoint, invalid_code="selection_checkpoint_invalid")
        if (
            payload.get("schema_version") != "uma.skill_batch_selection_checkpoint.v1"
            or payload.get("status") != "page_verified"
            or payload.get("pending_group_ids") != []
            or payload.get("remaining_action_ids") != []
            or payload.get("inventory_packet_sha256") != state.get("inventory_sha256")
            or payload.get("plan_sha256") != state.get("plan_sha256")
        ):
            raise TerminalSkillLifecycleError("selection_checkpoint_invalid")
        return {"checkpoint_path": _relative(checkpoint, allowed_run_root), "checkpoint_sha256": expected_checkpoint_sha256}
    if phase in _ATOMIC_TERMINAL_PHASES:
        return _validate_atomic_terminal_phase(
            phase,
            report,
            allowed_run_root=allowed_run_root,
            state=state,
            inputs=atomic_inputs,
        )
    if phase == "supervisor_resume":
        return _validate_supervisor_resume(
            report,
            allowed_run_root=allowed_run_root,
            lifecycle=lifecycle,
            lifecycle_root_path=lifecycle_root_path,
        )
    raise TerminalSkillLifecycleError("phase_order_invalid")


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    data = _canonical_json_bytes(payload)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise TerminalSkillLifecycleError("lifecycle_already_exists") from exc
    except OSError as exc:
        raise TerminalSkillLifecycleError("lifecycle_write_failed") from exc
    try:
        if path.read_bytes() != data:
            raise TerminalSkillLifecycleError("lifecycle_write_verification_failed")
    except OSError as exc:
        raise TerminalSkillLifecycleError("lifecycle_write_verification_failed") from exc


def initialize_terminal_skill_lifecycle(
    *,
    allowed_run_root: Path,
    lifecycle_dir: Path,
    run_session_id: str,
    handoff_report_path: Path,
    expected_handoff_report_sha256: str,
) -> Path:
    if not isinstance(run_session_id, str) or not run_session_id:
        raise TerminalSkillLifecycleError("run_session_id_invalid")
    _require_sha256(expected_handoff_report_sha256, code="handoff_report_sha_invalid")
    handoff_path = _require_inside_root(
        handoff_report_path,
        allowed_run_root=allowed_run_root,
        code="handoff_report_outside_root",
    )
    target_dir = _require_inside_root(
        lifecycle_dir,
        allowed_run_root=allowed_run_root,
        code="lifecycle_dir_outside_root",
        allow_missing_tail=True,
    )
    if not handoff_path.is_file():
        raise TerminalSkillLifecycleError("handoff_report_missing")
    if _sha256_file(handoff_path) != expected_handoff_report_sha256:
        raise TerminalSkillLifecycleError("handoff_report_sha_mismatch")
    handoff_payload = _load_strict_json(handoff_path, invalid_code="handoff_report_invalid")
    _validate_handoff(handoff_payload)
    _stored_file(
        handoff_payload["source_image_path"], handoff_payload["source_image_sha256"],
        allowed_run_root=allowed_run_root, code="handoff_report_invalid"
    )
    target = target_dir / _ROOT_NAME
    _require_inside_root(
        target,
        allowed_run_root=allowed_run_root,
        code="lifecycle_dir_outside_root",
        allow_missing_tail=True,
    )
    payload: dict[str, Any] = {
        "action_eligible": False,
        "device_action_performed": False,
        "handoff_report_path": str(handoff_path.relative_to(allowed_run_root.absolute())),
        "handoff_report_sha256": expected_handoff_report_sha256,
        "paid_resource_allowed": False,
        "phase_receipts": [],
        "run_session_id": run_session_id,
        "schema_version": _ROOT_SCHEMA,
    }
    _write_exclusive(target, payload)
    return target


def _load_root(
    *, allowed_run_root: Path, lifecycle_dir: Path, run_session_id: str
) -> tuple[dict[str, Any], Path]:
    target_dir = _require_inside_root(
        lifecycle_dir,
        allowed_run_root=allowed_run_root,
        code="lifecycle_dir_outside_root",
    )
    target = _require_inside_root(
        target_dir / _ROOT_NAME,
        allowed_run_root=allowed_run_root,
        code="lifecycle_dir_outside_root",
    )
    if not target.is_file():
        raise TerminalSkillLifecycleError("lifecycle_missing")
    payload = _load_canonical_json(target, invalid_code="lifecycle_invalid")
    expected_keys = {
        "action_eligible",
        "device_action_performed",
        "handoff_report_path",
        "handoff_report_sha256",
        "paid_resource_allowed",
        "phase_receipts",
        "run_session_id",
        "schema_version",
    }
    if (
        set(payload) != expected_keys
        or payload.get("schema_version") != _ROOT_SCHEMA
        or payload.get("action_eligible") is not False
        or payload.get("device_action_performed") is not False
        or payload.get("paid_resource_allowed") is not False
        or payload.get("phase_receipts") != []
        or not isinstance(payload.get("handoff_report_path"), str)
    ):
        raise TerminalSkillLifecycleError("lifecycle_state_invalid")
    if payload.get("run_session_id") != run_session_id:
        raise TerminalSkillLifecycleError("run_session_id_mismatch")
    _require_sha256(str(payload.get("handoff_report_sha256")), code="lifecycle_invalid")
    handoff = _stored_file(
        payload["handoff_report_path"], payload["handoff_report_sha256"],
        allowed_run_root=allowed_run_root, code="lifecycle_invalid"
    )
    _validate_handoff(_load_strict_json(handoff, invalid_code="lifecycle_invalid"))
    return payload, target


def _receipt_state(
    *, allowed_run_root: Path, lifecycle_dir: Path, lifecycle: Mapping[str, Any]
) -> tuple[int, str, str, dict[str, Any]]:
    previous_path = _relative(lifecycle_dir / _ROOT_NAME, allowed_run_root)
    previous_sha = _sha256_file(lifecycle_dir / _ROOT_NAME)
    state: dict[str, Any] = {}
    sequence = 0
    found: set[str] = set()
    if lifecycle_dir.exists():
        for path in lifecycle_dir.iterdir():
            if path.name != _ROOT_NAME:
                if _is_reparse_stat(os.lstat(path)) or not path.is_file():
                    raise TerminalSkillLifecycleError("receipt_chain_invalid")
                found.add(path.name)
    for expected_phase in _IMPLEMENTED_PHASES:
        if (expected_phase == "close_success" and state.get("batch_final_confirmation") is True
            and (lifecycle_dir / _receipt_name(sequence + 1, "return_after_purchase")).exists()):
            # A native direct-list return has no close action or close receipt.
            continue
        candidate = lifecycle_dir / _receipt_name(sequence + 1, expected_phase)
        if not candidate.exists():
            break
        sequence += 1
        found.discard(candidate.name)
        receipt = _load_canonical_json(candidate, invalid_code="receipt_chain_invalid")
        expected_keys = {
            "action_eligible", "device_action_performed", "input_report_path",
            "input_report_sha256", "paid_resource_allowed", "phase",
            "previous_receipt_path", "previous_receipt_sha256", "run_session_id",
            "schema_version", "sequence",
        }
        if expected_phase == "selection":
            expected_keys.update({"checkpoint_path", "checkpoint_sha256"})
        if expected_phase == "plan_promotion":
            expected_keys.update({
                "source_report_path", "expected_source_report_sha256",
                "review_path", "expected_review_sha256",
                "evidence_path", "expected_evidence_sha256",
                "expected_skill_session_id", "expected_capture_batch_id",
                "promoted_purchase_plan_path", "promoted_purchase_plan_raw_sha256",
            })
        if expected_phase in _ATOMIC_TERMINAL_PHASES:
            expected_keys.update(
                {
                    "atomic_session_receipt_path",
                    "atomic_session_receipt_sha256",
                }
            )
        if expected_phase == "return_after_purchase":
            expected_keys.update({"final_confirmation_report_sha256"})
            expected_keys.add("close_success_performed" if state.get("close_success_report_sha256") is None
                              else "close_success_report_sha256")
        if set(receipt) != expected_keys or (
            receipt.get("schema_version") != _RECEIPT_SCHEMA
            or receipt.get("sequence") != sequence
            or receipt.get("phase") != expected_phase
            or receipt.get("run_session_id") != lifecycle["run_session_id"]
            or receipt.get("previous_receipt_path") != previous_path
            or receipt.get("previous_receipt_sha256") != previous_sha
            or receipt.get("action_eligible") is not False
            or receipt.get("device_action_performed") is not False
            or receipt.get("paid_resource_allowed") is not False
        ):
            raise TerminalSkillLifecycleError("receipt_chain_invalid")
        report = _stored_file(
            receipt.get("input_report_path"), receipt.get("input_report_sha256"),
            allowed_run_root=allowed_run_root, code="receipt_chain_invalid"
        )
        report_payload = _load_strict_json(report, invalid_code="receipt_chain_invalid")
        checkpoint: Path | None = None
        checkpoint_sha: str | None = None
        promotion_inputs: dict[str, Any] | None = None
        if expected_phase == "selection":
            checkpoint_value = receipt.get("checkpoint_path")
            checkpoint_sha = receipt.get("checkpoint_sha256")
            if not isinstance(checkpoint_value, str) or not isinstance(checkpoint_sha, str):
                raise TerminalSkillLifecycleError("receipt_chain_invalid")
            checkpoint = _stored_file(
                checkpoint_value, checkpoint_sha, allowed_run_root=allowed_run_root,
                code="receipt_chain_invalid"
            )
        if expected_phase == "plan_promotion":
            if (
                receipt.get("promoted_purchase_plan_path")
                != receipt.get("input_report_path")
                or receipt.get("promoted_purchase_plan_raw_sha256")
                != receipt.get("input_report_sha256")
            ):
                raise TerminalSkillLifecycleError("receipt_chain_invalid")
            promotion_inputs = {
                key: receipt.get(key)
                for key in (
                    "source_report_path", "expected_source_report_sha256",
                    "review_path", "expected_review_sha256",
                    "evidence_path", "expected_evidence_sha256",
                    "expected_skill_session_id", "expected_capture_batch_id",
                )
            }
            promotion_inputs["report_raw_sha256"] = receipt["input_report_sha256"]
            promotion_inputs["report_path"] = receipt["input_report_path"]
        atomic_inputs: dict[str, Any] | None = None
        if expected_phase in _ATOMIC_TERMINAL_PHASES:
            if expected_phase == "return_after_purchase" and (
                receipt.get("final_confirmation_report_sha256")
                != state.get("final_confirmation_report_sha256")
                or receipt.get("close_success_report_sha256")
                != state.get("close_success_report_sha256")
            ):
                raise TerminalSkillLifecycleError("receipt_chain_invalid")
            if (expected_phase == "return_after_purchase" and state.get("close_success_report_sha256") is None
                and receipt.get("close_success_performed") is not False):
                raise TerminalSkillLifecycleError("receipt_chain_invalid")
            atomic_inputs = {
                "atomic_session_receipt_path": receipt.get(
                    "atomic_session_receipt_path"
                ),
                "expected_atomic_session_receipt_sha256": receipt.get(
                    "atomic_session_receipt_sha256"
                ),
                "report_raw_sha256": receipt["input_report_sha256"],
                "report_path": receipt["input_report_path"],
            }
        try:
            state_update = _validate_phase_report(
                expected_phase, report_payload, allowed_run_root=allowed_run_root,
                lifecycle=lifecycle, state=state, checkpoint_path=checkpoint,
            expected_checkpoint_sha256=checkpoint_sha,
            promotion_inputs=promotion_inputs,
            atomic_inputs=atomic_inputs,
            lifecycle_root_path=lifecycle_dir / _ROOT_NAME,
        )
        except TerminalSkillLifecycleError as exc:
            if expected_phase in _ATOMIC_TERMINAL_PHASES:
                raise TerminalSkillLifecycleError("receipt_chain_invalid") from exc
            raise
        state.update(state_update)
        previous_path = _relative(candidate, allowed_run_root)
        previous_sha = _sha256_file(candidate)
    if found:
        raise TerminalSkillLifecycleError("receipt_chain_invalid")
    return sequence, previous_path, previous_sha, state


def load_terminal_skill_lifecycle(
    *, allowed_run_root: Path, lifecycle_dir: Path, run_session_id: str
) -> dict[str, object]:
    lifecycle, lifecycle_root_path = _load_root(
        allowed_run_root=allowed_run_root,
        lifecycle_dir=lifecycle_dir,
        run_session_id=run_session_id,
    )
    _receipt_state(
        allowed_run_root=allowed_run_root,
        lifecycle_dir=lifecycle_dir.absolute(), lifecycle=lifecycle,
    )
    return lifecycle


def record_terminal_skill_phase(
    *,
    allowed_run_root: Path,
    lifecycle_dir: Path,
    run_session_id: str,
    phase: str,
    report_path: Path,
    expected_report_sha256: str,
    checkpoint_path: Path | None = None,
    expected_checkpoint_sha256: str | None = None,
    source_report_path: Path | None = None,
    expected_source_report_sha256: str | None = None,
    review_path: Path | None = None,
    expected_review_sha256: str | None = None,
    evidence_path: Path | None = None,
    expected_evidence_sha256: str | None = None,
    expected_skill_session_id: str | None = None,
    expected_capture_batch_id: str | None = None,
    atomic_session_receipt_path: Path | None = None,
    expected_atomic_session_receipt_sha256: str | None = None,
) -> Path:
    lifecycle, lifecycle_root_path = _load_root(
        allowed_run_root=allowed_run_root,
        lifecycle_dir=lifecycle_dir,
        run_session_id=run_session_id,
    )
    if run_session_id != lifecycle["run_session_id"]:
        raise TerminalSkillLifecycleError("run_session_id_mismatch")
    if phase not in _IMPLEMENTED_PHASES:
        raise TerminalSkillLifecycleError("phase_order_invalid")
    resolved_dir = _require_inside_root(lifecycle_dir, allowed_run_root=allowed_run_root, code="lifecycle_dir_outside_root")
    sequence, previous_path, previous_sha, state = _receipt_state(
        allowed_run_root=allowed_run_root, lifecycle_dir=resolved_dir, lifecycle=lifecycle
    )
    phase_index = sequence + (1 if state.get("direct_final_return") is True else 0)
    if phase_index >= len(_IMPLEMENTED_PHASES):
        raise TerminalSkillLifecycleError("phase_order_invalid")
    expected_phase = _IMPLEMENTED_PHASES[phase_index]
    if (expected_phase == "close_success" and phase == "return_after_purchase"
        and state.get("batch_final_confirmation") is True):
        expected_phase = "return_after_purchase"
    if phase in _IMPLEMENTED_PHASES[:sequence]:
        raise TerminalSkillLifecycleError("phase_replay_rejected")
    target = resolved_dir / _receipt_name(sequence + 1, phase)
    if target.exists():
        raise TerminalSkillLifecycleError("phase_replay_rejected")
    if phase != expected_phase:
        raise TerminalSkillLifecycleError("phase_order_invalid")
    _require_sha256(expected_report_sha256, code="report_sha_invalid")
    report = _stored_file(
        report_path, expected_report_sha256, allowed_run_root=allowed_run_root,
        code="report_sha_mismatch"
    )
    report_payload = _load_strict_json(report, invalid_code=f"{phase}_report_invalid")
    promotion_inputs = {
        "source_report_path": source_report_path,
        "expected_source_report_sha256": expected_source_report_sha256,
        "review_path": review_path,
        "expected_review_sha256": expected_review_sha256,
        "evidence_path": evidence_path,
        "expected_evidence_sha256": expected_evidence_sha256,
        "expected_skill_session_id": expected_skill_session_id,
        "expected_capture_batch_id": expected_capture_batch_id,
        "report_raw_sha256": expected_report_sha256,
        "report_path": _relative(report, allowed_run_root),
    }
    atomic_inputs = {
        "atomic_session_receipt_path": atomic_session_receipt_path,
        "expected_atomic_session_receipt_sha256": (
            expected_atomic_session_receipt_sha256
        ),
        "report_raw_sha256": expected_report_sha256,
        "report_path": report,
    }
    state_update = _validate_phase_report(
        phase, report_payload, allowed_run_root=allowed_run_root, lifecycle=lifecycle,
        state=state, checkpoint_path=checkpoint_path,
        expected_checkpoint_sha256=expected_checkpoint_sha256,
        promotion_inputs=promotion_inputs,
        atomic_inputs=atomic_inputs,
        lifecycle_root_path=lifecycle_root_path,
    )
    state.update(state_update)
    receipt = {
        "action_eligible": False,
        "device_action_performed": False,
        "input_report_path": _relative(report, allowed_run_root),
        "input_report_sha256": expected_report_sha256,
        "paid_resource_allowed": False,
        "phase": phase,
        "previous_receipt_path": previous_path,
        "previous_receipt_sha256": previous_sha,
        "run_session_id": run_session_id,
        "schema_version": _RECEIPT_SCHEMA,
        "sequence": sequence + 1,
    }
    if phase == "selection":
        receipt["checkpoint_path"] = state["checkpoint_path"]
        receipt["checkpoint_sha256"] = state["checkpoint_sha256"]
    if phase == "plan_promotion":
        for path_key, sha_key in (
            ("source_report_path", "expected_source_report_sha256"),
            ("review_path", "expected_review_sha256"),
            ("evidence_path", "expected_evidence_sha256"),
        ):
            bound = _stored_file(
                promotion_inputs[path_key], promotion_inputs[sha_key],
                allowed_run_root=allowed_run_root,
                code="plan_promotion_report_invalid",
            )
            receipt[path_key] = _relative(bound, allowed_run_root)
            receipt[sha_key] = promotion_inputs[sha_key]
        receipt["expected_skill_session_id"] = expected_skill_session_id
        receipt["expected_capture_batch_id"] = expected_capture_batch_id
        receipt["promoted_purchase_plan_path"] = state["purchase_plan_path"]
        receipt["promoted_purchase_plan_raw_sha256"] = state[
            "purchase_plan_raw_sha256"
        ]
    if phase in _ATOMIC_TERMINAL_PHASES:
        atomic_receipt = _stored_file(
            atomic_session_receipt_path,
            expected_atomic_session_receipt_sha256,
            allowed_run_root=allowed_run_root,
            code=f"{phase}_report_invalid",
        )
        receipt["atomic_session_receipt_path"] = _relative(
            atomic_receipt, allowed_run_root
        )
        receipt["atomic_session_receipt_sha256"] = (
            expected_atomic_session_receipt_sha256
        )
    if phase == "return_after_purchase":
        receipt["final_confirmation_report_sha256"] = state[
            "final_confirmation_report_sha256"
        ]
        if state.get("direct_final_return") is True:
            receipt["close_success_performed"] = False
        else:
            receipt["close_success_report_sha256"] = state["close_success_report_sha256"]
    _write_exclusive(target, receipt)
    _receipt_state(
        allowed_run_root=allowed_run_root,
        lifecycle_dir=resolved_dir,
        lifecycle=lifecycle,
    )
    return target


__all__ = [
    "TerminalSkillLifecycleError",
    "initialize_terminal_skill_lifecycle",
    "load_terminal_skill_lifecycle",
    "record_terminal_skill_phase",
]
