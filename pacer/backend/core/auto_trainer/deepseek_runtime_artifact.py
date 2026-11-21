from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


GAMEPLAY_BUILD_FILES = (
    "build/audit.js",
    "build/config.js",
    "build/context/ref-policy.js",
    "build/context/schema.js",
    "build/errors.js",
    "build/gameplay-cli.js",
    "build/gameplay/action-selector.js",
    "build/providers/deepseek-client.js",
    "build/providers/types.js",
    "build/security/redact.js",
    "build/security/sensitive-data.js",
    "build/tools/contracts.js",
)
RUNTIME_PACKAGE_BYTES = (
    b'{"name":"uma-deepseek-responder-runtime",'
    b'"private":true,"type":"module"}\n'
)
RUNTIME_MANIFEST_NAME = "runtime-manifest.json"
RUNTIME_SCHEMA_VERSION = "uma.deepseek_runtime_artifact.v1"
RUNTIME_ENTRYPOINT = "build/gameplay-cli.js"

_SOURCE_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_FIELDS = {
    "schema_version",
    "source_commit",
    "entrypoint",
    "files",
    "file_count",
    "total_bytes",
    "tree_sha256",
}
_FILE_RECORD_FIELDS = {"path", "size", "sha256"}


class DeepSeekRuntimeArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeArtifactIdentity:
    runtime_root: Path
    manifest_path: Path
    manifest_sha256: str
    tree_sha256: str
    entrypoint: str
    file_count: int
    total_bytes: int


def _raise(code: str) -> None:
    raise DeepSeekRuntimeArtifactError(code)


def _absolute_without_resolving(path: str | os.PathLike[str]) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        _raise("deepseek_runtime_path_unsafe")


def _reject_reparse(path: Path) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except (OSError, ValueError):
        _raise("deepseek_runtime_path_unsafe")
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse:
        _raise("deepseek_runtime_path_unsafe")
    try:
        if path.is_symlink():
            _raise("deepseek_runtime_path_unsafe")
    except OSError:
        _raise("deepseek_runtime_path_unsafe")


def _reject_reparse_chain(path: Path) -> None:
    current = _absolute_without_resolving(path)
    while True:
        if os.path.lexists(current):
            _reject_reparse(current)
        parent = current.parent
        if parent == current:
            return
        current = parent


def _require_safe_directory(path: Path) -> Path:
    absolute = _absolute_without_resolving(path)
    _reject_reparse_chain(absolute)
    try:
        info = absolute.stat(follow_symlinks=False)
    except OSError:
        _raise("deepseek_runtime_path_unsafe")
    if not stat.S_ISDIR(info.st_mode):
        _raise("deepseek_runtime_path_unsafe")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError:
        _raise("deepseek_runtime_path_unsafe")
    _reject_reparse_chain(resolved)
    return resolved


def _require_safe_regular_file(path: Path) -> Path:
    absolute = _absolute_without_resolving(path)
    _reject_reparse_chain(absolute)
    try:
        info = absolute.stat(follow_symlinks=False)
    except OSError:
        _raise("deepseek_runtime_path_unsafe")
    if not stat.S_ISREG(info.st_mode):
        _raise("deepseek_runtime_path_unsafe")
    _reject_alternate_streams(absolute)
    try:
        return absolute.resolve(strict=True)
    except OSError:
        _raise("deepseek_runtime_path_unsafe")


def _reject_alternate_streams(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _WIN32_FIND_STREAM_DATA(ctypes.Structure):
            _fields_ = (
                ("StreamSize", ctypes.c_longlong),
                ("cStreamName", wintypes.WCHAR * 296),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        find_first = kernel32.FindFirstStreamW
        find_first.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
            wintypes.DWORD,
        )
        find_first.restype = wintypes.HANDLE
        find_next = kernel32.FindNextStreamW
        find_next.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
        )
        find_next.restype = wintypes.BOOL
        find_close = kernel32.FindClose
        find_close.argtypes = (wintypes.HANDLE,)
        find_close.restype = wintypes.BOOL

        data = _WIN32_FIND_STREAM_DATA()
        handle = find_first(str(path), 0, ctypes.byref(data), 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            if error in (2, 38):
                return
            _raise("deepseek_runtime_path_unsafe")
        try:
            while True:
                if data.cStreamName != "::$DATA":
                    _raise("deepseek_runtime_path_unsafe")
                if not find_next(handle, ctypes.byref(data)):
                    error = ctypes.get_last_error()
                    if error == 38:
                        break
                    _raise("deepseek_runtime_path_unsafe")
        finally:
            find_close(handle)
    except DeepSeekRuntimeArtifactError:
        raise
    except Exception:
        _raise("deepseek_runtime_path_unsafe")


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        _raise("deepseek_runtime_manifest_invalid")
    if "\\" in value or ":" in value or "\x00" in value:
        _raise("deepseek_runtime_manifest_invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        _raise("deepseek_runtime_manifest_invalid")
    if pure.as_posix() != value:
        _raise("deepseek_runtime_manifest_invalid")
    return value


def _file_record(path: Path, relative_path: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        _raise("deepseek_runtime_path_unsafe")
    return {
        "path": relative_path,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _tree_sha(records: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(
        records,
        key=lambda item: str(item["path"]).encode("utf-8"),
    ):
        material = (
            f'{record["path"]}\0{record["size"]}\0'
            f'{record["sha256"]}\n'
        ).encode("utf-8")
        digest.update(material)
    return digest.hexdigest()


def _encode_manifest(payload: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        _raise("deepseek_runtime_manifest_invalid")


def _collect_zod_files(source_zod_root: Path) -> list[tuple[str, bytes]]:
    collected: list[tuple[str, bytes]] = []

    def visit(directory: Path, relative_directory: PurePosixPath) -> None:
        _require_safe_directory(directory)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.encode("utf-8"))
        except OSError:
            _raise("deepseek_runtime_path_unsafe")
        for child in children:
            _reject_reparse(child)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                _raise("deepseek_runtime_path_unsafe")
            child_relative = relative_directory / child.name
            if stat.S_ISDIR(info.st_mode):
                visit(child, child_relative)
            elif stat.S_ISREG(info.st_mode):
                source_file = _require_safe_regular_file(child)
                try:
                    raw = source_file.read_bytes()
                except OSError:
                    _raise("deepseek_runtime_path_unsafe")
                collected.append(
                    (f"node_modules/zod/{child_relative.as_posix()}", raw)
                )
            else:
                _raise("deepseek_runtime_path_unsafe")

    visit(source_zod_root, PurePosixPath())
    if not collected:
        _raise("deepseek_runtime_path_unsafe")
    return collected


def _validate_copy_paths(files: Sequence[tuple[str, bytes]]) -> None:
    casefolded: set[str] = set()
    for relative_path, _ in files:
        validated = _validate_relative_path(relative_path)
        folded = validated.casefold()
        if folded in casefolded:
            _raise("deepseek_runtime_manifest_invalid")
        casefolded.add(folded)


def _exclusive_write(path: Path, raw: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except FileExistsError:
        _raise("deepseek_runtime_target_exists")
    except OSError:
        _raise("deepseek_runtime_path_unsafe")


def build_deepseek_runtime_artifact(
    source_build_root: str | os.PathLike[str],
    source_zod_root: str | os.PathLike[str],
    target_root: str | os.PathLike[str],
    source_commit: str,
) -> RuntimeArtifactIdentity:
    if not isinstance(source_commit, str) or not _SOURCE_COMMIT_PATTERN.fullmatch(
        source_commit
    ):
        _raise("deepseek_runtime_source_commit_invalid")

    build_root = _require_safe_directory(Path(source_build_root))
    zod_root = _require_safe_directory(Path(source_zod_root))
    target = _absolute_without_resolving(target_root)
    if ":" in target.name or "\x00" in target.name:
        _raise("deepseek_runtime_path_unsafe")
    if os.path.lexists(target):
        _raise("deepseek_runtime_target_exists")
    parent = _require_safe_directory(target.parent)
    target = parent / target.name

    files: list[tuple[str, bytes]] = [("package.json", RUNTIME_PACKAGE_BYTES)]
    for destination_relative in GAMEPLAY_BUILD_FILES:
        source_relative = Path(destination_relative).relative_to("build")
        source_file = _require_safe_regular_file(build_root / source_relative)
        try:
            raw = source_file.read_bytes()
        except OSError:
            _raise("deepseek_runtime_path_unsafe")
        files.append((destination_relative, raw))
    files.extend(_collect_zod_files(zod_root))
    _validate_copy_paths(files)
    files.sort(key=lambda item: item[0].encode("utf-8"))

    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        _raise("deepseek_runtime_target_exists")
    except OSError:
        _raise("deepseek_runtime_path_unsafe")

    directory_paths = {
        PurePosixPath(relative_path).parent
        for relative_path, _ in files
        if PurePosixPath(relative_path).parent != PurePosixPath(".")
    }
    expanded_directories: set[PurePosixPath] = set()
    for directory in directory_paths:
        while directory != PurePosixPath("."):
            expanded_directories.add(directory)
            directory = directory.parent
    for directory in sorted(
        expanded_directories,
        key=lambda item: (len(item.parts), item.as_posix().encode("utf-8")),
    ):
        try:
            (target / Path(*directory.parts)).mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            _raise("deepseek_runtime_target_exists")
        except OSError:
            _raise("deepseek_runtime_path_unsafe")

    records: list[dict[str, object]] = []
    for relative_path, raw in files:
        destination = target / Path(*PurePosixPath(relative_path).parts)
        _exclusive_write(destination, raw)
        records.append(
            {
                "path": relative_path,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    tree_sha256 = _tree_sha(records)
    total_bytes = sum(int(record["size"]) for record in records)
    manifest = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "source_commit": source_commit,
        "entrypoint": RUNTIME_ENTRYPOINT,
        "files": records,
        "file_count": len(records),
        "total_bytes": total_bytes,
        "tree_sha256": tree_sha256,
    }
    manifest_raw = _encode_manifest(manifest)
    manifest_path = target / RUNTIME_MANIFEST_NAME
    _exclusive_write(manifest_path, manifest_raw)
    return verify_deepseek_runtime_artifact(target, manifest_path)


def _parse_manifest(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise("deepseek_runtime_manifest_invalid")
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        _raise("deepseek_runtime_manifest_invalid")
    if payload.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        _raise("deepseek_runtime_manifest_invalid")
    source_commit = payload.get("source_commit")
    if not isinstance(source_commit, str) or not _SOURCE_COMMIT_PATTERN.fullmatch(
        source_commit
    ):
        _raise("deepseek_runtime_manifest_invalid")
    if payload.get("entrypoint") != RUNTIME_ENTRYPOINT:
        _raise("deepseek_runtime_manifest_invalid")
    records = payload.get("files")
    if not isinstance(records, list):
        _raise("deepseek_runtime_manifest_invalid")
    normalized_records: list[dict[str, object]] = []
    casefolded: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != _FILE_RECORD_FIELDS:
            _raise("deepseek_runtime_manifest_invalid")
        relative_path = _validate_relative_path(record.get("path"))
        size = record.get("size")
        sha256 = record.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(sha256, str)
            or not _SHA256_PATTERN.fullmatch(sha256)
        ):
            _raise("deepseek_runtime_manifest_invalid")
        folded = relative_path.casefold()
        if folded in casefolded:
            _raise("deepseek_runtime_manifest_invalid")
        casefolded.add(folded)
        normalized_records.append(
            {"path": relative_path, "size": size, "sha256": sha256}
        )
    expected_order = sorted(
        normalized_records,
        key=lambda item: str(item["path"]).encode("utf-8"),
    )
    if normalized_records != expected_order:
        _raise("deepseek_runtime_manifest_invalid")
    if (
        payload.get("file_count") != len(normalized_records)
        or payload.get("total_bytes")
        != sum(int(record["size"]) for record in normalized_records)
        or payload.get("tree_sha256") != _tree_sha(normalized_records)
    ):
        _raise("deepseek_runtime_manifest_invalid")
    payload["files"] = normalized_records
    return payload


def _enumerate_runtime(
    runtime_root: Path,
    manifest_path: Path,
) -> tuple[list[dict[str, object]], set[str]]:
    records: list[dict[str, object]] = []
    directories: set[str] = set()

    def visit(directory: Path, relative_directory: PurePosixPath) -> None:
        _require_safe_directory(directory)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.encode("utf-8"))
        except OSError:
            _raise("deepseek_runtime_path_unsafe")
        for child in children:
            _reject_reparse(child)
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                _raise("deepseek_runtime_path_unsafe")
            child_relative = relative_directory / child.name
            relative_path = child_relative.as_posix()
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative_path)
                visit(child, child_relative)
            elif stat.S_ISREG(info.st_mode):
                resolved_child = _require_safe_regular_file(child)
                if resolved_child == manifest_path:
                    continue
                records.append(_file_record(resolved_child, relative_path))
            else:
                _raise("deepseek_runtime_path_unsafe")

    visit(runtime_root, PurePosixPath())
    records.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    return records, directories


def verify_deepseek_runtime_artifact(
    runtime_root: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str] | None = None,
) -> RuntimeArtifactIdentity:
    root = _require_safe_directory(Path(runtime_root))
    expected_manifest = root / RUNTIME_MANIFEST_NAME
    if manifest_path is not None:
        supplied = _require_safe_regular_file(Path(manifest_path))
        if supplied != expected_manifest:
            _raise("deepseek_runtime_manifest_invalid")
    manifest_file = _require_safe_regular_file(expected_manifest)
    try:
        manifest_raw = manifest_file.read_bytes()
    except OSError:
        _raise("deepseek_runtime_manifest_invalid")
    manifest = _parse_manifest(manifest_raw)
    manifest_records = manifest["files"]
    assert isinstance(manifest_records, list)

    actual_records, actual_directories = _enumerate_runtime(root, manifest_file)
    expected_directories: set[str] = set()
    for record in manifest_records:
        relative_path = PurePosixPath(str(record["path"]))
        parent = relative_path.parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_directories != expected_directories or actual_records != manifest_records:
        _raise("deepseek_runtime_tree_mismatch")
    actual_tree_sha = _tree_sha(actual_records)
    if (
        actual_tree_sha != manifest["tree_sha256"]
        or len(actual_records) != manifest["file_count"]
        or sum(int(record["size"]) for record in actual_records)
        != manifest["total_bytes"]
    ):
        _raise("deepseek_runtime_tree_mismatch")
    return RuntimeArtifactIdentity(
        runtime_root=root,
        manifest_path=manifest_file,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        tree_sha256=str(manifest["tree_sha256"]),
        entrypoint=str(manifest["entrypoint"]),
        file_count=int(manifest["file_count"]),
        total_bytes=int(manifest["total_bytes"]),
    )


__all__ = [
    "GAMEPLAY_BUILD_FILES",
    "RUNTIME_PACKAGE_BYTES",
    "DeepSeekRuntimeArtifactError",
    "RuntimeArtifactIdentity",
    "build_deepseek_runtime_artifact",
    "verify_deepseek_runtime_artifact",
]
