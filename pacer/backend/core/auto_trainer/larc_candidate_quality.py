"""Verified numeric lower bounds and bounded, non-consuming refresh requests.

An optional unread gain is not zero.  Only already verified gains are scored;
the primary gain and current failure evidence remain mandatory for execution.
"""
from core.auto_trainer.rejection_policy import is_hard_rejected
from copy import deepcopy
from math import isfinite
import re

from core.auto_trainer.state import STAT_KEYS, TRAINING_TYPES
from core.training_gain_reliability import classify_reliable_exact_gain, validate_dynamic_gain_column


def enabled(goal=None, state=None):
    """Observation retries are independent of the scoring revision."""
    return True


def has_positive_training_gain(gains):
    """Check whether the supplied attribute or skill Pt gains include a positive number."""
    for key in STAT_KEYS:
        value = gains.get(key)
        if (type(value) in (int, float) and value > 0
                and (type(value) is int or isfinite(value))):
            return True
    return False


def _verified_zero_column(data, column, attribute):
    """Recognize observed +0/absent rows, never an absent mapping entry."""
    if ((data.get('source'), data.get('reader')) not in {
            ('own_template_v4', 'own_template_v4'), ('ppocrv5_dynamic', 'ppocrv5_mobile')}
            or column.get('attribute_id') != attribute
            or column.get('reason') not in {'gain_rows_not_found', 'gain_rows_verified'}
            or (column.get('canonical_value') is not None and
                (type(column.get('canonical_value')) is not int or column['canonical_value'] != 0))):
        return False
    rows = column.get('rows')
    if not isinstance(rows, list) or len(rows) != 2:
        return False
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get('row_index')) is not int or row['row_index'] != index:
            return False
        if row.get('occupancy') == 'absent':
            if (row.get('status') != 'absent' or row.get('value') is not None
                    or row.get('raw_text') not in ('', None)):
                return False
        elif not (row.get('occupancy') == 'present' and row.get('status') == 'accepted'
                  and type(row.get('value')) is int and row['value'] == 0
                  and re.fullmatch(r'\+0{1,3}', str(row.get('raw_text', '')).strip())):
            return False
    return True


def _has_verified_zero_column(data, columns, attribute):
    matches = [col for col in columns if isinstance(col, dict) and col.get('attribute_id') == attribute]
    return len(matches) == 1 and _verified_zero_column(data, matches[0], attribute)


def numeric_evidence(candidate):
    """Return reasons without inferring values from a prior turn or another tab."""
    raw = candidate.raw_data or {}
    gains = candidate.stats_gain or {}
    data = raw.get('stats_gain_ocr') or {}
    columns = data.get('columns', []) if isinstance(data, dict) else []
    used = {key: value for key, value in gains.items()
            if key in STAT_KEYS and type(value) is int and value >= 0}
    reasons = []
    critical = {'parse_failed', 'training_type', 'stats_gain_unreliable',
                'stats_gain_unverified_provenance', 'failure_rate',
                'failure_rate_unreliable', 'confidence_invalid'}
    if critical.intersection(raw.get('unknown_fields') or []):
        reasons.append('critical_numeric_diagnostic')

    primary_zero = _has_verified_zero_column(data, columns, candidate.training_type)
    if candidate.training_type not in used and not primary_zero:
        reasons.append('primary_gain_unknown')
    if not any(value > 0 for key, value in used.items() if key != 'skill_pt'):
        # A capped layout can still pay verified skill Pt. Require all five
        # zero columns before treating a Pt-only preview as complete.
        pt_only = has_positive_training_gain(used) and all(
            _has_verified_zero_column(data, columns, key) for key in TRAINING_TYPES)
        if not pt_only:
            reasons.append('positive_attribute_gain_missing')
    if any(key in STAT_KEYS and (type(value) is not int or value < 0)
           for key, value in gains.items()):
        reasons.append('used_gain_invalid')
    unverified = []
    for key, value in used.items():
        matches = [col for col in columns if isinstance(col, dict) and col.get('attribute_id') == key]
        if len(matches) != 1:
            unverified.append(key)
            continue
        column = matches[0]
        exact = classify_reliable_exact_gain(column, attribute_id=key)
        if not (value == 0 and _verified_zero_column(data, column, key)
                or exact.get('status') == 'reliable_exact' and exact.get('value') == value
                or validate_dynamic_gain_column(data, column, attribute=key, value=value)):
            unverified.append(key)
    if unverified:
        reasons.append('used_gain_unverified')
    if candidate.training_type in unverified:
        reasons.append('primary_gain_unverified')
    failure = raw.get('failure_rate_ocr') or {}
    rate = candidate.failure_rate
    reliable_failure = (type(rate) in (int, float) and isfinite(rate) and 0 <= rate <= 100
        and isinstance(failure, dict) and failure.get('status') == 'accepted'
        and failure.get('selected_tab') == candidate.training_type
        and type(failure.get('value')) in (int, float) and failure.get('value') == rate)
    if not reliable_failure:
        reasons.append('failure_rate_not_verified')
    return {'usable': not reasons, 'reasons': reasons, 'verified_gains': {
                key: value for key, value in used.items() if key not in unverified},
            'unverified_used_attributes': unverified,
            'primary_gain': (used.get(candidate.training_type, 0 if primary_zero else None)
                             if candidate.training_type not in unverified else None),
            'failure_rate_verified': reliable_failure,
            'basis': 'current_primary_gain_and_all_used_gains_verified; optional_unknown_not_zero'}


def usable_numeric_evidence(candidate, *, require_complete_attributes=False):
    evidence = numeric_evidence(candidate)
    if not evidence['usable'] or not require_complete_attributes:
        return evidence['usable']
    missing = set(TRAINING_TYPES).difference(evidence['verified_gains'])
    if not missing:
        return True
    data = (candidate.raw_data or {}).get('stats_gain_ocr') or {}
    columns = data.get('columns', []) if isinstance(data, dict) else []
    return all(_has_verified_zero_column(data, columns, key) for key in missing)


def row_diagnostics(candidate):
    raw = candidate.raw_data or {}
    data = raw.get('stats_gain_ocr') or {}
    data = data if isinstance(data, dict) else {}
    rows = []
    for col in data.get('columns', []):
        if not isinstance(col, dict):
            continue
        for row in col.get('rows', []):
            if not isinstance(row, dict) or row.get('status') in ('accepted', 'absent'):
                continue
            reason = row.get('reason')
            stage = ('classification' if reason == 'plus_baseline_paired' else
                     'segmentation' if reason in {'unoutlined_trailing_digit', 'unpaired_digits',
                        'multiple_pluses', 'broken_digit_spacing', 'clipped_digit',
                        'orphan_plus_or_excess_digits'} else 'unknown')
            rows.append({'attribute': col.get('attribute_id'), 'row_index': row.get('row_index'),
                'status': row.get('status'), 'reason': reason, 'stage': stage,
                'segmentation_succeeded': reason == 'plus_baseline_paired',
                'style': row.get('style'), 'scores': deepcopy(row.get('scores'))})
    return {'training_type': candidate.training_type, 'confidence': candidate.confidence,
            **numeric_evidence(candidate), 'gain_rows': rows,
            'failure_rate_ocr': deepcopy(raw.get('failure_rate_ocr'))}


def refresh_requests(state, goal, scored_candidates, *, purchased_training=None, limit=2):
    """Choose at most two uncertain contenders for observation, never for action.

    A missing primary is ranked using a labelled optimistic numeric ceiling.
    This value cannot authorize training and is never inserted into a candidate.
    """
    if not enabled(goal, state):
        return []
    from core.auto_trainer.larc_training_value import gain_value
    by_id = {row.get('action_id'): row for row in scored_candidates}
    eligible = [row.get('score', 0.) for row in scored_candidates if not is_hard_rejected(row)]
    best = max(eligible, default=0.)
    result = []
    for candidate in state.training_options:
        row = by_id.get(candidate.action_id, {})
        evidence = numeric_evidence(candidate)
        failure_limit = getattr(goal, 'training_failure_rate_limit', None)
        if (evidence['failure_rate_verified']
                and candidate.failure_rate > (25. if failure_limit is None else failure_limit)):
            continue  # A reliable excessive failure rate needs no reread.
        missing = bool(evidence['reasons'])
        # Revisit partial gain columns even when the reliable lower bound can
        # now be used, so an optional missing gain is not silently forgotten.
        partial = bool(row_diagnostics(candidate)['gain_rows'])
        reasons = []
        if candidate.training_type == purchased_training and (missing or partial):
            reasons.append('purchased_training_quality_incomplete')
        known_value = float(row.get('score', 0.))
        if (is_hard_rejected(row) or partial) and known_value > best:
            reasons.append('known_lower_bound_exceeds_eligible')
        inspection_ceiling = known_value
        if ({'primary_gain_unknown', 'primary_gain_unverified'}.intersection(evidence['reasons'])
                and evidence['verified_gains']):
            # Two rows of at most three digits bound the reader's format.
            # This is only an inspection ceiling, never a predicted game gain.
            assumed = {**evidence['verified_gains'], candidate.training_type: 1998}
            inspection_ceiling = gain_value(state, assumed, goal, project=False)['score']
            if inspection_ceiling > best:
                reasons.append('unknown_primary_can_change_selection')
        if 'failure_rate_not_verified' in evidence['reasons'] and known_value > best:
            reasons.append('competitive_candidate_failure_unknown')
        if candidate.failure_rate is None:
            reasons.append('failure_rate_unreadable')
        if 'primary_gain_unknown' in evidence['reasons']:
            reasons.append('primary_gain_unknown')
        if reasons:
            result.append({'training_type': candidate.training_type, 'reasons': reasons,
                'known_score': known_value, 'best_eligible_score': best,
                'inspection_ceiling': inspection_ceiling,
                'ceiling_is_not_executable_evidence': True,
                'before': row_diagnostics(candidate)})
    result.sort(key=lambda row: (row['training_type'] != purchased_training,
                                -row['inspection_ceiling'], row['training_type']))
    return result[:max(0, min(2, int(limit)))]
