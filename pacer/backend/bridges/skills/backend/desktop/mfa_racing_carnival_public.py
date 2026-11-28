"""Current-login public scope for Racing Carnival; no SDK account book, login or trainer-ID read."""
import hashlib
import json
import os
from pathlib import Path


def public_scope(output, endpoint, *, day=None):
    from desktop.umaa_public_context import backend_root, normalize_endpoint, endpoint as selected_endpoint
    from core.racing_carnival.journal import game_day, prior_day_guard
    root = backend_root().resolve()
    output = Path(output).resolve()
    relative = output.relative_to(root / 'runs/public-tasks')
    if len(relative.parts) != 2 or relative.parts[-1] != 'racing_carnival':
        raise ValueError('racing_carnival_public_output_scope_invalid')
    canonical = normalize_endpoint(endpoint)
    if canonical != normalize_endpoint(selected_endpoint()):
        raise ValueError('racing_carnival_public_selected_device_mismatch')
    settings = root.parent / 'appsettings.json'
    config = json.loads(settings.read_text(encoding='utf-8-sig')) if settings.is_file() else {}
    profile = config.get('DefaultConfig', 'Default')
    if not isinstance(profile, str) or not profile.strip():
        profile = 'Default'
    profile = profile.strip().casefold()
    installation = os.path.normcase(str(root.parent.resolve()))
    identity = json.dumps([installation, canonical, profile], ensure_ascii=False, separators=(',', ':'))
    key = 'current-' + hashlib.sha256(identity.encode('utf8')).hexdigest()[:24]
    scope = dict(
        account=key,
        day=day or game_day(),
        event='racing_carnival',
        namespace='public_current',
        installation_root=installation,
        configuration=profile,
    )
    ledger = root / 'runs/module-ledgers/racing_carnival' / key / (scope['day'] + '.json')
    prior_day_guard(ledger)
    return ledger, scope


def execution_scope(output, endpoint, *, day=None):
    path = Path(output).resolve()
    if 'mfa-batches' in path.parts:
        from core.racing_carnival.journal import batch_scope
        if endpoint != '127.0.0.1:16416':
            raise ValueError('racing_carnival_device_not_allowed')
        return batch_scope(path, day=day)
    return public_scope(path, endpoint, day=day)
