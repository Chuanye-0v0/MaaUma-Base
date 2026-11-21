"""Bounded decoding of an existing screenshot file."""

from pathlib import Path

import cv2
import numpy as np


def read_image_with_retry(path: str | Path) -> np.ndarray | None:
    """Retry once and keep the first readable bytes for both decode attempts."""
    path = Path(path)
    data = None
    for _ in range(2):
        if data is None:
            try:
                data = path.read_bytes()
            except OSError:
                continue
        if not data:
            return None
        try:
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        except cv2.error:
            continue
        if image is not None:
            return image
    return None
