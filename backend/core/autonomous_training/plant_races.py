"""Same-row G1 cross-checks backed by the existing local race calendar."""
import json
import re
from functools import lru_cache
from pathlib import Path
VENUES = {'tokyo': '东京', 'hanshin': '阪神', 'nakayama': '中山', 'kyoto': '京都', 'chukyo': '中京', 'oi': '大井', 'kawasaki': '川崎', 'funabashi': '船桥', 'morioka': '盛冈'}
ALIASES = {'1003': ('大阪杯',), '1005': ('皋月奖',), '1007': ('广播协会', '英里杯'), '1008': ('维多利亚',), '1011': ('东京英里赛',), '1012': ('宝冢纪念', '宝家纪念'), '1014': ('秋华奖',), '1015': ('菊花奖',), '1016': ('天王奖秋', '天皇奖'), '1017': ('伊丽莎白女王杯', '伊丽莎白'), '1018': ('英里冠军赛', '冠军赛'), '1021': ('初级少女杯',), '1022': ('朝日杯',), '1024': ('希望锦标赛', '希望')}

@lru_cache(maxsize=1)
def g1_entries():
    path = Path(__file__).resolve().parents[2] / 'data/race_calendar/cn_standard_v1.json'
    return [entry for entry in json.loads(path.read_text(encoding='utf8'))['entries'] if entry['grade'] == 'G1']

def race_reference(date, aliases):
    matches = []
    for entry in g1_entries():
        when = entry['career_time']
        if not when or (when['year_index'] - 1, when['month'], int(when['half'] == 'second')) != date:
            continue
        names = (*ALIASES.get(Path(entry['apk_file_ref']).stem, ()), entry['names']['ja_jp'])
        if any((alias in names for alias in aliases)):
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None

def match_g1(date, aliases, tokens, image=None):
    """Use names first; permit unique same-date course evidence as a fallback."""
    reference = race_reference(date, aliases)
    if reference is None:
        return None
    if image is not None and reference['apk_file_ref'] == '1003.png':
        import cv2
        template = cv2.imread(str(Path(__file__).parent / 'assets/plan_osaka_name.png'))
        if template is not None:
            tokens = list(tokens)
            for index, token in enumerate(tokens):
                if token['text'].replace(' ', '') != '大阪杯' or not 0.5 <= token.get('confidence', 0) < 0.8:
                    continue
                x0, y0, x1, y1 = token['bbox']
                if not (20 <= x0 < x1 <= 280 and 230 <= y0 < y1 <= 1060):
                    continue
                crop = image[max(230, y0 - 8):min(1060, y1 + 8), max(20, x0 - 8):min(280, x1 + 8)]
                if crop.shape[0] < template.shape[0] or crop.shape[1] < template.shape[1]:
                    continue
                score = cv2.minMaxLoc(cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED))[1]
                if score >= 0.95:
                    tokens[index] = dict(token, confidence=score, source='osaka_banner_template')
    venue = VENUES[reference['venue']]
    surface = {'turf': '草地', 'dirt': '泥地'}[reference['surface']]
    prefix = f"{venue}{surface}{reference['distance_m']}米"
    direction = {'left': '逆', 'right': '顺'}.get(reference['direction'])
    matches = []
    for banner in tokens:
        x0, y0, x1, y1 = banner['bbox']
        text = banner['text'].replace(' ', '')
        confidence = banner.get('confidence', 0)
        if not (20 <= x0 < x1 <= 280 and 230 <= y0 < y1 <= 1060):
            continue
        if not any((alias in text if confidence >= 0.9 else alias == text and confidence >= 0.8 for alias in aliases)):
            continue
        headers = [t for t in tokens if 260 <= t['bbox'][0] and t['bbox'][2] <= 700 and (y0 - 100 <= t['bbox'][1] <= y0 + 25) and (t.get('confidence', 0) >= 0.9) and re.match('^(?:G[I1Ⅰ])?' + re.escape(prefix), t['text'].replace(' ', ''))]
        for header in headers:
            if direction and direction not in header['text']:
                continue
            grade_min = 0.75 if confidence >= 0.9 else 0.8
            if banner.get('source') == 'osaka_banner_template':
                grade_min = 0.9
            grades = [t for t in tokens if 260 <= t['bbox'][0] <= 340 and abs(t['bbox'][1] - header['bbox'][1]) <= 15 and (t.get('confidence', 0) >= grade_min) and re.match('^G(?:I|1|Ⅰ)(?![I1Ⅰ])(?:$|[^A-Za-z0-9Ⅰ])', t['text'].replace(' ', ''))]
            if grades:
                matches.append(banner)
                break
    if matches:
        return matches[0] if len(matches) == 1 else None
    same_course = [entry for entry in g1_entries() if entry['career_time'] == reference['career_time'] and all((entry[key] == reference[key] for key in ('venue', 'surface', 'distance_m')))]
    if len(same_course) != 1:
        return None
    candidates = []
    for header in tokens:
        x0, y0, x1, y1 = header['bbox']
        text = header['text'].replace(' ', '')
        if not (260 <= x0 < x1 <= 700 and 230 <= y0 <= 950 and (header.get('confidence', 0) >= 0.9) and re.match('^(?:G[I1Ⅰ])?' + re.escape(prefix), text) and (not direction or direction in text)):
            continue
        grades = [t for t in tokens if 260 <= t['bbox'][0] <= 340 and abs(t['bbox'][1] - y0) <= 15 and (t.get('confidence', 0) >= 0.9) and re.match('^G(?:I|1|Ⅰ)(?![I1Ⅰ])(?:$|[^A-Za-z0-9Ⅰ])', t['text'].replace(' ', ''))]
        if not grades:
            continue
        names = [t['text'].replace(' ', '') for t in tokens if t.get('confidence', 0) >= 0.9 and 20 <= t['bbox'][0] < t['bbox'][2] <= 280 and (y0 <= t['bbox'][1] <= y0 + 100) and (t['text'].replace(' ', '') not in ('设定中', '目标'))]
        if names and (not any((alias in name for alias in aliases for name in names))):
            continue
        candidates.append(dict(text='', confidence=min(header['confidence'], grades[0]['confidence']), bbox=[45, y0 + 15, 235, y0 + 70], source='unique_g1_metadata'))
    return candidates[0] if len(candidates) == 1 else None
