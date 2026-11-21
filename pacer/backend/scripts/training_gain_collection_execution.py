"""Training gain collection execution manifest validation and progress derivation.

Validates execution facts and deterministically derives
training_gain_targeted_collection_progress.v1 from them.

Strictly read-only: no OCR, no ADB, no auto-labeling.

Usage:
    python scripts/training_gain_collection_execution.py \\
        --manifest path/to/execution_manifest.json \\
        --collection-plan path/to/collection_plan.json \\
        [--output-dir path/to/output] \\
        [--pretty]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXECUTION_SCHEMA = "training_gain_collection_execution_manifest.v1"
PROGRESS_SCHEMA = "training_gain_targeted_collection_progress.v1"

VALID_GROUP_REVIEW_STATUSES = {"pending", "reviewed", "quarantined", "rejected"}
VALID_REVIEW_STATUSES = {"pending", "reviewed", "quarantined", "rejected"}
VALID_DISPOSITIONS = {"clean", "duplicate", "conflict", "invalid", "none"}

ACTUAL_GROUP_ID_RE = re.compile(r"^tg_group_[0-9]{8}_[0-9]{3,}$")
SOURCE_SCREEN_RE = re.compile(r"^[a-z0-9_]{1,32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(\.\d+)?"
    r"(Z|[+-]\d{2}:\d{2})$"
)

TOP_LEVEL_KEYS = {"schema", "source_collection_plan", "groups"}
SOURCE_PLAN_KEYS = {"path", "sha256"}
GROUP_KEYS = {
    "planned_slot_id", "actual_group_id", "run_session_id", "turn_index",
    "source_state_key", "capture_started_at", "capture_completed_at",
    "operator_id", "group_review_status", "reviewer_id", "reviewed_at",
    "planned_target_labels", "raw_files", "review_items", "conflict_records",
    "provenance",
}
RAW_FILE_KEYS = {
    "relative_path", "sha256", "size_bytes", "media_type", "original_extension",
    "sequence", "source_screen", "captured_at",
}
REVIEW_ITEM_KEYS = {
    "item_id", "raw_sha256", "sample_id", "sample_relative_path", "sample_sha256",
    "label", "review_status", "disposition", "duplicate_of_item_id",
    "reviewer_id", "reviewed_at", "provenance",
}
CONFLICT_RECORD_KEYS = {
    "conflict_id", "sample_sha256", "raw_sha256", "related_item_ids",
    "conflicting_labels", "reviewer_conclusion", "quarantine_reason",
    "reviewed_at", "provenance",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_unknown_fields(data: dict, allowed: set[str], context: str) -> None:
    unknown = set(data.keys()) - allowed
    if unknown:
        raise ValueError(f"{context}: unknown fields: {sorted(unknown)}")


def _normalize_path(rp: str) -> str:
    normalized = rp.replace("\\", "/")
    parts = []
    for part in PurePosixPath(normalized).parts:
        if part == "..":
            raise ValueError(f"Path traversal detected: {rp}")
        if part != ".":
            parts.append(part)
    return "/".join(parts)


def _validate_relative_path(rp: str, context: str) -> str:
    if not rp or not isinstance(rp, str):
        raise ValueError(f"{context}: relative_path must be non-empty string")
    check = rp.replace("\\", "/")
    if check.startswith("/"):
        raise ValueError(f"{context}: absolute path rejected: {rp}")
    if len(check) >= 2 and check[1] == ":":
        raise ValueError(f"{context}: drive-qualified path rejected: {rp}")
    if check.startswith("//"):
        raise ValueError(f"{context}: UNC path rejected: {rp}")
    raw_parts = check.split("/")
    if ".." in raw_parts:
        raise ValueError(f"{context}: path traversal rejected: {rp}")
    normalized = _normalize_path(rp)
    if not normalized:
        raise ValueError(f"{context}: empty path after normalization: {rp}")
    return normalized


def _validate_iso8601(value: str, context: str) -> None:
    if not value or not isinstance(value, str):
        raise ValueError(f"{context}: timestamp must be non-empty string")
    if not ISO8601_RE.match(value):
        raise ValueError(
            f"{context}: invalid ISO 8601 timestamp (must have timezone): {value}"
        )
    try:
        dt_str = value
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1]
        elif "+" in dt_str[10:] or (dt_str[10:].count("-") >= 1):
            idx = max(dt_str.rfind("+"), dt_str.rfind("-"))
            if idx > 10:
                dt_str = dt_str[:idx]
        datetime.fromisoformat(dt_str)
    except (ValueError, IndexError):
        raise ValueError(f"{context}: impossible date/time: {value}")


def _validate_timestamp_range(
    start: str | None, end: str | None, context: str
) -> None:
    if start and end:
        _validate_iso8601(start, f"{context} start")
        _validate_iso8601(end, f"{context} end")
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if s > e:
                raise ValueError(f"{context}: start ({start}) > end ({end})")
        except ValueError as ex:
            if "start" in str(ex) and ">" in str(ex):
                raise


def validate_execution_manifest(
    manifest: dict, valid_labels: set[int], planned_slots: set[str],
) -> None:
    """Validate execution manifest facts. Raises ValueError on any violation."""
    _check_unknown_fields(manifest, TOP_LEVEL_KEYS, "manifest")

    schema = manifest.get("schema")
    if schema != EXECUTION_SCHEMA:
        raise ValueError(
            f"Execution schema mismatch: expected '{EXECUTION_SCHEMA}', got '{schema}'"
        )

    source = manifest.get("source_collection_plan")
    if not isinstance(source, dict):
        raise ValueError("source_collection_plan must be a dict")
    _check_unknown_fields(source, SOURCE_PLAN_KEYS, "source_collection_plan")
    if not source.get("path") or not isinstance(source["path"], str):
        raise ValueError("source_collection_plan.path must be non-empty string")
    sha = source.get("sha256")
    if not sha or not isinstance(sha, str) or not SHA256_RE.match(sha):
        raise ValueError("source_collection_plan.sha256 must be 64-char hex string")

    groups = manifest.get("groups")
    if not isinstance(groups, list):
        raise ValueError("groups must be a list")

    # Global tracking
    seen_actual_ids: set[str] = set()
    seen_planned_slots_in_manifest: set[str] = set()
    seen_source_states: set[str] = set()
    seen_item_ids: set[str] = set()
    seen_sample_ids: set[str] = set()
    seen_sample_paths: set[str] = set()
    global_raw_shas: dict[str, str] = {}
    global_raw_paths: dict[str, str] = {}
    global_sample_shas: dict[str, str] = {}
    all_review_items: dict[str, dict] = {}
    all_review_items_by_group: dict[str, set[str]] = defaultdict(set)
    all_conflict_ids: set[str] = set()

    # Index: sample_sha -> list of (item_id, label, disposition, review_status)
    items_by_sample_sha: dict[str, list[dict]] = defaultdict(list)
    conflict_by_sample_sha: dict[str, dict] = {}

    for i, group in enumerate(groups):
        _check_unknown_fields(group, GROUP_KEYS, f"groups[{i}]")

        slot = group.get("planned_slot_id")
        if slot:
            if not isinstance(slot, str):
                raise ValueError(f"groups[{i}].planned_slot_id must be string or empty")
            if slot not in planned_slots:
                raise ValueError(f"groups[{i}].planned_slot_id unknown: {slot}")
            if slot in seen_planned_slots_in_manifest:
                raise ValueError(f"Duplicate planned_slot_id in manifest: {slot}")
            seen_planned_slots_in_manifest.add(slot)

        _validate_group(
            group, i, valid_labels,
            seen_actual_ids, seen_source_states,
            seen_item_ids, seen_sample_ids, seen_sample_paths,
            global_raw_shas, global_raw_paths, global_sample_shas,
            all_review_items, all_review_items_by_group, all_conflict_ids,
            items_by_sample_sha,
        )

        # Slot lifecycle invariant: non-empty slot requires reviewed+clean
        if slot:
            grs = group.get("group_review_status")
            if grs != "reviewed":
                raise ValueError(
                    f"groups[{i}] ({group.get('actual_group_id', '')}): "
                    f"planned_slot_id requires group_review_status='reviewed', got '{grs}'"
                )
            # Check for at least one canonical clean item
            has_clean = False
            for item in group.get("review_items", []):
                if (isinstance(item, dict)
                        and item.get("review_status") == "reviewed"
                        and item.get("disposition") == "clean"):
                    has_clean = True
                    break
            if not has_clean:
                raise ValueError(
                    f"groups[{i}] ({group.get('actual_group_id', '')}): "
                    f"planned_slot_id requires at least one canonical clean review item"
                )

        # Index conflicts by sample_sha
        for cr in group.get("conflict_records", []):
            if isinstance(cr, dict):
                cr_sha = cr.get("sample_sha256", "")
                if cr_sha:
                    if cr_sha in conflict_by_sample_sha:
                        raise ValueError(
                            f"Duplicate conflict record for sample_sha256 {cr_sha[:16]}..."
                        )
                    conflict_by_sample_sha[cr_sha] = cr

    # Pass 2: validate conflict references
    for i, group in enumerate(groups):
        agid = group.get("actual_group_id", "")
        for j, cr in enumerate(group.get("conflict_records", [])):
            _validate_conflict_references(
                cr, i, agid, j, all_review_items,
                all_review_items_by_group.get(agid, set()),
            )

    # Pass 3: global same-SHA invariants
    _validate_sample_sha_invariants(items_by_sample_sha, conflict_by_sample_sha)


def _validate_sample_sha_invariants(
    items_by_sample_sha: dict[str, list[dict]],
    conflict_by_sample_sha: dict[str, dict],
) -> None:
    """Enforce global invariants on sample SHA content identity."""
    for ssha, item_list in items_by_sample_sha.items():
        labels = {it["label"] for it in item_list}
        clean_items = [
            it for it in item_list
            if it.get("disposition") == "clean" and it.get("review_status") == "reviewed"
        ]

        if len(labels) == 1:
            # Single-label: max 1 canonical clean
            if len(clean_items) > 1:
                iids = [it["item_id"] for it in clean_items]
                raise ValueError(
                    f"Same sample_sha256 {ssha[:16]}... with same label "
                    f"has {len(clean_items)} clean items: {iids}. "
                    f"Max 1 canonical clean allowed."
                )
        else:
            # Multi-label: must have conflict record, no clean items
            if ssha not in conflict_by_sample_sha:
                iids = [it["item_id"] for it in item_list]
                raise ValueError(
                    f"Same sample_sha256 {ssha[:16]}... has multiple labels "
                    f"{sorted(labels)} across items {iids} but no conflict record."
                )
            if clean_items:
                iids = [it["item_id"] for it in clean_items]
                raise ValueError(
                    f"Same sample_sha256 {ssha[:16]}... has multiple labels "
                    f"but clean items exist: {iids}. All must be non-clean."
                )
            # Verify conflict covers ALL items
            cr = conflict_by_sample_sha[ssha]
            cr_related = set(cr.get("related_item_ids", []))
            actual_ids = {it["item_id"] for it in item_list}
            if cr_related != actual_ids:
                raise ValueError(
                    f"Conflict record for {ssha[:16]}... covers {sorted(cr_related)} "
                    f"but actual items are {sorted(actual_ids)}. Must cover exactly all."
                )
            cr_labels = sorted(cr.get("conflicting_labels", []))
            if cr_labels != sorted(labels):
                raise ValueError(
                    f"Conflict record for {ssha[:16]}... labels {cr_labels} "
                    f"!= actual labels {sorted(labels)}"
                )


def _validate_group(
    group: dict, idx: int, valid_labels: set[int],
    seen_actual_ids: set[str], seen_source_states: set[str],
    seen_item_ids: set[str], seen_sample_ids: set[str], seen_sample_paths: set[str],
    global_raw_shas: dict[str, str], global_raw_paths: dict[str, str],
    global_sample_shas: dict[str, str],
    all_review_items: dict[str, dict], all_review_items_by_group: dict[str, set[str]],
    all_conflict_ids: set[str],
    items_by_sample_sha: dict[str, list[dict]],
) -> None:
    if not isinstance(group, dict):
        raise ValueError(f"groups[{idx}] is not a dict")

    agid = group.get("actual_group_id")
    if not agid or not isinstance(agid, str):
        raise ValueError(f"groups[{idx}].actual_group_id must be non-empty string")
    if not ACTUAL_GROUP_ID_RE.match(agid):
        raise ValueError(f"groups[{idx}].actual_group_id format invalid: {agid}")
    if agid in seen_actual_ids:
        raise ValueError(f"Duplicate actual_group_id: {agid}")
    seen_actual_ids.add(agid)

    rsid = group.get("run_session_id")
    if not rsid or not isinstance(rsid, str):
        raise ValueError(f"groups[{idx}] ({agid}): run_session_id must be non-empty string")

    ti = group.get("turn_index")
    if not isinstance(ti, int) or isinstance(ti, bool) or ti < 0:
        raise ValueError(f"groups[{idx}] ({agid}): turn_index must be non-negative int")

    ssk = group.get("source_state_key")
    expected_ssk = f"{rsid}_turn{ti}"
    if ssk != expected_ssk:
        raise ValueError(
            f"groups[{idx}] ({agid}): source_state_key '{ssk}' != expected '{expected_ssk}'"
        )
    if ssk in seen_source_states:
        raise ValueError(f"Duplicate source_state_key: {ssk}")
    seen_source_states.add(ssk)

    grs = group.get("group_review_status")
    if grs not in VALID_GROUP_REVIEW_STATUSES:
        raise ValueError(f"groups[{idx}] ({agid}): invalid group_review_status '{grs}'")

    _validate_timestamp_range(
        group.get("capture_started_at"), group.get("capture_completed_at"),
        f"groups[{idx}] ({agid}) capture",
    )
    if group.get("reviewed_at"):
        _validate_iso8601(group["reviewed_at"], f"groups[{idx}] ({agid}) reviewed_at")

    if grs == "reviewed":
        if not group.get("reviewer_id"):
            raise ValueError(f"groups[{idx}] ({agid}): reviewed but no reviewer_id")
        if not group.get("reviewed_at"):
            raise ValueError(f"groups[{idx}] ({agid}): reviewed but no reviewed_at")

    raw_files = group.get("raw_files")
    if not isinstance(raw_files, list):
        raise ValueError(f"groups[{idx}] ({agid}): raw_files must be a list")
    if grs not in ("pending",) and len(raw_files) == 0:
        raise ValueError(f"groups[{idx}] ({agid}): non-pending group has no raw_files")

    group_shas: set[str] = set()
    group_paths: set[str] = set()
    group_seqs: set[int] = set()
    for j, raw in enumerate(raw_files):
        _validate_raw_file(raw, idx, agid, j, group_shas, group_paths, group_seqs,
                          global_raw_shas, global_raw_paths)

    review_items = group.get("review_items")
    if not isinstance(review_items, list):
        raise ValueError(f"groups[{idx}] ({agid}): review_items must be a list")
    for j, item in enumerate(review_items):
        _validate_review_item(
            item, idx, agid, j, valid_labels, seen_item_ids,
            seen_sample_ids, seen_sample_paths, group_shas,
            global_sample_shas, all_review_items, all_review_items_by_group,
            items_by_sample_sha,
        )

    # Validate duplicate references
    group_items = {it["item_id"]: it for it in review_items if isinstance(it, dict) and it.get("item_id")}
    for j, item in enumerate(review_items):
        if not isinstance(item, dict):
            continue
        doi = item.get("duplicate_of_item_id")
        if doi:
            _validate_duplicate_reference(item, j, agid, group_items)

    # Cross-group sample SHA check
    group_sample_shas: dict[str, str] = {}
    for item in review_items:
        if not isinstance(item, dict):
            continue
        ssha = item.get("sample_sha256", "")
        iid = item.get("item_id", "")
        if ssha:
            group_sample_shas[ssha] = iid
    for ssha, iid in group_sample_shas.items():
        if ssha in global_sample_shas and global_sample_shas[ssha] != iid:
            raise ValueError(
                f"groups[{idx}] ({agid}): sample_sha256 {ssha[:16]}... already used by "
                f"{global_sample_shas[ssha]} (cross-group duplicate)"
            )
        global_sample_shas[ssha] = iid

    conflict_records = group.get("conflict_records")
    if not isinstance(conflict_records, list):
        raise ValueError(f"groups[{idx}] ({agid}): conflict_records must be a list")
    for j, cr in enumerate(conflict_records):
        _validate_conflict_record_basic(cr, idx, agid, j, all_conflict_ids)


def _validate_raw_file(
    raw: dict, group_idx: int, agid: str, raw_idx: int,
    group_shas: set[str], group_paths: set[str], group_seqs: set[int],
    global_raw_shas: dict[str, str], global_raw_paths: dict[str, str],
) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"groups[{group_idx}] ({agid}).raw_files[{raw_idx}] is not a dict")
    _check_unknown_fields(raw, RAW_FILE_KEYS,
                         f"groups[{group_idx}] ({agid}).raw_files[{raw_idx}]")

    rp = raw.get("relative_path")
    normalized = _validate_relative_path(rp, f"raw_files[{raw_idx}]")
    if normalized in group_paths:
        raise ValueError(f"raw_files[{raw_idx}]: duplicate relative_path in group: {rp}")
    group_paths.add(normalized)
    if normalized in global_raw_paths:
        raise ValueError(
            f"raw_files[{raw_idx}]: relative_path already used by "
            f"{global_raw_paths[normalized]}: {rp}"
        )
    global_raw_paths[normalized] = agid

    sha = raw.get("sha256")
    if not sha or not isinstance(sha, str) or not SHA256_RE.match(sha):
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): sha256 must be 64-char hex string")
    if sha in group_shas:
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): duplicate sha256 in group: {sha[:16]}")
    group_shas.add(sha)
    if sha in global_raw_shas:
        raise ValueError(
            f"raw_files[{raw_idx}] ({rp}): sha256 already used by "
            f"{global_raw_shas[sha]}: {sha[:16]}"
        )
    global_raw_shas[sha] = agid

    sb = raw.get("size_bytes")
    if not isinstance(sb, int) or isinstance(sb, bool) or sb <= 0:
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): size_bytes must be positive int")

    mt = raw.get("media_type")
    if not mt or not isinstance(mt, str):
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): media_type must be non-empty string")

    ext = raw.get("original_extension")
    if not ext or not isinstance(ext, str):
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): original_extension must be non-empty string")

    seq = raw.get("sequence")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): sequence must be positive int")
    if seq in group_seqs:
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): duplicate sequence: {seq}")
    group_seqs.add(seq)

    ss = raw.get("source_screen")
    if not ss or not isinstance(ss, str) or not SOURCE_SCREEN_RE.match(ss):
        raise ValueError(f"raw_files[{raw_idx}] ({rp}): source_screen must match ^[a-z0-9_]{{1,32}}$")

    ca = raw.get("captured_at")
    if ca:
        _validate_iso8601(ca, f"raw_files[{raw_idx}] ({rp}) captured_at")


def _validate_review_item(
    item: dict, group_idx: int, agid: str, item_idx: int,
    valid_labels: set[int], seen_item_ids: set[str],
    seen_sample_ids: set[str], seen_sample_paths: set[str],
    group_shas: set[str],
    global_sample_shas: dict[str, str],
    all_review_items: dict[str, dict], all_review_items_by_group: dict[str, set[str]],
    items_by_sample_sha: dict[str, list[dict]],
) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"groups[{group_idx}] ({agid}).review_items[{item_idx}] is not a dict")
    _check_unknown_fields(item, REVIEW_ITEM_KEYS,
                         f"groups[{group_idx}] ({agid}).review_items[{item_idx}]")

    iid = item.get("item_id")
    if not iid or not isinstance(iid, str):
        raise ValueError(f"review_items[{item_idx}]: item_id must be non-empty string")
    if iid in seen_item_ids:
        raise ValueError(f"Duplicate item_id: {iid}")
    seen_item_ids.add(iid)

    sha = item.get("raw_sha256")
    if not sha or not isinstance(sha, str) or not SHA256_RE.match(sha):
        raise ValueError(f"review_items[{item_idx}] ({iid}): raw_sha256 must be 64-char hex string")
    if sha not in group_shas:
        raise ValueError(f"review_items[{item_idx}] ({iid}): raw_sha256 not in group raw_files: {sha[:16]}")

    sid = item.get("sample_id")
    if not sid or not isinstance(sid, str):
        raise ValueError(f"review_items[{item_idx}] ({iid}): sample_id must be non-empty string")
    if sid in seen_sample_ids:
        raise ValueError(f"review_items[{item_idx}] ({iid}): duplicate sample_id: {sid}")
    seen_sample_ids.add(sid)

    srp = item.get("sample_relative_path")
    normalized_srp = _validate_relative_path(srp, f"review_items[{item_idx}] ({iid})")
    if normalized_srp in seen_sample_paths:
        raise ValueError(
            f"review_items[{item_idx}] ({iid}): duplicate sample_relative_path: {srp}"
        )
    seen_sample_paths.add(normalized_srp)

    ssha = item.get("sample_sha256")
    if not ssha or not isinstance(ssha, str) or not SHA256_RE.match(ssha):
        raise ValueError(
            f"review_items[{item_idx}] ({iid}): sample_sha256 must be 64-char hex string"
        )

    label = item.get("label")
    if not isinstance(label, int) or isinstance(label, bool):
        raise ValueError(f"review_items[{item_idx}] ({iid}): label must be int")
    if label not in valid_labels:
        raise ValueError(f"review_items[{item_idx}] ({iid}): unknown label {label}")

    rs = item.get("review_status")
    if rs not in VALID_REVIEW_STATUSES:
        raise ValueError(f"review_items[{item_idx}] ({iid}): invalid review_status '{rs}'")

    disposition = item.get("disposition")
    if disposition not in VALID_DISPOSITIONS:
        raise ValueError(f"review_items[{item_idx}] ({iid}): invalid disposition '{disposition}'")

    if rs == "pending" and disposition == "clean":
        raise ValueError(f"review_items[{item_idx}] ({iid}): pending cannot have clean disposition")

    if rs == "reviewed":
        if not item.get("reviewer_id"):
            raise ValueError(f"review_items[{item_idx}] ({iid}): reviewed but no reviewer_id")
        if not item.get("reviewed_at"):
            raise ValueError(f"review_items[{item_idx}] ({iid}): reviewed but no reviewed_at")
        if item.get("reviewed_at"):
            _validate_iso8601(item["reviewed_at"], f"review_items[{item_idx}] ({iid}) reviewed_at")

    if rs in ("quarantined", "rejected") and disposition == "clean":
        raise ValueError(f"review_items[{item_idx}] ({iid}): {rs} cannot have clean disposition")

    doi = item.get("duplicate_of_item_id")
    if disposition != "duplicate" and doi:
        raise ValueError(
            f"review_items[{item_idx}] ({iid}): duplicate_of_item_id only allowed for duplicate disposition"
        )

    all_review_items[iid] = item
    all_review_items_by_group[agid].add(iid)
    items_by_sample_sha[ssha].append({
        "item_id": iid,
        "label": label,
        "disposition": disposition,
        "review_status": rs,
    })


def _validate_duplicate_reference(
    item: dict, item_idx: int, agid: str,
    group_items: dict[str, dict],
) -> None:
    iid = item["item_id"]
    doi = item.get("duplicate_of_item_id")
    if not doi or not isinstance(doi, str):
        raise ValueError(f"review_items[{item_idx}] ({iid}): duplicate_of_item_id must be non-empty string")
    if doi == iid:
        raise ValueError(f"review_items[{item_idx}] ({iid}): self-referencing duplicate_of_item_id")
    if doi not in group_items:
        raise ValueError(f"review_items[{item_idx}] ({iid}): duplicate_of_item_id not found: {doi}")

    canonical = group_items[doi]
    if item.get("sample_sha256") != canonical.get("sample_sha256"):
        raise ValueError(
            f"review_items[{item_idx}] ({iid}): sample_sha256 mismatch with canonical {doi}"
        )
    if item.get("label") != canonical.get("label"):
        raise ValueError(
            f"review_items[{item_idx}] ({iid}): label mismatch with canonical {doi}"
        )
    if canonical.get("disposition") != "clean":
        raise ValueError(
            f"review_items[{item_idx}] ({iid}): canonical {doi} is not clean"
        )


def _validate_conflict_record_basic(
    cr: dict, group_idx: int, agid: str, cr_idx: int,
    all_conflict_ids: set[str],
) -> None:
    if not isinstance(cr, dict):
        raise ValueError(f"groups[{group_idx}] ({agid}).conflict_records[{cr_idx}] is not a dict")
    _check_unknown_fields(cr, CONFLICT_RECORD_KEYS,
                         f"groups[{group_idx}] ({agid}).conflict_records[{cr_idx}]")

    cid = cr.get("conflict_id")
    if not cid or not isinstance(cid, str):
        raise ValueError(f"conflict_records[{cr_idx}]: conflict_id must be non-empty string")
    if cid in all_conflict_ids:
        raise ValueError(f"Duplicate conflict_id: {cid}")
    all_conflict_ids.add(cid)

    ssha = cr.get("sample_sha256")
    if not ssha or not isinstance(ssha, str) or not SHA256_RE.match(ssha):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): sample_sha256 must be 64-char hex string")

    rsha = cr.get("raw_sha256")
    if rsha:
        if not isinstance(rsha, str) or not SHA256_RE.match(rsha):
            raise ValueError(f"conflict_records[{cr_idx}] ({cid}): raw_sha256 must be 64-char hex string")

    related = cr.get("related_item_ids")
    if not isinstance(related, list) or len(related) < 2:
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): related_item_ids must have >= 2 items")
    if len(set(related)) < len(related):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): related_item_ids must be distinct")

    labels = cr.get("conflicting_labels")
    if not isinstance(labels, list) or len(labels) < 2:
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): conflicting_labels must have >= 2 items")
    if len(set(labels)) < len(labels):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): conflicting_labels must be distinct")

    if not cr.get("reviewer_conclusion") or not isinstance(cr["reviewer_conclusion"], str):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): reviewer_conclusion must be non-empty string")
    if not cr.get("quarantine_reason") or not isinstance(cr["quarantine_reason"], str):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): quarantine_reason must be non-empty string")
    if not cr.get("reviewed_at"):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): reviewed_at must be non-empty")
    _validate_iso8601(cr["reviewed_at"], f"conflict_records[{cr_idx}] ({cid}) reviewed_at")
    prov = cr.get("provenance")
    if not prov or not isinstance(prov, dict):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): provenance must be non-empty dict")
    if not prov.get("source") or not isinstance(prov["source"], str):
        raise ValueError(f"conflict_records[{cr_idx}] ({cid}): provenance.source must be non-empty string")


def _validate_conflict_references(
    cr: dict, group_idx: int, agid: str, cr_idx: int,
    all_review_items: dict[str, dict], group_item_ids: set[str],
) -> None:
    cid = cr.get("conflict_id", "")
    related = cr.get("related_item_ids", [])
    labels = cr.get("conflicting_labels", [])
    ssha = cr.get("sample_sha256", "")

    for rid in related:
        if rid not in all_review_items:
            raise ValueError(
                f"conflict_records[{cr_idx}] ({cid}): related_item_id not found: {rid}"
            )
        if rid not in group_item_ids:
            raise ValueError(
                f"conflict_records[{cr_idx}] ({cid}): related_item_id from different group: {rid}"
            )

    for rid in related:
        item = all_review_items[rid]
        if item.get("sample_sha256") != ssha:
            raise ValueError(
                f"conflict_records[{cr_idx}] ({cid}): item {rid} sample_sha256 "
                f"!= conflict sample_sha256"
            )

    related_labels = sorted({all_review_items[rid]["label"] for rid in related})
    if sorted(labels) != related_labels:
        raise ValueError(
            f"conflict_records[{cr_idx}] ({cid}): conflicting_labels {sorted(labels)} "
            f"!= referenced item labels {related_labels}"
        )

    for rid in related:
        item = all_review_items[rid]
        if item.get("disposition") == "clean" and item.get("review_status") == "reviewed":
            raise ValueError(
                f"conflict_records[{cr_idx}] ({cid}): referenced item {rid} "
                f"is reviewed+clean but appears in conflict"
            )

    # raw_sha256 consistency if present
    rsha = cr.get("raw_sha256")
    if rsha:
        for rid in related:
            item = all_review_items[rid]
            if item.get("raw_sha256") != rsha:
                raise ValueError(
                    f"conflict_records[{cr_idx}] ({cid}): item {rid} raw_sha256 "
                    f"!= conflict raw_sha256"
                )


def _validate_source_plan_binding(
    manifest_path: Path, plan_path: Path, declared_path_str: str, declared_sha: str,
) -> None:
    """Bind source plan path and SHA with path resolution."""
    if not plan_path.exists():
        raise FileNotFoundError(f"Collection plan not found: {plan_path}")

    # Resolve declared path relative to manifest directory
    manifest_dir = manifest_path.parent.resolve()
    declared_resolved = (manifest_dir / declared_path_str).resolve()
    actual_resolved = plan_path.resolve()

    if declared_resolved != actual_resolved:
        raise ValueError(
            f"Collection plan path mismatch: declared='{declared_path_str}' "
            f"(resolves to {declared_resolved}), actual={actual_resolved}"
        )

    actual_sha = _sha256_file(plan_path)
    if actual_sha != declared_sha:
        raise ValueError(
            f"Collection plan SHA mismatch: declared={declared_sha[:16]}..., "
            f"actual={actual_sha[:16]}..."
        )


def derive_targeted_collection_progress(
    manifest: dict,
    collection_plan: dict,
) -> dict:
    """Derive training_gain_targeted_collection_progress.v1 from execution facts."""
    valid_labels = {t["label"] for t in collection_plan.get("class_targets", [])}
    planned_slots = {
        f"planned_group_{i:02d}" for i in range(1, collection_plan.get("planned_groups", 8) + 1)
    }

    validate_execution_manifest(manifest, valid_labels, planned_slots)

    slot_data: dict[str, dict] = {}

    for group in manifest.get("groups", []):
        slot = group.get("planned_slot_id")
        if not slot:
            continue

        grs = group.get("group_review_status", "pending")
        if grs in ("rejected", "quarantined"):
            continue

        label_clean_counts: dict[int, int] = defaultdict(int)
        label_reviewed_counts: dict[int, int] = defaultdict(int)
        label_duplicate_counts: dict[int, int] = defaultdict(int)
        label_conflict_counts: dict[int, int] = defaultdict(int)
        label_clean_sample_shas: dict[int, set[str]] = defaultdict(set)

        for item in group.get("review_items", []):
            lbl = item["label"]
            rs = item.get("review_status", "pending")
            disp = item.get("disposition", "none")
            ssha = item.get("sample_sha256", "")

            if rs == "reviewed":
                label_reviewed_counts[lbl] += 1
                if disp == "clean":
                    if ssha not in label_clean_sample_shas[lbl]:
                        label_clean_counts[lbl] += 1
                        label_clean_sample_shas[lbl].add(ssha)
                elif disp == "duplicate":
                    label_duplicate_counts[lbl] += 1
                elif disp == "conflict":
                    label_conflict_counts[lbl] += 1

        for cr in group.get("conflict_records", []):
            for lbl in cr.get("conflicting_labels", []):
                if isinstance(lbl, int):
                    label_conflict_counts[lbl] = max(label_conflict_counts[lbl], 1)

        has_clean = any(c > 0 for c in label_clean_counts.values())

        labels = []
        for lbl in sorted(set(list(label_clean_counts.keys()) + list(label_reviewed_counts.keys()))):
            labels.append({
                "label": lbl,
                "reviewed_count": label_reviewed_counts.get(lbl, 0),
                "clean_count": label_clean_counts.get(lbl, 0),
                "duplicate_count": label_duplicate_counts.get(lbl, 0),
                "conflict_count": label_conflict_counts.get(lbl, 0),
            })

        if slot in slot_data:
            existing = slot_data[slot]
            existing_labels = {e["label"]: e for e in existing["labels"]}
            for lbl_entry in labels:
                lbl = lbl_entry["label"]
                if lbl in existing_labels:
                    el = existing_labels[lbl]
                    el["reviewed_count"] += lbl_entry["reviewed_count"]
                    el["clean_count"] += lbl_entry["clean_count"]
                    el["duplicate_count"] += lbl_entry["duplicate_count"]
                    el["conflict_count"] += lbl_entry["conflict_count"]
                else:
                    existing["labels"].append(lbl_entry)
            existing["has_clean"] = existing["has_clean"] or has_clean
        else:
            slot_data[slot] = {"labels": labels, "has_clean": has_clean}

    progress_groups = []
    for slot_id in sorted(planned_slots):
        if slot_id in slot_data:
            sd = slot_data[slot_id]
            cs = "completed" if sd["has_clean"] else "planned"
            progress_groups.append({
                "planned_group_id": slot_id,
                "capture_status": cs,
                "review_status": cs,
                "aggregate_status": cs,
                "quality_audit_status": cs,
                "clean_status": cs,
                "labels": sorted(sd["labels"], key=lambda x: x["label"]),
            })
        else:
            progress_groups.append({
                "planned_group_id": slot_id,
                "capture_status": "planned",
                "review_status": "planned",
                "aggregate_status": "planned",
                "quality_audit_status": "planned",
                "clean_status": "planned",
                "labels": [],
            })

    return {"schema": PROGRESS_SCHEMA, "groups": progress_groups}


def run_execution_validation(
    manifest_path: Path,
    collection_plan_path: Path,
    output_dir: Path | None = None,
    pretty: bool = False,
) -> dict:
    """Validate execution manifest and derive progress with transactional output."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Execution manifest not found: {manifest_path}")
    if not collection_plan_path.exists():
        raise FileNotFoundError(f"Collection plan not found: {collection_plan_path}")

    if output_dir and output_dir.exists():
        raise FileExistsError(f"Output dir already exists: {output_dir}")

    manifest = _load_json(manifest_path)
    collection_plan = _load_json(collection_plan_path)

    # Bind plan path + SHA
    declared_plan = manifest.get("source_collection_plan", {})
    _validate_source_plan_binding(
        manifest_path, collection_plan_path,
        declared_plan.get("path", ""), declared_plan.get("sha256", ""),
    )

    # Full validation
    valid_labels = {t["label"] for t in collection_plan.get("class_targets", [])}
    planned_slots = {
        f"planned_group_{i:02d}" for i in range(1, collection_plan.get("planned_groups", 8) + 1)
    }
    validate_execution_manifest(manifest, valid_labels, planned_slots)

    # Derive
    progress = derive_targeted_collection_progress(manifest, collection_plan)

    # Validate against old validator
    from scripts.build_training_gain_collection_pack import validate_progress_manifest
    validate_progress_manifest(progress, valid_labels, planned_slots)

    result = {
        "schema": "training_gain_execution_validation_report.v1",
        "execution_manifest_path": str(manifest_path),
        "collection_plan_path": str(collection_plan_path),
        "collection_plan_sha256": _sha256_file(collection_plan_path),
        "group_count": len(manifest.get("groups", [])),
        "total_review_items": sum(
            len(g.get("review_items", [])) for g in manifest.get("groups", [])
        ),
        "total_clean_items": sum(
            1 for g in manifest.get("groups", [])
            for item in g.get("review_items", [])
            if item.get("review_status") == "reviewed" and item.get("disposition") == "clean"
        ),
        "progress_derived": True,
        "progress_schema": PROGRESS_SCHEMA,
    }

    # Transactional output with unique staging
    if output_dir:
        staging_path = Path(
            tempfile.mkdtemp(
                prefix=f".{output_dir.name}.staging-",
                dir=str(output_dir.parent),
            )
        )
        original_error = None
        try:
            indent = 2 if pretty else None
            progress_path = staging_path / "progress.json"
            report_path = staging_path / "validation_report.json"

            progress_path.write_text(
                json.dumps(progress, ensure_ascii=False, indent=indent), encoding="utf-8"
            )
            report_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=indent), encoding="utf-8"
            )

            # Verify readback
            _load_json(progress_path)
            _load_json(report_path)

            # Atomic rename
            staging_path.rename(output_dir)
            staging_path = None  # Successfully renamed
        except Exception as e:
            original_error = e
            raise
        finally:
            if staging_path and staging_path.exists():
                try:
                    shutil.rmtree(staging_path)
                except Exception:
                    pass  # Cleanup failure preserved; original error takes precedence

    return result


# ---------- CLI ----------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate execution manifest and derive progress."
    )
    parser.add_argument("--manifest", required=True, help="Path to execution manifest JSON.")
    parser.add_argument("--collection-plan", required=True, help="Path to collection_plan.json.")
    parser.add_argument("--output-dir", help="Output directory for derived progress.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_execution_validation(
            Path(args.manifest),
            Path(args.collection_plan),
            Path(args.output_dir) if args.output_dir else None,
            args.pretty,
        )
    except (FileNotFoundError, ValueError, FileExistsError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
