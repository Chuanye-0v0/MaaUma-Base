"""Scoped L'Arc success-close compatibility for automatic lower skills.

Only a final-confirmed batch may use catalog-derived lower identities.  They
are not visual identity observations and never authorize another purchase.
The original dialog inspector and executor retain every action/safety gate.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("success_auto_lower_mapping_required")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def derive_success_auto_lower_binding(
    arguments: Mapping[str, Any], success_close: Any,
) -> dict[str, Any] | None:
    """Read the same batch's final proof, inventory and verified relations.

    Missing or contradictory evidence disables this compatibility; it does not
    replace the native preflight or turn an unreadable inventory into empty.
    """
    try:
        child_path = Path(arguments["child_plan_path"]).resolve()
        final_path = Path(arguments["final_confirmation_report_path"]).resolve()
        review_path = Path(arguments["promotion_review_path"]).resolve()
        expected_final_sha = arguments["expected_final_confirmation_report_sha256"]
        child = success_close._read_mapping(child_path)
        final = success_close._read_mapping(final_path)
        if not isinstance(child, Mapping) or not isinstance(final, Mapping):
            return None
        planned = success_close._child_purchase_ids(child)
        if not planned or len(planned) < 2:
            return None
        if (not success_close._is_sha256(expected_final_sha)
                or _sha(final_path) != expected_final_sha
                or success_close._validate_final_report(final, child_path, child)):
            return None

        source = final_path.parent.parent
        if (final_path.parent.name != "confirmation"
                or child_path.parent.parent != source / "selection"
                or review_path != source / "selection" / "promotion_review.json"):
            return None
        inventory_path = source / "prepare" / "skill_inventory.packet.json"
        inventory = _read(inventory_path)
        review = _read(review_path)
        worker = _read(source / "worker_report.json")
        # Import only after the worker has loaded its isolated D1 namespace.
        batch_api = importlib.import_module("core.skill_batch_purchase")
        valid, _reason = batch_api.validate_skill_inventory_packet(inventory)
        if not valid:
            return None
        promotion = child["execution_promotion"]
        capture = inventory.get("capture_binding")
        reviewed_rows = review.get("reviewed_rows")
        if (review.get("schema_version") != "uma.skill_purchase_current_run_review.v1"
                or review.get("inventory_packet_sha256") != inventory["packet_sha256"]
                or review.get("plan_id") != child["plan_id"]
                or review.get("sample_id") != promotion["sample_id"]
                or review.get("session_id") != promotion["session_id"]
                or review.get("capture_batch_id") != promotion["fresh_capture_id"]
                or not isinstance(capture, Mapping)
                or review.get("capture_sha256") != capture.get("capture_sha256")
                or not isinstance(reviewed_rows, Mapping)):
            return None
        purchases = child["purchases"]
        fingerprints = [purchase.get("source_row_fingerprint") for purchase in purchases]
        if (any(not isinstance(value, str) or not value for value in fingerprints)
                or len(set(fingerprints)) != len(fingerprints)
                or set(reviewed_rows) != set(fingerprints)):
            return None
        for purchase, fingerprint in zip(purchases, fingerprints):
            row = reviewed_rows[fingerprint]
            if (not isinstance(row, Mapping)
                    or row.get("source_row_fingerprint") != fingerprint
                    or row.get("item_id") != purchase["item_id"]):
                return None
        owned = inventory.get("owned_skill_ids")
        if (not isinstance(owned, list)
                or any(not isinstance(item, str) or not item for item in owned)
                or len(set(owned)) != len(owned)):
            return None
        context = worker.get("context")
        build = inventory.get("build_context")
        if not isinstance(context, Mapping) or not isinstance(build, Mapping):
            return None
        turn = context.get("turn_id")
        run = worker.get("run_session_id")
        turn_index = worker.get("turn_index")
        if (not isinstance(run, str) or not run
                or type(turn_index) is not int or turn_index < 1
                or not isinstance(turn, str)
                or not turn.startswith(f"{run}_turn{turn_index}_")
                or build.get("turn_id") != turn or capture.get("turn_id") != turn):
            return None
        catalog_name = context.get("planner_catalog")
        if not isinstance(catalog_name, str) or not catalog_name:
            return None
        catalog_path = Path(catalog_name).resolve()
        build_catalog = build.get("planner_catalog")
        if (not isinstance(build_catalog, str) or not build_catalog
                or Path(build_catalog).resolve() != catalog_path):
            return None
        catalog_api = importlib.import_module("core.skill_planner.catalog")
        catalog = catalog_api.SkillCatalog.load(catalog_path)
        automatic: list[str] = []
        pairs: list[dict[str, str]] = []
        excluded = set(owned) | set(planned)
        for upper_id in planned:
            upper = catalog.maybe_get(upper_id)
            if upper is None:
                return None
            lower_ids = upper.relations.get("lower", [])
            if not lower_ids:
                continue
            # Gold upgrades have one unambiguous, verified lower identity.
            if upper.rarity != "gold" or not upper.is_verified or len(lower_ids) != 1:
                return None
            lower_id = lower_ids[0]
            lower = catalog.maybe_get(lower_id)
            if (lower is None or not lower.is_verified or lower.rarity == "gold"
                    or lower.type != upper.type
                    or lower.relations.get("upper") != [upper_id]):
                return None
            if lower_id in excluded:
                continue
            excluded.add(lower_id)
            automatic.append(lower_id)
            pairs.append({"planned_item_id": upper_id, "lower_item_id": lower_id})
        if not automatic:
            return None
        return {
            "child": dict(child), "child_plan_sha256": _sha(child_path),
            "final_confirmation_report_sha256": expected_final_sha,
            "planned_item_ids": list(planned), "auto_included_item_ids": automatic,
            "provenance": {
                "source": "same_batch_verified_catalog_relations.lower",
                "visually_observed_item_ids": False,
                "inventory_packet_sha256": inventory["packet_sha256"],
                "inventory_path": str(inventory_path),
                "planner_catalog": str(catalog_path),
                "planner_catalog_sha256": _sha(catalog_path),
                "derived_pairs": pairs,
            },
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        return None


def install_success_auto_lower(harvest_skills: Any, success_close: Any) -> bool:
    """Install once on the isolated harvest module; return whether installed."""
    original_close = harvest_skills.close_skill_purchase_success
    if getattr(original_close, "_larc_success_auto_lower", False) is True:
        return False

    def close_with_auto_lower(*args: Any, **kwargs: Any) -> Any:
        binding = derive_success_auto_lower_binding(kwargs, success_close)
        if binding is None:
            return original_close(*args, **kwargs)
        planned = binding["planned_item_ids"]
        automatic = binding["auto_included_item_ids"]
        confirmed = planned + automatic
        original_inspect = success_close.inspect_skill_multi_purchase_success_dialog
        original_executor = success_close.SkillPurchaseExecutor
        original_writer = success_close._write_report

        def inspect(image: Any, *, confirmed_item_ids: Any, **options: Any) -> Any:
            ids = confirmed if confirmed_item_ids == planned else confirmed_item_ids
            return original_inspect(image, confirmed_item_ids=ids, **options)

        def executor(*executor_args: Any, **options: Any) -> Any:
            plan = executor_args[0] if executor_args else options.get("plan")
            parse = options.get("parse_fresh_state")
            if (plan == binding["child"] and callable(parse)
                    and options.get("stage") == "batch_close_success_probe"
                    and options.get("expected_action_id") == "skill_purchase.close_success"):
                def parse_bound(frame: Any) -> Any:
                    state = parse(frame)
                    if (isinstance(state, Mapping) and state.get("accepted") is True
                            and state.get("page_kind") == "skill_purchase_success"
                            and state.get("confirmed_item_ids") == confirmed):
                        state = dict(state)
                        state["planned_item_ids"] = list(planned)
                        state["auto_included_item_ids"] = list(automatic)
                        state["auto_included_identity_provenance"] = binding["provenance"]
                    return state
                options = {**options, "parse_fresh_state": parse_bound}
            return original_executor(*executor_args, **options)

        def write_report(root: Any, report: Any) -> Any:
            if (isinstance(report, Mapping)
                    and report.get("child_plan_sha256") == binding["child_plan_sha256"]
                    and report.get("final_confirmation_report_sha256")
                    == binding["final_confirmation_report_sha256"]):
                report = {**report, "planned_item_ids": list(planned),
                          "auto_included_item_ids": list(automatic),
                          "auto_included_identity_provenance": binding["provenance"]}
            return original_writer(root, report)

        success_close.inspect_skill_multi_purchase_success_dialog = inspect
        success_close.SkillPurchaseExecutor = executor
        success_close._write_report = write_report
        try:
            return original_close(*args, **kwargs)
        finally:
            success_close.inspect_skill_multi_purchase_success_dialog = original_inspect
            success_close.SkillPurchaseExecutor = original_executor
            success_close._write_report = original_writer

    close_with_auto_lower._larc_success_auto_lower = True
    harvest_skills.close_skill_purchase_success = close_with_auto_lower
    return True
