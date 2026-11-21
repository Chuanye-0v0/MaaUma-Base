"""Typed current training-energy observations; no valuation or decisions.

All point quantities are [low, high]. Percent bar readings are converted with
the existing capacity bounds; no planning midpoint or hidden recovery is read
back into an observation. A clipped applied zero is not an uncapped request.
"""
from collections.abc import Mapping
from copy import deepcopy
import math

SCHEMA = 'larc.training_energy_observation.v1'
UNIT = 'points'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _range(value):
    if _number(value):
        return [float(value), float(value)]
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_number(x) for x in value) and value[0] <= value[1]):
        return [float(value[0]), float(value[1])]
    return None


def _unknown(source=None, turn=None, source_kind='unknown', reason='energy_observation_unavailable'):
    return {'schema_version': SCHEMA, 'version': 1, 'status': 'unknown', 'unit': UNIT,
        'source_turn_index': turn, 'source_kind': source_kind,
        'source': deepcopy(dict(source)) if isinstance(source, Mapping) else {},
        'capacity_points': None, 'before_points': None, 'applied_delta_points': None,
        'requested_delta_points': None, 'clipped': None, 'applied_basis': 'unknown',
        'requested_basis': 'unknown', 'unknown_fields': [reason]}


def observation_from_points(*, source, source_turn_index, source_kind,
        expected_run_session_id, capacity_points, before_points, applied_delta_points=None,
        requested_delta_points=None, clipped=None, unit=UNIT,
        applied_basis='current_applied_points', requested_basis='explicit_uncapped_requested_points'):
    """Validate point observations shared by live and public-model frontends.

    This reads no file and does not upgrade provenance. A caller with only an
    already-clipped requested field must pass requested_delta_points=None.
    """
    def unknown(reason):
        return _unknown(source, source_turn_index, source_kind, reason)
    if unit != UNIT:
        return unknown('energy_unit_not_points')
    if (not isinstance(source, Mapping) or not isinstance(expected_run_session_id, str)
            or not expected_run_session_id or source.get('run_session_id') != expected_run_session_id
            or type(source_turn_index) is not int or source_turn_index < 0
            or type(source.get('turn_index')) is not int or source['turn_index'] != source_turn_index
            or not isinstance(source_kind, str) or not source_kind):
        return unknown('energy_source_scope_unknown')
    capacity, before = _range(capacity_points), _range(before_points)
    applied = _range(applied_delta_points) if applied_delta_points is not None else None
    requested = _range(requested_delta_points) if requested_delta_points is not None else None
    if capacity is None or not 0 < capacity[0] <= capacity[1]:
        return unknown('capacity_points_unavailable')
    if before is None or before[0] < 0 or before[1] > capacity[1] + 1e-6:
        return unknown('before_points_out_of_capacity')
    if applied_delta_points is not None and applied is None:
        return unknown('applied_delta_points_invalid')
    if requested_delta_points is not None and requested is None:
        return unknown('requested_delta_points_invalid')
    if applied is None and requested is None:
        return unknown('training_energy_delta_unavailable')
    if clipped is not None and type(clipped) is not bool:
        return unknown('clipping_flag_invalid')
    if applied is not None:
        if before[0] + applied[0] < -1e-6 or before[1] + applied[1] > capacity[1] + 1e-6:
            return unknown('applied_delta_out_of_capacity')
    exact = all(pair is not None and pair[0] == pair[1] for pair in (capacity, before, applied, requested))
    if exact:
        physical = min(capacity[0], max(0., before[0] + requested[0])) - before[0]
        if abs(physical - applied[0]) > 1e-5:
            return unknown('requested_and_applied_points_inconsistent')
        physically_clipped = abs(requested[0] - applied[0]) > 1e-5
        if clipped is not None and clipped != physically_clipped:
            return unknown('clipping_flag_inconsistent')
        clipped = physically_clipped
    # An interior applied bar change, or explicit uncut provenance, can identify
    # its request. At either boundary, retain an unknown request/lower bound.
    if requested is None and applied is not None:
        interior = before[0] + applied[0] > 1e-6 and before[1] + applied[1] < capacity[0] - 1e-6
        if clipped is False or clipped is None and interior:
            requested = list(applied)
            requested_basis = 'unclipped_applied_points_equal_requested'
            clipped = False
        else:
            requested_basis = 'unknown_uncapped_request_at_energy_boundary'
    result = {'schema_version': SCHEMA, 'version': 1, 'status': 'accepted', 'unit': UNIT,
        'source_turn_index': source_turn_index, 'source_kind': source_kind,
        'source': deepcopy(dict(source)), 'capacity_points': capacity, 'before_points': before,
        'applied_delta_points': applied, 'requested_delta_points': requested, 'clipped': clipped,
        'applied_basis': applied_basis, 'requested_basis': requested_basis,
        'applied_is_lower_bound_for_positive_request': bool(applied is not None and applied[0] > 0 and requested is None),
        'unknown_fields': ([] if applied is not None else ['applied_delta_points'])
            + ([] if requested is not None else ['requested_delta_points'])}
    return result


def training_energy_observation(candidate, state=None):
    """Read only an existing run-bound production bar preview, never OCR anew."""
    capture = (getattr(candidate, 'raw_data', {}) or {}).get('capture') or {}
    turn = getattr(state, 'turn', None)
    if not isinstance(capture, Mapping):
        return _unknown(turn=turn, reason='capture_unavailable')
    if capture.get('source_kind') == 'simulation':
        return _unknown(capture, turn, 'simulation', 'bound_public_model_frontend_required')
    kind = 'observed_training_vitality_bar_preview'
    if state is None:
        return _unknown(capture, turn, kind, 'current_state_unavailable')
    captures = [(getattr(c, 'raw_data', {}) or {}).get('capture') or {}
        for c in getattr(state, 'training_options', ())]
    ids = {c.get('run_session_id') for c in captures if isinstance(c, Mapping)}
    ids.discard(None)
    run = next(iter(ids)) if len(ids) == 1 else None
    if (not run or capture.get('run_session_id') != run or type(turn) is not int
            or capture.get('turn_index') != turn
            or any(not isinstance(capture.get(k), str) or not capture[k]
                for k in ('run_session_id', 'absolute_path', 'sha256'))):
        return _unknown(capture, turn, kind, 'capture_scope_unknown')
    preview = ((getattr(candidate, 'scenario_mechanics', {}) or {}).get('larc') or {}).get('vitality_preview') or {}
    from core.auto_trainer.larc_vitality_preview import accepted_preview
    source = preview.get('source') or {}
    if (not accepted_preview(preview) or preview.get('training_type') != candidate.training_type
            or not isinstance(source, Mapping)
            or any(source.get(k) != capture.get(k) for k in ('run_session_id', 'turn_index', 'sha256'))):
        return _unknown(capture, turn, kind, 'current_training_preview_unbound_or_unknown')
    from core.auto_trainer.larc_capacity import capacity_bounds
    capacity = list(capacity_bounds(state))
    before = [preview['current_percent'] * c / 100 for c in capacity]
    delta = sorted(preview['delta_percent'] * c / 100 for c in capacity)
    uncut = 0 < preview['after_percent'] < 100
    packet = observation_from_points(source=capture, source_turn_index=turn, source_kind=kind,
        expected_run_session_id=run, capacity_points=capacity, before_points=before,
        applied_delta_points=delta, clipped=False if uncut else None,
        applied_basis='bound_bar_percent_times_capacity_bounds',
        requested_basis='unknown_no_uncapped_request_in_bar_preview')
    packet['capacity_basis'] = 'existing_capacity_bounds_not_planning_midpoint'
    packet['before_percent'] = preview['current_percent']
    return packet
