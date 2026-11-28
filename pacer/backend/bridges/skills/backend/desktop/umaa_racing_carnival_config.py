"""Add Racing Carnival task with concise 3-option UI, auto jelly refill, and preserved defaults."""
from core.racing_carnival.options import DEFAULTS

NODE = 'UmaRacingCarnival'

OPPONENT_CASES = [
    ('1234轮序（推荐）', {'track_strategy': 'daily_rotate'}),
    *(
        (f'第{i}赛道（{"左上" if i == 1 else "右上" if i == 2 else "左下" if i == 3 else "右下"}）', {'track_strategy': 'single_track', 'target_track': i})
        for i in (1, 2, 3, 4)
    ),
]
CHALLENGE_CASES = [
    ('开启（优先打挑战赛）', {'challenge_enabled': True}),
    ('关闭（跳过挑战赛）', {'challenge_enabled': False}),
]
RACE_COUNT_CASES = [
    ('仅当前自然RP（不吃果冻）', {'max_races': 5, 'allow_existing_jelly': False, 'max_jellies': 0}),
    ('5场（不足自动吃已有果冻）', {'max_races': 5, 'allow_existing_jelly': True, 'max_jellies': 5}),
    ('10场（不足自动吃已有果冻）', {'max_races': 10, 'allow_existing_jelly': True, 'max_jellies': 10}),
    ('15场（不足自动吃已有果冻）', {'max_races': 15, 'allow_existing_jelly': True, 'max_jellies': 15}),
    ('20场（不足自动吃已有果冻）', {'max_races': 20, 'allow_existing_jelly': True, 'max_jellies': 20}),
]

OPTION_NAMES = [
    '嘉年华选择对手',
    '嘉年华出现隐藏先打',
    '嘉年华参赛次数',
]
_LEGACY_OPTIONS = (
    '嘉年华赛道策略',
    '嘉年华目标赛道',
    '嘉年华难度',
    '嘉年华每日场次上限',
    '嘉年华挑战赛事',
    '嘉年华额外补RP',
)


def configure(interface, pipeline, *, live=False):
    task_def = dict(
        name='竞速嘉年华',
        entry=NODE,
        default_check=False,
        description=(
            '按轮序或指定赛道参战竞速嘉年华（默认困难、复用传奇选马），出现隐藏挑战赛可优先参战，'
            'RP不足时按次数自动消耗已有迷你胡萝卜果冻，并自动领取日常与期间限定任务奖励。严禁花宝石、兑换或进养成。'
        ),
        option=list(OPTION_NAMES),
    )
    tasks = interface['task']
    existing = next((i for i, row in enumerate(tasks) if row.get('entry') == NODE), None)
    if existing is None:
        tasks.append(task_def)
    else:
        tasks[existing] = dict(tasks[existing], **task_def)

    settings = interface.setdefault('option', {})
    for legacy in _LEGACY_OPTIONS:
        settings.pop(legacy, None)
    for opt_name, default_case, pairs in [
        ('嘉年华选择对手', OPPONENT_CASES[0][0], OPPONENT_CASES),
        ('嘉年华出现隐藏先打', CHALLENGE_CASES[0][0], CHALLENGE_CASES),
    ]:
        settings[opt_name] = dict(
            type='select',
            default_case=default_case,
            cases=[
                dict(name=label, pipeline_override={NODE: dict(custom_action_param=dict(params))})
                for label, params in pairs
            ],
        )

    settings['嘉年华参赛次数'] = dict(
        type='input',
        inputs=[
            dict(
                name='max_races',
                label='嘉年华参赛次数',
                description=(
                    '填写本轮参赛场数上限（0–999，默认 10；填 0 表示不限场数，打完自然 RP 与已有迷你胡萝卜果冻为止）。'
                    '自然 RP 用尽后会自动按剩余场数补充已有迷你果冻，果冻或补充上限耗尽后自动领奖收尾。'
                ),
                default='10',
                pipeline_type='int',
                verify=r'^(?:0|[1-9]\d{0,2})$',
            )
        ],
        pipeline_override={
            NODE: dict(
                custom_action_param=dict(
                    max_races='{max_races}',
                    allow_existing_jelly=True,
                    max_jellies=999,
                )
            )
        },
    )

    for case in settings.get('执行方式', {}).get('cases', []):
        overrides = case.get('pipeline_override', {})
        reference = (overrides.get('UmaJjc') or {}).get('custom_action', '')
        if reference:
            overrides[NODE] = dict(
                custom_action='UmaRacingCarnivalBackend' + ('Live' if reference.endswith('Live') else '')
            )

    default_param = dict(DEFAULTS)
    default_param.update(allow_existing_jelly=True, max_jellies=999)
    pipeline[NODE] = dict(
        recognition='DirectHit',
        action='Custom',
        custom_action='UmaRacingCarnivalBackend' + ('Live' if live else ''),
        custom_action_param=default_param,
        next=[],
        focus={
            'Node.Action.Starting': '竞速嘉年华：赛道参战、挑战赛与任务领奖',
            'Node.Action.Failed': '竞速嘉年华未完成，保留原账号与原场次，请查看停止原因',
        },
    )
    return interface, pipeline
