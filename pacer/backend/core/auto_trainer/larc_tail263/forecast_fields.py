"""Bounded conditional-current forecasts; never computes missing predictions."""
from math import isfinite

FIELD = 'current_conditional_forecast'
ACTIONS = frozenset(('train:speed', 'train:stamina', 'train:power', 'train:guts', 'train:wisdom', 'rest.normal'))
SCALARS = ('score', 'mean_terminal_pt', 'mean_terminal_hp',
           'current_direct_expected_terminal_utility', 'future_contribution_including_future_aversion')


def numeric(value):
    return value if type(value) in (int, float) and isfinite(value) else None


def _same_basket(candidate, selected):
    if not isinstance(candidate, dict) or not isinstance(selected, dict):
        return False
    identity = selected.get('basket_id')
    if identity is None or candidate.get('basket_id') != identity:
        return False
    return all(key not in selected or candidate.get(key) == selected[key] for key in ('targets', 'cost'))


def compact_forecast(report):
    """At most6 actions, from one original selected basket; no source/paths."""
    research = report.get('research') or {}
    procurement = report.get('procurement') or {}
    selected = research.get('selected_basket') or {}
    proposals = research.get('ranking') or []
    rows, origin = [], 'not_retained'
    for item in procurement.get('basket_results') or []:
        if isinstance(item, dict) and _same_basket(item.get('basket'), selected):
            if isinstance(item.get('raw_ranking'), list):
                rows, origin = item['raw_ranking'], 'selected_basket_raw_ranking'
                break
    if origin == 'not_retained' and selected.get('basket_id') is not None:
        rows = [row for row in proposals if isinstance(row, dict)
                and row.get('basket_id') == selected['basket_id']]
        if rows:
            origin = 'selected_basket_original_proposals'
    proposal_ids = {row.get('action_id') for row in proposals if isinstance(row, dict)
                    and row.get('basket_id') == selected.get('basket_id')}
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = row.get('action_id')
        if action not in ACTIONS or action in seen:
            continue
        seen.add(action)
        if len(output) >= 6:
            break
        values = row.get('mean_terminal_display')
        mean = (list(values) if isinstance(values, (tuple, list)) and len(values) == 5
                and all(numeric(v) is not None for v in values) else None)
        output.append({'action_id': action, **{key: numeric(row.get(key)) for key in SCALARS},
            'mean_terminal_display': mean,
            'in_original_joint_proposals': action in proposal_ids,
            'is_original_advice': action == research.get('advice')})
    return {'schema_version': 'larc.current_conditional_forecast.v1',
        'source_kind': 'conditional_forecast', 'future_observation': False,
        'execution_or_payment_confirmed': False, 'missing_values_are_null_not_reconstructed': True,
        'original_planner_active': research.get('active') is True,
        'basket_scope': 'original_summary.selected_basket', 'rows_source': origin,
        'maximum_actions': 6, 'rows': output,
        'units': {'mean_terminal_display': 'display_stat_points', 'mean_terminal_pt': 'skill_Pt',
                  'mean_terminal_hp': 'HP_points_not_percent', 'score': 'planner_utility_not_actual_gain'}}


def append_forecast(original_summary, report):
    """Retain every old field/value/reference; add one independent small field."""
    try:
        extra = compact_forecast(report)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        extra = compact_forecast({})
        extra['diagnostic_unavailable'] = True
    return {**original_summary, FIELD: extra}
