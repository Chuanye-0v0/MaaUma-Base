"""Read-only skill-list capture and offline planner adapter.

The caller owns the connected device.  This module deliberately has no ADB
imports and can only request the observer-authorized list-scroll action.
"""
from __future__ import annotations
from desktop.umaa_public_context import endpoint as _public_endpoint
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping, MutableMapping
import cv2
from core.skill_acquisition_parser import SkillAcquisitionOcrRuntimeError
from core.skill_ocr_performance import active_harvest_ocr, harvest_skill_ocr
from core.skill_scroll import SKILL_OVERLAP_SWIPE, measure_skill_overlap
from scripts.plan_fresh_skill_acquisition import run_fresh_dry_run
ROOT = Path(__file__).resolve().parents[2]
MAX_SKILL_CAPTURE_PAGES = 64
MAX_UNKNOWN_SKILL_NAVIGATION = 2
_SHA256_LENGTH = 64
_ALLOWED_ENDPOINT = _public_endpoint()
_MAX_CAPTURE_FRESHNESS_MS = 30 * 60 * 1000

def _write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path

def _blocked(output_dir: Path, reason: str, *, capture_manifest: dict[str, Any] | None=None, detail: str | None=None) -> dict[str, Any]:
    result: dict[str, Any] = {'schema_version': 'uma.autonomous_training.skill_prepare.v1', 'status': 'blocked', 'reason': reason, 'execution_allowed': False, 'purchase_executed': False, 'tap_count': 0}
    if capture_manifest is not None:
        result['capture_manifest'] = capture_manifest
    if detail is not None:
        result['detail'] = detail
    result['report_path'] = str(_write_json(output_dir / 'skill_prepare.report.json', result))
    return result

def _capture_page(observation: Any, *, output_dir: Path, page_index: int) -> tuple[Path, dict[str, Any], str | None]:
    if not isinstance(observation, Mapping):
        return (Path(), {}, 'skill_capture_observation_invalid')
    if observation.get('page') != 'skills':
        return (Path(), {}, 'skills_page_required')
    source_value = observation.get('screenshot_path')
    frame_sha = observation.get('frame_sha256')
    if not isinstance(source_value, str) or not source_value:
        return (Path(), {}, 'skill_screenshot_path_missing')
    if not isinstance(frame_sha, str) or len(frame_sha) != _SHA256_LENGTH or any((character not in '0123456789abcdef' for character in frame_sha)):
        return (Path(), {}, 'skill_frame_sha256_invalid')
    source = Path(source_value)
    if not source.is_file() or source.suffix.lower() != '.png':
        return (Path(), {}, 'skill_screenshot_missing')
    raw_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if raw_sha != frame_sha:
        return (Path(), {}, 'skill_frame_sha256_mismatch')
    local_path = output_dir / 'capture' / f'page_{page_index:02d}.png'
    local_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, local_path)
    if hashlib.sha256(local_path.read_bytes()).hexdigest() != raw_sha:
        return (Path(), {}, 'skill_evidence_copy_unverified')
    return (local_path, {'page_index': page_index, 'path': str(local_path), 'source_screenshot_path': str(source), 'raw_sha256': raw_sha, 'frame_sha256': frame_sha}, None)

def _build_context_error(build_context: Any) -> str | None:
    if not isinstance(build_context, Mapping):
        return 'build_context_required'
    turn_id = build_context.get('turn_id')
    if not isinstance(turn_id, str) or not turn_id:
        return 'build_context_turn_id_required'
    if not isinstance(build_context.get('target_mode'), str) or not build_context['target_mode']:
        return 'build_context_target_mode_required'
    if build_context['target_mode'] not in {'parent', 'race'}:
        return 'build_context_target_mode_invalid'
    race_profile = build_context.get('race_profile')
    if not isinstance(race_profile, Mapping):
        return 'build_context_race_profile_required'
    for key in ('distance_type', 'style'):
        if race_profile.get(key) in (None, '', 'unknown'):
            return f'build_context_{key}_required'
    return None

def _capture_timing(observation: Any) -> tuple[int, int] | None:
    if not isinstance(observation, Mapping):
        return None
    captured_at = observation.get('captured_at_unix_ms')
    expires_at = observation.get('expires_at_unix_ms')
    if isinstance(captured_at, bool) or not isinstance(captured_at, int) or isinstance(expires_at, bool) or (not isinstance(expires_at, int)) or (captured_at <= 0) or (not 0 < expires_at - captured_at <= _MAX_CAPTURE_FRESHNESS_MS):
        return None
    return (captured_at, expires_at)

def _planner_block_reason(report: Mapping[str, Any]) -> str | None:
    planner = report.get('planner_dry_run', {})
    if isinstance(planner, Mapping) and planner.get('status') == 'blocked':
        return planner.get('optimizer_status') or 'skill_planner_blocked'
    parser = report.get('parser_manifest')
    if not isinstance(parser, Mapping) or parser.get('current_skill_points') is None:
        return 'skill_points_unknown'
    summary = report.get('resolver_summary')
    if not isinstance(summary, Mapping):
        return 'skill_catalog_unknown'
    verdict = report.get('verdict')
    if not isinstance(verdict, Mapping) or verdict.get('planner_verdict') != 'PLANNER_READY':
        return 'skill_planner_blocked'
    inventory = report.get('inventory_packet')
    batch_plan = report.get('batch_plan')
    if not isinstance(inventory, Mapping) or not isinstance(batch_plan, Mapping):
        return 'skill_planner_artifacts_missing'
    if inventory.get('execution_allowed') is not False or batch_plan.get('execution_allowed') is not False:
        return 'skill_planner_execution_scope_invalid'
    selected_ids = batch_plan.get('selected_action_ids')
    available = report.get('planner_observation', {}).get('available_skills')
    if not isinstance(selected_ids, list) or not isinstance(available, list):
        return 'selected_skill_identity_unconfirmed'
    bindings = {item.get('skill_id'): item.get('identity_binding') for item in available if isinstance(item, Mapping) and isinstance(item.get('skill_id'), str)}
    if any((not isinstance(skill_id, str) or not isinstance(bindings.get(skill_id), Mapping) or bindings[skill_id].get('status') != 'auto_confirmed' for skill_id in selected_ids)):
        return 'selected_skill_identity_unconfirmed'
    return None

@harvest_skill_ocr()
def prepare_skills(device: Any, output_dir: Path, build_context: dict[str, Any] | None, *, expected_endpoint: str=_ALLOWED_ENDPOINT) -> dict[str, Any]:
    """Capture an entire observed skill list and create offline-only plan artifacts.

    ``device`` must already be bound to the intended endpoint.  The only
    device action accepted here is an observer-advertised skill-list scroll.
    No skill selection, confirmation, or purchase action is issued.
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    page_evidence: list[dict[str, Any]] = []
    seen_frame_hashes: set[str] = set()
    overlap_evidence: list[dict[str, Any]] = []
    capture = getattr(device, 'capture', None)
    if active_harvest_ocr() is not None:
        capture = getattr(device, 'capture_skill_scroll', capture)
    step = getattr(device, 'step', None)
    if not callable(capture) or not callable(step):
        return _blocked(destination, 'explicit_device_capture_and_step_required')
    if expected_endpoint not in (_ALLOWED_ENDPOINT, _public_endpoint()) or getattr(device, 'endpoint', None) != expected_endpoint:
        return _blocked(destination, 'device_endpoint_not_allowed')
    context = getattr(device, 'context', None)
    if not isinstance(context, MutableMapping):
        return _blocked(destination, 'explicit_device_context_required')
    context['mode'] = 'harvest'
    observation = capture()
    rewind_count = 0
    unknown_rewinds = 0
    while observation.get('skill_list_at_top') is not True:
        if observation.get('page') != 'skills':
            return _blocked(destination, 'skills_page_required')
        at_top = observation.get('skill_list_at_top')
        if at_top is None:
            if observation.get('skill_list_navigation_verified') is not True:
                return _blocked(destination, 'skill_list_top_unknown')
            if unknown_rewinds >= MAX_UNKNOWN_SKILL_NAVIGATION:
                return _blocked(destination, 'skill_rewind_unknown_budget_exhausted')
            unknown_rewinds += 1
        elif at_top is not False:
            return _blocked(destination, 'skill_list_top_unknown')
        else:
            unknown_rewinds = 0
        if rewind_count >= MAX_SKILL_CAPTURE_PAGES:
            return _blocked(destination, 'skill_rewind_limit_reached')
        if 'scroll_skill_up' not in observation.get('actions', {}):
            return _blocked(destination, 'skill_rewind_action_missing')
        response = step('scroll_skill_up')
        if not isinstance(response, Mapping) or not isinstance(response.get('observation'), Mapping):
            return _blocked(destination, 'skill_scroll_fresh_observation_missing')
        observation = response['observation']
        rewind_count += 1
    timing = _capture_timing(observation)
    if timing is None:
        return _blocked(destination, 'skill_capture_timing_missing')
    actions = observation.get('actions')
    if active_harvest_ocr() is not None and isinstance(actions, Mapping) and ('scroll_skill_overlap' in actions):
        forward_action = 'scroll_skill_overlap'
        swipe_profile = dict(SKILL_OVERLAP_SWIPE)
    elif isinstance(actions, Mapping) and 'scroll_skill_reveal_bottom' in actions:
        forward_action = 'scroll_skill_reveal_bottom'
        swipe_profile = {'name': 'reveal-bottom', 'start': [360, 900], 'end': [360, 760], 'duration_ms': 350}
    else:
        forward_action = 'scroll_skill_list'
        swipe_profile = {'name': 'full-page', 'start': [360, 900], 'end': [360, 610], 'duration_ms': 450}
    unknown_forwards = 0
    while True:
        local_page, evidence, error = _capture_page(observation, output_dir=destination, page_index=len(pages))
        if error is not None:
            return _blocked(destination, error, capture_manifest={'page_evidence': page_evidence})
        frame_sha = evidence['frame_sha256']
        if frame_sha in seen_frame_hashes:
            return _blocked(destination, 'skill_capture_repeated_frame', capture_manifest={'page_evidence': page_evidence})
        seen_frame_hashes.add(frame_sha)
        pages.append(local_page)
        page_evidence.append(evidence)
        at_bottom = observation.get('skill_list_at_bottom')
        if at_bottom is True:
            break
        if at_bottom is None and observation.get('skill_list_navigation_verified') is True:
            if unknown_forwards >= MAX_UNKNOWN_SKILL_NAVIGATION:
                return _blocked(destination, 'skill_forward_unknown_budget_exhausted', capture_manifest={'page_evidence': page_evidence})
            unknown_forwards += 1
        elif at_bottom is not False:
            return _blocked(destination, 'skill_list_bottom_unknown', capture_manifest={'page_evidence': page_evidence})
        else:
            unknown_forwards = 0
        if len(pages) >= MAX_SKILL_CAPTURE_PAGES:
            return _blocked(destination, 'skill_capture_limit_reached', capture_manifest={'page_evidence': page_evidence})
        actions = observation.get('actions')
        if not isinstance(actions, Mapping) or forward_action not in actions:
            return _blocked(destination, 'skill_scroll_action_missing', capture_manifest={'page_evidence': page_evidence})
        try:
            response = step(forward_action)
        except RuntimeError as error:
            if str(error) != f'action_not_allowed:{forward_action}':
                raise
            settled = capture()
            if not isinstance(settled, Mapping) or settled.get('page') != 'skills' or settled.get('skill_list_at_bottom') is not True or (type(observation.get('current_skill_points')) is int and type(settled.get('current_skill_points')) is int and (settled['current_skill_points'] != observation['current_skill_points'])):
                return _blocked(destination, 'skill_scroll_boundary_unverified')
            pages.pop()
            page_evidence.pop()
            seen_frame_hashes.remove(frame_sha)
            observation = settled
            continue
        if not isinstance(response, Mapping) or not isinstance(response.get('observation'), Mapping):
            return _blocked(destination, 'skill_scroll_fresh_observation_missing', capture_manifest={'page_evidence': page_evidence})
        next_observation = response['observation']
        if forward_action == 'scroll_skill_overlap':
            coverage = measure_skill_overlap(cv2.imread(str(local_page)), cv2.imread(str(next_observation.get('screenshot_path', ''))))
            coverage['page_index'] = len(pages)
            overlap_evidence.append(coverage)
            if coverage.get('verified') is not True or next_observation.get('page') != 'skills' or (type(observation.get('current_skill_points')) is int and type(next_observation.get('current_skill_points')) is int and (next_observation['current_skill_points'] != observation['current_skill_points'])):
                return _blocked(destination, 'skill_scroll_overlap_not_verified', capture_manifest=dict(page_evidence=page_evidence, overlap_evidence=overlap_evidence, swipe_profile=swipe_profile))
        observation = next_observation
    manifest = {'schema_version': 'uma.skill_acquisition_capture.v1', 'capture_mode': 'passed_device_screenshot_and_authorized_vertical_scroll_only', 'swipe_profile': swipe_profile, 'captured_pages': [str(path) for path in pages], 'captured_page_count': len(pages), 'page_evidence': page_evidence, 'captured_at_unix_ms': timing[0], 'expires_at_unix_ms': timing[1], 'tap_count': 0, 'purchase_executed': False, 'rewind_scroll_count': rewind_count, 'forward_scroll_count': len(pages) - 1}
    if forward_action == 'scroll_skill_overlap':
        manifest['overlap_evidence'] = overlap_evidence
    manifest_path = _write_json(destination / 'capture_manifest.json', manifest)
    context_error = _build_context_error(build_context)
    if context_error is not None:
        result = _blocked(destination, context_error, capture_manifest=manifest)
        result['capture_manifest_path'] = str(manifest_path)
        _write_json(Path(result['report_path']), result)
        return result
    assert build_context is not None
    resolver_catalog = Path(build_context.get('resolver_catalog') or ROOT / 'data/skills/cn-20260916/cn_skill_resolver_catalog.json')
    planner_catalog = Path(build_context.get('planner_catalog') or ROOT / 'data/skills/cn-20260916/cn_skill_planner_catalog.json')
    if not resolver_catalog.is_file() or not planner_catalog.is_file():
        result = _blocked(destination, 'skill_catalog_files_missing', capture_manifest=manifest)
        result['capture_manifest_path'] = str(manifest_path)
        _write_json(Path(result['report_path']), result)
        return result
    try:
        planner_report = run_fresh_dry_run(pages=pages, resolver_catalog=resolver_catalog, planner_catalog=planner_catalog, output_dir=destination / 'planner', turn_id=build_context['turn_id'], build_context=dict(build_context), capture_manifest=manifest)
    except SkillAcquisitionOcrRuntimeError as error:
        result = _blocked(destination, 'skill_planner_ocr_runtime_unavailable', capture_manifest=manifest, detail=str(error))
        result['capture_manifest_path'] = str(manifest_path)
        _write_json(Path(result['report_path']), result)
        return result
    except Exception as error:
        result = _blocked(destination, 'skill_planner_failed', capture_manifest=manifest, detail=type(error).__name__)
        result['capture_manifest_path'] = str(manifest_path)
        _write_json(Path(result['report_path']), result)
        return result
    return publish_skill_plan(destination, planner_report, manifest)

def publish_skill_plan(destination: Path, planner_report: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Publish validated offline artifacts without recapturing or spending points."""
    manifest_path = destination / 'capture_manifest.json'
    blocked_reason = _planner_block_reason(planner_report)
    if blocked_reason is not None:
        result = _blocked(destination, blocked_reason, capture_manifest=manifest)
        result['capture_manifest_path'] = str(manifest_path)
        result['planner_report_path'] = str(destination / 'planner' / 'fresh_skill_dry_run.report.json')
        _write_json(Path(result['report_path']), result)
        return result
    proposal_path = _write_json(destination / 'skill_proposal.json', planner_report['planner_dry_run'])
    inventory_path = _write_json(destination / 'skill_inventory.packet.json', planner_report['inventory_packet'])
    batch_plan_path = _write_json(destination / 'skill_batch.plan.json', planner_report['batch_plan'])
    result = {'schema_version': 'uma.autonomous_training.skill_prepare.v1', 'status': 'ready', 'reason': 'skill_plan_ready', 'execution_allowed': False, 'purchase_executed': False, 'tap_count': 0, 'capture_manifest': manifest, 'capture_manifest_path': str(manifest_path), 'planner_report_path': str(destination / 'planner' / 'fresh_skill_dry_run.report.json'), 'proposal_path': str(proposal_path), 'inventory_path': str(inventory_path), 'batch_plan_path': str(batch_plan_path)}
    result['report_path'] = str(_write_json(destination / 'skill_prepare.report.json', result))
    return result
__all__ = ['MAX_SKILL_CAPTURE_PAGES', 'prepare_skills', 'publish_skill_plan']
