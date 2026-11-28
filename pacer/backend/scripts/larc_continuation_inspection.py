"""Current-screen inspection. Navigation only; no training, purchase or races."""
import json
from pathlib import Path
import re
import sys
import time
import os

import cv2
import numpy as np

from core.auto_trainer.cut_farming import atomic_json
from core.auto_trainer.main_menu_state_provider import provide_main_menu_state
from scripts.larc_continuation_collect import read, bond_facts_from_pages


def supporter_points(image, *, reader=None):
    from core.auto_trainer.cut_condition_probe import ocr_tokens
    small = cv2.resize(image, (720, 1280))
    tokens = ocr_tokens(small[:600, :260], reader)
    labels = [t for t in tokens if re.sub(r'\s+', '', t['text']) in {'协助者积分', '協助者積分'}]
    if len(labels) != 1:
        return None
    label = labels[0]['bbox']
    candidates = []
    for token in tokens:
        text = re.sub(r'\s+', '', token['text'])
        match = re.fullmatch(r'([0-9][0-9,，]*)[pP][tT]', text)
        if not match and re.fullmatch(r'[0-9][0-9,，]*', text):
            # OCR can split "16,210" and "pt" into overlapping boxes.
            # Bind the unit to this number's row, never to the label's Pt icon
            # or to an unrelated number elsewhere on the screen.
            box = token['bbox']; cy = (box[1] + box[3]) / 2
            units = [t for t in tokens if re.sub(r'\s+', '', t['text']).lower() == 'pt'
                and -20 <= t['bbox'][0] - box[2] <= 60
                and abs((t['bbox'][1] + t['bbox'][3]) / 2 - cy) <= 20]
            if len(units) == 1:
                match = re.fullmatch(r'([0-9][0-9,，]*)', text)
        if match and label[3] <= token['bbox'][1] <= label[3] + 100:
            candidates.append(int(match[1].replace(',', '').replace('，', '')))
    return candidates[0] if len(candidates) == 1 else None


_DISTANCE_LABELS = {
    'short': {'短距离', '短距', '短距離'},
    'mile': {'英里'},
    'medium': {'中距离', '中距', '中距離'},
    'long': {'长距离', '长距', '長距離', '長距'},
}
SURFACE_LABELS = {
    'turf': {'草地', '草场', '芝'},
    'dirt': {'泥地', '沙地', 'ダート'},
}


def _extract_labeled_ranks(tokens, labels):
    result = {}
    for key, names in labels.items():
        for token in tokens:
            compact = re.sub(r'\s+', '', token['text'])
            joined = next((compact[len(name):] for name in names
                           if compact.startswith(name) and compact[len(name):] in tuple('GFEDCBAS')), None)
            if joined:
                result[key] = joined
                continue
            if token['text'] not in names:
                continue
            box = token['bbox']; cy = (box[1] + box[3]) / 2
            ranks = [t for t in tokens if t['text'] in tuple('GFEDCBAS') and
                     t['bbox'][0] >= box[2] and t['bbox'][0] - box[2] < 95 and
                     abs((t['bbox'][1] + t['bbox'][3]) / 2 - cy) < 20]
            if len(ranks) == 1:
                result[key] = ranks[0]['text']
    return result


_APTITUDE_PILL_COLS = {
    'short': (159, 276),
    'mile': (291, 408),
    'medium': (423, 541),
    'long': (555, 673),
}
_DETAIL_TITLE_TOKENS = {'能力详情', '能力詳情', '优俊少女详情', '優駿少女詳情', '育成优俊少女详情'}
_RANK_CONFUSION_MAP = {
    '5': 'S',
    '$': 'S',
    '8': 'B',
    '6': 'G',
    '0': 'D',
    'O': 'D',
    '(': 'C',
}


def _classify_rank_color(crop):
    if crop is None or crop.size == 0 or crop.ndim != 3:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0]
    s = hsv[:, :, 1]
    v = hsv[:, :, 2]
    sat_mask = (s >= 60) & (v >= 55)
    sat_n = int(np.count_nonzero(sat_mask))
    if sat_n >= 30:
        h_vals = h[sat_mask]
        s_med = float(np.median(s[sat_mask]))
        pink_ratio = float(np.count_nonzero((h_vals >= 158) | (h_vals <= 5))) / sat_n
        if pink_ratio >= 0.55 and s_med >= 70.0:
            return 'B'
        h_med = float(np.median(h_vals))
        if 6.0 <= h_med <= 16.0 and s_med >= 115.0:
            return 'A'
        if 17.0 <= h_med <= 34.0 and s_med >= 130.0:
            return 'S'
        if 40.0 <= h_med <= 78.0 and s_med >= 70.0:
            return 'C'
        if 88.0 <= h_med <= 112.0 and s_med >= 85.0:
            return 'D'
        if 113.0 <= h_med <= 133.0 and s_med >= 65.0:
            return 'F'
        if 134.0 <= h_med <= 157.0 and s_med >= 70.0:
            return 'E'
    grey_n = int(np.count_nonzero((s <= 45) & (v >= 70) & (v <= 195)))
    if sat_n < 30 and grey_n >= 70:
        return 'G'
    return None


def _pill_rank_roi(key, box, labels, row_y=None):
    if box is not None:
        lx1 = int(box[2])
        y0 = max(0, int(box[1]) - 2)
        y1 = min(1280, int(box[3]) + 2)
    elif row_y is not None:
        lx1 = None
        y0, y1 = row_y
    else:
        return None
    if y1 <= y0:
        return None
    if row_y is not None and labels is _DISTANCE_LABELS and key in _APTITUDE_PILL_COLS:
        pill_x0, pill_x1 = _APTITUDE_PILL_COLS[key]
        if lx1 is None or abs(((box[0] + lx1) / 2.0) - ((pill_x0 + pill_x1) / 2.0)) < 90.0:
            x1 = min(720, pill_x1 - 4)
            default_x0 = max(pill_x0 + 4, pill_x1 - 40)
            if lx1 is not None and lx1 + 2 < x1 - 12:
                x0 = max(lx1 + 2, default_x0)
            else:
                x0 = default_x0
            return (x0, y0, x1, y1) if x1 > x0 else None
    if lx1 is None:
        return None
    x0, x1 = lx1 + 4, min(720, lx1 + 42)
    return (x0, y0, x1, y1) if x1 > x0 else None


def _norm_pad_crop(crop, pad):
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    norm = np.clip((gray.astype(np.float32) - 80.0) * (255.0 / 155.0), 0.0, 255.0).astype(np.uint8)
    bgr = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)
    if pad <= 0:
        return bgr
    return cv2.copyMakeBorder(bgr, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))


def _decode_rank_text(raw_text, color_rank):
    cleaned = re.sub(r'\s+', '', raw_text or '').upper()
    if len(cleaned) > 1:
        letters = [ch for ch in cleaned if ch in 'GFEDCBAS']
        if len(letters) == 1:
            cleaned = letters[0]
    if cleaned in tuple('GFEDCBAS'):
        return cleaned, False
    if color_rank and cleaned in _RANK_CONFUSION_MAP and _RANK_CONFUSION_MAP[cleaned] == color_rank:
        return color_rank, True
    return None, False


def _upscaled_pad_crop(crop, pad):
    padded = _norm_pad_crop(crop, pad)
    return cv2.resize(padded, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)


def _read_rank_crop(crop, rank_reader, confidence_is_valid, *, require_color=False):
    if crop is None or crop.size == 0:
        return None
    color_rank = _classify_rank_color(crop)
    variants = (crop, _norm_pad_crop(crop, 4), _norm_pad_crop(crop, 6), _upscaled_pad_crop(crop, 6))
    for variant in variants:
        text, confidence = rank_reader.read_line(variant)
        if not confidence_is_valid(confidence):
            continue
        letter, from_confusion = _decode_rank_text(text, color_rank)
        if not letter:
            continue
        if color_rank == letter and confidence >= 0.50:
            return letter
        if not require_color and not from_confusion and color_rank is None and confidence >= 0.95:
            return letter
    return None


def _find_label_token(tokens, names):
    exact = [t for t in tokens if re.sub(r'\s+', '', t['text']) in names]
    if len(exact) == 1:
        return exact[0]
    prefixed = [
        t for t in tokens
        if any(
            re.sub(r'\s+', '', t['text']).startswith(name)
            and len(re.sub(r'\s+', '', t['text'])) <= len(name) + 2
            for name in names
        )
    ]
    if len(prefixed) == 1:
        return prefixed[0]
    return None


def _distance_row_bounds(tokens):
    boxes = []
    for key, names in _DISTANCE_LABELS.items():
        tok = _find_label_token(tokens, names)
        if tok is None:
            continue
        b = tok['bbox']
        cy = (b[1] + b[3]) / 2.0
        cx = (b[0] + b[2]) / 2.0
        pill_x0, pill_x1 = _APTITUDE_PILL_COLS[key]
        if 380.0 <= cy <= 540.0 and abs(cx - ((pill_x0 + pill_x1) / 2.0)) < 90.0:
            boxes.append(b)
    if boxes:
        y_top = int(round(float(np.median([b[1] for b in boxes])))) - 2
        y_bot = int(round(float(np.median([b[3] for b in boxes])))) + 2
        return (max(0, y_top), min(1280, y_bot))
    if any(re.sub(r'\s+', '', t['text']) in _DETAIL_TITLE_TOKENS for t in tokens):
        return (446, 473)
    return None


def _read_labeled_ranks(image, tokens, labels, *, rank_reader=None):
    result = _extract_labeled_ranks(tokens, labels)
    if rank_reader is None:
        from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
        rank_reader = _CALENDAR_REGION_READER
    from core.auto_trainer.main_menu_state_provider import _confidence_is_valid
    small = cv2.resize(image, (720, 1280))
    row_y = _distance_row_bounds(tokens) if labels is _DISTANCE_LABELS else None
    for key, names in labels.items():
        if key in result:
            continue
        match = _find_label_token(tokens, names)
        roi = _pill_rank_roi(key, match['bbox'] if match is not None else None, labels, row_y=row_y)
        if roi is None:
            continue
        x0, y0, x1, y1 = roi
        letter = _read_rank_crop(
            small[y0:y1, x0:x1],
            rank_reader,
            _confidence_is_valid,
            require_color=(match is None),
        )
        if letter:
            result[key] = letter
    return result


def aptitude_tokens(tokens):
    return _extract_labeled_ranks(tokens, _DISTANCE_LABELS)


def read_aptitudes(image, tokens, *, rank_reader=None):
    """Recover missed rank letters beside an observed distance label only."""
    return _read_labeled_ranks(image, tokens, _DISTANCE_LABELS, rank_reader=rank_reader)


def surface_aptitude_tokens(tokens):
    return _extract_labeled_ranks(tokens, SURFACE_LABELS)


def read_surface_aptitudes(image, tokens, *, rank_reader=None):
    """Recover missed rank letters beside an observed surface label (turf/dirt)."""
    return _read_labeled_ranks(image, tokens, SURFACE_LABELS, rank_reader=rank_reader)


def inspect_aptitudes(ctx, output, *, frames=None, token_reader=None, sleeper=time.sleep, required_keys=None):
    from core.auto_trainer.cut_condition_probe import locate_ability_button, ocr_tokens
    from core.execution_frames import ExecutionFrames
    from core.native_capture_adb import adb_for_capture
    frames = frames or ExecutionFrames(adb_for_capture(ctx.port))
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    if not frames.connect():
        raise ValueError('continuation_details_connect_failed')
    before = output / 'before.png'
    image = frames.screenshot(str(before))
    if image is None:
        raise ValueError('continuation_details_before_missing')
    button = locate_ability_button(before.read_bytes())
    if button.get('abstained'):
        raise ValueError('continuation_ability_button_unknown')
    from core.auto_trainer.condition_probe import ConditionProbeExecutor
    point = ConditionProbeExecutor()._scaled_tap('main_menu', 'ability_details', image.shape[1::-1])
    if frames.tap(point['x'], point['y'], scaled=False) is not True:
        raise ValueError('continuation_details_open_unconfirmed')
    expected_keys = set(required_keys) if required_keys else set(_DISTANCE_LABELS)
    values = {}
    last_close = None
    last_image = None
    for _ in range(6):
        sleeper(.2)
        path = output / 'details.png'; image = frames.screenshot(str(path))
        if image is None:
            continue
        tokens = ocr_tokens(cv2.resize(image, (720, 1280)), token_reader)
        if not any(t['text'] in _DETAIL_TITLE_TOKENS for t in tokens):
            continue
        close = [t for t in tokens if t['text'] in {'关闭', '關閉'}]
        if len(close) != 1:
            continue
        last_close = close[0]
        last_image = image
        values.update({k: v for k, v in read_aptitudes(image, tokens).items() if k not in values})
        if expected_keys.issubset(values):
            break
        for _retry in range(2):
            sleeper(.2)
            retry_img = frames.screenshot(str(path))
            if retry_img is None:
                continue
            retry_tokens = ocr_tokens(cv2.resize(retry_img, (720, 1280)), token_reader)
            retry_close = [t for t in retry_tokens if t['text'] in {'关闭', '關閉'}]
            if any(t['text'] in _DETAIL_TITLE_TOKENS for t in retry_tokens) and len(retry_close) == 1:
                last_close = retry_close[0]
                last_image = retry_img
            values.update({k: v for k, v in read_aptitudes(retry_img, retry_tokens).items() if k not in values})
            if expected_keys.issubset(values):
                break
        break
    if last_close is None or last_image is None:
        raise ValueError('continuation_details_page_unknown')
    box = last_close['bbox']; scale = last_image.shape[1] / 720
    point = (round((box[0] + box[2]) / 2 * scale), round((box[1] + box[3]) / 2 * scale))
    if frames.tap(*point, scaled=False) is not True:
        raise ValueError('continuation_details_close_unconfirmed')
    for _ in range(6):
        sleeper(.2)
        returned = output / 'returned.png'; image = frames.screenshot(str(returned))
        if image is None:
            continue
        provider = provide_main_menu_state(returned, output / 'returned.json',
            turn_index=ctx.progress['next_turn'], scenario='larc')
        if provider.get('valid_for_training') is True:
            return {'source': str(returned), 'provider': provider, 'distances': values}
    raise ValueError('continuation_details_return_unknown')


def inspect_junior(ctx, source, provider, output, config, facts, *, section_name='junior'):
    image = cv2.imread(str(source))
    if image is not None:
        facts['supporter_points'] = supporter_points(image)
        from core.larc_outing_reader import read_friend_outing_hint
        hint = read_friend_outing_hint(image)
        outing = read(Path(ctx.root) / 'session/larc_friend_outing.json')
        if outing.get('run_session_id') in (None, ctx.run_id) and outing.get('unlocked') is True:
            facts['friend_outing_unlocked'] = True
        elif hint.get('available') is True and any(card['role'] == 'friend' for card in facts['supports']):
            facts['friend_outing_unlocked'] = True
    from core.auto_trainer.larc_continuation import custom_date_entry
    section = custom_date_entry(config, section_name) or config.get(section_name, {})
    needs_bonds = any(row['kind'] in {'bond_ready_min', 'bond_card_min'}
                      for row in section.get('conditions', []))
    visited = False
    if needs_bonds:
        from scripts.capture_ug_parent_live_tabs import main as capture
        from core.larc_support_reader import LarcSupportReader
        group = f'cj_{ctx.progress["next_turn"]:03d}'
        context_path = output / 'state.json'; atomic_json(context_path, provider['state_context'])
        # Manifest run_session_id carries identity; directory names need not
        # repeat it. Deep normal run paths otherwise exceed Win32 MAX_PATH.
        capture_root = Path(ctx.root) / 'cj' / f'{time.time_ns():x}'[-8:]
        argv = ['--output-root', str(capture_root), '--actual-group-id', group,
            '--run-session-id', ctx.run_id, '--turn-index', str(ctx.progress['next_turn']),
            '--source-state-key', 'main_menu', '--operator-id', 'larc_continuation',
            '--state-context-json', str(context_path), '--transition-session-json', str(ctx.route),
            '--adb-port', str(ctx.port), '--capture-transport', 'auto',
            '--live', '--execute', '--confirm-token', 'CAPTURE_UG_PARENT_LIVE_TABS']
        import contextlib
        import io
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = capture(argv)
        lines = captured.getvalue().splitlines()
        report = next((json.loads(line) for line in reversed(lines)
                       if line.startswith('{') and line.endswith('}')), {})
        atomic_json(output / 'bond_capture_report.json', report)
        if code:
            return {'ok': False, 'reason': 'continuation_bond_capture_failed',
                    'capture_errors': report.get('errors'), 'capture_root': str(capture_root)}
        pages = []
        reader = LarcSupportReader()
        for name in ('speed', 'stamina', 'power', 'guts', 'wisdom'):
            # A successful capture also publishes normalized copies. Those
            # are not additional independent observations of the card bonds.
            path = capture_root / 'raw_720' / (name + '.png')
            if path.is_file():
                frame = cv2.imread(str(path))
                if frame is not None:
                    pages.append(reader.read(frame))
        facts['supports'] = bond_facts_from_pages(facts['supports'], pages)
        returned = ctx.continuation_return_menu(output / 'return')
        source = returned[0] if isinstance(returned, tuple) else returned
        if not source:
            return {'ok': False, 'reason': 'continuation_bond_return_failed'}
        provider = provide_main_menu_state(source, output / 'after_bonds.json',
            turn_index=ctx.progress['next_turn'], scenario='larc')
        visited = True
    return {'ok': True, 'source': source, 'provider': provider, 'visited': visited}


def inventory_facts(inventory, catalog):
    names = {}
    rows = catalog.get('skills', [])
    if isinstance(rows, dict):
        rows = [{'id': key, **value} for key, value in rows.items()]
    for row in rows:
        identity = str(row.get('id', row.get('skill_id', '')))
        names[identity] = row.get('name_zh_cn', row.get('name_cn', row.get('name', row.get('name_zh', ''))))
    result = []
    packet = inventory.get('schema_version') == 'uma.skill_inventory_packet.v1'
    rejected = {str(row['skill_id']) for row in inventory.get('abstentions', [])
                if isinstance(row, dict) and row.get('skill_id')}
    for state, rows in (('learned', inventory.get('owned_skill_ids' if packet else 'owned_skills', [])),
                        ('available', inventory.get('candidates' if packet else 'available_skills', []))):
        for row in rows:
            identity = str(row.get('skill_id', row.get('id', ''))) if isinstance(row, dict) else str(row)
            if identity and identity not in rejected:
                observed_name = row.get('display_name', row.get('name', '')) if isinstance(row, dict) else ''
                name = observed_name or names.get(identity, '')
                if name:
                    result.append({'id': identity, 'name': name, 'state': state})
    return result


def inspect_inheritance(ctx, source, provider, output, config, facts, *, section_name='inheritance'):
    section = config[section_name]
    visited = False
    if section['distances']:
        result = inspect_aptitudes(ctx, output / 'aptitude', required_keys=set(section['distances']))
        source, provider = result['source'], result['provider']
        facts['distances'] = result['distances']
        visited = True
    from core.auto_trainer.larc_continuation import evaluate_inheritance, evaluate_inheritance2
    evaluator = evaluate_inheritance2 if section_name == 'inheritance2' else evaluate_inheritance
    if section['skills'] and evaluator(config, facts)['status'] != 'passed':
        from core.auto_trainer.larc_skill_driver import load_driver
        from scripts.larc_autonomy_support_skills import StartupNavigation
        from core.auto_trainer.controlled_process import run_controlled_process
        driver = load_driver(ctx.root)
        if not driver:
            return {'ok': False, 'reason': 'continuation_skill_driver_missing'}
        nav = StartupNavigation(ctx)
        if not nav.open(source, output / 'entry'):
            return {'ok': False, 'reason': 'continuation_skill_entry_unknown'}
        visited = True
        folder = Path(ctx.root) / 'ci' / f'{time.time_ns():x}'[-8:]
        argv = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).with_name('larc_continuation_skill_worker.py')),
            '--run-root', str(ctx.root), '--output-dir', str(folder),
            '--d1-root', driver['d1_root'], '--external-skill-db-path', driver['external_skill_db_path'],
            '--catalog-dir', driver.get('startup_skill_catalog_dir', ''),
            '--endpoint', f'127.0.0.1:{ctx.port}', '--execute-readonly']
        env = {**os.environ, 'UMA_ISOLATED_SKILL_WORKER': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        output.mkdir(parents=True, exist_ok=True)
        child = run_controlled_process(
            argv,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=env,
            startup_timeout=35.0,
            startup_probe=lambda: (folder / 'report.json').is_file() or (folder.is_dir() and any(folder.iterdir())),
            total_timeout=180.0,
            diagnostics_dir=output,
        )
        proc_payload = {
            'returncode': child.returncode,
            'stdout': child.stdout,
            'stderr': child.stderr,
            'startup_stalled': bool(getattr(child, 'startup_stalled', False)),
            'timed_out': bool(getattr(child, 'timed_out', False)),
            'diagnostics_path': getattr(child, 'diagnostics_path', None),
        }
        atomic_json(output / 'skill_worker_process.json', proc_payload)
        report_path = folder / 'report.json'
        if not report_path.is_file() and (not folder.exists() or not any(folder.iterdir())):
            atomic_json(output / 'skill_worker_process.attempt1.json', proc_payload)
            folder = Path(ctx.root) / 'ci' / f'{time.time_ns():x}'[-8:]
            argv[argv.index('--output-dir') + 1] = str(folder)
            child = run_controlled_process(
                argv,
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=env,
                startup_timeout=35.0,
                startup_probe=lambda: (folder / 'report.json').is_file() or (folder.is_dir() and any(folder.iterdir())),
                total_timeout=180.0,
                diagnostics_dir=output,
            )
            proc_payload = {
                'returncode': child.returncode,
                'stdout': child.stdout,
                'stderr': child.stderr,
                'startup_stalled': bool(getattr(child, 'startup_stalled', False)),
                'timed_out': bool(getattr(child, 'timed_out', False)),
                'diagnostics_path': getattr(child, 'diagnostics_path', None),
            }
            atomic_json(output / 'skill_worker_process.json', proc_payload)
            report_path = folder / 'report.json'
        if not report_path.is_file():
            try:
                source, provider = nav.return_menu(output / 'return')
            except Exception:
                pass
            stalled = bool(getattr(child, 'startup_stalled', False))
            return {
                'ok': False,
                'reason': 'continuation_skill_worker_startup_stalled' if stalled else 'continuation_skill_worker_report_missing',
                'returncode': child.returncode,
                'startup_stalled': stalled,
            }
        result = read(report_path)
        source, provider = nav.return_menu(output / 'return')
        if result.get('ok') is not True:
            return {'ok': False, 'reason': result.get('reason', 'continuation_skill_inspection_failed')}
        inventory = read(result['inventory_path'])
        facts['skills'] = inventory_facts(inventory, read(result['catalog_path']))
        facts['skills_abstentions'] = inventory.get('abstentions', [])
        packet = inventory.get('schema_version') == 'uma.skill_inventory_packet.v1'
        count = len(inventory.get('owned_skill_ids' if packet else 'owned_skills', [])) + len(
            inventory.get('candidates' if packet else 'available_skills', []))
        facts['skills_unresolved_count'] = max(0, count - len(facts['skills']))
        facts['skills_complete'] = (result.get('complete') is True
            and not facts['skills_abstentions'] and facts['skills_unresolved_count'] == 0)
    return {'ok': True, 'source': source, 'provider': provider, 'visited': visited}
