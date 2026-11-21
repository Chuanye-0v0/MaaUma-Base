from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

from core.auto_trainer.career_start_contracts import (
    ACTION_ID,
    ALLOWED_SOURCE_SIZES,
    DESTINATION_SCHEMA,
    SOURCE_MAX_AGE_NS,
    SOURCE_SCHEMA,
    SOURCE_STATE,
    CareerButtonAnchor,
    CareerStartDestinationPacket,
    CareerStartSourcePacket,
)
from core.auto_trainer.career_start_safety import (
    CareerStartLocalizedSafetyAnalyzer,
    CareerStartSafetyBundle,
    CareerStartSafetyScope,
    NAV_TERM_ROIS,
)
from core.perception import Perception
from core.auto_trainer.global_home_layout import (
    home_layout_anchors, has_blocking_home_overlay, has_large_low_texture_panel,
)


HOME_REFERENCE_SHA256 = "ac141aa42afe78520a1dbe3034c28e4902925a5d2baf6a9e045b4d2e4f09741b"
HOME_REFERENCE_2_SHA256 = "638e2957dfdb1ca7c0d3791e9ada7d347e307d7bf137d5b07de4408dac85be68"
HOME_REFERENCE_3_SHA256 = "8e256bbf7f95a8fc89aa62e8766457049f3f2f3d0795226b22a5ba44948300a2"
HOME_REFERENCE_4_SHA256 = "7220cb15e74b87a9c0b461976dffd5c6c02623cc0d59992c1841378ab4805da0"
HOME_REFERENCE_5_SHA256 = "c15f51c56fdb8a4ab2a55854d7d31d969d3d7cb886e2a63bef032f91489fc3cc"
HOME_REFERENCE_6_SHA256 = "360d3dcb94fcb9b8175aa621ec8fd5787193691c80890a91af4386edf90c253e"
HOME_REFERENCE_7_SHA256 = "0e127ecc41d8a9c576c8636f9b01f45d8893d85c7e04d32fcc2d6626e925fbf3"
GRAND_MASTERS_REFERENCE_SHA256 = "1591f917001ac89591bf5cddbcb40e183b56f06f940283305d421e60b2c6955d"
_CURRENT_HOME_REFERENCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "career_start"
    / "global_home_reference_20260916.png"
)
_GRAND_MASTERS_REFERENCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "career_start"
    / "grand_masters_scenario_reference.png"
)
HOME_REQUIRED_NAVIGATION_SLOTS = tuple(NAV_TERM_ROIS)
DESTINATION_REQUIRED_NAVIGATION_SLOTS = (HOME_REQUIRED_NAVIGATION_SLOTS[-1],)


class CareerStartPerceptor:
    def __init__(
        self,
        *,
        safety_analyzer: CareerStartLocalizedSafetyAnalyzer | None = None,
    ) -> None:
        self.safety_analyzer = safety_analyzer or CareerStartLocalizedSafetyAnalyzer()

    def analyze_source(
        self,
        png_bytes: bytes,
        *,
        captured_monotonic_ns: int,
    ) -> tuple[CareerStartSourcePacket, CareerStartSafetyBundle]:
        source_sha256 = hashlib.sha256(png_bytes).hexdigest()
        image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            scope = CareerStartSafetyScope(False, False, False, 0, False)
            safety = self.safety_analyzer.analyze(
                png_bytes,
                source_size=(0, 0),
                scope=scope,
            )
            return (
                self._source_packet(
                    source_sha256=source_sha256,
                    captured_monotonic_ns=captured_monotonic_ns,
                    source_size=(0, 0),
                    scope=scope,
                    safety=safety,
                    button=None,
                    errors=("image_decode",),
                ),
                safety,
            )

        height, width = image.shape[:2]
        source_size = (int(width), int(height))
        if source_size not in ALLOWED_SOURCE_SIZES:
            scope = CareerStartSafetyScope(False, False, False, 0, False)
            safety = self.safety_analyzer.analyze(
                png_bytes,
                source_size=source_size,
                scope=scope,
            )
            return (
                self._source_packet(
                    source_sha256=source_sha256,
                    captured_monotonic_ns=captured_monotonic_ns,
                    source_size=source_size,
                    scope=scope,
                    safety=safety,
                    button=None,
                    errors=("source_image_size",),
                ),
                safety,
            )

        canonical = (
            image
            if source_size == (720, 1280)
            else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
        )
        perception = Perception().analyze_image(canonical)
        top_status_bar, global_home_nav, home_tab_selected = _home_layout_anchors(
            canonical
        )
        blocking_modal = bool(
            _has_blocking_modal(canonical, perception.get("buttons", ()))
            or not _home_content_unobscured(canonical)
        )
        candidates = _career_button_candidates(perception.get("buttons", ()))
        unobscured_navigation_slots = _navigation_slots_unobscured(
            canonical,
            context="home",
        )
        scope = CareerStartSafetyScope(
            top_status_bar=top_status_bar,
            global_home_nav=global_home_nav,
            home_tab_selected=home_tab_selected,
            career_button_count=len(candidates),
            blocking_modal=blocking_modal,
            trusted_navigation_slots=unobscured_navigation_slots,
        )
        safety = self.safety_analyzer.analyze(
            png_bytes,
            source_size=source_size,
            scope=scope,
        )
        errors: list[str] = []
        if perception.get("screen") != "game_home":
            errors.append("screen")
        if not top_status_bar:
            errors.append("top_status_bar")
        if not global_home_nav:
            errors.append("global_home_nav")
        if not home_tab_selected:
            errors.append("home_tab_selected")
        if len(candidates) != 1:
            errors.append("career_button_count")
        if blocking_modal:
            errors.append("blocking_modal")
        if unobscured_navigation_slots != HOME_REQUIRED_NAVIGATION_SLOTS:
            errors.append("navigation_slots_obscured")
        if safety.scoped_packet.text_status != "ok":
            errors.append("frame_text_safety_abstained")
        if safety.scoped_packet.dangerous:
            errors.append("dangerous_frame")

        button = (
            _button_anchor(candidates[0], scale=source_size[0] // 720)
            if len(candidates) == 1
            else None
        )
        return (
            self._source_packet(
                source_sha256=source_sha256,
                captured_monotonic_ns=captured_monotonic_ns,
                source_size=source_size,
                scope=scope,
                safety=safety,
                button=button,
                errors=tuple(errors),
            ),
            safety,
        )

    def analyze_destination(
        self,
        png_bytes: bytes,
    ) -> tuple[CareerStartDestinationPacket, CareerStartSafetyBundle]:
        source_sha256 = hashlib.sha256(png_bytes).hexdigest()
        image = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            safety = self.safety_analyzer.analyze(
                png_bytes,
                source_size=(0, 0),
                scope=CareerStartSafetyScope(False, False, False, 0, False),
            )
            return self._destination_packet(
                source_sha256=source_sha256,
                page_state="unexpected_destination",
                safety=safety,
                errors=("image_decode",),
            ), safety

        height, width = image.shape[:2]
        source_size = (int(width), int(height))
        if source_size not in ALLOWED_SOURCE_SIZES:
            safety = self.safety_analyzer.analyze(
                png_bytes,
                source_size=source_size,
                scope=CareerStartSafetyScope(False, False, False, 0, False),
            )
            return self._destination_packet(
                source_sha256=source_sha256,
                page_state="unexpected_destination",
                safety=safety,
                errors=("source_image_size",),
            ), safety

        canonical = (
            image
            if source_size == (720, 1280)
            else cv2.resize(image, (720, 1280), interpolation=cv2.INTER_AREA)
        )
        known_loading = _known_loading_signature(canonical)
        perception = Perception().analyze_image(canonical)
        raw_blocking_modal = _has_blocking_modal(
            canonical, perception.get("buttons", ())
        )
        if known_loading:
            raw_blocking_modal = False
        home_anchors = _home_layout_anchors(canonical)
        home_buttons = _career_button_candidates(perception.get("buttons", ()))
        home_navigation_slots = _navigation_slots_unobscured(
            canonical,
            context="home",
        )
        scenario_navigation_slots = _navigation_slots_unobscured(
            canonical,
            context="grand_masters_destination",
        )
        home_blocking_modal = bool(
            raw_blocking_modal or not _home_content_unobscured(canonical)
        )
        strict_home = bool(
            all(home_anchors)
            and len(home_buttons) == 1
            and not home_blocking_modal
            and home_navigation_slots == HOME_REQUIRED_NAVIGATION_SLOTS
        )

        reference = _load_frozen_image(
            _GRAND_MASTERS_REFERENCE_PATH,
            GRAND_MASTERS_REFERENCE_SHA256,
        )
        if reference is not None and reference.shape[:2] != (1280, 720):
            reference = cv2.resize(reference, (720, 1280), interpolation=cv2.INTER_AREA)
        anchors = (
            _scenario_anchors(canonical, reference)
            if reference is not None
            else {}
        )
        scenario_identity = bool(
            anchors
            and anchors["continue_button_count"] == 1
            and all(
                value
                for key, value in anchors.items()
                if key != "continue_button_count"
            )
        )
        scenario_select_loading = _scenario_select_loading_signature(canonical, anchors)
        blocking_modal = bool(
            raw_blocking_modal
            and not (
                scenario_select_loading
                or (
                    anchors
                    and anchors["scenario_title_region"]
                    and anchors["grand_masters_hero_signature"]
                    and anchors["continue_button_count"] == 1
                )
            )
        )
        safety_scope = (
            CareerStartSafetyScope(
                top_status_bar=home_anchors[0],
                global_home_nav=home_anchors[1],
                home_tab_selected=home_anchors[2],
                career_button_count=len(home_buttons),
                blocking_modal=raw_blocking_modal,
                trusted_navigation_slots=home_navigation_slots,
            )
            if strict_home
            else CareerStartSafetyScope(
                top_status_bar=False,
                global_home_nav=False,
                home_tab_selected=bool(anchors.get("bottom_home_nav", False)),
                career_button_count=0,
                blocking_modal=blocking_modal,
                trusted_navigation_context=(
                    "grand_masters_destination"
                    if scenario_identity
                    else "scenario_select_loading"
                    if scenario_select_loading
                    else "none"
                ),
                trusted_navigation_slots=(
                    tuple(
                        term
                        for term in scenario_navigation_slots
                        if term == "招募"
                    )
                    if scenario_identity or scenario_select_loading
                    else ()
                ),
            )
        )
        safety = self.safety_analyzer.analyze(
            png_bytes,
            source_size=source_size,
            scope=safety_scope,
        )
        if (
            (known_loading or scenario_select_loading)
            and not safety.scoped_packet.resource_terms
            and not safety.scoped_packet.errors
        ):
            return self._destination_packet(
                source_sha256=source_sha256,
                page_state="known_loading",
                safety=safety,
            ), safety
        if safety.scoped_packet.dangerous:
            return self._destination_packet(
                source_sha256=source_sha256,
                page_state="forbidden_destination",
                safety=safety,
                active_forbidden_page="resource_or_forbidden",
                blocking_modal=blocking_modal,
                errors=("dangerous_frame",),
            ), safety

        if strict_home:
            return self._destination_packet(
                source_sha256=source_sha256,
                page_state="global_home_still_visible",
                safety=safety,
                blocking_modal=False,
            ), safety

        if reference is None:
            return self._destination_packet(
                source_sha256=source_sha256,
                page_state="unexpected_destination",
                safety=safety,
                blocking_modal=raw_blocking_modal,
                errors=("scenario_reference",),
            ), safety
        destination_navigation_trusted = (
            safety.scope.trusted_navigation_context
            == "grand_masters_destination"
            and safety.scope.trusted_navigation_slots
            == DESTINATION_REQUIRED_NAVIGATION_SLOTS
        )
        ready = bool(
            scenario_identity
            and not blocking_modal
            and destination_navigation_trusted
        )
        destination_errors: list[str] = []
        if not scenario_identity:
            destination_errors.append("scenario_identity")
        if not destination_navigation_trusted:
            destination_errors.append("navigation_slots_obscured")
        return self._destination_packet(
            source_sha256=source_sha256,
            page_state=("scenario_select_ready" if ready else "unexpected_destination"),
            safety=safety,
            scenario_id="grand_masters" if ready else None,
            selected=ready,
            anchors=anchors,
            blocking_modal=blocking_modal,
            errors=(() if ready else tuple(destination_errors)),
        ), safety

    @staticmethod
    def _destination_packet(
        *,
        source_sha256: str,
        page_state: str,
        safety: CareerStartSafetyBundle,
        scenario_id: str | None = None,
        selected: bool = False,
        anchors: dict[str, bool] | None = None,
        active_forbidden_page: str | None = None,
        blocking_modal: bool = False,
        errors: Sequence[str] = (),
    ) -> CareerStartDestinationPacket:
        from core.auto_trainer.career_start_contracts import canonical_sha256

        return CareerStartDestinationPacket(
            schema_version=DESTINATION_SCHEMA,
            source_image_sha256=source_sha256,
            page_state=page_state,
            scenario_id=scenario_id,
            selected=selected,
            anchors=anchors or {},
            active_forbidden_page=active_forbidden_page,
            blocking_modal=blocking_modal,
            raw_safety_digest=canonical_sha256(safety.raw_packet.to_dict()),
            scoped_safety_digest=canonical_sha256(safety.scoped_packet.to_dict()),
            errors=tuple(errors),
        )

    @staticmethod
    def _source_packet(
        *,
        source_sha256: str,
        captured_monotonic_ns: int,
        source_size: tuple[int, int],
        scope: CareerStartSafetyScope,
        safety: CareerStartSafetyBundle,
        button: CareerButtonAnchor | None,
        errors: Sequence[str],
    ) -> CareerStartSourcePacket:
        ready = not errors
        return CareerStartSourcePacket(
            schema_version=SOURCE_SCHEMA,
            source_image_sha256=source_sha256,
            captured_monotonic_ns=captured_monotonic_ns,
            expires_monotonic_ns=captured_monotonic_ns + SOURCE_MAX_AGE_NS,
            source_image_size=source_size,
            canonical_size=(720, 1280),
            orientation="portrait",
            page_state=SOURCE_STATE if ready else "source_rejected",
            page_confidence=1.0 if ready else 0.0,
            top_status_bar=scope.top_status_bar,
            global_home_nav=scope.global_home_nav,
            home_tab_selected=scope.home_tab_selected,
            career_button_count=scope.career_button_count,
            career_button=button,
            active_forbidden_page=(
                "resource_or_forbidden"
                if safety.scoped_packet.dangerous
                else None
            ),
            blocking_modal=scope.blocking_modal,
            scoped_safety_digest=safety.derivation_digest,
            errors=tuple(errors),
        )


def _home_layout_anchors(image: np.ndarray) -> tuple[bool, bool, bool]:
    return home_layout_anchors(image)


def _career_button_candidates(buttons: Sequence[object]) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for value in buttons:
        if not isinstance(value, dict) or value.get("kind") != "primary_green":
            continue
        bbox = value.get("bbox")
        center = value.get("center")
        if not isinstance(bbox, (tuple, list)) or not isinstance(
            center, (tuple, list)
        ):
            continue
        x, y, width, height = (int(item) for item in bbox)
        cx, cy = (int(item) for item in center)
        if not (720 * 0.62 <= cx <= 720 * 0.98):
            continue
        if not (1280 * 0.78 <= cy <= 1280 * 0.92):
            continue
        if width < 720 * 0.18 or height < 1280 * 0.035:
            continue
        candidates.append(
            {
                "bbox": (x, y, width, height),
                "center": (cx, cy),
            }
        )
    return candidates


def _button_anchor(value: dict[str, object], *, scale: int) -> CareerButtonAnchor:
    bbox = tuple(int(item) for item in value["bbox"])
    center = tuple(int(item) for item in value["center"])
    return CareerButtonAnchor(
        action_id=ACTION_ID,
        canonical_bbox=bbox,
        canonical_center=center,
        device_bbox=tuple(item * scale for item in bbox),
        device_center=tuple(item * scale for item in center),
    )


def _has_blocking_modal(image: np.ndarray, buttons: Sequence[object]) -> bool:
    return has_blocking_home_overlay(image, buttons)


def _has_large_low_texture_panel(image: np.ndarray) -> bool:
    return has_large_low_texture_panel(image)


def _scenario_anchors(
    image: np.ndarray,
    reference: np.ndarray,
) -> dict[str, bool | int]:
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    top_left = hsv[int(height * 0.10) : int(height * 0.18), 0 : int(width * 0.35)]
    center_hero = hsv[
        int(height * 0.18) : int(height * 0.78),
        int(width * 0.05) : int(width * 0.95),
    ]
    continue_region = hsv[
        int(height * 0.80) : int(height * 0.90),
        int(width * 0.25) : int(width * 0.75),
    ]
    bottom_nav = hsv[int(height * 0.90) : height, :]
    top_blue = float(
        np.mean(
            (top_left[:, :, 0] >= 85)
            & (top_left[:, :, 0] <= 125)
            & (top_left[:, :, 1] > 60)
            & (top_left[:, :, 2] > 90)
        )
    )
    top_purple = float(
        np.mean(
            (top_left[:, :, 0] >= 120)
            & (top_left[:, :, 0] <= 155)
            & (top_left[:, :, 1] > 30)
            & (top_left[:, :, 2] > 70)
        )
    )
    hero_saturated = float(
        np.mean((center_hero[:, :, 1] > 70) & (center_hero[:, :, 2] > 90))
    )
    continue_green = float(
        np.mean(
            (continue_region[:, :, 0] >= 38)
            & (continue_region[:, :, 0] <= 92)
            & (continue_region[:, :, 1] > 70)
            & (continue_region[:, :, 2] > 80)
        )
    )
    nav_green = float(
        np.mean(
            (bottom_nav[:, :, 0] >= 38)
            & (bottom_nav[:, :, 0] <= 92)
            & (bottom_nav[:, :, 1] > 70)
            & (bottom_nav[:, :, 2] > 80)
        )
    )
    nav_blue = float(
        np.mean(
            (bottom_nav[:, :, 0] >= 85)
            & (bottom_nav[:, :, 0] <= 125)
            & (bottom_nav[:, :, 1] > 60)
            & (bottom_nav[:, :, 2] > 90)
        )
    )
    title_correlation = _roi_correlation(
        image,
        reference,
        (0.00, 0.08, 0.40, 0.20),
    )
    continue_correlation = _roi_correlation(
        image,
        reference,
        (0.18, 0.79, 0.82, 0.91),
    )
    carousel_correlation = _roi_correlation(
        image,
        reference,
        (0.30, 0.75, 0.70, 0.82),
    )
    portrait_status_bar = _portrait_status_bar(image)
    continue_button_count = _count_continue_buttons(image, reference)
    return {
        "portrait_status_bar": portrait_status_bar,
        "scenario_title_region": bool(
            top_blue > 0.18 and top_purple > 0.18 and title_correlation > 0.55
        ),
        "grand_masters_hero_signature": bool(
            hero_saturated > 0.42 and _orb_identity_matches(image, reference)
        ),
        "scenario_carousel": carousel_correlation > 0.75,
        "continue_button_count": continue_button_count,
        "continue_button_enabled": continue_green > 0.55,
        "bottom_home_nav": bool(nav_green > 0.25 and nav_blue > 0.04),
    }


def _count_continue_buttons(image: np.ndarray, reference: np.ndarray) -> int:
    """Count frozen-layout continue buttons without turning a match into an action."""
    template = reference[1038:1118, 218:502]
    search = image[960:1229, 36:684]
    if template.size == 0 or search.size == 0:
        return 0
    matches = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    candidate_mask = (matches >= 0.85).astype(np.uint8)
    component_count, _labels, _stats, _centroids = cv2.connectedComponentsWithStats(
        candidate_mask,
        8,
    )
    return max(0, int(component_count) - 1)


def _portrait_status_bar(image: np.ndarray) -> bool:
    height, width = image.shape[:2]
    if width >= height:
        return False
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    left = hsv[0 : int(height * 0.13), 0 : int(width * 0.20)]
    center = hsv[0 : int(height * 0.13), int(width * 0.20) : int(width * 0.82)]
    right = hsv[0 : int(height * 0.12), int(width * 0.80) : width]
    left_saturated = float(np.mean((left[:, :, 1] > 70) & (left[:, :, 2] > 90)))
    center_white = float(np.mean((center[:, :, 1] < 65) & (center[:, :, 2] > 175)))
    right_green = float(
        np.mean(
            (right[:, :, 0] >= 38)
            & (right[:, :, 0] <= 92)
            & (right[:, :, 1] > 70)
            & (right[:, :, 2] > 80)
        )
    )
    return bool(left_saturated > 0.16 and center_white > 0.20 and right_green > 0.10)


def _known_loading_signature(image: np.ndarray) -> bool:
    height, width = image.shape[:2]
    if (width, height) != (720, 1280):
        return False
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    center = gray[
        int(height * 0.55) : int(height * 0.78),
        int(width * 0.30) : int(width * 0.70),
    ]
    background_mask = np.ones(gray.shape, dtype=bool)
    background_mask[
        int(height * 0.50) : int(height * 0.82),
        int(width * 0.25) : int(width * 0.75),
    ] = False
    background = gray[background_mask]
    center_bright = float(np.mean(center > 100))
    overall_bright = float(np.mean(gray > 100))
    return bool(
        35.0 <= float(np.mean(background)) <= 50.0
        and float(np.std(background)) < 1.0
        and float(np.std(center)) > 10.0
        and 0.01 < center_bright < 0.05
        and 0.001 < overall_bright < 0.01
    )


def _scenario_select_loading_signature(
    image: np.ndarray,
    anchors: dict[str, bool | int],
) -> bool:
    """Recognize the read-only scenario-page skeleton while content is loading."""
    content = image[210:1000, 30:690]
    gray = cv2.cvtColor(content, cv2.COLOR_BGR2GRAY)
    edge_ratio = float(np.mean(cv2.Canny(gray, 80, 160) > 0))
    white_ratio = float(np.mean(gray > 220))
    return bool(
        anchors
        and anchors.get("portrait_status_bar") is True
        and anchors.get("scenario_title_region") is True
        and anchors.get("bottom_home_nav") is True
        and anchors.get("grand_masters_hero_signature") is False
        and anchors.get("scenario_carousel") is False
        and type(anchors.get("continue_button_count")) is int
        and anchors.get("continue_button_count") == 0
        and edge_ratio < 0.04
        and white_ratio < 0.05
    )


def _home_content_unobscured(image: np.ndarray) -> bool:
    """Require enough frozen static home structure while allowing character motion."""
    root = Path(__file__).resolve().parents[2]
    references = (
        _load_frozen_image(
            root
            / "runs"
            / "ug_parent_live"
            / "u1_glory_goddess_cup_20260715"
            / "strict_global_home_probe_5"
            / "advance_step_001.png",
            HOME_REFERENCE_SHA256,
        ),
        _load_frozen_image(
            root
            / "runs"
            / "ug_parent_live"
            / "safe_advance_from_home_action_table_095"
            / "advance_step_002.png",
            HOME_REFERENCE_2_SHA256,
        ),
        _load_frozen_image(
            root
            / "runs"
            / "ug_parent_live"
            / "u1_career_start_route_20260715"
            / "live_20260715_092611"
            / "captures"
            / "career_start_capture_000000.png",
            HOME_REFERENCE_3_SHA256,
        ),
        _load_frozen_image(
            root
            / "runs"
            / "ug_parent_live"
            / "u1_new_run_20260715"
            / "recover_disconnect_20260715_102744"
            / "fresh_after_print_failure.png",
            HOME_REFERENCE_4_SHA256,
        ),
        _load_frozen_image(
            root
            / "runs"
            / "ug_parent_live"
            / "u1_new_run_20260715"
            / "u1_global_home_after_source_invalid_probe_20260716"
            / "advance_step_001.png",
            HOME_REFERENCE_5_SHA256,
        ),
        _load_frozen_image(
            root
            / "runs"
            / "ug_parent_live"
            / "u1_deepseek_two_run_preflight_20260723"
            / "current_zero_click.png",
            HOME_REFERENCE_6_SHA256,
        ),
        _load_frozen_image(
            _CURRENT_HOME_REFERENCE_PATH,
            HOME_REFERENCE_7_SHA256,
        ),
    )
    side_rois = ((90, 350, 210, 650), (510, 350, 630, 650))
    for reference in references:
        if reference is None or reference.shape[:2] != (1280, 720):
            continue
        matching_pixels = 0
        total_pixels = 0
        for x0, y0, x1, y1 in side_rois:
            region = image[y0:y1, x0:x1]
            reference_region = reference[y0:y1, x0:x1]
            max_channel_delta = np.max(
                np.abs(
                    region.astype(np.int16)
                    - reference_region.astype(np.int16)
                ),
                axis=2,
            )
            matching_pixels += int(
                np.sum(max_channel_delta <= 5)
            )
            total_pixels += int(region.shape[0] * region.shape[1])
        if total_pixels and matching_pixels / total_pixels >= 0.80:
            return True
    return False


def _navigation_slots_unobscured(
    image: np.ndarray,
    *,
    context: str,
) -> tuple[str, ...]:
    slot_rois = {
        "商店": (190, 1060, 285, 1145),
        "招募": (600, 1185, 710, 1265),
    }
    root = Path(__file__).resolve().parents[2]
    reference_specs = (
        (
            (
                root
                / "runs"
                / "ug_parent_live"
                / "u1_glory_goddess_cup_20260715"
                / "strict_global_home_probe_5"
                / "advance_step_001.png",
                HOME_REFERENCE_SHA256,
            ),
            (
                root
                / "runs"
                / "ug_parent_live"
                / "safe_advance_from_home_action_table_095"
                / "advance_step_002.png",
                HOME_REFERENCE_2_SHA256,
            ),
            (
                root
                / "runs"
                / "ug_parent_live"
                / "u1_career_start_route_20260715"
                / "live_20260715_092611"
                / "captures"
                / "career_start_capture_000000.png",
                HOME_REFERENCE_3_SHA256,
            ),
            (
                root
                / "runs"
                / "ug_parent_live"
                / "u1_new_run_20260715"
                / "recover_disconnect_20260715_102744"
                / "fresh_after_print_failure.png",
                HOME_REFERENCE_4_SHA256,
            ),
            (
                _CURRENT_HOME_REFERENCE_PATH,
                HOME_REFERENCE_7_SHA256,
            ),
        )
        if context == "home"
        else (
            (
                root
                / "data"
                / "career_start"
                / "grand_masters_scenario_reference.png",
                GRAND_MASTERS_REFERENCE_SHA256,
            ),
        )
    )
    references = [
        _load_frozen_image(path, expected_sha256)
        for path, expected_sha256 in reference_specs
    ]
    references = [
        reference
        for reference in references
        if reference is not None and reference.shape[:2] == (1280, 720)
    ]
    if not references:
        return ()
    trusted: list[str] = []
    for term, (x0, y0, x1, y1) in slot_rois.items():
        if context == "grand_masters_destination" and term != "招募":
            continue
        region = image[y0:y1, x0:x1]
        region_gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        dark_ratio = float(np.mean(region_gray < 40))
        if dark_ratio <= 0.02 and any(
            float(
                np.mean(
                    np.all(region == reference[y0:y1, x0:x1], axis=2)
                )
            )
            >= 0.50
            for reference in references
        ):
            trusted.append(term)
    return tuple(trusted)


def _load_frozen_image(path: Path, expected_sha256: str) -> np.ndarray | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        return None
    return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)


def _roi_correlation(
    image: np.ndarray,
    reference: np.ndarray,
    roi: tuple[float, float, float, float],
) -> float:
    height, width = image.shape[:2]
    x0, y0, x1, y1 = roi
    left = cv2.cvtColor(
        image[int(height * y0) : int(height * y1), int(width * x0) : int(width * x1)],
        cv2.COLOR_BGR2GRAY,
    )
    right = cv2.cvtColor(
        reference[
            int(height * y0) : int(height * y1),
            int(width * x0) : int(width * x1),
        ],
        cv2.COLOR_BGR2GRAY,
    )
    if left.size == 0 or right.size == 0 or left.shape != right.shape:
        return 0.0
    result = cv2.matchTemplate(left, right, cv2.TM_CCOEFF_NORMED)
    value = float(result[0, 0])
    return value if np.isfinite(value) else 0.0


def _orb_identity_matches(image: np.ndarray, reference: np.ndarray) -> bool:
    height, width = image.shape[:2]
    y0, y1 = int(height * 0.18), int(height * 0.78)
    x0, x1 = int(width * 0.05), int(width * 0.95)
    current_roi = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    reference_roi = cv2.cvtColor(reference[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(nfeatures=1200)
    ref_keypoints, ref_descriptors = orb.detectAndCompute(reference_roi, None)
    cur_keypoints, cur_descriptors = orb.detectAndCompute(current_roi, None)
    if (
        ref_descriptors is None
        or cur_descriptors is None
        or not ref_keypoints
        or not cur_keypoints
    ):
        return False
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
        ref_descriptors,
        cur_descriptors,
        k=2,
    )
    good = [
        first
        for pair in matches
        if len(pair) == 2
        for first, second in [pair]
        if first.distance < 0.75 * second.distance
    ]
    if len(good) < 24:
        return False
    quadrants = set()
    mid_x = reference_roi.shape[1] / 2
    mid_y = reference_roi.shape[0] / 2
    for match in good:
        x, y = ref_keypoints[match.queryIdx].pt
        quadrants.add((int(x >= mid_x), int(y >= mid_y)))
    return len(quadrants) >= 3


__all__ = ["CareerStartPerceptor"]
