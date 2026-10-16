"""Portable OCR/geometry adapters for U2's unchanged foreground TP parser."""
import re
from threading import local
import cv2
from core.perception import Perception
_LOCAL = local()

def engine():
    if not hasattr(_LOCAL, 'ocr'):
        from core.ocr_runtime import create_ocr
        _LOCAL.ocr = create_ocr()
    return _LOCAL.ocr

def ocr_tokens(image, reader=None):
    reader = reader or (lambda frame: engine()(frame)[0])
    regions = reader(image) if callable(reader) else reader.read_regions(image)
    result = []
    for row in regions or []:
        try:
            box, text, confidence = row[:3]
            if float(confidence) < 0.9:
                continue
            xs, ys = ([float(p[0]) for p in box], [float(p[1]) for p in box])
            result.append(dict(text=re.sub('\\s+', '', str(text)), bbox=[min(xs), min(ys), max(xs), max(ys)], confidence=float(confidence)))
        except (ValueError, TypeError, IndexError):
            continue
    return result

class LineReader:

    def read_line(self, image):
        rows, _ = engine()(cv2.resize(image, None, fx=4, fy=4), use_det=False, use_cls=False)
        if rows and len(rows) == 1:
            return (rows[0][0], float(rows[0][1]))
        return ('', 0)
LINE_READER = LineReader()

def button_boxes(image):
    boxes = [tuple(b['bbox']) for b in Perception().analyze_image(image)['buttons'] if b.get('score', 0) >= 0.8]
    edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 40, 100)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if 70 <= w <= 650 and 30 <= h <= 120 and (1.5 <= w / h <= 12):
            boxes.append((x, y, w, h))
    return boxes

def visible_control(tokens, boxes, labels):
    matches = []
    for token in tokens:
        if token['text'].strip('！!。') not in labels:
            continue
        x0, y0, x1, y1 = token['bbox']
        x, y = ((x0 + x1) / 2, (y0 + y1) / 2)
        if any((b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3] for b in boxes)):
            matches.append((round(x), round(y)))
    unique = set(matches)
    return list(unique)[0] if len(unique) == 1 else None
