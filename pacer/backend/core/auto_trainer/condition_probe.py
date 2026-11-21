"""Fail-closed recognition of explicit trainee conditions from the details page."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import cv2
import numpy as np

from core.coordinates import CoordinateConfig, CoordinateScaler
from core.main_menu_state import HIGH_CONFIDENCE, analyze_main_menu_state


CANONICAL_SIZE = (720, 1280)
SUPPORTED_SIZES = {CANONICAL_SIZE, (1440, 2560)}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PAGE_MATCH_THRESHOLD = 0.985
CONDITION_MATCH_THRESHOLD = 0.985

# Some game builds render the "no conditions" ability-details page without the
# older "暂无内容" placeholder.  When the page is verified, no registered
# condition matches, and the second condition row plus the old placeholder ROI
# are both completely blank, conclude no_conditions instead of abstaining.
BLANK_EMPTY_STATE_THRESHOLD = 0.995
BLANK_CONDITIONS_SECOND_ROW_ROI = (32, 813, 686, 918)
_BLANK_REGION_STD_LIMIT = 2.0
_BLANK_REGION_EDGE_LIMIT = 0.001
RETURN_VERIFY_MAX_ATTEMPTS = 3
RETURN_VERIFY_RETRY_SECONDS = 0.8
TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "auto_trainer"
    / "condition_probe"
)
CONDITION_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "main_menu_state_templates"
    / "condition_details"
)
ABILITY_DETAILS_SOURCE_SHA256 = "e63f222a025c24e8e92b670dbdfe81311b92a8692d9037fed9326401e72b5bb8"
ABILITY_BUTTON_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2] / "assets" / "main_menu_state_templates"
)
ABILITY_BUTTON_BANK_MANIFEST = (
    ABILITY_BUTTON_TEMPLATE_ROOT / "condition_probe_ability_button_bank.json"
)
EMPTY_STATE_MANIFEST = (
    ABILITY_BUTTON_TEMPLATE_ROOT / "condition_probe_empty_state_manifest.json"
)
HINT_CARD_MANIFEST = (
    ABILITY_BUTTON_TEMPLATE_ROOT / "condition_probe_skill_hint_card_manifest.json"
)
CONDITION_PROBE_CONFIRM_TOKEN = "UG_PARENT_CONDITION_PROBE_SANDBOX"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ConditionTemplate:
    condition_id: str
    display_name: str
    effects: dict[str, bool]
    template_path: Path
    source_sha256: str
    roi: tuple[int, int, int, int]
    provenance: str


@dataclass(frozen=True)
class AbilityButtonTemplate:
    template_id: str
    template_path: Path
    template_sha256: str
    source_sha256: str
    provenance: str


@dataclass(frozen=True)
class EmptyStateTemplate:
    value: str
    template_path: Path
    template_sha256: str
    source_sha256: str
    roi: tuple[int, int, int, int]
    provenance: str


@dataclass(frozen=True)
class AbilityButtonBank:
    roi: tuple[int, int, int, int]
    threshold: float
    templates: tuple[AbilityButtonTemplate, ...]


CHUBBY_TENDENCY = ConditionTemplate(
    condition_id="chubby_tendency",
    display_name="长胖倾向",
    effects={"speed_training_gain_blocked": True},
    template_path=CONDITION_TEMPLATE_ROOT / "chubby_tendency.png",
    source_sha256=ABILITY_DETAILS_SOURCE_SHA256,
    roi=(32, 690, 686, 795),
    provenance=(
        "runs/ug_parent_live/u1_condition_probe_turn016_20260711/"
        "ability_details.png"
    ),
)
INSOMNIA_TENDENCY = ConditionTemplate(
    condition_id="insomnia_tendency",
    display_name="熬夜倾向",
    effects={"random_vitality_loss_after_training": True},
    template_path=CONDITION_TEMPLATE_ROOT / "insomnia_tendency.png",
    source_sha256="3505155de9e69c2378d8c1c2dc7a0407a584c2ae53a85be2ec7576910c50f3a1",
    roi=(32, 690, 686, 795),
    provenance=(
        "runs/gm_third_acceptance_20260916/supervisor_retry/"
        "gm_third_acceptance_20260916_turn_025/condition_probe/ability_details.png"
    ),
)
ANCESTRAL_GUIDANCE_PASSION_ZONE = ConditionTemplate(
    condition_id="ancestral_guidance_passion_zone",
    display_name="热情领域：引领众人的始祖",
    effects={
        "group_card_friendship_training_enabled": True,
        "prevents_insomnia_and_slacker": True,
    },
    template_path=CONDITION_TEMPLATE_ROOT / "ancestral_guidance_passion_zone.png",
    source_sha256="459d26662f2359b8af958fce5df9885684efd87cf42a3466ad87bf56fb4e0b9c",
    roi=(32, 690, 686, 795),
    provenance=(
        "runs/ug_parent_live/u1_classic_turn025_20260712_turn_025/"
        "condition_probe/ability_details.png"
    ),
)
ANCESTRAL_GUIDANCE_PASSION_ZONE_SECOND_ROW = ConditionTemplate(
    condition_id="ancestral_guidance_passion_zone",
    display_name="热情领域：引领众人的始祖",
    effects={
        "group_card_friendship_training_enabled": True,
        "prevents_insomnia_and_slacker": True,
    },
    template_path=(
        CONDITION_TEMPLATE_ROOT / "ancestral_guidance_passion_zone_row2.png"
    ),
    source_sha256="1de0194e0dd6727f65ea4ba3fcd88927c5b189f2d164489971d8c4f11082501b",
    roi=(32, 813, 686, 918),
    provenance=(
        "runs/mission_20260808/supervised/pilot_batch56/"
        "mission_pilot_batch56_turn_061/condition_probe/ability_details.png"
    ),
)
CHARM_CIRCLE = ConditionTemplate(
    condition_id="charm_circle",
    display_name="惹人喜爱○",
    effects={"support_bond_gain_increased": True},
    template_path=CONDITION_TEMPLATE_ROOT / "charm_circle.png",
    source_sha256="4d9fd2ca16dc01f068903f95fffc39312bb4b517cc5d076b94dffb6767545df9",
    roi=(32, 690, 686, 795),
    provenance=(
        "runs/ug_parent_live/u1_script_only_truth_run_20260727/supervised_retry7/"
        "u1_script_only_truth_run_20260727_turn_011/race_condition_gate/"
        "condition_probe/ability_details.png"
    ),
)
MIGRAINE_AND_CHARM_SOURCE = (
    CONDITION_TEMPLATE_ROOT / "migraine_and_charm_circle.png"
)
MIGRAINE_AND_CHARM_SOURCE_SHA256 = (
    "dd51912a6d957203d2aa7b319aefa88c8db32717d835f25f083d29d7d149af7e"
)
MIGRAINE = ConditionTemplate(
    condition_id="migraine",
    display_name="偏头痛",
    effects={"mood_gain_blocked": True},
    template_path=MIGRAINE_AND_CHARM_SOURCE,
    source_sha256=MIGRAINE_AND_CHARM_SOURCE_SHA256,
    roi=(32, 690, 686, 795),
    provenance=(
        "runs/ug_parent_live/u1_script_only_truth_run_20260727/"
        "supervised_batch53_55_retry/u1_script_only_truth_run_20260727_turn_053/"
        "condition_probe/ability_details.png"
    ),
)
CHARM_CIRCLE_SECOND_ROW = ConditionTemplate(
    condition_id="charm_circle",
    display_name="惹人喜爱○",
    effects={"support_bond_gain_increased": True},
    template_path=MIGRAINE_AND_CHARM_SOURCE,
    source_sha256=MIGRAINE_AND_CHARM_SOURCE_SHA256,
    roi=(32, 813, 686, 918),
    provenance=(
        "runs/ug_parent_live/u1_script_only_truth_run_20260727/"
        "supervised_batch53_55_retry/u1_script_only_truth_run_20260727_turn_053/"
        "condition_probe/ability_details.png"
    ),
)
DEFAULT_CONDITION_REGISTRY: tuple[ConditionTemplate, ...] = (
    CHUBBY_TENDENCY,
    INSOMNIA_TENDENCY,
    ANCESTRAL_GUIDANCE_PASSION_ZONE,
    ANCESTRAL_GUIDANCE_PASSION_ZONE_SECOND_ROW,
    CHARM_CIRCLE,
    MIGRAINE,
    CHARM_CIRCLE_SECOND_ROW,
)
def _load_json_manifest(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _manifest_sha256(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _manifest_roi(value: Any) -> tuple[int, int, int, int] | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        return None
    x0, y0, x1, y1 = value
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _manifest_threshold(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    threshold = float(value)
    return threshold if math.isfinite(threshold) and 0.0 < threshold <= 1.0 else None


def _manifest_asset_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return ABILITY_BUTTON_TEMPLATE_ROOT / path


def _load_ability_button_bank() -> AbilityButtonBank | None:
    manifest = _load_json_manifest(ABILITY_BUTTON_BANK_MANIFEST)
    if manifest is None or manifest.get("schema_version") != "condition_probe.ability_button_bank.v1":
        return None
    roi = _manifest_roi(manifest.get("roi"))
    threshold = _manifest_threshold(manifest.get("threshold"))
    entries = manifest.get("templates")
    if roi is None or threshold is None or not isinstance(entries, list) or not entries:
        return None
    definitions: list[AbilityButtonTemplate] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        template_id = entry.get("template_id")
        path = _manifest_asset_path(entry.get("path"))
        asset_sha256 = _manifest_sha256(entry.get("asset_sha256"))
        source_sha256 = _manifest_sha256(entry.get("source_sha256"))
        provenance = entry.get("source_path")
        if (
            not isinstance(template_id, str)
            or not template_id
            or template_id in seen_ids
            or path is None
            or asset_sha256 is None
            or source_sha256 is None
            or not isinstance(provenance, str)
            or not provenance
        ):
            return None
        seen_ids.add(template_id)
        definitions.append(
            AbilityButtonTemplate(template_id, path, asset_sha256, source_sha256, provenance)
        )
    return AbilityButtonBank(roi, threshold, tuple(definitions))


def _load_empty_state_template() -> tuple[EmptyStateTemplate, float] | None:
    manifest = _load_json_manifest(EMPTY_STATE_MANIFEST)
    if manifest is None or manifest.get("schema_version") != "condition_probe.empty_state.v1":
        return None
    value = manifest.get("value")
    roi = _manifest_roi(manifest.get("roi"))
    threshold = _manifest_threshold(manifest.get("threshold"))
    template = manifest.get("template")
    if not isinstance(value, str) or not value or roi is None or threshold is None or not isinstance(template, dict):
        return None
    path = _manifest_asset_path(template.get("path"))
    asset_sha256 = _manifest_sha256(template.get("asset_sha256"))
    source_sha256 = _manifest_sha256(template.get("source_sha256"))
    provenance = template.get("source_path")
    if (
        path is None
        or asset_sha256 is None
        or source_sha256 is None
        or not isinstance(provenance, str)
        or not provenance
    ):
        return None
    return EmptyStateTemplate(value, path, asset_sha256, source_sha256, roi, provenance), threshold


# Compatibility exports are derived from the manifests; recognition reloads them per call.
_INITIAL_ABILITY_BUTTON_BANK = _load_ability_button_bank()
_INITIAL_EMPTY_STATE = _load_empty_state_template()
ABILITY_BUTTON_TEMPLATE_BANK = (
    _INITIAL_ABILITY_BUTTON_BANK.templates if _INITIAL_ABILITY_BUTTON_BANK is not None else ()
)
ABILITY_BUTTON_ROI = (
    _INITIAL_ABILITY_BUTTON_BANK.roi if _INITIAL_ABILITY_BUTTON_BANK is not None else (0, 0, 0, 0)
)
ABILITY_BUTTON_MATCH_THRESHOLD = (
    _INITIAL_ABILITY_BUTTON_BANK.threshold if _INITIAL_ABILITY_BUTTON_BANK is not None else 1.0
)
ABILITY_BUTTON_INK_MATCH_THRESHOLD = 0.99
EMPTY_STATE_NO_CONDITIONS = (
    _INITIAL_EMPTY_STATE[0]
    if _INITIAL_EMPTY_STATE is not None
    else EmptyStateTemplate("", Path(), "", "", (0, 0, 0, 0), "")
)
EMPTY_STATE_MATCH_THRESHOLD = _INITIAL_EMPTY_STATE[1] if _INITIAL_EMPTY_STATE is not None else 1.0


def analyze_condition_details(
    png_bytes: bytes,
    *,
    registry: Sequence[ConditionTemplate] | None = None,
) -> dict[str, Any]:
    """Recognize only registered conditions on a verified details/status page."""
    source_sha256 = hashlib.sha256(png_bytes).hexdigest()
    result = _new_result(source_sha256)
    image = _decode_png(png_bytes)
    if image is None:
        result["errors"].append("decode_failed")
        return result

    height, width = image.shape[:2]
    result["source"]["size"] = [width, height]
    if (width, height) not in SUPPORTED_SIZES:
        result["errors"].append("unsupported_layout")
        return result

    canonical = _to_canonical(image, width, height)
    result["normalization"] = {
        "canonical_size": list(CANONICAL_SIZE),
        "source_size": [width, height],
        "mode": "native" if (width, height) == CANONICAL_SIZE else "controlled_downscale",
    }
    template = _load_verified_template(
        CHUBBY_TENDENCY.template_path, CHUBBY_TENDENCY.source_sha256
    )
    if template is None:
        result["errors"].append("template_provenance_invalid")
        return result

    page = _analyze_details_page(canonical, template)
    result["page"] = page
    if page["abstained"]:
        result["warnings"].append("ability_details_page_unverified")
        return result

    conditions = _match_registered_conditions(
        canonical,
        template,
        registry if registry is not None else DEFAULT_CONDITION_REGISTRY,
        source_sha256,
    )
    result["conditions"] = conditions
    if conditions:
        result["abstained"] = False
        return result

    empty_state = _match_empty_state(canonical)
    result["empty_state"] = empty_state
    if not empty_state["abstained"]:
        result["abstained"] = False
        return result

    result["warnings"].append("unknown_or_uncovered_condition")
    return result


def analyze_condition_details_file(
    image_path: str | Path,
    *,
    registry: Sequence[ConditionTemplate] | None = None,
) -> dict[str, Any]:
    """Load an offline PNG and return the fail-closed condition report."""
    path = Path(image_path)
    try:
        return analyze_condition_details(path.read_bytes(), registry=registry)
    except OSError:
        result = _new_result(None)
        result["errors"].append("image_read_failed")
        return result


def analyze_ability_details_button_anchor(png_bytes: bytes) -> dict[str, Any]:
    """Verify the explicit ability-details button on an otherwise verified main menu."""
    source_sha256 = hashlib.sha256(png_bytes).hexdigest()
    image = _decode_png(png_bytes)
    if image is None:
        return _abstained_field({"reason": "decode_failed", "source_sha256": source_sha256})
    height, width = image.shape[:2]
    if (width, height) not in SUPPORTED_SIZES:
        return _abstained_field(
            {"reason": "unsupported_layout", "source_sha256": source_sha256, "size": [width, height]}
        )
    canonical = _to_canonical(image, width, height)
    bank = _load_ability_button_bank()
    if bank is None:
        return _abstained_field(
            {"reason": "template_manifest_invalid", "source_sha256": source_sha256}
        )
    candidates: list[tuple[AbilityButtonTemplate, np.ndarray, float]] = []
    for definition in bank.templates:
        template = _load_verified_ability_button_template(definition, bank.roi)
        if template is None:
            return _abstained_field(
                {
                    "reason": "template_provenance_invalid",
                    "source_sha256": source_sha256,
                    "template_id": definition.template_id,
                }
            )
        candidates.append(
            (
                definition,
                template,
                _roi_template_score(canonical, template, bank.roi),
            )
        )
    matched_template, _matched_image, score = max(
        candidates, key=lambda candidate: candidate[2]
    )
    structure = _ability_button_structure_evidence(
        canonical,
        bank.roi,
        [(definition, template) for definition, template, _score in candidates],
    )
    evidence = {
        "recognizer": "verified_ability_button_template_bank",
        "template_id": matched_template.template_id,
        "template_asset_sha256": matched_template.template_sha256,
        "template_source_sha256": matched_template.source_sha256,
        "template_provenance": matched_template.provenance,
        "template_candidates": [
            {
                "template_id": definition.template_id,
                "template_asset_sha256": definition.template_sha256,
                "template_source_sha256": definition.source_sha256,
                "template_provenance": definition.provenance,
                "score": round(candidate_score, 4),
            }
            for definition, _template, candidate_score in candidates
        ],
        "roi": list(bank.roi),
        "template_score": round(score, 4),
        "threshold": bank.threshold,
        "structure": structure,
    }
    if score < bank.threshold:
        consensus_template_count = sum(
            1
            for _definition, _template, candidate_score in candidates
            if candidate_score >= 0.97
        )
        if score >= 0.98 and consensus_template_count >= 4:
            evidence["acceptance"] = "template_bank_consensus"
            evidence["consensus_template_count"] = consensus_template_count
        elif structure["accepted"]:
            evidence["acceptance"] = "masked_button_structure"
            evidence["template_id"] = structure["template_id"]
            return _field("ability_details", structure["ink_score"], evidence)
        else:
            return _abstained_field(evidence)
    return _field("ability_details", score, evidence)


def analyze_race_day_ability_details_button_anchor(
    png_bytes: bytes,
    *,
    require_primary_action: bool = True,
) -> dict[str, Any]:
    """Verify the shifted ability-details control on a mandatory race-day frame."""
    source_sha256 = hashlib.sha256(png_bytes).hexdigest()
    image = _decode_png(png_bytes)
    if image is None:
        return _abstained_field({"reason": "decode_failed", "source_sha256": source_sha256})
    height, width = image.shape[:2]
    if (width, height) not in SUPPORTED_SIZES:
        return _abstained_field(
            {"reason": "unsupported_layout", "source_sha256": source_sha256, "size": [width, height]}
        )
    canonical = _to_canonical(image, width, height)

    # Reuse the race router's structural evidence, but independently verify the
    # shifted white/brown button before authorising its dedicated coordinate.
    from core.auto_trainer.safe_advance import SafeAdvancePlanner

    race_day_layout = SafeAdvancePlanner._looks_like_race_day_main_menu(canonical)
    primary_action = SafeAdvancePlanner._looks_like_race_day_primary_action(canonical)
    if not race_day_layout or (require_primary_action and not primary_action):
        return _abstained_field(
            {
                "reason": "race_day_layout_unverified",
                "source_sha256": source_sha256,
                "race_day_layout": race_day_layout,
                "primary_action": primary_action,
                "primary_action_required": require_primary_action,
            }
        )

    roi = (610, 800, 690, 875)
    x0, y0, x1, y1 = roi
    crop = cv2.cvtColor(canonical[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    if crop.size == 0:
        return _abstained_field({"reason": "button_roi_empty", "source_sha256": source_sha256})
    hue = crop[:, :, 0]
    saturation = crop[:, :, 1]
    value = crop[:, :, 2]
    white = float(np.mean((saturation < 70) & (value > 170)))
    brown = float(
        np.mean(
            (hue >= 5)
            & (hue <= 30)
            & (saturation > 50)
            & (value > 60)
        )
    )
    dark = float(np.mean(value < 130))
    saturated = float(np.mean(saturation > 70))
    evidence = {
        "recognizer": "race_day_layout_and_button_geometry",
        "source_sha256": source_sha256,
        "roi": list(roi),
        "point": [645, 835],
        "ratios": {
            "white": round(white, 4),
            "brown": round(brown, 4),
            "dark": round(dark, 4),
            "saturated": round(saturated, 4),
        },
        "thresholds": {
            "white_min": 0.75,
            "brown_min": 0.12,
            "dark_min": 0.08,
            "saturated_max": 0.20,
        },
    }
    if not (white >= 0.75 and brown >= 0.12 and dark >= 0.08 and saturated <= 0.20):
        return _abstained_field(evidence)
    confidence = min(1.0, white, brown / 0.12, dark / 0.08, 0.20 / max(saturated, 1e-6))
    return _field("ability_details", confidence, evidence)


def _new_result(source_sha256: str | None) -> dict[str, Any]:
    return {
        "schema_version": "condition_probe.v1",
        "source": {"sha256": source_sha256, "size": None},
        "normalization": None,
        "page": _abstained_field(),
        "empty_state": _abstained_field(),
        "conditions": [],
        "abstained": True,
        "warnings": [],
        "errors": [],
    }


def _abstained_field(evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"value": None, "confidence": 0.0, "abstained": True, "evidence": evidence or {}}


def _field(value: str, confidence: float, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": round(float(confidence), 4),
        "abstained": False,
        "evidence": evidence,
    }


def _decode_png(png_bytes: bytes) -> np.ndarray | None:
    if not png_bytes.startswith(PNG_SIGNATURE):
        return None
    return cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)


def _to_canonical(image: np.ndarray, width: int, height: int) -> np.ndarray:
    if (width, height) == CANONICAL_SIZE:
        return image
    return cv2.resize(image, CANONICAL_SIZE, interpolation=cv2.INTER_AREA)


def _load_verified_template(path: Path, source_sha256: str) -> np.ndarray | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != source_sha256:
        return None
    image = _decode_png(data)
    if image is None:
        return None
    height, width = image.shape[:2]
    if (width, height) not in SUPPORTED_SIZES:
        return None
    return _to_canonical(image, width, height)


def _load_verified_ability_button_template(
    definition: AbilityButtonTemplate,
    roi: tuple[int, int, int, int],
) -> np.ndarray | None:
    return _load_verified_roi_template(
        definition.template_path,
        definition.template_sha256,
        roi,
    )


def _load_verified_roi_template(
    path: Path,
    template_sha256: str,
    roi: tuple[int, int, int, int],
) -> np.ndarray | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != template_sha256:
        return None
    image = _decode_png(data)
    expected_width = roi[2] - roi[0]
    expected_height = roi[3] - roi[1]
    if image is None or image.shape[:2] != (expected_height, expected_width):
        return None
    return image


def _analyze_details_page(image: np.ndarray, template: np.ndarray) -> dict[str, Any]:
    anchors = {
        "title": (220, 45, 500, 91),
        "status_tab": (17, 617, 356, 660),
        "close_button": (215, 1142, 505, 1224),
    }
    scores = {name: _roi_score(image, template, roi) for name, roi in anchors.items()}
    confidence = min(scores.values()) if scores else 0.0
    evidence = {
        "template_source_sha256": ABILITY_DETAILS_SOURCE_SHA256,
        "anchors": [
            {"name": name, "roi": list(roi), "score": round(scores[name], 4)}
            for name, roi in anchors.items()
        ],
        "threshold": PAGE_MATCH_THRESHOLD,
    }
    if confidence < PAGE_MATCH_THRESHOLD:
        return _abstained_field(evidence)
    return _field("ability_details", confidence, evidence)


def _match_empty_state(image: np.ndarray) -> dict[str, Any]:
    loaded = _load_empty_state_template()
    if loaded is None:
        return _abstained_field({"reason": "template_manifest_invalid"})
    definition, threshold = loaded
    template = _load_verified_roi_template(
        definition.template_path,
        definition.template_sha256,
        definition.roi,
    )
    if template is None:
        return _abstained_field(
            {
                "reason": "template_provenance_invalid",
                "template_source_sha256": definition.source_sha256,
                "template_provenance": definition.provenance,
            }
        )
    score = _roi_template_score(image, template, definition.roi)
    evidence = {
        "recognizer": "verified_empty_state_roi_template",
        "template_asset_sha256": definition.template_sha256,
        "template_source_sha256": definition.source_sha256,
        "template_provenance": definition.provenance,
        "roi": list(definition.roi),
        "template_score": round(score, 4),
        "threshold": threshold,
    }
    if score < threshold:
        hint = _load_hint_card_template()
        if (
            hint is not None
            and _blank_region_verified(image, BLANK_CONDITIONS_SECOND_ROW_ROI)
            and _blank_region_verified(image, definition.roi)
        ):
            hint_template, hint_roi, hint_threshold = hint
            hint_score = _roi_template_score(image, hint_template, hint_roi)
            if hint_score >= hint_threshold:
                blank_evidence = dict(evidence)
                blank_evidence["blank_fallback"] = True
                blank_evidence["blank_threshold"] = BLANK_EMPTY_STATE_THRESHOLD
                blank_evidence["hint_card_roi"] = list(hint_roi)
                blank_evidence["hint_card_score"] = round(hint_score, 4)
                blank_evidence["template_score"] = 1.0
                blank_evidence["threshold"] = BLANK_EMPTY_STATE_THRESHOLD
                return _field(definition.value, 1.0, blank_evidence)
        return _abstained_field(evidence)
    return _field(definition.value, score, evidence)


def _load_hint_card_template() -> tuple[np.ndarray, tuple[int, int, int, int], float] | None:
    """Load the verified skill-hint card template used by the blank fallback."""

    manifest = _load_json_manifest(HINT_CARD_MANIFEST)
    if manifest is None or manifest.get("schema_version") != "condition_probe.hint_card.v1":
        return None
    roi = _manifest_roi(manifest.get("roi"))
    threshold = _manifest_threshold(manifest.get("threshold"))
    template = manifest.get("template")
    if roi is None or threshold is None or not isinstance(template, dict):
        return None
    path = _manifest_asset_path(template.get("path"))
    asset_sha256 = _manifest_sha256(template.get("asset_sha256"))
    source_sha256 = _manifest_sha256(template.get("source_sha256"))
    provenance = template.get("source_path")
    if (
        path is None
        or asset_sha256 is None
        or source_sha256 is None
        or not isinstance(provenance, str)
        or not provenance
    ):
        return None
    loaded = _load_verified_roi_template(path, asset_sha256, roi)
    if loaded is None:
        return None
    return loaded, roi, threshold


def _blank_region_verified(image: np.ndarray, roi: tuple[int, int, int, int]) -> bool:
    """Return whether an ROI is uniformly near-white with no visible edges."""

    x0, y0, x1, y1 = roi
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return False
    patch = image[y0:y1, x0:x1]
    if patch.size == 0:
        return False
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) >= _BLANK_REGION_STD_LIMIT:
        return False
    edge_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    edge_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    edge = np.hypot(edge_x, edge_y)
    if float((edge > 30).mean()) >= _BLANK_REGION_EDGE_LIMIT:
        return False
    return True


def _match_registered_conditions(
    image: np.ndarray,
    template: np.ndarray,
    registry: Sequence[ConditionTemplate],
    source_sha256: str,
) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for definition in registry:
        condition_template = _load_verified_template(
            definition.template_path, definition.source_sha256
        )
        if condition_template is None:
            continue
        score = _roi_score(image, condition_template, definition.roi)
        if score < CONDITION_MATCH_THRESHOLD:
            continue
        conditions.append(
            {
                "condition_id": definition.condition_id,
                "display_name": definition.display_name,
                "effects": dict(definition.effects),
                "confidence": round(score, 4),
                "evidence": {
                    "recognizer": "frozen_same_source_roi_template",
                    "template_source_sha256": definition.source_sha256,
                    "template_provenance": definition.provenance,
                    "roi": list(definition.roi),
                    "template_score": round(score, 4),
                    "threshold": CONDITION_MATCH_THRESHOLD,
                },
                "source_sha256": source_sha256,
            }
        )
    return conditions


def _roi_score(image: np.ndarray, template: np.ndarray, roi: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = roi
    return _roi_template_score(image, template[y0:y1, x0:x1], roi)


def _roi_template_score(
    image: np.ndarray,
    reference: np.ndarray,
    roi: tuple[int, int, int, int],
) -> float:
    x0, y0, x1, y1 = roi
    candidate = image[y0:y1, x0:x1]
    if candidate.size == 0 or candidate.shape != reference.shape:
        return 0.0
    return float(cv2.matchTemplate(candidate, reference, cv2.TM_CCOEFF_NORMED)[0, 0])


def _ability_button_ink_mask(crop: np.ndarray) -> np.ndarray:
    """Keep the stable brown label/outline ink, excluding character/background colours."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (3, 55, 0), (25, 255, 210)) > 0


def _binary_dice(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        return 0.0
    left_count = int(np.count_nonzero(left))
    right_count = int(np.count_nonzero(right))
    if left_count == 0 or right_count == 0:
        return 0.0
    overlap = int(np.count_nonzero(left & right))
    return (2.0 * overlap) / float(left_count + right_count)


def _ability_button_circle(crop: np.ndarray) -> dict[str, Any]:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=12,
        param1=90,
        param2=18,
        minRadius=27,
        maxRadius=38,
    )
    if circles is None:
        return {"circle_verified": False, "circle": None}
    height, width = crop.shape[:2]
    accepted: list[tuple[float, float, float]] = []
    for center_x, center_y, radius in circles[0]:
        if (
            0.39 * width <= center_x <= 0.55 * width
            and 0.35 * height <= center_y <= 0.58 * height
            and 0.39 * height <= radius <= 0.56 * height
        ):
            accepted.append((float(center_x), float(center_y), float(radius)))
    if not accepted:
        return {"circle_verified": False, "circle": None}
    center_x, center_y, radius = min(
        accepted,
        key=lambda item: abs(item[0] - 35.0) + abs(item[1] - 32.0),
    )
    return {
        "circle_verified": True,
        "circle": [
            round(center_x, 1),
            round(center_y, 1),
            round(radius, 1),
        ],
    }


def _ability_button_structure_evidence(
    image: np.ndarray,
    roi: tuple[int, int, int, int],
    references: Sequence[tuple[AbilityButtonTemplate, np.ndarray]],
) -> dict[str, Any]:
    x0, y0, x1, y1 = roi
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return {
            "accepted": False,
            "template_id": None,
            "ink_score": 0.0,
            "ink_threshold": ABILITY_BUTTON_INK_MATCH_THRESHOLD,
            "ink_pixels": 0,
            "circle_verified": False,
            "circle": None,
        }
    ink = _ability_button_ink_mask(crop)
    scored = [
        (definition, _binary_dice(ink, _ability_button_ink_mask(reference)))
        for definition, reference in references
    ]
    if not scored:
        return {
            "accepted": False,
            "template_id": None,
            "ink_score": 0.0,
            "ink_threshold": ABILITY_BUTTON_INK_MATCH_THRESHOLD,
            "ink_pixels": int(np.count_nonzero(ink)),
            "circle_verified": False,
            "circle": None,
        }
    definition, ink_score = max(scored, key=lambda item: item[1])
    circle = _ability_button_circle(crop)
    accepted = bool(
        ink_score >= ABILITY_BUTTON_INK_MATCH_THRESHOLD
        and circle["circle_verified"]
    )
    return {
        "accepted": accepted,
        "template_id": definition.template_id,
        "ink_score": round(ink_score, 4),
        "ink_threshold": ABILITY_BUTTON_INK_MATCH_THRESHOLD,
        "ink_pixels": int(np.count_nonzero(ink)),
        **circle,
    }


class ConditionProbeExecutor:
    """Perform the two-tap details probe only through explicit fail-closed gates."""

    def __init__(
        self,
        *,
        adb_factory: Callable[[], Any] | None = None,
        main_menu_analyzer: Callable[[bytes], dict[str, Any]] = analyze_main_menu_state,
        condition_analyzer: Callable[[str | Path], dict[str, Any]] = analyze_condition_details_file,
    ) -> None:
        self.adb_factory = adb_factory or self._default_adb_factory
        self.main_menu_analyzer = main_menu_analyzer
        self.condition_analyzer = condition_analyzer
        self.coordinates = CoordinateConfig()

    def run_once(
        self,
        *,
        execute: bool,
        confirm_token: str | None,
        screenshot_dir: str | Path | None = None,
        settle_seconds: float = 0.3,
    ) -> dict[str, Any]:
        if not execute:
            return self._stop("execute_required")
        if confirm_token != CONDITION_PROBE_CONFIRM_TOKEN:
            return self._stop("confirm_token_required")
        if settle_seconds < 0:
            return self._stop("invalid_settle_seconds")

        try:
            self.coordinates.require_verified("main_menu", "ability_details")
            self.coordinates.require_verified("race_day_main_menu", "ability_details")
            self.coordinates.require_verified("ability_details", "close")
        except ValueError as exc:
            return self._stop(str(exc))

        adb = self.adb_factory()
        if not adb.connect():
            return self._stop("adb_connect_failed")
        screen_size = tuple(adb.get_screen_size())
        if screen_size not in SUPPORTED_SIZES:
            return self._stop("unsupported_screen_size", screen_size=list(screen_size))

        destination = Path(screenshot_dir) if screenshot_dir is not None else (
            Path(tempfile.gettempdir()) / "uma_condition_probe"
        )
        before_path = destination / "before_details.png"
        details_path = destination / "ability_details.png"
        after_path = destination / "after_close.png"
        capture = self._capture(adb, before_path)
        if not capture["captured"]:
            return self._stop("before_screenshot_failed", capture=capture)

        image_bytes = self._read_capture(before_path)
        if image_bytes is None:
            return self._stop("before_screenshot_read_failed")
        main_menu = self.main_menu_analyzer(image_bytes)
        race_day_button_anchor = analyze_race_day_ability_details_button_anchor(
            image_bytes,
            # The large race button has an independent sparkle/character
            # animation.  The stable race-day layout plus the shifted,
            # geometry-verified ability button are the click authority here.
            require_primary_action=False,
        )
        if not race_day_button_anchor["abstained"]:
            source_section = "race_day_main_menu"
            button_anchor = race_day_button_anchor
        elif self._is_high_confidence_main_menu(main_menu):
            source_section = "main_menu"
            button_anchor = analyze_ability_details_button_anchor(image_bytes)
        else:
            return self._stop(
                "main_menu_unverified",
                main_menu=main_menu,
                race_day_button_anchor=race_day_button_anchor,
            )
        if button_anchor["abstained"]:
            return self._stop("ability_details_button_unverified", button_anchor=button_anchor)

        open_tap = self._scaled_tap(source_section, "ability_details", screen_size)
        adb.tap(open_tap["x"], open_tap["y"], scaled=False)
        time.sleep(settle_seconds)
        capture = self._capture(adb, details_path)
        if not capture["captured"]:
            return self._stop(
                "ability_details_screenshot_failed",
                taps=[open_tap],
                capture=capture,
                recovery=self._recover_close(adb, screen_size, settle_seconds),
            )

        details_bytes = self._read_capture(details_path)
        if details_bytes is None:
            return self._stop_after_open(
                "condition_report_invalid",
                adb=adb,
                screen_size=screen_size,
                settle_seconds=settle_seconds,
                taps=[open_tap],
                condition_report_error={"stage": "capture", "error": "details_read_failed"},
            )
        details_sha256 = hashlib.sha256(details_bytes).hexdigest()

        analyzer_completed = False
        try:
            condition_report = self.condition_analyzer(details_path)
            analyzer_completed = True
            page, confidence = self._validate_condition_report(condition_report, details_sha256)
        except Exception as exc:
            return self._stop_after_open(
                "condition_report_invalid",
                adb=adb,
                screen_size=screen_size,
                settle_seconds=settle_seconds,
                taps=[open_tap],
                condition_report_error={
                    "stage": "validation" if analyzer_completed else "analyzer",
                    "error": type(exc).__name__,
                },
            )

        if (
            page["abstained"]
            or page["value"] != "ability_details"
            or confidence < PAGE_MATCH_THRESHOLD
        ):
            return self._stop_after_open(
                "ability_details_unverified",
                adb=adb,
                screen_size=screen_size,
                settle_seconds=settle_seconds,
                taps=[open_tap],
                condition_report=condition_report,
            )
        if condition_report.get("abstained") is not False:
            return self._stop_after_open(
                "condition_content_unverified",
                adb=adb,
                screen_size=screen_size,
                settle_seconds=settle_seconds,
                taps=[open_tap],
                condition_report=condition_report,
            )

        close_tap = self._scaled_tap("ability_details", "close", screen_size)
        adb.tap(close_tap["x"], close_tap["y"], scaled=False)
        returned_menu = None
        returned_source = None
        returned_verified = False
        return_attempts = 0
        for attempt in range(1, RETURN_VERIFY_MAX_ATTEMPTS + 1):
            time.sleep(settle_seconds if attempt == 1 else RETURN_VERIFY_RETRY_SECONDS)
            capture = self._capture(adb, after_path)
            if not capture["captured"]:
                continue
            after_bytes = self._read_capture(after_path)
            if after_bytes is None:
                continue
            returned_menu = self.main_menu_analyzer(after_bytes)
            if source_section == "race_day_main_menu":
                returned_source = analyze_race_day_ability_details_button_anchor(
                    after_bytes,
                    require_primary_action=False,
                )
                returned_verified = not returned_source["abstained"]
            else:
                returned_source = returned_menu
                returned_verified = self._is_high_confidence_main_menu(returned_menu)
            return_attempts = attempt
            if returned_verified:
                break
        if not returned_verified:
            return self._stop(
                f"{source_section}_return_unverified",
                taps=[open_tap, close_tap],
                condition_report=condition_report,
                returned_menu=returned_menu,
                returned_source=returned_source,
                return_attempts=return_attempts,
            )
        return {
            "performed": True,
            "reason": "completed",
            "conditions": condition_report.get("conditions", []),
            "condition_report": condition_report,
            "taps": [open_tap, close_tap],
        }

    @staticmethod
    def _capture(adb: Any, path: Path) -> dict[str, Any]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"captured": False, "stage": "mkdir", "error": type(exc).__name__}
        try:
            adb.screenshot(str(path))
        except OSError as exc:
            return {"captured": False, "stage": "screenshot", "error": type(exc).__name__}
        try:
            size = path.stat().st_size
        except OSError as exc:
            return {"captured": False, "stage": "stat", "error": type(exc).__name__}
        if size <= 0:
            return {"captured": False, "stage": "stat", "error": "empty_file"}
        return {"captured": True, "size": size}

    @staticmethod
    def _read_capture(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _recover_close(
        self, adb: Any, screen_size: tuple[int, int], settle_seconds: float
    ) -> dict[str, Any]:
        close_tap = self._scaled_tap("ability_details", "close", screen_size)
        try:
            adb.tap(close_tap["x"], close_tap["y"], scaled=False)
        except Exception as exc:
            return {
                "status": "close_tap_failed",
                "tap": close_tap,
                "error": type(exc).__name__,
            }
        time.sleep(settle_seconds)
        return {"status": "close_tap_attempted", "tap": close_tap}

    def _stop_after_open(
        self,
        reason: str,
        *,
        adb: Any,
        screen_size: tuple[int, int],
        settle_seconds: float,
        **extra: Any,
    ) -> dict[str, Any]:
        return self._stop(
            reason,
            **extra,
            recovery=self._recover_close(adb, screen_size, settle_seconds),
        )

    @staticmethod
    def _validate_condition_report(
        report: Any, details_sha256: str
    ) -> tuple[dict[str, Any], float]:
        if not isinstance(report, dict):
            raise TypeError("condition report must be a dictionary")
        if report.get("schema_version") != "condition_probe.v1":
            raise ValueError("condition report schema version is invalid")
        if not isinstance(report.get("abstained"), bool):
            raise TypeError("condition report abstained must be a boolean")
        source = report.get("source")
        if not isinstance(source, dict):
            raise TypeError("condition report source must be a dictionary")
        source_sha256 = source.get("sha256")
        if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
            raise ValueError("condition report source sha256 is invalid")
        if source_sha256 != details_sha256:
            raise ValueError("condition report source sha256 does not match details capture")

        page = report.get("page")
        if not isinstance(page, dict):
            raise TypeError("condition report page must be a dictionary")
        if page.get("value") is not None and not isinstance(page.get("value"), str):
            raise TypeError("condition report page value must be a string or null")
        if not isinstance(page.get("abstained"), bool):
            raise TypeError("condition report page abstained must be a boolean")
        confidence = ConditionProbeExecutor._finite_number(page.get("confidence"), "page confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("condition report page confidence is out of range")
        if page["abstained"] is False:
            ConditionProbeExecutor._validate_page_evidence(page.get("evidence"))

        conditions = report.get("conditions")
        if not isinstance(conditions, list):
            raise TypeError("condition report conditions must be a list")
        if report["abstained"] is False:
            if conditions:
                ConditionProbeExecutor._validate_nonempty_conditions_empty_state(
                    report.get("empty_state")
                )
                for condition in conditions:
                    ConditionProbeExecutor._validate_condition_entry(condition, source_sha256)
            else:
                ConditionProbeExecutor._validate_empty_state(report.get("empty_state"), confidence)
        else:
            if conditions:
                raise ValueError("abstained condition report must not contain conditions")
            ConditionProbeExecutor._validate_abstained_empty_state(report.get("empty_state"))
        return page, confidence

    @staticmethod
    def _validate_page_evidence(evidence: Any) -> None:
        if not isinstance(evidence, dict):
            raise TypeError("condition report page evidence must be a dictionary")
        if evidence.get("template_source_sha256") != ABILITY_DETAILS_SOURCE_SHA256:
            raise ValueError("condition report page template source is invalid")
        threshold = ConditionProbeExecutor._finite_number(evidence.get("threshold"), "page threshold")
        if threshold != PAGE_MATCH_THRESHOLD:
            raise ValueError("condition report page threshold is invalid")
        anchors = evidence.get("anchors")
        expected = {
            "title": (220, 45, 500, 91),
            "status_tab": (17, 617, 356, 660),
            "close_button": (215, 1142, 505, 1224),
        }
        if not isinstance(anchors, list) or len(anchors) != len(expected):
            raise ValueError("condition report page anchors are invalid")
        for anchor in anchors:
            if not isinstance(anchor, dict) or anchor.get("name") not in expected:
                raise ValueError("condition report page anchor is invalid")
            if anchor.get("roi") != list(expected[anchor["name"]]):
                raise ValueError("condition report page anchor roi is invalid")
            score = ConditionProbeExecutor._finite_number(anchor.get("score"), "page anchor score")
            if not 0.0 <= score <= 1.0:
                raise ValueError("condition report page anchor score is out of range")

    @staticmethod
    def _validate_condition_entry(condition: Any, source_sha256: str) -> None:
        if not isinstance(condition, dict):
            raise TypeError("condition report condition must be a dictionary")
        condition_id = condition.get("condition_id")
        definitions = [
            item
            for item in DEFAULT_CONDITION_REGISTRY
            if item.condition_id == condition_id
        ]
        if not definitions:
            raise ValueError("condition report condition id is invalid")
        if not any(
            condition.get("display_name") == definition.display_name
            for definition in definitions
        ):
            raise ValueError("condition report condition display name is invalid")
        if not any(
            condition.get("effects") == definition.effects
            for definition in definitions
        ):
            raise ValueError("condition report condition effects are invalid")
        if condition.get("source_sha256") != source_sha256:
            raise ValueError("condition report condition source sha256 is invalid")
        confidence = ConditionProbeExecutor._finite_number(
            condition.get("confidence"), "condition confidence"
        )
        if not CONDITION_MATCH_THRESHOLD <= confidence <= 1.0:
            raise ValueError("condition report condition confidence is out of range")
        evidence = condition.get("evidence")
        if not isinstance(evidence, dict):
            raise TypeError("condition report condition evidence must be a dictionary")
        if evidence.get("recognizer") != "frozen_same_source_roi_template":
            raise ValueError("condition report condition recognizer is invalid")
        definitions = [
            definition
            for definition in definitions
            if evidence.get("template_source_sha256") == definition.source_sha256
        ]
        if not definitions:
            raise ValueError("condition report condition template source is invalid")
        definitions = [
            definition
            for definition in definitions
            if evidence.get("template_provenance") == definition.provenance
        ]
        if not definitions:
            raise ValueError("condition report condition provenance is invalid")
        if not any(
            evidence.get("roi") == list(definition.roi)
            for definition in definitions
        ):
            raise ValueError("condition report condition roi is invalid")
        score = ConditionProbeExecutor._finite_number(evidence.get("template_score"), "condition score")
        threshold = ConditionProbeExecutor._finite_number(
            evidence.get("threshold"), "condition threshold"
        )
        if not CONDITION_MATCH_THRESHOLD <= score <= 1.0 or threshold != CONDITION_MATCH_THRESHOLD:
            raise ValueError("condition report condition evidence is invalid")

    @staticmethod
    def _validate_empty_state(empty_state: Any, page_confidence: float) -> None:
        if not isinstance(empty_state, dict):
            raise TypeError("condition report empty state must be a dictionary")
        if empty_state.get("value") != "no_conditions" or empty_state.get("abstained") is not False:
            raise ValueError("condition report empty state is invalid")
        confidence = ConditionProbeExecutor._finite_number(
            empty_state.get("confidence"), "empty state confidence"
        )
        if not PAGE_MATCH_THRESHOLD <= page_confidence <= 1.0 or not 0.0 <= confidence <= 1.0:
            raise ValueError("condition report empty state confidence is invalid")
        evidence = empty_state.get("evidence")
        if not isinstance(evidence, dict):
            raise TypeError("condition report empty state evidence must be a dictionary")
        if evidence.get("recognizer") != "verified_empty_state_roi_template":
            raise ValueError("condition report empty state recognizer is invalid")
        for key in ("template_asset_sha256", "template_source_sha256"):
            value = evidence.get(key)
            if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                raise ValueError("condition report empty state template hash is invalid")
        if not isinstance(evidence.get("template_provenance"), str) or not evidence["template_provenance"]:
            raise ValueError("condition report empty state provenance is invalid")
        roi = evidence.get("roi")
        if (
            not isinstance(roi, list)
            or len(roi) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in roi)
            or roi[0] >= roi[2]
            or roi[1] >= roi[3]
        ):
            raise ValueError("condition report empty state roi is invalid")
        score = ConditionProbeExecutor._finite_number(evidence.get("template_score"), "empty state score")
        threshold = ConditionProbeExecutor._finite_number(
            evidence.get("threshold"), "empty state threshold"
        )
        if not 0.0 <= score <= 1.0 or not PAGE_MATCH_THRESHOLD <= threshold <= 1.0:
            raise ValueError("condition report empty state evidence is invalid")

    @staticmethod
    def _validate_nonempty_conditions_empty_state(empty_state: Any) -> None:
        if (
            not isinstance(empty_state, dict)
            or empty_state.get("value") is not None
            or empty_state.get("abstained") is not True
            or not isinstance(empty_state.get("evidence"), dict)
        ):
            raise ValueError("condition report nonempty conditions empty state is invalid")
        confidence = ConditionProbeExecutor._finite_number(
            empty_state.get("confidence"), "empty state confidence"
        )
        if confidence != 0.0:
            raise ValueError("condition report nonempty conditions empty state confidence is invalid")

    @staticmethod
    def _validate_abstained_empty_state(empty_state: Any) -> None:
        ConditionProbeExecutor._validate_nonempty_conditions_empty_state(empty_state)

    @staticmethod
    def _finite_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"condition report {name} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"condition report {name} must be finite")
        return number

    @staticmethod
    def _is_high_confidence_main_menu(observation: dict[str, Any]) -> bool:
        screen = observation.get("fields", {}).get("screen", {})
        return (
            isinstance(screen, dict)
            and not screen.get("abstained", True)
            and screen.get("value") == "main_menu"
            and float(screen.get("confidence", 0.0) or 0.0) >= HIGH_CONFIDENCE
        )

    def _scaled_tap(
        self, section: str, name: str, screen_size: tuple[int, int]
    ) -> dict[str, Any]:
        reference_x, reference_y = self.coordinates.verified_point(section, name)
        x, y = CoordinateScaler(
            self.coordinates.reference_size,
            screen_size,
        ).scale_point(reference_x, reference_y)
        return {
            "coordinate_name": f"{section}.{name}",
            "x": x,
            "y": y,
            "scaled": False,
        }

    @staticmethod
    def _stop(reason: str, **extra: Any) -> dict[str, Any]:
        return {"performed": False, "reason": reason, **extra}

    @staticmethod
    def _default_adb_factory() -> Any:
        from core.adb import ADB

        return ADB()
