"""Maa agent for current-account tasks only. No account inventory or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'backend'))
NATIVE = PACKAGE / 'runtimes/win-x64/native'
os.environ['PATH'] = str(NATIVE) + os.pathsep + str(PACKAGE / 'libs') + os.pathsep + os.environ.get('PATH', '')


class ProcessBackend:
    def __init__(self):
        self.root = PACKAGE / 'runs/jobs'

    def queue_generation(self):
        try:
            return (PACKAGE / 'runs/queue-generation.txt').read_text(encoding='utf8').strip()
        except OSError:
            return None

    def run(self, task, options, emit, stop, *, execute=False):
        from desktop.umaa_public_context import check_task
        check_task(task)
        if stop.is_set():
            return dict(ok=False, reason='operator_stop', game_task_complete=False, device_actions=0)
        connection = json.loads((PACKAGE/'config/public-connection.json').read_text(encoding='utf8'))
        self.root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='job-', dir=self.root))
        request = directory/'request.json'
        request.write_text(json.dumps(dict(task=task, options=options, execute=execute,
            connection=connection, parent_pid=os.getpid()), ensure_ascii=False), encoding='utf8')
        command = [str(PACKAGE/'runtime/python.exe'), '-B', '-X', 'utf8',
                   str(PACKAGE/'backend/scripts/umaa_public_worker.py'), str(request)]
        with subprocess.Popen(command, cwd=PACKAGE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as worker:
            offset = 0
            while True:
                exited = worker.poll() is not None
                if stop.is_set():
                    (directory/'stop').touch(exist_ok=True)
                events = directory/'events.jsonl'
                if events.exists():
                    with events.open('rb') as stream:
                        stream.seek(offset)
                        while True:
                            position = stream.tell(); line = stream.readline()
                            if not line or not line.endswith(b'\n'):
                                offset = position; break
                            offset = stream.tell()
                            try:
                                emit(json.loads(line))
                            except Exception:
                                (directory/'stop').touch(exist_ok=True)
                                raise
                if exited:
                    break
                time.sleep(.1)
        result = directory/'result.json'
        if not result.is_file():
            return dict(ok=False, reason='worker_result_missing', game_task_complete=False,
                        device_action_count_complete=False)
        return json.loads(result.read_text(encoding='utf8'))


def main():
    if sys.argv[1:] == ['--self-test']:
        from desktop.umaa_public_context import self_test
        print(json.dumps(self_test(), ensure_ascii=False)); return 0
    from maa.agent.agent_server import AgentServer
    from maa.toolkit import Toolkit
    from maa.library import Library
    from mfa_action import BackendTaskAction
    from desktop.umaa_public_context import TASKS
    # This is the bundled native SDK, not an emulator manufacturer's DLL.
    Library.open(NATIVE, agent_server=True)
    backend = ProcessBackend()
    names = {'plant':'UmaPlant','harvest':'UmaHarvest','jjc':'UmaJjc','activities':'UmaActivities',
        'daily_legend':'UmaDailyLegend','limited_legend':'UmaLimitedLegend',
        'champions_registration':'UmaChampionsRegistration','champions_race':'UmaChampionsRace',
        'story_event':'UmaStoryEvent','daily_rewards':'UmaDailyRewards','cleanup':'UmaCleanup',
        'seed_cycle':'UmaSeedCycle'}
    for task in TASKS:
        AgentServer.register_custom_action(names[task]+'Backend', BackendTaskAction(backend, task))
        AgentServer.register_custom_action(names[task]+'BackendLive', BackendTaskAction(backend, task, execute=True))
    if len(sys.argv) != 2:
        return 2
    Toolkit.init_option(str(PACKAGE))
    AgentServer.start_up(sys.argv[-1])
    try:
        AgentServer.join()
    finally:
        AgentServer.shut_down()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
