"""Portable, single-device context. Importing this module never opens a device."""
import json
import os
from pathlib import Path
import re
PRODUCT_ID = 'umaa-public'
TASKS = ('harvest', 'jjc', 'story_event', 'plant', 'daily_legend', 'activities', 'daily_rewards', 'limited_legend', 'champions_registration', 'champions_race', 'loh', 'cleanup', 'seed_cycle', 'guild_daily')

def backend_root():
    return Path(__file__).resolve().parents[1]

def normalize_endpoint(value):
    """Map a selected local emulator console serial to its paired ADB port.

    The GUI keeps the selected serial for Maa's controller. The worker and
    shared device lock use the same canonical TCP alias, never a guessed device
    index or the first device discovered. No ADB process is invoked here.
    """
    if not isinstance(value, str):
        raise ValueError('public_connection_invalid')
    local = re.fullmatch('127\\.0\\.0\\.1:([0-9]{1,5})', value)
    if local and 1 <= int(local[1]) <= 65535:
        return '127.0.0.1:' + str(int(local[1]))
    emulator = re.fullmatch('emulator-([0-9]{1,5})', value)
    if emulator:
        console = int(emulator[1])
        if 1 <= console < 65535 and console % 2 == 0:
            return '127.0.0.1:' + str(console + 1)
    raise ValueError('public_connection_invalid')

def endpoint():
    value = os.environ.get('UMA_PUBLIC_ENDPOINT', '')
    if not re.fullmatch('127\\.0\\.0\\.1:([0-9]{1,5})', value):
        raise ValueError('public_device_not_selected')
    try:
        return normalize_endpoint(value)
    except ValueError as error:
        raise ValueError('public_device_not_selected') from error

def bind(connection):
    if not isinstance(connection, dict) or set(connection) != {'endpoint', 'adb_path'}:
        raise ValueError('public_connection_invalid')
    value = normalize_endpoint(connection['endpoint'])
    port = int(value.rpartition(':')[2])
    adb = Path(connection['adb_path'])
    if not 1 <= port <= 65535 or not adb.is_file():
        raise ValueError('public_connection_invalid')
    os.environ['UMA_PUBLIC_ENDPOINT'] = value
    os.environ['UMA_ADB_PORT'] = str(port)
    os.environ['UMA_ADB_PATH'] = str(adb.resolve())

def check_task(task):
    if task not in TASKS:
        raise ValueError('public_task_not_available')

def lock_root():
    base = os.environ.get('UMA_PUBLIC_LOCK_ROOT') or os.environ.get('LOCALAPPDATA')
    if not base:
        raise RuntimeError('public_device_lock_unavailable')
    return Path(base) / 'UmaSuite' / 'device-locks'

def manual_startup_required(*args, **kwargs):
    raise RuntimeError('public_manual_game_start_required')

def self_test():
    """Inspect the portable payload and dry-run every public task; no ADB calls."""
    import threading
    os.environ['UMA_PUBLIC_ENDPOINT'] = '127.0.0.1:65535'
    from desktop.mfa_backend import CurrentAccountBackend, validate_options
    root = backend_root()
    from core.harvest_presets import catalog, empty_store, validate_store
    library = catalog(root / 'data/harvest/catalog.json')
    assert validate_store(empty_store(), library)['skills'] == []
    backend = CurrentAccountBackend(root / 'runs/self-test', live_enabled=False)
    for task in TASKS:
        if task == 'seed_cycle':
            from desktop.umaa_seed_cycle import SeedCycle
            options = {'cycle': {'targets': [{'key': library['factors'][0]['key'], 'min_stars': 1}]}}
            result = SeedCycle(root / 'runs/self-test-seeds', endpoint=endpoint(), adb_path='', skill_database=None).run(options, lambda event: None, threading.Event(), execute=False)
            assert result['ok'] and result['dry_run'] and (result['device_actions'] == 0)
            continue
        options = {'division': 'advanced'} if task == 'champions_registration' else {}
        if task == 'daily_legend':
            options = {'opponent_number': 1}
        result = backend.run(task, options, lambda event: None, threading.Event(), execute=False)
        assert result['ok'] and result['dry_run'] and (result['device_actions'] == 0)
        assert result['game_task_complete'] is False
    for rejected in ('batch', 'cut', 'career', 'login', 'switch_account'):
        try:
            validate_options(rejected, {})
        except ValueError:
            pass
        else:
            raise AssertionError('Private task was accepted: ' + rejected)
    import cv2
    import numpy
    import rapidocr_onnxruntime
    import pytesseract
    import maa
    from maa.library import Library
    native = root.parent / 'runtimes/win-x64/native'
    Library.open(native, agent_server=True)
    Library.agent_server()
    import importlib
    from unittest.mock import patch
    manifest = json.loads((root.parent / 'public-package-manifest.json').read_text(encoding='utf8'))
    with patch('subprocess.run', side_effect=AssertionError('Self-test attempted subprocess/device operation')), patch('subprocess.Popen', side_effect=AssertionError('Self-test attempted subprocess/device operation')):
        for name in manifest['backend_modules']:
            importlib.import_module(name)
    database = json.loads((root / 'data/skill_database.json').read_text(encoding='utf8'))
    assert isinstance(database, (dict, list)) and database
    return dict(ok=True, product_id=PRODUCT_ID, tasks=list(TASKS), dry_run_tasks=len(TASKS), device_actions=0, skill_count=len(library['skills']), factor_count=len(library['factors']), loaded_backend_modules=len(manifest['backend_modules']), native_sdk_loaded=True, live_run_verified=False)
