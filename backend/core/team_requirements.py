"""Editable team requirements from explicit words, without guessing an event."""
from copy import deepcopy
import re
import unicodedata
SCHEMA = 'uma.team_requirements.v1'
FIELD_ALIASES = {
    'event_mode': {'大赛': 'champions_meeting', '群英联赛': 'champions_meeting',
                   '冠军集会': 'champions_meeting', '冠军赛': 'champions_meeting',
                   'loh': 'loh', '英杰集结战': 'loh'},
    'direction': {'顺时针': 'right', '右回': 'right', '右转': 'right', '右绕': 'right',
                  '逆时针': 'left', '左回': 'left', '左转': 'left', '左绕': 'left',
                  '直线': 'straight', '直线赛道': 'straight'},
}
STYLES = ('nige', 'senko', 'sashi', 'oikomi')
VENUES = {'东京': 'tokyo', '中山': 'nakayama', '京都': 'kyoto', '阪神': 'hanshin', '中京': 'chukyo', '札幌': 'sapporo', '函馆': 'hakodate', '福岛': 'fukushima', '新潟': 'niigata', '小仓': 'kokura', '大井': 'oi', '川崎': 'kawasaki', '船桥': 'funabashi', '盛冈': 'morioka', '隆尚': 'longchamp'}
ENUMS = {'server': {'cn', 'jp', 'tw'}, 'event_mode': {'champions_meeting', 'loh', 'team_arena', 'custom'}, 'venue': set(VENUES.values()), 'surface': {'turf', 'dirt'}, 'distance_category': {'sprint', 'mile', 'middle', 'long'}, 'direction': {'left', 'right', 'straight'}, 'season': {'spring', 'summer', 'autumn', 'winter'}, 'weather': {'sunny', 'cloudy', 'rainy', 'snowy'}, 'track_condition': {'good', 'yielding', 'soft', 'heavy'}}
STYLE_PATTERNS = {'nige': '领跑|逃跑|大逃|逃(?=$|[马逃先差追、，,和与/＋+\\s\\d一二两三四五六七八九])', 'senko': '跟前|先行|先(?=$|[马逃先差追、，,和与/＋+\\s\\d一二两三四五六七八九])', 'sashi': '居中|差行|差(?=$|[马逃先差追、，,和与/＋+\\s\\d一二两三四五六七八九])', 'oikomi': '后追|追马|追(?=$|[马逃先差追、，,和与/＋+\\s\\d一二两三四五六七八九])'}
_NUMBERS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
_NUMBER = '(?:\\d{1,2}|[一二两三四五六七八九十])'

def _number(text):
    return int(text) if text.isdigit() else _NUMBERS[text]

def empty_requirements():
    return {'schema_version': SCHEMA, **{key: None for key in ENUMS}, 'event_name': None, 'edition': {'year': None, 'month': None, 'index': None}, 'distance_m': None, 'team_size': None, 'styles': {'allowed': None, 'preferred': [], 'excluded': [], 'counts': {}}}

def normalize_requirements(value):
    if not isinstance(value, dict) or set(value) - set(empty_requirements()):
        raise ValueError('团队需求字段不正确。')
    result = empty_requirements()
    if value.get('schema_version', SCHEMA) != SCHEMA:
        raise ValueError('团队需求格式版本不正确。')
    for key, choices in ENUMS.items():
        item = value.get(key)
        if key == 'server':
            item = 'cn' if item is None else item
            if item != 'cn':
                raise ValueError('当前种马规划仅支持国服；其他服攻略可作为待核对参考链接。')
        if isinstance(item, str):
            item = FIELD_ALIASES.get(key, {}).get(item.casefold(), item)
        if item is not None and (not isinstance(item, str) or item not in choices):
            raise ValueError('团队需求中的服务器、赛况或跑法类型无效。')
        result[key] = item
    event = value.get('event_name')
    if event is not None:
        if not isinstance(event, str) or not event.strip() or len(event) > 100:
            raise ValueError('赛事名称请填写不超过100字的简称。')
        result['event_name'] = event.strip()
    edition = value.get('edition', {})
    if not isinstance(edition, dict) or set(edition) - {'year', 'month', 'index'}:
        raise ValueError('赛事期次格式无效。')
    for key, lower, upper in (('year', 2000, 2100), ('month', 1, 12), ('index', 1, 99)):
        item = edition.get(key)
        if item is not None and (type(item) is not int or not lower <= item <= upper):
            raise ValueError('赛事年、月或届次超出有效范围。')
        result['edition'][key] = item
    for key, lower, upper in (('team_size', 1, 9), ('distance_m', 600, 5000)):
        item = value.get(key)
        if item is not None and (type(item) is not int or not lower <= item <= upper):
            raise ValueError('团队人数应为1～9，距离请填写明确的米数。')
        result[key] = item
    styles = value.get('styles', {})
    if not isinstance(styles, dict) or set(styles) - {'allowed', 'preferred', 'excluded', 'counts'}:
        raise ValueError('跑法条件字段无效。')
    for key in ('allowed', 'preferred', 'excluded'):
        items = styles.get(key, None if key == 'allowed' else [])
        if items is None and key == 'allowed':
            result['styles'][key] = None
        elif isinstance(items, list) and all((isinstance(item, str) and item in STYLES for item in items)):
            result['styles'][key] = list(dict.fromkeys(items))
        else:
            raise ValueError('跑法请选择领跑、跟前、居中或后追。')
    counts = styles.get('counts', {})
    if isinstance(counts, dict):
        counts = {key: n for key, n in counts.items() if n is not None and n != ''}
    if not isinstance(counts, dict) or set(counts) - set(STYLES) or any((type(n) is not int or not 0 <= n <= 9 for n in counts.values())):
        raise ValueError('各跑法人数应为0～9；留空由AI推荐，0表示不安排该跑法。')
    result['styles']['counts'] = deepcopy(counts)
    if result['team_size'] is None and result['event_mode'] in {'champions_meeting', 'loh'}:
        result['team_size'] = 3
    elif result['team_size'] is None and 0 < sum(counts.values()) <= 9:
        result['team_size'] = sum(counts.values())
    return result

def assess_requirements(value):
    result = normalize_requirements(value)
    missing, questions, conflicts = ([], [], [])
    if result['team_size'] is None:
        missing.append('team_size')
        questions.append('这次准备几匹战马（1～9匹）？')
    style = result['styles']
    counts = style['counts']
    total = sum(counts.values())
    if total > 9:
        conflicts.append('明确跑法分配合计超过9匹，请调整配比。')
    if result['team_size'] is not None and total > result['team_size']:
        conflicts.append('总人数为' + str(result['team_size']) + '，但明确跑法分配合计' + str(total) + '，请确认人数或配比。')
    excluded = set(style['excluded'])
    if any((key in excluded for key, n in counts.items() if n > 0)) or set(style['preferred']) & excluded:
        conflicts.append('跑法偏好或明确配比与排除条件冲突。')
    if style['allowed'] is not None:
        if not set(style['allowed']) - excluded or any((key not in style['allowed'] for key, n in counts.items() if n > 0)):
            conflicts.append('限定跑法与排除条件或明确配比冲突。')
    if set(counts) == set(STYLES) and result['team_size'] is not None and total != result['team_size']:
        conflicts.append('四种跑法人数合计与团队总人数不一致；请调整配比，或清空配比交给AI推荐。')
    unknown = [key for key in ('event_mode', 'venue', 'surface', 'distance_m', 'direction', 'season', 'weather', 'track_condition') if result[key] is None]
    context = any(result[key] is not None for key in ('event_name', 'event_mode', 'venue', 'surface', 'distance_m',
        'distance_category', 'direction', 'season', 'weather', 'track_condition', 'team_size')) or any(result['edition'].values()) or bool(counts or style['allowed'] or style['preferred'] or style['excluded'])
    return {'requirements': result, 'missing_fields': missing, 'clarification_questions': questions,
        'conflicts': conflicts, 'unknown_optional_fields': unknown,
        'ready_for_retrieval': bool(context), 'ready_for_planning': not missing and not conflicts,
        'notes': (['未填写的赛道、期次和赛况保留未知；可先检索，再给出标明条件的参考方案，不自动套用本期或日/台服赛期。'] if unknown else [])
            + (['跑法人数未限定，由AI结合资料推荐。'] if not counts and style['allowed'] is None else [])}

def parse_requirements(text, overrides=None, default_server='cn'):
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError('需求描述最多4000字。')
    text = unicodedata.normalize('NFKC', text)
    result = empty_requirements()
    assumptions, parse_conflicts = ([], [])
    if default_server not in (None, 'cn'):
        raise ValueError('当前种马规划仅支持国服。')
    result['server'] = 'cn'
    assumptions.append('当前按国服规划；不会自动使用日服或台服赛期。')
    if re.search('日服|日本服|台服|繁中服', text) and not re.search('国服|简中服', text):
        parse_conflicts.append('当前仅支持国服规划；描述提及其他服务器，请确认适用规则。')
    mode_patterns = [('champions_meeting', '冠军(?:集会|赛)|大赛|杯赛|群英联赛'), ('loh', '(?i)LOH|英杰集结战'), ('team_arena', '竞技场|团队竞赛')]
    modes = [key for key, pattern in mode_patterns if re.search(pattern, text)]
    if len(modes) == 1:
        result['event_mode'] = modes[0]
    elif len(modes) > 1:
        parse_conflicts.append('描述包含多种比赛模式，请确认本次模式。')
    event = re.search('(?:白羊|金牛|双子|巨蟹|狮子|处女|天秤|天蝎|射手|摩羯|水瓶|双鱼)(?:座)?杯|日本杯|有马纪念|菊花赏|樱花赏|皋月赏|秋华赏|天皇赏', text)
    if event:
        result['event_name'] = event.group()
        if result['event_name'].endswith('杯') and re.search('白羊|金牛|双子|巨蟹|狮子|处女|天秤|天蝎|射手|摩羯|水瓶|双鱼', result['event_name']):
            result['event_mode'] = result['event_mode'] or 'champions_meeting'
    elif result['event_mode'] == 'loh':
        result['event_name'] = 'LOH'
    year = re.search('(?<!\\d)(20\\d{2})(?:年|[-/.])', text)
    month = re.search('(?<!\\d)(1[0-2]|0?[1-9])月', text)
    year_month = re.search('(?<!\\d)(20\\d{2})[-/.](1[0-2]|0?[1-9])(?:\\D|$)', text)
    if year:
        result['edition']['year'] = int(year.group(1))
    if month:
        result['edition']['month'] = int(month.group(1))
    if year_month:
        result['edition'].update(year=int(year_month.group(1)), month=int(year_month.group(2)))
    index = re.search('第(' + _NUMBER + ')(?:届|期)', text)
    if index:
        result['edition']['index'] = _number(index.group(1))
    sets = {'venue': [(word, value) for word, value in VENUES.items()], 'surface': [('草地', 'turf'), ('草场', 'turf'), ('泥地', 'dirt'), ('泥场', 'dirt')], 'distance_category': [('短距离', 'sprint'), ('英里', 'mile'), ('中距离', 'middle'), ('长距离', 'long')], 'direction': [('左回', 'left'), ('左转', 'left'), ('左绕', 'left'), ('逆时针', 'left'), ('右回', 'right'), ('右转', 'right'), ('右绕', 'right'), ('顺时针', 'right'), ('直线赛道', 'straight')], 'season': [('春季', 'spring'), ('春天', 'spring'), ('夏季', 'summer'), ('夏天', 'summer'), ('秋季', 'autumn'), ('秋天', 'autumn'), ('冬季', 'winter'), ('冬天', 'winter')], 'weather': [('晴天', 'sunny'), ('晴朗', 'sunny'), ('阴天', 'cloudy'), ('多云', 'cloudy'), ('雨天', 'rainy'), ('下雨', 'rainy'), ('雪天', 'snowy'), ('下雪', 'snowy')], 'track_condition': [('良好', 'good'), ('良场', 'good'), ('稍重', 'yielding'), ('不良', 'heavy')]}
    for key, tokens in sets.items():
        values = {value for word, value in tokens if word in text}
        if len(values) == 1:
            result[key] = next(iter(values))
        elif len(values) > 1:
            parse_conflicts.append('描述包含多个' + key + '条件，请在需求卡选择。')
    for key, label, tokens in (('season', '季节', [('春', 'spring'), ('夏', 'summer'), ('秋', 'autumn'), ('冬', 'winter')]), ('weather', '天气', [('晴', 'sunny'), ('阴', 'cloudy'), ('雨', 'rainy'), ('雪', 'snowy')]), ('direction', '方向', [('左', 'left'), ('右', 'right'), ('直线', 'straight')])):
        values = {value for word, value in tokens if re.search('(?<![\\w\\u4e00-\\u9fff])' + word + '(?![\\w\\u4e00-\\u9fff])|' + label + '\\s*[:：]?\\s*' + word, text)}
        if len(values) == 1 and result[key] is None:
            result[key] = next(iter(values))
        elif len(values) > 1:
            parse_conflicts.append('描述包含多个' + key + '条件，请在需求卡选择。')
    if result['track_condition'] is None:
        if re.search('(?<!稍)(?<!不)重(?:场|马场|地|\\s|$)', text):
            result['track_condition'] = 'soft'
        elif re.search('(?<!不)良(?:马场|地|\\s|$)', text):
            result['track_condition'] = 'good'
    if result['direction'] is None and re.search(r'直线(?:赛道|跑道|[，,。；;\s]|$)', text):
        result['direction'] = 'straight'
    distances = {int(match.group(1)) for match in re.finditer('(?<!\\d)(\\d{3,4})\\s*(?:米|[mM](?![a-zA-Z]))', text)}
    distances.update((int(match.group(1)) for match in re.finditer('(?:草地|泥地|距离|跑道)\\s*(\\d{3,4})(?!\\d)', text)))
    distances.update((int(match.group(1)) for match in re.finditer('(?:' + '|'.join(VENUES) + ')\\s*(\\d{3,4})(?=$|[\\s，,;；。mM米])', text)))
    if len(distances) == 1:
        distance = next(iter(distances))
        if 600 <= distance <= 5000:
            result['distance_m'] = distance
        else:
            parse_conflicts.append('距离超出支持范围，请确认明确米数。')
    elif distances:
        parse_conflicts.append('描述包含多个距离，请确认这次赛道。')
    sizes = {_number(match.group(1)) for match in re.finditer('(?<!\\d)(' + _NUMBER + ')\\s*(?:匹|只|人)(?![A-Za-z])', text)}
    if len(sizes) == 1:
        size = next(iter(sizes))
        if 1 <= size <= 9:
            result['team_size'] = size
        else:
            parse_conflicts.append('人数应为1～9匹，请调整。')
    elif sizes:
        parse_conflicts.append('描述包含多个人数，请确认总共几匹。')
    for clause in re.split('[，,。；;\\n]', text):
        found = {key for key, pattern in STYLE_PATTERNS.items() if re.search(pattern, clause)}
        excluded = {key for key, pattern in STYLE_PATTERNS.items() if re.search('(?:不(?:要|用|选)?|排除)\\s*(?:' + pattern + ')', clause)}
        result['styles']['excluded'].extend((key for key in STYLES if key in excluded))
        active = found - excluded
        preferred = bool(re.search('偏|优先|倾向|最好', clause))
        if re.search('只要|只用|只选|仅用|仅选|限定|全(?:队|员|部|是)|纯.+队', clause):
            result['styles']['allowed'] = [key for key in STYLES if key in active]
        elif preferred:
            result['styles']['preferred'].extend((key for key in STYLES if key in active))
        if not preferred:
            for key, pattern in STYLE_PATTERNS.items():
                for match in re.finditer('(' + _NUMBER + ')\\s*(?:(?:匹|只|个)\\s*)?(?:' + pattern + ')', clause):
                    n = _number(match.group(1))
                    if 0 <= n <= 9:
                        result['styles']['counts'][key] = result['styles']['counts'].get(key, 0) + n
                    else:
                        parse_conflicts.append('单项跑法人数应为0～9；留空由AI推荐。')
    if result['team_size'] is None and result['styles']['counts'] and (0 < sum(result['styles']['counts'].values()) <= 9):
        result['team_size'] = sum(result['styles']['counts'].values())
        assumptions.append('总人数按明确跑法分配求和。')
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError('可编辑需求应为对象。')
        for key, value in deepcopy(overrides).items():
            if key in {'edition', 'styles'} and isinstance(value, dict):
                result[key] = {**result[key], **value}
            else:
                result[key] = value
        issue_words = {'server': '服务器', 'event_mode': '比赛模式', 'team_size': '人数', 'distance_m': '距离', 'styles': '跑法', 'venue': 'venue', 'surface': 'surface', 'distance_category': 'distance_category', 'direction': 'direction', 'season': 'season', 'weather': 'weather', 'track_condition': 'track_condition'}
        parse_conflicts = [message for message in parse_conflicts if not any((word in message for field, word in issue_words.items() if field in overrides))]
        if 'server' in overrides:
            assumptions = [text for text in assumptions if '默认服务器' not in text and '默认国服' not in text]
    normalized = normalize_requirements(result)
    if normalized['distance_m'] is not None:
        distance = normalized['distance_m']
        category = 'sprint' if distance <= 1400 else 'mile' if distance <= 1800 else 'middle' if distance <= 2400 else 'long'
        if normalized['distance_category'] is None:
            normalized['distance_category'] = category
        elif normalized['distance_category'] != category:
            parse_conflicts.append('明确距离米数与距离类别不一致，请确认。')
    if all((normalized[key] is not None for key in ('venue', 'surface', 'distance_m'))) and normalized['event_mode'] is None:
        normalized['event_mode'] = 'custom'
    report = assess_requirements(normalized)
    report['conflicts'] = list(dict.fromkeys([*parse_conflicts, *report['conflicts']]))
    report['ready_for_retrieval'] = report['ready_for_retrieval'] or bool(text.strip())
    report['ready_for_planning'] = report['ready_for_planning'] and not report['conflicts']
    report['request_text'] = text.strip()
    report['assumptions'] = assumptions
    return report
