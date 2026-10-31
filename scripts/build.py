"""Build the public sources, optionally assembling with an explicit runtime base.

No private generators, saved user settings, device operations or publication.
The source checkout stays clean: restore/build output lives in a fresh --work.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.5.8'
PROJECTS = {'gui': 'gui/MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj',
    'identity': 'tools/MaaUmaIdentity/MaaUmaIdentity.csproj',
    'check': 'tools/UmaPublicCheck/UmaPublicCheck.csproj'}
SKIP = {'bin', 'obj', '.git', '.vs', '__pycache__', '.build'}
PERSONAL = {'private-service-auth.json', 'distribution-receipt.json', 'accounts.json',
    'mfa-accounts.json', 'credentials.json', 'device-key-v1.bin', 'control_token.json'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def member(root, name):
    """Resolve a release member without accepting traversal or linked files."""
    relative = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe_release_member')
    target = root / Path(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise ValueError('linked_release_member')
    return target


def check():
    references = 0
    for relative in PROJECTS.values():
        if not (ROOT / relative).is_file():
            raise ValueError('project_missing:' + relative)
    for folder in ('gui', 'tools'):
        source = ROOT / folder
        for project in source.rglob('*.csproj'):
            if set(project.relative_to(source).parts) & SKIP:
                continue
            for node in ET.parse(project).getroot().iter():
                tag = node.tag.rsplit('}', 1)[-1]
                if tag == 'ProjectReference':
                    value = node.get('Include', '').replace('\\', '/')
                    target = (project.parent / value).resolve()
                    if re.match(r'[A-Za-z]:|/', value) or not target.is_relative_to(source.resolve()) or not target.is_file():
                        raise ValueError('project_reference_not_self_contained:' + str(project))
                    references += 1
                if tag == 'HintPath' and node.text:
                    value = node.text.replace('\\', '/')
                    if re.match(r'[A-Za-z]:|/', value) or '../nuget/' in value:
                        raise ValueError('developer_hint_path:' + str(project))
    count = 0
    for path in (ROOT / 'backend').rglob('*.py'):
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path)); count += 1
    return {'ok': True, 'version': VERSION, 'projects': PROJECTS,
        'project_references': references, 'backend_python_files': count, 'device_actions': 0}


def mark_rgba(size):
    """Original M geometry, matching gui/MFAAvalonia/Assets/maauma-mark.svg."""
    points = [(26,96),(26,32),(40,32),(64,62),(88,32),(102,32),
              (102,96),(86,96),(86,58),(64,84),(42,58),(42,96)]
    pixels = bytearray()
    for y in range(size):
        for x in range(size):
            px, py = (x + .5) * 128 / size, (y + .5) * 128 / size
            dx, dy = max(28-px, 0, px-100), max(28-py, 0, py-100)
            if dx*dx + dy*dy > 24*24:
                pixels.extend((0,0,0,0)); continue
            inside = False
            for index, (ax, ay) in enumerate(points):
                bx, by = points[index-1]
                if (ay > py) != (by > py) and px < (bx-ax)*(py-ay)/(by-ay)+ax:
                    inside = not inside
            pixels.extend((255,255,255,255) if inside else (37,99,235,255))
    return pixels


def icon_bytes():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    sizes = (16,20,24,32,40,48,64,128,256)
    frames = []
    for size in sizes:
        rgba = mark_rgba(size)
        rows = b''.join(b'\0' + rgba[y*size*4:(y+1)*size*4] for y in range(size))
        frames.append(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size,size,8,6,0,0,0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    entries = []; offset = 6 + 16*len(sizes)
    for size, frame in zip(sizes, frames):
        entries.append(struct.pack('<BBBBHHII', size % 256,size % 256,0,0,1,32,len(frame),offset))
        offset += len(frame)
    return struct.pack('<HHH', 0,1,len(sizes)) + b''.join(entries) + b''.join(frames)


def run(command, log, env):
    result = subprocess.run(list(map(str, command)), cwd=ROOT, env=env,
        capture_output=True, text=True, encoding='utf8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    log.write_text(result.stdout + result.stderr, encoding='utf8')
    if result.returncode:
        raise RuntimeError('build_failed:' + str(log))


def build(work, dotnet, offline=False):
    check()
    work = work.resolve()
    if work.exists():
        raise ValueError('fresh_work_directory_required')
    dotnet = Path(shutil.which(str(dotnet)) or dotnet).resolve()
    if not dotnet.is_file():
        raise ValueError('dotnet_sdk_not_found')
    work.mkdir(parents=True)
    source = work / 'source'; source.mkdir()
    for name in ('gui', 'tools'):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns(*SKIP))
    shutil.copy2(ROOT / 'global.json', source / 'global.json')
    shutil.copy2(ROOT / 'NuGet.Config', source / 'NuGet.Config')
    icon = source / 'gui/MFAAvalonia/Assets/logo.ico'
    icon.write_bytes(icon_bytes())
    for name in ('temp', 'dotnet-home', 'empty-feed', 'logs', 'publish'):
        (work / name).mkdir()
    env = dict(os.environ, TEMP=str(work/'temp'), TMP=str(work/'temp'),
        DOTNET_CLI_HOME=str(work/'dotnet-home'), DOTNET_CLI_TELEMETRY_OPTOUT='1',
        DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1', DOTNET_GENERATE_ASPNET_CERTIFICATE='false',
        DOTNET_NOLOGO='1', DOTNET_CLI_UI_LANGUAGE='en', MSBUILDDISABLENODEREUSE='1',
        AVALONIA_TELEMETRY_OPTOUT='1', DOTNET_CLI_USE_MSBUILD_SERVER='0',
        NUGET_PACKAGES=os.environ.get('NUGET_PACKAGES', str(Path.home()/'.nuget/packages')))
    outputs = {}
    for name, relative in PROJECTS.items():
        project = source / relative
        restore = [dotnet, 'restore', project, '-r', 'win-x64', '--disable-parallel',
            '-p:NuGetAudit=false', '-p:SelfContained=false', '-p:Configuration=Release',
            '-p:EnableRuntimePackDownload=false', '-p:EnableAppHostPackDownload=false']
        if offline:
            restore += ['--source', work/'empty-feed', '--ignore-failed-sources']
        else:
            restore += ['--configfile', source/'NuGet.Config']
        run(restore, work/'logs'/(name+'-restore.log'), env)
        run([dotnet, 'publish', project, '-c', 'Release', '-r', 'win-x64', '--no-restore',
            '--no-self-contained', '-p:CopyLocalLockFileAssemblies=true', '-p:NuGetAudit=false',
            '-p:EnableRuntimePackDownload=false', '-p:EnableAppHostPackDownload=false',
            '-v', 'minimal', '-o', work/'publish'/name], work/'logs'/(name+'-build.log'), env)
        outputs[name] = str(work/'publish'/name)
    result = {'ok': True, 'version': VERSION, 'work': str(work), 'outputs': outputs,
        'offline_restore': offline, 'dotnet': str(dotnet), 'device_actions': 0,
        'source_checkout_build_outputs_created': False, 'bit_identical_release_claimed': False}
    write(work/'build-result.json', result)
    return result


def runtime_manifest(base):
    manifest = read(base/'public-package-manifest.json')
    if manifest.get('product_id') != 'umaa-public' or manifest.get('version') != VERSION or (base/'pacer').exists():
        raise ValueError('same_version_public_runtime_base_required')
    for name in manifest['files']:
        path = member(base, name)
        if PurePosixPath(name).parts[0] in {'pacer', 'runs', 'updates', 'logs', 'temp'} or path.name.casefold() in PERSONAL:
            raise ValueError('non_generic_runtime_base')
        if not path.is_file():
            raise ValueError('runtime_base_member_missing:' + name)
    return manifest


def assemble(work, base, output):
    work, base, output = work.resolve(), base.resolve(), output.resolve()
    build_result = read(work/'build-result.json')
    if not build_result.get('ok') or build_result.get('version') != VERSION:
        raise ValueError('build_required')
    manifest = runtime_manifest(base)
    if output.exists():
        raise ValueError('fresh_output_directory_required')
    output.mkdir(parents=True)
    for name in manifest['files']:
        if name.startswith(('source/', 'backend/core/account_login/assets/')):
            # Current source is supplied by this repository; personal login
            # templates are neither a build input nor part of rebuilt output.
            continue
        target = member(output, name); target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(member(base, name), target)
    for folder in ('backend', 'agent', 'config', 'resource'):
        if (ROOT/folder).is_dir():
            shutil.copytree(ROOT/folder, output/folder, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(*SKIP, '*.pyc'))
    for name in ('interface.json', 'appsettings.json', 'maauma-suite.json'):
        if (ROOT/name).is_file(): shutil.copy2(ROOT/name, output/name)
    for name in ('LICENSE', 'NOTICE.md', 'SOURCE-ORIGIN.json'):
        if (ROOT/name).is_file(): shutil.copy2(ROOT/name, output/name)
    if (ROOT/'licenses').is_dir():
        shutil.copytree(ROOT/'licenses', output/'licenses', dirs_exist_ok=True)
    for name in PROJECTS:
        publish = work/'publish'/name
        shutil.copytree(publish, output, dirs_exist_ok=True)
    # The public checker also supports the historical libs/ convention.
    (output/'libs').mkdir(exist_ok=True)
    for path in (work/'publish/gui').glob('*.dll'):
        if path.name != 'MFAAvalonia.dll': shutil.copy2(path, output/'libs'/path.name)
    holds = list(dict.fromkeys([*manifest.get('release_holds', []),
        *manifest.get('release_decision', {}).get('accepted_holds', [])]))
    manifest.update(publication_ready=False, local_review_candidate=True,
        release_stage='local_review', real_device_acceptance=False,
        release_holds=holds,
        release_decision={'schema_version':'maauma.release_decision.v1',
            'decision':'local_source_rebuild','publication_authorized':False,
            'version':VERSION, 'rights_independently_verified':False,
            'accepted_holds':[], 'remaining_holds':holds})
    manifest['release_preparation'] = {'gui_source_supplied':False,
        'source_repository_required':True, 'gui_refresh_pending':False}
    edition = read(output/'maauma-edition.json')
    edition.update(publication_ready=False, local_review_candidate=True,
        release_stage='local_review', real_device_acceptance=False)
    write(output/'maauma-edition.json', edition)
    manifest.setdefault('source_origins', {})['gui'] = 'Built from the ordinary public source checkout.'
    manifest['files'] = sorted(p.relative_to(output).as_posix() for p in output.rglob('*')
        if p.is_file() and p.name != 'public-package-manifest.json')
    write(output/'public-package-manifest.json', manifest)
    result = {'ok':True, 'version':VERSION, 'output':str(output), 'runtime_base':str(base),
        'publication_ready':False, 'device_actions':0, 'runtime_resources_are_external_inputs':True}
    write(work/'assembly-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('check')
    compiler = sub.add_parser('build')
    compiler.add_argument('--dotnet', default='dotnet')
    compiler.add_argument('--work', type=Path, default=ROOT/'.build')
    compiler.add_argument('--offline', action='store_true', help='Use only the existing NuGet cache.')
    app = sub.add_parser('assemble')
    app.add_argument('--work', type=Path, default=ROOT/'.build')
    app.add_argument('--runtime-base', type=Path, required=True)
    app.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check() if args.action == 'check' else build(args.work, args.dotnet, args.offline) if args.action == 'build' else assemble(args.work, args.runtime_base, args.output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
