"""Open-source pass-through run control preserving the RunControl boundary.

Public builds require no online license verification, activation receipt, or
runtime authorization grace window. Safe pause/resume/stop lifecycle hooks
remain unchanged on ``RunControl``.
"""
from __future__ import annotations

from pathlib import Path
import time

from core.auto_trainer.runtime_control import ResumeFromCurrentPage, RunControl, RunStopped


class LicensedRunControl(RunControl):
    """Non-gating RunControl subclass preserving caller compatibility."""

    CHECK_INTERVAL = 300.0
    VERIFY_TIMEOUT = 60.0
    RETRY_INTERVAL = 60.0
    MAX_OFFLINE_GRACE = 86400

    def __init__(
        self,
        application_root=None,
        *,
        checker=None,
        sessions=None,
        identity_checker=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.application_root = Path(application_root).resolve() if application_root is not None else None
        self._license_state = {
            "status": "open",
            "code": None,
            "message": "开源版无需激活，可直接运行。",
        }

    def snapshot(self):
        result = super().snapshot()
        with self._condition:
            value = dict(self._license_state)
            value.update(
                remaining_seconds=0,
                online_remaining_seconds=0,
                offline_grace_seconds=0,
                permission_scope="open_source",
                session_reused=False,
                renewal_in_progress=False,
            )
            result["runtime_license"] = value
        return result


__all__ = [
    "LicensedRunControl",
    "ResumeFromCurrentPage",
    "RunStopped",
]
