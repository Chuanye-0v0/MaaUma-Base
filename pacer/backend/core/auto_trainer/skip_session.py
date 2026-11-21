from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


SKIP_SESSION_SCHEMA_VERSION = "uma.run_skip_session.v2"
SKIP_OBSERVATION_LEVELS = frozenset({"absent", "off", "single", "double"})
_PROGRESS_LEVELS = {"off": 0, "single": 1, "double": 2}
_OPERATION_KINDS = frozenset({"initial", "repair"})


class SkipSessionError(ValueError):
    """Raised when persisted or observed run-level skip state is unsafe."""


class SkipSessionState(str, Enum):
    UNKNOWN = "unknown"
    ENABLE_PENDING = "enable_pending"
    ENABLED = "enabled"
    CONFLICT = "conflict"


@dataclass
class RunSkipSession:
    run_session_id: str
    state: SkipSessionState = SkipSessionState.UNKNOWN
    operation_kind: str | None = None
    operation_previous_level: str | None = None
    operation_tap_count: int = 0
    lifetime_toggle_tap_count: int = 0
    regression_level: str | None = None
    regression_frame_count: int = 0
    last_observed_level: str | None = None
    last_source_sha256: str | None = None
    confirmed_source_sha256: str | None = None
    conflict_reason: str | None = None
    deferred_reason: str | None = None
    recovery_observed_at: float | None = None

    @property
    def toggle_tap_count(self) -> int:
        """Compatibility view for callers that only need the lifetime count."""
        return self.lifetime_toggle_tap_count

    @property
    def can_continue_operation(self) -> bool:
        return bool(
            self.state == SkipSessionState.ENABLE_PENDING
            and self.operation_tap_count < 2
        )

    @classmethod
    def new(cls, run_session_id: str) -> "RunSkipSession":
        session = cls(run_session_id=str(run_session_id))
        session._validate()
        return session

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        expected_run_session_id: str,
    ) -> "RunSkipSession":
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SkipSessionError(f"skip_session_read:{type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise SkipSessionError("skip_session_root_not_object")
        if payload.get("schema_version") != SKIP_SESSION_SCHEMA_VERSION:
            raise SkipSessionError("skip_session_schema_version")
        if payload.get("run_session_id") != expected_run_session_id:
            raise SkipSessionError("run_session_id_mismatch")
        try:
            state = SkipSessionState(str(payload.get("state")))
        except ValueError as exc:
            raise SkipSessionError("skip_session_state") from exc
        session = cls(
            run_session_id=str(payload.get("run_session_id")),
            state=state,
            operation_kind=payload.get("operation_kind"),
            operation_previous_level=payload.get("operation_previous_level"),
            operation_tap_count=payload.get("operation_tap_count"),
            lifetime_toggle_tap_count=payload.get("lifetime_toggle_tap_count"),
            regression_level=payload.get("regression_level"),
            regression_frame_count=payload.get("regression_frame_count"),
            last_observed_level=payload.get("last_observed_level"),
            last_source_sha256=payload.get("last_source_sha256"),
            confirmed_source_sha256=payload.get("confirmed_source_sha256"),
            conflict_reason=payload.get("conflict_reason"),
            deferred_reason=payload.get("deferred_reason"),
            recovery_observed_at=payload.get("recovery_observed_at"),
        )
        session._validate()
        return session

    def observe(self, level: str, *, source_sha256: str) -> None:
        normalized = str(level)
        if normalized not in SKIP_OBSERVATION_LEVELS:
            raise SkipSessionError("observation_level")
        self._validate_sha(source_sha256, "source_sha256")
        previous_source = self.last_source_sha256
        self.last_observed_level = normalized
        self.last_source_sha256 = source_sha256

        if self.state == SkipSessionState.CONFLICT:
            self.defer("legacy_optional_skip:" + str(self.conflict_reason))
        if normalized == "double":
            self._confirm_enabled(source_sha256)
            return
        if self.state == SkipSessionState.UNKNOWN:
            return
        if self.state == SkipSessionState.ENABLE_PENDING:
            if normalized == "absent":
                self._clear_regression()
                self.defer("pending_level_unreadable")
                return
            previous = self.operation_previous_level
            if previous is None or _PROGRESS_LEVELS[normalized] <= _PROGRESS_LEVELS[previous]:
                self.defer(f"pending_not_advanced:{previous}->{normalized}")
                return
            if self.deferred_reason == "pending_level_unreadable":
                # Resume only the remaining off->single->double operation;
                # distinct recent observations must show that the first tap
                # advanced before a formerly obscured toggle is retried.
                if previous != "off" or normalized != "single" or self.operation_tap_count != 1:
                    return
                now = time.monotonic()
                recent = (self.recovery_observed_at is not None
                          and 0 <= now-self.recovery_observed_at <= 10)
                if self.regression_level != normalized or not recent:
                    self.regression_level, self.regression_frame_count = normalized, 1
                elif source_sha256 != previous_source:
                    self.regression_frame_count = min(2, self.regression_frame_count + 1)
                self.recovery_observed_at = now
                if self.regression_frame_count >= 2:
                    self.deferred_reason = None
            return
        if self.state == SkipSessionState.ENABLED:
            if normalized == "absent":
                self._clear_regression()
                return
            if normalized in {"off", "single"}:
                if normalized == self.regression_level:
                    self.regression_frame_count = min(2, self.regression_frame_count + 1)
                else:
                    self.regression_level = normalized
                    self.regression_frame_count = 1

    def may_toggle(self, observed_level: str) -> bool:
        if self.deferred_reason is not None:
            return False
        normalized = str(observed_level)
        if normalized not in {"off", "single"}:
            return False
        if self.last_observed_level != normalized:
            return False
        if self.state == SkipSessionState.UNKNOWN:
            return self.operation_tap_count == 0
        if self.state == SkipSessionState.ENABLE_PENDING:
            previous = self.operation_previous_level
            return bool(
                self.operation_tap_count < 2
                and previous in _PROGRESS_LEVELS
                and _PROGRESS_LEVELS[normalized] > _PROGRESS_LEVELS[previous]
            )
        return bool(
            self.state == SkipSessionState.ENABLED
            and self.regression_frame_count >= 1
            and self.regression_level == normalized
        )

    def mark_toggle_performed(self, *, source_sha256: str) -> None:
        self._validate_sha(source_sha256, "source_sha256")
        observed_level = self.last_observed_level
        if not self.may_toggle(str(observed_level)):
            raise SkipSessionError("toggle_not_allowed")
        if self.state == SkipSessionState.UNKNOWN:
            self.state = SkipSessionState.ENABLE_PENDING
            self.operation_kind = "initial"
            self.operation_tap_count = 1
        elif self.state == SkipSessionState.ENABLED:
            self.state = SkipSessionState.ENABLE_PENDING
            self.operation_kind = "repair"
            self.operation_tap_count = 1
            self._clear_regression()
        else:
            self.operation_tap_count += 1
        self.operation_previous_level = observed_level
        self.lifetime_toggle_tap_count += 1
        self.last_source_sha256 = source_sha256

    def to_dict(self) -> dict[str, Any]:
        self._validate()
        return {
            "schema_version": SKIP_SESSION_SCHEMA_VERSION,
            "run_session_id": self.run_session_id,
            "state": self.state.value,
            "operation_kind": self.operation_kind,
            "operation_previous_level": self.operation_previous_level,
            "operation_tap_count": self.operation_tap_count,
            "lifetime_toggle_tap_count": self.lifetime_toggle_tap_count,
            "regression_level": self.regression_level,
            "regression_frame_count": self.regression_frame_count,
            "last_observed_level": self.last_observed_level,
            "last_source_sha256": self.last_source_sha256,
            "confirmed_source_sha256": self.confirmed_source_sha256,
            "conflict_reason": self.conflict_reason,
            "deferred_reason": self.deferred_reason,
            "recovery_observed_at": self.recovery_observed_at,
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            temp_path.replace(target)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _confirm_enabled(self, source_sha256: str) -> None:
        self.state = SkipSessionState.ENABLED
        self.operation_kind = None
        self.operation_previous_level = None
        self.operation_tap_count = 0
        self._clear_regression()
        self.confirmed_source_sha256 = source_sha256
        self.conflict_reason = None
        self.deferred_reason = None

    def defer(self, reason: str) -> None:
        """An optional animation-speed setting cannot block the career."""
        if reason == "pending_level_unreadable" and self.state == SkipSessionState.ENABLE_PENDING:
            # Preserve the bounded operation, including a fresh recovery probe.
            if self.deferred_reason != reason:
                self._clear_regression()
            self.deferred_reason = reason
            return
        self.state = SkipSessionState.UNKNOWN
        self.operation_kind = self.operation_previous_level = None
        self.operation_tap_count = 0
        self._clear_regression()
        self.conflict_reason = None
        self.confirmed_source_sha256 = None
        self.deferred_reason = str(reason)

    def _clear_regression(self) -> None:
        self.regression_level = None
        self.regression_frame_count = 0
        self.recovery_observed_at = None

    def _validate(self) -> None:
        if not isinstance(self.run_session_id, str) or not self.run_session_id.strip():
            raise SkipSessionError("run_session_id")
        if not isinstance(self.state, SkipSessionState):
            raise SkipSessionError("skip_session_state")
        if self.operation_kind is not None and self.operation_kind not in _OPERATION_KINDS:
            raise SkipSessionError("operation_kind")
        if self.operation_previous_level is not None and self.operation_previous_level not in {"off", "single"}:
            raise SkipSessionError("operation_previous_level")
        self._validate_count("operation_tap_count", self.operation_tap_count, {0, 1, 2})
        if isinstance(self.lifetime_toggle_tap_count, bool) or not isinstance(self.lifetime_toggle_tap_count, int) or self.lifetime_toggle_tap_count < 0:
            raise SkipSessionError("lifetime_toggle_tap_count")
        if self.lifetime_toggle_tap_count < self.operation_tap_count:
            raise SkipSessionError("lifetime_toggle_tap_count")
        self._validate_count("regression_frame_count", self.regression_frame_count, {0, 1, 2})
        if self.regression_level is not None and self.regression_level not in {"off", "single"}:
            raise SkipSessionError("regression_level")
        if (self.regression_level is None) != (self.regression_frame_count == 0):
            raise SkipSessionError("regression_probe")
        if self.last_observed_level is not None and self.last_observed_level not in SKIP_OBSERVATION_LEVELS:
            raise SkipSessionError("last_observed_level")
        for name, value in (("last_source_sha256", self.last_source_sha256), ("confirmed_source_sha256", self.confirmed_source_sha256)):
            if value is not None:
                self._validate_sha(value, name)
        if self.conflict_reason is not None and not isinstance(self.conflict_reason, str):
            raise SkipSessionError("conflict_reason")
        if self.deferred_reason is not None and not isinstance(self.deferred_reason, str):
            raise SkipSessionError("deferred_reason")
        if self.recovery_observed_at is not None and (type(self.recovery_observed_at) not in {int,float}
                or not math.isfinite(self.recovery_observed_at) or self.recovery_observed_at < 0):
            raise SkipSessionError("recovery_observed_at")
        if self.state == SkipSessionState.ENABLE_PENDING:
            if self.operation_kind not in _OPERATION_KINDS or self.operation_previous_level not in {"off", "single"} or self.operation_tap_count not in {1, 2}:
                raise SkipSessionError("pending_operation")
        elif self.state != SkipSessionState.CONFLICT and any((self.operation_kind is not None, self.operation_previous_level is not None, self.operation_tap_count != 0)):
            raise SkipSessionError("operation_outside_pending")
        if self.state == SkipSessionState.ENABLED and self.confirmed_source_sha256 is None:
            raise SkipSessionError("enabled_without_confirmation")
        if self.state == SkipSessionState.CONFLICT and not self.conflict_reason:
            raise SkipSessionError("conflict_without_reason")

    @staticmethod
    def _validate_count(name: str, value: Any, allowed: set[int]) -> None:
        if isinstance(value, bool) or value not in allowed:
            raise SkipSessionError(name)

    @staticmethod
    def _validate_sha(value: str, field: str) -> None:
        if not isinstance(value, str) or len(value) != 64:
            raise SkipSessionError(field)
        if any(char not in "0123456789abcdefABCDEF" for char in value):
            raise SkipSessionError(field)
