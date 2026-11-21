"""In-career deck inspection: home menu -> 编成信息 -> six support cards.

Pure vision + profile persistence; no device I/O (the route lives in
scripts/larc_autonomy_deck.py). All geometry is in the 720x1280 frame space;
other frames with the same 9:16 aspect are resized first, while level-digit OCR
reads native-resolution crops when supplied for maximum sharpness.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess

import cv2
import numpy as np

CARD_TYPES = ('speed', 'stamina', 'power', 'guts', 'wisdom', 'friend', 'group')
RARITIES = ('R', 'SR', 'SSR')
LEVEL_CAPS = {
    'R': (20, 25, 30, 35, 40),
    'SR': (25, 30, 35, 40, 45),
    'SSR': (30, 35, 40, 45, 50),
}
SLOTS = 6
BORROWED_SLOT = 5

# Tap points (720x1280). Calibrated from the recorded 942x1674 frames.
FORMATION_BUTTON = (195, 696)       # 菜单 -> 编成信息
FORMATION_BUTTON_BOX = (60, 664, 330, 728)
FORMATION_CLOSE = (359, 1183)       # 编成信息 -> 关闭
MENU_CLOSE = (359, 917)             # 菜单 -> 关闭
MENU_CLOSE_BOX = (230, 890, 490, 945)
FORMATION_CLOSE_BOX = (230, 1155, 490, 1210)

# Six-card row geometry in 720x1280 space.
_ICON_X = tuple(round((160 + 142.4 * i) * 720 / 942) for i in range(SLOTS))
_ICON_Y = (640, 685)
_LEVEL_Y = (738, 780)
_SLOT_X0 = tuple(round(35 + 108.84 * i) for i in range(SLOTS))
_ART_X0 = tuple(round(37 + 108.2 * i) for i in range(SLOTS))
_ART_Y0, _ART_W, _ART_H = 646, 96, 128
_CATALOG_TYPE_ALIASES = {'intelligence': 'wisdom'}


def normalize(image):
    """Return a 720x1280 BGR frame, or None for an unsupported shape."""
    if image is None or image.ndim != 3:
        return None
    height, width = image.shape[:2]
    if (width, height) == (720, 1280):
        return image
    if width <= 0 or abs(height / width - 1280 / 720) > .01:
        return None
    return cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)


def _compact(text):
    return ''.join(str(text or '').split())


def _light_box(image, box, *, minimum=.6):
    x0, y0, x1, y1 = box
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    return float(((hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 200)).mean()) >= minimum


def _green_header(image, y0, y1):
    hsv = cv2.cvtColor(image[y0:y1, 40:680], cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] >= 30) & (hsv[:, :, 0] <= 60) & (hsv[:, :, 1] > 110) & (hsv[:, :, 2] > 120)
    return float(green.mean()) >= .45


def _classify_icon(hue, ring_white, center_white):
    """Background hue decides the family; ring-vs-center white separates inversions.

    Stat badges have a colored square border and a white center glyph;
    friend/group badges have a white square background and a colored silhouette.
    """
    inverted = ring_white >= .08 and ring_white > center_white
    if 92 <= hue <= 130:
        return 'speed'
    if hue >= 175 or hue <= 9:
        return 'stamina'
    if 148 <= hue <= 174:
        return 'guts'
    if 60 <= hue <= 91:
        return 'wisdom'
    if 34 <= hue <= 59:
        return 'group' if inverted else 'wisdom'
    if 10 <= hue <= 33:
        return 'friend' if inverted else 'power'
    return None


_RING_MASK = np.ones((18, 18), dtype=bool)
_RING_MASK[4:14, 4:14] = False
_CENTER_MASK = ~_RING_MASK


def _icon_box_stats(hsv, x0, y0, size):
    patch = hsv[y0:y0 + size, x0:x0 + size].astype(int)
    saturated = (patch[:, :, 1] > 90) & (patch[:, :, 2] > 120)
    white = (patch[:, :, 1] < 75) & (patch[:, :, 2] > 195)
    cream = (patch[:, :, 1] <= 90) & (patch[:, :, 2] > 180)
    hues = patch[:, :, 0][saturated]
    if hues.size < 8 or float(saturated.mean()) < .25 or float(white.mean()) < .08:
        return None
    histogram = np.bincount(hues, minlength=180).astype(float)
    window = np.convolve(np.concatenate([histogram[-8:], histogram, histogram[:8]]), np.ones(17), 'valid')
    mode = int(np.argmax(window))
    distance = np.minimum(abs(hues - mode), 180 - abs(hues - mode))
    return {'hue': mode, 'purity': float((distance <= 10).mean()),
            'coverage': float((saturated | white | cream).mean()),
            'white': float(white.mean()),
            'ring_white': float(white[_RING_MASK].mean()),
            'center_white': float(white[_CENTER_MASK].mean())}


def read_slot_icon(image, slot):
    """Classify one type icon (~22px rounded square at the card's top-right).

    Fixed geometry with a +-4px search: card artwork touches the icon, so a
    connected-component crop is unreliable; a pure, fully colored box is not.
    """
    cx, size = _ICON_X[slot], 18
    hsv = cv2.cvtColor(image[_ICON_Y[0]:_ICON_Y[1], :], cv2.COLOR_BGR2HSV)
    best = None
    for dy in range(-4, 5, 2):
        for dx in range(-4, 5, 2):
            x0, y0 = cx + dx - size // 2, 661 - _ICON_Y[0] + dy - size // 2
            stats = _icon_box_stats(hsv, x0, y0, size)
            if stats is None:
                continue
            score = stats['purity'] * stats['coverage']
            if best is None or score > best[0]:
                best = (score, x0, y0 + _ICON_Y[0], stats)
    if best is None or best[3]['purity'] < .55 or best[3]['coverage'] < .75:
        return {'slot': slot, 'type': None, 'reason': 'icon_not_found',
                **({'hue': best[3]['hue'], 'purity': round(best[3]['purity'], 3),
                    'coverage': round(best[3]['coverage'], 3)} if best else {})}
    _, x0, y0, stats = best
    kind = _classify_icon(stats['hue'], stats['ring_white'], stats['center_white'])
    return {'slot': slot, 'type': kind, 'hue': stats['hue'], 'white': round(stats['white'], 3),
            'ring_white': round(stats['ring_white'], 3),
            'center_white': round(stats['center_white'], 3),
            'purity': round(stats['purity'], 3), 'bbox': [x0, y0, size, size],
            'reason': None if kind else 'icon_hue_unclassified'}


def read_slot_rarity(frame, slot):
    """Classify top-left rarity badge: rainbow 'SSR', gold 'SR', or silver 'R'."""
    x0 = _SLOT_X0[slot]
    patch = frame[647:665, x0 + 2:x0 + 32]
    if patch.size == 0:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    sat = (hsv[:, :, 1] >= 55) & (hsv[:, :, 2] >= 90)
    sat_ratio = float(sat.mean())
    silver = (hsv[:, :, 1] < 55) & (hsv[:, :, 2] >= 125)
    if sat_ratio < .18 and float(silver.mean()) >= .35:
        return 'R'
    if int(sat.sum()) < 25:
        return None
    hues = hsv[:, :, 0][sat]
    gold = float(((hues >= 12) & (hues <= 35)).mean())
    purple_cyan = float(((hues >= 85) & (hues <= 170)).mean())
    if gold >= .55 and purple_cyan < .25:
        return 'SR'
    if purple_cyan >= .35:
        return 'SSR'
    return None


def _min_breaks_for_level(rarity, level):
    caps = LEVEL_CAPS.get(rarity)
    if caps is None or type(level) is not int or not 1 <= level <= caps[-1]:
        return None
    for idx, cap in enumerate(caps):
        if level <= cap:
            return idx
    return None


def read_slot_limit_breaks(frame, slot, *, rarity=None, level=None):
    """Count lit cyan limit-break diamonds (0..4), with unique level-cap fallback."""
    x0 = _SLOT_X0[slot]
    yc, dxs = (748, (14, 25, 36, 47)) if slot == BORROWED_SLOT else (766, (12, 21, 30, 39))
    lit_flags, unlit_flags = [], []
    for dx in dxs:
        patch = frame[yc - 4:yc + 5, x0 + dx - 2:x0 + dx + 3]
        if patch.shape[:2] != (9, 5):
            lit_flags.append(False)
            unlit_flags.append(False)
            continue
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        cyan = (hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 115) & (hsv[:, :, 1] >= 75) & (hsv[:, :, 2] >= 135)
        grey = (hsv[:, :, 1] < 55) & (hsv[:, :, 2] >= 95) & (hsv[:, :, 2] <= 225)
        lit_flags.append(float(cyan.mean()) >= .35)
        unlit_flags.append(float(grey.mean()) >= .40)
    count = sum(lit_flags)
    monotonic = lit_flags == [True] * count + [False] * (4 - count)
    unlit_clean = all(unlit_flags[count:])
    min_lb = _min_breaks_for_level(rarity, level)
    observed = count if (monotonic and unlit_clean) else None
    if observed is not None and min_lb is not None and observed < min_lb:
        observed = None
    if observed is not None:
        return observed
    if min_lb == 4:
        return 4
    return None


def resolve_support_catalog_path(catalog_path=None):
    """Locate runs/support-card-library/catalog.json in packaged or worktree layouts."""
    if catalog_path is not None:
        explicit = Path(catalog_path)
        return explicit.resolve() if explicit.is_file() else None
    candidates = []
    try:
        from core.cut_portable_runtime import portable_root
        portable = portable_root()
    except Exception:
        portable = None
    if portable is not None:
        candidates.extend((
            portable / 'backend/runs/support-card-library/catalog.json',
            portable / 'runs/support-card-library/catalog.json',
            portable.parent / 'runs/support-card-library/catalog.json',
        ))
    for ancestor in Path(__file__).resolve().parents:
        candidates.append(ancestor / 'runs/support-card-library/catalog.json')
        candidates.append(ancestor / 'backend/runs/support-card-library/catalog.json')
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _canonical_character_id(card_id_str, raw_character_id):
    if card_id_str == '30160' or raw_character_id in (9043, '9043', 'satake_mei'):
        return 'satake_mei'
    if raw_character_id in (None, '', 'unknown'):
        return None
    return str(raw_character_id)


@lru_cache(maxsize=4)
def _load_catalog_index(resolved_path_str):
    path = Path(resolved_path_str)
    catalog = json.loads(path.read_text(encoding='utf-8'))
    if catalog.get('schema') != 'uma.support_card_art.v1':
        raise ValueError('unsupported_support_card_catalog_schema')
    by_id = {}
    buckets = {}
    for entry in catalog.get('cards', ()):
        if entry.get('image_status') != 'ready':
            continue
        cid_int = int(entry['card_id'])
        cid_str = str(entry['card_id'])
        raw_type = entry.get('type')
        card_type = _CATALOG_TYPE_ALIASES.get(raw_type, raw_type)
        rarity = entry.get('rarity')
        if card_type not in CARD_TYPES or rarity not in {'SR', 'SSR'}:
            continue
        meta = {
            'card_id': cid_str,
            'character_id': _canonical_character_id(cid_str, entry.get('character_id')),
            'rarity': rarity,
            'type': card_type,
            'image_path': str(entry['image_path']),
        }
        by_id[cid_int] = meta
        buckets.setdefault((card_type, rarity), []).append(cid_int)
    return {'base_dir': path.parent, 'by_id': by_id,
            'buckets': {k: tuple(v) for k, v in buckets.items()}}


@lru_cache(maxsize=32)
def _bucket_matcher(resolved_path_str, card_type, rarity, min_score, min_margin):
    from core.support_library.art_matcher import CardArtMatcher, read_image
    index = _load_catalog_index(resolved_path_str)
    rarities = (rarity,) if rarity in {'SR', 'SSR'} else ('SSR', 'SR')
    ids = [cid for r in rarities for cid in index['buckets'].get((card_type, r), ())]
    if not ids:
        return None
    refs = {cid: read_image(index['base_dir'] / index['by_id'][cid]['image_path']) for cid in ids}
    return CardArtMatcher(refs, min_score=min_score, min_margin=min_margin)


def _subwindow_fallback(matcher, crop, *, min_score=.72, min_margin=.15):
    from core.support_library.art_matcher import CardArtMatch
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    normalized = cv2.resize(gray, (_ART_W, _ART_H), interpolation=cv2.INTER_AREA)
    for y0, y1, x0, x1 in ((34, 74, 20, 78), (40, 88, 34, 80)):
        sub = normalized[y0:y1, x0:x1]
        sh, sw = sub.shape[:2]
        if float(sub.std()) < 8:
            continue
        scores = []
        for card_id, variants in matcher.templates.items():
            best = -1.0
            for ref in variants:
                rh, rw = ref.shape[:2]
                rx, ry = round(x0 * rw / _ART_W), round(y0 * rh / _ART_H)
                wx0, wy0 = max(0, rx - 6), max(0, ry - 6)
                wx1, wy1 = min(rw, rx + sw + 6), min(rh, ry + sh + 6)
                if wx1 - wx0 < sw or wy1 - wy0 < sh:
                    continue
                score = float(cv2.matchTemplate(ref[wy0:wy1, wx0:wx1], sub, cv2.TM_CCOEFF_NORMED).max())
                best = max(best, score)
            scores.append((card_id, best))
        if not scores:
            continue
        scores.sort(key=lambda item: item[1], reverse=True)
        best_id, best_score = scores[0]
        margin = best_score - (scores[1][1] if len(scores) > 1 else 1.0)
        if best_score >= min_score and margin >= min_margin:
            return CardArtMatch('matched', best_id, 'matched_artwork_subwindow',
                                round(best_score, 4), round(margin, 4),
                                tuple((cid, round(sc, 4)) for cid, sc in scores[:3]))
    return None


def match_slot_card_art(frame, slot, *, card_type, rarity, catalog_path=None):
    """Match one 720p card portrait against the (type, rarity) catalog bucket."""
    if not card_type:
        return {'status': 'unmatched', 'card_id': None, 'character_id': None,
                'rarity': rarity, 'reason': 'missing_card_type', 'score': 0.0, 'margin': 0.0}
    if rarity == 'R':
        return {'status': 'unmatched', 'card_id': None, 'character_id': None,
                'rarity': 'R', 'reason': 'rarity_r_uncatalogued', 'score': 0.0, 'margin': 0.0}
    resolved = resolve_support_catalog_path(catalog_path)
    if resolved is None:
        return {'status': 'unmatched', 'card_id': None, 'character_id': None,
                'rarity': rarity, 'reason': 'catalog_unavailable', 'score': 0.0, 'margin': 0.0}
    ux0 = _ART_X0[slot]
    crop = frame[_ART_Y0:_ART_Y0 + _ART_H, ux0:ux0 + _ART_W]
    min_score, min_margin = (.58, .12) if rarity in {'SR', 'SSR'} else (.72, .15)
    try:
        index = _load_catalog_index(str(resolved))
        matcher = _bucket_matcher(str(resolved), card_type, rarity, min_score, min_margin)
    except Exception as exc:
        return {'status': 'unmatched', 'card_id': None, 'character_id': None,
                'rarity': rarity, 'reason': f'catalog_error:{type(exc).__name__}', 'score': 0.0, 'margin': 0.0}
    if matcher is None:
        return {'status': 'unmatched', 'card_id': None, 'character_id': None,
                'rarity': rarity, 'reason': 'empty_bucket', 'score': 0.0, 'margin': 0.0}
    match = matcher.match(crop)
    # Single-card bucket (if any) has no runner-up; margin in CardArtMatcher is best_score - 0.0.
    if match.status != 'matched':
        sub_match = _subwindow_fallback(matcher, crop)
        if sub_match is not None:
            match = sub_match
    if match.status != 'matched' or match.card_id not in index['by_id']:
        return {'status': 'unmatched', 'card_id': None, 'character_id': None,
                'rarity': rarity, 'reason': match.reason, 'score': match.score, 'margin': match.margin}
    meta = index['by_id'][match.card_id]
    return {'status': 'matched', 'card_id': meta['card_id'], 'character_id': meta['character_id'],
            'rarity': meta['rarity'], 'reason': match.reason, 'score': match.score, 'margin': match.margin}


def _read_levels_tesseract(image, target_slots, *, rarities=None):
    """Single-call stdin->stdout TSV digit OCR over native-resolution `NN` crops."""
    if image is None or image.ndim != 3 or not target_slots:
        return {}
    height, width = image.shape[:2]
    if width <= 0 or abs(height / width - 1280 / 720) > .01:
        return {}
    cell_w, cell_h = 128, 96
    cells, cell_slots = [], []
    for slot in target_slots:
        x0_720 = _SLOT_X0[slot]
        rx0 = max(0, round((x0_720 + 77) * width / 720))
        rx1 = min(width, round((x0_720 + 95) * width / 720))
        ry0 = max(0, round((739 if slot == BORROWED_SLOT else 757) * height / 1280))
        ry1 = min(height, round((757 if slot == BORROWED_SLOT else 775) * height / 1280))
        raw_crop = image[ry0:ry1, rx0:rx1]
        if raw_crop.size == 0:
            continue
        enlarged = cv2.resize(raw_crop, (72, 64), interpolation=cv2.INTER_CUBIC)
        _, binary = cv2.threshold(cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY), 165, 255, cv2.THRESH_BINARY)
        if int((binary == 0).sum()) < 20:
            continue
        cell = np.full((cell_h, cell_w), 255, dtype=np.uint8)
        cell[16:80, 28:100] = binary
        cells.append(cell)
        cell_slots.append(slot)
    if not cells:
        return {}
    ok, encoded = cv2.imencode('.png', np.hstack(cells))
    if not ok:
        return {}
    try:
        from core.skill_acquisition_parser import resolve_tesseract_runtime
        runtime = resolve_tesseract_runtime()
        completed = subprocess.run(
            [
                str(runtime.executable), 'stdin', 'stdout',
                '--tessdata-dir', str(runtime.tessdata_dir),
                '-l', 'eng', '--psm', '7',
                '-c', 'tessedit_char_whitelist=0123456789 ',
                '-c', 'tessedit_create_tsv=1',
            ],
            input=encoded.tobytes(),
            capture_output=True,
            timeout=4,
            check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except Exception:
        return {}
    if completed.returncode != 0:
        return {}
    grouped = {}
    for line in completed.stdout.decode('utf-8', errors='replace').splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 12 or parts[0] != '5' or not parts[11].strip():
            continue
        try:
            left, span = int(parts[6]), int(parts[8])
        except ValueError:
            continue
        idx = int((left + span / 2) // cell_w)
        if 0 <= idx < len(cell_slots):
            grouped.setdefault(idx, []).append((left, parts[11].strip()))
    levels = {}
    for idx, items in grouped.items():
        items.sort(key=lambda pair: pair[0])
        digits = ''.join(re.findall(r'\d+', ''.join(text for _, text in items)))
        if not digits:
            continue
        value = int(digits)
        slot = cell_slots[idx]
        rarity = (rarities or {}).get(slot)
        cap = LEVEL_CAPS[rarity][-1] if rarity in LEVEL_CAPS else 50
        if 1 <= value <= cap:
            levels[slot] = value
    return levels


def _read_levels(tokens, *, raw_image=None, frame=None, slots=None, line_reader=None):
    """Read `等级NN` from tokens, custom line_reader, or built-in native strip OCR."""
    levels = {}
    rarities = {row['slot']: row.get('rarity') for row in (slots or ()) if isinstance(row, dict)}
    occupied = [row['slot'] for row in (slots or ()) if isinstance(row, dict) and row.get('type')]
    if not occupied and frame is not None:
        occupied = list(range(SLOTS))
    for token in tokens or ():
        match = re.search(r'等级\s*(\d{1,2})', str(token.get('text', '')))
        box = token.get('bbox')
        if not match or not box or not _LEVEL_Y[0] - 10 <= box[1] <= _LEVEL_Y[1]:
            continue
        x = (box[0] + box[2]) / 2
        slot = min(range(SLOTS), key=lambda i: abs(_ICON_X[i] - 30 - x))
        value = int(match.group(1))
        cap = LEVEL_CAPS[rarities[slot]][-1] if rarities.get(slot) in LEVEL_CAPS else 50
        if 1 <= value <= cap:
            levels[slot] = value
    if line_reader is not None and frame is not None:
        for slot in occupied:
            if slot in levels:
                continue
            cx = _ICON_X[slot]
            y0, y1 = (740, 758) if slot == BORROWED_SLOT else (758, 776)
            crop = frame[y0:y1, max(0, cx - 50):min(720, cx + 10)]
            try:
                text, confidence = line_reader(crop)
            except Exception:
                continue
            match = re.search(r'(\d{1,2})', str(text)) if confidence >= .75 else None
            if match:
                value = int(match.group(1))
                cap = LEVEL_CAPS[rarities[slot]][-1] if rarities.get(slot) in LEVEL_CAPS else 50
                if 1 <= value <= cap:
                    levels[slot] = value
        return levels
    pending = [slot for slot in occupied if slot not in levels]
    if pending:
        source = raw_image if raw_image is not None else frame
        levels.update(_read_levels_tesseract(source, pending, rarities=rarities))
    return levels


def parse_formation_info(image, *, tokens=None, line_reader=None, catalog_path=None, icons_only=False):
    """Read the six 协助卡 slots (type, rarity, card_id, level, limit_breaks)."""
    frame = normalize(image)
    if frame is None:
        return {'status': 'unsupported_frame', 'slots': [], 'complete': False}
    slots = [read_slot_icon(frame, slot) for slot in range(SLOTS)]
    types = [row['type'] for row in slots]
    counts = dict.fromkeys(CARD_TYPES, 0)
    for kind in types:
        if kind:
            counts[kind] += 1
    complete = all(types)
    if icons_only:
        for row in slots:
            row['borrowed'] = row['slot'] == BORROWED_SLOT
        return {'status': 'complete' if complete else 'partial' if any(types) else 'missing',
                'types': types, 'counts': counts, 'slots': slots, 'complete': complete}
    for row in slots:
        row['borrowed'] = row['slot'] == BORROWED_SLOT
        if not row['type']:
            row.update(rarity=None, card_id=None, character_id=None, level=None, limit_breaks=None)
            continue
        row['rarity'] = read_slot_rarity(frame, row['slot'])
        art = match_slot_card_art(
            frame, row['slot'], card_type=row['type'], rarity=row['rarity'], catalog_path=catalog_path
        )
        row['card_id'] = art['card_id']
        row['character_id'] = art['character_id']
        row['art_match'] = {k: art[k] for k in ('status', 'reason', 'score', 'margin')}
        if row['rarity'] is None and art.get('rarity'):
            row['rarity'] = art['rarity']
    levels = _read_levels(
        tokens, raw_image=image, frame=frame if any(types) else None,
        slots=slots, line_reader=line_reader,
    )
    for row in slots:
        row['level'] = levels.get(row['slot']) if row['type'] else None
        row['limit_breaks'] = (
            read_slot_limit_breaks(frame, row['slot'], rarity=row.get('rarity'), level=row.get('level'))
            if row['type'] else None
        )
    return {
        'status': 'complete' if complete else 'partial' if any(types) else 'missing',
        'types': types,
        'counts': counts,
        'card_ids': [row.get('card_id') for row in slots],
        'rarities': [row.get('rarity') for row in slots],
        'levels': [row.get('level') for row in slots],
        'limit_breaks': [row.get('limit_breaks') for row in slots],
        'slots': slots,
        'complete': complete,
    }


def classify_page(image, text=None):
    """'main_menu' | 'career_menu' | 'formation_info' | None.

    OCR text (e.g. the FrameSafety packet's visible_text) is preferred when
    supplied; the pixel fallback keeps offline checks OCR-free.
    """
    frame = normalize(image)
    if frame is None:
        return None
    if text is not None:
        compact = _compact(text)
        if '编成信息' in compact and '协助卡' in compact:
            return 'formation_info'
        if '菜单' in compact and '编成信息' in compact and ('保存并退出' in compact or '放弃' in compact):
            return 'career_menu'
    if (_green_header(frame, 40, 80)
            and sum(bool(read_slot_icon(frame, slot)['type']) for slot in range(SLOTS)) >= 2
            and _light_box(frame, FORMATION_CLOSE_BOX, minimum=.45)):
        return 'formation_info'
    if _green_header(frame, 305, 345) and _light_box(frame, FORMATION_BUTTON_BOX, minimum=.45) \
            and _light_box(frame, MENU_CLOSE_BOX, minimum=.45):
        return 'career_menu'
    if _menu_icon(frame) is not None:
        return 'main_menu'
    return None


# Same three-bar detector as cut_restart_route.menu_icon_center, without the
# config/coordinates.json lookup (training_menu.menu = 1292,2426 @1440 -> 646,1213).
_MENU_ICON = (646, 1218)


def _menu_icon(frame):
    x0, y0, x1, y1 = 608, 1190, 686, 1272
    if not _light_box(frame, (x0, y0, x1, y1), minimum=.40):
        return None
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    brown = (hsv[:, :, 0] <= 35) & (hsv[:, :, 1] >= 35) & (hsv[:, :, 2] >= 35) & (hsv[:, :, 2] <= 215)
    row_counts = brown.sum(axis=1)
    runs, start = [], None
    for y, count in enumerate((*row_counts, 0)):
        if count >= 16 and start is None:
            start = y
        elif count < 16 and start is not None:
            if 2 <= y - start <= 11:
                runs.append((start, y - 1))
            start = None
    for idx in range(len(runs) - 2):
        g = runs[idx:idx + 3]
        if all(4 <= g[i + 1][0] - g[i][1] <= 15 for i in (0, 1)):
            ys = [y for a, b in g for y in range(a, b + 1)]
            xs = np.where(brown[ys])[1]
            if xs.size:
                return (x0 + round(float(xs.mean())), y0 + round(sum((a + b) / 2 for a, b in g) / 3))
    return None


def home_menu_point(image):
    frame = normalize(image)
    return None if frame is None else _menu_icon(frame)


# ---------------------------------------------------------------- persistence

def needs_inspection(profile):
    """Inspect once per run unless all six slots already have both card_id and type."""
    if not isinstance(profile, dict):
        return True
    if (profile.get('deck_inspect') or {}).get('status') in {'complete', 'partial', 'failed'}:
        return False
    cards = profile.get('cards') or []
    complete_slots = {
        card.get('slot') for card in cards
        if isinstance(card, dict) and card.get('card_id') and card.get('type')
    }
    return complete_slots != set(range(SLOTS))


def _catalog_entry_by_card_id(card_id, catalog_path=None):
    if not card_id or not str(card_id).isdigit():
        return None
    resolved = resolve_support_catalog_path(catalog_path)
    if resolved is None:
        return None
    try:
        index = _load_catalog_index(str(resolved))
    except Exception:
        return None
    return index['by_id'].get(int(card_id))


def _compatible_stats(rarity, level, limit_breaks):
    if rarity not in LEVEL_CAPS:
        return True
    caps = LEVEL_CAPS[rarity]
    if level is not None and not 1 <= level <= caps[-1]:
        return False
    if level is not None and limit_breaks is not None and level > caps[limit_breaks]:
        return False
    return True


def _merge_declared_slot(decl, obs, *, mei_taken, conflicts, catalog_path=None):
    """Preserve user-declared card_id/type; backfill missing non-conflicting fields."""
    slot = decl['slot']
    effective = deepcopy(decl)
    if effective.get('borrowed') is None:
        effective['borrowed'] = slot == BORROWED_SLOT
    field_sources = {
        k: {'kind': 'user_declared'}
        for k, v in effective.items()
        if k not in {'source', 'actual_effects', 'actual_effects_source'} and v is not None
    }
    if obs is None:
        effective['source'] = {'kind': 'user_declared'}
        return effective

    catalog_meta = _catalog_entry_by_card_id(decl.get('card_id'), catalog_path=catalog_path)
    expected_type = decl.get('type') or (catalog_meta['type'] if catalog_meta else None)
    id_conflict = bool(decl.get('card_id') and obs.get('card_id') and decl['card_id'] != obs['card_id'])
    type_conflict = bool(expected_type and obs.get('type') and expected_type != obs['type'])

    for key in ('card_id', 'type', 'rarity', 'level', 'limit_breaks'):
        if decl.get(key) is not None and obs.get(key) is not None and decl[key] != obs[key]:
            conflict = {
                'slot': slot,
                'field': key,
                'declared': deepcopy(decl[key]),
                'observed': deepcopy(obs[key]),
                'resolution': 'user_declared_retained',
            }
            if conflict not in conflicts:
                conflicts.append(conflict)

    if not id_conflict and not type_conflict:
        # Type-only declaration: allow backfilling card_id & character_id when observed.
        if effective.get('card_id') is None and obs.get('card_id') is not None:
            effective['card_id'] = obs['card_id']
            field_sources['card_id'] = {'kind': 'observed'}
        if effective.get('type') is None and obs.get('type') is not None:
            effective['type'] = obs['type']
            field_sources['type'] = {'kind': 'observed'}
        cand_char = obs.get('character_id') or (catalog_meta['character_id'] if catalog_meta else None)
        if effective.get('character_id') is None and cand_char is not None:
            if cand_char != 'satake_mei' or not mei_taken:
                effective['character_id'] = cand_char
                field_sources['character_id'] = {'kind': 'observed'}
        cand_rarity = effective.get('rarity') or obs.get('rarity') or (catalog_meta['rarity'] if catalog_meta else None)
        if effective.get('rarity') is None and cand_rarity is not None:
            if _compatible_stats(cand_rarity, effective.get('level'), effective.get('limit_breaks')):
                effective['rarity'] = cand_rarity
                field_sources['rarity'] = {'kind': 'observed'}
        if effective.get('level') is None and obs.get('level') is not None:
            if _compatible_stats(effective.get('rarity'), obs['level'], effective.get('limit_breaks')):
                effective['level'] = obs['level']
                field_sources['level'] = {'kind': 'observed'}
        if effective.get('limit_breaks') is None and obs.get('limit_breaks') is not None:
            if _compatible_stats(effective.get('rarity'), effective.get('level'), obs['limit_breaks']):
                effective['limit_breaks'] = obs['limit_breaks']
                field_sources['limit_breaks'] = {'kind': 'observed'}

    used_observed = any(v.get('kind') == 'observed' for v in field_sources.values())
    effective['source'] = (
        {'kind': 'composed', 'field_sources': field_sources}
        if used_observed else {'kind': 'user_declared'}
    )
    return effective


def record_inspection(run_root, run_id, parsed, *, turn_index, source_sha256, catalog_path=None):
    """Persist slot-level backfill without overwriting user-declared slots."""
    from core.auto_trainer.run_support_profile import SCHEMA, _snapshot, load_profile, normalize_profile, save_profile
    obs_source = {
        'kind': 'observed',
        'phase': 'deck_inspect',
        'turn_index': turn_index,
        'source_sha256': source_sha256,
    }
    observed_cards = [
        {
            'slot': row['slot'],
            'card_id': row.get('card_id'),
            'character_id': row.get('character_id'),
            'rarity': row.get('rarity'),
            'type': row['type'],
            'level': row.get('level'),
            'limit_breaks': row.get('limit_breaks'),
            'borrowed': bool(row.get('borrowed')),
            'source': dict(obs_source),
        }
        for row in parsed['slots']
        if row.get('type')
    ]
    observed_norm = normalize_profile(
        {'schema_version': SCHEMA, 'source': obs_source, 'cards': observed_cards, 'max_vitality': None},
        run_id,
    )
    observed_by_slot = {row['slot']: deepcopy(row) for row in observed_norm['cards']}
    summary = {
        'status': parsed['status'],
        'types': list(parsed['types']),
        'counts': dict(parsed['counts']),
        'card_ids': [row.get('card_id') for row in parsed['slots']],
        'rarities': [row.get('rarity') for row in parsed['slots']],
        'levels': [row.get('level') for row in parsed['slots']],
        'limit_breaks': [row.get('limit_breaks') for row in parsed['slots']],
        'turn_index': turn_index,
        'source_sha256': source_sha256,
    }
    previous = load_profile(run_root, run_id)
    declaration_raw = None
    if previous:
        if previous.get('declared_profile'):
            declaration_raw = previous['declared_profile']
        elif (previous.get('source') or {}).get('kind') == 'user_declared':
            declaration_raw = _snapshot(previous)

    complete_obs = (
        len(observed_norm['cards']) == SLOTS
        and all(row.get('card_id') and row.get('type') for row in observed_norm['cards'])
    )
    obs_status = 'complete' if complete_obs else 'partial' if observed_norm['cards'] else 'missing'

    if declaration_raw is not None:
        declared = normalize_profile(declaration_raw, run_id)
        declared_slots = {
            row['slot']: deepcopy(row)
            for row in declared['cards']
            if row.get('card_id') is not None or row.get('type') is not None
        }
        conflicts = deepcopy((previous or {}).get('conflicts', []))
        mei_taken = any(row.get('character_id') == 'satake_mei' for row in declared_slots.values())
        merged_by_slot = {}
        for slot in range(SLOTS):
            if slot in declared_slots:
                card = _merge_declared_slot(
                    declared_slots[slot], observed_by_slot.get(slot),
                    mei_taken=mei_taken and declared_slots[slot].get('character_id') != 'satake_mei',
                    conflicts=conflicts, catalog_path=catalog_path,
                )
                mei_taken = mei_taken or card.get('character_id') == 'satake_mei'
                merged_by_slot[slot] = card
            elif slot in observed_by_slot:
                card = deepcopy(observed_by_slot[slot])
                if mei_taken and card.get('character_id') == 'satake_mei':
                    card['character_id'] = None
                mei_taken = mei_taken or card.get('character_id') == 'satake_mei'
                merged_by_slot[slot] = card
        result = {
            'schema_version': SCHEMA,
            'run_session_id': run_id,
            'source': {'kind': 'composed'} if declared_slots else dict(obs_source),
            'cards': [merged_by_slot[slot] for slot in sorted(merged_by_slot)],
            'max_vitality': (
                previous.get('max_vitality') if previous and previous.get('max_vitality') is not None
                else declared.get('max_vitality')
            ),
            'declared_profile': _snapshot(declared),
            'observed_profile': _snapshot(observed_norm),
            'declaration_frozen': True,
            'observation_status': obs_status,
            'conflicts': conflicts,
            'deck_inspect': summary,
        }
    else:
        keep = {card['slot']: deepcopy(card) for card in (previous or {}).get('cards', [])}
        mei_taken = False
        for slot in sorted(set(keep) | set(observed_by_slot)):
            if slot in observed_by_slot:
                card = {**keep.get(slot, {}), **deepcopy(observed_by_slot[slot])}
            else:
                card = keep[slot]
            if mei_taken and card.get('character_id') == 'satake_mei':
                card['character_id'] = None
            mei_taken = mei_taken or card.get('character_id') == 'satake_mei'
            keep[slot] = card
        result = {
            'schema_version': SCHEMA,
            'run_session_id': run_id,
            'source': dict(obs_source),
            'cards': [keep[slot] for slot in sorted(keep)],
            'max_vitality': previous.get('max_vitality') if previous else None,
            'observed_profile': _snapshot(observed_norm),
            'observation_status': obs_status,
            'deck_inspect': summary,
        }
        if previous and previous.get('max_vitality_source') is not None:
            result['max_vitality_source'] = deepcopy(previous['max_vitality_source'])

    if previous:
        history = deepcopy(previous.get('observation_history', []))
        if previous.get('observed_profile'):
            history.append(_snapshot(previous['observed_profile']))
        if history:
            result['observation_history'] = history[-4:]
        for key in ('reference_snapshot', 'legacy_confirmations'):
            if key in previous:
                result[key] = deepcopy(previous[key])
    return save_profile(run_root, run_id, result)


def record_failure(run_root, run_id, reason, *, turn_index):
    """One attempt per run: a failed route must not loop every turn."""
    from core.auto_trainer.run_support_profile import SCHEMA, load_profile, save_profile
    previous = load_profile(run_root, run_id) or {'schema_version': SCHEMA, 'source': {'kind': 'observed'},
                                                  'cards': [], 'max_vitality': None}
    return save_profile(run_root, run_id, {**previous, 'deck_inspect': {
        'status': 'failed', 'reason': str(reason)[:120], 'turn_index': turn_index}})
