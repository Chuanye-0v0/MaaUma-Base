from __future__ import annotations

import cv2
import numpy as np


def help_carousel_visible(img: np.ndarray) -> bool:
    """A paged illustration with three footer controls is not event choices."""
    if img is None or img.ndim != 3 or img.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
        return False
    image = img if img.shape[:2] == (1280, 720) else cv2.resize(img, (720, 1280))
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 235)
    # Only the card/footer layout, no title or paragraph OCR and no game input.
    return bool(float(white[978:1130, 16:704].mean()) > .68
        and all(float(white[1185:1228, left:right].mean()) > .70
                for left, right in ((45, 130), (235, 485), (555, 660)))
        and float(white[1185:1228, 150:200].mean()) < .65
        and float(white[1185:1228, 510:530].mean()) < .65)


def is_carrot_event_narrative(
    visible_text: str,
    resource_terms: list[str] | tuple[str, ...],
) -> bool:
    """Allow the known event title without weakening real resource prompts."""
    terms = {str(term) for term in resource_terms}
    if not terms or not terms.issubset({"胡萝卜", "萝卜"}):
        return False
    if "买些胡萝卜" not in str(visible_text):
        return False
    prompt_terms = (
        "使用",
        "消耗",
        "恢复",
        "不足",
        "需要",
        "所需",
        "花费",
        "购买",
        "购入",
        "支付",
        "充值",
        "兑换",
        "继续",
        "确认",
    )
    return not any(term in str(visible_text) for term in prompt_terms)


def detect_event_option_points(img: np.ndarray) -> dict[str, tuple[int, int]]:
    return detect_event_option_layout(img)["points"]


def event_option_rows_ready(img: np.ndarray) -> bool:
    """Actual options are centred and regularly spaced, not scenery panels."""
    points = detect_event_option_layout(img).get("ordered_points", [])
    if not points and single_story_event_option_point(img) is not None:
        return True
    if not 2 <= len(points) <= 5:
        return False
    height, width = img.shape[:2]
    return (all(abs(x - width / 2) <= width * .035 for x, _ in points)
        and all(height * .07 <= b[1] - a[1] <= height * .105
                for a, b in zip(points, points[1:])))


def single_story_event_option_point(img: np.ndarray) -> tuple[int,int] | None:
    """Recognize a sole rendered story choice, not a generic white button."""
    if img is None or img.ndim != 3 or img.shape[1::-1] not in {(720,1280),(1440,2560)}:
        return None
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    point=SafeAdvancePlanner._single_green_event_option_button(img)
    if point is None or SafeAdvancePlanner._looks_like_confirm_modal_panel(img):
        return None
    image=img if img.shape[:2]==(1280,720) else cv2.resize(img,(720,1280))
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    heading=hsv[195:310,115:625]
    green=((heading[:,:,0]>=35)&(heading[:,:,0]<=92)&(heading[:,:,1]>70)&(heading[:,:,2]>80))
    white=(heading[:,:,1]<65)&(heading[:,:,2]>175)
    if float(green.mean())<.25 or float(white.mean())<.015:
        return None
    return point


def _outlined_event_layout(img, visible_text):
    """Same-frame event heading + regular rendered controls, independent of fill hue.

    This only locates rows. Event title, option count, resource and input bindings
    remain the planner/executor's responsibility.
    """
    from core.event_knowledge_lookup import normalize_text
    labels={'协助卡事件','辅助卡事件','支援卡事件','养成优俊少女事件',
            '育成赛马娘事件','赛马娘事件','养成事件','育成事件'}
    lines=[''.join(normalize_text(line).split()) for line in str(visible_text or '').splitlines() if line.strip()]
    headers=[i for i,line in enumerate(lines) if line in labels]
    if len(headers)!=1 or headers[0]+1>=len(lines):return None
    title=lines[headers[0]+1]
    if title in labels or len(title)<2:return None
    h,w=img.shape[:2]
    image=cv2.resize(img,(720,1280)) if (w,h)!=(720,1280) else img
    # A category alone is not an event identity. Confirm the candidate title
    # within this frame's title banner, so option prose cannot stand in for it.
    from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader
    try:titles=LocalRapidOCRScreenTextReader().read_regions(image[180:320,80:700]) or []
    except Exception:return None
    if title not in {''.join(normalize_text(row[1]).split()) for row in titles
                     if len(row)>2 and float(row[2])>=.90}:return None
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
    edge=cv2.Canny(gray[320:922],35,95)
    edge=cv2.morphologyEx(edge,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    contours,_=cv2.findContours(edge,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    rows=[]
    for contour in contours:
        x,y,width,height=cv2.boundingRect(contour);y+=320
        if not (10<=x<=55 and width>=600 and 45<=height<=105 and x+width>=640):continue
        ink=gray[y+10:y+height-10,max(x+65,80):min(x+width-80,590)]
        if not ink.size or float(np.percentile(ink,75)-np.percentile(ink,2))<65:continue
        cy=y+height//2
        if all(abs(cy-old)>15 for old in rows):rows.append(cy)
    rows.sort()
    if not 2<=len(rows)<=5 or any(not 88<=b-a<=136 for a,b in zip(rows,rows[1:])):return None
    points=[(w//2,round(y*h/1280)) for y in rows]
    return {'points':{'top':points[0],'bottom':points[-1]},'ordered_points':points,
            'option_count':len(points),'basis':'same_frame_event_heading_and_control_outlines'}


def detect_event_option_layout(img: np.ndarray, *, visible_text=None) -> dict[str, object]:
    if help_carousel_visible(img):
        return {"points": {}, "option_count": 0, "reason": "help_carousel_not_event"}
    height, width = img.shape[:2]
    if width > height:
        return {"points": {}, "option_count": 0}
    x_offset = int(width * 0.02)
    y_offset = int(height * 0.25)
    crop = img[y_offset : int(height * 0.72), x_offset : int(width * 0.98)]
    if crop.size == 0:
        return {"points": {}, "option_count": 0}
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 1] < 80) & (hsv[:, :, 2] > 170)).astype("uint8") * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w > width * 0.65 and h > height * 0.02:
            boxes.append((x, y, w, h))
    boxes.sort(key=lambda item: item[1])
    option_boxes: list[tuple[int, int, int, int]] = []
    for box in boxes:
        if not option_boxes:
            option_boxes.append(box)
            continue
        previous = option_boxes[-1]
        if box[1] - (previous[1] + previous[3]) > height * 0.015:
            option_boxes.append(box)
    if len(option_boxes) < 2:
        outlined=_outlined_event_layout(img,visible_text)
        if outlined is not None:return outlined
        return {"points": {}, "option_count": len(option_boxes)}
    top = option_boxes[0]
    bottom = option_boxes[-1]
    return {
        "points": {
            "top": (x_offset + top[0] + top[2] // 2, y_offset + top[1] + top[3] // 2),
            "bottom": (
                x_offset + bottom[0] + bottom[2] // 2,
                y_offset + bottom[1] + bottom[3] // 2,
            ),
        },
        "ordered_points": [(x_offset + x + w // 2, y_offset + y + h // 2) for x, y, w, h in option_boxes],
        "option_count": len(option_boxes),
    }
