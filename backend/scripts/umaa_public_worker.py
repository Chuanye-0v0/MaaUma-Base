"""One current-account task in the embedded public runtime; no device discovery."""
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main():
    from desktop.umaa_public_context import bind, check_task, endpoint, self_test
    if sys.argv[1:] == ['--self-test']:
        print(json.dumps(self_test(), ensure_ascii=False))
        return 0
    request_path = Path(sys.argv[1]).resolve()
    package = ROOT.parent.resolve()
    if not request_path.is_relative_to(package / 'runs/jobs'):
        raise ValueError('public_request_scope_invalid')
    request = json.loads(request_path.read_text(encoding='utf8'))
    check_task(request['task'])
    bind(request['connection'])
    ocr = package / 'ocr'
    os.environ['PATH'] = str(ocr) + os.pathsep + os.environ.get('PATH', '')
    os.environ['TESSDATA_PREFIX'] = str(ocr / 'tessdata')
    from scripts.mfa_worker import FileStop
    from desktop.mfa_backend import CurrentAccountBackend
    stop = FileStop(request_path.parent / 'stop', request.get('parent_pid'))

    def emit(event):
        with (request_path.parent / 'events.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    try:
        if request['task'] == 'seed_cycle':
            from desktop.umaa_seed_cycle import SeedCycle
            backend = SeedCycle(ROOT / 'runs/seed-cycles', endpoint=endpoint(), adb_path=request['connection']['adb_path'], skill_database=ROOT / 'data/skill_database.json')
            result = backend.run(request['options'], emit, stop, execute=request.get('execute') is True)
        else:
            backend = CurrentAccountBackend(ROOT / 'runs/public-tasks', endpoint=endpoint(), adb_path=request['connection']['adb_path'], skill_database=ROOT / 'data/skill_database.json', live_enabled=True)
            result = backend.run(request['task'], request['options'], emit, stop, execute=request.get('execute') is True)
    except Exception as error:
        reason = str(error) if request['task'] == 'seed_cycle' and isinstance(error, (ValueError, RuntimeError)) else 'worker_exception'
        result = dict(ok=False, game_task_complete=False, reason=reason, error_type=type(error).__name__, device_action_count_complete=False)
    finally:
        stop.close()
    (request_path.parent / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf8')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
