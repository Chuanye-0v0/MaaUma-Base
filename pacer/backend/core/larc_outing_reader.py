"""Read Mei's name and five progress arrows from the outing chooser.

No support-card identity is inferred from a generic friend icon. Coordinates
are produced here from verified pixels, not supplied by a policy or advisor.
"""
from __future__ import annotations

import cv2
import numpy as np

from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader
from core.auto_trainer.safe_advance import SafeAdvancePlanner


def read_friend_outing_hint(image):
    """Read the pink/red friend badge, without OCR or guessing its owner.

    Presence prompts one inspection of the chooser. Absence never revokes an
    already recorded unlock, and the badge alone cannot supply outing progress.
    """
    unknown = {"status": "unknown", "available": None, "basis": "friend_badge"}
    if image is None or (image.shape[1], image.shape[0]) not in {(720, 1280), (1440, 2560)}:
        return unknown
    im = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    if not SafeAdvancePlanner._looks_like_main_training_menu(im):
        return unknown
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    patch = hsv[1050:1120, 300:358]
    pink = (((patch[:, :, 0] < 12) | (patch[:, :, 0] > 160))
            & (patch[:, :, 1] > 90) & (patch[:, :, 2] > 160)).astype(np.uint8)
    # The pink outline can touch the trainee's red outfit or the upgrade label.
    # Anchor the white head/neck and its enclosed eyes/smile instead. Blurred
    # facial pixels may lose saturation; only the surround needs to be pink.
    white = ((patch[:, :, 1] < 60) & (patch[:, :, 2] > 220)).astype(np.uint8)
    _, labels, components, _ = cv2.connectedComponentsWithStats(white)
    for index, (x, y, width, height, area) in enumerate(components[1:], 1):
        if not (20 <= width <= 36 and 28 <= height <= 48 and 350 <= area <= 1100):
            continue
        body = (labels[y:y+height, x:x+width] == index).astype(np.uint8)
        if float(pink[y:y+height, x:x+width].mean()) < .1:
            continue
        contours, _ = cv2.findContours(body, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        solid = np.zeros_like(body)
        cv2.drawContours(solid, contours, -1, 1, -1)
        _, _, parts, _ = cv2.connectedComponentsWithStats(solid * (1-body))
        eyes = [(px+pw/2, py+ph/2) for px, py, pw, ph, pa in parts[1:]
                if 2 <= pw <= width*.3 and 1 <= ph <= height*.18 and pa >= 3
                and height*.15 <= py+ph/2 <= height*.42]
        if len(eyes) != 2:
            continue
        left, right = sorted(eyes)
        if not (left[0] < width/2 < right[0] and right[0]-left[0] >= width*.2
                and abs(left[1]-right[1]) <= 3):
            continue
        if any(pw >= width*.3 and 2 <= ph <= height*.25 and pa >= 10
               and left[0] < px+pw/2 < right[0]
               and max(left[1], right[1])+3 < py+ph/2 < height*.8
               for px, py, pw, ph, pa in parts[1:]):
            return {"status": "accepted", "available": True, "basis": "friend_badge",
                    "head_bounds_720": [int(x+300), int(y+1050), int(width), int(height)],
                    "shape_basis": "white_head_with_face_cutouts"}
    return {"status": "accepted", "available": False, "basis": "friend_badge_not_observed"}


class MeiOutingReader:
    def __init__(self, text_reader=None):
        self.text = text_reader or LocalRapidOCRScreenTextReader()

    def read_selected_confirmation(self, image):
        """Read only for the owner of a successful, reserved outing selection.

        An OCR title may lose its suffix or split into separate words. Keep
        the .9 text threshold and require the outing title, both real button
        labels, and this modal's green header/white body/two-button layout.
        This fallback is never used to authorize a fresh outing selection.
        """
        unknown = {"status": "unknown", "screen": "unknown", "confirm": None}
        if image is None or (image.shape[1], image.shape[0]) not in {(720, 1280), (1440, 2560)}:
            return unknown
        height, width = image.shape[:2]
        im = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
        rows = []
        for box, text, confidence in self.text.read_regions(im):
            if confidence < .9:
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            rows.append({"text": "".join(text.split()),
                         "box": [min(xs), min(ys), max(xs), max(ys)], "confidence": float(confidence)})
        if any("Connecting" in row["text"] or "连接中" in row["text"] for row in rows):
            return dict(unknown, reason="outing_transition_not_settled")
        titles = [row for row in rows if row["text"] in {"外出", "外出确认"}
                  and 250 < row["box"][0] < 410 and 360 < row["box"][1] < 470]
        if len(titles) != 1:
            return unknown

        def control(label, left, right):
            local = [row for row in rows if left < row["box"][0] < row["box"][2] < right
                     and 780 < row["box"][1] < row["box"][3] < 900]
            matches = [row for row in local if row["text"] == label]
            # Some OCR versions emit the spaced button glyphs separately.
            for first in local:
                for second in local:
                    a, b = first["box"], second["box"]
                    if (first["text"] == label[0] and second["text"] == label[1]
                            and 0 <= b[0]-a[2] < 70
                            and abs((a[1]+a[3])-(b[1]+b[3])) < 24):
                        matches.append({"text": label, "box": [a[0], min(a[1], b[1]), b[2], max(a[3], b[3])],
                                        "confidence": min(first["confidence"], second["confidence"])})
            return matches[0] if len(matches) == 1 else None

        cancel, confirm = control("取消", 100, 310), control("确定", 440, 630)
        if cancel is None or confirm is None:
            return unknown
        if abs(sum(cancel["box"][1::2])-sum(confirm["box"][1::2])) > 40:
            return unknown
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        green = ((hsv[:, :, 0] > 30) & (hsv[:, :, 0] < 85)
                 & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 120))
        white = (hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 215)
        ratios = {"header_green": float(green[385:440, 20:700].mean()),
                  "body_white": float(white[450:780, 40:680].mean()),
                  "confirm_green": float(green[800:870, 385:650].mean()),
                  "cancel_white": float(white[800:870, 75:330].mean())}
        if not (ratios["header_green"] > .65 and ratios["body_white"] > .65
                and ratios["confirm_green"] > .65 and ratios["cancel_white"] > .65):
            return unknown
        def point(row):
            x1, y1, x2, y2 = row["box"]
            return [round((x1+x2)*width/1440), round((y1+y2)*height/2560)]
        return dict(unknown, status="accepted", screen="mei_outing_confirm", confirm=point(confirm), cancel=point(cancel),
                    evidence={"recognition_mode": "selected_outing_controls", "title": titles[0],
                              "confirm_control": confirm, "cancel_control": cancel,
                              "layout_ratios": {key: round(value, 4) for key, value in ratios.items()}})

    def read(self, image):
        unknown = {"status": "unknown", "screen": "unknown", "character": None,
                   "completed": None, "open": None, "select": None, "cancel": None}
        if image is None or (image.shape[1], image.shape[0]) not in {(720, 1280), (1440, 2560)}:
            return unknown
        height, width = image.shape[:2]
        im = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
        def point(x, y):
            return [round(x * width / 720), round(y * height / 1280)]
        # Own L'Arc layout: orange outing button between clinic and aptitude.
        if SafeAdvancePlanner._looks_like_main_training_menu(im):
            button = hsv[1090:1190, 210:355]
            orange = ((button[:, :, 0] >= 14) & (button[:, :, 0] <= 40)
                      & (button[:, :, 1] > 100) & (button[:, :, 2] > 160)).mean()
            if orange > .18:
                return dict(unknown, status="accepted", screen="main_menu", open=point(280, 1125),
                            evidence={"orange_outing_button_ratio": round(float(orange), 4)},
                            friend_outing_hint=read_friend_outing_hint(im))
        rows = []
        for box, text, confidence in self.text.read_regions(im):
            if confidence < .9:
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            rows.append({"text": "".join(text.split()).replace("鳴", "鸣"),
                         "box": [min(xs), min(ys), max(xs), max(ys)], "confidence": float(confidence)})
        if any("Connecting" in row["text"] or "连接中" in row["text"] for row in rows):
            return dict(unknown, reason="outing_transition_not_settled")
        labels = {row["text"]: row for row in rows}
        # The title and the two controls identify the selected outing modal.
        # Narrative/cost captions can be obscured by the tap sparkle.
        if ("外出确认" in labels and "取消" in labels and "确定" in labels
                and 360 < labels["外出确认"]["box"][1] < 470
                and 780 < labels["确定"]["box"][1] < 900):
            x1, y1, x2, y2 = labels["确定"]["box"]
            green = hsv[795:875, 375:665]
            green_ratio = ((green[:, :, 0] > 30) & (green[:, :, 0] < 85)
                           & (green[:, :, 1] > 90) & (green[:, :, 2] > 120)).mean()
            if green_ratio > .25:
                return dict(unknown, status="accepted", screen="mei_outing_confirm",
                            confirm=point((x1+x2)/2, (y1+y2)/2),
                            cancel=point((labels["取消"]["box"][0]+labels["取消"]["box"][2])/2,
                                         (labels["取消"]["box"][1]+labels["取消"]["box"][3])/2),
                            evidence={"confirm_green_ratio": round(float(green_ratio), 4)})
        titles = [row for row in rows if row["text"] == "外出" and 180 < row["box"][1] < 650]
        names = [row for row in rows if row["text"] == "佐岳鸣" and 120 < row["box"][0] < 380]
        cancels = [row for row in rows if row["text"] == "取消" and row["box"][1] > 650]
        # Content recognition and non-consuming navigation have different
        # jobs. These existing high-confidence chooser-body labels can prove
        # a cancel-only page even if the title/name/progress is occluded.
        # No cancel coordinate is guessed, and this never authorizes Select.
        controls = [row for row in rows if row["text"] in {"取消", "返回"}
                    and 650 < row["box"][1] < 1250]
        friendship = [row for row in rows if row["text"] == "友情槽"]
        progress = [row for row in rows if row["text"] == "事件进度"]
        generic_cancel = None
        if len(controls) == 1:
            control = controls[0]
            if (any(180 < row["box"][1] < control["box"][1]-50 for row in friendship)
                    and any(180 < row["box"][1] < control["box"][1]-50 for row in progress)):
                x1, y1, x2, y2 = control["box"]
                generic_cancel = dict(unknown, screen="outing_chooser",
                    cancel=point((x1+x2)/2, (y1+y2)/2), reason="outing_identity_unresolved",
                    evidence={"navigation_only": True, "control": control,
                              "body_labels": ["友情槽", "事件进度"]})
        if len(titles) != 1 or len(cancels) != 1:
            return generic_cancel or unknown
        title, cancel = titles[0], cancels[0]
        tx1, ty1, tx2, ty2 = map(int, title["box"])
        title_bg = hsv[max(0, ty1-8):ty2+8, max(0, tx1-30):tx2+30]
        green = ((title_bg[:, :, 0] > 30) & (title_bg[:, :, 0] < 80)
                 & (title_bg[:, :, 1] > 80) & (title_bg[:, :, 2] > 120)).mean()
        if green < .35 or cancel["box"][1] <= ty2:
            return generic_cancel or unknown
        cx1, cy1, cx2, cy2 = cancel["box"]
        cancel_only = dict(unknown, screen="outing_chooser", cancel=point((cx1+cx2)/2, (cy1+cy2)/2))
        if len(names) != 1:
            return dict(cancel_only, reason="mei_not_observed_in_chooser")
        name = names[0]
        y = int((name["box"][1] + name["box"][3]) / 2 + 64)
        if not ty2 < name["box"][1] < y < cancel["box"][1] - 60:
            return cancel_only
        # Five distinct, ordered arrow interiors; yellow friendship MAX is a
        # different row. An occluded/mixed arrow stays unknown.
        arrows, measurements = [], []
        for x in (449, 500, 550, 601, 651):
            outline = hsv[y-27:y+27, x-24:x+24]
            white = (outline[:, :, 1] < 55) & (outline[:, :, 2] > 235)
            border = np.ones(white.shape, dtype=bool)
            border[8:-8, 7:-7] = False
            separated = float(white[border].mean()) >= .4
            patch = hsv[y-16:y+16, x-7:x+7]
            blue = float(((patch[:, :, 0] >= 85) & (patch[:, :, 0] <= 115)
                          & (patch[:, :, 1] > 100) & (patch[:, :, 2] > 150)).mean())
            gray = float(((patch[:, :, 1] < 55) & (patch[:, :, 2] >= 130)
                          & (patch[:, :, 2] <= 235)).mean())
            arrows.append((True if blue >= .65 else False if gray >= .65 else None) if separated else None)
            measurements.append({"blue": round(blue, 4), "gray": round(gray, 4), "separated": separated})
        if None in arrows or arrows != sorted(arrows, reverse=True):
            return dict(cancel_only, reason="outing_progress_unresolved", evidence={"arrows": measurements})
        cx1, cy1, cx2, cy2 = cancel["box"]
        completed = sum(arrows)
        return {"status": "accepted", "screen": "mei_outing_chooser", "character": "satake_mei",
                "completed": completed, "open": None,
                "select": point(350, y-30) if completed < 5 else None,
                "cancel": point((cx1+cx2)/2, (cy1+cy2)/2),
                "evidence": {"name": name, "title": title, "arrows": measurements}}
