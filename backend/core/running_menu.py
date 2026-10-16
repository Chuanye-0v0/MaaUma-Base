"""Native-resolution running UI controls, confined to the bottom-right panel."""
from itertools import combinations
import cv2
import numpy as np
MENU_REGION = (590, 1185, 700, 1280)

def detect(image):
    """Find one white button enclosing three aligned brown bars.

    Background shapes are never counted as button strokes. Page identity,
    timer, fresh observation and submission checks belong to the caller.
    """
    if image is None or image.shape[:2] != (1280, 720):
        return None
    x0, y0, x1, y1 = MENU_REGION
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 190)).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(white)
    candidates = []
    for index, (x, y, width, height, area) in enumerate(stats):
        if not index or not (58 <= width <= 100 and 58 <= height <= 95 and (0.72 <= width / height <= 1.4) and (area / (width * height) >= 0.55)):
            continue
        panel = hsv[y:y + height, x:x + width]
        brown = ((panel[:, :, 0] >= 5) & (panel[:, :, 0] <= 25) & (panel[:, :, 1] >= 80) & (panel[:, :, 2] >= 60)).astype(np.uint8)
        count, _, bars, centers = cv2.connectedComponentsWithStats(brown)
        strokes = [(bars[i], centers[i]) for i in range(1, count) if 35 <= bars[i, 2] <= 60 and 3 <= bars[i, 3] <= 12 and (bars[i, 4] >= 200) and (5 <= bars[i, 0]) and (bars[i, 0] + bars[i, 2] <= width - 5)]
        for group in combinations(strokes, 3):
            group = sorted(group, key=lambda value: value[1][1])
            xs = [center[0] for _, center in group]
            ys = [center[1] for _, center in group]
            widths = [bar[2] for bar, _ in group]
            if max(xs) - min(xs) > 5 or max(widths) - min(widths) > 6 or any((not 8 <= b - a <= 25 for a, b in zip(ys, ys[1:]))) or (abs(ys[1] - ys[0] - (ys[2] - ys[1])) > 4) or (not 8 <= group[0][0][1]) or (group[-1][0][1] + group[-1][0][3] > height - 8):
                continue
            cx, cy = (float(np.mean(xs)), float(np.mean(ys)))
            if abs(cx - width / 2) > 8 or abs(cy - height / 2) > 10:
                continue
            left = min((bar[0] for bar, _ in group))
            right = max((bar[0] + bar[2] for bar, _ in group))
            top, bottom = (group[0][0][1], group[-1][0][1] + group[-1][0][3])
            body = labels[y:y + height, x:x + width] == index
            if body[top:bottom, left - 5:left - 1].mean() < 0.8 or body[top:bottom, right + 1:right + 5].mean() < 0.8:
                continue
            candidates.append(dict(point=[round(x0 + x + cx), round(y0 + y + cy)], button_bbox=[int(x0 + x), int(y0 + y), int(x0 + x + width), int(y0 + y + height)]))
    return candidates[0] if len(candidates) == 1 else None
