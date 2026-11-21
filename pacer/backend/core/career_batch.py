"""Explicit ordinary-career batch options; old documents still mean one career."""


MAX_CAREERS = 1000


def normalize(value=None):
    if value is None:
        return {'max_careers': 1}
    if (not isinstance(value, dict) or set(value) - {'max_careers'}
            or type(value.get('max_careers', 1)) is not int
            or not 1 <= value.get('max_careers', 1) <= MAX_CAREERS):
        raise ValueError(f'本轮育成次数请填1～{MAX_CAREERS}的整数。')
    return {'max_careers': value.get('max_careers', 1)}


def validate_document(document):
    config = normalize(document.get('career_batch'))
    if config['max_careers'] == 1:
        return config
    nodes = document['run_graph']['nodes']
    if any(node['kind'] in {'cut', 'restart'} or 'ss_sieve' in node or 'ace_cycle' in node for node in nodes):
        raise ValueError('连续育成次数用于普通自动育成；刷切和SS筛局使用各自的次数预算。')
    by_id = {node['id']: node for node in nodes}
    kinds = sorted(node['kind'] for node in nodes)
    if kinds != ['career', 'entry', 'finish']:
        raise ValueError('连续多局请使用“开始→自动育成→结束”流程；带检查或保留分支的工作流仍按单局执行。')
    first = by_id[document['run_graph']['entry']]
    while first['kind'] == 'entry':
        target = next(edge['to'] for edge in document['run_graph']['edges']
                      if edge['from'] == first['id'] and edge['outcome'] == 'next')
        first = by_id[target]
    options = document['policies'][first.get('policy', 'default')].get('career_options', {})
    from scripts.larc_ordinary_setup import parse_ordinary_setup
    setup = parse_ordinary_setup(options.get('setup'))
    if setup['character_id'] in {'current', 'validation'}:
        raise ValueError('连续育成请先选择具体马娘；沿用当前育成仍可单局接管。')
    return config
