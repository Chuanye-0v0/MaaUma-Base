"""Local visual perception helpers for Uma Musume automation.

This module keeps expensive image reasoning local. It extracts compact state
from screenshots so the decision layer can work with JSON instead of images.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import cv2
import numpy as np

@dataclass(frozen=True)
class ButtonCandidate:
    kind: str
    bbox: tuple[int, int, int, int]
    center: tuple[int, int]
    area: int
    score: float

class Perception:
    """Analyze screenshots with lightweight OpenCV rules."""

    def analyze_file(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        img = cv2.imread(str(path))
        if img is None:
            raise ValueError(f'Cannot read image: {path}')
        state = self.analyze_image(img)
        state['image'] = str(path)
        return state

    def analyze_image(self, img: np.ndarray) -> dict[str, Any]:
        if img.ndim != 3 or img.shape[2] != 3:
            raise ValueError('Expected a BGR color image')
        height, width = img.shape[:2]
        orientation = 'landscape' if width > height else 'portrait'
        green_buttons = self.detect_green_buttons(img)
        screen = self.infer_screen(img, width, height, orientation, green_buttons)
        return {'size': [width, height], 'orientation': orientation, 'screen': screen['name'], 'confidence': screen['confidence'], 'buttons': [asdict(button) for button in green_buttons]}

    def detect_green_buttons(self, img: np.ndarray) -> list[ButtonCandidate]:
        """Find large Uma-style green action buttons."""
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower = np.array([38, 70, 80], dtype=np.uint8)
        upper = np.array([92, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        height, width = img.shape[:2]
        image_area = width * height
        candidates: list[ButtonCandidate] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = int(cv2.contourArea(contour))
            if not self._looks_like_action_button(x, y, w, h, area, image_area):
                continue
            center = (x + w // 2, y + h // 2)
            aspect = w / max(h, 1)
            fill = area / max(w * h, 1)
            score = min(1.0, fill * 0.75 + min(aspect / 4.0, 1.0) * 0.25)
            candidates.append(ButtonCandidate(kind='primary_green', bbox=(int(x), int(y), int(w), int(h)), center=(int(center[0]), int(center[1])), area=area, score=round(float(score), 3)))
        candidates.sort(key=lambda item: (item.area, item.score), reverse=True)
        return candidates[:8]

    def infer_screen(self, img: np.ndarray, width: int, height: int, orientation: str, green_buttons: list[ButtonCandidate]) -> dict[str, Any]:
        if self._looks_like_event_choice(img, orientation):
            return {'name': 'event_choice', 'confidence': 0.7}
        if self._looks_like_training_menu(img, orientation):
            return {'name': 'training_menu', 'confidence': 0.78}
        if self._looks_like_game_home(img, orientation):
            return {'name': 'game_home', 'confidence': 0.8}
        if not green_buttons:
            return {'name': 'unknown', 'confidence': 0.2}
        main = green_buttons[0]
        _, y, w, h = main.bbox
        cx, cy = main.center
        center_bias = abs(cx - width / 2) / max(width, 1)
        width_ratio = w / max(width, 1)
        height_ratio = h / max(height, 1)
        if orientation == 'portrait':
            is_right_or_center_button = width * 0.45 <= cx <= width * 0.94
            if is_right_or_center_button and 0.25 <= width_ratio <= 0.55 and (0.035 <= height_ratio <= 0.08):
                if 0.58 <= cy / height <= 0.78:
                    return {'name': 'modal_confirm', 'confidence': 0.82}
                if cy / height > 0.78:
                    return {'name': 'bottom_action', 'confidence': 0.68}
            return {'name': 'portrait_ui', 'confidence': 0.45}
        if center_bias < 0.28 and cy / height > 0.55:
            return {'name': 'landscape_action', 'confidence': 0.72}
        return {'name': 'landscape_ui', 'confidence': 0.45}

    def _looks_like_training_menu(self, img: np.ndarray, orientation: str) -> bool:
        if orientation != 'portrait':
            return False
        height, width = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.78):int(height * 0.94), :]
        orange_tab_ratio = np.mean((bottom[:, :, 0] >= 5) & (bottom[:, :, 0] <= 28) & (bottom[:, :, 1] > 70) & (bottom[:, :, 2] > 80))
        yellow_tab_ratio = np.mean((bottom[:, :, 0] >= 20) & (bottom[:, :, 0] <= 38) & (bottom[:, :, 1] > 70) & (bottom[:, :, 2] > 80))
        green_ratio = np.mean((bottom[:, :, 0] >= 38) & (bottom[:, :, 0] <= 92) & (bottom[:, :, 1] > 70) & (bottom[:, :, 2] > 80))
        blue_fail_region = hsv[int(height * 0.68):int(height * 0.8), 0:int(width * 0.35)]
        blue_fail_ratio = np.mean((blue_fail_region[:, :, 0] >= 88) & (blue_fail_region[:, :, 0] <= 118) & (blue_fail_region[:, :, 1] > 80) & (blue_fail_region[:, :, 2] > 120))
        orange_header = hsv[0:int(height * 0.18), :]
        orange_header_ratio = np.mean((orange_header[:, :, 0] >= 5) & (orange_header[:, :, 0] <= 28) & (orange_header[:, :, 1] > 70) & (orange_header[:, :, 2] > 120))
        has_orange_tabs = orange_tab_ratio + yellow_tab_ratio > 0.2
        not_main_menu = green_ratio < 0.3
        has_training_ui = blue_fail_ratio > 0.01 or 0.04 <= orange_header_ratio <= 0.12
        return bool(has_orange_tabs and not_main_menu and has_training_ui)

    def _looks_like_event_choice(self, img: np.ndarray, orientation: str) -> bool:
        if orientation != 'portrait':
            return False
        height, _ = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        top = hsv[0:int(height * 0.18), :]
        bottom = hsv[int(height * 0.78):int(height * 0.94), :]
        orange_top_ratio = np.mean((top[:, :, 0] >= 5) & (top[:, :, 0] <= 28) & (top[:, :, 1] > 70) & (top[:, :, 2] > 120))
        bottom_green_ratio = np.mean((bottom[:, :, 0] >= 38) & (bottom[:, :, 0] <= 92) & (bottom[:, :, 1] > 70) & (bottom[:, :, 2] > 80))
        return bool(orange_top_ratio > 0.12 and bottom_green_ratio < 0.08)

    def _looks_like_game_home(self, img: np.ndarray, orientation: str) -> bool:
        if orientation != 'portrait':
            return False
        height, width = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bottom = hsv[int(height * 0.88):height, :]
        center_bottom = hsv[int(height * 0.88):height, int(width * 0.32):int(width * 0.68)]
        white_nav_ratio = np.mean((bottom[:, :, 1] < 45) & (bottom[:, :, 2] > 180))
        blue_home_ratio = np.mean((center_bottom[:, :, 0] >= 85) & (center_bottom[:, :, 0] <= 115) & (center_bottom[:, :, 1] > 70) & (center_bottom[:, :, 2] > 120))
        return bool(white_nav_ratio > 0.15 and blue_home_ratio > 0.12)

    def _looks_like_action_button(self, x: int, y: int, w: int, h: int, area: int, image_area: int) -> bool:
        if area < image_area * 0.0015:
            return False
        if w < 120 or h < 45:
            return False
        aspect = w / max(h, 1)
        if aspect < 1.8 or aspect > 8.0:
            return False
        if y < 0:
            return False
        return True
