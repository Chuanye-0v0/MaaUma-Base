"""Sanitized traceback persistence and lookup for runner_error stops."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import traceback
import uuid

from core.auto_trainer.pacer_storage import _io

SCHEMA_VERSION = "uma.runner_error.v1"

_USER_HOME_PATH = re.compile(
    r"(?i)(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\r\n<>\"'|]+|"
    r"/(?:Users|home)/[^/\r\n<>\"']+)"
)
_INLINE_SECRET = re.compile(
    r"""(?ix)(\b(?:token|secret|password|passwd|authorization|api[_-]?key|
    account(?:[_-]?(?:id|name))?|user[_-]?name|activation[_-]?code|invite[_-]?code)\s*[:=]\s*)
    (?:(?:"[^"\r\n]*")|(?:'[^'\r\n]*')|[^\s,;]+)"""
)
_TOKENS = re.compile(
    r"(?i)\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{12,}|"
    r"\bAKID[A-Za-z0-9]{16,}|\bsk-[A-Za-z0-9_-]{16,}|"
    r"\b(?:md_|mc_)[A-Za-z0-9._~-]{8,}|\bMU-(?:[A-F0-9]{4}-){3}[A-F0-9]{4}\b"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|password|passwd|authorization|apikey|accesskey|"
    r"activation|invitecode|account|username|nickname)"
)


def redact_text(value: object, *, limit: int = 4000) -> str:
    """Redact user-home directories and inline credentials from error text."""
    text = str(value or "")
    text = _USER_HOME_PATH.sub("<USER_HOME>", text)
    text = _INLINE_SECRET.sub(r"\1<REDACTED>", text)
    text = _TOKENS.sub("<REDACTED>", text)
    if len(text) > limit:
        text = text[-limit:] if limit > 1200 else text[:limit]
    return text


def _short_location(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    for marker in ("/pacer/backend/", "/backend/"):
        if marker in normalized:
            return normalized.rsplit(marker, 1)[-1]
    parts = [p for p in normalized.split("/") if p]
    return "/".join(parts[-3:]) if len(parts) > 3 else (normalized or "<unknown>")


def _primary_frame(error: BaseException) -> tuple[str, int, str]:
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    if not frames:
        return "<unknown>", 0, "<unknown>"
    preferred = [
        f for f in frames
        if ("/pacer/backend/" in f.filename.replace("\\", "/") or "/backend/" in f.filename.replace("\\", "/"))
    ]
    chosen = preferred[-1] if preferred else frames[-1]
    return _short_location(chosen.filename), int(chosen.lineno or 0), str(chosen.name or "<unknown>")


def _atomic_write_json(path: Path, payload: dict) -> None:
    native_parent = _io(path.parent)
    native_parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    native_tmp = _io(tmp)
    try:
        native_tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(native_tmp, _io(path))
    finally:
        try:
            if native_tmp.exists():
                native_tmp.unlink()
        except OSError:
            pass


def build_runner_error_payload(
    error: BaseException,
    *,
    reason: str = "runner_error",
    mode: str | None = None,
    run_id: str | None = None,
) -> dict:
    location, line, function = _primary_frame(error)
    full_tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    redacted_tb = redact_text(full_tb, limit=4000)
    tail = redacted_tb[-1200:] if len(redacted_tb) > 1200 else redacted_tb
    return {
        "schema_version": SCHEMA_VERSION,
        "reason": str(reason or "runner_error")[:96],
        "mode": str(mode)[:32] if isinstance(mode, str) and mode else None,
        "run_id": str(run_id)[:128] if isinstance(run_id, str) and run_id else None,
        "error_type": type(error).__name__[:80],
        "error_message": redact_text(str(error), limit=500),
        "location": location[:200],
        "line": line,
        "function": function[:120],
        "traceback": redacted_tb,
        "traceback_tail": tail,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def record_runner_error(
    target_dir: str | Path | None,
    error: BaseException,
    *,
    reason: str = "runner_error",
    mode: str | None = None,
    run_id: str | None = None,
    control_state_dir: str | Path | None = None,
) -> dict | None:
    """Persist sanitized traceback to run/batch and control dir without ever raising."""
    try:
        payload = build_runner_error_payload(
            error, reason=reason, mode=mode, run_id=run_id
        )
        if target_dir:
            try:
                _atomic_write_json(Path(target_dir) / "runner_error.json", payload)
            except Exception:
                pass
        if control_state_dir:
            try:
                _atomic_write_json(Path(control_state_dir) / "last_runner_error.json", payload)
            except Exception:
                pass
        try:
            print(
                f"[pacer:{payload['reason']}] {payload['error_type']} at "
                f"{payload['location']}:{payload['line']} ({payload['function']}): "
                f"{payload['error_message']}",
                file=sys.stderr,
                flush=True,
            )
            if payload["traceback"]:
                print(payload["traceback"], file=sys.stderr, flush=True)
        except Exception:
            pass
        return payload
    except Exception:
        return None


def load_runner_error(
    path: str | Path,
    *,
    expected_run_id: str | None = None,
    max_bytes: int = 65536,
) -> dict | None:
    """Read a bounded, sanitized runner_error diagnostic document."""
    try:
        native = _io(Path(path))
        if not native.is_file() or native.stat().st_size > max_bytes:
            return None
        data = json.loads(native.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return None
        if expected_run_id is not None and data.get("run_id") not in (None, expected_run_id):
            return None
        error_type = data.get("error_type")
        if not isinstance(error_type, str) or not error_type:
            return None
        line = data.get("line")
        safe_line = line if type(line) is int and line >= 0 else 0
        out = {
            "schema_version": SCHEMA_VERSION,
            "reason": str(data.get("reason") or "runner_error")[:96],
            "error_type": error_type[:80],
            "error_message": redact_text(data.get("error_message", ""), limit=500),
            "location": redact_text(data.get("location", ""), limit=200),
            "line": safe_line,
            "function": str(data.get("function") or "")[:120],
            "traceback_tail": redact_text(
                data.get("traceback_tail") or data.get("traceback") or "", limit=1200
            ),
            "recorded_at_utc": str(data.get("recorded_at_utc") or "")[:48],
        }
        return out
    except (OSError, ValueError, TypeError):
        return None
