"""Observed CN race-preview / tactics controls; no strategy selection."""
import re
import cv2
import numpy as np

STYLE_LABELS = {"back": "后追", "middle": "居中", "front": "跟前", "leader": "领跑"}
STYLE_CENTERS = {"back": 130, "middle": 283, "front": 436, "leader": 590}


def race_tactic_control(image, text, desired, *, preview=False):
    if desired not in STYLE_LABELS:
        raise ValueError("race_tactic_style_invalid")
    compact = "".join(str(text).split())
    im = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    green = (hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120)
    white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 190)
    target = STYLE_LABELS[desired]
    result = {"target_style": desired, "target_label": target}
    if ("战术" in compact and all(label in compact for label in STYLE_LABELS.values())
            and green[310:348, 30:690].mean() > .7
            and green[897:940, 400:635].mean() > .65
            and all(white[753:800, x-42:x+42].mean() > .45 for x in STYLE_CENTERS.values())):
        selected = []
        for style, x in STYLE_CENTERS.items():
            corners = [green[y:y+10, left:left+12].mean()
                       for y in (732, 813) for left in (x-61, x+46)]
            if np.mean(corners) > .5:
                selected.append(style)
        if len(selected) != 1:
            return {**result, "kind": "wait", "reason": "race_tactic_selection_loading"}
        actual = selected[0]
        return {**result, "actual_style": actual,
                "kind": "confirm" if actual == desired else "select",
                "point": (518, 917) if actual == desired else (STYLE_CENTERS[desired], 777)}
    if not preview:
        return None
    match = re.search(r"(?:短距离|英里|中距离|长距离)[SABCDEFG](后追|居中|跟前|领跑)[SABCDEFG]", compact)
    if match is None:
        return {**result, "kind": "wait", "reason": "race_tactic_preview_loading"}
    actual = next(style for style, label in STYLE_LABELS.items() if label == match[1])
    if actual == desired:
        return {**result, "kind": "ready", "actual_style": actual}
    if "更改战术" not in compact or green[731:758, 530:615].mean() < .45:
        return {**result, "kind": "wait", "reason": "race_tactic_control_loading"}
    return {**result, "kind": "open", "actual_style": actual, "point": (575, 744)}
