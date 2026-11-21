from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Callable, Literal, Mapping, Protocol

from core.auto_trainer.career_supervisor_turn import TurnDecisionPacket
from core.auto_trainer.structured_advisor import (
    StructuredAdvisorError,
    StructuredAdvisorRequest,
    StructuredAdvisorResolution,
    build_structured_advisor_request,
    resolve_structured_advisor_response,
)


_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_SYSTEM_ENV_KEYS = (
    "SystemRoot",
    "SystemDrive",
    "ComSpec",
    "TEMP",
    "TMP",
    "USERPROFILE",
)
_FIXED_BASE_URL = "https://api.deepseek.com"
_SAFE_GATEWAY_ERROR_CODES = frozenset(
    {
        "INVALID_CONTEXT_PACKET",
        "SENSITIVE_DATA_BLOCKED",
        "PROVIDER_NOT_ALLOWED",
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_REJECTED",
        "INVALID_MODEL_OUTPUT",
        "PATCH_POLICY_VIOLATION",
        "INTERNAL_ERROR",
    }
)
_SAFE_PROVIDER_CONTENT_KINDS = frozenset(
    {
        "tool_call_missing",
        "tool_call_count_invalid",
        "tool_call_wrong_name",
        "tool_arguments_invalid",
        "tool_arguments_missing_action_id",
        "tool_arguments_extra_fields",
        "tool_action_not_allowed",
    }
)
_SAFE_AUDIT_REQUIRED_KEYS = frozenset(
    {
        "timestamp",
        "requestId",
        "tool",
        "provider",
        "model",
        "packet",
        "durationMs",
        "status",
        "errorCode",
    }
)
_SAFE_AUDIT_OPTIONAL_KEYS = frozenset({"contentKind"})
_SAFE_CLI_ERROR_KEYS = frozenset({"event", "code", "retryable"})
_CLASSIFIED_COMMAND_FAILURE_PREFIX = "advisor_command_failed_"
_CLASSIFIED_UNAVAILABLE_PREFIX = "advisor_unavailable_"
_SAFE_CLASSIFIED_FAILURE_SUFFIXES = frozenset(
    {code.lower() for code in _SAFE_GATEWAY_ERROR_CODES}
    | {
        f"invalid_model_output_{kind}"
        for kind in _SAFE_PROVIDER_CONTENT_KINDS
    }
)


class StructuredAdvisorClient(Protocol):
    def request_advice(
        self,
        payload: dict,
    ) -> Mapping[str, object] | None: ...


class _AdvisorSecret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "[redacted]"

    __str__ = __repr__

    def reveal(self) -> str:
        return self._value


def build_restricted_advisor_environment(
    source_environment: Mapping[str, str],
    *,
    expected_model_id: str,
    base_url: str,
    timeout_ms: int,
) -> dict[str, str]:
    if (
        not isinstance(source_environment, Mapping)
        or not isinstance(expected_model_id, str)
        or _SAFE_MODEL_PATTERN.fullmatch(expected_model_id) is None
        or base_url != _FIXED_BASE_URL
        or type(timeout_ms) is not int
        or not 1_000 <= timeout_ms <= 300_000
    ):
        raise StructuredAdvisorError("advisor_environment_invalid")
    try:
        items = tuple(source_environment.items())
    except Exception as error:
        raise StructuredAdvisorError(
            "advisor_environment_invalid"
        ) from error
    snapshot: dict[str, tuple[str, str]] = {}
    try:
        for key, value in items:
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or "\x00" in key
                or "\x00" in value
            ):
                raise TypeError
            folded = key.casefold()
            if folded in snapshot:
                raise ValueError
            snapshot[folded] = (key, value)
    except Exception as error:
        raise StructuredAdvisorError(
            "advisor_environment_invalid"
        ) from error

    result: dict[str, str] = {}
    for canonical_name in _SYSTEM_ENV_KEYS:
        item = snapshot.get(canonical_name.casefold())
        if item is not None:
            result[canonical_name] = item[1]
    credential = snapshot.get("deepseek_api_key")
    if credential is not None:
        value = credential[1]
        if not value or len(value) > 8_192:
            raise StructuredAdvisorError(
                "advisor_environment_invalid"
            )
        result["DEEPSEEK_API_KEY"] = value
    result["DEEPSEEK_BASE_URL"] = base_url
    result["DEEPSEEK_MODEL"] = expected_model_id
    result["DEEPSEEK_TIMEOUT_MS"] = str(timeout_ms)
    return result


class CommandStructuredAdvisorClient:
    """Call an explicitly configured local adapter without shell expansion."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        expected_model_id: str,
        timeout_seconds: int = 60,
        cwd: str | Path | None = None,
        max_stdout_chars: int = 16_384,
        source_environment: Mapping[str, str] | None = None,
        base_url: str = _FIXED_BASE_URL,
        provider_timeout_ms: int = 60_000,
        max_stderr_chars: int = 8_192,
    ) -> None:
        if (
            not isinstance(command, tuple)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise StructuredAdvisorError("advisor_command_invalid")
        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 300
        ):
            raise StructuredAdvisorError("advisor_timeout_invalid")
        if (
            type(max_stdout_chars) is not int
            or not 256 <= max_stdout_chars <= 1_048_576
            or type(max_stderr_chars) is not int
            or not 256 <= max_stderr_chars <= 1_048_576
        ):
            raise StructuredAdvisorError("advisor_output_limit_invalid")
        restricted = build_restricted_advisor_environment(
            os.environ if source_environment is None else source_environment,
            expected_model_id=expected_model_id,
            base_url=base_url,
            timeout_ms=provider_timeout_ms,
        )
        credential_value = restricted.pop("DEEPSEEK_API_KEY", None)
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.cwd = Path(cwd) if cwd is not None else None
        self.max_stdout_chars = max_stdout_chars
        self.max_stderr_chars = max_stderr_chars
        self._environment = MappingProxyType(dict(restricted))
        self._credential = (
            _AdvisorSecret(credential_value)
            if credential_value is not None
            else None
        )

    @property
    def credential_present(self) -> bool:
        return self._credential is not None

    def request_advice(
        self,
        payload: dict,
    ) -> Mapping[str, object] | None:
        try:
            request_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise StructuredAdvisorError(
                "advisor_command_output_invalid"
            ) from None
        child_environment = dict(self._environment)
        if self._credential is not None:
            child_environment["DEEPSEEK_API_KEY"] = (
                self._credential.reveal()
            )
        returncode, stdout_text, stderr_text = _run_bounded_command(
            self.command,
            request_bytes=request_bytes,
            timeout_seconds=self.timeout_seconds,
            cwd=str(self.cwd) if self.cwd is not None else None,
            environment=child_environment,
            max_stdout_bytes=self.max_stdout_chars,
            max_stderr_bytes=self.max_stderr_chars,
        )
        if returncode != 0:
            classified = _classify_safe_advisor_stderr(stderr_text)
            if classified is not None:
                raise StructuredAdvisorError(
                    f"{_CLASSIFIED_COMMAND_FAILURE_PREFIX}{classified}"
                )
            raise StructuredAdvisorError("advisor_command_failed")
        stdout = stdout_text.strip()
        if not stdout or len(stdout) > self.max_stdout_chars:
            raise StructuredAdvisorError("advisor_command_output_invalid")
        try:
            decoded = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            raise StructuredAdvisorError(
                "advisor_command_output_invalid"
            ) from None
        if not isinstance(decoded, dict):
            raise StructuredAdvisorError("advisor_command_output_invalid")
        return decoded


def _read_bounded_pipe(
    stream: BinaryIO,
    *,
    limit: int,
    chunks: list[bytes],
    overflow: threading.Event,
    process: subprocess.Popen,
) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            total += len(chunk)
            if total > limit:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            chunks.append(chunk)
    except OSError:
        overflow.set()
        try:
            process.kill()
        except OSError:
            pass


def _run_bounded_command(
    command: tuple[str, ...],
    *,
    request_bytes: bytes,
    timeout_seconds: int,
    cwd: str | None,
    environment: Mapping[str, str],
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> tuple[int, str, str]:
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt"
        else 0
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            cwd=cwd,
            env=dict(environment),
            creationflags=creationflags,
        )
    except (OSError, ValueError) as error:
        raise StructuredAdvisorError(
            "advisor_command_failed"
        ) from error
    if process.stdin is None or process.stdout is None or process.stderr is None:
        try:
            process.kill()
        except OSError:
            pass
        raise StructuredAdvisorError("advisor_command_failed")

    overflow = threading.Event()
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    readers = (
        threading.Thread(
            target=_read_bounded_pipe,
            kwargs={
                "stream": process.stdout,
                "limit": max_stdout_bytes,
                "chunks": stdout_chunks,
                "overflow": overflow,
                "process": process,
            },
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_pipe,
            kwargs={
                "stream": process.stderr,
                "limit": max_stderr_bytes,
                "chunks": stderr_chunks,
                "overflow": overflow,
                "process": process,
            },
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    timed_out = False
    write_failed = False
    try:
        process.stdin.write(request_bytes)
        process.stdin.close()
    except OSError:
        write_failed = True
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
    for reader in readers:
        reader.join(timeout=2)
    readers_stuck = any(reader.is_alive() for reader in readers)
    if readers_stuck:
        overflow.set()
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    if overflow.is_set() or readers_stuck:
        raise StructuredAdvisorError("advisor_command_output_invalid")
    if timed_out or write_failed:
        raise StructuredAdvisorError("advisor_command_failed")
    try:
        stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="strict")
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise StructuredAdvisorError(
            "advisor_command_output_invalid"
        ) from None
    return process.returncode, stdout_text, stderr_text


def _classify_safe_advisor_stderr(stderr_text: str) -> str | None:
    if not isinstance(stderr_text, str):
        return None
    lines = stderr_text.splitlines()
    if not 1 <= len(lines) <= 2 or any(not line for line in lines):
        return None

    audit_code: str | None = None
    audit_content_kind: str | None = None
    cli_code: str | None = None
    for line in lines:
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(item, dict):
            return None
        keys = frozenset(item)
        if keys == _SAFE_CLI_ERROR_KEYS:
            code = item.get("code")
            if (
                item.get("event") != "gameplay_cli_error"
                or item.get("retryable") is not False
                or not isinstance(code, str)
                or code not in _SAFE_GATEWAY_ERROR_CODES
                or cli_code is not None
            ):
                return None
            cli_code = code
            continue

        if (
            not _SAFE_AUDIT_REQUIRED_KEYS.issubset(keys)
            or not keys.issubset(
                _SAFE_AUDIT_REQUIRED_KEYS | _SAFE_AUDIT_OPTIONAL_KEYS
            )
            or audit_code is not None
        ):
            return None
        packet = item.get("packet")
        error_code = item.get("errorCode")
        content_kind = item.get("contentKind")
        if (
            item.get("tool") != "select_allowed_action"
            or item.get("provider") != "deepseek"
            or item.get("status") != "error"
            or not isinstance(item.get("timestamp"), str)
            or not isinstance(item.get("requestId"), str)
            or not isinstance(item.get("model"), str)
            or type(item.get("durationMs")) is not int
            or int(item["durationMs"]) < 0
            or not isinstance(packet, dict)
            or frozenset(packet) != {
                "sha256",
                "bytes",
                "snippetCount",
            }
            or not isinstance(packet.get("sha256"), str)
            or _SHA_PATTERN.fullmatch(packet["sha256"]) is None
            or type(packet.get("bytes")) is not int
            or int(packet["bytes"]) < 0
            or type(packet.get("snippetCount")) is not int
            or int(packet["snippetCount"]) < 0
            or not isinstance(error_code, str)
            or error_code not in _SAFE_GATEWAY_ERROR_CODES
        ):
            return None
        if content_kind is not None and (
            error_code != "INVALID_MODEL_OUTPUT"
            or not isinstance(content_kind, str)
            or content_kind not in _SAFE_PROVIDER_CONTENT_KINDS
        ):
            return None
        audit_code = error_code
        audit_content_kind = content_kind

    if audit_code is not None and cli_code is not None:
        if audit_code != cli_code:
            return None
        code = audit_code
    else:
        code = audit_code if audit_code is not None else cli_code
    if code is None:
        return None
    suffix = code.lower()
    if code == "INVALID_MODEL_OUTPUT" and audit_content_kind is not None:
        suffix = f"{suffix}_{audit_content_kind}"
    return suffix


def _classified_unavailable_reason(error_code: str) -> str | None:
    if not error_code.startswith(_CLASSIFIED_COMMAND_FAILURE_PREFIX):
        return None
    suffix = error_code[len(_CLASSIFIED_COMMAND_FAILURE_PREFIX) :]
    if suffix not in _SAFE_CLASSIFIED_FAILURE_SUFFIXES:
        return None
    return f"{_CLASSIFIED_UNAVAILABLE_PREFIX}{suffix}"


@dataclass(frozen=True)
class StructuredAdvisorAudit:
    schema_version: str
    mode: Literal["observer", "advisor"]
    run_session_id: str
    generation: int
    global_action_turn: int
    decision_packet_sha256: str
    request_sha256: str
    decision_context_sha256: str
    expected_model_id: str
    response_received: bool
    resolution_status: str
    resolution_reason: str
    local_preferred_action_id: str
    advisor_action_id: str | None
    selected_action_id: str | None
    advisor_selection_applied: bool
    safety_gate_state: Literal["pending"]
    execution_authorized: bool
    audit_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != "uma.structured_advisor_audit.v1":
            raise StructuredAdvisorError("advisor_audit_schema_invalid")
        if self.safety_gate_state != "pending":
            raise StructuredAdvisorError("advisor_audit_gate_not_pending")
        if self.execution_authorized:
            raise StructuredAdvisorError(
                "advisor_audit_cannot_authorize_execution"
            )
        if type(self.response_received) is not bool:
            raise StructuredAdvisorError(
                "advisor_audit_response_flag_invalid"
            )
        if type(self.advisor_selection_applied) is not bool:
            raise StructuredAdvisorError(
                "advisor_audit_selection_flag_invalid"
            )
        _require_sha(
            self.decision_packet_sha256,
            "advisor_audit_decision_sha_invalid",
        )
        _require_sha(
            self.request_sha256,
            "advisor_audit_request_sha_invalid",
        )
        _require_sha(
            self.decision_context_sha256,
            "advisor_audit_context_sha_invalid",
        )
        _require_sha(self.audit_sha256, "advisor_audit_sha_invalid")
        payload = self.to_dict()
        payload.pop("audit_sha256")
        if _canonical_sha(payload) != self.audit_sha256:
            raise StructuredAdvisorError("advisor_audit_sha_mismatch")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "run_session_id": self.run_session_id,
            "generation": self.generation,
            "global_action_turn": self.global_action_turn,
            "decision_packet_sha256": self.decision_packet_sha256,
            "request_sha256": self.request_sha256,
            "decision_context_sha256": self.decision_context_sha256,
            "expected_model_id": self.expected_model_id,
            "response_received": self.response_received,
            "resolution_status": self.resolution_status,
            "resolution_reason": self.resolution_reason,
            "local_preferred_action_id": (
                self.local_preferred_action_id
            ),
            "advisor_action_id": self.advisor_action_id,
            "selected_action_id": self.selected_action_id,
            "advisor_selection_applied": (
                self.advisor_selection_applied
            ),
            "safety_gate_state": self.safety_gate_state,
            "execution_authorized": self.execution_authorized,
            "audit_sha256": self.audit_sha256,
        }


@dataclass(frozen=True)
class StructuredAdvisorOutcome:
    request: StructuredAdvisorRequest
    resolution: StructuredAdvisorResolution
    audit: StructuredAdvisorAudit

    @property
    def selected_action_id(self) -> str | None:
        return self.resolution.selected_action_id


class StructuredAdvisorRuntime:
    def __init__(
        self,
        *,
        mode: Literal["observer", "advisor"],
        expected_model_id: str,
        client: StructuredAdvisorClient,
        clock_unix_ms: Callable[[], int],
        ttl_ms: int,
        invalid_response_policy: Literal[
            "local_fallback",
            "stop",
        ] = "local_fallback",
    ) -> None:
        if mode not in {"observer", "advisor"}:
            raise StructuredAdvisorError("advisor_mode_invalid")
        if not isinstance(expected_model_id, str) or not expected_model_id:
            raise StructuredAdvisorError("advisor_model_id_invalid")
        if not callable(clock_unix_ms):
            raise StructuredAdvisorError("advisor_clock_invalid")
        if type(ttl_ms) is not int or not 1 <= ttl_ms <= 60_000:
            raise StructuredAdvisorError("advisor_ttl_invalid")
        if invalid_response_policy not in {"local_fallback", "stop"}:
            raise StructuredAdvisorError("advisor_invalid_policy")
        self.mode = mode
        self.expected_model_id = expected_model_id
        self.client = client
        self.clock_unix_ms = clock_unix_ms
        self.ttl_ms = ttl_ms
        self.invalid_response_policy = invalid_response_policy

    def evaluate(
        self,
        decision_packet: TurnDecisionPacket,
        *,
        decision_context: Mapping[str, object] | None = None,
    ) -> StructuredAdvisorOutcome:
        issued_at = self.clock_unix_ms()
        if type(issued_at) is not int or issued_at < 0:
            raise StructuredAdvisorError("advisor_now_invalid")
        request = build_structured_advisor_request(
            decision_packet=decision_packet,
            mode=self.mode,
            expected_model_id=self.expected_model_id,
            decision_context=decision_context,
            issued_at_unix_ms=issued_at,
            expires_at_unix_ms=issued_at + self.ttl_ms,
        )
        stop_only = (
            len(request.allowed_candidates) == 1
            and request.allowed_candidates[0].action_kind == "stop"
        )
        raw_response: Mapping[str, object] | None = None
        response_received = False
        unavailable_reason: str | None = None
        if not stop_only:
            try:
                raw_response = self.client.request_advice(
                    request.to_dict()
                )
            except StructuredAdvisorError as error:
                unavailable_reason = _classified_unavailable_reason(
                    error.code
                )
                raw_response = None
            except Exception:
                raw_response = None
            response_received = isinstance(raw_response, Mapping)
        resolved_at = self.clock_unix_ms()
        if type(resolved_at) is not int or resolved_at < 0:
            raise StructuredAdvisorError("advisor_now_invalid")
        resolution = resolve_structured_advisor_response(
            request=request,
            raw_response=raw_response,
            now_unix_ms=resolved_at,
            invalid_response_policy=self.invalid_response_policy,
        )
        if (
            raw_response is None
            and unavailable_reason is not None
            and resolution.reason == "advisor_unavailable"
        ):
            resolution = StructuredAdvisorResolution(
                status=resolution.status,
                reason=unavailable_reason,
                selected_action_id=resolution.selected_action_id,
                local_preferred_action_id=(
                    resolution.local_preferred_action_id
                ),
                advisor_action_id=resolution.advisor_action_id,
                advisor_selection_applied=(
                    resolution.advisor_selection_applied
                ),
                decision_packet_sha256=(
                    resolution.decision_packet_sha256
                ),
                request_sha256=resolution.request_sha256,
                safety_gate_required=resolution.safety_gate_required,
                execution_authorized=resolution.execution_authorized,
            )
        audit = _build_audit(
            request=request,
            resolution=resolution,
            response_received=response_received,
        )
        return StructuredAdvisorOutcome(
            request=request,
            resolution=resolution,
            audit=audit,
        )


def _build_audit(
    *,
    request: StructuredAdvisorRequest,
    resolution: StructuredAdvisorResolution,
    response_received: bool,
) -> StructuredAdvisorAudit:
    payload = {
        "schema_version": "uma.structured_advisor_audit.v1",
        "mode": request.mode,
        "run_session_id": request.run_session_id,
        "generation": request.generation,
        "global_action_turn": request.global_action_turn,
        "decision_packet_sha256": request.decision_packet_sha256,
        "request_sha256": request.request_sha256,
        "decision_context_sha256": request.decision_context_sha256,
        "expected_model_id": request.expected_model_id,
        "response_received": response_received,
        "resolution_status": resolution.status,
        "resolution_reason": resolution.reason,
        "local_preferred_action_id": (
            resolution.local_preferred_action_id
        ),
        "advisor_action_id": resolution.advisor_action_id,
        "selected_action_id": resolution.selected_action_id,
        "advisor_selection_applied": (
            resolution.advisor_selection_applied
        ),
        "safety_gate_state": "pending",
        "execution_authorized": False,
    }
    payload["audit_sha256"] = _canonical_sha(payload)
    return StructuredAdvisorAudit(**payload)


def _canonical_sha(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_sha(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise StructuredAdvisorError(code)
