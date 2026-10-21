"""Source-labelled guide evidence and local facts, never game instructions."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit
from core.team_requirements import normalize_requirements, VENUES
ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'data/parent_library/team_guide_sources.json'
MAX_SNIPPET = 480

def load_sources(path=None):
    value = json.loads(Path(path or SOURCES).read_text('utf-8-sig'))
    if value.get('schema_version') != 'uma.team_guide_sources.v1' or not isinstance(value.get('sources'), list):
        raise ValueError('攻略来源目录格式无效。')
    return value['sources']

def source_profile(url, registry=None):
    parsed = urlsplit(url)
    for row in registry if registry is not None else load_sources():
        if parsed.hostname in row['hosts'] and (not row.get('path_prefixes') or any((parsed.path.startswith(prefix) for prefix in row['path_prefixes']))) and all((dict(parse_qsl(parsed.query)).get(key) == value for key, value in row.get('query_equals', {}).items())):
            return dict(row)
    return {'source_key': 'user-public-source', 'locale': 'unknown', 'server': 'unknown', 'priority': 0}

def _clean(value, limit):
    return re.sub('\\s+', ' ', str(value or '')).strip()[:limit]

def source_id(kind, url):
    return kind + '-' + hashlib.sha256(url.encode('utf8')).hexdigest()[:20]

def article_evidence(url, title, text, requirements, *, published=None, version=None, registry=None, retrieved_at=None):
    req = normalize_requirements(requirements)
    profile = source_profile(url, registry)
    title, text = (_clean(title, 180), _clean(text, 30000))
    content = title + ' ' + text
    locale, server = (profile['locale'], profile['server'])
    explicit = {code for pattern, code in (('国服|简中|闪耀优俊少女', 'cn'), ('日服|日本服', 'jp'), ('台服|繁中服', 'tw')) if re.search(pattern, content)}
    if len(explicit) == 1:
        server = next(iter(explicit))
    elif len(explicit) > 1:
        server = 'unknown'
    date = None
    if isinstance(published, str) and re.match('^\\d{4}-\\d{2}-\\d{2}', published):
        try:
            date = datetime.fromisoformat(published[:10]).date().isoformat()
        except ValueError:
            pass
    if version is None:
        match = re.search('(?i)(?:版本|version|\\bv)\\s*([1-9]\\.\\d+(?:\\.\\d+)?)', content)
        version = match.group(1) if match else None
    version = _clean(version, 40) or None
    status, reasons = ('candidate', [])
    if req['server'] is not None and server not in (req['server'], 'unknown'):
        status = 'reference_only'
        reasons.append('其他服务器资料，仅可参考，不直接套用本服。')
    elif req['server'] == 'cn' and profile['server'] in {'jp', 'tw'}:
        status = 'reference_only'
        reasons.append('来源按日服/台服参考登记，本期国服适用性仍需人工核对。')
    elif server == 'unknown':
        status = 'needs_review'
        reasons.append('文章适用服务器未明确。')
    if profile['priority'] == 0:
        if status == 'candidate':
            status = 'needs_review'
        reasons.append('该来源未在优先资料目录登记，需要人工核对内容与适用游戏。')
    if version is None:
        if status == 'candidate':
            status = 'needs_review'
        reasons.append('规则版本未明确，不能描述为当前国服已验证攻略。')
    if date is None:
        if status == 'candidate':
            status = 'needs_review'
        reasons.append('发布时间未取得。')
    edition = req['edition']
    target_period = (edition['year'], edition['month'])
    source_period = re.search('(?<!\\d)(20\\d{2})\\s*(?:年|[-/.])\\s*(1[0-2]|0?[1-9])\\s*(?:月|\\b)', title)
    if None not in target_period:
        if source_period and tuple(map(int, source_period.groups())) != target_period:
            status = 'out_of_scope'
            reasons.append('文章明确赛事期次与本次目标不同。')
        elif date and int(date[:4]) < edition['year']:
            if status != 'out_of_scope':
                status = 'reference_only'
            reasons.append('资料早于目标赛事年份，不能当作本期攻略。')
        elif date and date[:7] != f"{edition['year']:04}-{edition['month']:02}" and (not source_period):
            if status == 'candidate':
                status = 'needs_review'
            reasons.append('文章日期不等于目标期次，需核对规则适用时间。')
    elif edition['year'] is not None and date and (int(date[:4]) < edition['year']):
        if status != 'out_of_scope':
            status = 'reference_only'
        reasons.append('资料早于目标赛事年份，不能当作本期攻略。')
    zodiac = '(?:白羊|金牛|双子|巨蟹|狮子|处女|天秤|天蝎|射手|摩羯|水瓶|双鱼)(?:座)?杯'
    requested = re.search(zodiac, req['event_name'] or '')
    mentioned = re.findall(zodiac, title)
    if requested and mentioned and (requested.group().replace('座', '') not in {name.replace('座', '') for name in mentioned}):
        status = 'out_of_scope'
        reasons.append('标题对应另一项杯赛。')
    if profile.get('metadata_only'):
        status = 'reference_only' if status != 'out_of_scope' else status
        reasons.append('仅取得视频页面公开标题和简介，未读取视频画面或字幕。')
    return {'source_id': source_id('guide', url), 'kind': 'guide', 'url': url, 'title': title or '公开攻略页面', 'date': date, 'locale': locale, 'server': server, 'version': version, 'source_key': profile['source_key'], 'priority': profile['priority'], 'applicability': {'status': status, 'reasons': reasons or ['按本次服务器、期次与版本条件保留为候选。']}, 'snippet': _clean(text or title, MAX_SNIPPET), 'trust': 'untrusted_public_text', 'retrieved_at': retrieved_at or datetime.now(timezone.utc).isoformat(timespec='seconds')}

def local_facts(requirements, *, race_catalog=None, skill_reference=None):
    """A matching career-calendar entry is not a current cup announcement."""
    from core.parent_race_records import RaceCatalog
    req = normalize_requirements(requirements)
    races = race_catalog or RaceCatalog.load()
    facts = []
    filters = ('venue', 'surface', 'distance_m', 'distance_category', 'direction')
    specified = any((req[key] is not None for key in filters))
    matches = [row for row in races.by_id.values() if specified and all((req[key] is None or row.get(key) == req[key] for key in filters))]
    for row in matches[:5]:
        label = next((name for name, code in VENUES.items() if code == row.get('venue')), row.get('venue', ''))
        identifier = 'local-race:' + row['race_id']
        facts.append({'source_id': source_id('fact', identifier), 'kind': 'local_fact', 'fact_type': 'race_calendar', 'url': None, 'title': '本地赛历事实 · 非赛事公告', 'date': None, 'data_generated_at': races.source.get('generated_at'), 'locale': 'unknown', 'server': 'unknown', 'version': races.source.get('calendar_id'), 'applicability': {'status': 'reference_only', 'reasons': ['生涯赛历窗口符合所填条件，不证明本期杯赛/LOH也使用该赛道。']}, 'snippet': _clean(f"{label} · {row.get('surface')} {row.get('distance_m')}m · {row.get('grade')} · 原文 {row.get('source_name')}", MAX_SNIPPET), 'data': {key: row.get(key) for key in ('race_id', 'event_key', 'venue', 'surface', 'distance_m', 'distance_category', 'direction', 'career_time')}, 'trust': 'local_reference'})
    if skill_reference is not None:
        query = '泥地' if req['surface'] == 'dirt' else '草地' if req['surface'] == 'turf' else None
        if query:
            for skill in skill_reference.search(query, limit=3)['skills']:
                detail = skill_reference.detail(skill['id'])
                item = detail['skill']
                facts.append({'source_id': 'fact-skill-' + str(skill['id']), 'kind': 'local_fact', 'fact_type': 'skill_reference', 'url': item.get('source_url') or None, 'title': '本地技能资料 · ' + skill['name'], 'date': None, 'locale': 'zh-CN', 'server': 'cn', 'version': detail.get('data_version'), 'applicability': {'status': 'reference_only', 'reasons': ['技能条目与来源目录是静态资料，不是赛事攻略、账号持有或发动保证。']}, 'snippet': _clean(item.get('description') or item.get('trigger'), MAX_SNIPPET), 'trust': 'local_reference'})
    return facts
