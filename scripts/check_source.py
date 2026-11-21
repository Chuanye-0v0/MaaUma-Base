"""Check the public source boundary, binaries, credentials, syntax, and strategy leaks."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.build', 'bin', 'obj', '__pycache__', '.venv', '.pytest_cache'}
FORBIDDEN_NAMES = {
    'admin.json', 'worker-secrets.json', 'private-service-auth.json',
    'distribution-receipt.json', 'mfa-accounts.json', 'credentials.json',
    'authorization.json', 'control_token.json', 'device-key-v1.bin',
}
FORBIDDEN_PATHS = (
    'pacer/runtime/', 'pacer/ocr/', 'pacer/backend/data/', 'pacer/backend/assets/',
    'pacer/backend/bridges/', 'pacer/backend/runs/', 'pacer/backend/logs/',
    'pacer/backend/.cache/', 'pacer/strategy_pack/v29_pack/',
    'backend/core/auto_trainer/', 'backend/desktop/mfa_batch.py',
    'backend/desktop/mfa_accounts.py', 'pacer/backend/desktop/mfa_batch.py',
    'pacer/backend/desktop/mfa_accounts.py', 'runtime/', 'dotnet/', 'ocr/',
    'local_models/', 'backend/data/', 'backend/assets/', 'resource/portraits/',
)
FORBIDDEN_BINARY = {
    '.exe', '.dll', '.pyd', '.pdb', '.onnx', '.traineddata', '.pt', '.pth', '.zip',
    '.nupkg', '.ttf', '.otf', '.woff', '.woff2', '.png', '.jpg', '.jpeg', '.gif',
    '.webp', '.bmp', '.ico', '.icns', '.wav', '.mp3', '.ogg',
}
# Untouched pre-existing 0.4.5/0.5.9 non-Pacer binary/data entries in upstream MaaUma-Base 74f8dae.
LEGACY_PREEXISTING_059_PATHS = {
    'MaaUma.Entry.dll',
    'MaaUma.exe',
    'backend/core/guild_daily/assets/guild-donation-list-icon.png',
    'backend/core/guild_daily/assets/guild-request-icon.png',
    'backend/core/guild_daily/assets/locked.png',
    'backend/core/guild_daily/assets/unlocked.png',
    'backend/core/loh/assets/watch-carrot-icon.png',
    'backend/core/loh/assets/watch-fast-forward.png',
    'backend/core/loh/assets/watch-reward-title.png',
    'backend/core/strongest_team/assets/score_update_title.png',
    'backend/data/parent_library/aptitude_catalog.json',
    'backend/data/parent_library/compatibility_relations.json',
    'backend/data/parent_library/planner_catalog.json',
    'backend/data/parent_library/race_catalog.json',
    'backend/data/parent_library/skill_sources.json',
    'backend/data/parent_library/team_guide_sources.json',
    'backend/data/skills/cn-20260916/cn_skill_master.json',
    'gui/MaaUma.Entry/assets/MaaUma.ico',
    'tools/maintenance/MaaUma-storage-audit-tool.zip',
}
TOKEN = re.compile(
    r'\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})\b'
    r'|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'
)
STRATEGY_LEAK_TEXT_EXTS = {'.py', '.cs', '.axaml', '.xaml', '.json', '.md', '.txt'}
STRATEGY_LEAK_SKIP_FILES = {
    'scripts/check_source.py',
    'SOURCE-ORIGIN.json',
    'SOURCE-CONTENTS.json',
}
STRATEGY_LEAK_PATTERNS = tuple(
    (label, re.compile(pat))
    for label, pat in (
        ('PRESET_2_[4-9]', r'PRESET_2_[4-9]'),
        ('v27_', r'\bv27_'),
        ('v28_', r'\bv28_'),
        ('v29_', r'\bv29_'),
        ('v29c_', r'\bv29c_'),
        ('thin_surplus_enabled', r'\bthin_surplus_enabled\b'),
        ('year1_tuning_bridge', r'\byear1_tuning_bridge'),
        ('dynamic_risk_enabled', r'\bdynamic_risk_enabled\b'),
        ('ss_early_exception_4p', r'\bss_early_exception_4p'),
        ('arc2_step_vitality', r'\barc2_step_vitality'),
        ('event_energy_tempo', r'\bevent_energy_tempo'),
        ('arc1_cn_race_ap', r'\barc1_cn_race_ap'),
        ('arc1_race_a1', r'\barc1_race_a1'),
        ('arc1_race_a2', r'\barc1_race_a2'),
        ('arc2_courage_zero_stamina', r'\barc2_courage_zero_stamina'),
        ('arc2_high_cash_monetization', r'\barc2_high_cash_monetization'),
        ('arc2_unmodeled_reserve', r'\barc2_unmodeled_reserve'),
        ('2.8.3', r'2\.8\.3'),
        ('2.9.1', r'2\.9\.1'),
        ('2.9.2', r'2\.9\.2'),
        ('"2.9"', r'"2\.9"'),
        ('"2.8"', r'"2\.8"'),
        ('"2.7"', r'"2\.7"'),
        ('"2.6"', r'"2\.6"'),
        ('"2.5"', r'"2\.5"'),
        ('"2.4"', r'"2\.4"'),
        ('拼好权', r'拼好权'),
        ('二炉冠军', r'二炉冠军'),
        ('bench_29', r'bench_29'),
    )
)
QUOTED_VERSION_LEAK_LABELS = frozenset({'"2.9"', '"2.8"', '"2.7"', '"2.6"', '"2.5"', '"2.4"'})
SKILL_CATALOG_PREFIXES = (
    'backend/data/skills/',
    'knowledge-tools/data/knowledge/',
)


def check(root=ROOT, *, strict_legacy=False):
    root = Path(root).resolve()
    issues = []
    legacy_warnings = []
    count = 0
    python_count = 0
    projects = 0
    paths = []
    for directory, folders, names in os.walk(root, followlinks=False):
        for folder in folders:
            path = Path(directory) / folder
            if folder not in SKIP and path.is_symlink():
                issues.append({'path': path.relative_to(root).as_posix(), 'kind': 'linked_source'})
        folders[:] = [folder for folder in folders if folder not in SKIP and not (Path(directory) / folder).is_symlink()]
        paths.extend(Path(directory) / name for name in names)

    for path in paths:
        relative = path.relative_to(root)
        if any(part in SKIP for part in relative.parts):
            continue
        if path.is_symlink():
            issues.append({'path': relative.as_posix(), 'kind': 'linked_source'})
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        count += 1
        is_legacy = name in LEGACY_PREEXISTING_059_PATHS

        if path.name.casefold() in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PATHS):
            entry = {'path': name, 'kind': 'private_or_runtime_path'}
            if not strict_legacy and is_legacy:
                legacy_warnings.append(entry)
            else:
                issues.append(entry)
        if path.suffix.lower() in FORBIDDEN_BINARY:
            entry = {'path': name, 'kind': 'runtime_or_model_binary'}
            if not strict_legacy and is_legacy:
                legacy_warnings.append(entry)
            else:
                issues.append(entry)
        if path.name.startswith('.env') and path.name != '.env.example':
            issues.append({'path': name, 'kind': 'environment_credentials_file'})
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (UnicodeError, OSError):
            continue
        if TOKEN.search(text):
            issues.append({'path': name, 'kind': 'credential_signature'})
        if path.suffix.lower() in STRATEGY_LEAK_TEXT_EXTS and name not in STRATEGY_LEAK_SKIP_FILES:
            is_axaml = path.suffix.lower() == '.axaml'
            is_skill_catalog = name.startswith(SKILL_CATALOG_PREFIXES)
            for lineno, line in enumerate(text.splitlines(), 1):
                if is_axaml and 'ScaleTransform.Scale' in line:
                    continue
                for label, rx in STRATEGY_LEAK_PATTERNS:
                    if is_skill_catalog and label in QUOTED_VERSION_LEAK_LABELS:
                        continue
                    if rx.search(line):
                        issues.append({
                            'path': f'{name}:{lineno}',
                            'kind': 'private_strategy_leak',
                            'token': label,
                        })
                        break
        if name == 'pacer/backend/core/auto_trainer/cut_runtime_license.py':
            if 'ensure_runtime_license' in text or '_online_check' in text:
                issues.append({'path': name, 'kind': 'runtime_license_gate'})
        if path.suffix == '.py':
            python_count += 1
            try:
                ast.parse(text, filename=name)
            except SyntaxError:
                issues.append({'path': name, 'kind': 'python_syntax'})
        if path.suffix == '.csproj':
            projects += 1
            for node in ET.fromstring(text).iter():
                tag = node.tag.rsplit('}', 1)[-1]
                if tag == 'HintPath' and re.match(r'\s*(?:[A-Za-z]:|/)', node.text or ''):
                    issues.append({'path': name, 'kind': 'machine_local_hintpath'})
                if tag != 'ProjectReference':
                    continue
                value = node.get('Include', '').replace('\\', '/')
                if re.match(r'^(?:[A-Za-z]:|/)', value) or '$(' in value:
                    issues.append({'path': name, 'kind': 'nonportable_project_reference'})
                    continue
                target = (path.parent / value).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    issues.append({'path': name, 'kind': 'missing_project_reference'})

    for required in (
        'README.md',
        'LICENSE',
        'NOTICE.md',
        'SOURCE-ORIGIN.json',
        'gui/MFAAvalonia/MFAAvalonia.csproj',
        'gui/MFAAvalonia/Helper/MaaUmaPacer/StrategyPackCatalog.cs',
        'gui/MFAAvalonia.Desktop/MFAAvalonia.Desktop.csproj',
        'pacer/backend/core/auto_trainer/larc_strategy_extension.py',
        'pacer/strategy_pack/README.md',
        'tests/test_strategy_extension_spi.py',
        'tools/MaaUmaIdentity/MaaUmaIdentity.csproj',
        'tools/UmaPublicCheck/UmaPublicCheck.csproj',
    ):
        if not (root / required).is_file():
            issues.append({'path': required, 'kind': 'required_source_missing'})

    return {
        'ok': not issues,
        'schema_version': 'maauma.check_source.v2',
        'files': count,
        'python_files': python_count,
        'projects': projects,
        'issues': issues,
        'legacy_preexisting_warnings': legacy_warnings,
        'legacy_preexisting_count': len(legacy_warnings),
        'device_actions': 0,
        'old_history_audited': False,
        'scope': 'source paths, binaries, credentials, project references, syntax, open-source license decoupling, and strategy split boundary',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict-legacy', action='store_true', help='Fail on pre-existing 0.5.9 non-Pacer binary/data paths as well.')
    args = parser.parse_args()
    result = check(strict_legacy=args.strict_legacy)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
