"""Fail-closed coordination for the final skill-acquisition page.

This module can capture and plan, but it cannot purchase skills or tap the UI.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.auto_trainer.frame_safety import FRAME_SAFETY_SCHEMA_VERSION
from core.skill_planner.replay import capture_fingerprint


HANDOFF_SCHEMA = "uma.skill_acquisition_handoff.v1"
HANDOFF_REPORT_SCHEMA = "uma.skill_acquisition_handoff_report.v1"
CONSUMPTION_MARKER_ROOT = (
    Path(__file__).resolve().parents[2] / "runs" / "skill_handoff_consumed"
)


class SkillAcquisitionHandoffSafetyError(RuntimeError):
    """Raised before resume when a handoff violates a safety invariant."""


@dataclass(frozen=True)
class SkillHandoffResumeAuthorization:
    trigger_sha256: str
    report_path: Path
    report_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "trigger_sha256": self.trigger_sha256,
            "report_path": str(self.report_path),
            "report_sha256": self.report_sha256,
        }


@dataclass
class SkillHandoffSession:
    expected_trigger_sha256: str | None = None
    expected_turn_id: str | None = None
    consumed_report_sha256: str | None = None

    @staticmethod
    def consumption_marker_path(
        authorization: SkillHandoffResumeAuthorization,
    ) -> Path:
        return CONSUMPTION_MARKER_ROOT / f"{authorization.report_sha256}.json"

    def consume(self, authorization: SkillHandoffResumeAuthorization) -> bool:
        if self.consumed_report_sha256 is not None:
            return False
        if authorization.trigger_sha256 != self.expected_trigger_sha256:
            return False
        marker_path = self.consumption_marker_path(authorization)
        marker = {
            "schema_version": "uma.skill_acquisition_handoff_consumption.v1",
            "report_sha256": authorization.report_sha256,
            "trigger_sha256": authorization.trigger_sha256,
        }
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            with marker_path.open("x", encoding="utf-8") as handle:
                json.dump(marker, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except (FileExistsError, OSError):
            return False
        self.consumed_report_sha256 = authorization.report_sha256
        return True


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_handoff(handoff: dict[str, Any]) -> tuple[Path, str]:
    if handoff.get("schema_version") != HANDOFF_SCHEMA:
        raise SkillAcquisitionHandoffSafetyError("handoff_schema")
    if handoff.get("page_state") != "skill_acquisition":
        raise SkillAcquisitionHandoffSafetyError("page_state")
    if handoff.get("purchase_executed") is not False:
        raise SkillAcquisitionHandoffSafetyError("purchase_executed")

    source_path = Path(str(handoff.get("source_image_path") or ""))
    if not source_path.is_file():
        raise SkillAcquisitionHandoffSafetyError("source_image_missing")
    source_sha256 = _sha256(source_path)
    if handoff.get("source_image_sha256") != source_sha256:
        raise SkillAcquisitionHandoffSafetyError("source_sha_mismatch")

    frame_safety = handoff.get("frame_safety")
    if not isinstance(frame_safety, dict):
        raise SkillAcquisitionHandoffSafetyError("frame_safety_missing")
    if frame_safety.get("schema_version") != FRAME_SAFETY_SCHEMA_VERSION:
        raise SkillAcquisitionHandoffSafetyError("frame_safety_schema")
    if frame_safety.get("source_image_sha256") != source_sha256:
        raise SkillAcquisitionHandoffSafetyError("frame_safety_sha_mismatch")
    if frame_safety.get("text_source") not in {
        "local_tesseract",
        "local_rapidocr",
        "local_tesseract_fallback",
    }:
        raise SkillAcquisitionHandoffSafetyError("frame_safety_text_source")
    if frame_safety.get("text_status") != "ok":
        raise SkillAcquisitionHandoffSafetyError("frame_safety_text_status")
    visible_text = frame_safety.get("visible_text")
    if not isinstance(visible_text, str) or not visible_text.strip():
        raise SkillAcquisitionHandoffSafetyError("frame_safety_visible_text")
    dangerous = frame_safety.get("dangerous")
    if not isinstance(dangerous, bool):
        raise SkillAcquisitionHandoffSafetyError("frame_safety_dangerous_type")
    if dangerous:
        raise SkillAcquisitionHandoffSafetyError("frame_safety_dangerous")
    resource_terms = frame_safety.get("resource_terms")
    if not isinstance(resource_terms, (list, tuple)):
        raise SkillAcquisitionHandoffSafetyError("frame_safety_resource_terms_type")
    if resource_terms:
        raise SkillAcquisitionHandoffSafetyError("frame_safety_resource_terms")
    errors = frame_safety.get("errors")
    if not isinstance(errors, (list, tuple)) or errors:
        raise SkillAcquisitionHandoffSafetyError("frame_safety_errors")
    return source_path, source_sha256


def _validate_capture(capture: dict[str, Any]) -> list[Path]:
    if capture.get("schema_version") != "uma.skill_acquisition_capture.v1":
        raise SkillAcquisitionHandoffSafetyError("capture_schema")
    if capture.get("capture_mode") != "screenshot_and_vertical_roi_swipe_only":
        raise SkillAcquisitionHandoffSafetyError("capture_mode")
    tap_count = capture.get("tap_count")
    if type(tap_count) is not int or tap_count != 0:
        raise SkillAcquisitionHandoffSafetyError("capture_tap_count")
    raw_pages = capture.get("captured_pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise SkillAcquisitionHandoffSafetyError("capture_pages_missing")
    pages = [Path(str(value)) for value in raw_pages]
    if any(not path.is_file() for path in pages):
        raise SkillAcquisitionHandoffSafetyError("capture_page_missing")
    if capture.get("captured_page_count") != len(pages):
        raise SkillAcquisitionHandoffSafetyError("capture_page_count")
    return pages


def _validate_dry_run(report: dict[str, Any]) -> None:
    if report.get("schema_version") != "uma.fresh_skill_acquisition_dry_run.v1":
        raise SkillAcquisitionHandoffSafetyError("dry_run_schema")
    if report.get("mode") != "offline_dry_run_only":
        raise SkillAcquisitionHandoffSafetyError("dry_run_mode")
    if report.get("execution_allowed") is not False:
        raise SkillAcquisitionHandoffSafetyError("execution_allowed")
    if report.get("currency_scope") != "skill_points_only":
        raise SkillAcquisitionHandoffSafetyError("currency_scope")

    planner = report.get("planner_dry_run")
    checks = planner.get("resource_checks") if isinstance(planner, dict) else None
    if not isinstance(checks, dict) or checks.get("uses_skill_points_only") is not True:
        raise SkillAcquisitionHandoffSafetyError("skill_points_only_required")
    for key in ("uses_carrots", "uses_clocks", "uses_paid_currency"):
        if checks.get(key) is not False:
            raise SkillAcquisitionHandoffSafetyError(f"forbidden_resource:{key}")


def _validate_capture_binding(
    report: dict[str, Any],
    pages: list[Path],
    *,
    expected_turn_id: str | None = None,
) -> dict[str, Any]:
    binding = report.get("capture_binding")
    if not isinstance(binding, dict):
        raise SkillAcquisitionHandoffSafetyError("capture_binding_missing")
    turn_id = binding.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        raise SkillAcquisitionHandoffSafetyError("capture_binding_turn_id")
    if expected_turn_id is not None and turn_id != expected_turn_id:
        raise SkillAcquisitionHandoffSafetyError("capture_binding_turn_id_mismatch")
    expected = capture_fingerprint(pages, turn_id=turn_id)
    if binding != expected:
        raise SkillAcquisitionHandoffSafetyError("capture_binding_mismatch")
    return expected


def validate_resume_authorization(
    authorization: SkillHandoffResumeAuthorization,
) -> dict[str, Any]:
    if not isinstance(authorization, SkillHandoffResumeAuthorization):
        raise SkillAcquisitionHandoffSafetyError("authorization_type")
    report_path = Path(authorization.report_path)
    if not report_path.is_file():
        raise SkillAcquisitionHandoffSafetyError("report_missing")
    try:
        report_bytes = report_path.read_bytes()
    except OSError as exc:
        raise SkillAcquisitionHandoffSafetyError("report_unreadable") from exc
    if hashlib.sha256(report_bytes).hexdigest() != authorization.report_sha256:
        raise SkillAcquisitionHandoffSafetyError("report_sha_mismatch")
    try:
        report = json.loads(report_bytes.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SkillAcquisitionHandoffSafetyError("report_unreadable") from exc
    if not isinstance(report, dict) or report.get("schema_version") != HANDOFF_REPORT_SCHEMA:
        raise SkillAcquisitionHandoffSafetyError("report_schema")
    if report.get("trigger_sha256") != authorization.trigger_sha256:
        raise SkillAcquisitionHandoffSafetyError("trigger_sha_mismatch")
    if report.get("page_state") != "skill_acquisition":
        raise SkillAcquisitionHandoffSafetyError("report_page_state")
    report_turn_id = report.get("turn_id")
    if not isinstance(report_turn_id, str) or not report_turn_id:
        raise SkillAcquisitionHandoffSafetyError("report_turn_id")
    if report.get("purchase_executed") is not False:
        raise SkillAcquisitionHandoffSafetyError("report_purchase_executed")
    capture = report.get("capture")
    if not isinstance(capture, dict):
        raise SkillAcquisitionHandoffSafetyError("report_capture_missing")
    pages = _validate_capture(capture)
    dry_run = report.get("dry_run")
    if not isinstance(dry_run, dict):
        raise SkillAcquisitionHandoffSafetyError("report_dry_run_missing")
    _validate_dry_run(dry_run)
    _validate_capture_binding(
        dry_run,
        pages,
        expected_turn_id=report_turn_id,
    )
    return report


def validate_resume_session(
    session: Any,
    authorization: SkillHandoffResumeAuthorization,
    report: dict[str, Any],
) -> None:
    if not isinstance(session, SkillHandoffSession):
        raise SkillAcquisitionHandoffSafetyError("type")
    if not session.expected_trigger_sha256 or not session.expected_turn_id:
        raise SkillAcquisitionHandoffSafetyError("unbound")
    if session.expected_trigger_sha256 != authorization.trigger_sha256:
        raise SkillAcquisitionHandoffSafetyError("trigger_sha_mismatch")
    if session.expected_trigger_sha256 != report.get("trigger_sha256"):
        raise SkillAcquisitionHandoffSafetyError("report_trigger_sha_mismatch")
    if session.expected_turn_id != report.get("turn_id"):
        raise SkillAcquisitionHandoffSafetyError("turn_id_mismatch")
    if session.consumed_report_sha256 is not None:
        raise SkillAcquisitionHandoffSafetyError("already_consumed")


def run_skill_acquisition_handoff(
    *,
    handoff: dict[str, Any],
    output_dir: Path,
    capture_runner: Callable[[Path], dict[str, Any]],
    dry_run_runner: Callable[..., dict[str, Any]],
    resolver_catalog: Path,
    planner_catalog: Path | None,
    turn_id: str,
    build_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run bounded capture plus offline planning and persist a non-purchase report."""

    _source_path, trigger_sha256 = validate_handoff(handoff)
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise SkillAcquisitionHandoffSafetyError("turn_id")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    capture = capture_runner(output_dir / "capture")
    pages = _validate_capture(capture)
    dry_run = dry_run_runner(
        pages=pages,
        resolver_catalog=Path(resolver_catalog),
        planner_catalog=Path(planner_catalog) if planner_catalog is not None else None,
        output_dir=output_dir / "dry_run",
        turn_id=turn_id,
        build_context=build_context,
        capture_manifest=capture,
    )
    _validate_dry_run(dry_run)
    capture_binding = _validate_capture_binding(
        dry_run,
        pages,
        expected_turn_id=turn_id,
    )

    persisted = {
        "schema_version": HANDOFF_REPORT_SCHEMA,
        "trigger_sha256": trigger_sha256,
        "turn_id": turn_id,
        "page_state": "skill_acquisition",
        "capture": capture,
        "capture_binding": capture_binding,
        "dry_run": dry_run,
        "purchase_executed": False,
    }
    report_path = output_dir / "skill_handoff.report.json"
    report_path.write_text(
        json.dumps(persisted, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report_sha256 = _sha256(report_path)
    authorization = SkillHandoffResumeAuthorization(
        trigger_sha256=trigger_sha256,
        report_path=report_path,
        report_sha256=report_sha256,
    )
    return {
        **persisted,
        "report_path": str(report_path),
        "report_sha256": report_sha256,
        "resume_authorization": authorization.to_dict(),
    }


__all__ = [
    "HANDOFF_REPORT_SCHEMA",
    "HANDOFF_SCHEMA",
    "SkillAcquisitionHandoffSafetyError",
    "SkillHandoffResumeAuthorization",
    "SkillHandoffSession",
    "run_skill_acquisition_handoff",
    "validate_handoff",
    "validate_resume_authorization",
    "validate_resume_session",
]
