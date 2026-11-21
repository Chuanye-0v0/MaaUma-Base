from __future__ import annotations

from typing import Any

import cv2
import numpy as np


CRYSTAL_REWARD_TEXT_TERMS = (
    "\u83b7\u5f97\u77e5\u8bc6\u7684\u7ed3\u6676",
    "\u83b7\u5f97\u4e86\u529b\u91cf\u7684\u7ed3\u6676",
    "\u77e5\u8bc6\u7684\u7ed3\u6676",
    "\u529b\u91cf\u7684\u7ed3\u6676",
)

_MODAL_BACKGROUND_LAPLACIAN_MAX = 80.0
_MODAL_BACKGROUND_VALUE_MEAN_MAX = 150.0
_MODAL_BACKGROUND_DARK_RATIO_MIN = 0.25


def text_has_grand_masters_crystal_reward(value: Any) -> bool:
    text = _flatten_text(value)
    compact = "".join(text.split())
    if any(term in compact for term in CRYSTAL_REWARD_TEXT_TERMS[:2]):
        return True
    return "\u83b7\u5f97" in compact and "\u7ed3\u6676" in compact and (
        "\u77e5\u8bc6" in compact or "\u529b\u91cf" in compact
    )


def detect_grand_masters_crystal_reward_overlay(
    img: np.ndarray,
    *,
    visible_text: Any = None,
) -> dict[str, Any]:
    text_match = text_has_grand_masters_crystal_reward(visible_text)
    image_evidence = _image_evidence(img)
    image_match = bool(image_evidence.pop("present"))
    return {
        "present": bool(text_match or image_match),
        "text_match": bool(text_match),
        "image_match": bool(image_match),
        **image_evidence,
    }


def _image_evidence(img: np.ndarray) -> dict[str, Any]:
    height, width = img.shape[:2]
    if width > height:
        return {"present": False, "reason": "landscape"}
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    def ratio(x0: float, x1: float, y0: float, y1: float, mask: np.ndarray) -> float:
        crop = mask[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)]
        if crop.size == 0:
            return 0.0
        return float(np.mean(crop))

    orange = (
        (hsv[:, :, 0] >= 5)
        & (hsv[:, :, 0] <= 35)
        & (hsv[:, :, 1] > 70)
        & (hsv[:, :, 2] > 100)
    )
    cyan_blue = (
        (hsv[:, :, 0] >= 85)
        & (hsv[:, :, 0] <= 125)
        & (hsv[:, :, 1] > 45)
        & (hsv[:, :, 2] > 70)
    )
    green = (
        (hsv[:, :, 0] >= 38)
        & (hsv[:, :, 0] <= 92)
        & (hsv[:, :, 1] > 70)
        & (hsv[:, :, 2] > 80)
    )
    white = (hsv[:, :, 1] < 65) & (hsv[:, :, 2] > 170)

    center_orange = ratio(0.06, 0.94, 0.20, 0.72, orange)
    center_cyan_blue = ratio(0.06, 0.94, 0.20, 0.72, cyan_blue)
    bottom_skip_green = ratio(0.20, 0.48, 0.925, 0.985, green)
    dialogue_white = ratio(0.02, 0.98, 0.68, 0.93, white)
    left_fragment_orange = ratio(0.00, 0.23, 0.25, 0.62, orange)
    left_fragment_white = ratio(0.00, 0.23, 0.25, 0.62, white)
    training_fragment_panel = left_fragment_orange > 0.05 and left_fragment_white > 0.20
    modal_background = _modal_background_evidence(img, hsv[:, :, 2], height, width)

    dialogue_phase = (
        center_orange > 0.10
        and center_cyan_blue > 0.08
        and bottom_skip_green > 0.18
        and dialogue_white > 0.45
        and not training_fragment_panel
        and modal_background["present"]
    )
    reveal_phase = (
        center_orange > 0.18
        and center_cyan_blue > 0.04
        and bottom_skip_green > 0.18
        and dialogue_white < 0.10
        and not training_fragment_panel
        and modal_background["present"]
    )
    faded_reveal_phase = (
        center_orange > 0.035
        and center_cyan_blue > 0.02
        and bottom_skip_green > 0.18
        and dialogue_white < 0.10
        and left_fragment_orange < 0.04
        and not training_fragment_panel
        and modal_background["present"]
    )
    present = dialogue_phase or reveal_phase or faded_reveal_phase
    return {
        "present": bool(present),
        "phase": (
            "dialogue"
            if dialogue_phase
            else "reveal"
            if reveal_phase
            else "faded_reveal"
            if faded_reveal_phase
            else None
        ),
        "ratios": {
            "center_orange": round(center_orange, 4),
            "center_cyan_blue": round(center_cyan_blue, 4),
            "bottom_skip_green": round(bottom_skip_green, 4),
            "dialogue_white": round(dialogue_white, 4),
            "left_fragment_orange": round(left_fragment_orange, 4),
            "left_fragment_white": round(left_fragment_white, 4),
        },
        "training_fragment_panel": bool(training_fragment_panel),
        "modal_background": modal_background,
    }


def _modal_background_evidence(
    img: np.ndarray,
    value: np.ndarray,
    height: int,
    width: int,
) -> dict[str, Any]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def metrics(regions: tuple[tuple[float, float, float, float], ...]) -> dict[str, Any]:
        laplacian_variances = []
        values = []
        for x0, x1, y0, y1 in regions:
            y_slice = slice(int(height * y0), int(height * y1))
            x_slice = slice(int(width * x0), int(width * x1))
            gray_crop = gray[y_slice, x_slice]
            value_crop = value[y_slice, x_slice]
            laplacian_variances.append(float(cv2.Laplacian(gray_crop, cv2.CV_64F).var()))
            values.append(value_crop.reshape(-1))

        combined_values = np.concatenate(values)
        laplacian_variance = float(np.mean(laplacian_variances))
        value_mean = float(np.mean(combined_values))
        dark_ratio = float(np.mean(combined_values < 110))
        blurred = laplacian_variance <= _MODAL_BACKGROUND_LAPLACIAN_MAX
        dimmed = value_mean <= _MODAL_BACKGROUND_VALUE_MEAN_MAX
        darkened = dark_ratio >= _MODAL_BACKGROUND_DARK_RATIO_MIN
        return {
            "laplacian_variance": round(laplacian_variance, 2),
            "value_mean": round(value_mean, 2),
            "dark_ratio_below_110": round(dark_ratio, 4),
            "blurred": bool(blurred),
            "dimmed": bool(dimmed),
            "darkened": bool(darkened),
            "present": bool(blurred and dimmed and darkened),
        }

    top = metrics(((0.05, 0.95, 0.02, 0.16),))
    side = metrics(
        (
            (0.02, 0.23, 0.16, 0.35),
            (0.77, 0.98, 0.16, 0.35),
            (0.02, 0.18, 0.35, 0.65),
            (0.82, 0.98, 0.35, 0.65),
        )
    )
    return {
        "top": top,
        "side": side,
        "present": bool(top["present"] and side["present"]),
    }


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        for key in ("text", "ocr_text", "visible_text", "normalized_text", "summary"):
            if key in value:
                parts.append(_flatten_text(value.get(key)))
        return "\n".join(part for part in parts if part)
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten_text(item) for item in value if item is not None)
    return str(value)
