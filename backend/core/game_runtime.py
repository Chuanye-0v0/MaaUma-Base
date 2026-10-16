"""Public candidate: startup and reauthentication remain manual.

The ordinary task owners still handle known in-game navigation. This module
does not contain saved-login, password input, account switching or restart code.
"""
import json
from pathlib import Path


class GameRuntime:
    def __init__(self, endpoint, adb_path, output_dir, *, agree_startup, emit, stop):
        self.endpoint, self.output_dir = endpoint, Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.emit, self.stop = emit, stop

    def launch_if_exited(self):
        return None

    def prepare(self, **kwargs):
        return dict(ok=False, reason='public_manual_game_start_required', records=[],
                    message='请手动登录当前账号并进入游戏主页；公开候选不会登录或切换账号。')

    def restart_if_stalled(self, observed):
        return None

    def save(self, name, value):
        target = self.output_dir / name
        target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
        return str(target)
