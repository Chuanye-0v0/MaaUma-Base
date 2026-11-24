"""Observed CN race-preview / tactics controls; no strategy selection."""
import re
import cv2
import numpy as np

STYLE_LABELS = {"back": "后追", "middle": "居中", "front": "跟前", "leader": "领跑"}
STYLE_CENTERS = {"back": 130, "middle": 283, "front": 436, "leader": 590}
ALLOWED_SOLO_FRONT_APTITUDES = frozenset({"S", "A", "B", "C", "D", "E"})
MODAL_CANCEL_POINT = (201, 917)
_PREVIEW_COUNT_DX = (("back", -94), ("middle", -36), ("front", 23), ("leader", 84))


def preview_tactic_opponent_inks(image):
    """Measure opponent count text ink above the green `更改战术` button."""
    if image is None or len(getattr(image, "shape", ())) < 2:
        return None
    im = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
    row_density = green[710:790, 530:625].mean(axis=1)
    bar_ys = np.where(row_density > 0.65)[0] + 710
    if bar_ys.size < 12:
        return None
    gy1, gy2 = int(bar_ys.min()), int(bar_ys.max())
    col_density = green[gy1:gy2 + 1, 490:670].mean(axis=0)
    bar_xs = np.where(col_density > 0.45)[0] + 490
    if bar_xs.size < 40:
        return None
    gx1, gx2 = int(bar_xs.min()), int(bar_xs.max())
    cx = (gx1 + gx2) / 2.0
    btn_center = (int(round(cx)), int(round((gy1 + gy2) / 2.0)))
    cy1, cy2 = max(0, gy1 - 31), max(1, gy1 - 2)
    if cy2 - cy1 < 12:
        return None
    inks = {}
    for style, dx in _PREVIEW_COUNT_DX:
        x1 = max(0, int(round(cx + dx - 28)))
        x2 = min(720, int(round(cx + dx + 28)))
        chsv = hsv[cy1:cy2, x1:x2]
        inks[style] = round(float(((chsv[:, :, 2] < 165) & (chsv[:, :, 1] > 15)).mean()), 4)
    return inks, btn_center


def preview_is_solo_leader(image):
    """Return `(is_solo, inks, btn_center)` or `(None, ...)` when counts are not readable."""
    parsed = preview_tactic_opponent_inks(image)
    if parsed is None:
        return None, None, None
    inks, btn_center = parsed
    if sum(1 for key in ("back", "middle", "front") if inks[key] >= 0.03) < 2:
        return None, inks, btn_center
    return inks["leader"] < 0.015, inks, btn_center


def parse_modal_style_aptitudes(text):
    compact = "".join(str(text).split())
    aptitudes = {}
    for style, label in STYLE_LABELS.items():
        match = re.search(rf"{label}([SABCDEFG])", compact)
        if match is not None:
            aptitudes[style] = match[1]
    return aptitudes


def race_tactic_control(
    image,
    text,
    desired,
    *,
    preview=False,
    modal_closing=False,
    solo_leader_to_front=False,
    solo_leader_fallback=False,
    solo_leader_restore_style=None,
    front_aptitude_rejected=False,
    is_debut_race=False,
    allowed_front_aptitudes=ALLOWED_SOLO_FRONT_APTITUDES,
):
    if desired is None:
        if not solo_leader_to_front:
            raise ValueError("race_tactic_style_invalid")
    elif desired not in STYLE_LABELS:
        raise ValueError("race_tactic_style_invalid")
    compact = "".join(str(text).split())
    im = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
    white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 190)
    target = STYLE_LABELS.get(desired)
    result = {"target_style": desired, "target_label": target}
    is_tactic_modal = (
        "战术" in compact
        and all(label in compact for label in STYLE_LABELS.values())
        and green[310:348, 30:690].mean() > .7
        and all(white[753:800, x-42:x+42].mean() > .45 for x in STYLE_CENTERS.values())
    )
    if is_tactic_modal:
        if modal_closing or desired not in STYLE_LABELS or green[897:940, 400:635].mean() <= .65:
            return {**result, "kind": "wait", "reason": "race_tactic_modal_closing"}
        selected = []
        for style, x in STYLE_CENTERS.items():
            corners = [green[y:y+10, left:left+12].mean()
                       for y in (732, 813) for left in (x-61, x+46)]
            if np.mean(corners) > .5:
                selected.append(style)
        if len(selected) != 1:
            return {**result, "kind": "wait", "reason": "race_tactic_selection_loading"}
        actual = selected[0]
        extra = {}
        if solo_leader_fallback and desired == "front":
            aptitudes = parse_modal_style_aptitudes(text)
            if "front" not in aptitudes:
                return {**result, "actual_style": actual, "solo_leader_fallback": True,
                        "kind": "wait", "reason": "race_tactic_aptitude_loading"}
            front_aptitude = aptitudes["front"]
            if front_aptitude not in allowed_front_aptitudes:
                return {**result, "actual_style": actual, "front_aptitude": front_aptitude,
                        "solo_leader_fallback": True, "kind": "cancel",
                        "reason": "solo_leader_front_aptitude_blocked", "point": MODAL_CANCEL_POINT}
            extra = {"front_aptitude": front_aptitude, "solo_leader_fallback": True}
        elif solo_leader_fallback:
            extra = {"solo_leader_fallback": True}
        return {**result, "actual_style": actual, **extra,
                "kind": "confirm" if actual == desired else "select",
                "point": (518, 917) if actual == desired else (STYLE_CENTERS[desired], 777)}
    if not preview:
        return None
    match = re.search(r"(?:短距离|英里|中距离|长距离)[SABCDEFG](后追|居中|跟前|领跑)[SABCDEFG]", compact)
    if match is None:
        if desired is None and not solo_leader_to_front:
            return None
        return {**result, "kind": "wait", "reason": "race_tactic_preview_loading"}
    actual = next(style for style, label in STYLE_LABELS.items() if label == match[1])
    if desired is not None:
        base_target = desired
    elif solo_leader_restore_style == "leader" and actual == "front":
        base_target = "leader"
    else:
        base_target = actual
    debut = bool(is_debut_race or "初级新人战" in compact)
    check_solo = bool(solo_leader_to_front and base_target == "leader" and not debut and not front_aptitude_rejected)
    btn_center = None
    inks = None
    solo = None
    if check_solo:
        solo, inks, btn_center = preview_is_solo_leader(image)
        if solo is None:
            return {"target_style": base_target, "target_label": STYLE_LABELS[base_target],
                    "actual_style": actual, "kind": "wait", "reason": "race_tactic_counts_loading"}
        effective_target = "front" if solo else "leader"
        is_fallback = bool(solo)
    else:
        if desired is None and solo_leader_restore_style is None:
            return None
        effective_target = base_target
        is_fallback = False
    res_base = {"target_style": effective_target, "target_label": STYLE_LABELS[effective_target],
                "actual_style": actual}
    if check_solo:
        res_base.update(solo_leader_detected=solo, solo_leader_fallback=is_fallback, opponent_inks=inks)
    if actual == effective_target:
        return {**res_base, "kind": "ready"}
    if "更改战术" not in compact or (green[731:758, 530:615].mean() < .45 and btn_center is None):
        return {**res_base, "kind": "wait", "reason": "race_tactic_control_loading"}
    return {**res_base, "kind": "open", "point": btn_center or (575, 744)}
