"""Pre-UAF trainee choices with stable card and costume identities."""
import json
from pathlib import Path
TRAINEES = {'scarlet': '大和赤骥', 'bakushin': '樱花进王', 'urara': '春乌菈菈', 'gold_ship': '黄金船', 'vodka': '伏特加'}
ROSTER = json.loads(Path(__file__).with_name('plant_roster.json').read_text(encoding='utf8'))
PREVIEW_VARIANTS = {r['id']: (r['name'], r['outfit']) for r in ROSTER['records'] if not r['initial_key']}
VARIANTS = {r['id']: r for r in ROSTER['records']}
INITIAL_VARIANTS = {r['initial_key']: r['id'] for r in ROSTER['records'] if r['initial_key']}

def trainee_name(key):
    if not isinstance(key, str) or (key not in TRAINEES and key not in PREVIEW_VARIANTS):
        raise ValueError('mfa_plant_trainee_invalid')
    if key in PREVIEW_VARIANTS:
        return PREVIEW_VARIANTS[key][0]
    return TRAINEES[key]
