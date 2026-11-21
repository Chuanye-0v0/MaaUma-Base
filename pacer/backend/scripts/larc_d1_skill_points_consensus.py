"""Install Pacer's verified Pt consensus in an isolated D1 parser.

Only the numeric Pt reader is replaced.  The current D1 ROI and 0.90 confidence
floor stay intact; the external parser still owns image normalization,
OCR/runtime helpers, and its non-Tesseract backend behavior.
Install before D1 consumers import the reader by name.
"""
from collections import Counter
import re

import cv2


def install_skill_points_consensus(parser_module):
    original = parser_module.read_current_skill_points

    def read_current_skill_points(image, *, runtime=None, backend="tesseract"):
        if backend != "tesseract":
            return original(image, runtime=runtime, backend=backend)
        normalized, _scale = parser_module._normalize_image(image)
        crop = normalized[400:440, 520:650]
        enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
        threshold_values = []
        for threshold in (170, 190, 200):
            _ignored, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
            text, confidence = parser_module._ocr_data(
                binary, lang="eng", psm=7, whitelist="0123456789", runtime=runtime,
            )
            tokens = [int(token) for token in re.findall(r"\d+", text)
                      if 0 <= int(token) <= 99999]
            if len(tokens) == 1 and confidence >= .90:
                threshold_values.append(tokens[0])
        if len(threshold_values) >= 2:
            counts = Counter(threshold_values)
            value, votes = counts.most_common(1)[0]
            if votes >= 2 and sum(count == votes for count in counts.values()) == 1:
                return value
            return None

        raw_values = parser_module._read_numeric_tokens(crop, psm=7, runtime=runtime)
        raw_plausible = [value for value in raw_values if 0 <= value <= 99999]
        if (len(threshold_values) == 1 and len(raw_plausible) == 1
                and threshold_values[0] == raw_plausible[0]):
            return threshold_values[0]
        return None

    parser_module.read_current_skill_points = read_current_skill_points
