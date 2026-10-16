"""One local, cancellable library scan shared by the two panels."""
from contextlib import ExitStack, contextmanager
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
ROOT = Path(__file__).resolve().parents[1]
from core.parent_library import store_path, load_library, save_library, upsert_entry, LibraryConflict

def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf8'))
    except (OSError, ValueError):
        return {}

def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temp.replace(path)

def _alive(pid):
    import psutil
    return type(pid) is int and pid > 0 and psutil.pid_exists(pid)

@contextmanager
def _device_lease(directory, endpoint):
    """Same single-byte lock protocol as both existing device owners."""
    import msvcrt
    path = Path(directory) / (endpoint.replace(':', '_') + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('device_busy') from None
        try:
            _write(path.with_suffix('.json'), {'owner': 'parent_library_scan', 'endpoint': endpoint, 'pid': os.getpid(), 'started_at': time.time()})
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

def scan_status(root=None):
    base = store_path(root).parent
    current = _read(base / 'scan-status.json')
    if not current:
        return {'available': True, 'state': 'idle', 'running': False, 'scan_id': None, 'scanned': 0, 'total': None, 'complete': False, 'scope': 'visible_library', 'message': '请先在游戏中打开名人堂列表，关闭筛选，然后开始扫描。'}
    if current.get('running') and (not _alive(current.get('pid'))):
        current.update(state='error', running=False, complete=False, message='扫描进程已退出，已读取的成品仍保留。')
    return {key: value for key, value in current.items() if key not in {'pid', 'adb_path', 'store_root'}}

def start_scan(request, root=None):
    endpoint = request.get('endpoint', '')
    adb = Path(request.get('adb_path', ''))
    match = re.fullmatch('127\\.0\\.0\\.1:(\\d{1,5})', str(endpoint))
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ValueError('请在面板中选择本机模拟器连接。')
    if not adb.is_file() or adb.name.lower() != 'adb.exe':
        raise ValueError('请先设置模拟器的 adb.exe。')
    limit = request.get('limit')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 2000):
        raise ValueError('扫描数量应为1～2000，留空扫描整个库。')
    owner_pid = request.get('owner_pid')
    if owner_pid is not None and (type(owner_pid) is not int or owner_pid <= 0 or (not _alive(owner_pid))):
        raise ValueError('发起扫描的面板已退出，请重新打开后再试。')
    base = store_path(root).parent
    base.mkdir(parents=True, exist_ok=True)
    guard = base / 'scan-start.lock'
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
    except FileExistsError:
        raise ValueError('另一个面板正在启动扫描，请稍后刷新。') from None
    try:
        os.close(fd)
        if scan_status(root)['running']:
            raise ValueError('种马库正在扫描，请先停止当前扫描。')
        scan_id = uuid.uuid4().hex
        job = base / 'scans' / scan_id
        job.mkdir(parents=True)
        config = {'scan_id': scan_id, 'endpoint': endpoint, 'adb_path': str(adb.resolve()), 'limit': limit}
        if owner_pid is not None:
            config['owner_pid'] = owner_pid
        _write(job / 'request.json', config)
        state = {'available': True, 'state': 'starting', 'running': True, 'scan_id': scan_id, 'endpoint': endpoint, 'scanned': 0, 'total': None, 'complete': False, 'scope': 'visible_library', 'message': '正在检查名人堂列表…'}
        args = [sys.executable, '-B', '-X', 'utf8', '-m', 'desktop.parent_library_jobs', '--worker', scan_id, '--store-root', str(base)]
        with (job / 'worker.log').open('w', encoding='utf8') as log:
            process = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _write(base / 'scan-status.json', {**state, 'pid': process.pid})
        return state
    finally:
        guard.unlink()

def stop_scan(request, root=None):
    state = scan_status(root)
    if request.get('scan_id') != state.get('scan_id') or not state.get('scan_id'):
        raise ValueError('扫描任务已变化，请刷新后再停止。')
    if state['running']:
        (store_path(root).parent / 'scans' / state['scan_id'] / 'stop').write_text('stop', encoding='ascii')
        state['message'] = '正在停止；已读取的成品会保留。'
    return state

class ScanDevice:

    def __init__(self, adb, endpoint, job, base):
        self.adb, self.endpoint, self.job, self.base = (adb, endpoint, job, base)

    def command(self, *args):
        return subprocess.run([self.adb, '-s', self.endpoint, *map(str, args)], capture_output=True, timeout=15, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def connect(self):
        reply = subprocess.run([self.adb, 'connect', self.endpoint], capture_output=True, text=True, encoding='utf8', errors='replace', timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        accepted = {'connected to ' + self.endpoint, 'already connected to ' + self.endpoint}
        if reply.returncode or not accepted.intersection((line.strip() for line in reply.stdout.splitlines())):
            raise RuntimeError('ADB连接未成功，请检查所选模拟器端口。')

    def capture(self):
        import cv2, numpy as np
        data = self.command('exec-out', 'screencap', '-p').stdout
        frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError('未取得模拟器画面，请检查连接。')
        self.last_frame = frame
        return frame

    def tap(self, x, y):
        self.command('shell', 'input', 'tap', int(x), int(y))

    def swipe(self, x1, y1, x2, y2, duration=450):
        self.command('shell', 'input', 'swipe', int(x1), int(y1), int(x2), int(y2), int(duration))

    def save_evidence(self, image, page, entry_id):
        import cv2
        path = self.job / entry_id / f'page-{page:03d}.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imencode('.png', image)[1].tofile(str(path))
        return path.relative_to(self.base).as_posix()

def worker(scan_id, root):
    if not re.fullmatch('[a-f0-9]{32}', scan_id):
        raise ValueError('scan_id_invalid')
    base = store_path(root).parent
    job = base / 'scans' / scan_id
    config = _read(job / 'request.json')
    state = {'available': True, 'state': 'running', 'running': True, 'scan_id': scan_id, 'pid': os.getpid(), 'endpoint': config.get('endpoint'), 'scanned': 0, 'total': None, 'complete': False, 'scope': 'visible_library', 'message': '正在读取种马库…'}
    for _ in range(100):
        if _read(base / 'scan-status.json').get('scan_id') == scan_id:
            break
        time.sleep(0.02)

    def progress(value):
        state.update({k: v for k, v in value.items() if k in {'scanned', 'total', 'complete', 'message'}})
        if isinstance(value.get('current'), dict):
            state['current'] = {key: item for key, item in value['current'].items() if key in {'ordinal', 'page'} and type(item) is int}
        current = state.get('current', {})
        detail = f"，正在读取第 {current['ordinal']} 只的因子" if current.get('ordinal') else '，正在读取因子'
        state['message'] = f"已处理 {state['scanned']} / {(state.get('total') if state.get('total') is not None else '未知')} 只{detail}…"
        _write(base / 'scan-status.json', state)

    def stop():
        return (job / 'stop').exists() or (config.get('owner_pid') is not None and (not _alive(config['owner_pid'])))
    saved = []

    def entry_ready(entry, evidence=None):
        for attempt in range(3):
            library = load_library(root=base)
            try:
                save_library(upsert_entry(library, entry), root=base, expected_revision=library['revision'])
                break
            except LibraryConflict:
                if attempt == 2:
                    raise
                time.sleep(0.08)
        saved.append(entry['entry_id'])
        if evidence:
            _write(job / entry['entry_id'] / 'scan-evidence.json', evidence)
    driver = None
    try:
        os.environ.update(UMA_ADB_PORT=config['endpoint'].rsplit(':', 1)[1], UMA_ADB_PATH=config['adb_path'])
        with ExitStack() as stack:
            if ROOT.name.lower() == 'backend':
                from desktop import product_updates
                backend_lease = getattr(product_updates, 'backend_process_lease', None)
                if backend_lease is not None:
                    stack.enter_context(backend_lease(ROOT.parent))
            local = os.environ.get('LOCALAPPDATA')
            if not local:
                raise RuntimeError('无法确定本机设备占用目录。')
            stack.enter_context(_device_lease(Path(local) / 'UmaSuite/device-locks', config['endpoint']))
            stack.enter_context(_device_lease(ROOT / 'runs/device-occupancy', config['endpoint']))
            stack.enter_context(_device_lease(base / 'device-occupancy', config['endpoint']))
            from core.parent_library_scan import run_scan
            driver = ScanDevice(config['adb_path'], config['endpoint'], job, base)
            if not stop():
                driver.connect()
            result = run_scan(driver, on_entry=entry_ready, on_progress=progress, stop=stop, limit=config.get('limit'))
        reason = result.get('reason')
        messages = {'operator_stop': '已停止扫描，已读取的成品保留。', 'scan_limit': '本次限量扫描完成，已读取的成品保留。', 'scan_complete': '种马库扫描完成。', 'scan_contains_incomplete_entries': '已遍历本次列表，部分因子待校对，请查看各条目的缺项。', 'next_detail_unconfirmed': '没有确认切换到下一只，已停止扫描并保留记录。', 'library_top_unconfirmed': '没有确认回到列表顶部，未把当前页当成完整库。', 'library_first_cell_unreadable': '列表第一只未读清，请检查画面后重新扫描。', 'library_page_required': '所选模拟器未停在名人堂列表。请核对扫描设备，并打开“强化编成 → 优俊少女名人堂 → 一览”。', 'library_filter_active': '名人堂列表开启了筛选，请先关闭筛选后再扫描。', 'library_filter_unreadable': '未读清名人堂的筛选状态，请等待画面稳定、确认筛选关闭后重试。', 'library_count_unreadable': '未读清名人堂的成品总数，请等待列表加载完成后重试。', 'library_cells_unreadable': '未读清名人堂列表中的成品，请等待列表加载完成后重试。', 'capture_or_observation_failed': '未能读取所选模拟器画面，请检查设备连接后重试。', 'unfiltered_library_required': '请核对扫描设备，并停在关闭筛选的名人堂一览页面后重试。', 'scroll_overlap_missing': '当前成品的因子翻页未能衔接，补采后仍未读全，已停止并保留待校对记录。', 'scroll_overlap_ambiguous': '当前成品出现难以区分的重复因子行，补采后仍未读全，已停止并保留待校对记录。', 'detail_changed_during_scroll': '未能确认滚动前后仍为同一成品，已停止并保留当前记录。', 'factor_scroll_stalled': '当前成品的因子列表没有继续移动，未确认读到底，已停止并保留记录。', 'factor_page_limit': '当前成品的因子读取超过本次检查上限，尚未确认完整，已停止并保留记录。', 'factor_top_unverified': '未确认当前成品的因子列表顶部，已停止并保留记录。', 'factor_rows_unreadable': '当前页因子未读清，已停止，未跳过当前成品。', 'group_header_unreadable': '本体或亲代的因子起始行未读清，已停止，未跳过当前成品。', 'factor_groups_incomplete': '本体及两位亲代的因子尚未读全，已停止，未跳过当前成品。', 'bottom_factor_row_clipped': '末尾因子仍有遮挡，未确认完整，已停止并保留记录。', 'too_many_factor_groups': '本体与亲代的因子分组未能确认，已停止并保留记录。'}
        finished = reason in {'scan_limit', 'scan_complete', 'scan_contains_incomplete_entries'}
        state.update(state='stopped' if stop() else 'completed' if finished else 'error', running=False, complete=result.get('complete') is True, scanned=len(saved), message=result.get('message') or messages.get(reason, '扫描未能继续，已读取条目保留。'), coverage_complete=result.get('coverage_complete') is True, reason=reason)
    except Exception as error:
        state.update(state='error', running=False, complete=False, scanned=len(saved), message='同设备正在执行其他任务，请先停止该任务。' if str(error) == 'device_busy' else '扫描暂停，已读取条目保留。' + str(error)[:180])
        _write(job / 'error.json', {'type': type(error).__name__, 'message': str(error)[:500]})
    if state['state'] == 'error' and driver is not None and (getattr(driver, 'last_frame', None) is not None):
        import cv2
        cv2.imencode('.png', driver.last_frame)[1].tofile(str(job / 'stopped.png'))
        state['diagnostic_screenshot'] = (job / 'stopped.png').relative_to(base).as_posix()
    _write(base / 'scan-status.json', state)
    _write(job / 'result.json', {**state, 'saved_entry_ids': saved})
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', required=True)
    parser.add_argument('--store-root', type=Path, required=True)
    args = parser.parse_args()
    worker(args.worker, args.store_root)
