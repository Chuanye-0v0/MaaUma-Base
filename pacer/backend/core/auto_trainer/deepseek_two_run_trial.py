from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from config.settings import ADB_ENDPOINT, ADB_PORT
from core.auto_trainer.deepseek_runtime_artifact import (
    DeepSeekRuntimeArtifactError,
    RuntimeArtifactIdentity,
    verify_deepseek_runtime_artifact,
)


_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_ROLES = ("observer", "advisor")
_ROOT_FIELDS = {
    "schema_version",
    "trial_id",
    "trial_authorized",
    "production_ready",
    "adb_endpoint",
    "expected_model_id",
    "resource_policy",
    "runtime",
    "environment_policy",
    "runs",
}
_RESOURCE_FIELDS = {
    "paid_resource_allowed",
    "carrot_allowed",
    "clock_allowed",
    "gacha_allowed",
    "shop_allowed",
}
_RUNTIME_FIELDS = {
    "node_executable_path",
    "node_executable_sha256",
    "runtime_root",
    "manifest_path",
    "manifest_sha256",
    "tree_sha256",
    "entrypoint",
    "command_cwd",
}
_ENVIRONMENT_FIELDS = {
    "base_url",
    "timeout_ms",
    "credential_name",
    "credential_persisted",
    "forbidden_names",
}
_RUN_FIELDS = {
    "run_session_id",
    "structured_advisor_mode",
    "invalid_response_policy",
    "command_argv_sha256",
}
_FIXED_BASE_URL = "https://api.deepseek.com"
_FIXED_TIMEOUT_MS = 60_000
_FIXED_ENTRYPOINT = "build/gameplay-cli.js"
_FIXED_MANIFEST_PATH = "runtime-manifest.json"


class DeepSeekTwoRunTrialError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedDeepSeekTwoRunTrial:
    trial_id: str
    contract_sha256: str
    runtime_tree_sha256: str
    runtime_manifest_sha256: str


def build_deepseek_two_run_trial_contract(
    *,
    trial_id: str,
    observer_session_id: str,
    advisor_session_id: str,
    expected_model_id: str,
    runtime_root: str | Path,
    runtime_manifest: str | Path,
    node_executable: str | Path,
    environment_base_url: str,
    environment_timeout_ms: int,
    observer_command: Sequence[str],
    advisor_command: Sequence[str],
    command_cwd: str | Path,
) -> dict[str, object]:
    if (
        not _safe_id(trial_id)
        or not _safe_id(observer_session_id)
        or not _safe_id(advisor_session_id)
        or observer_session_id == advisor_session_id
        or not _safe_id(expected_model_id)
        or environment_base_url != _FIXED_BASE_URL
        or type(environment_timeout_ms) is not int
        or environment_timeout_ms != _FIXED_TIMEOUT_MS
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    identity = _verify_runtime_for_build(runtime_root, runtime_manifest)
    resolved_node = _resolve_safe_regular_file(
        node_executable,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    resolved_cwd = _resolve_safe_directory(
        command_cwd,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    expected_entrypoint = (identity.runtime_root / identity.entrypoint).resolve(
        strict=True
    )
    expected_command = (str(resolved_node), str(expected_entrypoint))
    if (
        resolved_cwd != identity.runtime_root
        or tuple(observer_command) != expected_command
        or tuple(advisor_command) != expected_command
    ):
        raise DeepSeekTwoRunTrialError("deepseek_trial_command_mismatch")
    try:
        node_sha256 = hashlib.sha256(resolved_node.read_bytes()).hexdigest()
    except OSError as error:
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_node_mismatch"
        ) from error
    runs = {
        role: {
            "run_session_id": session_id,
            "structured_advisor_mode": role,
            "invalid_response_policy": "local_fallback",
            "command_argv_sha256": command_argv_sha256(expected_command),
        }
        for role, session_id in (
            ("observer", observer_session_id),
            ("advisor", advisor_session_id),
        )
    }
    return {
        "schema_version": "uma.deepseek_two_run_trial.v2",
        "trial_id": trial_id,
        "trial_authorized": True,
        "production_ready": False,
        "adb_endpoint": ADB_ENDPOINT,
        "expected_model_id": expected_model_id,
        "resource_policy": {
            "paid_resource_allowed": False,
            "carrot_allowed": False,
            "clock_allowed": False,
            "gacha_allowed": False,
            "shop_allowed": False,
        },
        "runtime": {
            "node_executable_path": str(resolved_node),
            "node_executable_sha256": node_sha256,
            "runtime_root": str(identity.runtime_root),
            "manifest_path": _FIXED_MANIFEST_PATH,
            "manifest_sha256": identity.manifest_sha256,
            "tree_sha256": identity.tree_sha256,
            "entrypoint": identity.entrypoint,
            "command_cwd": str(identity.runtime_root),
        },
        "environment_policy": {
            "base_url": _FIXED_BASE_URL,
            "timeout_ms": _FIXED_TIMEOUT_MS,
            "credential_name": "DEEPSEEK_API_KEY",
            "credential_persisted": False,
            "forbidden_names": ["NODE_OPTIONS", "NODE_PATH"],
        },
        "runs": runs,
    }


def validate_deepseek_two_run_trial(
    path: str | Path,
    *,
    role: str,
    run_session_id: str,
    structured_advisor_mode: str,
    structured_advisor_command: Sequence[str],
    structured_advisor_model_id: str,
    structured_advisor_invalid_policy: str,
    live: bool,
    adb_port: int | None,
    command_cwd: str | Path,
) -> ValidatedDeepSeekTwoRunTrial:
    contract, contract_sha256 = _load_contract(path)
    if role not in _ROLES:
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_role_required"
        )
    selected = contract["runs"][role]
    if structured_advisor_mode != role:
        raise DeepSeekTwoRunTrialError("deepseek_trial_mode_mismatch")
    if run_session_id != selected["run_session_id"]:
        raise DeepSeekTwoRunTrialError("deepseek_trial_session_mismatch")
    if structured_advisor_model_id != contract["expected_model_id"]:
        raise DeepSeekTwoRunTrialError("deepseek_trial_model_mismatch")
    if (
        structured_advisor_invalid_policy
        != selected["invalid_response_policy"]
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_invalid_policy_mismatch"
        )

    identity, resolved_node = _verify_runtime_binding(contract)
    resolved_cwd = _resolve_safe_directory(
        command_cwd,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    expected_command = (
        str(resolved_node),
        str((identity.runtime_root / identity.entrypoint).resolve(strict=True)),
    )
    if (
        resolved_cwd != identity.runtime_root
        or tuple(structured_advisor_command) != expected_command
        or command_argv_sha256(structured_advisor_command)
        != selected["command_argv_sha256"]
    ):
        raise DeepSeekTwoRunTrialError("deepseek_trial_command_mismatch")
    if live and (
        contract["adb_endpoint"] != ADB_ENDPOINT
        or adb_port != ADB_PORT
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_adb_endpoint_mismatch"
        )
    return ValidatedDeepSeekTwoRunTrial(
        trial_id=contract["trial_id"],
        contract_sha256=contract_sha256,
        runtime_tree_sha256=identity.tree_sha256,
        runtime_manifest_sha256=identity.manifest_sha256,
    )


def _verify_runtime_for_build(
    runtime_root: str | Path,
    runtime_manifest: str | Path,
) -> RuntimeArtifactIdentity:
    root_path = _absolute_without_resolving(runtime_root)
    manifest_path = _absolute_without_resolving(runtime_manifest)
    _reject_reparse_chain(
        root_path,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    if not os.path.lexists(manifest_path):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_manifest_invalid"
        )
    _reject_reparse_chain(
        manifest_path,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    try:
        return verify_deepseek_runtime_artifact(root_path, manifest_path)
    except DeepSeekRuntimeArtifactError as error:
        _map_runtime_artifact_error(error)


def _verify_runtime_binding(
    contract: dict[str, object],
) -> tuple[RuntimeArtifactIdentity, Path]:
    runtime = contract["runtime"]
    assert isinstance(runtime, dict)
    root_path = _absolute_without_resolving(runtime["runtime_root"])
    if str(root_path) != runtime["runtime_root"]:
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_path_unsafe"
        )
    _reject_reparse_chain(
        root_path,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    manifest_path = root_path / str(runtime["manifest_path"])
    if not os.path.lexists(manifest_path):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_manifest_invalid"
        )
    _reject_reparse_chain(
        manifest_path,
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    try:
        identity = verify_deepseek_runtime_artifact(root_path, manifest_path)
    except DeepSeekRuntimeArtifactError as error:
        _map_runtime_artifact_error(error)
    if (
        str(identity.runtime_root) != runtime["runtime_root"]
        or runtime["manifest_path"] != _FIXED_MANIFEST_PATH
        or identity.manifest_sha256 != runtime["manifest_sha256"]
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_manifest_invalid"
        )
    if (
        identity.tree_sha256 != runtime["tree_sha256"]
        or identity.entrypoint != runtime["entrypoint"]
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_tree_mismatch"
        )
    if runtime["command_cwd"] != str(identity.runtime_root):
        raise DeepSeekTwoRunTrialError("deepseek_trial_command_mismatch")

    resolved_node = _resolve_safe_regular_file(
        runtime["node_executable_path"],
        error_code="deepseek_trial_runtime_path_unsafe",
    )
    if str(resolved_node) != runtime["node_executable_path"]:
        raise DeepSeekTwoRunTrialError("deepseek_trial_node_mismatch")
    try:
        node_sha256 = hashlib.sha256(resolved_node.read_bytes()).hexdigest()
    except OSError as error:
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_node_mismatch"
        ) from error
    if node_sha256 != runtime["node_executable_sha256"]:
        raise DeepSeekTwoRunTrialError("deepseek_trial_node_mismatch")
    return identity, resolved_node


def _map_runtime_artifact_error(
    error: DeepSeekRuntimeArtifactError,
) -> None:
    code = str(error)
    if code == "deepseek_runtime_tree_mismatch":
        mapped = "deepseek_trial_runtime_tree_mismatch"
    elif code == "deepseek_runtime_path_unsafe":
        mapped = "deepseek_trial_runtime_path_unsafe"
    else:
        mapped = "deepseek_trial_runtime_manifest_invalid"
    raise DeepSeekTwoRunTrialError(mapped) from error


def _load_contract(
    path: str | Path,
) -> tuple[dict[str, object], str]:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        ) from error
    if not raw or len(raw) > 65_536:
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        ) from error
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS:
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    if (
        payload.get("schema_version") != "uma.deepseek_two_run_trial.v2"
        or payload.get("trial_authorized") is not True
        or payload.get("production_ready") is not False
        or payload.get("adb_endpoint") != ADB_ENDPOINT
        or not _safe_id(payload.get("trial_id"))
        or not _safe_id(payload.get("expected_model_id"))
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    _validate_resource_policy(payload.get("resource_policy"))
    _validate_runtime_fields(payload.get("runtime"))
    _validate_environment_policy(payload.get("environment_policy"))
    _validate_runs(payload.get("runs"))
    return payload, hashlib.sha256(raw).hexdigest()


def _validate_resource_policy(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _RESOURCE_FIELDS
        or any(value[field] is not False for field in _RESOURCE_FIELDS)
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )


def _validate_runtime_fields(value: object) -> None:
    if not isinstance(value, dict) or set(value) != _RUNTIME_FIELDS:
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    string_fields = (
        "node_executable_path",
        "runtime_root",
        "manifest_path",
        "entrypoint",
        "command_cwd",
    )
    if any(
        not isinstance(value.get(field), str) or not value[field]
        for field in string_fields
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    if (
        value["manifest_path"] != _FIXED_MANIFEST_PATH
        or value["entrypoint"] != _FIXED_ENTRYPOINT
        or not _is_sha(value.get("node_executable_sha256"))
        or not _is_sha(value.get("manifest_sha256"))
        or not _is_sha(value.get("tree_sha256"))
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )


def _validate_environment_policy(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _ENVIRONMENT_FIELDS
        or value.get("base_url") != _FIXED_BASE_URL
        or value.get("timeout_ms") != _FIXED_TIMEOUT_MS
        or value.get("credential_name") != "DEEPSEEK_API_KEY"
        or value.get("credential_persisted") is not False
        or value.get("forbidden_names") != ["NODE_OPTIONS", "NODE_PATH"]
    ):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )


def _validate_runs(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(_ROLES):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )
    session_ids: list[str] = []
    for role in _ROLES:
        run = value.get(role)
        if (
            not isinstance(run, dict)
            or set(run) != _RUN_FIELDS
            or run.get("structured_advisor_mode") != role
            or run.get("invalid_response_policy") != "local_fallback"
            or not _safe_id(run.get("run_session_id"))
            or not _is_sha(run.get("command_argv_sha256"))
        ):
            raise DeepSeekTwoRunTrialError(
                "deepseek_two_run_trial_contract_invalid"
            )
        session_ids.append(run["run_session_id"])
    if len(set(session_ids)) != len(session_ids):
        raise DeepSeekTwoRunTrialError(
            "deepseek_two_run_trial_contract_invalid"
        )


def command_argv_sha256(command: Sequence[str]) -> str:
    if (
        not isinstance(command, (list, tuple))
        or not command
        or any(not isinstance(item, str) or not item for item in command)
    ):
        raise DeepSeekTwoRunTrialError("deepseek_trial_command_mismatch")
    return hashlib.sha256(
        json.dumps(
            list(command),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _absolute_without_resolving(path: object) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_path_unsafe"
        )
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as error:
        raise DeepSeekTwoRunTrialError(
            "deepseek_trial_runtime_path_unsafe"
        ) from error


def _reject_reparse_chain(path: Path, *, error_code: str) -> None:
    current = _absolute_without_resolving(path)
    while True:
        if os.path.lexists(current):
            try:
                info = current.stat(follow_symlinks=False)
            except OSError as error:
                raise DeepSeekTwoRunTrialError(error_code) from error
            attributes = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            try:
                is_symlink = current.is_symlink()
            except OSError as error:
                raise DeepSeekTwoRunTrialError(error_code) from error
            if attributes & reparse or is_symlink:
                raise DeepSeekTwoRunTrialError(error_code)
        parent = current.parent
        if parent == current:
            return
        current = parent


def _resolve_safe_regular_file(
    path: object,
    *,
    error_code: str,
) -> Path:
    absolute = _absolute_without_resolving(path)
    _reject_reparse_chain(absolute, error_code=error_code)
    try:
        info = absolute.stat(follow_symlinks=False)
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise DeepSeekTwoRunTrialError(error_code) from error
    if not stat.S_ISREG(info.st_mode):
        raise DeepSeekTwoRunTrialError(error_code)
    return resolved


def _resolve_safe_directory(path: object, *, error_code: str) -> Path:
    absolute = _absolute_without_resolving(path)
    _reject_reparse_chain(absolute, error_code=error_code)
    try:
        info = absolute.stat(follow_symlinks=False)
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise DeepSeekTwoRunTrialError(error_code) from error
    if not stat.S_ISDIR(info.st_mode):
        raise DeepSeekTwoRunTrialError(error_code)
    return resolved


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA_PATTERN.fullmatch(value) is not None


def _safe_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_ID_PATTERN.fullmatch(value) is not None
    )
