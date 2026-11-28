"""In-memory, exact-pixel regional OCR for every live daily-task device.

This caches text, never observations/actions/timestamps. The current screenshot
still goes through its owner observer and safety gates on every capture.
"""
from copy import deepcopy
from collections import OrderedDict
import math
import numpy as np

class RegionRecognizer:
    """Reuse only byte-identical detected text crops; positions are always fresh."""

    def __init__(self, recognize):
        self.recognize = recognize
        self.cache = OrderedDict()

    def clear(self):
        self.cache.clear()

    def __call__(self, images, return_word_box=False):
        images = [images] if isinstance(images, np.ndarray) else images
        keys = [(image.shape, image.dtype.str, bool(return_word_box), image.tobytes()) for image in images]
        results = [None] * len(images)
        missing = []
        for i, key in enumerate(keys):
            if key in self.cache:
                results[i] = deepcopy(self.cache[key])
                self.cache.move_to_end(key)
            else:
                missing.append(i)
        elapsed = 0.0
        if missing:
            fresh, elapsed = self.recognize([images[i] for i in missing], return_word_box)
            if len(fresh) != len(missing):
                self.clear()
                raise RuntimeError('ocr_region_count_mismatch')
            for i, value in zip(missing, fresh):
                results[i] = value
                if float(value[1]) >= 0.9:
                    self.cache[keys[i]] = deepcopy(value)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
        return (results, elapsed)

class IncrementalOCR:

    def __init__(self, engine):
        self.engine = engine
        if hasattr(engine, 'text_rec'):
            engine.text_rec = RegionRecognizer(engine.text_rec)
        self.clear()

    def clear(self):
        self.image = self.rows = self.options = None
        self.local_reads = 0
        self.last_mode = None
        if isinstance(getattr(self.engine, 'text_rec', None), RegionRecognizer):
            self.engine.text_rec.clear()

    @staticmethod
    def bounds(row):
        points = np.asarray(row[0], dtype=float)
        if points.shape != (4, 2) or not np.isfinite(points).all():
            raise ValueError('invalid_ocr_polygon')
        return (math.floor(points[:, 0].min()), math.floor(points[:, 1].min()), math.ceil(points[:, 0].max()), math.ceil(points[:, 1].max()))

    def remember(self, image, rows, options, mode):
        self.image = image.copy()
        self.rows = deepcopy(rows or [])
        self.options = dict(options)
        self.last_mode = mode

    def full(self, image, options):
        self.image = self.rows = self.options = None
        self.local_reads = 0
        rows, elapsed = self.engine(image, **options)
        self.remember(image, rows, options, 'full')
        return (rows, elapsed)

    def __call__(self, image, **options):
        if not isinstance(image, np.ndarray) or image.shape != (1280, 720, 3) or options.get('use_det') is False or (options.get('use_rec') is False):
            return self.engine(image, **options)
        if self.image is None or self.options != options:
            return self.full(image, options)
        changed = np.any(image != self.image, axis=2)
        if not changed.any():
            self.last_mode = 'identical'
            return (deepcopy(self.rows), [0.0, 0.0, 0.0])
        if self.local_reads >= 8:
            return self.full(image, options)
        ys, xs = np.nonzero(changed)
        x0, y0, x1, y1 = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        pad = 32
        x0, y0, x1, y1 = (max(0, x0 - pad), max(0, y0 - pad), min(720, x1 + pad), min(1280, y1 + pad))
        try:
            boxes = [self.bounds(row) for row in self.rows]
        except (ValueError, TypeError, IndexError):
            return self.full(image, options)
        while True:
            old = (x0, y0, x1, y1)
            for a, b, c, d in boxes:
                if a < x1 and c > x0 and (b < y1) and (d > y0):
                    x0, y0, x1, y1 = (min(x0, max(0, a - pad)), min(y0, max(0, b - pad)), max(x1, min(720, c + pad)), max(y1, min(1280, d + pad)))
            if old == (x0, y0, x1, y1):
                break
        if (x1 - x0) * (y1 - y0) > 720 * 1280 * 0.65:
            return self.full(image, options)
        try:
            local, elapsed = self.engine(image[y0:y1, x0:x1], **options)
            translated = []
            for row in local or []:
                a, b, c, d = self.bounds(row)
                if float(row[2]) < 0.9 or (x0 > 0 and a < 8) or (y0 > 0 and b < 8) or (x1 < 720 and c > x1 - x0 - 8) or (y1 < 1280 and d > y1 - y0 - 8):
                    return self.full(image, options)
                points = (np.asarray(row[0], dtype=float) + [x0, y0]).tolist()
                translated.append([points, row[1], row[2]])
            intersecting = [row for row, (a, b, c, d) in zip(self.rows, boxes) if a < x1 and c > x0 and (b < y1) and (d > y0)]
            if intersecting and (not translated):
                return self.full(image, options)
            rows = []
            remaining = list(translated)
            for previous, (a, b, c, d) in zip(self.rows, boxes):
                if not (a < x1 and c > x0 and (b < y1) and (d > y0)):
                    rows.append(deepcopy(previous))
                    continue
                matches = []
                for i, fresh in enumerate(remaining):
                    e, f, g, h = self.bounds(fresh)
                    overlap = max(0, min(c, g) - max(a, e)) * max(0, min(d, h) - max(b, f))
                    if overlap > 0.5 * max(1, (c - a) * (d - b), (g - e) * (h - f)):
                        matches.append(i)
                if len(matches) != 1:
                    return self.full(image, options)
                fresh = remaining.pop(matches[0])
                if not changed[max(0, b):min(1280, d), max(0, a):min(720, c)].any():
                    if fresh[1] != previous[1]:
                        return self.full(image, options)
                    rows.append(deepcopy(previous))
                else:
                    rows.append(fresh)
            for fresh in remaining:
                key = (fresh[0][0][1], fresh[0][0][0])
                index = next((i for i, r in enumerate(rows) if (r[0][0][1], r[0][0][0]) > key), len(rows))
                rows.insert(index, fresh)
            self.local_reads += 1
            self.remember(image, rows, options, 'regional')
            return (rows, elapsed)
        except Exception:
            self.clear()
            raise
