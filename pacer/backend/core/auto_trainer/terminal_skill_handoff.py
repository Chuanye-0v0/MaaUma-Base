from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TerminalSkillHandoffError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TerminalSkillHandoffProgress:
    source_image_path: str
    source_image_sha256: str
    handoff_ready: bool = True
    phase: str = "terminal_skill"
    purchase_executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "uma.terminal_skill_handoff_progress.v1",
            "phase": self.phase,
            "handoff_ready": self.handoff_ready,
            "source_image_path": self.source_image_path,
            "source_image_sha256": self.source_image_sha256,
            "purchase_executed": self.purchase_executed,
            "action_eligible": False,
            "next_required_phase": "full_page_capture_required",
        }


def load_terminal_skill_handoff_artifact(
    artifact_path: Path,
    *,
    expected_artifact_sha256: str,
    allowed_root: Path,
) -> TerminalSkillHandoffProgress:
    if (
        not isinstance(expected_artifact_sha256, str)
        or not _SHA256_RE.fullmatch(expected_artifact_sha256)
    ):
        raise TerminalSkillHandoffError(
            "terminal_skill_handoff_artifact_sha_invalid"
        )
    path = _resolve_inside_root(
        artifact_path,
        allowed_root=allowed_root,
        outside_code="terminal_skill_handoff_artifact_outside_root",
    )
    if not path.is_file():
        raise TerminalSkillHandoffError(
            "terminal_skill_handoff_artifact_missing"
        )
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_artifact_sha256:
        raise TerminalSkillHandoffError(
            "terminal_skill_handoff_artifact_sha_mismatch"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalSkillHandoffError(
            "terminal_skill_handoff_artifact_invalid"
        ) from exc
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
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise TerminalSkillHandoffError(
            "terminal_skill_handoff_artifact_schema_invalid"
        )
    if (
        payload.get("schema_version")
        != "uma.terminal_skill_handoff_progress.v1"
        or payload.get("phase") != "terminal_skill"
        or payload.get("handoff_ready") is not True
        or payload.get("purchase_executed") is not False
        or payload.get("action_eligible") is not False
        or payload.get("next_required_phase") != "full_page_capture_required"
    ):
        raise TerminalSkillHandoffError(
            "terminal_skill_handoff_artifact_state_invalid"
        )
    source_sha = payload.get("source_image_sha256")
    source_path = payload.get("source_image_path")
    if not isinstance(source_sha, str) or not _SHA256_RE.fullmatch(source_sha):
        raise TerminalSkillHandoffError("terminal_skill_handoff_sha_invalid")
    if not isinstance(source_path, str) or not source_path:
        raise TerminalSkillHandoffError("terminal_skill_handoff_path_invalid")
    source = _resolve_inside_root(
        Path(source_path),
        allowed_root=allowed_root,
        outside_code="terminal_skill_handoff_path_outside_root",
    )
    if not source.is_file():
        raise TerminalSkillHandoffError("terminal_skill_handoff_source_missing")
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
        raise TerminalSkillHandoffError("terminal_skill_handoff_sha_mismatch")
    return TerminalSkillHandoffProgress(
        source_image_path=str(source),
        source_image_sha256=source_sha,
    )


def _resolve_inside_root(
    path: Path,
    *,
    allowed_root: Path,
    outside_code: str,
) -> Path:
    root = allowed_root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TerminalSkillHandoffError(outside_code) from exc
    return resolved


def inspect_terminal_skill_handoff(
    report: Mapping[str, Any],
    *,
    allowed_root: Path,
) -> TerminalSkillHandoffProgress:
    if not isinstance(report, Mapping):
        raise TerminalSkillHandoffError("terminal_skill_handoff_report_invalid")
    if report.get("stop_reason") != "skill_acquisition_handoff":
        raise TerminalSkillHandoffError("terminal_skill_handoff_stop_mismatch")
    steps = report.get("step_reports")
    if not isinstance(steps, list) or not steps:
        raise TerminalSkillHandoffError("terminal_skill_handoff_step_missing")
    step = steps[-1]
    if not isinstance(step, Mapping):
        raise TerminalSkillHandoffError("terminal_skill_handoff_step_invalid")
    result = step.get("result")
    if not isinstance(result, Mapping):
        raise TerminalSkillHandoffError("terminal_skill_handoff_result_missing")
    if result.get("reason") != "skill_acquisition_handoff":
        raise TerminalSkillHandoffError("terminal_skill_handoff_reason_mismatch")
    if result.get("performed") is not False:
        raise TerminalSkillHandoffError("terminal_skill_handoff_performed_invalid")
    handoff = result.get("skill_handoff")
    if not isinstance(handoff, Mapping):
        raise TerminalSkillHandoffError("terminal_skill_handoff_payload_missing")
    if handoff.get("schema_version") != "uma.skill_acquisition_handoff.v1":
        raise TerminalSkillHandoffError("terminal_skill_handoff_schema_invalid")
    if handoff.get("page_state") != "skill_acquisition":
        raise TerminalSkillHandoffError("terminal_skill_handoff_page_invalid")
    if handoff.get("purchase_executed") is not False:
        raise TerminalSkillHandoffError("terminal_skill_handoff_already_executed")

    source_path = handoff.get("source_image_path")
    step_path = step.get("screenshot_path")
    if not isinstance(source_path, str) or not source_path:
        raise TerminalSkillHandoffError("terminal_skill_handoff_path_invalid")
    if not isinstance(step_path, str) or step_path != source_path:
        raise TerminalSkillHandoffError("terminal_skill_handoff_path_mismatch")
    path = _resolve_inside_root(
        Path(source_path),
        allowed_root=allowed_root,
        outside_code="terminal_skill_handoff_path_outside_root",
    )
    if not path.is_file():
        raise TerminalSkillHandoffError("terminal_skill_handoff_source_missing")

    source_sha = handoff.get("source_image_sha256")
    frame_safety = handoff.get("frame_safety")
    result_frame_safety = result.get("frame_safety")
    if not isinstance(source_sha, str) or not _SHA256_RE.fullmatch(source_sha):
        raise TerminalSkillHandoffError("terminal_skill_handoff_sha_invalid")
    if (
        not isinstance(frame_safety, Mapping)
        or frame_safety.get("source_image_sha256") != source_sha
        or not isinstance(result_frame_safety, Mapping)
        or result_frame_safety.get("source_image_sha256") != source_sha
    ):
        raise TerminalSkillHandoffError("terminal_skill_handoff_sha_mismatch")
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha != source_sha:
        raise TerminalSkillHandoffError("terminal_skill_handoff_sha_mismatch")

    return TerminalSkillHandoffProgress(
        source_image_path=str(path),
        source_image_sha256=source_sha,
    )


__all__ = [
    "TerminalSkillHandoffError",
    "TerminalSkillHandoffProgress",
    "inspect_terminal_skill_handoff",
    "load_terminal_skill_handoff_artifact",
]
