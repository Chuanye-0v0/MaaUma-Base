"""Fresh callback-scoped SS deadline permit; never game input or a counter."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_CURRENT = ContextVar('larc_ss_deadline_execution', default=None)


def requires_ss_observation(status):
    return bool(status.get('enabled') and status.get('reason') not in {
        'ss_deadline_domain_unavailable', 'current_turn_unavailable'} and
        (status.get('collect_pending') or status.get('force_current_ss')
            or status.get('due') and status.get('current_full') is None))


def current_deadline_evidence(state, goal, preview=None):
    from core.auto_trainer.larc_ss_deadline import deadline_status
    from core.auto_trainer.larc_ss_visible import visible_ss_participants, ss_victory_evidence
    status = deadline_status(state, goal, preview)
    result = {'status': status, 'executable': False, 'risk_known': False,
        'guaranteed': False, 'risk_bands': []}
    if not status['force_current_ss'] or not isinstance(preview, dict):
        return result
    if (preview.get('status') != 'accepted' or preview.get('stage') != 'preview'
            or preview.get('kind') not in {'SS', 'SSS'} or preview.get('ready_count') != 5):
        return result
    rows = visible_ss_participants(preview)
    if len(rows) != 5 or [p.get('slot') for p in rows] != list(range(5)):
        return result
    victory = ss_victory_evidence(preview)
    observation = preview.get('participants_observation') or {}
    rates = observation.get('win_rates') or []
    known = (observation.get('win_rates_status') == 'accepted' and len(rates) == 5
        and [p.get('slot') for p in rates] == list(range(5))
        and all(p.get('band') in {'blue', 'green'} for p in rates))
    # SSS retains its scenario rule; ordinary unknown badges are not a risk
    # preference and cannot become executable merely because time is due.
    result.update(executable=bool(victory['guaranteed'] or known),
        risk_known=bool(victory['guaranteed'] or known), guaranteed=victory['guaranteed'],
        risk_bands=[p.get('band') for p in rates])
    return result


@contextmanager
def execution_context(state, goal):
    token = _CURRENT.set({'state': state, 'goal': goal, 'preview': None})
    try:
        yield
    finally:
        _CURRENT.reset(token)


def register_bound_preview(preview):
    current = _CURRENT.get()
    if current is not None:
        current['preview'] = deepcopy(preview)


def _current_facts(preview):
    observation = preview.get('participants_observation') or {}
    return {**{k: preview.get(k) for k in ('status', 'stage', 'kind', 'ready_count', 'stats_gain')},
        'win_rates': observation.get('win_rates'),
        'rows': [{k: p.get(k) for k in ('slot', 'win_rate_band', 'win_guaranteed')}
            for p in observation.get('participants', [])]}


def current_execution_permit(actual_preview):
    current = _CURRENT.get()
    bound = current.get('preview') if current else None
    if not isinstance(bound, dict) or _current_facts(bound) != _current_facts(actual_preview):
        return {'executable': False}
    expected=bound.get('source')or{}
    # The raw page reader has no source before the existing roster binder.
    # If an actual packet *does* claim scope, reject any explicit mismatch;
    # equal numbers never override a contradictory run/turn identity.
    for source in (actual_preview.get('source'),
            (actual_preview.get('participants_observation')or{}).get('source')):
        if isinstance(source,dict)and any(k in source and source[k]!=expected.get(k)
                for k in ('run_session_id','turn_index')):
            return {'executable':False}
    # Recompute from the callback's current run-bound packet. A caller's
    # selected/forced/ss_deadline marker is deliberately not consulted.
    return current_deadline_evidence(current['state'], current['goal'], bound)
