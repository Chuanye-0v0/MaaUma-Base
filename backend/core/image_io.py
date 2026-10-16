"""Path-based image I/O, including non-ASCII Windows directories."""
from pathlib import Path
import cv2
import numpy as np

def read_image(path, flags=cv2.IMREAD_COLOR):
    """Return decoded pixels or None, as file-based OpenCV callers expect."""
    try:
        raw = Path(path).read_bytes()
        return cv2.imdecode(np.frombuffer(raw, np.uint8), flags) if raw else None
    except (OSError, cv2.error):
        return None

def write_image(path, image, params=None):
    """Encode before opening the path; retain the existing boolean result."""
    try:
        target = Path(path)
        ok, encoded = cv2.imencode(target.suffix, image, params or [])
        if not ok:
            return False
        target.write_bytes(encoded.tobytes())
        return True
    except (OSError, cv2.error):
        return False
