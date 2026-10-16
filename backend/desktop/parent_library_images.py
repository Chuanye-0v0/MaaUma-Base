"""Small portrait previews from this record's saved game frames, never name lookup."""
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from core.parent_library import store_path

def _image(relative, base, cache):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        return None
    parts = Path(relative)
    if parts.is_absolute() or '..' in parts.parts or ':' in relative:
        return None
    if relative in cache:
        return cache[relative]
    result = None
    try:
        from PIL import Image
        path = (base / parts).resolve()
        anchors = {base.resolve(), (base / 'library.json').resolve().parent}
        if not any((path.is_relative_to(anchor) for anchor in anchors)):
            return None
        with Image.open(path) as source:
            if source.width * source.height <= 16000000:
                result = source.convert('RGB')
    except (OSError, ValueError, SyntaxError):
        pass
    cache[relative] = result
    return result

def _sources(node, kind=None):
    result = []
    for factor in (node or {}).get('factors', []):
        if kind is not None and factor.get('kind') != kind:
            continue
        for source in factor.get('sources', []):
            path = source.get('screenshot')
            if path and path not in result:
                result.append(path)
    return result

def _headers(frame):
    """Locate full blue/red header pairs in the already saved factor panel."""
    import cv2
    import numpy as np
    image = cv2.resize(np.asarray(frame), (720, 1280))
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)

    def boxes(left, right, low, high):
        mask = cv2.inRange(hsv[591:1110, left:right], np.array(low), np.array(high))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        found = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            top = 591 + y
            if width >= 220 and height >= 36 and (cv2.contourArea(contour) >= 1800) and (top > 594) and (top + height + 16 < 1107):
                found.append((top, top + height))
        return found
    blue = boxes(139, 408, (85, 65, 110), (120, 255, 255))
    red = boxes(415, 685, (145, 35, 110), (179, 255, 255))
    return sorted((top for top, bottom in blue if any((abs(top - other) <= 7 and abs(bottom - end) <= 9 for other, end in red))))

def _scaled_box(box, frame):
    return [round(box[0] * frame.width / 720), round(box[1] * frame.height / 1280), round(box[2] * frame.width / 720), round(box[3] * frame.height / 1280)]

def _refs(entry, base, cache):
    result = dict(entry.get('portraits') or {})
    nodes = [entry.get('self'), *(entry.get('parents') or [])[:2]]
    nodes += [None] * (3 - len(nodes))
    roles = ['self', 'parent_1', 'parent_2']
    if 'self' not in result:
        for relative in _sources(nodes[0]):
            frame = _image(relative, base, cache)
            if frame is not None:
                result['self'] = {'screenshot': relative, 'bbox': _scaled_box([65, 94, 230, 265], frame)}
                break
    header_sources = [set(_sources(node, 'blue')) & set(_sources(node, 'red')) for node in nodes]
    pages = sorted(set().union(*header_sources))
    for relative in pages:
        indices = [i for i, paths in enumerate(header_sources) if relative in paths]
        if not any((i > 0 and roles[i] not in result for i in indices)):
            continue
        frame = _image(relative, base, cache)
        if frame is None:
            continue
        tops = _headers(frame)
        if len(tops) != len(indices):
            continue
        for index, top in zip(indices, tops):
            if index == 0 or roles[index] in result or top - 18 < 591 or (top + 96 > 1110):
                continue
            result[roles[index]] = {'screenshot': relative, 'bbox': _scaled_box([31, top - 18, 136, top + 96], frame)}
    return result

def observed_portrait_refs(entry, *, root=None):
    """Return provenance only; does not write or change the stored record."""
    return _refs(entry, store_path(root).parent, {})

def entry_portraits(entry, *, root=None):
    """Read bounded thumbnails for the selected entry; missing files are normal."""
    base = store_path(root).parent
    cache = {}
    result = {}
    for role, ref in _refs(entry, base, cache).items():
        if role not in {'self', 'parent_1', 'parent_2'} or not isinstance(ref, dict):
            continue
        frame = _image(ref.get('screenshot'), base, cache)
        box = ref.get('bbox')
        if frame is None or not isinstance(box, list) or len(box) != 4 or any((type(value) is not int for value in box)) or (not (0 <= box[0] < box[2] <= frame.width and 0 <= box[1] < box[3] <= frame.height)):
            continue
        crop = frame.crop(box)
        crop.thumbnail((144, 144))
        output = BytesIO()
        crop.save(output, format='PNG')
        result[role] = {'png_base64': b64encode(output.getvalue()).decode('ascii'), 'source': 'saved_game_frame'}
    return result
