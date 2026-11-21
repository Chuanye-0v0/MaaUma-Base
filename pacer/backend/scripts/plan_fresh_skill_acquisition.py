"""Build an offline skill-planner dry-run from a freshly saved screenshot set.

This module intentionally has no ADB, executor, or click dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.skill_acquisition_parser import (  # noqa: E402
    build_skill_planner_observation,
    parse_skill_acquisition_pages,
)
from core.skill_name_resolver import load_skill_catalog, resolve_skill_manifest  # noqa: E402
from core.skill_planner.catalog import SkillCatalog  # noqa: E402
from core.skill_planner.models import SkillPurchaseContext  # noqa: E402
from core.skill_planner.planner import plan_skill_purchases  # noqa: E402
from core.skill_planner.preferences import (  # noqa: E402
    SkillPreferenceProfile,
    default_terminal_skill_preference_profile,
    load_skill_preference_profile,
)
from core.skill_planner.replay import (  # noqa: E402
    build_replay_verdict,
    capture_fingerprint,
    validate_capture_binding,
)
from core.skill_batch_purchase import (  # noqa: E402
    build_skill_batch_plan,
    build_skill_inventory_packet,
)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_metadata_known(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict) or not context.get("target_mode"):
        return False
    race_profile = context.get("race_profile")
    if not isinstance(race_profile, dict):
        return False
    required = ("distance_type", "style")
    return all(
        race_profile.get(key) not in (None, "", "unknown") for key in required
    )


def _planner_policy_from_build_context(
    build_context: dict[str, Any] | None,
) -> dict[str, Any]:
    requested_policy = (
        build_context.get("policy")
        if isinstance(build_context, dict)
        else None
    )
    allow_gold_without_lower = (
        isinstance(requested_policy, dict)
        and requested_policy.get("allow_gold_without_lower") is True
    )
    return {
        "min_confidence": 0.75,
        "allow_unverified_skills": False,
        "require_condition_match": True,
        "allow_gold_without_lower": allow_gold_without_lower,
    }


def run_fresh_dry_run(
    *,
    pages: list[Path],
    resolver_catalog: Path,
    planner_catalog: Path | None,
    output_dir: Path,
    turn_id: str,
    build_context: dict[str, Any] | None,
    preference_profile: SkillPreferenceProfile | None = None,
    prior_owned_skill_ids: list[str] | None = None,
    capture_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse fresh files and issue an offline-only, fail-closed planner report."""

    binding = capture_fingerprint(
        pages,
        turn_id=turn_id,
        capture_manifest=capture_manifest,
    )
    parsed = parse_skill_acquisition_pages(
        pages,
        output_dir=output_dir / "parser_output",
        assume_continuous_scroll=True,
    )
    resolved = resolve_skill_manifest(
        parsed,
        load_skill_catalog(resolver_catalog),
        manifest_dir=output_dir / "parser_output",
    )
    mapping = {
        item["name"]: item["skill_id"]
        for item in resolved.get("skills", [])
        if item.get("name") and item.get("skill_id")
    }
    observation = build_skill_planner_observation(resolved, name_to_skill_id=mapping)
    if prior_owned_skill_ids is not None:
        if (
            not isinstance(prior_owned_skill_ids, list)
            or any(
                not isinstance(item, str) or not item
                for item in prior_owned_skill_ids
            )
            or len(prior_owned_skill_ids) != len(set(prior_owned_skill_ids))
        ):
            raise ValueError("prior_owned_skill_ids_invalid")
        observation["owned_skills"] = sorted(
            set(observation["owned_skills"]) | set(prior_owned_skill_ids)
        )
    verdict = build_replay_verdict(
        parsed_manifest=parsed,
        resolved_manifest=resolved,
        planner_observation=observation,
        binding=validate_capture_binding(binding, binding),
    )

    if not _build_metadata_known(build_context):
        verdict["planner_verdict"] = "PLANNER_BLOCKED_BUILD_METADATA_UNKNOWN"
        dry_run = {
            "executed": True,
            "purchases": [],
            "skipped": [],
            "spent_skill_pt": 0,
            "remaining_skill_pt": parsed.get("current_skill_points"),
            "reason": "target_mode_distance_type_and_style_are_required",
        }
    elif parsed.get("current_skill_points") is None:
        verdict["planner_verdict"] = "PLANNER_BLOCKED_NO_FRESH_INPUT"
        dry_run = {
            "executed": False,
            "purchases": [],
            "skipped": [],
            "spent_skill_pt": 0,
            "remaining_skill_pt": None,
            "reason": "fresh_parser_current_skill_points_unreadable",
        }
    elif planner_catalog is None:
        verdict["planner_verdict"] = "PLANNER_BLOCKED_CATALOG_MISSING"
        dry_run = {
            "executed": False,
            "purchases": [],
            "skipped": [],
            "spent_skill_pt": 0,
            "remaining_skill_pt": parsed["current_skill_points"],
            "reason": "planner_catalog_required_when_build_metadata_is_known",
        }
    else:
        context_data = dict(build_context)
        context_data.update(
            {
                "schema_version": "uma.skill_purchase_context.v1",
                "trigger": "final_build",
                "source_capture_sha256": binding["capture_sha256"],
                "current_skill_pt": parsed["current_skill_points"],
                "owned_skills": observation["owned_skills"],
                "available_skills": observation["available_skills"],
                "policy": _planner_policy_from_build_context(build_context),
            }
        )
        effective_profile = preference_profile or default_terminal_skill_preference_profile(
            str(context_data["target_mode"])
        )
        plan = plan_skill_purchases(
            SkillPurchaseContext.from_dict(context_data),
            SkillCatalog.load(planner_catalog),
            preference_profile=effective_profile,
        )
        dry_run = {"executed": True, **plan.to_dict()}

    inventory_packet: dict[str, Any] | None = None
    batch_plan: dict[str, Any] | None = None
    batch_plan_omitted_action_ids: list[str] = []
    if (
        isinstance(parsed.get("current_skill_points"), int)
        and capture_manifest is not None
    ):
        inventory_packet = build_skill_inventory_packet(
            capture_binding=binding,
            planner_observation=observation,
            planner_plan={
                **dry_run,
                "resource_checks": {
                    "uses_skill_points_only": True,
                    "uses_carrots": False,
                    "uses_clocks": False,
                    "uses_paid_currency": False,
                },
            },
            build_context=build_context or {},
        )
        local_action_ids = [
            str(item["skill_id"])
            for item in dry_run.get("purchases", [])
            if isinstance(item, dict)
            and isinstance(item.get("skill_id"), str)
        ]
        allowed = set(inventory_packet["allowed_action_ids"])
        batch_plan_omitted_action_ids = [
            action_id for action_id in local_action_ids if action_id not in allowed
        ]
        batch_plan = build_skill_batch_plan(
            inventory=inventory_packet,
            selected_action_ids=[
                action_id for action_id in local_action_ids if action_id in allowed
            ],
            decision_source="local_auto",
        )

    report = {
        "schema_version": "uma.fresh_skill_acquisition_dry_run.v1",
        "mode": "offline_dry_run_only",
        "execution_allowed": False,
        "currency_scope": "skill_points_only",
        "capture_binding": binding,
        "verdict": verdict,
        "build_context_provided": _build_metadata_known(build_context),
        "parser_manifest": parsed,
        "resolver_summary": resolved["resolution_summary"],
        "planner_observation": observation,
        "inventory_packet": inventory_packet,
        "batch_plan": batch_plan,
        "batch_plan_omitted_action_ids": batch_plan_omitted_action_ids,
        "planner_dry_run": {
            **dry_run,
            "resource_checks": {
                "uses_skill_points_only": True,
                "uses_carrots": False,
                "uses_clocks": False,
                "uses_paid_currency": False,
            },
        },
        "notes": [
            "The supplied pages are the only recognition input for this report.",
            "This is not a purchase command and cannot invoke a live executor.",
        ],
    }
    _write_json(output_dir / "fresh_skill_dry_run.report.json", report)
    return report


def _read_context(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("build context must contain a JSON object")
    return data


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline fresh skill-page parse and planner dry-run.")
    parser.add_argument("--pages", nargs="+", type=Path, required=True)
    parser.add_argument("--resolver-catalog", type=Path, required=True)
    parser.add_argument("--planner-catalog", type=Path)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--build-context", type=Path)
    parser.add_argument("--preference-profile", type=Path)
    parser.add_argument("--capture-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = (
            load_skill_preference_profile(args.preference_profile)
            if args.preference_profile is not None
            else None
        )
        report = run_fresh_dry_run(
            pages=args.pages,
            resolver_catalog=args.resolver_catalog,
            planner_catalog=args.planner_catalog,
            output_dir=args.output_dir,
            turn_id=args.turn_id,
            build_context=_read_context(args.build_context),
            preference_profile=profile,
            capture_manifest=_read_context(args.capture_manifest),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"fresh skill dry-run input error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report["verdict"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
