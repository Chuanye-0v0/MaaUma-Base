from __future__ import annotations
import json
from pathlib import Path

def to_home(device, stop, receipt_sink=None, observed=None):
    """Return from an ordinary page without logging out or discarding settlement."""
    return _navigate(device, stop, receipt_sink, destination='home', observed=observed)

def to_profile(device, stop, receipt_sink=None, observed=None):
    """Locate and read the current profile, regardless of the ordinary entry page."""
    return _navigate(device, stop, receipt_sink, destination='profile', observed=observed)

def _navigate(device, stop, receipt_sink, *, destination, observed=None, allow_records=False):
    from core.location_navigation import MODAL_ROUTES, SETTLEMENT_ROUTES, NavigationFailure, inventory_routes, navigate_location
    prefix = {'home': 'return_home', 'recent': 'return_title', 'profile': 'read_profile'}[destination]
    terminal = lambda state: state.get('page') == destination and (destination != 'profile' or bool(state.get('trainer_id'))) or (destination == 'recent' and (state.get('page') == 'startup_terms' or (allow_records and state.get('page') == 'records')))
    directory = getattr(device, 'output_dir', None)

    def trace_sink(trace):
        if isinstance(directory, Path):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ('navigation-' + destination + '.json')).write_text(json.dumps(dict(destination=destination, trace=trace), ensure_ascii=False, indent=2), encoding='utf8')
    try:
        return navigate_location(device, stop, inventory_routes(destination), terminal, observed=observed, receipt_sink=receipt_sink, trace_sink=trace_sink, readiness_pages={'autonomous_running', 'training_menu', 'profile', 'menu'} | set(SETTLEMENT_ROUTES), repeat_pages=set(MODAL_ROUTES) - set(SETTLEMENT_ROUTES) | {'date_change'})
    except NavigationFailure as error:
        reason = 'operator_stop' if error.kind == 'operator_stop' else prefix + '_' + error.kind
        failure = RuntimeError(reason)
        failure.navigation_trace = error.trace
        failure.last_observation = error.observation
        raise failure from error
