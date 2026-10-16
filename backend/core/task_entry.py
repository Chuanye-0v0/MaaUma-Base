"""Prefer task-owned continuation; navigate other ordinary pages through inventory."""
import json
import time
from core.account_inventory.public_navigation import to_home
from core.account_inventory.live import InventoryDevice
ORDINARY_ENTRY_PAGES = {'profile', 'menu', 'uma_list', 'uma_detail', 'enhance_menu', 'uma_menu', 'profile_help', 'profile_support_help'}

def prepare_task_entry(device, stop, emit, *, task=None):
    ordinary_pages = {'harvest': {'trainee', 'exam_mode', 'training_menu', 'team_arena'}, 'plant': {'training_menu', 'team_arena'}}.get(task, set())
    waiting_pages = {'unknown', 'loading'} | ordinary_pages | ORDINARY_ENTRY_PAGES
    deadline = time.monotonic() + 15
    navigator = None
    for _ in range(51):
        if stop.is_set():
            raise RuntimeError('operator_stop')
        observed = device.capture()
        if observed.get('page') not in waiting_pages:
            return observed
        if navigator is None:
            navigator = InventoryDevice(device.endpoint, device.adb, device.output_dir / 'task-entry')
        ordinary = navigator.capture()
        if ordinary.get('page') not in {'unknown', 'loading'}:
            receipts = []
            directory = device.output_dir / 'task-entry'
            directory.mkdir(parents=True, exist_ok=True)

            def save(receipt):
                receipts.append(receipt)
                (directory / 'receipts.json').write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding='utf8')
            emit(dict(type='progress', message='当前页面不属于本任务，先按已识别路径返回主页'))
            try:
                to_home(navigator, stop, save, observed=ordinary)
            except RuntimeError as error:
                if hasattr(error, 'navigation_trace'):
                    (directory / 'navigation.json').write_text(json.dumps(dict(trace=error.navigation_trace, reason=str(error)), ensure_ascii=False, indent=2), encoding='utf8')
                if str(error) == 'return_home_page_unverified':
                    last = getattr(error, 'last_observation', ordinary)
                    raise RuntimeError('task_entry_page_unverified:' + str(last.get('page'))) from error
                raise
            from core.location_navigation import wait_for_transition, NavigationFailure
            fresh = device.capture()
            try:
                return wait_for_transition(device, stop, fresh, lambda state: state.get('page') not in waiting_pages, waiting_pages=waiting_pages, endpoint=device.endpoint)
            except NavigationFailure as error:
                failure = RuntimeError('operator_stop' if error.kind == 'operator_stop' else 'task_entry_page_unverified:' + str(error.observation.get('page')))
                failure.last_observation = error.observation
                failure.navigation_trace = error.trace
                raise failure from error
        if time.monotonic() >= deadline:
            break
        if stop.wait(0.3):
            raise RuntimeError('operator_stop')
    raise RuntimeError('task_entry_page_unverified:unknown')
