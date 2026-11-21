from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol

from core.auto_trainer.control_metadata_io import (
    metadata_target, note_metadata_failure, permission_retry, safe_metadata_failure,
)


ControlAction = Literal["start", "pause", "resume", "stop"]
ControlBoundary = Literal["before_capture", "before_decision", "before_tap", "after_fresh_frame", "after_resync"]
CheckpointDecision = Literal["continue", "pause", "stop"]
RunnerOutcome = Literal["completed", "stopped"]
_ACTIVE_RUNNER_STATES = {
    "starting",
    "running",
    "pause_requested",
    "paused",
    "resume_requested",
    "stop_requested",
}


@dataclass(frozen=True)
class ControlProfile:
    profile_id: str
    runner_config: Mapping[str, Any]


class RunnerControlPort(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    def heartbeat(self, *, run_id: str, session_id: str) -> None: ...

    def checkpoint(
        self,
        boundary: ControlBoundary,
        *,
        run_id: str,
        session_id: str,
        fresh_frame: bool = False,
        resynced: bool = False,
        safety_gate_passed: bool = False,
    ) -> CheckpointDecision: ...

    def finish(
        self,
        *,
        outcome: RunnerOutcome,
        reason: str,
        evidence_sha256: str | None,
    ) -> None: ...


RunnerFactory = Callable[[ControlProfile, RunnerControlPort], None]


class ControlStateError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _BoundRunnerPort:
    def __init__(self, service: "DashboardControlService", run_id: str, session_id: str):
        self._service = service
        self.run_id = run_id
        self.session_id = session_id

    def heartbeat(self, *, run_id: str, session_id: str) -> None:
        self._service._runner_heartbeat(run_id=run_id, session_id=session_id)

    def heartbeat_failed(self, *, error_type: str) -> None:
        self._service._runner_heartbeat_failed(self.run_id, self.session_id, error_type)

    def attach_runtime(self, control) -> None:
        self._service._attach_runtime(self.run_id, self.session_id, control)

    def runtime_state(self, state: str) -> None:
        self._service._runtime_state(self.run_id, self.session_id, state)

    def checkpoint(
        self,
        boundary: ControlBoundary,
        *,
        run_id: str,
        session_id: str,
        fresh_frame: bool = False,
        resynced: bool = False,
        safety_gate_passed: bool = False,
    ) -> CheckpointDecision:
        return self._service._runner_checkpoint(
            boundary,
            run_id=run_id,
            session_id=session_id,
            fresh_frame=fresh_frame,
            resynced=resynced,
            safety_gate_passed=safety_gate_passed,
        )

    def finish(
        self,
        *,
        outcome: RunnerOutcome,
        reason: str,
        evidence_sha256: str | None,
    ) -> None:
        self._service._runner_finish(
            run_id=self.run_id,
            session_id=self.session_id,
            outcome=outcome,
            reason=reason,
            evidence_sha256=evidence_sha256,
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DashboardControlService:
    def __init__(
        self,
        *,
        state_dir: str | Path,
        profiles: Mapping[str, ControlProfile],
        runner_factory: RunnerFactory | None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        heartbeat_timeout: float = 15.0,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.profiles = dict(profiles)
        self.runner_factory = runner_factory
        self.clock = clock
        self.wall_clock = wall_clock
        self.heartbeat_timeout = heartbeat_timeout
        self._mutex = threading.RLock()
        self._closed = False
        self._runtime = None

        token_path = self.state_dir / "control_token.json"
        token_payload = self._read_json(token_path)
        if not isinstance(token_payload, dict) or not isinstance(token_payload.get("token"), str):
            token_payload = {"schema_version": "ug_parent_dashboard.v2.control_token", "token": secrets.token_urlsafe(32)}
            self._write_json(token_path, token_payload)
        self._token = token_payload["token"]

        state_path = self.state_dir / "control_state.json"
        state = self._read_json(state_path)
        if not isinstance(state, dict) or state.get("schema_version") != "ug_parent_dashboard.v2.control_state":
            state = {
                "schema_version": "ug_parent_dashboard.v2.control_state",
                "state": "idle",
                "state_version": 0,
                "run_id": None,
                "session_id": None,
                "profile_id": None,
                "heartbeat_at": None,
                "active_boundary": None,
                "tap_in_flight": False,
                "last_error": None,
                "last_result": None,
                "last_audit_sha256": None,
            }
            self._write_json(state_path, state)
        else:
            state_changed = False
            if type(state.get("tap_in_flight")) is not bool:
                state["tap_in_flight"] = (
                    state.get("state") in _ACTIVE_RUNNER_STATES
                    and state.get("active_boundary") == "before_tap"
                )
                state_changed = True
            if "last_result" not in state:
                state["last_result"] = None
                state_changed = True
            if state_changed:
                self._write_json(state_path, state)
        self._state = state

        requests_path = self.state_dir / "control_requests.json"
        requests = self._read_json(requests_path)
        if not isinstance(requests, dict) or requests.get("schema_version") != "ug_parent_dashboard.v2.control_requests":
            requests = {"schema_version": "ug_parent_dashboard.v2.control_requests", "requests": {}}
            self._write_json(requests_path, requests)
        self._requests = requests

    @property
    def token(self) -> str:
        return self._token

    def recover_detached_runner(self) -> None:
        """Retire a dead control session while the caller owns the device lease.

        This only repairs process-control metadata. Career progress and pending
        game transactions belong to the runner and are not changed here.
        """
        with self._mutex:
            if self._runtime is not None:
                raise ControlStateError("runner_runtime_still_attached")
            if self._state["state"] in {"idle", "stopped"} and not (self.state_dir/"runner.lock").exists():
                return
            before = self._state["state_version"]
            self._release_runner_lock()
            if (self.state_dir/"runner.lock").exists():
                raise ControlStateError("runner_lock_owner_mismatch")
            self._state.update(state="stopped", state_version=before+1, tap_in_flight=False,
                active_boundary=None, heartbeat_at=None, last_error=None,
                last_result={"outcome":"stopped", "reason":"backend_reconnected"})
            self._persist_state()
            self._record_audit("detached_runner_recovered", {
                "before_state_version":before, "after_state_version":before+1,
                "run_id":self._state.get("run_id"), "reason":"backend_reconnected",
                "game_progress_changed":False})

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            self._expire_heartbeat_if_needed()
            heartbeat = self._state.get("heartbeat_at")
            return {
                "schema_version": "ug_parent_dashboard.v2.control_state",
                "state": self._state["state"],
                "state_version": self._state["state_version"],
                "run_id": self._state.get("run_id"),
                "session_id": self._state.get("session_id"),
                "profile_id": self._state.get("profile_id"),
                "heartbeat_at": heartbeat,
                "heartbeat_age": None if heartbeat is None else max(0.0, self.clock() - heartbeat),
                "heartbeat_error_type": self._state.get("heartbeat_error_type"),
                "heartbeat_io_failure": safe_metadata_failure(self._state.get("heartbeat_io_failure")),
                "active_boundary": self._state.get("active_boundary"),
                "tap_in_flight": self._state["tap_in_flight"],
                "last_error": self._state.get("last_error"),
                "last_result": self._state.get("last_result"),
                "runtime": self._runtime.snapshot() if self._runtime is not None else None,
            }

    def submit(self, action: ControlAction, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._mutex:
            request_id, expected_version = self._validate_request(action, payload)
            request_hash = hashlib.sha256(_canonical({"action": action, "payload": dict(payload)}).encode("utf-8")).hexdigest()
            stored = self._requests["requests"].get(request_id)
            if isinstance(stored, dict):
                if stored.get("request_sha256") != request_hash:
                    raise ControlStateError("request_id_conflict")
                return dict(stored["response"])

            self._expire_heartbeat_if_needed()
            if self._state["state"] == "unresponsive" and not (action == "stop" and self._runtime is not None):
                raise ControlStateError("runner_unresponsive")
            if expected_version != self._state["state_version"]:
                raise ControlStateError("stale_state_version")

            if action == "start":
                response, thread = self._submit_start(payload, request_id)
            else:
                response, thread = self._submit_control(action, payload, request_id)
            self._requests["requests"][request_id] = {"request_sha256": request_hash, "response": response}
            self._write_json(self.state_dir / "control_requests.json", self._requests)
            self._record_audit(f"control_{action}", response)
        if thread is not None:
            thread.start()
        return dict(response)

    def request_result(self, request_id: str) -> dict[str, Any]:
        with self._mutex:
            stored = self._requests["requests"].get(request_id)
            if not isinstance(stored, dict):
                raise ControlStateError("request_not_found")
            return dict(stored["response"])

    def runner_port(self, *, run_id: str, session_id: str) -> RunnerControlPort:
        return _BoundRunnerPort(self, run_id, session_id)

    def _validate_request(self, action: str, payload: Mapping[str, Any]) -> tuple[str, int]:
        if action not in {"start", "pause", "resume", "stop"} or not isinstance(payload, Mapping):
            raise ControlStateError("invalid_request")
        expected_keys = {"request_id", "expected_state_version", "profile_id"} if action == "start" else {
            "request_id", "expected_state_version", "run_id"
        }
        if set(payload) != expected_keys:
            if "request_id" not in payload:
                raise ControlStateError("request_id_required")
            if "expected_state_version" not in payload:
                raise ControlStateError("expected_state_version_required")
            raise ControlStateError("invalid_request")
        request_id = payload.get("request_id")
        version = payload.get("expected_state_version")
        if not isinstance(request_id, str) or not request_id or "/" in request_id or "\\" in request_id:
            raise ControlStateError("request_id_required")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ControlStateError("expected_state_version_required")
        return request_id, version

    def _submit_start(self, payload: Mapping[str, Any], request_id: str) -> tuple[dict[str, Any], threading.Thread | None]:
        profile_id = payload.get("profile_id")
        if not isinstance(profile_id, str) or profile_id not in self.profiles:
            raise ControlStateError("unknown_profile")
        if self.runner_factory is None:
            raise ControlStateError("runner_unavailable")
        if self._state["state"] not in {"idle", "stopped"} or (self.state_dir / "runner.lock").exists():
            raise ControlStateError("runner_already_active")
        run_id = f"run-{secrets.token_hex(8)}"
        session_id = f"session-{secrets.token_hex(8)}"
        self._acquire_runner_lock(run_id, session_id, profile_id)
        self._runtime = None
        before = self._state["state_version"]
        self._state.update({
            "state": "starting",
            "state_version": before + 1,
            "run_id": run_id,
            "session_id": session_id,
            "profile_id": profile_id,
            "heartbeat_at": self.clock(),
            "heartbeat_error_type": None,
            "heartbeat_io_failure": None,
            "active_boundary": None,
            "tap_in_flight": False,
            "last_error": None,
            "last_result": None,
        })
        self._persist_state()
        response = {
            "accepted": True,
            "request_id": request_id,
            "state": "starting",
            "state_version": before + 1,
            "run_id": run_id,
            "before_state_version": before,
            "after_state_version": before + 1,
        }
        port = self.runner_port(run_id=run_id, session_id=session_id)
        thread = threading.Thread(
            target=self._run_factory,
            args=(self.profiles[profile_id], port),
            daemon=True,
            name=f"dashboard-control-{run_id}",
        )
        return response, thread

    def _submit_control(self, action: str, payload: Mapping[str, Any], request_id: str) -> tuple[dict[str, Any], None]:
        if payload.get("run_id") != self._state.get("run_id"):
            raise ControlStateError("unknown_run")
        allowed = {
            "pause": ({"running"}, "pause_requested"),
            "resume": ({"paused"}, "resume_requested"),
            "stop": ({"starting", "running", "pause_requested", "paused", "resume_requested", "unresponsive"}, "stop_requested"),
        }
        states, target = allowed[action]
        if self._state["state"] not in states:
            raise ControlStateError("state_transition_rejected")
        before = self._state["state_version"]
        self._state["state"] = target
        self._state["state_version"] = before + 1
        if self._runtime is not None:
            self._runtime.request(action)
        self._persist_state()
        return ({
            "accepted": True,
            "request_id": request_id,
            "state": target,
            "state_version": before + 1,
            "run_id": self._state["run_id"],
            "before_state_version": before,
            "after_state_version": before + 1,
        }, None)

    def _attach_runtime(self, run_id, session_id, control):
        with self._mutex:
            self._require_runner(run_id, session_id)
            self._runtime = control
            if self._state["state"] == "stop_requested":
                control.request("stop")
            elif self._state["state"] == "pause_requested":
                control.request("pause")

    def _runtime_state(self, run_id, session_id, state):
        with self._mutex:
            self._require_runner(run_id, session_id)
            if self._state["state"] in {"stop_requested", "stopped", "unresponsive", "error"}:
                return
            if state not in {"paused", "running"}:
                raise ControlStateError("invalid_runtime_state")
            if state != self._state["state"]:
                self._state.update(state=state, state_version=self._state["state_version"] + 1)
                self._persist_state()

    def _run_factory(self, profile: ControlProfile, port: RunnerControlPort) -> None:
        try:
            assert self.runner_factory is not None
            self.runner_factory(profile, port)
        except Exception as exc:
            with self._mutex:
                if (
                    self._state.get("session_id") == getattr(port, "session_id", None)
                    and self._state["state"] != "stopped"
                ):
                    before = self._state["state_version"]
                    self._state.update({
                        "state": "error",
                        "state_version": before + 1,
                        "tap_in_flight": False,
                        "last_error": type(exc).__name__,
                    })
                    self._persist_state()
                    self._record_audit("runner_error", {"before_state_version": before, "after_state_version": before + 1, "result": "error"})

    def _runner_heartbeat(self, *, run_id: str, session_id: str) -> None:
        with self._mutex:
            self._require_runner(run_id, session_id)
            before = self._state["state_version"]
            before_state = self._state["state"]
            before_heartbeat = self._state.get("heartbeat_at")
            heartbeat = self.clock()
            if before_state != "starting" and heartbeat == before_heartbeat:
                return
            self._state["heartbeat_at"] = heartbeat
            self._state.pop("heartbeat_io_failure", None)
            try:
                if before_state == "starting":
                    self._state["state"] = "running"
                    self._state["state_version"] = before + 1
                elif (before_state == "unresponsive" and not self._closed
                        and self._state.get("last_error") == "heartbeat_expired"
                        and self._runtime is not None):
                    runtime = self._runtime.snapshot()
                    if (runtime.get("state") == "paused" and runtime.get("pause_requested") is True
                            and runtime.get("input_in_flight") is False):
                        # A fresh heartbeat from the same bound runner restores only
                        # its confirmed pause. Human acknowledgement still owns resume.
                        self._state.update(state="paused", state_version=before + 1, last_error=None)
                        self._record_audit("paused_heartbeat_recovered", {
                            "before_state_version": before, "after_state_version": before + 1,
                            "result": "paused", "automatic_resume": False})
                self._persist_state()
                self._record_audit("runner_heartbeat", {
                    "before_state_version": before,
                    "after_state_version": self._state["state_version"],
                    "before_state": before_state,
                    "after_state": self._state["state"],
                    "before_heartbeat_at": before_heartbeat,
                    "after_heartbeat_at": heartbeat,
                    "result": "updated",
                })
            except OSError as exc:
                failure = safe_metadata_failure(getattr(exc, "control_metadata_io_failure", None))
                if failure:
                    self._state["heartbeat_io_failure"] = failure
                raise

    def _runner_heartbeat_failed(self, run_id, session_id, error_type):
        with self._mutex:
            self._require_runner(run_id, session_id)
            if self._state["state"] not in _ACTIVE_RUNNER_STATES | {"unresponsive"}:
                return
            # Wake a manual pause before diagnostic I/O. This requests the owned
            # worker to stop; it neither kills a process nor releases its lock.
            if self._runtime is not None:
                self._runtime.request("stop")
            before = self._state["state_version"]
            safe_type = error_type if isinstance(error_type, str) and error_type.isidentifier() else "Exception"
            self._state.update(state="unresponsive", state_version=before + 1,
                last_error="runner_heartbeat_failed", heartbeat_error_type=safe_type[:80])
            self._persist_state()
            self._record_audit("runner_heartbeat_failed", {
                "before_state_version": before, "after_state_version": before + 1,
                "error_type": safe_type[:80], "result": "stop_requested"})

    def _runner_checkpoint(
        self,
        boundary: ControlBoundary,
        *,
        run_id: str,
        session_id: str,
        fresh_frame: bool = False,
        resynced: bool = False,
        safety_gate_passed: bool = False,
    ) -> CheckpointDecision:
        with self._mutex:
            self._require_runner(run_id, session_id)
            if boundary not in {
                "before_capture",
                "before_decision",
                "before_tap",
                "after_fresh_frame",
                "after_resync",
            }:
                raise ControlStateError("invalid_request")

            state = self._state["state"]
            tap_in_flight = self._state["tap_in_flight"]

            if state == "paused" and boundary == "after_resync":
                return "pause"

            if state in {"pause_requested", "stop_requested"} and tap_in_flight:
                if boundary != "after_fresh_frame" or fresh_frame is not True:
                    raise ControlStateError("fresh_frame_required")

            if boundary == "after_fresh_frame":
                if tap_in_flight and fresh_frame is not True:
                    raise ControlStateError("fresh_frame_required")
                if fresh_frame is True:
                    self._state["tap_in_flight"] = False

            if state == "stop_requested":
                self._state["active_boundary"] = boundary
                return self._transition_from_runner("stopped", boundary, "stop")

            if boundary == "after_resync":
                if state != "resume_requested":
                    raise ControlStateError("state_transition_rejected")
                if not (
                    fresh_frame is True
                    and resynced is True
                    and safety_gate_passed is True
                ):
                    raise ControlStateError("resume_resync_required")
                self._state["active_boundary"] = boundary
                return self._transition_from_runner("running", boundary, "continue")

            self._state["active_boundary"] = boundary
            if state == "resume_requested":
                self._persist_checkpoint_state()
                return "pause"

            if state == "pause_requested":
                return self._transition_from_runner("paused", boundary, "pause")

            if state == "paused":
                self._persist_checkpoint_state()
                return "pause"
            if state == "stopped":
                self._persist_checkpoint_state()
                return "stop"
            if state not in {"starting", "running"}:
                raise ControlStateError("state_transition_rejected")

            if boundary == "before_tap":
                self._state["tap_in_flight"] = True
            self._persist_checkpoint_state()
            return "continue"

    def _runner_finish(
        self,
        *,
        run_id: str,
        session_id: str,
        outcome: RunnerOutcome,
        reason: str,
        evidence_sha256: str | None,
    ) -> None:
        with self._mutex:
            self._require_runner(run_id, session_id)
            if (
                not isinstance(outcome, str)
                or outcome not in {"completed", "stopped"}
                or not isinstance(reason, str)
                or not (1 <= len(reason) <= 96)
                or not ("a" <= reason[0] <= "z")
                or any(
                    not (("a" <= char <= "z") or ("0" <= char <= "9") or char in "_.:-")
                    for char in reason
                )
                or (
                    evidence_sha256 is not None
                    and (
                        not isinstance(evidence_sha256, str)
                        or len(evidence_sha256) != 64
                        or any(char not in "0123456789abcdef" for char in evidence_sha256)
                    )
                )
            ):
                raise ControlStateError("runner_finish_invalid")
            settling_unresponsive = (self._state["state"] == "unresponsive" and outcome == "stopped"
                and self._runtime is not None and self._runtime.stopping)
            if not settling_unresponsive and self._state["state"] not in {
                "starting",
                "running",
                "pause_requested",
                "paused",
                "resume_requested",
                "stop_requested",
            }:
                raise ControlStateError("runner_finish_rejected")

            before = self._state["state_version"]
            result = {
                "outcome": outcome,
                "reason": reason,
                "boundary": self._state.get("active_boundary"),
                "evidence_sha256": evidence_sha256,
            }
            self._state.update({
                "state": "stopped",
                "state_version": before + 1,
                "tap_in_flight": False,
                "last_error": None,
                "last_result": result,
            })
            self._persist_state()
            self._record_audit("runner_finished", {
                "before_state_version": before,
                "after_state_version": before + 1,
                "run_id": run_id,
                **result,
            })
            self._release_runner_lock()

    def _transition_from_runner(
        self,
        target: str,
        boundary: ControlBoundary,
        decision: CheckpointDecision,
    ) -> CheckpointDecision:
        before = self._state["state_version"]
        self._state.update({
            "state": target,
            "state_version": before + 1,
            "active_boundary": boundary,
            "tap_in_flight": False,
        })
        self._persist_checkpoint_state()
        result = {
            "before_state_version": before,
            "after_state_version": before + 1,
            "boundary": boundary,
            "state": target,
            "decision": decision,
            "run_id": self._state.get("run_id"),
        }
        self._record_audit("runner_checkpoint", result)
        if target == "stopped":
            self._release_runner_lock()
        return decision

    def _require_runner(self, run_id: str, session_id: str) -> None:
        if run_id != self._state.get("run_id") or session_id != self._state.get("session_id"):
            raise ControlStateError("run_session_mismatch")

    def _expire_heartbeat_if_needed(self) -> None:
        heartbeat = self._state.get("heartbeat_at")
        if self._state["state"] in _ACTIVE_RUNNER_STATES and heartbeat is not None and self.clock() - heartbeat > self.heartbeat_timeout:
            before = self._state["state_version"]
            self._state.update({"state": "unresponsive", "state_version": before + 1, "last_error": "heartbeat_expired"})
            self._persist_state()
            self._record_audit("heartbeat_expired", {"before_state_version": before, "after_state_version": before + 1, "result": "unresponsive"})

    def _acquire_runner_lock(self, run_id: str, session_id: str, profile_id: str) -> None:
        path = self.state_dir / "runner.lock"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ControlStateError("runner_already_active") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_canonical({"run_id": run_id, "session_id": session_id, "profile_id": profile_id}))
            stream.flush()
            os.fsync(stream.fileno())

    def _release_runner_lock(self) -> None:
        path = self.state_dir / "runner.lock"
        lock = self._read_json(path)
        if not isinstance(lock, dict):
            return
        if lock.get("run_id") != self._state.get("run_id") or lock.get("session_id") != self._state.get("session_id"):
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _persist_state(self) -> None:
        self._write_json(self.state_dir / "control_state.json", self._state)

    def _persist_checkpoint_state(self) -> None:
        # The caller still holds the mutex. Rewriting the same snapshot is
        # safe even when the first replace committed but its status was lost.
        # Keep state transitions, audit appends and device input out of retries.
        try:
            self._persist_state()
        except (TimeoutError, ConnectionError, InterruptedError, BlockingIOError):
            self._persist_state()

    def _record_audit(self, event: str, result: Mapping[str, Any]) -> dict[str, Any]:
        with self._mutex:
            row = {
                "event": event,
                "request_id": result.get("request_id"),
                "before_state_version": result.get("before_state_version", self._state["state_version"]),
                "after_state_version": result.get("after_state_version", self._state["state_version"]),
                "result": dict(result),
                "timestamp": self.wall_clock(),
                "previous_audit_sha256": self._state.get("last_audit_sha256"),
            }
            row["audit_sha256"] = hashlib.sha256(_canonical(row).encode("utf-8")).hexdigest()
            audit_path = self.state_dir / "control_audit.jsonl"
            stream = permission_retry("audit_open", "control_audit",
                lambda: audit_path.open("a", encoding="utf-8", newline="\n"))
            operation = "audit_write"
            try:
                with stream:
                    stream.write(_canonical(row) + "\n")
                    operation = "audit_flush"
                    stream.flush()
                    operation = "audit_fsync"
                    os.fsync(stream.fileno())
                    operation = "audit_close"
            except OSError as exc:
                # A failed append may already be partial. Do not append again.
                note_metadata_failure(exc, operation, "control_audit")
                raise
            self._state["last_audit_sha256"] = row["audit_sha256"]
            self._write_json(self.state_dir / "control_state.json", self._state)
            return row

    def close(self) -> None:
        with self._mutex:
            self._closed = True
            if self._state["state"] in {"starting", "running", "pause_requested", "paused", "resume_requested"} or (
                    self._state["state"] == "unresponsive" and self._runtime is not None):
                self._state["state"] = "stop_requested"
                self._state["state_version"] += 1
                self._persist_state()
            if self._runtime is not None:
                self._runtime.request("stop")

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
        target = metadata_target(path)
        permission_retry("mkdir", target, lambda: path.parent.mkdir(parents=True, exist_ok=True))
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = permission_retry("temp_open", target,
            lambda: os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        operation = "temp_write"
        primary_failure = None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical(payload))
                operation = "temp_flush"
                stream.flush()
                operation = "temp_fsync"
                os.fsync(stream.fileno())
                operation = "temp_close"
            operation = "replace"
            permission_retry(operation, target, lambda: os.replace(temp, path))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except BaseException as exc:
            primary_failure = exc
            if isinstance(exc, OSError):
                note_metadata_failure(exc, operation, target)
            raise
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                if primary_failure is None:
                    note_metadata_failure(exc, "temp_cleanup", target)
                    raise
