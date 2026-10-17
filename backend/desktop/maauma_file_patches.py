"""File-patch inspection and sparse incoming plans; no authorization policy."""
import hashlib
import json
from pathlib import Path
import re
import shutil
from zipfile import ZipFile, BadZipFile

from desktop import product_updates as updates
from desktop import maauma_service as service

MANIFEST = 'maauma-file-patch.json'
IDENTITY = ('component_id', 'product_id', 'from_version', 'version', 'base_version',
            'channel', 'target_archive_sha256', 'from_manifest_sha256', 'target_manifest_sha256')
PREFERENCE = 'updates/file-patch-fallback.json'


class PatchUnusable(updates.UpdateError):
    pass


class LocalMismatch(updates.UpdateError):
    pass


def _hash(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream, 'sha256').hexdigest()


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _edge(kind, source, target):
    return ':'.join((kind, source, target['version'], target.get('base_version', target['version'])))


def prefer_full(root, kind, source, target):
    path = updates.contained(root, PREFERENCE)
    value = updates.read_json(path) if path.is_file() else {}
    return value.get(_edge(kind, source, target)) == 'local_state_mismatch'


def mark_full(root, kind, source, target):
    path = updates.contained(root, PREFERENCE)
    value = updates.read_json(path) if path.is_file() else {}
    value[_edge(kind, source, target)] = 'local_state_mismatch'
    updates.write_json(path, value)


def descriptor(value, full, *, kind, source, channel):
    if not isinstance(value, dict):raise PatchUnusable('patch_descriptor_invalid')
    component = 'base' if kind == 'base' else 'pacer'
    product = 'umaa-public' if kind == 'base' else 'uma-cut'
    if (value.get('schema_version') != 'maauma.file_patch.v1' or value.get('transfer') != 'file_patch' or
            not re.fullmatch(r'fp_[0-9a-f]{32}', str(value.get('patch_id', ''))) or
            value.get('component_id') != component or value.get('product_id') != product or
            value.get('from_version') != source or value.get('version') != full['version'] or
            value.get('base_version') != full.get('base_version', full['version']) or
            value.get('channel') != channel or value.get('target_archive_sha256') != full['sha256']):
        raise PatchUnusable('patch_descriptor_invalid')
    for key in ('sha256', 'target_archive_sha256', 'from_manifest_sha256', 'target_manifest_sha256'):
        if not _sha(value.get(key)):raise PatchUnusable('patch_descriptor_invalid')
    size = value.get('size_bytes')
    if type(size) is not int or not 0 < size <= updates.MAX_ARCHIVE:
        raise PatchUnusable('patch_descriptor_invalid')
    if kind == 'pacer' and (value.get('base_size_bytes') != size or
                           value.get('integrity_scope') != 'base_archive_prefix'):
        raise PatchUnusable('patch_descriptor_invalid')
    return dict(value)


def _json(archive, name, *, limit=4 * 1024**2):
    row = archive.getinfo(name)
    if row.file_size > limit:raise PatchUnusable('patch_metadata_too_large')
    value = json.loads(archive.read(name).decode('utf-8-sig'))
    if not isinstance(value, dict):raise PatchUnusable('patch_manifest_invalid')
    return value


def inspect(path, entry, *, trace_id=None, verify=None, progress=None):
    """Return immutable target plan. A personal receipt proves PATCH prefix SHA."""
    try:
        return _inspect(Path(path), entry, trace_id=trace_id, verify=verify, progress=progress)
    except (BadZipFile, KeyError, TypeError, ValueError, UnicodeError) as error:
        raise PatchUnusable('patch_manifest_invalid') from None
    except updates.UpdateError as error:
        if isinstance(error,PatchUnusable):raise
        raise PatchUnusable(str(error)) from None


def _inspect(path, entry, *, trace_id, verify, progress):
    from desktop import maauma_components as components
    private = entry['component_id'] == 'pacer'
    size = entry['size_bytes']
    receipt = None
    raw = path
    if private:
        if not size < path.stat().st_size <= updates.MAX_ARCHIVE:
            raise PatchUnusable('patch_integrity_failed')
        raw = path.with_name(path.stem + '-prefix.zip')
        remaining = size;hashed = hashlib.sha256()
        if progress is not None:progress.verification('archive_integrity', 0, size)
        with path.open('rb') as source, raw.open('wb') as target:
            while remaining:
                block = source.read(min(1024**2, remaining))
                if not block:raise PatchUnusable('patch_integrity_failed')
                target.write(block);hashed.update(block);remaining -= len(block)
                if progress is not None:progress.verification('archive_integrity', size - remaining, size)
        if hashed.hexdigest() != entry['sha256']:raise PatchUnusable('patch_integrity_failed')
    elif path.stat().st_size != size or components._digest(path, progress=progress) != entry['sha256']:
        raise PatchUnusable('patch_integrity_failed')
    with ZipFile(raw) as archive:
        inventory = components._archive_inventory(archive)
        if private:
            with ZipFile(path) as final:
                personal = components._archive_inventory(final)
                if set(personal) != set(inventory) | {'distribution-receipt.json'}:
                    raise PatchUnusable('patch_personalization_invalid')
                for name, row in inventory.items():
                    if components._metadata(row) != components._metadata(personal[name]):
                        raise PatchUnusable('patch_personalization_invalid')
                receipt_row = personal['distribution-receipt.json']
                if receipt_row.header_offset < size or receipt_row.file_size > 16384:
                    raise PatchUnusable('patch_personalization_invalid')
                receipt = service.validate_receipt(_json(final, 'distribution-receipt.json', limit=16384),
                                                   product='uma-cut', kind='package')
                if (receipt['trace_id'] != trace_id or receipt['version'] != entry['version'] or
                        receipt['base_sha256'] != entry['sha256']):
                    raise service.ServiceError('distribution_receipt_mismatch')
                if progress is not None:progress.verification('receipt', 0, None)
                if verify is None or verify(receipt) is not True:
                    raise service.ServiceError('distribution_receipt_unverified')
        patch = _json(archive, MANIFEST)
        if patch.get('schema_version') != 'maauma.file_patch_manifest.v1':
            raise PatchUnusable('patch_manifest_invalid')
        if any(patch.get(key) != entry.get(key) for key in IDENTITY):
            raise PatchUnusable('patch_identity_mismatch')
        if 'patch_id' in patch and patch['patch_id'] != entry['patch_id']:
            raise PatchUnusable('patch_identity_mismatch')
        target_member = patch.get('target_manifest_member')
        if target_member != 'target-manifest.json':raise PatchUnusable('patch_manifest_invalid')
        target_raw = archive.read(target_member)
        if len(target_raw) > 4 * 1024**2 or hashlib.sha256(target_raw).hexdigest() != entry['target_manifest_sha256']:
            raise PatchUnusable('patch_target_manifest_invalid')
        target = json.loads(target_raw.decode('utf-8-sig'))
        if target.get('version') != entry['version'] or target.get('product_id') != entry['product_id']:
            raise PatchUnusable('patch_target_manifest_invalid')
        if target.get('publication_ready') is not True or target.get('release_holds'):
            raise PatchUnusable('candidate_not_released')
        target_name = patch.get('target_manifest_name')
        if private:
            if (target_name != components.MANIFEST or target.get('schema_version') != 'maauma.component.v1' or
                    target.get('component_id') != 'pacer' or target.get('base_version') != entry['base_version']):
                raise PatchUnusable('patch_target_manifest_invalid')
            target_paths = target.get('files')
            hashes = target.get('file_hashes')
            mutable = target.get('mutable_files', [])
            if (not isinstance(target_paths, list) or not target_paths or not isinstance(hashes, dict) or
                    set(hashes) != set(target_paths) or not isinstance(mutable, list) or
                    not set(mutable) <= components.MUTABLE or not set(mutable) <= set(target_paths) or
                    target.get('receipt_targets', []) not in ([], ['pacer/distribution-receipt.json'])):
                raise PatchUnusable('patch_target_manifest_invalid')
        else:
            updates.validate_manifest(target)
            if target_name not in updates.MANIFESTS:raise PatchUnusable('patch_target_manifest_invalid')
            target_paths = target['files'] + [target_name]
        files = patch.get('target_files')
        if not isinstance(files, dict) or len(files) > updates.MAX_FILES or set(files) != set(target_paths):
            raise PatchUnusable('patch_target_inventory_invalid')
        folded = set();unpacked = 0
        for name, row in files.items():
            updates.relative_path(name)
            if private and (name.casefold() in {item.casefold() for item in updates.MANIFESTS}|{components.STATE.casefold()} or name.casefold().startswith('updates/')):
                raise PatchUnusable('reserved_component_path')
            if name.casefold() in folded or not isinstance(row, dict) or not _sha(row.get('sha256')):
                raise PatchUnusable('patch_target_inventory_invalid')
            folded.add(name.casefold())
            length = row.get('size_bytes')
            if type(length) is not int or not 0 <= length <= updates.MAX_UNPACKED:
                raise PatchUnusable('patch_target_inventory_invalid')
            unpacked += length
            if private and row['sha256'] != hashes[name]:raise PatchUnusable('patch_target_inventory_invalid')
        if unpacked > updates.MAX_UNPACKED:raise PatchUnusable('package_too_large')
        if any('/'.join(name.split('/')[:index]).casefold() in folded
               for name in files for index in range(1, len(name.split('/')))):
            raise PatchUnusable('patch_target_inventory_invalid')
        if not private and files[target_name] != {'sha256': entry['target_manifest_sha256'], 'size_bytes': len(target_raw)}:
            raise PatchUnusable('patch_target_manifest_invalid')
        operations = patch.get('operations');deleted = patch.get('deleted')
        if not isinstance(operations, list) or not isinstance(deleted, list):raise PatchUnusable('patch_manifest_invalid')
        used = {MANIFEST, target_member};paths = set()
        total = sum(files[operation.get('path')]['size_bytes'] for operation in operations
                    if isinstance(operation, dict) and operation.get('path') in files and operation.get('mode') == 'file')
        processed = 0
        if progress is not None:progress.verification('file_integrity', 0, total)
        for operation in operations:
            if not isinstance(operation, dict):raise PatchUnusable('patch_operation_invalid')
            name = updates.relative_path(operation.get('path'))
            if (name not in files or name in paths or components.preserved(name) or
                    name in components.MUTABLE or name.casefold() in {'distribution-receipt.json', 'pacer/distribution-receipt.json'}):
                raise PatchUnusable('patch_operation_invalid')
            paths.add(name)
            if operation.get('mode') == 'file':
                member = updates.relative_path(operation.get('member'));used.add(member)
                if inventory[member].file_size != files[name]['size_bytes']:
                    raise PatchUnusable('patch_payload_invalid')
                hashed = hashlib.sha256()
                with archive.open(member) as source:
                    while block := source.read(1024**2):
                        hashed.update(block);processed += len(block)
                        if progress is not None:progress.verification('file_integrity', processed, total)
                if hashed.hexdigest() != files[name]['sha256']:raise PatchUnusable('patch_payload_invalid')
            elif operation.get('mode') == 'zip_recipe':
                if not name.startswith('source/') or not name.endswith('.zip'):
                    raise PatchUnusable('patch_recipe_invalid')
                recipe_member = updates.relative_path(operation.get('recipe_member'))
                literal_member = updates.relative_path(operation.get('literal_member'))
                used.update((recipe_member, literal_member))
                recipe = _json(archive, recipe_member)
                if (recipe.get('schema_version') != 'maauma.zip_copy_recipe.v1' or
                        recipe.get('target_sha256') != files[name]['sha256'] or
                        recipe.get('target_size_bytes') != files[name]['size_bytes']):
                    raise PatchUnusable('patch_recipe_invalid')
            else:raise PatchUnusable('patch_operation_invalid')
        deleted_seen = set()
        for name in deleted:
            updates.relative_path(name)
            if name in deleted_seen or name in files or components.preserved(name) or name in components.MUTABLE:
                raise PatchUnusable('patch_deletion_invalid')
            deleted_seen.add(name)
        if used != set(inventory):raise PatchUnusable('patch_inventory_mismatch')
        return {'manifest': target, 'manifest_name': target_name, 'manifest_member': target_member,
                'files': files, 'operations': operations, 'receipt': receipt, 'raw_archive': str(raw)}


def patch_plan(root, stage, row, *, verify, materialize=False, progress=None):
    """Keep archive objects alive in plan['archives'] until the caller is done."""
    from desktop import maauma_components as components
    entry = row['patch_entry'];path = updates.contained(stage, row['file'])
    if row['kind'] == 'pacer' and (path.stat().st_size != row['personal_size_bytes'] or
                                _hash(path) != row['personal_sha256']):
        raise PatchUnusable('patch_integrity_failed')
    data = inspect(path, entry, trace_id=row.get('trace_id'), verify=verify, progress=progress)
    archive = ZipFile(data['raw_archive'])
    sources = {name: {'sha256': value['sha256'], 'size_bytes': value['size_bytes'],
                      'source': None, 'owner': row['kind']}
               for name, value in data['files'].items() if not components.preserved(name) and name not in components.MUTABLE}
    try:
        for operation in data['operations']:
            name = operation['path']
            if operation['mode'] == 'file':sources[name]['source'] = ('zip', archive, operation['member'])
            else:
                output = updates.contained(stage, 'materialized/' + row['kind'] + '/' + name)
                if materialize:
                    from desktop.zip_recipe import reconstruct
                    recipe = _json(archive, operation['recipe_member'])
                    literal = updates.contained(stage, 'recipe-literals/' + row['kind'] + '/' + name + '.bin')
                    literal.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(operation['literal_member']) as source, literal.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    try:reconstruct(root, recipe, literal, output, expected=data['files'][name], progress=progress)
                    except updates.UpdateError as error:
                        if str(error) == 'patch_basis_mismatch':raise LocalMismatch('patch_basis_mismatch') from None
                        raise PatchUnusable('patch_recipe_invalid') from None
                if not output.is_file() or output.stat().st_size != data['files'][name]['size_bytes'] or _hash(output) != data['files'][name]['sha256']:
                    raise PatchUnusable('patch_materialized_invalid')
                sources[name]['source'] = ('file', output)
        if row['kind'] == 'base':
            sources[data['manifest_name']]['source'] = ('zip', archive, data['manifest_member'])
        return {'kind': row['kind'], 'manifest': data['manifest'], 'manifest_name': data['manifest_name'],
                'sources': sources, 'receipt': data['receipt'], 'archives': [archive],
                'mutable_payloads': {}}
    except Exception:
        archive.close();raise


def full_plan(path, entry, *, kind, trace_id=None, verify=None, progress=None):
    from desktop import maauma_components as components
    path = Path(path)
    if kind == 'base':
        if path.stat().st_size != entry['size_bytes'] or components._digest(path, progress=progress) != entry['sha256']:
            raise updates.UpdateError('package_integrity_failed')
        name, manifest = updates.inspect_archive(path, entry, local_test=False)
        paths = manifest['files'] + [name];receipt = None;mutable = set()
    else:
        manifest, receipt = components.inspect_component(path, entry, trace_id=trace_id, verify=verify, progress=progress)
        name = components.MANIFEST;paths = manifest['files'];mutable = set(manifest.get('mutable_files', []))
    archive = ZipFile(path)
    try:
        sources = {}
        for target in paths:
            if components.preserved(target) or target in mutable:continue
            row = archive.getinfo(target)
            sources[target] = {'sha256': manifest.get('file_hashes', {}).get(target), 'size_bytes': row.file_size,
                               'source': ('zip', archive, target), 'owner': kind}
        return {'kind': kind, 'manifest': manifest, 'manifest_name': name, 'sources': sources,
                'receipt': receipt, 'archives': [archive],
                'mutable_payloads': {target: archive.read(target) for target in mutable}}
    except Exception:
        archive.close();raise


def effective(plans):
    merged = {}
    for kind in ('base', 'pacer'):
        for plan in plans:
            if plan['kind'] == kind:merged.update(plan['sources'])
    return merged


def validate_reuse(root, sources, *, progress=None):
    rows = [(name, row) for name, row in sources.items() if row['source'] is None]
    total = sum(row['size_bytes'] for _, row in rows);processed = 0
    if progress is not None:progress.verification('file_integrity', 0, total)
    for name, row in rows:
        path = updates.contained(root, name)
        if not path.is_file() or path.stat().st_size != row['size_bytes']:
            error = LocalMismatch('patch_reuse_mismatch');error.owner = row['owner'];raise error
        hashed = hashlib.sha256()
        with path.open('rb') as source:
            while block := source.read(1024**2):
                hashed.update(block);processed += len(block)
                if progress is not None:progress.verification('file_integrity', processed, total)
        if hashed.hexdigest() != row['sha256']:
            error = LocalMismatch('patch_reuse_mismatch');error.owner = row['owner'];raise error


def close(plans):
    for plan in plans:
        for archive in plan.get('archives', []):archive.close()


def prepare_transfer(root, stage, *, kind, full, source_version, trace_id, public,
                     progress=None, force_full=False, fallback_reason=None):
    from desktop import maauma_components as components
    item = 'base' if kind == 'base' else 'authorized_extension'
    entry = None
    reason = fallback_reason
    if not force_full and source_version is not None:
        if prefer_full(root,kind,source_version,full):reason='local_state_mismatch'
        else:
            value=service.patch_catalog(root,kind=kind,from_version=source_version,
                                        to_version=full['version'],base_version=full.get('base_version',full['version']))
            if value is None:reason='no_patch'
            else:
                try:
                    entry=descriptor(value,full,kind=kind,source=source_version,channel=service.config(root)['channel'])
                    if entry['size_bytes'] >= full.get('base_size_bytes',full['size_bytes']):
                        entry=None;reason='no_patch'
                    elif kind=='base':
                        _,name,_=updates.installation(root)
                        if _hash(root/name)!=entry['from_manifest_sha256']:
                            entry=None;reason='local_state_mismatch'
                except PatchUnusable:entry=None;reason='patch_unusable'
    elif reason is None:reason='no_patch'
    if entry is not None:
        if progress is not None:progress.transfer(item,'file_patch',entry['size_bytes'] if kind=='base' else None)
        path=Path(stage)/(kind+'-patch.zip')
        components.download(root,entry,path,product=entry['product_id'],public=kind=='base',progress=progress,item_id=item)
        row={'kind':kind,'delivery':'file_patch','file':path.name,'entry':full,'patch_entry':entry,
             'from_version':source_version,'trace_id':trace_id}
        if kind=='pacer':row.update(personal_sha256=_hash(path),personal_size_bytes=path.stat().st_size)
        try:
            plan=patch_plan(root,stage,row,verify=lambda receipt:service.verify_receipt(root,receipt),
                            materialize=True,progress=progress)
            row['receipt']=plan['receipt']
            return row,plan
        except LocalMismatch:reason='local_state_mismatch'
        except PatchUnusable:reason='patch_unusable'
        # Only a locally unusable patch selects this one full fallback. Network,
        # service/receipt authorization and filesystem errors propagate unchanged.
    if progress is not None:progress.transfer(item,'full',full['size_bytes'] if kind=='base' else None,reason)
    path=Path(stage)/(kind+'.zip')
    components.download(root,full,path,product='umaa-public' if kind=='base' else 'uma-cut',
                        public=public,progress=progress,item_id=item)
    plan=full_plan(path,full,kind=kind,trace_id=trace_id,
                   verify=lambda receipt:service.verify_receipt(root,receipt),progress=progress)
    row={'kind':kind,'file':path.name,'entry':full,'from_version':source_version,'trace_id':trace_id}
    if kind=='pacer':row.update(receipt=plan['receipt'],personal_sha256=_hash(path),personal_size_bytes=path.stat().st_size)
    return row,plan
