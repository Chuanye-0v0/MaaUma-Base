"""Shared, read-only global-home evidence; recognition never submits input."""
from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np


def _canonical(image: np.ndarray | None) -> np.ndarray | None:
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        return None
    if image.shape[:2] == (1280, 720):
        return image
    if image.shape[:2] == (2560, 1440):
        return cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
    return None


def _gold_rank_hud(hsv: np.ndarray) -> bool:
    """Rank wreath + red score ribbon in the fixed HUD, not home artwork."""
    badge = hsv[20:118, 0:155]
    gold = ((badge[:, :, 0] >= 12) & (badge[:, :, 0] <= 38)
            & (badge[:, :, 1] >= 90) & (badge[:, :, 2] >= 130))
    if float(np.mean(gold)) <= .20:
        return False
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(gold.astype(np.uint8), 8)
    wreath = [(x, y, width, height) for x, y, width, height, area in stats[1:]
              if 30 <= width <= 80 and 40 <= height <= 95 and area >= 700]
    aligned_sides = any(left[0] < 55 and 75 <= right[0] <= 130
                        and abs(left[1] - right[1]) <= 20
                        for left in wreath for right in wreath)
    ribbon = hsv[116:150, 0:160]
    red = (((ribbon[:, :, 0] <= 10) | (ribbon[:, :, 0] >= 170))
           & (ribbon[:, :, 1] >= 100) & (ribbon[:, :, 2] >= 120))
    return bool(aligned_sides and float(np.mean(red)) > .25)


def _tp_rp_status_hud(hsv: np.ndarray) -> bool:
    """The fixed status controls also identify homes with a silver rank badge."""
    tp = hsv[27:66, 162:207]
    rp = hsv[27:66, 405:453]
    orange_tp = np.mean((tp[:, :, 0] >= 5) & (tp[:, :, 0] <= 38)
                       & (tp[:, :, 1] > 70) & (tp[:, :, 2] > 100))
    blue_rp = np.mean((rp[:, :, 0] >= 90) & (rp[:, :, 0] <= 125)
                     & (rp[:, :, 1] > 55) & (rp[:, :, 2] > 120))
    plus = [hsv[45:78, 365:395], hsv[44:78, 570:601]]
    green_plus = [np.mean((roi[:, :, 0] >= 38) & (roi[:, :, 0] <= 92)
                         & (roi[:, :, 1] > 70) & (roi[:, :, 2] > 80)) for roi in plus]
    return bool(orange_tp > .15 and blue_rp > .15 and min(green_plus) > .35)


def home_layout_anchors(image: np.ndarray | None) -> tuple[bool, bool, bool]:
    image = _canonical(image)
    if image is None:
        return False, False, False
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    def ratio(y0: float, y1: float, x0: float, x1: float, kind: str) -> float:
        region = hsv[int(1280*y0):int(1280*y1), int(720*x0):int(720*x1)]
        if kind == "blue":
            mask = ((region[:, :, 0] >= 90) & (region[:, :, 0] <= 125)
                    & (region[:, :, 1] > 55) & (region[:, :, 2] > 120))
        elif kind == "white":
            mask = (region[:, :, 1] < 65) & (region[:, :, 2] > 175)
        else:
            mask = ((region[:, :, 0] >= 38) & (region[:, :, 0] <= 92)
                    & (region[:, :, 1] > 70) & (region[:, :, 2] > 80))
        return float(np.mean(mask))

    hud = ((ratio(.02, .13, 0, .18, "blue") > .15 or _gold_rank_hud(hsv) or _tp_rp_status_hud(hsv))
           and ratio(0, .13, .20, .82, "white") > .35
           and ratio(0, .12, .80, 1, "green") > .10)
    selected = ratio(.90, .99, .35, .65, "blue") > .30
    # Home artwork overlaps the footer: .45 rejected the real decorated home.
    navigation = selected and ratio(.90, .99, 0, 1, "white") > .28
    return bool(hud), bool(navigation), bool(selected)


def has_large_low_texture_panel(image: np.ndarray) -> bool:
    image = _canonical(image)
    if image is None:
        return True
    gray = cv2.cvtColor(image[250:1160, 50:700], cv2.COLOR_BGR2GRAY).astype(np.float32)
    mean = cv2.boxFilter(gray, -1, (9, 9), normalize=True)
    square = cv2.boxFilter(gray * gray, -1, (9, 9), normalize=True)
    low_texture = (np.sqrt(np.maximum(square - mean * mean, 0.0)) <= 2).astype(np.uint8)
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(low_texture, 8)
    return any(area >= 50_000 and width >= 180 and height >= 180
               for _x, _y, width, height, area in stats[1:])


def has_blocking_home_overlay(image: np.ndarray, buttons: Sequence[object] = ()) -> bool:
    image = _canonical(image)
    if image is None or has_large_low_texture_panel(image):
        return True
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    body = hsv[int(1280*.22):int(1280*.75), int(720*.08):int(720*.92)]
    light = np.mean((body[:, :, 1] < 55) & (body[:, :, 2] > 185))
    middle_green = any(
        isinstance(button, dict) and button.get("kind") == "primary_green"
        and 720*.25 <= button.get("center", (0, 0))[0] <= 720*.75
        and 1280*.55 <= button.get("center", (0, 0))[1] <= 1280*.75
        for button in buttons)
    if not middle_green and light > .35:
        # Settlement/reconnect don't have the entry perceptor's button list.
        middle = hsv[704:960, 180:541]
        green = cv2.inRange(middle, (38, 70, 80), (92, 255, 255))
        _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(green, 8)
        middle_green = any(w >= 70 and h >= 25 and area >= 1500
                           for _x, _y, w, h, area in stats[1:])
    return bool(light > .35 and middle_green)


def global_home_portal_point(image: np.ndarray | None) -> tuple[int, int] | None:
    native = image
    image = _canonical(image)
    if image is None:
        return None
    hud, navigation, selected = home_layout_anchors(image)
    if not navigation or not selected:
        return None
    if not hud:
        # A login/mission-complete toast can cover TP/RP while the selected
        # home tab and career artwork remain fully visible. Its white banner
        # plus pink completion stamp is a distinct foreground, not a dialog.
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        banner = hsv[25:95, 30:700]
        stamp = hsv[25:100, 610:700]
        light = np.mean((banner[:, :, 1] < 65) & (banner[:, :, 2] > 175))
        pink = np.mean((stamp[:, :, 0] >= 150) & (stamp[:, :, 1] > 100)
                       & (stamp[:, :, 2] > 140))
        if light < .50 or pink < .12:
            return None
    if has_blocking_home_overlay(image):
        return None
    # A HUD alone can survive a setup/modal transition. Require the actual
    # bottom-right career portal, not a central Continue/confirm button.
    hsv = cv2.cvtColor(image[990:1160, 360:720], cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (38, 70, 80), (92, 255, 255))
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(green, 8)
    candidates = [(x,y,w,h,area) for x,y,w,h,area in stats[1:]
        if x+360 >= 400 and y+990 >= 1020 and w >= 80 and 30 <= h <= 100 and area >= 1500]
    if not candidates:
        return None
    x,y,width,height,_area = max(candidates, key=lambda row: row[4])
    scale = native.shape[1]/720
    return round((x+360+width/2)*scale), round((y+990+height/2)*scale)


def is_global_home(image: np.ndarray | None) -> bool:
    return global_home_portal_point(image) is not None
