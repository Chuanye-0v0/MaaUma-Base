from __future__ import annotations
from typing import Any
import cv2
import numpy as np

class RaceRoutePlanner:

    @staticmethod
    def _race_preview_action_points(img: np.ndarray) -> dict[str, Any] | None:
        height, width = img.shape[:2]
        if width > height:
            return None
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        left = hsv[int(height * 0.875):int(height * 0.955), int(width * 0.18):int(width * 0.5)]
        right = hsv[int(height * 0.875):int(height * 0.955), int(width * 0.5):int(width * 0.82)]
        gap = hsv[int(height * 0.875):int(height * 0.955), int(width * 0.47):int(width * 0.53)]
        if left.size == 0 or right.size == 0:
            return None
        left_green = np.mean((left[:, :, 0] >= 38) & (left[:, :, 0] <= 92) & (left[:, :, 1] > 70) & (left[:, :, 2] > 80))
        left_white = np.mean((left[:, :, 1] < 70) & (left[:, :, 2] > 170))
        left_brown = np.mean((left[:, :, 0] >= 5) & (left[:, :, 0] <= 30) & (left[:, :, 1] > 50) & (left[:, :, 2] > 70))
        right_green = np.mean((right[:, :, 0] >= 38) & (right[:, :, 0] <= 92) & (right[:, :, 1] > 70) & (right[:, :, 2] > 80))
        right_white = np.mean((right[:, :, 1] < 70) & (right[:, :, 2] > 170))
        lock_icon = hsv[int(height * 0.83):int(height * 0.9), int(width * 0.18):int(width * 0.3)]
        lock_gold = 0.0
        if lock_icon.size:
            lock_gold = np.mean((lock_icon[:, :, 0] >= 15) & (lock_icon[:, :, 0] <= 35) & (lock_icon[:, :, 1] > 60) & (lock_icon[:, :, 2] > 120))
        gap_green = 0.0
        if gap.size:
            gap_green = np.mean((gap[:, :, 0] >= 38) & (gap[:, :, 0] <= 92) & (gap[:, :, 1] > 70) & (gap[:, :, 2] > 80))
        if not (left_white > 0.42 and left_brown > 0.04 and (right_green > 0.35) and (right_white < 0.2)):
            left_gray = np.mean((left[:, :, 1] < 60) & (left[:, :, 2] > 80) & (left[:, :, 2] < 210))
            left_locked = left_gray > 0.45 and left_white < 0.1 and (left_green < 0.1) and (right_green > 0.35) and (gap_green < 0.35) or (lock_gold > 0.04 and left_gray > 0.35 and (left_green < 0.2) and (right_green > 0.35))
            if not left_locked:
                return None
        else:
            left_gray = np.mean((left[:, :, 1] < 60) & (left[:, :, 2] > 80) & (left[:, :, 2] < 210))
            left_locked = False
        left_locked = bool(left_locked or (lock_gold > 0.04 and left_gray > 0.35 and (left_green < 0.2) and (right_green > 0.35) and (gap_green < 0.35)))
        return {'view_result': (int(width * 0.351), int(height * 0.919)), 'go_to_race': RaceRoutePlanner._bottom_center_green_region(img, min_y_ratio=0.86) or (int(width * 0.65), int(height * 0.919)), 'view_result_locked': left_locked}

    @staticmethod
    def _bottom_center_green_region(img: np.ndarray, *, min_y_ratio: float) -> tuple[int, int] | None:
        height, width = img.shape[:2]
        x0, x1 = (int(width * 0.18), int(width * 0.82))
        y0, y1 = (int(height * min_y_ratio), int(height * 0.98))
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([38, 70, 80], dtype=np.uint8), np.array([92, 255, 255], dtype=np.uint8))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[int, int, int]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = int(cv2.contourArea(contour))
            if area < width * height * 0.002:
                continue
            cx = x0 + x + w // 2
            cy = y0 + y + h // 2
            if not width * 0.24 <= cx <= width * 0.76:
                continue
            candidates.append((area, int(cx), int(cy)))
        if not candidates:
            return None
        _, cx, cy = max(candidates, key=lambda item: item[0])
        return (cx, cy)
