"""Single-account preset editor API, without account manager dependencies."""
import json
import sys
from core.harvest_presets import catalog, load_store, save_store, STORE, CATALOG

def request(value):
    if not isinstance(value, dict) or value.get('action') not in {'load', 'save'}:
        raise ValueError('harvest_editor_request_invalid')
    library = catalog(CATALOG)
    store = save_store(value['store'], STORE, library) if value['action'] == 'save' else load_store(STORE, library)
    return dict(ok=True, results=[dict(store=store, catalog=library)])

def main():
    try:
        result = request(json.loads(sys.stdin.read()))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = dict(ok=False, error=str(error))
    print(json.dumps(result, ensure_ascii=False))
if __name__ == '__main__':
    main()
