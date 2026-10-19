"""Explicit public-tasks and seed-cycles lifecycle adapters; no Pacer run sweep."""
import json
import os
from pathlib import Path
import time
from desktop.screenshot_archive import DAY, REF_KEYS, Policy, io_path, _read, _token, _reparse, _inside, _pending, apply

class Archive:

    def __init__(self, root, kind, policy, now, deadline, stop):
        self.root = Path(root).absolute()
        expected = {'public_tasks': 'public-tasks', 'seed_cycles': 'seed-cycles'}
        if self.root.name != expected[kind] or self.root.parent.name != 'runs' or _reparse(self.root):
            raise ValueError('public_png_root_invalid')
        self.root = self.root.resolve()
        self.policy = policy
        self.now = now
        self.deadline = deadline
        self.stop = stop
        self.planning_deadline = time.monotonic() + max(0, deadline - time.monotonic()) * 0.6
        self.report = dict(root=str(self.root), adapter=kind, dry_run=True, mode=policy.failure_mode, candidates=[], protected=[], errors=[], budget_exhausted=False, deadline_monotonic=deadline)
        self.tokens = {self.root: _token(self.root)}
        self.units = []
        self.seen = set()

    def expired(self):
        return time.monotonic() > self.planning_deadline or (self.stop is not None and self.stop.is_set())

    def protect(self, path, reason):
        path = Path(path).absolute()
        allowed = (self.root.parent / 'public-tasks', self.root.parent / 'seed-cycles')
        if any((_inside(path, r) for r in allowed)):
            self.report['protected'].append(dict(scope=str(path), reason=reason))

    def read(self, path, scope):
        try:
            before = _token(path)
            v = _read(path) if before is not None else {}
            if _token(path) != before:
                raise ValueError('metadata_changed_during_read')
            self.tokens[path] = before
            return v
        except (OSError, ValueError, TypeError):
            try:
                self.tokens[path] = _token(path)
            except OSError:
                pass
            self.protect(scope, 'metadata_unreadable:' + path.name)
            self.report['errors'].append(dict(path=str(path), reason='metadata_unreadable'))
            return {}

    def targets(self, value):
        for key in REF_KEYS + ('source', 'plant_source', 'harvest_source', 'refill_source', 'output'):
            name = value.get(key)
            if isinstance(name, str) and name:
                p = Path(name).resolve()
                if p.suffix.lower() == '.json':
                    p = p.parent
                if any((_inside(p, self.root.parent / r) for r in ('public-tasks', 'seed-cycles'))):
                    yield p

    def owner(self, scope, value, reason):
        queue = [(Path(scope), value)]
        for _ in range(16):
            if not queue:
                break
            p, v = queue.pop()
            self.protect(p, reason)
            queue.extend(((x, {}) for x in self.targets(v)))
            if p in self.seen:
                continue
            self.seen.add(p)
            if p.suffix.lower() == '.png':
                continue
            for name in ('context.json', 'adapter_result.json'):
                queue.extend(((x, {}) for x in self.targets(self.read(p / name, p))))
        for p, _ in queue:
            self.protect(p, 'recovery_reference_budget')

    def tasks(self):
        active = self.read(self.root / 'active.json', self.root)
        if self.tokens.get(self.root / 'active.json') is not None:
            output = active.get('output')
            if isinstance(output, str) and _inside(Path(output).resolve(), self.root):
                self.owner(Path(output).resolve(), active, 'root_active_output')
            else:
                self.protect(self.root, 'active_scope_unknown')
        records = []
        resolved = set()
        for directory in sorted(self.root.iterdir()):
            if self.expired():
                self.report['budget_exhausted'] = True
                return
            if not directory.name.isdigit() or not directory.is_dir():
                continue
            if _reparse(directory):
                self.protect(directory, 'reparse_attempt')
                continue
            self.tokens[directory] = _token(directory)
            for unit in directory.iterdir():
                if not unit.is_dir() or unit.name.lower() in {'fixtures', 'fixture'}:
                    continue
                if _reparse(unit):
                    self.protect(unit, 'reparse_task')
                    continue
                terminal = unit / 'adapter_result.json'
                v = self.read(terminal, unit)
                done = v.get('ok') is True and (v.get('game_task_complete') is True or v.get('skip_verified') is True)
                records.append((unit, terminal, v, done))
                if done:
                    resolved.update(self.targets(v))
        for unit, terminal, v, done in records:
            if self.expired():
                self.report['budget_exhausted'] = True
                return
            context = self.read(unit / 'context.json', unit) if not done else {}
            if not done and (_pending(v) or _pending(context)) and (unit not in resolved):
                self.owner(unit, v, 'pending_public_task')
                self.owner(unit, context, 'pending_public_context')
                continue
            if not done and v.get('ok') is not False:
                self.protect(unit, 'terminal_result_unavailable')
                continue
            self.units.append((unit, terminal, v, not done))

    def seeds(self):
        for port in sorted(self.root.iterdir()):
            if self.expired():
                self.report['budget_exhausted'] = True
                return
            if not port.is_dir() or not port.name.isdigit() or (not 0 < int(port.name) <= 65535):
                continue
            if _reparse(port):
                self.protect(port, 'reparse_port')
                continue
            self.tokens[port] = _token(port)
            latest = self.read(port / 'latest.json', port)
            if latest.get('directory'):
                target = Path(latest['directory']).resolve()
                if target.parent != port.resolve() or not target.name.isdigit():
                    self.protect(port, 'latest_scope_invalid')
            for run in sorted(port.iterdir()):
                if self.expired():
                    self.report['budget_exhausted'] = True
                    return
                if not run.is_dir() or not run.name.isdigit():
                    continue
                if _reparse(run):
                    self.protect(run, 'reparse_seed')
                    continue
                terminal = run / 'state.json'
                state = self.read(terminal, run)
                status = state.get('status')
                if status in {'running', 'paused'}:
                    current = state.get('current') or {}
                    if not isinstance(current, dict):
                        self.protect(run, 'seed_current_invalid')
                        continue
                    self.owner(run, current, 'seed_' + status)
                    attempt = current.get('attempt') or {}
                    if isinstance(attempt, dict):
                        self.owner(run, attempt, 'seed_current_attempt')
                elif status in {'completed', 'ended'}:
                    self.units.append((run, terminal, state, False))
                else:
                    self.protect(run, 'seed_status_unknown')

    def choose(self, protected, limit):
        for unit, terminal, result, failed in self.units:
            if self.expired():
                self.report['budget_exhausted'] = True
                break
            if any((_inside(unit, p) for p in protected)):
                continue
            stamp = self.tokens.get(terminal)
            if stamp is None or self.now - stamp[3] / 1000000000.0 < DAY:
                continue
            images = []
            stack = [unit]
            while stack:
                if self.expired():
                    self.report['budget_exhausted'] = True
                    break
                directory = stack.pop()
                try:
                    with os.scandir(io_path(directory)) as entries:
                        for e in entries:
                            p = directory / e.name
                            if e.is_dir(follow_symlinks=False):
                                if not _reparse(p) and e.name.lower() not in {'fixtures', 'fixture', '__pycache__'}:
                                    stack.append(p)
                            elif e.name.lower().endswith('.png') and (not e.is_symlink()):
                                images.append(p)
                except OSError:
                    self.protect(directory, 'enumeration_failed')
                    self.report['errors'].append(dict(path=str(directory), reason='enumeration_failed'))
            if self.report['budget_exhausted']:
                break
            protected = protected + [Path(x['scope']) for x in self.report['protected']]
            wanted = (result.get('last_observation') or {}).get('screenshot_path') or (result.get('last_observation') or {}).get('screenshot')
            ranked = sorted(images, key=lambda p: (str(p) == wanted, p.name), reverse=True)
            keys = set(ranked[:self.policy.failure_keys]) if failed and self.policy.failure_mode == 'keys_7d' else set()
            for p in images:
                if self.expired():
                    self.report['budget_exhausted'] = True
                    break
                if any((_inside(p, scope) for scope in protected)):
                    continue
                try:
                    token = _token(p)
                except OSError:
                    continue
                if token is None or self.now - max(stamp[3], token[3]) / 1000000000.0 < (7 * DAY if p in keys else DAY):
                    continue
                self.report['candidates'].append(dict(path=str(p), unit=str(unit), size=token[2], token=token, guards=[dict(path=str(terminal), token=stamp)]))
                if len(self.report['candidates']) >= limit:
                    self.report['budget_exhausted'] = True
                    break
            if self.report['budget_exhausted']:
                break

def plan(workspace, *, now=None, policy=None, stop=None):
    """Discover both explicit lifecycle roots before selecting any expired PNG."""
    root = Path(workspace).resolve()
    policy = policy or Policy()
    now = time.time() if now is None else now
    deadline = time.monotonic() + policy.max_seconds
    archives = []
    for kind, name in [('public_tasks', 'public-tasks'), ('seed_cycles', 'seed-cycles')]:
        path = root / 'runs' / name
        if not io_path(path).exists():
            continue
        a = Archive(path, kind, policy, now, deadline, stop)
        archives.append(a)
        try:
            a.tasks() if kind == 'public_tasks' else a.seeds()
        except (OSError, ValueError, TypeError):
            a.protect(a.root, 'ownership_discovery_failed')
            a.report['errors'].append(dict(path=str(a.root), reason='ownership_discovery_failed'))
    protected = [Path(x['scope']) for a in archives for x in a.report['protected']]
    metadata = [dict(path=str(p), token=t) for a in archives for p, t in a.tokens.items()]
    discovered = not any((a.report['budget_exhausted'] for a in archives))
    remaining = policy.max_files
    for a in archives:
        if discovered and remaining > 0:
            a.choose(protected, remaining)
        else:
            a.report['budget_exhausted'] = True
        remaining -= len(a.report['candidates'])
        a.report['metadata_tokens'] = metadata
        a.report['candidate_count'] = len(a.report['candidates'])
        a.report['candidate_bytes'] = sum((x['size'] for x in a.report['candidates']))
    return [a.report for a in archives]

def maintain(workspace, *, policy=None, now=None, stop=None):
    root = Path(workspace).resolve()
    policy = policy or Policy()
    now = time.time() if now is None else now
    state = root / 'runs/maintenance/public-png-retention.json'
    if io_path(state).exists():
        try:
            saved = _read(state)
            if now < saved.get('retry_after', 0):
                return dict(skipped='recent_sweep')
        except (OSError, ValueError, TypeError):
            pass
    reports = plan(root, policy=policy, now=now, stop=stop)
    for report in reports:
        apply(report, stop=stop)
    complete = all((not r['budget_exhausted'] and (not r['errors']) for r in reports))
    summary = dict(checked_at=now if complete else 0, retry_after=now + (policy.min_interval if complete else 60), removed=sum((r['removed'] for r in reports)), removed_bytes=sum((r['removed_bytes'] for r in reports)), errors=[x for r in reports for x in r['errors']][:20], complete=complete)
    try:
        io_path(state.parent).mkdir(parents=True, exist_ok=True)
        temporary = state.with_suffix('.%s.tmp' % os.getpid())
        io_path(temporary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')
        io_path(temporary).replace(io_path(state))
    except OSError:
        pass
    return summary

def worker_maintenance(workspace, execute, emit, stop=None):
    if execute is not True:
        return
    try:
        if stop is not None and stop.is_set():
            return
        result = maintain(workspace, stop=stop)
        if result.get('removed') or result.get('errors'):
            emit(dict(type='maintenance', message='历史截图清理：移除%d张，清理错误%d项；任务照常继续。' % (result.get('removed', 0), len(result.get('errors', [])))))
    except Exception:
        try:
            emit(dict(type='maintenance', message='历史截图清理未完成；任务照常继续。'))
        except Exception:
            pass
