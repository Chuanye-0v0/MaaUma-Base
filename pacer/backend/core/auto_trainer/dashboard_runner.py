from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from core.auto_trainer.dashboard_control import (
    ControlBoundary,
    ControlStateError,
    RunnerControlPort,
    RunnerFactory,
)


PreActionBoundary = Literal["before_capture", "before_decision", "before_tap"]
SIMULATED_PROFILE_ID = "master-cup-simulated"


@dataclass(frozen=True)
class ResyncEvidence:
    fresh_frame: bool
    resynced: bool
    safety_gate_passed: bool
    frame_sha256: str | None = None


@dataclass(frozen=True)
class FreshFrameEvidence:
    path: str
    sha256: str
    captured_at: float


class ResyncProvider(Protocol):
    def __call__(self, *, run_id: str, session_id: str) -> ResyncEvidence: ...


class ControlledStop(RuntimeError):
    def __init__(self, boundary: ControlBoundary):
        super().__init__(f"controlled_stop:{boundary}")
        self.boundary = boundary


class RunnerHeartbeatWorker:
    def __init__(
        self,
        *,
        port: RunnerControlPort,
        interval_seconds: float = 5.0,
        join_timeout_seconds: float = 1.0,
        event_factory: Callable[[], threading.Event] = threading.Event,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat_interval_must_be_positive")
        if join_timeout_seconds <= 0:
            raise ValueError("heartbeat_join_timeout_must_be_positive")
        self.port = port
        self.interval_seconds = float(interval_seconds)
        self.join_timeout_seconds = float(join_timeout_seconds)
        self._stop_event = event_factory()
        self._thread_factory = thread_factory
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def __enter__(self) -> "RunnerHeartbeatWorker":
        if self._thread is not None:
            raise RuntimeError("heartbeat_worker_already_started")
        try:
            self._send_heartbeat()
        except BaseException as exc:
            self._report_failure(exc)
            raise
        self._thread = self._thread_factory(
            target=self._run,
            name=f"dashboard-heartbeat-{self.port.run_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.join_timeout_seconds)
            if thread.is_alive() and exc_type is None:
                raise RuntimeError("heartbeat_worker_join_timeout")
        if exc_type is None:
            self.raise_if_failed()
        return False

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def _report_failure(self, exc: BaseException) -> None:
        self._failure = exc
        self._stop_event.set()
        notify = getattr(self.port, "heartbeat_failed", None)
        if callable(notify):
            try:
                notify(error_type=type(exc).__name__)
            except BaseException:
                # Preserve the original failure even if diagnostic persistence
                # failed too. The bound service requests stop before writing.
                pass

    def _send_heartbeat(self) -> None:
        self.port.heartbeat(run_id=self.port.run_id, session_id=self.port.session_id)

    def _run(self) -> None:
        try:
            while not self._stop_event.wait(self.interval_seconds):
                self._send_heartbeat()
        except BaseException as exc:
            self._report_failure(exc)


class CooperativeRunnerDriver:
    def __init__(
        self,
        *,
        port: RunnerControlPort,
        resync_provider: ResyncProvider,
        heartbeat_worker: RunnerHeartbeatWorker,
        wait: Callable[[float], None],
        pause_poll_seconds: float = 0.1,
    ) -> None:
        if pause_poll_seconds <= 0:
            raise ValueError("pause_poll_seconds_must_be_positive")
        self.port = port
        self.resync_provider = resync_provider
        self.heartbeat_worker = heartbeat_worker
        self.wait = wait
        self.pause_poll_seconds = float(pause_poll_seconds)

    def reach(self, boundary: PreActionBoundary) -> None:
        if boundary not in {"before_capture", "before_decision", "before_tap"}:
            raise ValueError("invalid_cooperative_boundary")
        self.heartbeat_worker.raise_if_failed()
        decision = self.port.checkpoint(
            boundary,
            run_id=self.port.run_id,
            session_id=self.port.session_id,
        )
        self._apply_decision(decision, boundary)

    def after_fresh_frame(self, evidence: FreshFrameEvidence) -> None:
        if not isinstance(evidence.path, str) or not evidence.path:
            raise ValueError("fresh_frame_path_required")
        if not isinstance(evidence.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", evidence.sha256) is None:
            raise ValueError("fresh_frame_sha256_required")
        if (
            isinstance(evidence.captured_at, bool)
            or not isinstance(evidence.captured_at, (int, float))
            or evidence.captured_at <= 0
        ):
            raise ValueError("fresh_frame_capture_time_required")
        self.heartbeat_worker.raise_if_failed()
        decision = self.port.checkpoint(
            "after_fresh_frame",
            run_id=self.port.run_id,
            session_id=self.port.session_id,
            fresh_frame=True,
        )
        self._apply_decision(decision, "after_fresh_frame")

    def _apply_decision(self, decision: str, boundary: ControlBoundary) -> None:
        if decision == "stop":
            raise ControlledStop(boundary)
        if decision == "pause":
            self._wait_until_resumed()
        elif decision != "continue":
            raise RuntimeError("invalid_checkpoint_decision")

    def _wait_until_resumed(self) -> None:
        while True:
            self.heartbeat_worker.raise_if_failed()
            try:
                decision = self.port.checkpoint(
                    "after_resync",
                    run_id=self.port.run_id,
                    session_id=self.port.session_id,
                )
            except ControlStateError as error:
                if error.code != "resume_resync_required":
                    raise
                evidence = self.resync_provider(
                    run_id=self.port.run_id,
                    session_id=self.port.session_id,
                )
                if any(
                    type(value) is not bool
                    for value in (
                        evidence.fresh_frame,
                        evidence.resynced,
                        evidence.safety_gate_passed,
                    )
                ):
                    raise ValueError("resync_evidence_flags_must_be_bool")
                try:
                    decision = self.port.checkpoint(
                        "after_resync",
                        run_id=self.port.run_id,
                        session_id=self.port.session_id,
                        fresh_frame=evidence.fresh_frame,
                        resynced=evidence.resynced,
                        safety_gate_passed=evidence.safety_gate_passed,
                    )
                except ControlStateError as retry_error:
                    if retry_error.code != "resume_resync_required":
                        raise
                    self.wait(self.pause_poll_seconds)
                    continue
            if decision == "continue":
                return
            if decision == "stop":
                raise ControlledStop("after_resync")
            if decision != "pause":
                raise RuntimeError("invalid_checkpoint_decision")
            self.wait(self.pause_poll_seconds)


def make_simulated_runner_factory(
    *,
    boundary_observer: Callable[[ControlBoundary], None] | None = None,
    cycle_wait_seconds: float = 0.1,
    wait: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], float] = time.time,
) -> RunnerFactory:
    if cycle_wait_seconds <= 0:
        raise ValueError("simulation_cycle_wait_must_be_positive")
    observe = boundary_observer or (lambda _boundary: None)

    def run_simulation(profile, port) -> None:
        if profile.profile_id != SIMULATED_PROFILE_ID:
            raise ValueError("unexpected_simulation_profile")

        def simulated_resync_provider(**_) -> ResyncEvidence:
            return ResyncEvidence(
                fresh_frame=True,
                resynced=True,
                safety_gate_passed=True,
                frame_sha256="simulated",
            )

        try:
            with RunnerHeartbeatWorker(port=port) as heartbeat:
                driver = CooperativeRunnerDriver(
                    port=port,
                    resync_provider=simulated_resync_provider,
                    heartbeat_worker=heartbeat,
                    wait=wait,
                )
                while True:
                    for boundary in ("before_capture", "before_decision", "before_tap"):
                        observe(boundary)
                        driver.reach(boundary)
                    observe("after_fresh_frame")
                    driver.after_fresh_frame(FreshFrameEvidence(
                        path="simulated://fresh-frame",
                        sha256="0" * 64,
                        captured_at=wall_clock(),
                    ))
                    wait(cycle_wait_seconds)
        except ControlledStop:
            return

    return run_simulation


__all__ = [
    "ControlledStop",
    "CooperativeRunnerDriver",
    "FreshFrameEvidence",
    "ResyncEvidence",
    "ResyncProvider",
    "RunnerHeartbeatWorker",
    "SIMULATED_PROFILE_ID",
    "make_simulated_runner_factory",
]
