"""Bounded decoding and Windows long-path I/O helpers for screenshots."""

import os
from pathlib import Path

import cv2
import numpy as np


def io_path(path: str | Path) -> Path:
    """Apply Windows extended-length prefix only at the actual filesystem boundary."""
    p = Path(path)
    if os.name != "nt":
        return p
    value = str(p.absolute())
    if not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def read_image_compat(path: str | Path, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Read an image via cv2.imread first, falling back to long-path byte decode."""
    image = cv2.imread(str(path)) if flags == cv2.IMREAD_COLOR else cv2.imread(str(path), flags)
    if image is not None:
        return image
    try:
        data = io_path(path).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)
    except cv2.error:
        return None


def read_image_with_retry(path: str | Path) -> np.ndarray | None:
    """Retry once and keep the first readable bytes for both decode attempts."""
    path = Path(path)
    data = None
    for _ in range(2):
        if data is None:
            try:
                data = io_path(path).read_bytes()
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
