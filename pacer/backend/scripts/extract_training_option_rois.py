"""Read-only training-option ROI extractor.

Defines/exports 5 training button regions (speed, stamina, power, guts, wisdom)
from a full training-screen screenshot.  Strictly read-only: no ADB, no game
interaction, no tap/click, no safety gate bypass, no writes to logs/.

Usage:
    python scripts/extract_training_option_rois.py --image path/to/screenshot.png
    python scripts/extract_training_option_rois.py --image shot.png --output-json rois.json
    python scripts/extract_training_option_rois.py --image shot.png --debug-dir debug/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "training_option_rois.v1"

# Minimum image dimensions — rejects already-cropped or tiny images.
MIN_INPUT_W = 1000
MIN_INPUT_H = 1600

# Reference resolution
REF_W, REF_H = 1440, 2560

# Training button centres in 1440x2560 reference coords (from config/coordinates.json)
_TRAINING_BUTTON_CENTERS: dict[str, tuple[int, int]] = {
    "speed":   (205, 2138),
    "stamina": (432, 2178),
    "power":   (668, 2178),
    "guts":    (912, 2178),
    "wisdom":  (1192, 2178),
}

# Half-size of the bounding box around each button centre (reference coords).
# Buttons are ~120 px diameter; use 90 px half-size for a generous crop.
_BUTTON_HALF_W = 90
_BUTTON_HALF_H = 90

# Display names
_DISPLAY_NAMES: dict[str, str] = {
    "speed": "Speed",
    "stamina": "Stamina",
    "power": "Power",
    "guts": "Guts",
    "wisdom": "Wisdom",
}

# Layout presets (only one for now)
LAYOUT_PRESETS: dict[str, str] = {
    "umamusume_1440x2560_v1": "Standard 1440x2560 vertical training screen",
}


# ---------- core ----------

def _scale_coord(x: int, y: int, img_w: int, img_h: int) -> tuple[float, float]:
    """Scale a reference-resolution coordinate to actual image size."""
    return x * img_w / REF_W, y * img_h / REF_H


def compute_training_option_rois(
    img_w: int,
    img_h: int,
    layout_preset: str = "umamusume_1440x2560_v1",
) -> dict:
    """Compute training-option ROI layout for the given image dimensions.

    Returns a dict matching the training_option_rois.v1 schema (without
    image/errors/warnings — caller fills those).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if layout_preset not in LAYOUT_PRESETS:
        errors.append(f"unknown_layout_preset: {layout_preset}")

    if img_w < MIN_INPUT_W or img_h < MIN_INPUT_H:
        errors.append(
            f"image_too_small: {img_w}x{img_h} < minimum {MIN_INPUT_W}x{MIN_INPUT_H}"
        )

    if errors:
        return {
            "schema_version": SCHEMA_VERSION,
            "image_size": [img_w, img_h],
            "training_options": [],
            "errors": errors,
            "warnings": warnings,
        }

    # Mark as approximate until validated against real screenshots
    warnings.append("layout_is_approximate")

    options: list[dict] = []
    for option_id, (cx_ref, cy_ref) in _TRAINING_BUTTON_CENTERS.items():
        # Scale centre
        cx, cy = _scale_coord(cx_ref, cy_ref, img_w, img_h)

        # Scale half-sizes
        hw = _BUTTON_HALF_W * img_w / REF_W
        hh = _BUTTON_HALF_H * img_h / REF_H

        x1 = max(0, int(round(cx - hw)))
        y1 = max(0, int(round(cy - hh)))
        x2 = min(img_w, int(round(cx + hw)))
        y2 = min(img_h, int(round(cy + hh)))

        options.append({
            "option_id": option_id,
            "display_name": _DISPLAY_NAMES[option_id],
            "bbox_xyxy": [x1, y1, x2, y2],
            "center_xy": [int(round(cx)), int(round(cy))],
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "image_size": [img_w, img_h],
        "training_options": options,
        "errors": errors,
        "warnings": warnings,
    }


def build_result(image_path: Path, layout_preset: str) -> dict:
    """Build the full output envelope (never raises)."""
    from PIL import Image  # noqa: F811

    result: dict = {
        "image": str(image_path),
        "schema_version": SCHEMA_VERSION,
        "image_size": [0, 0],
        "training_options": [],
        "errors": [],
        "warnings": [],
    }

    if not image_path.is_file():
        result["errors"].append(f"image_not_found: {image_path}")
        return result

    try:
        img = Image.open(image_path)
        w, h = img.size
        img.close()
    except Exception as exc:
        result["errors"].append(f"image_open_error: {exc}")
        return result

    result["image_size"] = [w, h]

    roi_result = compute_training_option_rois(w, h, layout_preset)
    result["schema_version"] = roi_result["schema_version"]
    result["training_options"] = roi_result["training_options"]
    result["errors"].extend(roi_result["errors"])
    result["warnings"].extend(roi_result["warnings"])

    return result


def draw_debug_image(image_path: Path, result: dict, debug_dir: Path) -> Path | None:
    """Draw ROI bboxes on a copy of the image and save to debug_dir.

    Returns the path to the saved debug image, or None on failure.
    """
    try:
        from PIL import Image, ImageDraw  # noqa: F811
    except ImportError:
        print("[debug] Pillow not available, skipping debug image.", file=sys.stderr)
        return None

    try:
        img = Image.open(image_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        colours = {
            "speed":   (0, 200, 0, 120),
            "stamina": (0, 100, 255, 120),
            "power":   (255, 165, 0, 120),
            "guts":    (255, 0, 0, 120),
            "wisdom":  (160, 32, 240, 120),
        }

        for opt in result.get("training_options", []):
            x1, y1, x2, y2 = opt["bbox_xyxy"]
            oid = opt["option_id"]
            fill = colours.get(oid, (200, 200, 200, 100))
            draw.rectangle([x1, y1, x2, y2], fill=fill, outline=fill[:3] + (255,), width=3)
            # Label
            draw.text((x1 + 4, y1 + 4), oid, fill=(255, 255, 255, 255))

        composite = Image.alpha_composite(img, overlay).convert("RGB")
        debug_dir.mkdir(parents=True, exist_ok=True)
        out_path = debug_dir / "training_option_rois_debug.png"
        composite.save(out_path)
        img.close()
        composite.close()
        return out_path

    except Exception as exc:
        print(f"[debug] Failed to create debug image: {exc}", file=sys.stderr)
        return None


# ---------- CLI ----------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract training-option ROI layout from a full screenshot (read-only)."
    )
    parser.add_argument("--image", required=True, help="Path to full training-screen screenshot.")
    parser.add_argument("--output-json", default=None, help="Write ROI layout to JSON file.")
    parser.add_argument("--debug-dir", default=None, help="Directory for debug images.")
    parser.add_argument(
        "--layout-preset",
        default="umamusume_1440x2560_v1",
        choices=list(LAYOUT_PRESETS.keys()),
        help="Layout preset (default: umamusume_1440x2560_v1).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    image_path = Path(args.image)

    result = build_result(image_path, args.layout_preset)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.debug_dir:
        if result["errors"]:
            print("[debug] Skipping debug image due to errors.", file=sys.stderr)
        else:
            dp = draw_debug_image(image_path, result, Path(args.debug_dir))
            if dp:
                print(f"[debug] Saved {dp}", file=sys.stderr)

    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
