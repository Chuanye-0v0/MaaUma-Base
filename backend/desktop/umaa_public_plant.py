"""Public plant setup preferences; shared U1 owns execution semantics."""
import json
from pathlib import Path
import sys
from core.autonomous_training.plant_support import catalog, validate as validate_support, TYPE_LABELS, OPTION_KEYS
SETUP_KEYS = {'inheritance_selection', 'inheritance_borrow_friend'}
STORE = Path(__file__).resolve().parents[1] / 'runs/plant-support-settings.json'

def validate_settings(draft):
    if not isinstance(draft, dict) or set(draft) - (OPTION_KEYS | SETUP_KEYS):
        raise ValueError('support_options_invalid')
    setup = {key: draft[key] for key in SETUP_KEYS if key in draft}
    if 'inheritance_selection' in setup and setup['inheritance_selection'] not in ('keep', 'auto') or ('inheritance_borrow_friend' in setup and type(setup['inheritance_borrow_friend']) is not bool):
        raise ValueError('plant_setup_options_invalid')
    support = validate_support({key: value for key, value in draft.items() if key not in SETUP_KEYS})
    return {**support, **setup}

def request(value):
    if not isinstance(value, dict) or value.get('action') not in {'load', 'save'}:
        raise ValueError('plant_editor_request_invalid')
    stored = json.loads(STORE.read_text(encoding='utf8')) if STORE.exists() else {}
    draft = value.get('settings', {}) if value['action'] == 'save' else stored
    chosen = validate_settings(draft)
    if value['action'] == 'save':
        STORE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STORE.with_suffix('.tmp')
        temporary.write_text(json.dumps(chosen, ensure_ascii=False), encoding='utf8')
        temporary.replace(STORE)
    cards = [dict(id=key, label=f"{row['rarity']} {TYPE_LABELS[row['type']]} · {row['name_cn']} · {row.get('title_cn', '')} [{key}]") for key, row in catalog().items() if row['type'] in TYPE_LABELS]
    return dict(ok=True, results=[dict(settings=chosen, cards=cards)])

def main():
    try:
        result = request(json.loads(sys.stdin.read()))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = dict(ok=False, error=str(error))
    print(json.dumps(result, ensure_ascii=False))
if __name__ == '__main__':
    main()
