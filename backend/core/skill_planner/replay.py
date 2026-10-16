"""Offline capture identity checks for skill-planner replay.

The planner may consume a parsed skill page only when it is bound to the
same screenshot set and turn identifier.  This module is deliberately pure:
it neither captures images nor invokes a purchase executor.
"""
from __future__ import annotations
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
MAX_CAPTURE_FRESHNESS_MS = 30 * 60 * 1000

@dataclass(frozen=True)
class CaptureBindingVerdict:
    accepted: bool
    reason_codes: list[str]

def capture_fingerprint(page_paths: Iterable[str | Path], *, turn_id: str, capture_manifest: Mapping[str, Any] | None=None) -> dict[str, Any]:
    """Return a deterministic identity for an offline screenshot sequence."""
    paths = [Path(path) for path in page_paths]
    if not paths:
        raise ValueError('at least one page is required for a capture fingerprint')
    if not turn_id:
        raise ValueError('turn_id is required for a capture fingerprint')
    digest = hashlib.sha256()
    page_hashes: list[dict[str, str]] = []
    for index, path in enumerate(paths):
        if not path.is_file():
            raise ValueError(f'capture page is missing: {path}')
        content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f'{index}:{path.name}:{content_sha256}\n'.encode('utf-8'))
        page_hashes.append({'name': path.name, 'sha256': content_sha256})
    timing: dict[str, int] = {}
    if capture_manifest is not None:
        if capture_manifest.get('schema_version') != 'uma.skill_acquisition_capture.v1':
            raise ValueError('capture manifest schema mismatch')
        manifest_pages = capture_manifest.get('captured_pages')
        manifest_count = capture_manifest.get('captured_page_count')
        evidence = capture_manifest.get('page_evidence')
        if not isinstance(manifest_pages, list) or manifest_count != len(paths) or len(manifest_pages) != len(paths) or (not isinstance(evidence, list)) or (len(evidence) != len(paths)):
            raise ValueError('capture manifest page count mismatch')
        for index, (path, manifest_path, item) in enumerate(zip(paths, manifest_pages, evidence, strict=True)):
            if not isinstance(manifest_path, str) or Path(manifest_path).resolve() != path.resolve():
                raise ValueError('capture manifest page sequence mismatch')
            if not isinstance(item, Mapping):
                raise ValueError('capture page evidence must be an object')
            if item.get('page_index') != index:
                raise ValueError('capture page evidence index mismatch')
            if not isinstance(item.get('path'), str) or Path(str(item['path'])).resolve() != path.resolve():
                raise ValueError('capture page evidence path mismatch')
            if item.get('raw_sha256') != page_hashes[index]['sha256']:
                raise ValueError('capture page evidence sha256 mismatch')
        captured_at = capture_manifest.get('captured_at_unix_ms')
        expires_at = capture_manifest.get('expires_at_unix_ms')
        if isinstance(captured_at, bool) or not isinstance(captured_at, int) or isinstance(expires_at, bool) or (not isinstance(expires_at, int)):
            raise ValueError('capture timing is required')
        freshness_ms = expires_at - captured_at
        if captured_at <= 0 or not 0 < freshness_ms <= MAX_CAPTURE_FRESHNESS_MS:
            raise ValueError('capture freshness window is invalid')
        digest.update(f'captured_at_unix_ms:{captured_at}\nexpires_at_unix_ms:{expires_at}\n'.encode('utf-8'))
        timing = {'captured_at_unix_ms': captured_at, 'expires_at_unix_ms': expires_at}
    return {'schema_version': 'uma.skill_capture_binding.v1', 'turn_id': str(turn_id), 'capture_sha256': digest.hexdigest(), 'page_hashes': page_hashes, **timing}

def validate_capture_binding(expected: dict[str, Any], observed: dict[str, Any]) -> CaptureBindingVerdict:
    """Fail closed unless both capture SHA and turn identity match exactly."""
    reasons: list[str] = []
    if expected.get('capture_sha256') != observed.get('capture_sha256'):
        reasons.append('capture_sha256_mismatch')
    if expected.get('turn_id') != observed.get('turn_id'):
        reasons.append('turn_id_mismatch')
    for field in ('captured_at_unix_ms', 'expires_at_unix_ms'):
        if field in expected or field in observed:
            if expected.get(field) != observed.get(field):
                reasons.append(f'{field}_mismatch')
    return CaptureBindingVerdict(accepted=not reasons, reason_codes=reasons)

def build_replay_verdict(*, parsed_manifest: dict[str, Any], resolved_manifest: dict[str, Any], planner_observation: dict[str, Any], binding: CaptureBindingVerdict) -> dict[str, Any]:
    """Classify an offline replay without turning it into an executor input."""
    summary = resolved_manifest.get('resolution_summary', {})
    admitted = list(planner_observation.get('available_skills', []))
    current_pt = parsed_manifest.get('current_skill_points')
    parser_ready = binding.accepted and current_pt is not None and (int(summary.get('auto_confirmed', 0)) > 0)
    planner_ready = parser_ready and bool(admitted)
    return {'parser_verdict': 'PARSER_READY' if parser_ready else 'PARSER_NOT_READY', 'planner_verdict': 'PLANNER_READY' if planner_ready else 'PLANNER_BLOCKED_NO_FRESH_INPUT', 'live_purchase_verdict': 'LIVE_PURCHASE_NOT_READY', 'capture_binding_accepted': binding.accepted, 'capture_binding_reasons': list(binding.reason_codes), 'parsed_skill_count': len(parsed_manifest.get('skills', [])), 'auto_confirmed_count': int(summary.get('auto_confirmed', 0)), 'needs_review_count': int(summary.get('needs_review', 0)), 'planner_admitted_count': len(admitted), 'current_skill_points': current_pt, 'purchase_execution_allowed': False}
__all__ = ['CaptureBindingVerdict', 'MAX_CAPTURE_FRESHNESS_MS', 'build_replay_verdict', 'capture_fingerprint', 'validate_capture_binding']
