"""Source-bound adapters for the existing 2-6 icon terminal batch route."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from core.skill_batch_purchase import build_skill_batch_plan, project_ai_skill_actions
from core.skill_purchase_plan_promotion import (
    _select_complete_source_row,
    _select_unique_planner_candidate,
    _validate_identity_binding,
    _source_auto_included_rows,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_NO_SHA = object()


def bound_file(root: Path, value: Any, expected_sha: Any = _NO_SHA) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError("batch_artifact_path_invalid")
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    path.relative_to(root.resolve())
    if not path.is_file() or (expected_sha is not _NO_SHA and (
        not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
        or file_sha256(path) != expected_sha
    )):
        raise ValueError("batch_artifact_sha_mismatch")
    return path


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("batch_artifact_not_object")
    return value


def expected_confirmation_rows(
    plan: Mapping[str, Any], review: Mapping[str, Any], source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    from core.skill_purchase_dialog import _ALLOWED_AUTO_INCLUDED_LOWER_SKILLS, _valid_multi_page_expected_rows

    purchases = plan.get("purchases")
    promotion = plan.get("execution_promotion", {})
    reviews = review.get("reviewed_rows")
    if (
        not isinstance(purchases, list) or not 2 <= len(purchases) <= 6
        or not isinstance(reviews, Mapping)
        or review.get("plan_id") != plan.get("plan_id")
        or review.get("session_id") != promotion.get("session_id")
        or review.get("capture_batch_id") != promotion.get("fresh_capture_id")
        or review.get("sample_id") != promotion.get("sample_id")
        or set(reviews) != {item.get("source_row_fingerprint") for item in purchases}
    ):
        raise ValueError("batch_closure_review_mismatch")
    rows = []
    owned = source.get("inventory_packet", {}).get("owned_skill_ids", [])
    for purchase in purchases:
        row = reviews[purchase["source_row_fingerprint"]]
        description = row.get("observed_description")
        if (
            row.get("item_id") != purchase.get("item_id")
            or row.get("purchase_binding_sha256") != purchase.get("purchase_binding_sha256")
            or row.get("source_report_sha256") != purchase.get("source_report_sha256")
            or row.get("description_sha256") != purchase.get("purchase_binding", {}).get("description_sha256")
            or row.get("source_binding") != purchase.get("purchase_binding", {}).get("source_binding")
            or not isinstance(description, str) or not description
            or hashlib.sha256(description.encode("utf-8")).hexdigest() != row.get("description_sha256")
        ):
            raise ValueError("batch_closure_row_mismatch")
        rows.append({"item_id": purchase["item_id"], "name": purchase["name"],
                     "description": description, "kind": "planned"})
        if promotion.get("confirmation_row_limit") == 5:
            candidates = [item for item in source["inventory_packet"]["candidates"] if item.get("skill_id") == purchase["item_id"]]
            if len(candidates) != 1:
                raise ValueError("batch_closure_candidate_not_unique")
            rows.extend(_source_auto_included_rows(source, candidates[0]))
            continue
        lower = _ALLOWED_AUTO_INCLUDED_LOWER_SKILLS.get(purchase["item_id"])
        if lower is not None and lower not in owned:
            candidate = _select_unique_planner_candidate(source["planner_observation"]["available_skills"], lower)
            _validate_identity_binding(candidate.get("identity_binding", {}), lower)
            _, observed = _select_complete_source_row(candidate, source["parser_manifest"]["skills"], lower)
            rows.append({"item_id": lower, "name": observed.get("name"),
                         "description": observed.get("description"), "kind": "auto_included_lower",
                         "parent_item_id": purchase["item_id"]})
    if not 2 <= len(rows) <= 6 or not _valid_multi_page_expected_rows(rows):
        raise ValueError("batch_closure_supported_rows_required")
    if promotion.get("confirmation_row_limit") == 5 and len(rows) > 5:
        raise ValueError("batch_closure_single_page_limit_exceeded")
    return rows


def build_batch_closure_contract(
    *, root: Path, plan_path: Path, review_path: Path, source_report_path: Path,
    checkpoint_path: Path, top_frame: Path,
) -> dict[str, Any]:
    paths = [bound_file(root, path) for path in (plan_path, review_path, source_report_path, checkpoint_path, top_frame)]
    plan_path, review_path, source_report_path, checkpoint_path, top_frame = paths
    plan, review, source, checkpoint = [read_json(path) for path in paths[:4]]
    promotion = plan.get("execution_promotion", {})
    ids = [item["item_id"] for item in plan.get("purchases", [])]
    decision_source = promotion.get("decision_source")
    advisor_valid = True
    if decision_source == "ai_advisor":
        try:
            actions = project_ai_skill_actions(
                source["inventory_packet"], promotion.get("advisor_response", {})
            )
            rebuilt = build_skill_batch_plan(
                inventory=source["inventory_packet"], selected_action_ids=actions,
                decision_source="ai_advisor",
            )
            advisor_valid = (
                rebuilt["plan_sha256"] == promotion.get("batch_plan_sha256")
                and rebuilt["selected_action_ids"] == ids
            )
        except (KeyError, TypeError, ValueError):
            advisor_valid = False
    if (
        decision_source not in {"local_auto", "ai_advisor"}
        or not advisor_valid
        or plan.get("execution_allowed") is not True
        or plan.get("input", {}).get("source_report_sha256") != file_sha256(source_report_path)
        or checkpoint.get("schema_version") != "uma.skill_batch_selection_checkpoint.v1"
        or checkpoint.get("status") != "page_verified"
        or checkpoint.get("selected_action_ids") != ids
        or checkpoint.get("pending_group_ids") != []
        or checkpoint.get("remaining_action_ids") != []
        or checkpoint.get("inventory_packet_sha256") != promotion.get("inventory_packet_sha256")
        or checkpoint.get("plan_sha256") != promotion.get("batch_plan_sha256")
    ):
        raise ValueError("batch_closure_selection_mismatch")
    return {
        "schema_version": "uma.skill_multi_page_confirmation_contract.v1",
        "plan_sha256": file_sha256(plan_path),
        "top_frame_sha256": file_sha256(top_frame),
        "selection_checkpoint_path": str(checkpoint_path),
        "selection_checkpoint_sha256": file_sha256(checkpoint_path),
        "expected_rows": expected_confirmation_rows(plan, review, source),
        "expected_remaining_skill_points": plan["expected_remaining_skill_points"],
        "final_confirmation_allowed": True,
        "review_path": str(review_path), "review_sha256": file_sha256(review_path),
        "source_report_path": str(source_report_path), "source_report_sha256": file_sha256(source_report_path),
    }


def load_batch_final_evidence(
    report_path: Path, *, root: Path, plan_path: Path, review_path: Path,
) -> dict[str, Any]:
    """Recheck the native final report, pre-action receipt, closure and frames."""
    report_path, plan_path, review_path = [bound_file(root, p) for p in (report_path, plan_path, review_path)]
    report, plan = read_json(report_path), read_json(plan_path)
    receipt_path = bound_file(root, report_path.parent / "batch_final_confirmation_live_session.json", report.get("live_session_receipt_sha256"))
    receipt = read_json(receipt_path)
    closure_path = bound_file(root, receipt.get("closure_contract_path"), report.get("closure_contract_sha256"))
    closure = read_json(closure_path)
    top = bound_file(root, receipt.get("top_frame_path"), report.get("top_frame_sha256"))
    source = bound_file(root, closure.get("source_report_path"), closure.get("source_report_sha256"))
    checkpoint = bound_file(root, closure.get("selection_checkpoint_path"), closure.get("selection_checkpoint_sha256"))
    rebuilt = build_batch_closure_contract(root=root, plan_path=plan_path, review_path=review_path,
        source_report_path=source, checkpoint_path=checkpoint, top_frame=top)
    if rebuilt != closure:
        raise ValueError("batch_final_closure_mismatch")
    promotion = plan["execution_promotion"]
    action = "skill_purchase.final_confirm"
    rows = closure["expected_rows"]
    planned = [row["item_id"] for row in rows if row["kind"] == "planned"]
    auto = [row["item_id"] for row in rows if row["kind"] == "auto_included_lower"]
    frames = report.get("captured_frames")
    if (
        set(report) != {"schema_version", "is_valid", "performed", "plan_id", "plan_sha256",
            "closure_contract_sha256", "top_frame_sha256", "expected_remaining_skill_points",
            "final_confirmation_performed", "stop_reason", "confirmed_item_ids", "planned_item_ids",
            "auto_included_item_ids", "page_row_indexes", "dangerous_resource_flags", "current_frame_sha256",
            "captured_frames", "device_command_count", "actions", "tap_count", "post_frame_sha256", "live_session_receipt_sha256"}
        or
        report.get("schema_version") != "uma.skill_batch_final_confirmation_report.v1"
        or report.get("is_valid") is not True or report.get("performed") is not True
        or report.get("final_confirmation_performed") is not True
        or report.get("stop_reason") != "batch_final_confirmation_probe_complete"
        or report.get("actions") != [action] or type(report.get("tap_count")) is not int or report["tap_count"] != 1
        or report.get("plan_id") != plan.get("plan_id") or report.get("plan_sha256") != file_sha256(plan_path)
        or report.get("expected_remaining_skill_points") != plan.get("expected_remaining_skill_points")
        or report.get("planned_item_ids") != planned or report.get("auto_included_item_ids") != auto
        or report.get("confirmed_item_ids") != [row["item_id"] for row in rows]
        or report.get("dangerous_resource_flags") != []
        or not isinstance(frames, list) or len(frames) != 2
        or receipt.get("schema_version") != "uma.skill_batch_final_confirmation_session.v1"
        or receipt.get("status") != "consumed_before_device_connect"
        or receipt.get("action_id") != action or receipt.get("plan_id") != plan.get("plan_id")
        or receipt.get("plan_sha256") != file_sha256(plan_path)
        or receipt.get("closure_contract_sha256") != file_sha256(closure_path)
        or receipt.get("top_frame_sha256") != file_sha256(top)
        or receipt.get("selection_checkpoint_sha256") != file_sha256(checkpoint)
        or receipt.get("expected_remaining_skill_points") != plan.get("expected_remaining_skill_points")
        or receipt.get("session_id") != promotion.get("session_id")
        or receipt.get("capture_batch_id") != promotion.get("fresh_capture_id")
        or receipt.get("review_sha256") != file_sha256(review_path)
    ):
        raise ValueError("batch_final_report_mismatch")
    bound_file(root, frames[0], report.get("current_frame_sha256"))
    bound_file(root, frames[1], report.get("post_frame_sha256"))
    indexes = report.get("page_row_indexes")
    if (not isinstance(indexes, list) or not indexes
        or any(not isinstance(page, list) or not page or any(type(i) is not int for i in page)
               or page != list(range(page[0], page[0] + len(page))) for page in indexes)
        or indexes[0][0] != 0 or indexes[-1][-1] != len(rows) - 1
        or any(b[0] <= a[0] or b[0] > a[-1] for a, b in zip(indexes, indexes[1:]))
        or sorted({i for page in indexes for i in page}) != list(range(len(rows)))):
        raise ValueError("batch_final_pages_incomplete")
    return {**report, "report_sha256": file_sha256(report_path),
            "receipt_path": str(receipt_path), "review_sha256": file_sha256(review_path),
            "session_id": promotion["session_id"], "capture_batch_id": promotion["fresh_capture_id"]}


def load_batch_success_evidence(
    report_path: Path, *, root: Path, plan_path: Path, review_path: Path, final_report_sha256: str,
) -> dict[str, Any]:
    report_path, plan_path, review_path = [bound_file(root, p) for p in (report_path, plan_path, review_path)]
    report, plan = read_json(report_path), read_json(plan_path)
    receipt_path = bound_file(root, report_path.parent / "atomic_live_session.json", report.get("live_session_receipt_sha256"))
    receipt = read_json(receipt_path)
    promotion = plan["execution_promotion"]
    stage, action = "batch-close-success-probe", "skill_purchase.close_success"
    if (
        set(report) != {"is_valid", "stage", "mode", "performed", "action_id", "tap_count",
            "device_command_count", "plan_id", "live_session_receipt_sha256", "stop_reason", "actions",
            "captured_frames", "pre_frame_path", "pre_frame_sha256", "post_frame_path", "post_frame_sha256",
            "final_confirmation_report_sha256"}
        or report.get("stage") != stage or report.get("mode") != "live"
        or report.get("is_valid") is not True or report.get("performed") is not True
        or report.get("plan_id") != plan["plan_id"] or report.get("actions") != [action]
        or report.get("action_id") != action or type(report.get("tap_count")) is not int or report["tap_count"] != 1
        or report.get("stop_reason") != "batch_close_success_probe_complete"
        or report.get("final_confirmation_report_sha256") != final_report_sha256
        or receipt != {"schema_version": "uma.skill_purchase_atomic_live_session.v1",
            "status": "consumed_before_device_connect", "stage": stage, "action_id": action,
            "plan_id": plan["plan_id"], "plan_sha256": file_sha256(plan_path), "review_sha256": file_sha256(review_path),
            "session_id": promotion["session_id"], "capture_batch_id": promotion["fresh_capture_id"],
            "final_confirmation_report_sha256": final_report_sha256}
    ):
        raise ValueError("batch_success_report_mismatch")
    frames = report.get("captured_frames")
    if not isinstance(frames, list) or len(frames) != 2:
        raise ValueError("batch_success_frames_missing")
    for index, prefix in enumerate(("pre", "post")):
        frame = bound_file(root, report.get(prefix + "_frame_path"), report.get(prefix + "_frame_sha256"))
        if frame != bound_file(root, frames[index], report[prefix + "_frame_sha256"]):
            raise ValueError("batch_success_frames_mismatch")
    return {**report, "report_sha256": file_sha256(report_path)}
