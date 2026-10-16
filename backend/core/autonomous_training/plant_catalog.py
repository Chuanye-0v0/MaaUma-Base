"""Pre-UAF trainee choices with stable card and costume identities."""
import json
from pathlib import Path
TRAINEES = {'scarlet': '大和赤骥', 'bakushin': '樱花进王', 'urara': '乌拉拉', 'gold_ship': '黄金船', 'vodka': '伏特加'}
ROSTER = json.loads(Path(__file__).with_name('plant_roster.json').read_text(encoding='utf8'))
PREVIEW_VARIANTS = {r['id']: (r['name'], r['outfit']) for r in ROSTER['records'] if not r['initial_key']}
VARIANTS = {r['id']: r for r in ROSTER['records']}
INITIAL_VARIANTS = {r['initial_key']: r['id'] for r in ROSTER['records'] if r['initial_key']}

def resolve_trainee_key(value):
    """Resolve explicit game IDs without reinterpreting saved panel card IDs.

    The six-digit namespaces overlap: card 100102 is not costume 100102.
    Legacy strings remain card keys/initial aliases. Game costume IDs must be
    named explicitly, for example {'character_id': 1001, 'costume_id': 100130}.
    """
    if isinstance(value, str):
        if value in TRAINEES or value in PREVIEW_VARIANTS:
            return value
        if value in VARIANTS:
            return VARIANTS[value].get('initial_key') or value
        raise ValueError('mfa_plant_trainee_invalid')
    if not isinstance(value, dict) or not value or set(value) - {'card_id', 'character_id', 'costume_id'}:
        raise ValueError('mfa_plant_trainee_id_namespace_required')

    def number(field, width):
        item = value.get(field)
        if type(item) is int:
            item = str(item)
        if not isinstance(item, str) or len(item) != width or (not item.isascii()) or (not item.isdecimal()):
            raise ValueError('mfa_plant_trainee_identity_invalid')
        return int(item)
    character = number('character_id', 4) if 'character_id' in value else None
    costume = number('costume_id', 6) if 'costume_id' in value else None
    if 'card_id' in value:
        card = str(number('card_id', 6))
        record = VARIANTS.get(card)
        if record is None:
            raise ValueError('mfa_plant_trainee_invalid')
        if character is not None and record['character_id'] != character or (costume is not None and record['costume_id'] != costume):
            raise ValueError('mfa_plant_trainee_identity_mismatch')
    else:
        if costume is None:
            raise ValueError('mfa_plant_trainee_costume_required')
        matches = [r for r in VARIANTS.values() if r['costume_id'] == costume and (character is None or r['character_id'] == character)]
        if len(matches) != 1:
            raise ValueError('mfa_plant_trainee_identity_ambiguous' if matches else 'mfa_plant_trainee_invalid')
        record = matches[0]
    return record.get('initial_key') or record['id']

def trainee_identity(value):
    key = resolve_trainee_key(value)
    record = VARIANTS[INITIAL_VARIANTS.get(key, key)]
    return dict(trainee=key, card_id=record['id'], character_id=record['character_id'], costume_id=record['costume_id'], name=record['name'], outfit=record['outfit'])

def trainee_name(key):
    key = resolve_trainee_key(key)
    if key in PREVIEW_VARIANTS:
        return PREVIEW_VARIANTS[key][0]
    return TRAINEES[key]
