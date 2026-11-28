"""Bounded Racing Carnival options with strict type & budget validation."""

STRATEGIES = ('daily_rotate', 'first_win_then_target', 'single_track')
DIFFICULTIES = ('困难', '普通', '简单')

DEFAULTS = dict(
    track_strategy='daily_rotate',
    target_track=1,
    difficulty='困难',
    max_races=10,
    challenge_enabled=True,
    allow_existing_jelly=False,
    max_jellies=0,
)


def _parse_bounded_int(val, default, hi, err_code):
    if type(val) is bool:
        raise ValueError(err_code)
    if isinstance(val, str):
        s = val.strip()
        if s == '':
            val = default
        elif s.isascii() and s.isdigit():
            val = int(s)
        else:
            raise ValueError(err_code)
    if type(val) is not int or not 0 <= val <= hi:
        raise ValueError(err_code)
    return val


def normalize_options(options=None):
    raw = {} if options is None else options
    if not isinstance(raw, dict):
        raise ValueError('racing_carnival_options_invalid')
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError('racing_carnival_unknown_options:' + ','.join(sorted(unknown)))
    merged = {**DEFAULTS, **raw}

    strategy = merged['track_strategy']
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ValueError('racing_carnival_strategy_invalid')

    target_track = merged['target_track']
    if type(target_track) is not int or target_track not in {1, 2, 3, 4}:
        raise ValueError('racing_carnival_target_track_invalid')

    difficulty = merged['difficulty']
    if not isinstance(difficulty, str) or difficulty not in DIFFICULTIES:
        raise ValueError('racing_carnival_difficulty_invalid')

    merged['max_races'] = _parse_bounded_int(
        merged['max_races'], DEFAULTS['max_races'], 999, 'racing_carnival_max_races_invalid'
    )

    for key in ('challenge_enabled', 'allow_existing_jelly'):
        if type(merged[key]) is not bool:
            raise ValueError(f'racing_carnival_{key}_invalid')

    merged['max_jellies'] = _parse_bounded_int(
        merged['max_jellies'], DEFAULTS['max_jellies'], 999, 'racing_carnival_max_jellies_invalid'
    )

    return merged


validate = normalize_options
