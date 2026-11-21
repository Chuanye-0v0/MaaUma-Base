from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.auto_trainer.speed_toggle import SpeedToggleExecutor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enable Uma bottom skip/fast-forward toggle when it is off.")
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--screenshot-path", required=True)
    parser.add_argument("--input-image")
    parser.add_argument("--adb-port", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-token")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    indent = 2 if args.pretty else None
    try:
        report = run(args)
        code = 0 if report.get("is_valid", False) else 2
    except Exception as exc:
        return _print(
            {"is_valid": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"},
            indent=indent,
            code=2,
        )
    return _print(report, indent=indent, code=code)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_image and args.mode != "dry-run":
        raise ValueError("input_image_requires_dry_run")

    executor = SpeedToggleExecutor(adb_factory=_adb_factory(args.adb_port))
    result = executor.run_once(
        mode=args.mode,
        execute=args.execute,
        confirm_token=args.confirm_token,
        screenshot_path=Path(args.input_image) if args.input_image else Path(args.screenshot_path),
    )
    return {
        "is_valid": True,
        "status": "completed" if result.get("performed") else "stopped",
        "performed": bool(result.get("performed")),
        "reason": str(result.get("reason", "unknown")),
        "result": result,
    }


def _adb_factory(adb_port: int | None):
    def create_adb():
        from core.adb import ADB

        return ADB(port=adb_port) if adb_port is not None else ADB()

    return create_adb


def _print(data: dict[str, Any], *, indent: int | None, code: int) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=indent))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
