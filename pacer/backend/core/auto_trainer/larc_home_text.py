"""Read a verified, ready L'Arc home's fixed text lines without box detection.

The owner still checks the current home controls/overlays and binds the frame.
Unknown goals or unclear shortfalls use its existing region-reader fallback.
"""
import math
import re

from core.main_menu_state import _analyze_target_text

NUMBER = r"(?:[1-9][0-9]{0,2}(?:[,，][0-9]{3})+|[0-9]+)"


def _line(image, roi, reader, minimum=.95):
    x1, y1, x2, y2 = roi
    try:
        text, confidence = reader(image[y1:y2, x1:x2])
        if not math.isfinite(float(confidence)) or float(confidence) < minimum:
            return None
        return re.sub(r"\s+", "", str(text))
    except Exception:
        return None


def read_ready_home_text(image, *, reader):
    if image is None or image.shape[:2] != (1280, 720):
        return None
    # The already-established glyph template recognizes the short debut goal
    # more reliably than a text line surrounded by white padding.
    known = _analyze_target_text(image)
    if known.get("value") == "参加初级新人战" and known.get("confidence", 0.) >= .95:
        goal = known["value"]
    else:
        goal = _line(image, (230, 75, 590, 103), reader)
    if not goal:
        return None
    supporter = re.fullmatch(rf"获得{NUMBER}点协助者积分", goal)
    fan_goal = goal == "全国德比中进入前3"
    if not supporter and not fan_goal and goal not in {
            "参加初级新人战", "参加尼埃尔锦标", "参加福伊锦标"}:
        return None
    lines = [goal]
    if supporter or fan_goal:
        condition = _line(image, (348, 105, 610, 132), reader)
        pattern = (rf"达成条件还需(?:[Pp][Tt]?)?{NUMBER}积分" if supporter
                   else rf"参赛条件剩余{NUMBER}人")
        if condition is None or not (re.fullmatch(pattern, condition)
                or condition in {"参赛条件已达成!", "参赛条件已达成！", "目标达成!", "目标达成！"}):
            return None  # A missed numeric gap must not look like a met goal.
        lines.append(condition)
    expectation = _line(image, (35, 302, 179, 348), reader, minimum=.9)
    if expectation and re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]+)?[%％]", expectation):
        if 0 <= float(expectation[:-1]) <= 200:
            lines.append("期待度" + expectation.replace("％", "%"))
    points = _line(image, (18, 430, 178, 474), reader)
    if points and re.fullmatch(rf"{NUMBER}[Pp][Tt]", points):
        lines.append("协助者积分" + points)
    return "\n".join(lines)
