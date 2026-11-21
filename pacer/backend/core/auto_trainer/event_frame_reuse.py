"""Reuse immediately handed-off event text after a cheap current-UI comparison."""
from dataclasses import replace
from pathlib import Path
import hashlib
import time

import cv2
import numpy as np

from core.auto_trainer.event_options import detect_event_option_layout
from core.auto_trainer.frame_safety import FrameSafetyPacket


def reuse_event_packet(path, observation, *, clock=time.monotonic):
    if not observation or not 0 <= clock() - observation.get("observed_at", -100) <= 2:
        return None
    data = observation.get("frame_safety", {})
    if data.get("text_status") != "ok" or data.get("errors") or not str(data.get("text_source", "")).startswith("local_"):
        return None
    try:
        old_path = Path(observation["path"])
        old_bytes = old_path.read_bytes()
        if hashlib.sha256(old_bytes).hexdigest() != data["source_image_sha256"]:
            return None
        old = cv2.imdecode(np.frombuffer(old_bytes, np.uint8), cv2.IMREAD_COLOR)
        new_bytes = Path(path).read_bytes()
        new = cv2.imdecode(np.frombuffer(new_bytes, np.uint8), cv2.IMREAD_COLOR)
        if old is None or new is None or old.shape != new.shape:
            return None
        a, b = detect_event_option_layout(old), detect_event_option_layout(new)
        if not 2 <= a["option_count"] <= 5 or a != b:
            return None
        height, width = new.shape[:2]
        # Stable text only: FPS, portraits and energy/mood glow animate.
        # The selector reads current energy/mood pixels separately, not cached OCR.
        regions = [(round(x1 * width / 720), round(y1 * height / 1280),
                    round(x2 * width / 720), round(y2 * height / 1280))
                   for x1, y1, x2, y2 in [(110, 235, 550, 285), (160, 35, 690, 130)]]
        for x, y in b["ordered_points"]:
            regions.append((int(width * .03), max(0, y - int(height * .022)),
                            int(width * .97), min(height, y + int(height * .022))))
        for x1, y1, x2, y2 in regions:
            diff = cv2.absdiff(old[y1:y2, x1:x2], new[y1:y2, x1:x2])
            if diff.mean() > .5 or np.mean(diff > 12) > .002:
                return None
        packet = FrameSafetyPacket(**{**data, "resource_terms": tuple(data.get("resource_terms", ())),
                                      "errors": tuple(data.get("errors", ()))})
        return replace(packet, source_image_sha256=hashlib.sha256(new_bytes).hexdigest())
    except (OSError, ValueError, KeyError, TypeError, cv2.error):
        return None
