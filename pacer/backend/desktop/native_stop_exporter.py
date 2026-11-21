"""Read-only export of one selected career's persisted provider failure.

No device, screen capture, HTTP request, task control or source-file mutation.
The only writes are a uniquely named diagnostic ZIP in the selected output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
import zipfile

sys.dont_write_bytecode = True
DEFAULT_RUN_ID = 'larc_panel_20261017_221528_ef7987'
TARGET_STATUS = 'state_context_provider_invalid'
TARGET_STATUSES = (TARGET_STATUS, 'training_page_observation_failed')
SCHEMA = 'maauma.stop_report_export.v2'
TRAINING_TABS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
RUN_ID = re.compile(r'larc_panel_\d{8}_\d{6}_[a-fA-F0-9]{6}\Z')
SEGMENT = re.compile(r'autonomous_[A-Za-z0-9_]+\Z')
TURN = re.compile(r'turn_\d+(?:_[A-Za-z0-9]+)*\Z')
MAX_JSON = 16 * 1024 * 1024
_SENSITIVE = re.compile(r'(?i)(token|secret|password|passwd|authorization|apikey|accesskey|cookie|'
    r'activation|invitecode|account|username|nickname|playername|trainername|email|contact|'
    r'deviceid|bindingid|用户名|账号|账户|昵称|密码|令牌|密钥|激活码|邀请码)')
_RAW_TEXT = {'screen_text', 'visible_text', 'normalized_text', 'ocr_text'}
_SAFE_IDS = {'run_id', 'run_session_id', 'schema_version', 'report_id'}
_INLINE_SECRET = re.compile(r'''(?ix)(\b(?:token|secret|password|passwd|authorization|api[_-]?key|
    account(?:[_-]?(?:id|name))?|user[_-]?name|activation[_-]?code|invite[_-]?code)\s*[:=]\s*)
    (?:(?:"[^"\r\n]*")|(?:'[^'\r\n]*')|[^\s,;]+)''')
_TOKENS = re.compile(r'(?i)\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{12,}|'
    r'\bAKID[A-Za-z0-9]{16,}|\bsk-[A-Za-z0-9_-]{16,}|'
    r'\b(?:md_|mc_)[A-Za-z0-9._~-]{8,}|\bMU-(?:[A-F0-9]{4}-){3}[A-F0-9]{4}\b')
_WINDOWS_PATH = re.compile(r'''(?i)(?<!\w)(?:[a-z]:[\\/]|\\\\[^\\\r\n]+\\)[^\r\n<>"'|]*''')
_UNIX_PATH = re.compile(r'''(?<![\w:])/(?:Users|home|root|mnt|tmp|var|opt|srv|workspace)(?:/[^\r\n<>"']*)?''')


class ExportError(RuntimeError):
    pass


def _resolved(path):
    path = Path(path).absolute()
    if os.name == 'nt':
        value = str(path)
        if not value.startswith('\\\\?\\'):
            value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
        path = Path(value)
    return path.resolve()


def _within(path, root):
    try:
        path = _resolved(path)
        return path if path.is_relative_to(root) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _read_json(path, run_root, warnings):
    path = _within(path, run_root)
    if path is None:
        warnings.append('引用越出指定局目录，已跳过。')
        return None
    if not path.is_file():
        return None
    for _ in range(2):
        try:
            before = path.stat()
            if before.st_size > MAX_JSON:
                warnings.append(path.relative_to(run_root).as_posix() + '：JSON 过大，已跳过。')
                return None
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                continue
            value = json.loads(raw.decode('utf-8-sig'))
            if isinstance(value, dict):
                return value
            break
        except (OSError, ValueError, UnicodeError):
            continue
    warnings.append(path.relative_to(run_root).as_posix() + '：暂时读不完整，已跳过；可稍后重新导出。')
    return None


def _children(folder, run_root, warnings):
    try:
        return sorted(folder.iterdir())
    except OSError:
        warnings.append(folder.relative_to(run_root).as_posix() + '：目录暂时无法读取，已跳过；可稍后重新导出。')
        return []


def _load_redactor(panel_root):
    """Import the shipped pure text helper, without calling any service API."""
    backend = panel_root / 'backend'
    if not (backend / 'desktop/maauma_error_feedback.py').is_file():
        return None
    sys.path.insert(0, str(backend))
    try:
        helper = importlib.import_module('desktop.maauma_error_feedback')
        return helper.redact
    except Exception:
        return None
    finally:
        sys.path.remove(str(backend))


def _text(value, run_root, redactor):
    # Preserve explicit in-run paths as relative paths before general redaction.
    try:
        candidate = Path(value)
        if candidate.is_absolute():
            relative = _within(candidate, run_root)
            if relative is not None:
                return relative.relative_to(run_root).as_posix()
    except (OSError, ValueError):
        pass
    value = value.replace(str(run_root), '[RUN]').replace(run_root.as_posix(), '[RUN]')
    if redactor is not None:
        try:
            value = redactor(value, min(262144, max(4000, len(value))))
        except Exception:
            pass
    value = re.sub(r'(?i)\bBearer\s+[^\s,;"\']+', 'Bearer [REDACTED]', value)
    value = _INLINE_SECRET.sub(lambda match: match[1] + '[REDACTED]', value)
    value = _TOKENS.sub('[REDACTED]', value)
    value = _WINDOWS_PATH.sub('[PATH]', value)
    value = _UNIX_PATH.sub('[PATH]', value)
    value = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[EMAIL]', value)
    return value


def _redact(value, run_root, redactor):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = str(key)
            normalized = re.sub(r'[_\s-]', '', key)
            sensitive = key not in _SAFE_IDS and (_SENSITIVE.search(normalized) or key in _RAW_TEXT)
            result[_text(key, run_root, redactor)] = '[REDACTED]' if sensitive else _redact(item, run_root, redactor)
        return result
    if isinstance(value, list):
        return [_redact(item, run_root, redactor) for item in value]
    if isinstance(value, str):
        return _text(value, run_root, redactor)
    return value


def _json_bytes(value, run_root, redactor):
    return json.dumps(_redact(value, run_root, redactor), ensure_ascii=False, indent=2).encode('utf8')


def _choose_run(panel_root, run_directory, run_id, registered_run_directory=False):
    if run_directory is not None:
        run_root = _resolved(run_directory)
        actual = run_root.name
        if registered_run_directory:
            # Native host only, after its existing record/attempt ownership check.
            # This flag is intentionally absent from the command-line interface.
            roots = (panel_root / 'pacer/backend/runs', panel_root / 'backend/runs')
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', actual)
                    or not any(_within(run_root, root) is not None and run_root != root for root in roots)):
                raise ExportError('已登记记录必须位于当前面板的运行目录中。')
        elif not RUN_ID.fullmatch(actual):
            raise ExportError('拖入的文件夹须为指定局号的 larc_panel_... 运行目录。')
        if run_id is not None and run_id != actual:
            raise ExportError('拖入的文件夹须为指定局号的 larc_panel_... 运行目录。')
        return run_root, actual, ['explicit_run_directory']
    if registered_run_directory:
        raise ExportError('已登记记录导出需要明确的原局运行目录。')
    run_id = run_id or DEFAULT_RUN_ID
    if not RUN_ID.fullmatch(run_id):
        raise ExportError('局号格式无效。请使用 larc_panel_日期_时间_六位编号。')
    candidates = [panel_root / 'pacer/backend/runs' / run_id, panel_root / 'backend/runs' / run_id]
    for path in candidates:
        resolved = _within(path, panel_root)
        if resolved is not None and resolved.is_dir():
            return resolved, run_id, [path.relative_to(panel_root).as_posix()]
    return None, run_id, [path.relative_to(panel_root).as_posix() for path in candidates]


def _image_bytes(path, run_root, warnings):
    path = _within(path, run_root)
    if path is None or path.suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
        warnings.append('原图引用无效或越出指定局目录，未读取。')
        return None
    try:
        raw = path.read_bytes()
        if raw.startswith(b'\x89PNG\r\n\x1a\n') or (raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9')):
            return raw
    except OSError:
        pass
    warnings.append(path.relative_to(run_root).as_posix() + '：原图缺失或不是 PNG/JPG，未补拍。')
    return None


def _code(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,240}', value):
        return value
    match = re.match(r'[A-Za-z][A-Za-z0-9_.-]{0,119}', value)
    return match[0] if match else None


def failure_details(pipeline):
    """Small pure diagnostic view; no filesystem, image reads or root-cause guess.

    A native host can call this on its already-bound last pipeline report rather
    than running the collector on every snapshot. Free-form paths/OCR are omitted.
    """
    pipeline = pipeline if isinstance(pipeline, dict) else {}
    report = pipeline.get('report', pipeline)
    report = report if isinstance(report, dict) else {}
    stages = report.get('stages') if isinstance(report.get('stages'), dict) else {}
    provider = report.get('state_context_provider', stages.get('state_context_provider'))
    provider = provider if isinstance(provider, dict) else {}
    result = {'status': _code(report.get('status')), 'provider_error': _code(provider.get('error'))}
    observation = stages.get('training_page_observation')
    if isinstance(observation, dict):
        data = {key: observation[key] for key in ('is_valid', 'consensus_complete')
            if type(observation.get(key)) is bool}
        if _code(observation.get('status')):
            data['status'] = _code(observation['status'])
        data['errors'] = [code for value in observation.get('errors', []) if (code := _code(value))] if isinstance(observation.get('errors'), list) else []
        data['tabs'] = {}
        tabs = observation.get('tabs') if isinstance(observation.get('tabs'), dict) else {}
        for tab in TRAINING_TABS:
            row = tabs.get(tab)
            if isinstance(row, dict):
                data['tabs'][tab] = {key: code for key in ('status', 'reason') if (code := _code(row.get(key)))}
                values = row.get('values')
                if isinstance(values, dict):
                    data['tabs'][tab]['values'] = {name: value for name, value in values.items()
                        if name in (*TRAINING_TABS, 'skill_pt') and type(value) is int}
        result['training_page_observation'] = data
    return result


def _reference(value, run_root, base):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = run_root / path if path.parts and path.parts[0].startswith('autonomous_') else base / path
    return _within(path, run_root)


def _observation_evidence(report, turn, run_root, entries, failure, warnings, redactor):
    stages = report.get('stages') if isinstance(report.get('stages'), dict) else {}
    observation = stages.get('training_page_observation')
    observation = observation if isinstance(observation, dict) else {}
    capture = stages.get('capture') if isinstance(stages.get('capture'), dict) else {}
    execution_root = _reference(capture.get('output_root'), run_root, turn)
    manifest_refs = [observation.get('source_manifest'), capture.get('manifest_path')]
    manifests = []
    for value in manifest_refs:
        path = _reference(value, run_root, turn)
        if path is None:
            if value:
                warnings.append('识别清单引用越出指定局目录或无效，已跳过。')
            continue
        if path in [item[0] for item in manifests] or path.suffix.lower() != '.json' or 'manifest' not in path.name.lower():
            continue
        data = _read_json(path, run_root, warnings)
        if data is not None:
            manifests.append((path, data))
    if execution_root is None and manifests:
        execution_root = manifests[0][0].parent
    # The writer's exact known filename is derived only from the explicitly
    # referenced execution root; invalid early observations may not write it.
    observation_paths = [observation.get('output_path'), observation.get('observation_path')]
    artifacts = report.get('artifacts') if isinstance(report.get('artifacts'), dict) else {}
    observation_paths.append(artifacts.get('training_page_observation_path'))
    if execution_root is not None:
        observation_paths.append(str(execution_root / 'training_page_observation.generated.json'))
    observations = [observation]
    for value in observation_paths:
        path = _reference(value, run_root, execution_root or turn)
        if path is None or path.suffix.lower() != '.json' or not path.name.startswith('training_page_observation'):
            continue
        data = _read_json(path, run_root, warnings)
        if data is None:
            continue
        name = path.relative_to(run_root).as_posix()
        entries[name] = _json_bytes(data, run_root, redactor)
        if name not in failure['files']:
            failure['files'].append(name)
        observations.append(data)
    group_id = observation.get('actual_group_id') or capture.get('actual_group_id')
    image_refs = {}
    def remember_image(tab, value):
        if execution_root is None or tab in image_refs:
            return
        path = _reference(value, run_root, execution_root)
        if path is None:
            if value:
                warnings.append('训练原图引用越出指定局目录或无效，已跳过。')
            return
        if not isinstance(group_id, str) or group_id not in path.parts:
            warnings.append('训练原图未明确绑定当前捕获组，已跳过。')
            return
        image_refs[tab] = path
    for data in observations:
        tabs = data.get('tabs') if isinstance(data.get('tabs'), dict) else {}
        for tab in TRAINING_TABS:
            row = tabs.get(tab)
            if isinstance(row, dict) and execution_root is not None:
                remember_image(tab, row.get('relative_path'))
    for path, data in manifests:
        groups = data.get('groups') if isinstance(data.get('groups'), list) else []
        selected = [group for group in groups if isinstance(group, dict) and group_id is not None
            and group.get('actual_group_id') == group_id
            and group.get('run_session_id') in (None, run_root.name)]
        projected_groups = []
        for group in selected:
            projected = {key: group[key] for key in ('actual_group_id', 'run_session_id', 'turn_index', 'errors', 'warnings') if key in group}
            projected['raw_files'] = []
            raw_files = group.get('raw_files') if isinstance(group.get('raw_files'), list) else []
            for raw in raw_files:
                if not isinstance(raw, dict):
                    continue
                tab = str(raw.get('source_screen', '')).removesuffix('_tab')
                if tab not in TRAINING_TABS:
                    continue
                projected['raw_files'].append({key: raw[key] for key in
                    ('relative_path', 'source_screen', 'sha256', 'sequence', 'width', 'height') if key in raw})
                if execution_root is not None:
                    remember_image(tab, raw.get('relative_path'))
            projected_groups.append(projected)
        # Copy only this failed capture group, never unrelated manifest history.
        projected_manifest = {key: data[key] for key in ('schema_version', 'schema') if key in data}
        projected_manifest.update(groups=projected_groups, export_scope='only explicitly referenced failing capture group')
        name = path.relative_to(run_root).as_posix()
        entries[name] = _json_bytes(projected_manifest, run_root, redactor)
        if name not in failure['files']:
            failure['files'].append(name)
        if not selected:
            warnings.append(name + '：未找到明确绑定的当前捕获组，未猜测其他组。')
    failure['observation_image_files'] = []
    for image in dict.fromkeys(image_refs.values()):
        raw = _image_bytes(image, run_root, warnings)
        if raw is not None:
            name = image.relative_to(run_root).as_posix()
            entries[name] = raw
            if name not in failure['files']:
                failure['files'].append(name)
            failure['observation_image_files'].append(name)


def collect(panel_root, *, run_directory=None, run_id=None, redactor=None, registered_run_directory=False):
    panel_root = _resolved(panel_root)
    run_root, run_id, checked = _choose_run(panel_root, run_directory, run_id, registered_run_directory)
    index = {'schema_version': SCHEMA, 'run_id': run_id, 'target_statuses': list(TARGET_STATUSES),
        'exported_at': datetime.now(timezone.utc).isoformat(), 'status': 'run_missing',
        'checked_locations': checked, 'segments': [], 'failures': [], 'warnings': [],
        'read_only': True, 'device_actions': 0, 'network_requests': 0, 'automatic_upload': False}
    entries = {}
    if run_root is None or not run_root.is_dir():
        index['warnings'].append('没有找到指定局的运行目录；未扫描其他局或磁盘。')
        return index, entries, run_root
    warnings = index['warnings']
    for segment in _children(run_root, run_root, warnings):
        if not SEGMENT.fullmatch(segment.name) or not segment.is_dir():
            continue
        if _within(segment, run_root) is None:
            warnings.append('运行段引用越出指定局目录，已跳过。')
            continue
        summary = _read_json(segment / 'summary.json', run_root, warnings) or {}
        # Summary is an index only. Do not copy progress, configuration, accounts,
        # raw OCR text or the whole successful/current segment.
        index['segments'].append({'segment': segment.name, **{key: summary[key] for key in
            ('status', 'stop_reason', 'career_complete', 'completion_scope') if key in summary}})
        for turn in _children(segment, run_root, warnings):
            if not TURN.fullmatch(turn.name) or not turn.is_dir() or _within(turn, run_root) is None:
                continue
            pipeline_path = turn / 'pipeline_report.json'
            pipeline = _read_json(pipeline_path, run_root, warnings)
            report = pipeline.get('report') if isinstance(pipeline, dict) else None
            if not isinstance(report, dict) or report.get('status') not in TARGET_STATUSES:
                continue
            relative = turn.relative_to(run_root).as_posix()
            stages = report.get('stages') if isinstance(report.get('stages'), dict) else {}
            provider = report.get('state_context_provider', stages.get('state_context_provider'))
            provider = provider if isinstance(provider, dict) else {}
            failure = {'segment': segment.name, 'turn_directory': turn.name,
                'pipeline_report': relative + '/pipeline_report.json',
                **failure_details(pipeline), 'files': []}
            index['failures'].append(failure)
            entries[relative + '/pipeline_report.json'] = _json_bytes(pipeline, run_root, redactor)
            failure['files'].append(relative + '/pipeline_report.json')
            wrapper = None
            for name in ('state_context.provider.json', 'state_context.provider.source.json'):
                source = turn / 'capture' / name
                data = _read_json(source, run_root, warnings)
                if data is not None:
                    archive_name = relative + '/capture/' + name
                    entries[archive_name] = _json_bytes(data, run_root, redactor)
                    failure['files'].append(archive_name)
                    if name == 'state_context.provider.json':
                        wrapper = data
            image = turn / 'capture/state_context.source_frame.png'
            if image.is_file():
                raw = _image_bytes(image, run_root, warnings)
                if raw is not None:
                    archive_name = relative + '/capture/state_context.source_frame.png'
                    entries[archive_name] = raw
                    failure['files'].append(archive_name)
                    failure['image_basis'] = 'persisted_provider_source_frame'
            if not failure.get('image_basis'):
                # Only a literal source reference in this failed provider is
                # eligible. Never substitute the newest frame or another turn.
                references = [provider.get('source_screenshot_path')]
                for data in (provider.get('report'), wrapper):
                    if isinstance(data, dict) and isinstance(data.get('source'), dict):
                        references.append(data['source'].get('path'))
                for value in references:
                    if not isinstance(value, str) or not value:
                        continue
                    source = Path(value)
                    source = source if source.is_absolute() else turn / 'capture' / source
                    raw = _image_bytes(source, run_root, warnings)
                    if raw is not None:
                        suffix = '.png' if raw.startswith(b'\x89PNG') else '.jpg'
                        archive_name = relative + '/capture/source-original' + suffix
                        entries[archive_name] = raw
                        failure['files'].append(archive_name)
                        failure['image_basis'] = 'explicit_failed_provider_reference'
                        failure['image_source'] = str(source)
                        break
                if not failure.get('image_basis'):
                    failure['image_basis'] = 'unavailable_no_replacement_or_capture'
            if report.get('status') == 'training_page_observation_failed':
                _observation_evidence(report, turn, run_root, entries, failure, warnings, redactor)
    try:
        from desktop.runtime_stop_reports import append_stop_export
        append_stop_export(panel_root,run_root,index,entries)
    except Exception:
        index['warnings'].append('保存的异常停机图片暂未加入导出；既有诊断仍保留。')
    index['status'] = 'matched' if index['failures'] or index.get('stop_evidence') else 'no_matching_failure'
    if not index['failures'] and not index.get('stop_evidence'):
        warnings.append('现有完整 JSON 中没有找到目标错误；没有推测或编造停机原因。')
    return index, entries, run_root


def export_report(panel_root, *, run_directory=None, run_id=None, output_dir=None, redactor=None, registered_run_directory=False, diagnostic_context=None):
    panel_root = _resolved(panel_root)
    if redactor is None:
        redactor = _load_redactor(panel_root)
    index, entries, run_root = collect(panel_root, run_directory=run_directory, run_id=run_id,
        redactor=redactor, registered_run_directory=registered_run_directory)
    output = _resolved(output_dir) if output_dir is not None else _resolved(panel_root / 'logs/diagnostic-exports')
    if run_root is not None and output.is_relative_to(run_root):
        raise ExportError('输出目录不能位于原局运行目录内；请另选可写文件夹。')
    if output_dir is None and not output.is_relative_to(panel_root):
        raise ExportError('默认导出目录指向面板之外；请用 --output-dir 明确指定可写文件夹。')
    try:
        output.mkdir(parents=True, exist_ok=True)
        name = 'maauma-stop-report-' + index['run_id'] + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '-' + uuid.uuid4().hex[:8] + '.zip'
        destination = output / name
        scope = run_root if run_root is not None else panel_root
        if isinstance(diagnostic_context,dict):
            index['outer_control_context']=diagnostic_context
            entries['control-context.json']=_json_bytes(diagnostic_context,scope,redactor)
        entries['index.json'] = _json_bytes(index, scope, redactor)
        # Exclusive creation: never replace an existing file, including on retry.
        with destination.open('xb') as stream:
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for path, payload in entries.items():
                    archive.writestr(path, payload)
        return destination, index
    except (PermissionError, OSError) as error:
        raise ExportError('无法写入导出目录。可重新运行并指定 --output-dir "可写文件夹"；没有改动原运行记录。') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读导出指定育成局的旧停机证据，不连接模拟器、不上传。')
    parser.add_argument('run_directory', nargs='?', help='可拖入单个 larc_panel_... 运行目录')
    script_dir = Path(__file__).resolve().parent
    default_panel = next((path for path in (script_dir, script_dir.parent)
        if (path / 'runtime/python.exe').is_file()), script_dir)
    parser.add_argument('--panel-root', default=str(default_panel))
    parser.add_argument('--run-id', help='默认本次停机局号；也可指定其他明确局号')
    parser.add_argument('--output-dir', help='可选的可写导出目录，须位于原局目录外')
    args = parser.parse_args(argv)
    try:
        path, index = export_report(args.panel_root, run_directory=args.run_directory,
            run_id=args.run_id, output_dir=args.output_dir)
    except ExportError as error:
        print(str(error), file=sys.stderr)
        return 2
    display = str(path)
    if display.startswith('\\\\?\\UNC\\'):
        display = '\\\\' + display[8:]
    elif display.startswith('\\\\?\\'):
        display = display[4:]
    print('已生成只读停机报告：' + display)
    print('找到目标失败回合：' + str(len(index['failures'])))
    for message in index['warnings']:
        print('提示：' + message)
    print('没有暂停任务、补拍截图或自动上传。请查看压缩包后发给维护者。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
