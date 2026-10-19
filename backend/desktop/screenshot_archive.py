"""PNG archive primitives; explicit lifecycle adapters own all root semantics."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import time
from desktop.product_updates import transaction_io_path as io_path
DAY = 86400
REF_KEYS = ('output_dir', 'daily_output_dir', 'legend_output_dir', 'jjc_output_dir', 'resumed_from', 'resume_from', 'seed_resume_setup_from', 'setup_original_seed_source', 'harvest_resume_from', 'prior_submission')
PENDING_KEYS = ('pending', 'pending_race', 'pending_spin', 'pending_setting', 'pending_exchange', 'start_submitted', 'completion_submitted', 'finish_submitted', 'purchase_submitted', 'inheritance_confirmation_submitted')

@dataclass(frozen=True)
class Policy:
    failure_mode: str = 'keys_7d'
    failure_keys: int = 4
    min_interval: int = 6 * 3600
    max_files: int = 20000
    max_seconds: float = 12

    def __post_init__(self):
        if self.failure_mode not in {'keys_7d', 'all_24h'} or not 0 <= self.failure_keys <= 12 or self.max_files < 1 or (self.max_seconds <= 0) or (self.min_interval < 0):
            raise ValueError('daily_png_policy_invalid')

def _read(path):
    if _reparse(path):
        raise ValueError('daily_png_metadata_link')
    value = json.loads(io_path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('daily_png_metadata_invalid')
    return value

def _token(path):
    try:
        s = io_path(path).lstat()
        return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]
    except FileNotFoundError:
        return None

def _reparse(path):
    s = io_path(path).lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 1024)

def _inside(path, root):
    return path == root or path.is_relative_to(root)

def _pending(value):
    if value.get('ok') is True and value.get('game_task_complete') is True:
        return False
    outcome = value.get('action_outcome')
    if outcome is not None and (not isinstance(outcome, dict)):
        return True
    if any((value.get(k) is not None and type(value.get(k)) is not bool for k in PENDING_KEYS)):
        return True
    return any((value.get(k) is True for k in PENDING_KEYS)) or (value.get('submitted') is True and value.get('ok') is not True) or (outcome or {}).get('execution_status') in {'uncertain', 'unknown', 'partial'}

def _unlink(path):
    io_path(path).unlink()

def apply(report, *, stop=None):
    """Revalidate each exact PNG; errors never become a task failure."""
    base = Path(report['root'])
    removed = removed_bytes = 0
    roots = {'mfa_batches': 'mfa-batches', 'public_tasks': 'public-tasks', 'seed_cycles': 'seed-cycles'}
    if base.name != roots.get(report.get('adapter')) or base.parent.name != 'runs' or _reparse(base):
        raise ValueError('daily_png_root_invalid')
    try:
        changed = any((_token(Path(row['path'])) != row['token'] for row in report.get('metadata_tokens', [])))
    except OSError:
        changed = True
    if changed:
        report['errors'].append(dict(path=str(base), reason='discovery_metadata_changed'))
        report.update(dry_run=False, removed=0, removed_bytes=0)
        return report
    for row in report['candidates']:
        if time.monotonic() > report['deadline_monotonic'] or (stop is not None and stop.is_set()):
            report['budget_exhausted'] = True
            break
        path = Path(row['path'])
        unit = Path(row['unit'])
        try:
            guards = row.get('guards') or [dict(path=str(base / 'control.json'), token=row['control_token']), dict(path=row['terminal'], token=row['terminal_token']), dict(path=row['account_state'], token=row['account_token'])]
            if any((_token(Path(guard['path'])) != guard['token'] for guard in guards)):
                report['errors'].append(dict(path=str(unit), reason='ownership_metadata_changed'))
                continue
            if path.suffix.lower() != '.png' or not path.is_relative_to(base) or path.resolve() != path or (_token(path) != row['token']):
                continue
            parents = [path, *path.parents]
            owned = parents[:parents.index(base) + 1]
            if any((_reparse(p) for p in owned)):
                continue
            marker_parents = [p for p in owned[1:] if not (p == base and report['adapter'] == 'public_tasks')]
            if any((io_path(p / 'active.json').exists() for p in marker_parents)):
                continue
            _unlink(path)
            removed += 1
            removed_bytes += row['size']
        except (OSError, ValueError):
            report['errors'].append(dict(path=str(path), reason='remove_failed'))
    report.update(dry_run=False, removed=removed, removed_bytes=removed_bytes)
    return report
