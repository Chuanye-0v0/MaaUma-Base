"""Read only known page anchors, retaining full-frame coordinates."""
import numpy as np

def read_regions(image, ocr, boxes):
    width = max((x1 - x0 for x0, y0, x1, y1 in boxes))
    height = sum((y1 - y0 + 24 for x0, y0, x1, y1 in boxes))
    packed = np.full((height, width + 24, 3), 255, dtype=image.dtype)
    regions = []
    offset = 12
    for x0, y0, x1, y1 in boxes:
        h, w = (y1 - y0, x1 - x0)
        packed[offset:offset + h, 12:12 + w] = image[y0:y1, x0:x1]
        regions.append((x0, y0, w, h, offset))
        offset += h + 24
    tokens = []
    rows, _ = ocr(packed)
    for points, text, confidence in rows or []:
        a, b = (min((p[0] for p in points)), min((p[1] for p in points)))
        c, d = (max((p[0] for p in points)), max((p[1] for p in points)))
        for x0, y0, w, h, offset in regions:
            if 12 <= a < c <= 12 + w and offset <= b < d <= offset + h:
                tokens.append(dict(text=text, confidence=float(confidence), bbox=[int(a) - 12 + x0, int(b) - offset + y0, int(c) - 12 + x0, int(d) - offset + y0]))
                break
    return tokens
