"""Read the optional event-boost checkbox on the final training page.

The unchecked control still contains a grey tick.  Its shape alone is therefore
not an enabled-state signal; read the tick colour and its adjacent event banner.
"""
import re
import cv2
import numpy as np

def _text(token):
    return re.sub('[\\s（）()]', '', str(token.get('text', '')))

def _within(token, box):
    bounds = token.get('bbox')
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return False
    left, top, right, bottom = bounds
    x0, y0, x1, y1 = box
    return x0 <= left < right <= x1 and y0 <= top < bottom <= y1

def read(image, tokens):
    """Return observed state and, when clear, a checkbox-only toggle candidate.

    Optional or unrecognized activity rows do not gate ordinary final-page
    navigation.  Nothing here depends on the configured target or the TP values.
    Coordinates are native 720x1280, as in the surrounding observer.
    """
    result = dict(event_boost_available=False, event_boost_enabled=None, event_boost_reason='optional_event_row_absent')
    if image is None or image.shape[:2] != (1280, 720):
        result['event_boost_reason'] = 'unsupported_frame_size'
        return result
    good = [token for token in tokens if token.get('confidence', 0) >= 0.9]
    if not any((_text(token) == '最终确认' and _within(token, (180, 0, 540, 120)) for token in good)):
        result['event_boost_reason'] = 'not_final_confirmation'
        return result
    rows = [token for token in good if _text(token) == '活动加速消耗2倍训练值' and _within(token, (90, 875, 610, 1045))]
    if len(rows) != 1:
        return result
    left, top, right, bottom = rows[0]['bbox']
    center_y = (top + bottom) // 2
    x0, x1 = (max(20, left - 110), left - 8)
    y0, y1 = (center_y - 38, center_y + 39)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    region = hsv[y0:y1, x0:x1]
    border = ((region[:, :, 1] < 65) & (region[:, :, 2] < 225)).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(border, 8)
    boxes = []
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if 35 <= width <= 65 and 35 <= height <= 65 and (0.8 <= width / height <= 1.2) and (area >= 180) and (abs(y0 + y + height / 2 - center_y) <= 12):
            boxes.append((x0 + x, y0 + y, width, height))
    if len(boxes) != 1:
        result['event_boost_reason'] = 'optional_checkbox_unrecognized'
        return result
    x, y, width, height = boxes[0]
    interior = hsv[y + 5:y + height - 5, x + 5:x + width - 5]
    if float(((interior[:, :, 1] < 60) & (interior[:, :, 2] >= 220)).mean()) < 0.55:
        result['event_boost_reason'] = 'optional_checkbox_obscured'
        return result
    banner = hsv[center_y - 20:center_y + 21, left - 12:680]
    pink = (banner[:, :, 0] >= 150) & (banner[:, :, 0] <= 179) & (banner[:, :, 1] >= 80) & (banner[:, :, 2] >= 100)
    if float(pink.mean()) < 0.35:
        result['event_boost_reason'] = 'optional_event_banner_unrecognized'
        return result
    result['event_boost_available'] = True
    green = (interior[:, :, 0] >= 30) & (interior[:, :, 0] <= 90) & (interior[:, :, 1] >= 80) & (interior[:, :, 2] >= 100)
    green_pixels = int(green.sum())
    bright_pink = float((banner[:, :, 2][pink] >= 210).mean())
    if green_pixels >= 50 and bright_pink >= 0.6:
        result.update(event_boost_enabled=True, event_boost_reason='green_tick_bright_banner')
    elif green_pixels <= 3 and bright_pink <= 0.1:
        result.update(event_boost_enabled=False, event_boost_reason='grey_tick_muted_banner')
    else:
        result['event_boost_reason'] = 'optional_checkbox_state_unclear'
        return result
    result['toggle_event_boost'] = dict(point=[x + width // 2, y + height // 2])
    return result
