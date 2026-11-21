"""Device-free cooperative runner for the progressive career route.

This module owns ordering and audit persistence only.  Frame perception and
action execution are injected ports; the runner never opens an image or talks
to a device.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Protocol

from core.auto_trainer.dashboard_runner import (
    ControlledStop,
    CooperativeRunnerDriver,
    FreshFrameEvidence,
)
from core.auto_trainer.progressive_route_action_port import ProgressiveActionPort
from core.auto_trainer.progressive_route_packet import (
    build_transition_packet,
    load_transition_packet,
    write_transition_packet,
)
from core.auto_trainer.progressive_route_runtime import (
    CapturedRouteFrame,
    ProgressiveRouteRuntime,
)
from core.auto_trainer.progressive_route_session import (
    ProgressiveRouteError,
    ProgressiveRouteSession,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RouteFrameSource(Protocol):
    def capture(
        self, candidate_detector_ids: tuple[str, ...]
    ) -> CapturedRouteFrame: ...


class RunnerDriver(Protocol):
    def reach(self, boundary: str) -> None: ...

    def after_fresh_frame(self, evidence: FreshFrameEvidence) -> None: ...


def canonical_runner_binding_sha256(
    *,
    run_id: str,
    session_id: str,
    run_session_id: str,
    packet_sha256: str,
) -> str:
    payload = {
        "packet_sha256": packet_sha256,
        "run_id": run_id,
        "run_session_id": run_session_id,
        "session_id": session_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_boundary_record_sha256(record: dict[str, object]) -> str:
    payload = dict(record)
    payload.pop("boundary_record_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_runner_head_sha256(head: dict[str, object]) -> str:
    payload = dict(head)
    payload.pop("head_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TransitionStore:
    root: Path
    run_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("root must be Path")
        for value, label in ((self.run_id, "run_id"), (self.session_id, "session_id")):
            if type(value) is not str or not value or value in {".", ".."}:
                raise ProgressiveRouteError(f"{label} must be non-empty")
            if any(character in value for character in ("/", "\\", ":")):
                raise ProgressiveRouteError(f"{label} contains a path separator")

    @property
    def private_directory(self) -> Path:
        return self.root / self.run_id

    @property
    def packet_path(self) -> Path:
        return self.private_directory / "transition.packet.json"

    @property
    def boundary_log_path(self) -> Path:
        return self.private_directory / "runner.boundaries.jsonl"

    @property
    def head_path(self) -> Path:
        return self.private_directory / "runner.head.json"

    def save(
        self,
        session: ProgressiveRouteSession,
        *,
        boundary: str,
        period_text: str | None = None,
        directive_sequence: int = 0,
    ) -> dict[str, object]:
        if not isinstance(session, ProgressiveRouteSession):
            raise TypeError("session must be ProgressiveRouteSession")
        if session.run_session_id != self.session_id:
            raise ProgressiveRouteError("session run_session_id mismatch")
        if type(boundary) is not str or not boundary:
            raise ProgressiveRouteError("boundary must be non-empty")
        if type(directive_sequence) is not int or directive_sequence < 0:
            raise ProgressiveRouteError("directive_sequence must be non-negative")
        if session.last_observation_sha256 is None:
            raise ProgressiveRouteError("transition save requires an accepted observation")
        existing_records = self._load_boundary_records()
        self._validate_head(existing_records)
        if existing_records and directive_sequence < existing_records[-1]["directive_sequence"]:
            raise ProgressiveRouteError("boundary log directive sequence regressed")
        phase, status = self._phase_and_status(session)
        packet = build_transition_packet(
            session,
            phase=phase,
            period_text=period_text or session.turn_context.period_key,
            status=status,
        )
        self.private_directory.mkdir(parents=True, exist_ok=True)
        write_transition_packet(self.packet_path, packet, session=session)
        reloaded = load_transition_packet(
            self.packet_path,
            session=session,
            expected_run_session_id=self.session_id,
        )
        packet_sha256 = str(reloaded["packet_sha256"])
        record: dict[str, object] = {
            "boundary": boundary,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "run_session_id": session.run_session_id,
            "packet_sha256": packet_sha256,
            "directive_sequence": directive_sequence,
            "previous_boundary_sha256": (
                existing_records[-1]["boundary_record_sha256"]
                if existing_records
                else None
            ),
        }
        record["runner_binding_sha256"] = canonical_runner_binding_sha256(
            run_id=self.run_id,
            session_id=self.session_id,
            run_session_id=session.run_session_id,
            packet_sha256=packet_sha256,
        )
        record["boundary_record_sha256"] = canonical_boundary_record_sha256(record)
        self._append_boundary(record)
        self._write_head(record)
        return record

    def load_last_directive_sequence(self, session: ProgressiveRouteSession) -> int:
        records = self._load_boundary_records()
        if not records:
            if self.packet_path.exists() or self.head_path.exists():
                raise ProgressiveRouteError("runner persistence head is incomplete")
            return 0
        head = self._validate_head(records)
        last = records[-1]["directive_sequence"]
        reloaded = load_transition_packet(
            self.packet_path,
            session=session,
            expected_run_session_id=self.session_id,
        )
        if (
            records[-1]["packet_sha256"] != reloaded["packet_sha256"]
            or head["packet_sha256"] != reloaded["packet_sha256"]
        ):
            raise ProgressiveRouteError("boundary log does not bind the current packet")
        return last

    def _validate_head(
        self,
        records: list[dict[str, object]],
    ) -> dict[str, object] | None:
        if not records:
            if self.head_path.exists() or self.packet_path.exists():
                raise ProgressiveRouteError("runner persistence exists without a boundary chain")
            return None
        try:
            head = json.loads(self.head_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProgressiveRouteError("runner persistence head is unavailable") from exc
        required = {
            "run_id",
            "session_id",
            "run_session_id",
            "packet_sha256",
            "directive_sequence",
            "boundary_record_sha256",
            "head_sha256",
        }
        if not isinstance(head, dict) or set(head) != required:
            raise ProgressiveRouteError("runner persistence head fields are invalid")
        tail = records[-1]
        expected = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "run_session_id": self.session_id,
            "packet_sha256": tail["packet_sha256"],
            "directive_sequence": tail["directive_sequence"],
            "boundary_record_sha256": tail["boundary_record_sha256"],
        }
        if any(head[key] != value for key, value in expected.items()):
            raise ProgressiveRouteError("runner persistence head does not match the boundary tail")
        if head["head_sha256"] != canonical_runner_head_sha256(head):
            raise ProgressiveRouteError("runner persistence head hash mismatch")
        try:
            packet = load_transition_packet(
                self.packet_path,
                expected_run_session_id=self.session_id,
            )
        except Exception as exc:
            raise ProgressiveRouteError("runner persistence packet is unavailable") from exc
        if packet["packet_sha256"] != head["packet_sha256"]:
            raise ProgressiveRouteError("runner persistence head packet mismatch")
        return head

    def _load_boundary_records(self) -> list[dict[str, object]]:
        if not self.boundary_log_path.exists():
            return []
        try:
            lines = self.boundary_log_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ProgressiveRouteError("boundary log is unavailable") from exc
        last = 0
        required = {
            "boundary",
            "run_id",
            "session_id",
            "run_session_id",
            "packet_sha256",
            "directive_sequence",
            "runner_binding_sha256",
            "previous_boundary_sha256",
            "boundary_record_sha256",
        }
        records: list[dict[str, object]] = []
        previous_boundary_sha256: str | None = None
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProgressiveRouteError("boundary log record is invalid") from exc
            if not isinstance(record, dict) or set(record) != required:
                raise ProgressiveRouteError("boundary log record fields are invalid")
            if (
                record["run_id"] != self.run_id
                or record["session_id"] != self.session_id
                or record["run_session_id"] != self.session_id
            ):
                raise ProgressiveRouteError("boundary log identity mismatch")
            sequence = record["directive_sequence"]
            if type(sequence) is not int or sequence < last:
                raise ProgressiveRouteError("boundary log directive sequence regressed")
            if (
                type(record["packet_sha256"]) is not str
                or _SHA256_RE.fullmatch(record["packet_sha256"]) is None
            ):
                raise ProgressiveRouteError("boundary log packet SHA is invalid")
            expected_binding = canonical_runner_binding_sha256(
                run_id=self.run_id,
                session_id=self.session_id,
                run_session_id=self.session_id,
                packet_sha256=record["packet_sha256"],
            )
            if record["runner_binding_sha256"] != expected_binding:
                raise ProgressiveRouteError("boundary log runner binding mismatch")
            if record["previous_boundary_sha256"] != previous_boundary_sha256:
                raise ProgressiveRouteError("boundary log hash chain mismatch")
            if record["boundary_record_sha256"] != canonical_boundary_record_sha256(record):
                raise ProgressiveRouteError("boundary log record hash mismatch")
            previous_boundary_sha256 = record["boundary_record_sha256"]
            last = sequence
            records.append(record)
        return records

    @staticmethod
    def _phase_and_status(session: ProgressiveRouteSession) -> tuple[str, str]:
        if session.current_node_id == "career.complete.main":
            return "complete", "complete"
        if session.current_node_id == "mismatch.stop":
            return "mismatch", "mismatch"
        return "career", "running"

    def _append_boundary(self, record: dict[str, object]) -> None:
        existing = b""
        if self.boundary_log_path.exists():
            try:
                existing = self.boundary_log_path.read_bytes()
            except OSError as exc:
                raise ProgressiveRouteError("boundary log is unavailable") from exc
        line = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                "wb",
                dir=self.private_directory,
                prefix=".runner.boundaries.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(existing)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.boundary_log_path)
            temporary = None
        except OSError as exc:
            raise ProgressiveRouteError("boundary log atomic write failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _write_head(self, record: dict[str, object]) -> None:
        head: dict[str, object] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "run_session_id": self.session_id,
            "packet_sha256": record["packet_sha256"],
            "directive_sequence": record["directive_sequence"],
            "boundary_record_sha256": record["boundary_record_sha256"],
        }
        head["head_sha256"] = canonical_runner_head_sha256(head)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.private_directory,
                prefix=".runner.head.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    head,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.head_path)
            temporary = None
        except OSError as exc:
            raise ProgressiveRouteError("runner persistence head atomic write failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class ProgressiveRouteRunner:
    """Run deterministic directives through injected offline-safe ports."""

    def __init__(
        self,
        *,
        runtime: ProgressiveRouteRuntime,
        frame_source: RouteFrameSource,
        action_port: ProgressiveActionPort,
        driver: CooperativeRunnerDriver | RunnerDriver,
        transition_store: TransitionStore,
        wait: Callable[[float], None],
    ) -> None:
        if runtime.session.run_session_id != transition_store.session_id:
            raise ProgressiveRouteError("runner and store session_id mismatch")
        control_port = getattr(driver, "port", None)
        if control_port is None:
            raise ProgressiveRouteError("runner driver must expose its control port")
        if (
            getattr(control_port, "run_id", None) != transition_store.run_id
            or getattr(control_port, "session_id", None) != transition_store.session_id
        ):
            raise ProgressiveRouteError("runner driver control identity mismatch")
        self.runtime = runtime
        self.frame_source = frame_source
        self.action_port = action_port
        self.driver = driver
        self.transition_store = transition_store
        self.wait = wait
        self._directive_sequence = transition_store.load_last_directive_sequence(
            runtime.session
        )

    def run(self, max_directives: int) -> str:
        if type(max_directives) is not int or max_directives <= 0:
            raise ValueError("max_directives must be a positive integer")
        pending = self.runtime.next_directive()
        processed = 0
        try:
            while processed < max_directives:
                processed += 1
                if pending.kind == "capture":
                    pending = self._capture(pending.expected_node_ids)
                    continue
                if pending.kind == "wait":
                    delay = pending.delay_seconds
                    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay <= 0:
                        return self._safe_stop("invalid_wait_directive")
                    self.wait(float(delay))
                    self._persist("wait_complete")
                    pending = self.runtime.next_directive()
                    continue
                if pending.kind == "execute":
                    pending = self._execute(pending)
                    continue
                if pending.kind == "stop":
                    self._persist("completion" if pending.reason == "career_complete" else "safe_stop")
                    return pending.reason
                if pending.kind in {"explicit_probe", "collect_scenario", "handoff"}:
                    self._persist(pending.kind)
                    return f"{pending.kind}:{pending.reason}"
                return self._safe_stop("unknown_directive_kind")
        except ControlledStop as exc:
            self._persist("pause_safe_stop")
            return f"controlled_stop:{exc.boundary}"
        except (ProgressiveRouteError, RuntimeError, ValueError) as exc:
            self._persist("runner_error")
            return f"runner_error:{exc}"
        return self._safe_stop("max_directives_exhausted")

    def _capture(self, expected_node_ids: tuple[str, ...]):
        self.driver.reach("before_capture")
        detector_ids = self.runtime.graph.detector_ids_for(expected_node_ids)
        frame = self.frame_source.capture(detector_ids)
        if not isinstance(frame, CapturedRouteFrame):
            raise ProgressiveRouteError("frame source returned an invalid frame")
        self.driver.after_fresh_frame(
            FreshFrameEvidence(
                path=frame.path,
                sha256=frame.source_sha256,
                captured_at=frame.captured_at,
            )
        )
        pending = self.runtime.accept_frame(frame)
        self._persist("accepted_observation")
        return pending

    def _execute(self, pending):
        self._directive_sequence += 1
        current = self._persist("sealed_directive")
        directive = self.runtime.seal_pending_directive(
            pending,
            run_id=self.transition_store.run_id,
            session_id=self.transition_store.session_id,
            run_session_id=self.runtime.session.run_session_id,
            directive_sequence=self._directive_sequence,
            source_frame_sha256=self.runtime.session.last_observation_sha256,
            transition_packet_sha256=str(current["packet_sha256"]),
        )
        self.driver.reach("before_decision")
        self.driver.reach("before_tap")
        result = self.action_port.execute(directive)
        pending = self.runtime.accept_execution(result)
        self._persist("action_result")
        return pending

    def _persist(self, boundary: str) -> dict[str, object]:
        return self.transition_store.save(
            self.runtime.session,
            boundary=boundary,
            directive_sequence=self._directive_sequence,
        )

    def _safe_stop(self, reason: str) -> str:
        self._persist("safe_stop")
        return reason


__all__ = [
    "ProgressiveRouteRunner",
    "RouteFrameSource",
    "TransitionStore",
    "canonical_boundary_record_sha256",
    "canonical_runner_head_sha256",
    "canonical_runner_binding_sha256",
]
