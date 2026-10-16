"""Small-ROI item/entry perception; original cost and empty-slot gates remain."""
import cv2
from core.legend_race.observer import observe

def fast_items(raw, image, ocr, *, daily=False):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    from core.passive_tutorial import pager_identity
    if pager_identity(image):
        return None
    contours, _ = cv2.findContours(cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 35, 100), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    panel = any((x <= 40 and w >= 640 and (200 <= y <= 400) and (450 <= h <= 800) and (y + h <= 1030) for x, y, w, h in (cv2.boundingRect(contour) for contour in contours)))
    if not panel:
        return None
    from core.ocr_regions import read_regions
    tokens = read_regions(image, ocr, [(270, 295, 450, 365), (270, 408, 450, 480), (90, 815, 650, 956), (525, 45, 685, 115), (480, 1190, 715, 1280)])
    if sum((t['text'].replace(' ', '') == '选择道具' and t['confidence'] >= 0.95 for t in tokens)) != 1:
        return None
    if sum((t['text'].replace(' ', '') == '请选择道具' and t['confidence'] >= 0.95 for t in tokens)) != 1:
        return None
    if not any((t['text'].replace(' ', '') == '消耗1' and t['confidence'] >= 0.95 for t in tokens)):
        box = (430, 910, 590, 952)
        x, y, r, b = box
        rows, _ = ocr(cv2.resize(image[y:b, x:r], None, fx=3, fy=3), use_det=False, use_cls=False)
        for text, score in rows or []:
            if text.replace(' ', '') == '消耗1' and score >= 0.95:
                tokens.append(dict(text='消耗1', confidence=float(score), bbox=list(box)))
    result = observe(raw, tokens, navigation=True, daily=daily)
    if result['page'] == 'loading':
        return result
    if result['page'] == 'items' and type(result.get('tickets')) is int and (type(result.get('tickets_after')) is int):
        result['perception'] = 'fast_items'
        return result
    return None
