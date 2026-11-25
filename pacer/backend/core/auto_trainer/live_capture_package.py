from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from core.auto_trainer.state import TRAINING_TYPES
from core.auto_trainer.pacer_storage import _io
from core.frame_io import read_image_compat


LIVE_CAPTURE_SCHEMA_VERSION = "ug_parent_live_capture_manifest.v1"


def build_live_capture_package(
    *,
    output_root: str | Path,
    actual_group_id: str,
    run_session_id: str,
    turn_index: int,
    source_state_key: str,
    operator_id: str,
    source_images: dict[str, str | Path],
    state_context: dict[str, Any],
    source_resolution: tuple[int, int],
    normalized_resolution: tuple[int, int] = (1440, 2560),
    prior_group: dict[str, Any] | None = None,
    refreshed_tabs: set[str] | None = None,
) -> dict[str, Any]:
    output_root = Path(output_root)
    manifest_path = output_root / "execution_manifest.json"
    group_dir = output_root / "r" / actual_group_id
    generation_dir = group_dir / ("g" + uuid.uuid4().hex[:5])
    refreshing = _io(manifest_path).is_file()
    native_resolution = tuple(source_resolution) == tuple(normalized_resolution)
    normalized_dir = (generation_dir if native_resolution else generation_dir / "normalized") if refreshing else group_dir
    # Native capture is already at the parser's resolution. The manifest and
    # legacy raw_720_dir report field can reference the same canonical pages.
    raw_720_dir = normalized_dir if native_resolution else generation_dir / "raw_720" if refreshing else output_root / "raw_720"
    _io(generation_dir).mkdir(parents=True, exist_ok=False)
    _io(raw_720_dir).mkdir(parents=True, exist_ok=True)
    _io(normalized_dir).mkdir(parents=True, exist_ok=True)

    missing = [tab for tab in TRAINING_TYPES if tab not in source_images]
    if missing:
        raise ValueError(f"source_images_missing_tabs:{','.join(missing)}")

    now = _now_iso()
    raw_files: list[dict[str, Any]] = []
    montage_images = []
    source_images_by_tab: dict[str, dict[str, str]] = {}
    refreshed = set(refreshed_tabs or [])
    prior_rows = {row["source_screen"].removesuffix("_tab"): row
                  for row in (prior_group or {}).get("raw_files", [])}
    scenario_state = state_context.get("scenario_state")
    preserve_larc_pixels = (
        isinstance(scenario_state, dict) and scenario_state.get("scenario") == "larc"
        and tuple(source_resolution) == (720, 1280)
        and tuple(normalized_resolution) == (1440, 2560)
    )
    for sequence, tab in enumerate(TRAINING_TYPES, start=1):
        source_path = Path(source_images[tab])
        image = read_image_compat(source_path)
        if image is None:
            raise ValueError(f"source_image_unreadable:{tab}:{source_path}")
        width, height = image.shape[1], image.shape[0]
        if (width, height) != tuple(source_resolution):
            raise ValueError(
                f"source_image_size_mismatch:{tab}:{width}x{height}:"
                f"expected={source_resolution[0]}x{source_resolution[1]}"
            )

        raw_720_path = raw_720_dir / f"{tab}.png"
        if native_resolution:
            if os.path.normcase(os.path.abspath(source_path)) != os.path.normcase(os.path.abspath(raw_720_path)):
                shutil.copyfile(_io(source_path), _io(raw_720_path))
        else:
            _write_png(raw_720_path, image)

        normalized_path = normalized_dir / f"{tab}.png"
        if not native_resolution:
            normalized = cv2.resize(
                image,
                tuple(normalized_resolution),
                # The L'Arc template reader reduces this package back to 720p.
                # Integer nearest-neighbour enlargement preserves its source pixels.
                interpolation=cv2.INTER_NEAREST if preserve_larc_pixels else cv2.INTER_CUBIC,
            )
            _write_png(normalized_path, normalized)

        small = cv2.resize(image, (360, 640), interpolation=cv2.INTER_AREA)
        cv2.putText(
            small,
            tab,
            (10, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 0, 255),
            2,
        )
        montage_images.append(small)

        relative_path = normalized_path.relative_to(output_root).as_posix()
        source_sha = _sha256_file(source_path)
        normalized_sha = source_sha if native_resolution else _sha256_file(normalized_path)
        source_images_by_tab[tab] = {
            "path": str(source_path),
            "sha256": source_sha,
        }
        raw_files.append(
            {
                "relative_path": relative_path,
                "sha256": normalized_sha,
                "size_bytes": _io(normalized_path).stat().st_size,
                "media_type": "image/png",
                "original_extension": ".png",
                "sequence": sequence,
                "source_screen": f"{tab}_tab",
                "captured_at": prior_rows.get(tab, {}).get("captured_at", now) if prior_group is not None and tab not in refreshed else now,
                "source_resolution": list(source_resolution),
                "normalized_resolution": list(normalized_resolution),
                "source_image_path": str(source_path),
                "source_image_sha256": source_sha,
            }
        )

    montage_path = generation_dir / "five_tabs_montage.jpg"
    _write_png(montage_path, cv2.hconcat(montage_images))

    context_path = generation_dir / "state_context.json"
    _write_json(context_path, dict(state_context))
    manifest = {
        "schema_version": LIVE_CAPTURE_SCHEMA_VERSION,
        "groups": [
            {
                "actual_group_id": actual_group_id,
                "run_session_id": run_session_id,
                "turn_index": turn_index,
                "source_state_key": source_state_key,
                "operator_id": operator_id,
                "group_review_status": "manual_pending",
                "raw_files": raw_files,
                "provenance": {
                    "source": "ug_parent_live_capture_package",
                    "source_resolution": list(source_resolution),
                    "normalized_resolution": list(normalized_resolution),
                    "canonical_commit_order": list(TRAINING_TYPES),
                    "source_images_by_tab": source_images_by_tab,
                    "capture_artifacts": {
                        "state_context": context_path.relative_to(output_root).as_posix(),
                        "montage": montage_path.relative_to(output_root).as_posix(),
                    },
                    **({"refreshed_tabs": sorted(refreshed),
                        "retained_same_turn_tabs": [tab for tab in TRAINING_TYPES if tab not in refreshed]}
                       if prior_group is not None else {}),
                },
            }
        ],
    }
    # This is the only publication point. Earlier failures leave the old group,
    # its montage and context untouched and usable by the formal reader.
    _write_json(manifest_path, manifest)

    result = {
        "is_valid": True,
        "output_root": str(output_root),
        "manifest_path": str(manifest_path),
        "state_context_path": str(context_path),
        "montage_path": str(montage_path),
        "raw_720_dir": str(raw_720_dir),
        "normalized_dir": str(normalized_dir),
        "actual_group_id": actual_group_id,
    }
    if prior_group is not None:
        result["candidate_quality_refresh"] = {
            "refreshed_tabs": sorted(refreshed), "turn_consumed": False,
            "retained_same_turn_tabs": [tab for tab in TRAINING_TYPES if tab not in refreshed],
            "run_session_id": run_session_id, "turn_index": turn_index,
        }
    return result


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        _io(temporary).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _io(temporary).replace(_io(path))
    finally:
        if _io(temporary).exists():
            _io(temporary).unlink()


def _write_png(path: Path, image: Any) -> None:
    _io(path.parent).mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix, image)
    if not ok:
        raise ValueError(f"write_image_failed:{path}")
    _io(path).write_bytes(encoded.tobytes())


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_io(path).read_bytes()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()
