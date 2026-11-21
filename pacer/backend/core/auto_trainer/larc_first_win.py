"""The game's current maiden-win requirement, not an outcome reconciliation."""
import re

import cv2
import numpy as np


def first_win_required(visible_text):
    text = "".join(str(visible_text or "").split())
    return bool(re.search(r"零胜战第[1一]名", text))


def selected_first_win_candidate(image, visible_text):
    """Use the already-selected recommended card and its two aptitude stars."""
    if not first_win_required(visible_text) or image is None:
        return False
    height, width = image.shape[:2]
    if width >= height:
        return False
    image = cv2.resize(image, (720, 1280))
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # These are the selected card's surface/distance marks, not the next card.
    for y0, y1 in ((765, 800), (797, 832)):
        mark = hsv[y0:y1, 571:608]
        gold = ((mark[:, :, 0] >= 12) & (mark[:, :, 0] <= 40)
                & (mark[:, :, 1] >= 85) & (mark[:, :, 2] >= 100))
        if float(np.mean(gold)) < .045:
            return False
    return True
