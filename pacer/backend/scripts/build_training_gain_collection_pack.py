"""Build training gain targeted collection pack.

Reads gap_analysis.json and generates a collection plan,
task list, group progress template, and review checklist.

Strictly read-only: no OCR, no ADB, no auto-labeling, no model training.

Usage:
    python scripts/build_training_gain_collection_pack.py \\
        --gap-analysis path/to/gap_analysis.json \\
        --output-dir path/to/output \\
        --planned-groups 8 \\
        [--progress-manifest path/to/progress.json] \\
        [--overwrite]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLLECTION_PACK_SCHEMA = "training_gain_targeted_collection_pack.v1"
GAP_ANALYSIS_SCHEMA = "training_gain_dataset_gap_analysis.v1"
PROGRESS_SCHEMA = "training_gain_targeted_collection_progress.v1"

PLANNED_GROUPS_DEFAULT = 8

VALID_PRIORITY_LEVELS = {"high", "medium", "low", "sufficient"}
VALID_GROUP_STATUSES = {"planned", "in_progress", "completed"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def validate_gap_analysis(data: dict) -> None:
    """Validate gap analysis with full cross-validation."""
    schema = data.get("schema")
    if schema != GAP_ANALYSIS_SCHEMA:
        raise ValueError(
            f"Gap analysis schema mismatch: expected '{GAP_ANALYSIS_SCHEMA}', "
            f"got '{schema}'"
        )

    if "class_rows" not in data:
        raise ValueError("Gap analysis missing 'class_rows' key")
    class_rows = data["class_rows"]
    if not isinstance(class_rows, list) or not class_rows:
        raise ValueError("Gap analysis class_rows must be a non-empty list")

    summary = data.get("dataset_summary", {})
    collection_plan = data.get("collection_plan", {})

    # Per-row validation
    seen_labels: set[int] = set()
    sum_samples = 0
    sum_add_samples = 0
    sum_add_groups = 0
    derived_priority: dict[str, list[int]] = {"high": [], "medium": [], "low": [], "sufficient": []}
    derived_zero_recall: list[int] = []

    for i, row in enumerate(class_rows):
        if not isinstance(row, dict):
            raise ValueError(f"class_rows[{i}] is not a dict")

        label = row.get("label")
        if not isinstance(label, int) or isinstance(label, bool):
            raise ValueError(f"class_rows[{i}].label must be int, got {type(label).__name__}")
        if label in seen_labels:
            raise ValueError(f"Duplicate label in class_rows: {label}")
        seen_labels.add(label)

        sample_count = row.get("sample_count")
        if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 0:
            raise ValueError(f"class_rows[{i}] ({label}): sample_count must be non-negative int")

        group_count = row.get("group_count")
        if not isinstance(group_count, int) or isinstance(group_count, bool) or group_count < 0:
            raise ValueError(f"class_rows[{i}] ({label}): group_count must be non-negative int")

        recall = row.get("recall")
        if recall is not None:
            if not isinstance(recall, (int, float)) or isinstance(recall, bool):
                raise ValueError(f"class_rows[{i}] ({label}): recall must be number or null")
            if recall < 0.0 or recall > 1.0:
                raise ValueError(f"class_rows[{i}] ({label}): recall must be in [0,1]")

        zero_recall = row.get("zero_recall")
        if not isinstance(zero_recall, bool):
            raise ValueError(f"class_rows[{i}] ({label}): zero_recall must be bool")

        # Cross-validate zero_recall vs recall
        if zero_recall and recall is not None and recall != 0.0:
            raise ValueError(
                f"class_rows[{i}] ({label}): zero_recall=true but recall={recall}"
            )
        if not zero_recall and recall is not None and recall == 0.0:
            raise ValueError(
                f"class_rows[{i}] ({label}): recall=0 but zero_recall=false"
            )

        priority = row.get("priority_level")
        if priority not in VALID_PRIORITY_LEVELS:
            raise ValueError(
                f"class_rows[{i}] ({label}): invalid priority_level '{priority}'"
            )
        derived_priority[priority].append(label)

        if zero_recall:
            derived_zero_recall.append(label)

        add_samples = row.get("recommended_additional_samples", 0)
        if not isinstance(add_samples, int) or isinstance(add_samples, bool) or add_samples < 0:
            raise ValueError(f"class_rows[{i}] ({label}): recommended_additional_samples must be non-negative int")

        add_groups = row.get("recommended_additional_groups", 0)
        if not isinstance(add_groups, int) or isinstance(add_groups, bool) or add_groups < 0:
            raise ValueError(f"class_rows[{i}] ({label}): recommended_additional_groups must be non-negative int")

        sum_samples += sample_count
        sum_add_samples += add_samples
        sum_add_groups += add_groups

    # Cross-validate class count
    expected_class_count = summary.get("clean_class_count")
    if expected_class_count is not None and len(class_rows) != expected_class_count:
        raise ValueError(
            f"class_rows count ({len(class_rows)}) != "
            f"dataset_summary.clean_class_count ({expected_class_count})"
        )

    # Cross-validate sample sum
    expected_total = summary.get("clean_total_items")
    if expected_total is not None and sum_samples != expected_total:
        raise ValueError(
            f"sum(sample_count) ({sum_samples}) != "
            f"dataset_summary.clean_total_items ({expected_total})"
        )

    # Cross-validate recommended samples total
    plan_total_samples = collection_plan.get("total_recommended_additional_samples")
    if plan_total_samples is not None and sum_add_samples != plan_total_samples:
        raise ValueError(
            f"sum(recommended_additional_samples) ({sum_add_samples}) != "
            f"collection_plan.total_recommended_additional_samples ({plan_total_samples})"
        )

    # Cross-validate recommended groups total
    plan_total_groups = collection_plan.get("total_recommended_additional_groups")
    if plan_total_groups is not None and sum_add_groups != plan_total_groups:
        raise ValueError(
            f"sum(recommended_additional_groups) ({sum_add_groups}) != "
            f"collection_plan.total_recommended_additional_groups ({plan_total_groups})"
        )

    # Cross-validate priority sets
    for level in VALID_PRIORITY_LEVELS:
        key = f"{level}_priority_labels"
        declared = collection_plan.get(key)
        if declared is not None:
            declared_set = sorted(declared)
            derived_set = sorted(derived_priority[level])
            if declared_set != derived_set:
                raise ValueError(
                    f"collection_plan.{key} {declared_set} != "
                    f"derived from class_rows {derived_set}"
                )

    # Cross-validate zero-recall set
    declared_zero = summary.get("zero_recall_labels")
    if declared_zero is not None:
        declared_zero_set = sorted(declared_zero)
        derived_zero_set = sorted(derived_zero_recall)
        if declared_zero_set != derived_zero_set:
            raise ValueError(
                f"dataset_summary.zero_recall_labels {declared_zero_set} != "
                f"derived from class_rows {derived_zero_set}"
            )

    # Cross-validate clean classes list
    declared_classes = summary.get("clean_classes")
    if declared_classes is not None:
        row_labels = sorted(seen_labels)
        if sorted(declared_classes) != row_labels:
            raise ValueError(
                f"dataset_summary.clean_classes {sorted(declared_classes)} != "
                f"class_rows labels {row_labels}"
            )


def validate_progress_manifest(progress: dict, valid_labels: set[int], planned_group_ids: set[str]) -> None:
    """Validate progress manifest against collection pack."""
    schema = progress.get("schema")
    if schema != PROGRESS_SCHEMA:
        raise ValueError(
            f"Progress schema mismatch: expected '{PROGRESS_SCHEMA}', got '{schema}'"
        )

    groups = progress.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Progress 'groups' must be a list")

    seen_group_ids: set[str] = set()
    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"groups[{i}] is not a dict")

        gid = group.get("planned_group_id")
        if not gid or not isinstance(gid, str):
            raise ValueError(f"groups[{i}].planned_group_id must be non-empty string")
        if gid in seen_group_ids:
            raise ValueError(f"Duplicate planned_group_id: {gid}")
        seen_group_ids.add(gid)
        if gid not in planned_group_ids:
            raise ValueError(f"Unknown planned_group_id: {gid}")

        # Validate statuses
        for status_field in ["capture_status", "review_status", "aggregate_status",
                             "quality_audit_status", "clean_status"]:
            val = group.get(status_field)
            if val not in VALID_GROUP_STATUSES:
                raise ValueError(f"groups[{i}] ({gid}): invalid {status_field} '{val}'")

        # Status ordering: review completed implies capture completed, etc.
        status_order = ["capture_status", "review_status", "aggregate_status",
                        "quality_audit_status", "clean_status"]
        status_rank = {"planned": 0, "in_progress": 1, "completed": 2}
        for j in range(1, len(status_order)):
            prev = group.get(status_order[j - 1], "planned")
            curr = group.get(status_order[j], "planned")
            if status_rank.get(curr, 0) > status_rank.get(prev, 0):
                raise ValueError(
                    f"groups[{i}] ({gid}): {status_order[j]}='{curr}' but "
                    f"{status_order[j - 1]}='{prev}'"
                )

        labels = group.get("labels")
        if not isinstance(labels, list):
            raise ValueError(f"groups[{i}] ({gid}): 'labels' must be a list")

        seen_labels_in_group: set[int] = set()
        for k, label_entry in enumerate(labels):
            if not isinstance(label_entry, dict):
                raise ValueError(f"groups[{i}] ({gid}).labels[{k}] is not a dict")

            lbl = label_entry.get("label")
            if not isinstance(lbl, int) or isinstance(lbl, bool):
                raise ValueError(f"groups[{i}] ({gid}).labels[{k}].label must be int")
            if lbl not in valid_labels:
                raise ValueError(f"groups[{i}] ({gid}).labels[{k}]: unknown label {lbl}")
            if lbl in seen_labels_in_group:
                raise ValueError(f"groups[{i}] ({gid}): duplicate label {lbl}")
            seen_labels_in_group.add(lbl)

            for count_field in ["reviewed_count", "clean_count", "duplicate_count", "conflict_count"]:
                val = label_entry.get(count_field, 0)
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    raise ValueError(
                        f"groups[{i}] ({gid}).labels[{k}].{count_field} must be non-negative int"
                    )

            reviewed = label_entry.get("reviewed_count", 0)
            clean = label_entry.get("clean_count", 0)
            duplicate = label_entry.get("duplicate_count", 0)
            conflict = label_entry.get("conflict_count", 0)

            if clean > reviewed:
                raise ValueError(
                    f"groups[{i}] ({gid}).labels[{k}]: clean_count ({clean}) > reviewed_count ({reviewed})"
                )
            if clean + duplicate + conflict > reviewed:
                raise ValueError(
                    f"groups[{i}] ({gid}).labels[{k}]: clean+duplicate+conflict ({clean + duplicate + conflict}) > reviewed ({reviewed})"
                )


def compute_class_targets(
    class_rows: list[dict],
    planned_groups: int,
    minimum_target_count: int = 8,
    minimum_target_groups: int = 3,
) -> list[dict]:
    """Compute per-class collection targets."""
    targets = []
    for row in class_rows:
        label = row["label"]
        current_samples = row["sample_count"]
        current_groups = row["group_count"]
        current_recall = row.get("recall")
        zero_recall = row.get("zero_recall", False)
        priority_level = row.get("priority_level", "low")
        recommended_additional_samples = row.get("recommended_additional_samples", 0)
        recommended_additional_groups = row.get("recommended_additional_groups", 0)
        reason_codes = row.get("reason_codes", [])
        top_confused = row.get("top_confused_with", [])

        # Skip labels with 0 samples and 0 recommended (e.g., label 12)
        if current_samples == 0 and recommended_additional_samples == 0 and recommended_additional_groups == 0:
            continue

        targets.append({
            "label": label,
            "priority_level": priority_level,
            "current_sample_count": current_samples,
            "current_group_count": current_groups,
            "current_recall": current_recall,
            "zero_recall": zero_recall,
            "recommended_additional_samples": recommended_additional_samples,
            "recommended_additional_groups": recommended_additional_groups,
            "planned_sample_target": recommended_additional_samples,
            "planned_group_target": recommended_additional_groups,
            "remaining_sample_deficit": max(0, recommended_additional_samples),
            "remaining_group_deficit": max(0, recommended_additional_groups),
            "reason_codes": reason_codes,
            "top_confused_with": top_confused,
        })

    priority_order = {"high": 0, "medium": 1, "low": 2, "sufficient": 3}
    targets.sort(key=lambda t: (priority_order.get(t["priority_level"], 9), t["label"]))

    return targets


def apply_progress(
    class_targets: list[dict],
    progress: dict,
) -> dict:
    """Apply progress manifest. Returns per-label and global stats."""
    valid_labels = {t["label"] for t in class_targets}
    planned_group_ids = set()
    for i in range(1, len(class_targets) + PLANNED_GROUPS_DEFAULT + 1):
        planned_group_ids.add(f"planned_group_{i:02d}")

    validate_progress_manifest(progress, valid_labels, planned_group_ids)

    # Aggregate per-label clean counts and group coverage
    per_label_clean: dict[int, int] = {}
    per_label_groups: dict[int, set[str]] = {}
    completed_groups = 0

    for group in progress.get("groups", []):
        gid = group["planned_group_id"]
        has_clean = False
        for label_entry in group.get("labels", []):
            lbl = label_entry["label"]
            clean = label_entry.get("clean_count", 0)
            if clean > 0:
                per_label_clean[lbl] = per_label_clean.get(lbl, 0) + clean
                if lbl not in per_label_groups:
                    per_label_groups[lbl] = set()
                per_label_groups[lbl].add(gid)
                has_clean = True
        if group.get("clean_status") == "completed" and has_clean:
            completed_groups += 1

    # Update deficits
    for target in class_targets:
        label = target["label"]
        total_clean = per_label_clean.get(label, 0)
        group_count = len(per_label_groups.get(label, set()))
        target["remaining_sample_deficit"] = max(0, target["planned_sample_target"] - total_clean)
        target["remaining_group_deficit"] = max(0, target["planned_group_target"] - group_count)

    return {
        "completed_global_groups": completed_groups,
        "per_label_clean": per_label_clean,
        "per_label_groups": {k: sorted(v) for k, v in per_label_groups.items()},
    }


def build_collection_plan(
    gap_analysis: dict,
    planned_groups: int,
    gap_analysis_path: Path,
    progress: dict | None = None,
) -> dict:
    """Build the full collection plan."""
    params = gap_analysis.get("parameters", {})
    summary = gap_analysis.get("dataset_summary", {})
    collection_plan = gap_analysis.get("collection_plan", {})
    class_rows = gap_analysis.get("class_rows", [])

    class_targets = compute_class_targets(
        class_rows,
        planned_groups,
        params.get("minimum_target_count", 8),
        params.get("minimum_target_groups", 3),
    )

    total_new_samples = sum(t["planned_sample_target"] for t in class_targets)
    total_group_deficit = sum(t["planned_group_target"] for t in class_targets)

    high_priority_labels = collection_plan.get("high_priority_labels", [])
    zero_recall_labels = summary.get("zero_recall_labels", [])

    # Apply progress if provided
    progress_stats = None
    if progress:
        progress_stats = apply_progress(class_targets, progress)

    completion_rules = [
        "High-priority labels must reach at least 8 clean samples and 3 independent groups.",
        "One group can satisfy multiple label requirements simultaneously.",
        "Only human-reviewed and clean-pipeline-approved samples count toward completion.",
        "Duplicates and conflicts do not count toward clean completion.",
        "82 is a heuristic next-round target, not a statistical guarantee.",
        "Actual collection must prioritize high-priority labels first.",
        "Do not mark a group as complete for a label unless that label was actually observed.",
        "Progress updates require explicit progress-manifest; no auto-scanning.",
        "Global completed groups count requires clean_status=completed AND at least one clean label.",
    ]

    limitations = [
        "Recall values are derived from LOGOO benchmark aggregate metrics.",
        "Priority score is a collection heuristic, not a statistical confidence measure.",
        "recommended_additional_samples represents a minimum next-round collection target.",
        "No automatic label filling or OCR-based annotation.",
        "Progress tracking requires human-maintained progress manifest.",
    ]

    plan = {
        "schema": COLLECTION_PACK_SCHEMA,
        "source_gap_analysis_path": str(gap_analysis_path),
        "source_gap_analysis_sha256": _sha256_file(gap_analysis_path),
        "source_parameters": params,
        "existing_dataset_summary": {
            "clean_sample_count": summary.get("clean_total_items", 0),
            "clean_class_count": summary.get("clean_class_count", 0),
            "clean_group_count": summary.get("clean_group_count", 0),
            "benchmark_balanced_accuracy": summary.get("benchmark_balanced_accuracy"),
        },
        "global_targets": {
            "planned_groups": planned_groups,
            "target_new_clean_samples": total_new_samples,
            "class_level_group_allocation_deficit": total_group_deficit,
            "high_priority_labels": high_priority_labels,
            "zero_recall_labels": zero_recall_labels,
        },
        "class_targets": class_targets,
        "planned_groups": planned_groups,
        "completion_rules": completion_rules,
        "limitations": limitations,
    }

    if progress_stats:
        plan["progress"] = {
            "completed_global_groups": progress_stats["completed_global_groups"],
            "global_8_group_target_met": progress_stats["completed_global_groups"] >= 8,
        }

    return plan


def write_collection_tasks_csv(class_targets: list[dict], output_path: Path) -> int:
    """Write collection tasks CSV. Returns row count."""
    fieldnames = [
        "priority_rank",
        "label",
        "priority_level",
        "current_samples",
        "target_new_samples",
        "current_groups",
        "target_new_group_allocations",
        "zero_recall",
        "top_confused_with",
        "reason_codes",
        "status",
        "notes",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rank, target in enumerate(class_targets, 1):
            confused = "; ".join(
                f"{tc['predicted_label']}({tc.get('error_count', 0)})"
                for tc in target.get("top_confused_with", [])
            )
            reasons = "; ".join(target.get("reason_codes", []))
            writer.writerow({
                "priority_rank": rank,
                "label": target["label"],
                "priority_level": target["priority_level"],
                "current_samples": target["current_sample_count"],
                "target_new_samples": target["planned_sample_target"],
                "current_groups": target["current_group_count"],
                "target_new_group_allocations": target["planned_group_target"],
                "zero_recall": target["zero_recall"],
                "top_confused_with": confused,
                "reason_codes": reasons,
                "status": "pending",
                "notes": "",
            })

    return len(class_targets)


def write_group_progress_template(planned_groups: int, output_path: Path) -> int:
    """Write group progress template CSV. Returns row count."""
    fieldnames = [
        "planned_group_id",
        "capture_status",
        "review_status",
        "aggregate_status",
        "quality_audit_status",
        "clean_status",
        "source_screenshot_count",
        "reviewed_component_count",
        "clean_component_count",
        "labels_observed",
        "high_priority_labels_observed",
        "duplicate_count",
        "conflict_count",
        "notes",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for i in range(1, planned_groups + 1):
            group_id = f"planned_group_{i:02d}"
            writer.writerow({
                "planned_group_id": group_id,
                "capture_status": "planned",
                "review_status": "planned",
                "aggregate_status": "planned",
                "quality_audit_status": "planned",
                "clean_status": "planned",
                "source_screenshot_count": "",
                "reviewed_component_count": "",
                "clean_component_count": "",
                "labels_observed": "",
                "high_priority_labels_observed": "",
                "duplicate_count": "",
                "conflict_count": "",
                "notes": "",
            })

    return planned_groups


def write_review_checklist_md(plan: dict, output_path: Path) -> None:
    """Write review checklist markdown."""
    summary = plan.get("existing_dataset_summary", {})
    targets = plan.get("global_targets", {})
    class_targets = plan.get("class_targets", [])
    high_priority = targets.get("high_priority_labels", [])
    zero_recall = targets.get("zero_recall_labels", [])

    lines = [
        "# Training Gain Collection Pack — Review Checklist",
        "",
        "## Global Targets",
        "",
        f"- Planned groups: {targets.get('planned_groups', 0)}",
        f"- Target new clean samples: {targets.get('target_new_clean_samples', 0)}",
        f"- Class-level group allocation deficit: {targets.get('class_level_group_allocation_deficit', 0)}",
        f"- Existing clean samples: {summary.get('clean_sample_count', 0)}",
        f"- Existing clean classes: {summary.get('clean_class_count', 0)}",
        f"- Existing groups: {summary.get('clean_group_count', 0)}",
        "",
        "## High-Priority Labels",
        "",
        "These labels need the most attention:",
        "",
    ]
    for label in high_priority:
        ct = next((t for t in class_targets if t["label"] == label), None)
        if ct:
            lines.append(
                f"- **{label}**: current={ct['current_sample_count']}, "
                f"target={ct['planned_sample_target']} new, "
                f"groups={ct['current_group_count']}/{ct['planned_group_target']} new"
            )

    lines.extend([
        "",
        "## Zero-Recall Labels",
        "",
        "These labels have zero recall in the benchmark:",
        "",
    ])
    for label in zero_recall:
        lines.append(f"- {label}")

    lines.extend([
        "",
        "## Collection Rules",
        "",
        "1. Prioritize high-priority labels first.",
        "2. One group can satisfy multiple label requirements.",
        "3. Only human-reviewed and clean-pipeline-approved samples count.",
        "4. Duplicates and conflicts do not count toward clean completion.",
        "5. Do not auto-fill labels or use OCR for annotation.",
        "6. Mark group as complete for a label only when actually observed.",
        "7. 82 is a heuristic target, not a statistical guarantee.",
        "",
        "## Special Attention Labels",
        "",
        "Labels 14, 15, 19, 21, 23, 24 require special attention.",
        "",
        "## Completion Criteria",
        "",
        "- High-priority: at least 8 clean samples, at least 3 independent groups.",
        "- Medium-priority: fill frequency and group deficits.",
        "- Low/sufficient: maintain current levels, no active collection needed.",
        "- Global 8-group target is independent of class-level allocations.",
        "",
    ])

    output_path.write_text("\n".join(lines), encoding="utf-8")


def _write_outputs(output_dir: Path, plan: dict, planned_groups: int, pretty: bool) -> dict:
    """Write all output files to output_dir. Returns result dict."""
    indent = 2 if pretty else None

    plan_path = output_dir / "collection_plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=indent), encoding="utf-8")

    tasks_path = output_dir / "collection_tasks.csv"
    task_count = write_collection_tasks_csv(plan["class_targets"], tasks_path)

    template_path = output_dir / "group_progress_template.csv"
    template_count = write_group_progress_template(planned_groups, template_path)

    checklist_path = output_dir / "review_checklist.md"
    write_review_checklist_md(plan, checklist_path)

    return {
        "schema": COLLECTION_PACK_SCHEMA,
        "output_dir": str(output_dir),
        "planned_groups": planned_groups,
        "target_new_clean_samples": plan["global_targets"]["target_new_clean_samples"],
        "class_level_group_allocation_deficit": plan["global_targets"]["class_level_group_allocation_deficit"],
        "high_priority_labels": plan["global_targets"]["high_priority_labels"],
        "zero_recall_labels": plan["global_targets"]["zero_recall_labels"],
        "collection_tasks_count": task_count,
        "group_template_count": template_count,
        "output_files": [str(plan_path), str(tasks_path), str(template_path), str(checklist_path)],
    }


def _verify_output(output_dir: Path, planned_groups: int) -> None:
    """Verify output files exist and are self-consistent."""
    for name in ["collection_plan.json", "collection_tasks.csv",
                 "group_progress_template.csv", "review_checklist.md"]:
        if not (output_dir / name).exists():
            raise RuntimeError(f"Output verification failed: {name} missing")

    plan = json.loads((output_dir / "collection_plan.json").read_text(encoding="utf-8"))
    if plan.get("schema") != COLLECTION_PACK_SCHEMA:
        raise RuntimeError("Output verification failed: wrong schema")

    with open(output_dir / "group_progress_template.csv", "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != planned_groups:
        raise RuntimeError(f"Output verification failed: template rows {len(rows)} != {planned_groups}")


def run_collection_pack(
    gap_analysis_path: Path,
    output_dir: Path,
    planned_groups: int = PLANNED_GROUPS_DEFAULT,
    progress_manifest_path: Path | None = None,
    overwrite: bool = False,
    pretty: bool = False,
) -> dict:
    """Build the collection pack with transactional overwrite."""
    # Validate inputs
    if not gap_analysis_path.exists():
        raise FileNotFoundError(f"Gap analysis not found: {gap_analysis_path}")

    gap_data = json.loads(gap_analysis_path.read_text(encoding="utf-8"))
    validate_gap_analysis(gap_data)

    if planned_groups < 1:
        raise ValueError(f"planned_groups must be >= 1, got {planned_groups}")

    # Load progress if provided
    progress = None
    if progress_manifest_path:
        if not progress_manifest_path.exists():
            raise FileNotFoundError(f"Progress manifest not found: {progress_manifest_path}")
        progress = json.loads(progress_manifest_path.read_text(encoding="utf-8"))
        validate_progress_manifest(
            progress,
            {t["label"] for t in compute_class_targets(gap_data["class_rows"], planned_groups)},
            {f"planned_group_{i:02d}" for i in range(1, planned_groups + 1)},
        )

    # Check output dir
    if output_dir.exists() and not overwrite:
        if any(output_dir.iterdir()):
            raise FileExistsError(f"Output dir not empty: {output_dir} (use --overwrite)")

    # Build plan
    plan = build_collection_plan(gap_data, planned_groups, gap_analysis_path, progress)

    # Transactional write
    if overwrite and output_dir.exists():
        staging_dir = output_dir.parent / f".staging_{output_dir.name}"
        backup_dir = output_dir.parent / f".backup_{output_dir.name}"
        try:
            # Clean any leftover staging/backup
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            if backup_dir.exists():
                shutil.rmtree(backup_dir)

            staging_dir.mkdir(parents=True)
            result = _write_outputs(staging_dir, plan, planned_groups, pretty)
            _verify_output(staging_dir, planned_groups)

            # Atomic replace
            output_dir.rename(backup_dir)
            staging_dir.rename(output_dir)

            # Clean backup
            shutil.rmtree(backup_dir)
            return result
        except Exception:
            # Restore backup if rename failed
            if backup_dir.exists() and not output_dir.exists():
                backup_dir.rename(output_dir)
            # Clean staging
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            raise
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        return _write_outputs(output_dir, plan, planned_groups, pretty)


# ---------- CLI ----------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build training gain targeted collection pack."
    )
    parser.add_argument("--gap-analysis", required=True, help="Path to gap_analysis.json.")
    parser.add_argument("--output-dir", required=True, help="Output directory.")
    parser.add_argument("--planned-groups", type=int, default=PLANNED_GROUPS_DEFAULT,
                         help=f"Number of planned new groups (default: {PLANNED_GROUPS_DEFAULT}).")
    parser.add_argument("--progress-manifest", help="Path to progress manifest JSON.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting existing output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        result = run_collection_pack(
            Path(args.gap_analysis),
            Path(args.output_dir),
            args.planned_groups,
            Path(args.progress_manifest) if args.progress_manifest else None,
            args.overwrite,
            args.pretty,
        )
    except (FileNotFoundError, ValueError, FileExistsError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
