DEFAULTS = dict(max_entries=15, special_extra=True, allow_existing_jelly=True,
                max_jellies=5, max_clocks=5,
                recruit=True, special_race=True)


def normalize_options(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('strongest_team_options_invalid')
    result = {**DEFAULTS, **value}
    for key in ['special_extra', 'allow_existing_jelly', 'recruit', 'special_race']:
        if type(result[key]) is not bool:
            raise ValueError('strongest_team_options_invalid:'+key)
    for key, maximum in [('max_entries',15),('max_jellies',5),('max_clocks',5)]:
        if type(result[key]) is not int or not 0 <= result[key] <= maximum:
            raise ValueError('strongest_team_budget_invalid:'+key)
    return result
