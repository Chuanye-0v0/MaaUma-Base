from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


ROUTE_TRANSITION_SCHEMA_VERSION = "uma.route_transition_session.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
POST_ACTION_ROUTE_SUCCESSORS = (
    "dialogue",
    "event_choice",
    "loading",
    "main_menu",
    "modal",
)
_DELAYED_EVENT_SOURCE_ACTIONS = frozenset(
    {"training.execute", "rest.execute", "outing.execute", "safe_advance.normal_outing_confirm", "safe_advance.rest_confirm", "safe_advance.rest_outing_confirm", "safe_advance.larc_ss_confirm"}
    | {f"outing.satake_mei.{stage}" for stage in range(1, 6)}
)
def _is_event_source_action(action: str | None) -> bool:
    # Receipt classification follows the planner's slot grammar. Execution still
    # requires the current frame's actual row/count/coordinate contract; a
    # persisted receipt is not permission to perform another input.
    return isinstance(action, str) and re.fullmatch(
        r"event_choice\.(?:top|second|third|fourth|bottom|dialogue_next|option_[1-9][0-9]*)", action
    ) is not None

_EXTERNAL_ACTION_STATE = {
    "training.execute": "training_menu",
    "rest.execute": "main_menu",
    "outing.execute": "main_menu",
    "safe_advance.normal_outing_confirm": "modal",
    "safe_advance.rest_confirm": "modal",
    "safe_advance.rest_outing_confirm": "modal",
    "skill_purchase.return_after_purchase": "skill_acquisition",
}


class RouteTransitionError(ValueError):
    """Raised when a progressive route contract cannot be trusted."""


class RouteTransitionStatus(str, Enum):
    BOOTSTRAP = "bootstrap"
    TRACKING = "tracking"
    MISMATCH = "mismatch"


@dataclass
class RouteTransitionSession:
    run_session_id: str
    status: RouteTransitionStatus = RouteTransitionStatus.BOOTSTRAP
    current_state: str | None = None
    last_action_id: str | None = None
    expected_next_states: tuple[str, ...] = ()
    observation_sha256: str | None = None
    action_source_sha256: str | None = None
    mismatch_state: str | None = None
    transition_version: int = 0

    @classmethod
    def new(cls, run_session_id: str) -> "RouteTransitionSession":
        return cls(run_session_id=_require_text(run_session_id, "run_session_id"))

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        expected_run_session_id: str,
    ) -> "RouteTransitionSession":
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RouteTransitionError(f"route_transition_load:{exc}") from exc
        if not isinstance(payload, dict):
            raise RouteTransitionError("route_transition_root_not_object")
        if payload.get("schema_version") != ROUTE_TRANSITION_SCHEMA_VERSION:
            raise RouteTransitionError("schema_version")
        run_session_id = _require_text(payload.get("run_session_id"), "run_session_id")
        if run_session_id != expected_run_session_id:
            raise RouteTransitionError("run_session_id_mismatch")
        try:
            status = RouteTransitionStatus(payload.get("status"))
        except (TypeError, ValueError) as exc:
            raise RouteTransitionError("status") from exc
        current_state = _optional_text(payload.get("current_state"), "current_state")
        last_action_id = _optional_text(payload.get("last_action_id"), "last_action_id")
        mismatch_state = _optional_text(payload.get("mismatch_state"), "mismatch_state")
        expected_next_states = _normalize_states(payload.get("expected_next_states", ()))
        observation_sha256 = _optional_sha(payload.get("observation_sha256"), "observation_sha256")
        action_source_sha256 = _optional_sha(
            payload.get("action_source_sha256"),
            "action_source_sha256",
        )
        # Older v1 writers retained the now-consumed pending contract after
        # marking a mismatch.  A mismatch is already fail-closed and requires
        # an explicit resync, so discard only those stale pending fields while
        # preserving the mismatch evidence and last action for audit.
        if status == RouteTransitionStatus.MISMATCH:
            expected_next_states = ()
            action_source_sha256 = None
        transition_version = payload.get("transition_version", 0)
        if isinstance(transition_version, bool) or not isinstance(transition_version, int) or transition_version < 0:
            raise RouteTransitionError("transition_version")
        session = cls(
            run_session_id=run_session_id,
            status=status,
            current_state=current_state,
            last_action_id=last_action_id,
            expected_next_states=expected_next_states,
            observation_sha256=observation_sha256,
            action_source_sha256=action_source_sha256,
            mismatch_state=mismatch_state,
            transition_version=transition_version,
        )
        session._validate_invariants()
        return session

    def observe(
        self,
        observed_state: str,
        *,
        source_sha256: str,
        resync: bool = False,
    ) -> None:
        state = _require_text(observed_state, "observed_state")
        source_sha = _require_sha(source_sha256, "source_sha256")
        if resync:
            self.status = RouteTransitionStatus.TRACKING
            self.current_state = state
            self.expected_next_states = ()
            self.observation_sha256 = source_sha
            self.action_source_sha256 = None
            self.mismatch_state = None
            self.transition_version += 1
            return
        if self.status == RouteTransitionStatus.MISMATCH:
            raise RouteTransitionError("route_transition_mismatch_requires_resync")
        if self.expected_next_states:
            if source_sha == self.action_source_sha256 and not str(self.last_action_id).startswith("wait:"):
                self._mark_mismatch(state)
                raise RouteTransitionError("transition_frame_not_fresh")
            if state not in self.expected_next_states:
                self._mark_mismatch(state)
                raise RouteTransitionError("expected_transition_mismatch")
        elif self.current_state is not None and state != self.current_state:
            self._mark_mismatch(state)
            raise RouteTransitionError("transition_contract_missing")
        self.status = RouteTransitionStatus.TRACKING
        self.current_state = state
        self.observation_sha256 = source_sha
        keep_delayed_event_pending = (
            state in POST_ACTION_ROUTE_SUCCESSORS
            and self.last_action_id in _DELAYED_EVENT_SOURCE_ACTIONS
            and "event_choice" in self.expected_next_states
        )
        # With skip enabled, a successful event click can still be followed by
        # event/dialogue/loading frames before the game returns home on its own.
        # These observations do not finish the original action's transition.
        keep_event_transition_pending = (
            _is_event_source_action(self.last_action_id)
            and state in {"event_choice", "dialogue", "loading"}
            and bool(self.expected_next_states)
        )
        keep_wait_transition_pending = (
            str(self.last_action_id).startswith("wait:")
            and state in {"loading", "dialogue", "modal", "event_choice"}
            and bool(self.expected_next_states)
        )
        # Modal shells can remain visible while their close/continue action
        # leads to dialogue or a new page. A new input replaces this pending
        # transition; observing the shell alone must not erase it.
        keep_modal_transition_pending = state == "modal" and bool(self.expected_next_states)
        if not (keep_delayed_event_pending or keep_event_transition_pending or keep_wait_transition_pending
                or keep_modal_transition_pending):
            self.expected_next_states = ()
            self.action_source_sha256 = None
        self.mismatch_state = None
        self.transition_version += 1

    def can_resume_skipped_event_to_home(self) -> bool:
        """Recognize the old writer's consumed transition after an auto-skip."""
        return (self.status == RouteTransitionStatus.TRACKING
                and self.current_state == "event_choice"
                and self.last_action_id in _DELAYED_EVENT_SOURCE_ACTIONS
                and not self.expected_next_states)

    def commit_action(
        self,
        action_id: str,
        *,
        expected_next_states: Iterable[str],
        source_sha256: str,
    ) -> None:
        if self.status != RouteTransitionStatus.TRACKING or self.current_state is None:
            raise RouteTransitionError("route_transition_not_tracking")
        action = _require_text(action_id, "action_id")
        states = _normalize_states(expected_next_states)
        if not states:
            raise RouteTransitionError("expected_next_states_required")
        source_sha = _require_sha(source_sha256, "source_sha256")
        if source_sha != self.observation_sha256:
            raise RouteTransitionError("action_source_sha_mismatch")
        self.last_action_id = action
        self.expected_next_states = states
        self.action_source_sha256 = source_sha
        self.transition_version += 1

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, destination)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ROUTE_TRANSITION_SCHEMA_VERSION,
            "run_session_id": self.run_session_id,
            "status": self.status.value,
            "current_state": self.current_state,
            "last_action_id": self.last_action_id,
            "expected_next_states": list(self.expected_next_states),
            "observation_sha256": self.observation_sha256,
            "action_source_sha256": self.action_source_sha256,
            "mismatch_state": self.mismatch_state,
            "transition_version": self.transition_version,
        }

    def _mark_mismatch(self, state: str) -> None:
        self.status = RouteTransitionStatus.MISMATCH
        self.mismatch_state = state
        self.expected_next_states = ()
        self.action_source_sha256 = None
        self.transition_version += 1

    def _validate_invariants(self) -> None:
        if self.status == RouteTransitionStatus.BOOTSTRAP:
            if any(
                value is not None
                for value in (
                    self.current_state,
                    self.last_action_id,
                    self.observation_sha256,
                    self.action_source_sha256,
                    self.mismatch_state,
                )
            ) or self.expected_next_states:
                raise RouteTransitionError("bootstrap_invariant")
        if self.expected_next_states and (
            self.status != RouteTransitionStatus.TRACKING
            or self.action_source_sha256 is None
            or self.last_action_id is None
        ):
            raise RouteTransitionError("pending_transition_invariant")
        if self.status == RouteTransitionStatus.TRACKING and (
            self.current_state is None or self.observation_sha256 is None
        ):
            raise RouteTransitionError("tracking_invariant")
        if self.status == RouteTransitionStatus.MISMATCH and self.mismatch_state is None:
            raise RouteTransitionError("mismatch_invariant")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouteTransitionError(field)
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _normalize_states(values: Any) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise RouteTransitionError("expected_next_states")
    normalized = sorted({_require_text(value, "expected_next_states") for value in values})
    return tuple(normalized)


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.lower()) is None:
        raise RouteTransitionError(field)
    return value.lower()


def _optional_sha(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_sha(value, field)


def record_external_action_receipt(
    session_path: str | Path,
    *,
    expected_run_session_id: str,
    action_state: str,
    action_id: str,
    source_path: str | Path,
    source_sha256: str,
) -> RouteTransitionSession:
    action = _require_text(action_id, "action_id")
    state = _require_text(action_state, "action_state")
    expected_state = "event_choice" if _is_event_source_action(action) else _EXTERNAL_ACTION_STATE.get(action)
    if expected_state != state:
        raise RouteTransitionError("external_action_state_mismatch")
    expected_sha = _require_sha(source_sha256, "source_sha256")
    try:
        source = Path(source_path).resolve(strict=True)
        source_bytes = source.read_bytes()
    except OSError as exc:
        raise RouteTransitionError(f"external_action_source:{exc}") from exc
    if not source_bytes:
        raise RouteTransitionError("external_action_source_empty")
    actual_sha = hashlib.sha256(source_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise RouteTransitionError("external_action_source_sha_mismatch")

    persisted = RouteTransitionSession.load(
        session_path,
        expected_run_session_id=expected_run_session_id,
    )
    candidate = copy.deepcopy(persisted)
    if action in {"training.execute", "rest.execute", "outing.execute", "safe_advance.normal_outing_confirm", "safe_advance.rest_confirm", "safe_advance.rest_outing_confirm"}:
        candidate.status = RouteTransitionStatus.TRACKING
        candidate.current_state = state
        candidate.expected_next_states = ()
        candidate.observation_sha256 = actual_sha
        candidate.action_source_sha256 = None
        candidate.mismatch_state = None
        candidate.transition_version += 1
    else:
        candidate.observe(state, source_sha256=actual_sha)
    candidate.commit_action(
        action,
        expected_next_states=POST_ACTION_ROUTE_SUCCESSORS,
        source_sha256=actual_sha,
    )
    candidate.save(session_path)
    return candidate
