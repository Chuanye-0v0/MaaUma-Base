from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import ADB_ENDPOINT, ADB_PORT  # noqa: E402
from core.auto_trainer.condition_probe import (
    CONDITION_PROBE_CONFIRM_TOKEN,
    ConditionProbeExecutor,
    analyze_condition_details_file,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail-closed trainee-condition details probe.")
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--input-image")
    parser.add_argument("--adb-port", type=int, default=ADB_PORT)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--screenshot-dir", default="runs/condition_probe")
    parser.add_argument("--settle-seconds", type=float, default=0.3)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run(args)
    except ValueError as exc:
        report = {"is_valid": False, "status": "error", "error": str(exc)}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if report.get("is_valid", False) else 2


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_image:
        if args.mode != "dry-run":
            raise ValueError("input_image_requires_dry_run")
        report = analyze_condition_details_file(args.input_image)
        report["is_valid"] = not report["abstained"]
        report["status"] = "completed" if report["is_valid"] else "abstained"
        return report
    if args.mode != "live":
        raise ValueError("dry_run_requires_input_image")
    if args.adb_port != ADB_PORT:
        raise ValueError(f"adb_port_must_be_{ADB_PORT}")

    result = ConditionProbeExecutor(adb_factory=_adb_factory(args.adb_port)).run_once(
        execute=args.execute,
        confirm_token=args.confirm_token,
        screenshot_dir=args.screenshot_dir,
        settle_seconds=args.settle_seconds,
    )
    return {
        "is_valid": bool(result.get("performed")),
        "status": "completed" if result.get("performed") else "stopped",
        "performed": bool(result.get("performed")),
        "reason": str(result.get("reason", "unknown")),
        "result": result,
    }


def _adb_factory(adb_port: int) -> Any:
    def create_adb() -> Any:
        from core.adb import ADB

        return ADB(port=adb_port)

    return create_adb


if __name__ == "__main__":
    raise SystemExit(main())
