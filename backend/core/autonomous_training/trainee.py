"""Exact Sakura Bakushin O selection for the guild-unlock route."""
from pathlib import Path
from functools import lru_cache
from difflib import SequenceMatcher
import cv2
import numpy as np
REQUIRED_TRAINEE = '樱花进王'
ASSET_DIR = Path(__file__).with_name('assets')

@lru_cache(maxsize=160)
def portrait_templates(key):
    """Face-only hints; exact outfit identity is still checked after selection."""
    root = Path(__file__).resolve().parents[2]
    portrait = cv2.imread(str(root.parent / f'resource/portraits/{key}.png'))
    templates = []
    if portrait is not None:
        portrait = cv2.resize(portrait, (256, 256))
        face = portrait[65:185, 55:200]
        templates.extend((cv2.resize(face, None, fx=s, fy=s) for s in (0.4, 0.45, 0.5, 0.55, 0.6)))
    for appearance in ('school', 'race'):
        path = ASSET_DIR / f'{key}_{appearance}_face.png'
        if path.exists():
            face = cv2.imread(str(path))
            if face is not None:
                templates.extend((cv2.resize(face, None, fx=s, fy=s) for s in (0.9, 1.0, 1.1)))
    return templates

def portrait_scores(image, key, points):
    """Search only visible card interiors, excluding stars and bottom controls."""
    templates = portrait_templates(key)
    scores = []
    for x, y in points:
        region = image[max(640, y - 75):min(970, y + 45), max(10, x - 65):min(710, x + 65)]
        best = 0.0
        for template in templates:
            if region.shape[0] >= template.shape[0] and region.shape[1] >= template.shape[1]:
                score = cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))[1]
                best = max(best, score)
        scores.append(round(best, 4))
    return scores

def variant_observation(image, tokens, key):
    """Read selected name/outfit, and expose only visible card-shaped candidates."""
    import re
    from core.autonomous_training.plant_catalog import VARIANTS, trainee_identity
    identity = trainee_identity(key)
    key = identity['card_id']
    record = VARIANTS[key]
    normalize = lambda value: re.sub('[^\\w\\u4e00-\\u9fff]', '', value).lower()
    header = ''.join((normalize(t['text']) for t in tokens if t.get('confidence', 0) >= 0.9 and 0 <= t['bbox'][0] < 400 and (375 <= t['bbox'][1] < 450)))
    names = [record['name'], *record.get('aliases', [])]
    outfit = normalize(record['outfit'])
    outfits = [outfit, *[normalize(value) for value in record.get('outfit_aliases', [])]]
    name_matched = any((normalize(name) in header for name in names))
    matched = name_matched and any((value and value in header for value in outfits))
    if name_matched and (not matched):
        path = ASSET_DIR / f'{key}_outfit_title.png'
        if path.exists():
            title = cv2.imread(str(path))
            if title is not None:
                score = cv2.minMaxLoc(cv2.matchTemplate(image[385:420, 30:330], title, cv2.TM_CCOEFF_NORMED))[1]
                matched = score >= 0.94
    if name_matched and (not matched):
        name = next((normalize(n) for n in sorted(names, key=len, reverse=True) if normalize(n) and header.endswith(normalize(n))), '')
        observed_outfit = header[:-len(name)] if name else ''
        if len(observed_outfit) >= 4 and len(outfit) >= 4:
            scores = sorted(((SequenceMatcher(None, observed_outfit, normalize(r['outfit'])).ratio(), k) for k, r in VARIANTS.items() if r['character_id'] == record['character_id']))
            best, selected = scores[-1]
            runner_up = scores[-2][0] if len(scores) > 1 else 0.0
            matched = selected == key and best >= 0.75 and (best - runner_up >= 0.2)
    edges = cv2.Canny(image[640:970], 70, 160)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    points = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (85 < w < 145 and 85 < h < 160):
            continue
        point = [x + w // 2, 640 + y + h // 2]
        if not any((abs(point[0] - p[0]) < 45 and abs(point[1] - p[1]) < 65 for p in points)):
            points.append(point)
    rows = []
    for point in sorted(points, key=lambda p: p[1]):
        if not rows or abs(point[1] - float(np.median(rows[-1]))) > 65:
            rows.append([point[1]])
        else:
            rows[-1].append(point[1])
    points = []
    for row in rows:
        y = int(np.median(row))
        for x in (88, 221, 355, 489, 622):
            crop = image[max(640, y - 48):min(970, y + 48), x - 45:x + 45]
            if crop.size and float(np.mean(np.min(crop, axis=2) < 180)) > 0.2:
                points.append([x, y])
    scores = portrait_scores(image, key, points)
    if record['initial_key']:
        initial = initial_candidate(image, record['initial_key'])
        if initial:
            for index, (x, y) in enumerate(points):
                if abs(x - initial['point'][0]) < 45 and abs(y - initial['point'][1]) < 65:
                    scores[index] = max(scores[index], initial['confidence'])
    return dict(selected_variant=key if matched else None, selected_header=header, selected_name=record['name'] if matched else None, selected_card_id=key if matched else None, selected_character_id=record['character_id'] if matched else None, selected_costume_id=record['costume_id'] if matched else None, identity_basis='selected_name_and_outfit' if matched else None, candidates=[dict(point=p, confidence=0.95, variant_key=key, portrait_score=s) for p, s in zip(points, scores)], signature=cv2.resize(cv2.cvtColor(image[640:970], cv2.COLOR_BGR2GRAY), (24, 12)).flatten().tolist())

def initial_candidate(image, key):
    """Match either appearance of the five initial cards, never list ordinals."""
    from core.autonomous_training.plant_catalog import trainee_name
    if image.shape[:2] != (1280, 720):
        return None
    templates = []
    for name in (f'{key}_face.png', f'{key}_race_face.png'):
        path = ASSET_DIR / name
        if path.exists():
            template = cv2.imread(str(path))
            if template is not None:
                templates.append((template, (0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1)))
    if key == 'scarlet':
        reference = cv2.imread(str(ASSET_DIR / 'scarlet_reference.png'))
        if reference is not None:
            templates.append((reference[20:100, 20:78], (*np.arange(0.8, 1.61, 0.025), 4 / 3)))
    region = image[640:1010, 10:710]
    candidates = []
    for template, scales in templates:
        for scale in scales:
            scaled = cv2.resize(template, None, fx=scale, fy=scale)
            scores = cv2.matchTemplate(region, scaled, cv2.TM_CCOEFF_NORMED)
            for y, x in zip(*np.where(scores >= 0.94)):
                candidates.append((float(scores[y, x]), int(x + scaled.shape[1] // 2 + 10), int(y + scaled.shape[0] // 2 + 640)))
    if not candidates:
        return None
    best = max(candidates)
    if any((abs(x - best[1]) > 35 or abs(y - best[2]) > 35 for _, x, y in candidates)):
        return None
    return dict(point=list(best[1:]), confidence=best[0], trainee_name=trainee_name(key))

def scarlet_candidate(image):
    return initial_candidate(image, 'scarlet')

def bakushin_candidate(image):
    return initial_candidate(image, 'bakushin')
