"""Parse one packed Android ``screencap`` frame, without device or file I/O.

AOSP 7.1 and 8.0 write width/height/format (12 bytes); 8.1, 9 and 13 also write
the mapped colorspace (16 bytes). All write width * bytesPerPixel per row,
excluding the source buffer's stride padding. Android's HAL documents the
byte order for formats 1-5; RGB565 has R in bits 11-15, G in 5-10, B in 0-4.

Sources:
https://android.googlesource.com/platform/frameworks/base/+/android-7.1.2_r1/cmds/screencap/screencap.cpp
https://android.googlesource.com/platform/frameworks/base/+/android-8.0.0_r1/cmds/screencap/screencap.cpp
https://android.googlesource.com/platform/frameworks/base/+/android-9.0.0_r1/cmds/screencap/screencap.cpp
https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r1/cmds/screencap/screencap.cpp
https://android.googlesource.com/platform/hardware/interfaces/+/android-8.1.0_r1/graphics/common/1.0/types.hal

The returned BGR samples keep the source colorspace; this is not a gamut
conversion. Unsupported formats, headers and lengths must use another capture
path. Bytes are never repaired as terminal text or assumed to be PNG data.
"""
from __future__ import annotations

import struct

import numpy as np


MAX_DIMENSION = 8192
MAX_PIXELS = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = MAX_PIXELS * 4 + 16
PIXEL_FORMATS = {
    1: ("RGBA_8888", 4),
    2: ("RGBX_8888", 4),
    3: ("RGB_888", 3),
    4: ("RGB_565", 2),
    5: ("BGRA_8888", 4),
}
COLOR_SPACES = {0: "unknown", 1: "sRGB", 2: "Display_P3"}


def parse_adb_raw_screencap(payload: bytes | bytearray | memoryview, *,
                          header_bytes: int | None = None) -> tuple[np.ndarray, dict]:
    """Return an owned contiguous uint8 BGR image and raw-header metadata.

    Width, height and pixel format are little-endian unsigned 32-bit values.
    Exact total length selects the 12- or 16-byte header before allocating any
    pixels. Invalid input raises ValueError, including unsupported colorspaces.
    Alpha and the RGBX unused byte are not evidence of frame validity.
    Set header_bytes to 12 or 16 when the protocol version is known; automatic
    length detection cannot distinguish some four-byte truncations or trailers.
    """
    if header_bytes is not None and (type(header_bytes) is not int or header_bytes not in (12, 16)):
        raise ValueError("raw_screencap_header_bytes_must_be_12_or_16")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise ValueError("raw_screencap_bytes_required")
    buffer = memoryview(payload)
    if not buffer.c_contiguous:
        raise ValueError("raw_screencap_noncontiguous_input")
    buffer = buffer.cast("B")
    length = buffer.nbytes
    if length < 12:
        raise ValueError("raw_screencap_header_truncated")
    if length > MAX_PAYLOAD_BYTES:
        raise ValueError("raw_screencap_payload_limit")

    width, height, pixel_format = struct.unpack_from("<III", buffer)
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise ValueError(f"raw_screencap_dimensions:{width}x{height}")
    pixels = width * height
    if pixels > MAX_PIXELS:
        raise ValueError("raw_screencap_pixel_limit")
    descriptor = PIXEL_FORMATS.get(pixel_format)
    if descriptor is None:
        raise ValueError(f"raw_screencap_pixel_format:{pixel_format}")
    name, bytes_per_pixel = descriptor
    expected_pixels = pixels * bytes_per_pixel
    actual_header_bytes = length - expected_pixels
    if header_bytes is not None and actual_header_bytes != header_bytes:
        raise ValueError(f"raw_screencap_length:{length};expected={expected_pixels + header_bytes}")
    if actual_header_bytes not in (12, 16):
        raise ValueError(f"raw_screencap_length:{length};expected={expected_pixels + 12}_or_{expected_pixels + 16}")
    header_bytes = actual_header_bytes
    color_space = struct.unpack_from("<I", buffer, 12)[0] if header_bytes == 16 else None
    if color_space is not None and color_space not in COLOR_SPACES:
        raise ValueError(f"raw_screencap_color_space:{color_space}")

    image = np.empty((height, width, 3), dtype=np.uint8)
    if pixel_format == 4:
        packed = np.frombuffer(buffer, dtype="<u2", count=pixels, offset=header_bytes).reshape(height, width)
        # HAL RGB565 samples are UNORM. Round to the nearest 8-bit value,
        # keeping black/white endpoints and avoiding multiplication overflow.
        image[..., 0] = ((packed & 31) * 255 + 15) // 31
        image[..., 1] = (((packed >> 5) & 63) * 255 + 31) // 63
        image[..., 2] = ((packed >> 11) * 255 + 15) // 31
    else:
        channels = np.frombuffer(buffer, dtype=np.uint8, count=expected_pixels,
                                 offset=header_bytes).reshape(height, width, bytes_per_pixel)
        if pixel_format == 5:
            image[:] = channels[..., :3]
        else:
            image[..., 0] = channels[..., 2]
            image[..., 1] = channels[..., 1]
            image[..., 2] = channels[..., 0]

    metadata = {"width": width, "height": height, "pixel_format": pixel_format,
                "pixel_format_name": name, "header_bytes": header_bytes,
                "bytes_per_pixel": bytes_per_pixel, "payload_bytes": length,
                "color_space": color_space,
                "color_space_name": COLOR_SPACES.get(color_space),
                "color_conversion": "channel_order_to_BGR; source_colorspace_preserved"}
    return image, metadata
