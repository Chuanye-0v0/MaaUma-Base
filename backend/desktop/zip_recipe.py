"""Exact-byte source ZIP reuse; standard library only, no generic binary delta."""
import hashlib
from pathlib import Path

from desktop import product_updates as updates


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def reconstruct(root, recipe, literal_path, output, *, expected, progress=None):
    if not isinstance(recipe, dict) or recipe.get('schema_version') != 'maauma.zip_copy_recipe.v1':
        raise updates.UpdateError('patch_recipe_invalid')
    basis_name = updates.relative_path(recipe.get('basis_path'))
    if not basis_name.startswith('source/') or not basis_name.endswith('.zip'):
        raise updates.UpdateError('patch_recipe_invalid')
    basis = updates.contained(root, basis_name)
    size = recipe.get('basis_size_bytes')
    target_size = recipe.get('target_size_bytes')
    if (type(size) is not int or not 0 < size <= updates.MAX_ARCHIVE or
            type(target_size) is not int or not 0 < target_size <= updates.MAX_ARCHIVE or
            target_size != expected['size_bytes'] or recipe.get('target_sha256') != expected['sha256']):
        raise updates.UpdateError('patch_recipe_invalid')
    if not basis.is_file() or basis.stat().st_size != size or digest(basis) != recipe.get('basis_sha256'):
        raise updates.UpdateError('patch_basis_mismatch')
    literal_path = Path(literal_path)
    if digest(literal_path) != recipe.get('literal_sha256'):
        raise updates.UpdateError('patch_recipe_invalid')
    operations = recipe.get('operations')
    if not isinstance(operations, list) or not operations or len(operations) > 200000:
        raise updates.UpdateError('patch_recipe_invalid')
    literal_size = literal_path.stat().st_size
    count = 0
    for operation in operations:
        if not isinstance(operation, dict) or operation.get('op') not in {'copy', 'literal'}:
            raise updates.UpdateError('patch_recipe_invalid')
        offset, length = operation.get('offset'), operation.get('length')
        limit = size if operation['op'] == 'copy' else literal_size
        if (type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 or
                offset + length > limit):
            raise updates.UpdateError('patch_recipe_invalid')
        count += length
        if count > target_size:
            raise updates.UpdateError('patch_recipe_invalid')
    if count != target_size:
        raise updates.UpdateError('patch_recipe_invalid')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    processed = 0
    hashed = hashlib.sha256()
    if progress is not None:progress.verification('file_integrity', 0, target_size)
    with basis.open('rb') as old, literal_path.open('rb') as literal, output.open('wb') as target:
        for operation in operations:
            source = old if operation['op'] == 'copy' else literal
            source.seek(operation['offset'])
            remaining = operation['length']
            while remaining:
                block = source.read(min(1024**2, remaining))
                if not block:raise updates.UpdateError('patch_recipe_invalid')
                target.write(block);hashed.update(block)
                processed += len(block);remaining -= len(block)
                if progress is not None:progress.verification('file_integrity', processed, target_size)
    if hashed.hexdigest() != expected['sha256']:
        output.unlink(missing_ok=True)
        raise updates.UpdateError('patch_recipe_invalid')
    return output
