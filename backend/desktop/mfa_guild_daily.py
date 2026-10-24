"""Task boundary and original-source binding for guild shoe daily visits."""
import json
from pathlib import Path
from core.guild_daily import validate_options

def classify(result, endpoint):
    observed = result.get('last_observation') or {}
    safe = result.get('ok') is True and (not result.get('dry_run')) and (result.get('pending') is False) and (observed.get('page') == 'home') and (observed.get('endpoint') == endpoint) and (not result.get('account_conflict')) and (not result.get('account_paused'))
    skip = safe and result.get('skipped') is True and (result.get('skip_verified') is True) and (result.get('reason') == 'guild_daily_skipped_home_verified' and result.get('skip_reason') in {'not_joined', 'guild_locked'})
    complete = safe and (not result.get('skipped')) and (result.get('reason') == 'guild_daily_home_verified')
    return dict(game_task_complete=bool(complete), skip_verified=bool(skip), ok=bool(complete or skip))

def active_source(root, options, endpoint):
    root = Path(root).resolve()
    marker = root / 'active.json'
    if not marker.exists():
        candidates = []
        for archived in root.glob('*/guild_daily/retry-released.json'):
            value = json.loads(archived.read_text(encoding='utf8'))
            folder = archived.parent.resolve()
            result = json.loads((folder / 'adapter_result.json').read_text(encoding='utf8'))
            book = json.loads((folder / 'guild-state.json').read_text(encoding='utf8'))
            if result.get('pending') is not True or result.get('ok') is not False:
                continue
            if value.get('task') != 'guild_daily' or value.get('endpoint') != endpoint or value.get('options') != options or (Path(value.get('output', '')).resolve() != folder) or (folder.parent.parent != root) or (book.get('version') != 1) or (book.get('endpoint') != endpoint) or (book.get('options') != options) or (Path(result.get('state_path', '')).resolve() != folder / 'guild-state.json'):
                raise RuntimeError('guild_daily_archived_source_mismatch')
            candidates.append(archived)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise RuntimeError('guild_daily_multiple_archived_sources')
        from desktop.mfa_retry_lock import retry_cleanup_guard
        with retry_cleanup_guard(root) as acquired:
            if not acquired:
                raise RuntimeError('guild_daily_source_busy')
            if not marker.exists():
                with marker.open('xb') as stream:
                    stream.write(candidates[0].read_bytes())
                (candidates[0].parent / 'marker-restored-from-original.json').write_text(json.dumps(dict(source=str(candidates[0]), original_marker_preserved=True, no_game_input=True)), encoding='utf8')
    value = json.loads(marker.read_text(encoding='utf8'))
    if value.get('task') != 'guild_daily':
        raise RuntimeError('guild_daily_active_source_mismatch')
    folder = Path(value.get('output', '')).resolve()
    if value.get('endpoint') != endpoint or value.get('options') != options or folder.parent.parent != root or (folder.name != 'guild_daily'):
        raise RuntimeError('guild_daily_active_source_mismatch')
    journal = folder / 'guild-state.json'
    book = json.loads(journal.read_text(encoding='utf8'))
    if book.get('endpoint') != endpoint or book.get('options') != options or book.get('version') != 1:
        raise RuntimeError('guild_daily_active_source_mismatch')
    return folder

def resumable_marker(root, endpoint):
    """Preflight can retain this owner; only its exact later request may resume."""
    try:
        marker = json.loads((Path(root) / 'active.json').read_text(encoding='utf8'))
        if marker.get('task') != 'guild_daily':
            return False
        options = validate_options(marker.get('options'))
        return active_source(root, options, endpoint) is not None
    except (OSError, ValueError, RuntimeError, TypeError):
        return False

def execute(options, *, endpoint, adb_path, output, emit, stop, resume_from=None):
    from core.guild_daily.live import GuildDailyDevice
    from core.guild_daily.runner import GuildDailyRunner, Journal
    options = validate_options(options)
    folder = Path(resume_from) if resume_from else Path(output)
    journal = folder / 'guild-state.json'
    device = None
    Journal(journal, endpoint, options)
    try:
        device = GuildDailyDevice(endpoint, adb_path, folder)
        device.connect()
        return GuildDailyRunner(device, emit, stop, options=options, journal=journal).run()
    except Exception as error:
        from core.action_recovery import failure_details
        return dict(kind='guild_daily', ok=False, game_task_complete=False, pending=True, reason=str(error), state_path=str(journal.resolve()), device_action_count_complete=False, **failure_details(error))
    finally:
        if device is not None:
            device.close()
