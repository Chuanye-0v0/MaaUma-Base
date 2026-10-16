"""Offline-first MFA boundary. No device or account imports in the preview package."""
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class HarvestOptions:
    learn_skills: bool = True
    update_team: bool = True
    return_home: bool = False
    refine_skills: bool = False
    skill_recheck_min_pt: int = 60

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) - {'learn_skills', 'update_team', 'return_home', 'refine_skills', 'skill_recheck_min_pt'}:
            raise ValueError('invalid_harvest_options')
        if any(type(item) is not bool for key, item in value.items() if key != 'skill_recheck_min_pt'):
            raise ValueError('invalid_harvest_option_type')
        threshold = value.get('skill_recheck_min_pt', 60)
        if type(threshold) is not int or not 0 <= threshold <= 9999:
            raise ValueError('invalid_harvest_option_type')
        return cls(**value)

    def profile_fields(self):
        return dict(harvest_learn_skills=self.learn_skills, harvest_update_team=self.update_team,
                    harvest_return_home=self.return_home)


class MaaStopSignal:
    """Adapt Maa task cancellation to the existing runner stop-event contract."""
    def __init__(self, tasker):
        self.tasker = tasker

    def is_set(self):
        return bool(self.tasker.stopping)

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(.05, remaining))
        return True


def run_preview(options, emit, stop, *, fail_at=None):
    """Simulated receipts only; success never represents a completed game task."""
    stages = ['检查成熟育成']
    stages += ['学习技能'] if options.learn_skills else ['跳过技能']
    stages += ['结束育成', '保留原因子', '完成结算与领奖']
    stages += ['更新JJC编成'] if options.update_team else ['跳过JJC编成']
    stages += ['返回主页']
    events = []
    result = dict(simulated=True, device_actions=0, game_task_complete=False,
                  profile_fields=options.profile_fields(), events=events)
    for stage in stages:
        if stop.wait(.15):
            return {**result, 'ok': False, 'reason': 'operator_stop'}
        if stage == fail_at:
            return {**result, 'ok': False, 'reason': 'simulated_failure:' + stage}
        event = dict(type='progress', phase='harvest', simulated=True, message='[离线模拟] ' + stage)
        events.append(event)
        emit(event)
    return {**result, 'ok': True, 'reason': 'offline_preview_complete',
            'next_action': 'stop_current_account' if options.return_home else 'continue_selected_tasks'}
