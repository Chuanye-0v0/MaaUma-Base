"""LOH boundary shared by private batches and the public current-account worker."""
import json
from pathlib import Path

def validate_options(options):
    if (not isinstance(options, dict) or set(options) - {'max_entries', 'fill_empty_team'}
            or type(options.get('fill_empty_team', False)) is not bool):
        raise ValueError('loh_options_invalid')
    cap = options.get('max_entries')
    if cap is not None and (type(cap) is not int or cap < 0):
        raise ValueError('loh_max_entries_invalid')
    fill = options.get('fill_empty_team', False)
    return {'max_entries': cap, 'fill_empty_team': fill}

def execute(options, *, endpoint, adb_path, output, emit, stop, resume_from=None):
    from core.loh.live import run_loh, LohDevice
    from core.loh.resume import StateBook
    from core.screenshot_retention import ScreenshotSession
    if resume_from is not None and Path(resume_from).is_dir():
        resume_from = Path(resume_from) / 'loh-state.json'
    directory = Path(resume_from).parent if resume_from else Path(output)
    journal = Path(resume_from) if resume_from else directory / 'loh-state.json'
    device = None
    try:
        with ScreenshotSession() as screenshots:
            StateBook(directory, endpoint, resume_from=resume_from)
            menu = MenuNavigation()
            device = LohDevice(endpoint, adb_path, directory, observer=menu)
            device.connect()
            step = device.step

            def menu_step(action, stop_event, expected):
                receipt = step(action, stop_event, expected)
                if receipt.get('performed') is True:
                    menu.executed(action)
                    receipt['observation'] = menu.route(receipt['observation'])
                return receipt
            device.step = menu_step
            result = run_loh(endpoint=endpoint, adb_path=adb_path, output=output, emit=emit, stop=stop, options=options, resume_from=resume_from, device=device)
            if result.get('status') in {'failed', 'cancelled'} and result.get('pending') is False and ((result.get('last_observation') or {}).get('page') not in {'home', 'races', 'events'}):
                book = json.loads(journal.read_text(encoding='utf8'))
                if book.get('loh_context_verified') is True:
                    result.update(pending=True, pending_kind='loh_navigation', ticket_entry_pending=False)
            result['screenshot_retention'] = screenshots.finish(result, complete=bool(result.get('game_task_complete')))
            return result
    except Exception as error:
        return dict(kind='loh', ok=False, game_task_complete=False, status='cancelled' if stop.is_set() else 'failed', reason=str(error), error_type=type(error).__name__, pending=True, state_path=str(journal), device_action_count_complete=False)
    finally:
        if device is not None:
            device.close()

class MenuNavigation:
    """Four acknowledged menu scrolls, down then up, inside U1's own loop."""

    def __init__(self):
        self.scrolls = 0

    def __call__(self, frame, ocr_items=None):
        return self.route(observe(frame, ocr_items))

    def executed(self, action):
        if action == 'scroll_events':
            self.scrolls += 1
        elif action == 'open_loh':
            self.scrolls = 0

    def route(self, observed):
        if observed.get('page') != 'events' or 'open_loh' in observed.get('actions', {}):
            return observed
        actions = {}
        direction = 'event_scroll_down' if self.scrolls < 2 else 'event_scroll_up'
        candidate = observed.get('navigation_actions', {}).get(direction)
        if self.scrolls < 4 and candidate:
            actions['scroll_events'] = dict(swipe=[*candidate['start'], *candidate['end']], duration_ms=candidate['duration_ms'], effect='navigation')
        return dict(observed, actions=actions)

def observe(frame, ocr_items=None):
    """Use the shared multi-card menu before delegating all LOH pages to U2."""
    from core.event_menu import menu_entries
    from core.loh.observer import observe as observe_loh
    menu = menu_entries(frame, ocr_items or [])
    if menu is None:
        import cv2
        from core.limited_legend.observer import observe as observe_navigation
        raw = cv2.imencode('.png', frame)[1].tobytes()
        navigation = observe_navigation(raw, frame, ocr_items or [])
        page = navigation.get('page')
        action = {'home': 'open_races', 'races': 'open_events'}.get(page)
        if action and action in navigation.get('actions', {}):
            return dict(page=page, actions={action: navigation['actions'][action]}, reason='shared_verified_' + page + '_navigation')
        return observe_loh(frame, ocr_items=ocr_items)
    if menu.get('loh_available') is False:
        return dict(menu, page='loh_closed', actions={}, reason='named_loh_card_closed')
    actions = {}
    if 'open_loh' in menu['actions']:
        actions['open_loh'] = menu['actions']['open_loh']
    elif menu.get('navigation_actions', {}).get('scroll_events'):
        actions['scroll_events'] = menu['navigation_actions']['scroll_events']
    return dict(menu, actions=actions, reason='named_loh_card' if 'open_loh' in actions else 'loh_menu_search')

def classify(result, endpoint):
    """Configuration-needed advances the queue without claiming game success."""
    status = result.get('status')
    observed = result.get('last_observation') or {}
    settled = result.get('pending') is False and observed.get('endpoint') == endpoint and (not result.get('account_conflict')) and (not result.get('account_paused')) and ((result.get('action_outcome') or {}).get('execution_status') not in {'unknown', 'uncertain', 'partial'})
    complete = settled and status == 'done' and (result.get('ok') is True)
    skipped = settled and status == 'skipped' and (result.get('ok') is True)
    configuration = settled and status == 'needs_configuration'
    return dict(ok=bool(complete or skipped), game_task_complete=bool(complete), skip_verified=bool(skipped), queue_continue=bool(configuration), needs_configuration=bool(configuration))

def active_source(root, options, endpoint):
    """Reuse this current-account operation's journal, never create a second entry."""
    root = Path(root).resolve()
    marker = root / 'active.json'
    if not marker.exists():
        return None
    value = json.loads(marker.read_text(encoding='utf8'))
    source = Path(value.get('output', '')).resolve()
    # Older markers did not serialize the optional team-fill switch. Their
    # missing value means False; preserve the original journal and marker.
    try:
        same_options = validate_options(value.get('options')) == validate_options(options)
    except ValueError:
        same_options = False
    if value.get('task') != 'loh' or value.get('endpoint') != endpoint or not same_options or (source.parent.parent != root) or (source.name != 'loh') or (not source.is_dir()):
        raise RuntimeError('loh_active_source_mismatch')
    return source
