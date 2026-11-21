"""Bounded live capture for the five Uma Musume training tabs.

This module is intentionally narrow: it verifies the current training menu,
normalizes to the speed tab, then switches once through the remaining
whitelisted tabs and registers the five PNGs through the formal training-gain
execution manifest validator.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import cv2

from core.coordinates import CoordinateConfig


CONFIRM_TOKEN = "CAPTURE_FIVE_TRAINING_TABS"
EXPECTED_SIZE = (1440, 2560)


class CaptureSafetyError(RuntimeError):
    """Raised when a bounded capture safety gate refuses to continue."""


class TrainingTab(str, Enum):
    SPEED = "speed"
    STAMINA = "stamina"
    POWER = "power"
    GUTS = "guts"
    WISDOM = "wisdom"


TRAINING_TAB_ORDER = (
    TrainingTab.SPEED,
    TrainingTab.STAMINA,
    TrainingTab.POWER,
    TrainingTab.GUTS,
    TrainingTab.WISDOM,
)
MAXIMUM_TAPS = 5
POST_TAP_MAX_ATTEMPTS = 5
POST_TAP_REQUIRED_CONSECUTIVE_MATCHES = 2
POST_TAP_INTERVAL_SECONDS = 0.25
POST_TAP_MAX_INITIAL_SETTLE_SECONDS = 1.0
GAIN_DISPLAY_STABILITY_ROI = (180, 1170, 1260, 1390)
GAIN_DISPLAY_MAX_MEAN_ABS_DIFF = 8.0
GAIN_DISPLAY_MAX_FOREGROUND_XOR_RATIO = 0.015


def _normalize_gain_display_frame(image):
    height, width = image.shape[:2]
    if (width, height) == EXPECTED_SIZE:
        return image
    if (width, height) == (720, 1280):
        return cv2.resize(image, EXPECTED_SIZE, interpolation=cv2.INTER_LINEAR)
    raise CaptureSafetyError(
        f"unexpected gain display frame size: {width}x{height}"
    )


def _gain_display_foreground_mask(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    red_or_gold = (hue < 35) | (hue > 165)
    foreground = ((saturation > 100) & (value > 220) & red_or_gold).astype("uint8")
    # The gain bubbles have looping sparkles around their borders.  Eroding one
    # pixel preserves the digit cores while removing threshold-edge flicker.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.erode(foreground, kernel, iterations=1).astype(bool)


def gain_display_foreground_xor_ratio(
    earlier_path: str | Path,
    later_path: str | Path,
    *,
    roi: tuple[int, int, int, int] = GAIN_DISPLAY_STABILITY_ROI,
) -> float:
    earlier = cv2.imread(str(earlier_path))
    later = cv2.imread(str(later_path))
    if earlier is None or later is None:
        raise CaptureSafetyError("cannot read post-tap PNG for gain stability")
    earlier = _normalize_gain_display_frame(earlier)
    later = _normalize_gain_display_frame(later)
    x1, y1, x2, y2 = roi
    earlier_roi = earlier[y1:y2, x1:x2]
    later_roi = later[y1:y2, x1:x2]
    if earlier_roi.shape != later_roi.shape or earlier_roi.size == 0:
        raise CaptureSafetyError("invalid gain display stability ROI")
    earlier_mask = _gain_display_foreground_mask(earlier_roi)
    later_mask = _gain_display_foreground_mask(later_roi)
    return float((earlier_mask != later_mask).mean())


def gain_display_mean_abs_difference(
    earlier_path: str | Path,
    later_path: str | Path,
) -> float:
    earlier = cv2.imread(str(earlier_path))
    later = cv2.imread(str(later_path))
    if earlier is None or later is None:
        raise CaptureSafetyError("cannot read post-tap PNG for gain stability")
    earlier = _normalize_gain_display_frame(earlier)
    later = _normalize_gain_display_frame(later)
    x1, y1, x2, y2 = GAIN_DISPLAY_STABILITY_ROI
    earlier_roi = earlier[y1:y2, x1:x2]
    later_roi = later[y1:y2, x1:x2]
    if earlier_roi.shape != later_roi.shape or earlier_roi.size == 0:
        raise CaptureSafetyError("invalid gain display stability ROI")
    return float(cv2.absdiff(earlier_roi, later_roi).mean())


@dataclass(frozen=True)
class ExecutionGateResult:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class BoundedCaptureConfig:
    execution_root: Path
    collection_plan: Path
    manifest_path: Path
    run_session_id: str
    actual_group_id: str
    turn_index: int
    source_state_key: str
    operator_id: str
    expected_manifest_sha: str | None = None
    settle_seconds: float = 1.0
    post_tap_interval_seconds: float = POST_TAP_INTERVAL_SECONDS
    post_tap_max_attempts: int = POST_TAP_MAX_ATTEMPTS
    post_tap_required_consecutive_matches: int = POST_TAP_REQUIRED_CONSECUTIVE_MATCHES
    coordinate_config: Path | str = "config/coordinates.json"


@dataclass(frozen=True)
class VerifiedTrainingState:
    selected_tab: TrainingTab
    image_path: Path
    verification: Any


@dataclass(frozen=True)
class PostTapSettleResult:
    expected_tab: TrainingTab
    final_selected_tab: TrainingTab
    attempts: int
    consecutive_matches: int
    verified_frame_path: Path
    evidence: dict[str, Any]


class DefaultVerifier:
    """Local page verifier used by the live CLI.

    Tests inject deterministic fake verifiers. The live verifier delegates all
    structural and selected-tab evidence to TrainingMenuPrechecker.
    """

    def precheck(self, image_path: Path | None = None) -> Any:
        if image_path is None:
            raise CaptureSafetyError("precheck requires a screenshot")
        from core.training_menu_precheck import TrainingMenuPrechecker

        result = TrainingMenuPrechecker().check(image_path)
        if not result.accepted:
            raise CaptureSafetyError(f"precheck rejected: {result.reason}")
        return result

    def inspect_after_capture(self, tab: TrainingTab, image_path: Path) -> Any:
        from core.training_menu_precheck import TrainingMenuPrechecker

        result = TrainingMenuPrechecker().check(image_path, expected_tab=tab.value)
        return result

    def verify_after_capture(self, tab: TrainingTab, image_path: Path) -> Any:
        result = self.inspect_after_capture(tab, image_path)
        if not result.accepted:
            raise CaptureSafetyError(f"post-capture rejected: {result.reason}")
        return result


def validate_execution_gate(
    execute: bool,
    confirm_token: str | None,
    *,
    live: bool = False,
) -> ExecutionGateResult:
    """Validate the explicit live execution gate."""
    if not live:
        return ExecutionGateResult(False, "--live is required for real tap execution")
    if not execute:
        return ExecutionGateResult(False, "--execute is required for real tap execution")
    if confirm_token != CONFIRM_TOKEN:
        return ExecutionGateResult(False, "confirm token mismatch")
    return ExecutionGateResult(True, "allowed")


def build_dry_run_plan(config: BoundedCaptureConfig) -> dict[str, Any]:
    coords = _training_tab_points(config)
    return {
        "dry_run": True,
        "will_tap": False,
        "live_capture_count": 5,
        "live_expected_taps": "4_if_speed_initially_selected_else_5",
        "live_maximum_taps": MAXIMUM_TAPS,
        "initial_selected_tab": "determined_at_live_precheck",
        "selected_tab_capture": "speed_no_tap_only_if_initially_selected",
        "remaining_tabs": "normalize_to_speed_then_capture_remaining_tabs",
        "canonical_commit_order": [tab.value for tab in TRAINING_TAB_ORDER],
        "post_tap_verification": "bounded_no_tap_polling",
        "post_tap_max_attempts": config.post_tap_max_attempts,
        "post_tap_required_consecutive_matches": config.post_tap_required_consecutive_matches,
        "post_tap_interval_ms": int(config.post_tap_interval_seconds * 1000),
        "post_tap_max_total_wait_ms": int(
            (
                config.settle_seconds
                + (config.post_tap_max_attempts - 1) * config.post_tap_interval_seconds
            )
            * 1000
        ),
        "post_tap_retry_performs_tap": False,
        "will_modify_manifest": False,
        "will_write_formal_raw": False,
        "execution_root": str(config.execution_root),
        "collection_plan": str(config.collection_plan),
        "manifest_path": str(config.manifest_path),
        "run_session_id": config.run_session_id,
        "actual_group_id": config.actual_group_id,
        "turn_index": config.turn_index,
        "source_state_key": config.source_state_key,
        "operator_id": config.operator_id,
        "canonical_raws": [
            {
                "index": i,
                "tab": tab.value,
                "target": f"{tab.value}_tab",
                "point": {"x": coords[tab][0], "y": coords[tab][1]},
                "raw_path": str(_raw_relative_path(config.actual_group_id, i)),
                "live_action": (
                    "capture_without_tap_if_speed_initially_selected_"
                    "otherwise_normalize_to_speed_then_capture"
                ),
            }
            for i, tab in enumerate(TRAINING_TAB_ORDER, start=1)
        ],
        "validator": "scripts/training_gain_collection_execution.py",
        "note": "dry-run only: no ADB input/tap, no manifest update, no formal raw write",
    }


class BoundedTrainingTabCapture:
    def __init__(
        self,
        config: BoundedCaptureConfig,
        *,
        adb: Any,
        verifier: Any | None = None,
        validator: Callable[[Path, Path, Path | None, bool], Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.adb = adb
        self.verifier = verifier or DefaultVerifier()
        self.validator = validator or _run_formal_validator
        self.coords = _training_tab_points(config)
        self.sleep_fn = sleep_fn

    def run(self) -> dict[str, Any]:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{self.config.actual_group_id}.staging-",
                dir=str(self.config.execution_root),
            )
        )
        candidate = self.config.execution_root / (
            f".execution_manifest.candidate.{self.config.run_session_id}."
            f"{self.config.actual_group_id}.json"
        )
        committed_raws: list[Path] = []
        candidate_created = False

        try:
            initial_state = self._preflight(staging)
            captures, trace = self._capture_to_staging(staging, initial_state)
            self._validate_captures(captures)

            manifest = _load_json(self.config.manifest_path)
            candidate_manifest = self._build_candidate_manifest(manifest, captures, trace)
            _write_json_without_bom(candidate, candidate_manifest)
            candidate_created = True
            self.validator(candidate, self.config.collection_plan, None, False)

            backup = self._backup_authoritative()
            committed_raws = self._commit_raws(captures)
            os.replace(candidate, self.config.manifest_path)
            self._append_operator_log(backup, captures, trace)

            return {
                "status": "committed",
                "actual_group_id": self.config.actual_group_id,
                "raw_files": [str(path) for path in committed_raws],
                "manifest_path": str(self.config.manifest_path),
                "backup_path": str(backup),
                **trace,
            }
        except Exception:
            for path in reversed(committed_raws):
                path.unlink(missing_ok=True)
            group_dir = self.config.execution_root / "incoming_raw" / self.config.actual_group_id
            if group_dir.exists() and not any(group_dir.iterdir()):
                group_dir.rmdir()
            if candidate_created:
                candidate.unlink(missing_ok=True)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _preflight(self, staging: Path) -> VerifiedTrainingState:
        if not self.config.manifest_path.is_file():
            raise CaptureSafetyError(f"manifest not found: {self.config.manifest_path}")
        if not self.config.collection_plan.is_file():
            raise CaptureSafetyError(f"collection plan not found: {self.config.collection_plan}")
        if not 0 <= self.config.settle_seconds <= POST_TAP_MAX_INITIAL_SETTLE_SECONDS:
            raise CaptureSafetyError(
                "settle_seconds outside bounded range: "
                f"{self.config.settle_seconds} not in "
                f"[0, {POST_TAP_MAX_INITIAL_SETTLE_SECONDS}]"
            )
        if not 0 <= self.config.post_tap_interval_seconds <= POST_TAP_INTERVAL_SECONDS:
            raise CaptureSafetyError(
                "post_tap_interval_seconds outside bounded range: "
                f"{self.config.post_tap_interval_seconds} not in "
                f"[0, {POST_TAP_INTERVAL_SECONDS}]"
            )
        if not (
            self.config.post_tap_required_consecutive_matches
            <= self.config.post_tap_max_attempts
            <= POST_TAP_MAX_ATTEMPTS
        ):
            raise CaptureSafetyError(
                "post_tap_max_attempts outside bounded range: "
                f"{self.config.post_tap_max_attempts}"
            )
        if not (
            2
            <= self.config.post_tap_required_consecutive_matches
            <= POST_TAP_REQUIRED_CONSECUTIVE_MATCHES
        ):
            raise CaptureSafetyError(
                "post_tap_required_consecutive_matches outside bounded range: "
                f"{self.config.post_tap_required_consecutive_matches}"
            )

        if self.config.expected_manifest_sha:
            actual = _sha256_file(self.config.manifest_path)
            if actual != self.config.expected_manifest_sha.lower():
                raise CaptureSafetyError(
                    f"authoritative manifest SHA drift: expected={self.config.expected_manifest_sha}, actual={actual}"
                )

        manifest = _load_json(self.config.manifest_path)
        existing_group_ids = {g.get("actual_group_id") for g in manifest.get("groups", [])}
        existing_state_keys = {g.get("source_state_key") for g in manifest.get("groups", [])}
        if self.config.actual_group_id in existing_group_ids:
            raise CaptureSafetyError(f"actual_group_id already exists: {self.config.actual_group_id}")
        if self.config.source_state_key in existing_state_keys:
            raise CaptureSafetyError(f"source_state_key already exists: {self.config.source_state_key}")

        if not self.adb.connect():
            raise CaptureSafetyError("ADB connect failed")
        if tuple(self.adb.get_screen_size()) != EXPECTED_SIZE:
            raise CaptureSafetyError(f"unsupported screen size: {self.adb.get_screen_size()}")

        precheck_path = staging / f".{self.config.actual_group_id}.precheck.png"
        try:
            self.adb.screenshot(str(precheck_path))
            result = self.verifier.precheck(precheck_path)
            selected_value = getattr(result, "selected_tab", None)
            try:
                selected_tab = TrainingTab(selected_value)
            except (TypeError, ValueError) as exc:
                raise CaptureSafetyError(
                    f"precheck did not determine a whitelisted selected tab: {selected_value}"
                ) from exc
            return VerifiedTrainingState(
                selected_tab=selected_tab,
                image_path=precheck_path,
                verification=result,
            )
        except Exception:
            precheck_path.unlink(missing_ok=True)
            raise

    def _capture_to_staging(
        self,
        staging: Path,
        initial_state: VerifiedTrainingState,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        captures_by_tab: dict[TrainingTab, dict[str, Any]] = {}
        capture_order: list[str] = []
        tap_targets: list[str] = []
        tap_count = 0
        post_tap_attempts_by_tab: dict[str, int] = {}
        settled_frame_by_tab: dict[str, str] = {}
        no_tap_capture_tab: str | None = None

        current_selected = initial_state.selected_tab
        if initial_state.selected_tab == TrainingTab.SPEED:
            speed_path = staging / f"{self.config.actual_group_id}__raw__001.png"
            os.replace(initial_state.image_path, speed_path)
            no_tap_capture_tab = TrainingTab.SPEED.value
            captures_by_tab[TrainingTab.SPEED] = self._capture_record(
                TrainingTab.SPEED,
                1,
                speed_path,
            )
        else:
            initial_state.image_path.unlink(missing_ok=True)
            tap_count = self._tap_training_tab(
                TrainingTab.SPEED,
                current_selected,
                tap_count=tap_count,
            )
            tap_targets.append(TrainingTab.SPEED.value)
            if self.config.settle_seconds > 0:
                self.sleep_fn(self.config.settle_seconds)
            settled = self._wait_for_expected_selected_tab(
                expected_tab=TrainingTab.SPEED,
                previous_tab=current_selected,
                staging=staging,
                sequence=1,
            )
            current_selected = TrainingTab.SPEED
            post_tap_attempts_by_tab[TrainingTab.SPEED.value] = settled.attempts
            settled_frame_by_tab[TrainingTab.SPEED.value] = settled.verified_frame_path.name
            captures_by_tab[TrainingTab.SPEED] = self._capture_record(
                TrainingTab.SPEED,
                1,
                settled.verified_frame_path,
            )

        capture_order.append(TrainingTab.SPEED.value)
        for tab in TRAINING_TAB_ORDER:
            if tab == TrainingTab.SPEED:
                continue
            tap_count = self._tap_training_tab(tab, current_selected, tap_count=tap_count)
            tap_targets.append(tab.value)
            if self.config.settle_seconds > 0:
                self.sleep_fn(self.config.settle_seconds)

            sequence = TRAINING_TAB_ORDER.index(tab) + 1
            settled = self._wait_for_expected_selected_tab(
                expected_tab=tab,
                previous_tab=current_selected,
                staging=staging,
                sequence=sequence,
            )
            current_selected = tab
            capture_order.append(tab.value)
            post_tap_attempts_by_tab[tab.value] = settled.attempts
            settled_frame_by_tab[tab.value] = settled.verified_frame_path.name
            captures_by_tab[tab] = self._capture_record(
                tab,
                sequence,
                settled.verified_frame_path,
            )

        expected_taps = 4 if initial_state.selected_tab == TrainingTab.SPEED else MAXIMUM_TAPS
        if tap_count != expected_taps or len(captures_by_tab) != len(TRAINING_TAB_ORDER):
            raise CaptureSafetyError(
                f"bounded capture budget mismatch: captures={len(captures_by_tab)}, taps={tap_count}"
            )

        captures = [captures_by_tab[tab] for tab in TRAINING_TAB_ORDER]
        trace = {
            "initial_selected_tab": initial_state.selected_tab.value,
            "no_tap_capture_tab": no_tap_capture_tab,
            "expected_taps": expected_taps,
            "maximum_taps": MAXIMUM_TAPS,
            "tap_targets": tap_targets,
            "tap_count": tap_count,
            "capture_order": capture_order,
            "canonical_commit_order": [tab.value for tab in TRAINING_TAB_ORDER],
            "post_tap_attempts_by_tab": post_tap_attempts_by_tab,
            "settled_frame_by_tab": settled_frame_by_tab,
        }
        return captures, trace

    def _wait_for_expected_selected_tab(
        self,
        *,
        expected_tab: TrainingTab,
        previous_tab: TrainingTab,
        staging: Path,
        sequence: int,
    ) -> PostTapSettleResult:
        consecutive = 0
        last_result: Any | None = None
        last_matching_frame_path: Path | None = None
        last_gain_display_difference: float | None = None
        attempt_paths: list[Path] = []
        final_path = staging / f"{self.config.actual_group_id}__raw__{sequence:03d}.png"

        try:
            for attempt in range(1, self.config.post_tap_max_attempts + 1):
                if attempt > 1:
                    self.sleep_fn(self.config.post_tap_interval_seconds)
                attempt_path = staging / (
                    f".{self.config.actual_group_id}__raw__{sequence:03d}"
                    f".attempt-{attempt:02d}.png"
                )
                attempt_paths.append(attempt_path)
                self.adb.screenshot(str(attempt_path))
                self._validate_frame_file(attempt_path)

                inspect = getattr(self.verifier, "inspect_after_capture", None)
                if inspect is None:
                    inspect = self.verifier.verify_after_capture
                result = inspect(expected_tab, attempt_path)
                last_result = result
                failures = list(getattr(result, "failures", []) or [])
                selected = getattr(result, "selected_tab", None)
                hard_failures = [failure for failure in failures if failure != "selected_tab"]
                retryable_failures = [failure for failure in failures if failure == "selected_tab"]

                if hard_failures:
                    raise self._post_tap_error(
                        expected_tab=expected_tab,
                        previous_tab=previous_tab,
                        result=result,
                        attempt_count=attempt,
                        hard_failures=hard_failures,
                        retryable_failures=retryable_failures,
                    )

                if getattr(result, "accepted", False) and selected == expected_tab.value:
                    consecutive += 1
                    if last_matching_frame_path is not None:
                        last_gain_display_difference = self._gain_display_difference(
                            last_matching_frame_path,
                            attempt_path,
                        )
                    if (
                        consecutive >= self.config.post_tap_required_consecutive_matches
                        and last_gain_display_difference is not None
                        and last_gain_display_difference
                        <= GAIN_DISPLAY_MAX_FOREGROUND_XOR_RATIO
                    ):
                        os.replace(attempt_path, final_path)
                        attempt_paths.remove(attempt_path)
                        return PostTapSettleResult(
                            expected_tab=expected_tab,
                            final_selected_tab=expected_tab,
                            attempts=attempt,
                            consecutive_matches=consecutive,
                            verified_frame_path=final_path,
                            evidence=getattr(result, "evidence", {}) or {},
                        )
                    last_matching_frame_path = attempt_path
                    continue

                consecutive = 0
                last_matching_frame_path = None
                last_gain_display_difference = None
                if failures != ["selected_tab"]:
                    raise self._post_tap_error(
                        expected_tab=expected_tab,
                        previous_tab=previous_tab,
                        result=result,
                        attempt_count=attempt,
                        hard_failures=["unclassified_post_tap_failure"],
                        retryable_failures=[],
                    )
                _VALID_TAB_VALUES = {t.value for t in TrainingTab} | {None}
                if selected not in _VALID_TAB_VALUES:
                    raise self._post_tap_error(
                        expected_tab=expected_tab,
                        previous_tab=previous_tab,
                        result=result,
                        attempt_count=attempt,
                        hard_failures=["unexpected_selected_tab"],
                        retryable_failures=retryable_failures,
                    )

            raise self._post_tap_error(
                expected_tab=expected_tab,
                previous_tab=previous_tab,
                result=last_result,
                attempt_count=self.config.post_tap_max_attempts,
                hard_failures=(
                    ["gain display unstable"]
                    if last_gain_display_difference is not None
                    else []
                ),
                retryable_failures=(
                    [] if last_gain_display_difference is not None else ["selected_tab"]
                ),
            )
        finally:
            for path in attempt_paths:
                path.unlink(missing_ok=True)

    def _validate_frame_file(self, path: Path) -> None:
        img = cv2.imread(str(path))
        if img is None:
            raise CaptureSafetyError(f"cannot read post-tap PNG: {path}")
        height, width = img.shape[:2]
        if (width, height) != EXPECTED_SIZE:
            raise CaptureSafetyError(
                f"unexpected post-tap PNG size for {path.name}: {width}x{height}"
            )

    def _gain_display_difference(self, earlier_path: Path, later_path: Path) -> float:
        return gain_display_foreground_xor_ratio(earlier_path, later_path)

    def _post_tap_error(
        self,
        *,
        expected_tab: TrainingTab,
        previous_tab: TrainingTab,
        result: Any | None,
        attempt_count: int,
        hard_failures: list[str],
        retryable_failures: list[str],
    ) -> CaptureSafetyError:
        selected = getattr(result, "selected_tab", None) if result is not None else None
        evidence = getattr(result, "evidence", {}) if result is not None else {}
        return CaptureSafetyError(
            "post-tap settle failed: "
            f"expected_tab={expected_tab.value}; "
            f"previous_tab={previous_tab.value}; "
            f"last_detected_tab={selected}; "
            f"attempt_count={attempt_count}; "
            "required_consecutive_matches="
            f"{self.config.post_tap_required_consecutive_matches}; "
            f"hard_failures={hard_failures}; "
            f"retryable_failures={retryable_failures}; "
            f"last_evidence={evidence}"
        )

    def _tap_training_tab(
        self,
        target_tab: TrainingTab,
        current_selected_tab: TrainingTab,
        *,
        tap_count: int,
    ) -> int:
        if target_tab not in TRAINING_TAB_ORDER:
            raise CaptureSafetyError(f"non-whitelisted training tab target: {target_tab}")
        if target_tab == current_selected_tab:
            raise CaptureSafetyError(
                f"refusing to tap currently selected training tab: {target_tab.value}"
            )
        if tap_count >= MAXIMUM_TAPS:
            raise CaptureSafetyError(
                f"training tab tap budget exceeded: maximum={MAXIMUM_TAPS}"
            )
        x, y = self.coords[target_tab]
        self.adb.tap(x, y, scaled=False)
        return tap_count + 1

    def _capture_record(
        self,
        tab: TrainingTab,
        sequence: int,
        path: Path,
    ) -> dict[str, Any]:
        return {
            "tab": tab,
            "sequence": sequence,
            "path": path,
            "relative_path": _raw_relative_path(self.config.actual_group_id, sequence),
        }

    def _validate_captures(self, captures: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        for capture in captures:
            path = capture["path"]
            img = cv2.imread(str(path))
            if img is None:
                raise CaptureSafetyError(f"cannot read captured PNG: {path}")
            h, w = img.shape[:2]
            if (w, h) != EXPECTED_SIZE:
                raise CaptureSafetyError(f"unexpected PNG size for {path.name}: {w}x{h}")
            digest = _sha256_file(path)
            if digest in seen:
                raise CaptureSafetyError(f"duplicate screenshot SHA: {digest}")
            seen.add(digest)
            capture["sha256"] = digest
            capture["size_bytes"] = path.stat().st_size

        manifest = _load_json(self.config.manifest_path)
        existing_raw_shas = {
            raw.get("sha256")
            for group in manifest.get("groups", [])
            for raw in group.get("raw_files", [])
        }
        overlap = seen & existing_raw_shas
        if overlap:
            raise CaptureSafetyError(f"captured SHA already exists in manifest: {sorted(overlap)}")

    def _build_candidate_manifest(
        self,
        manifest: dict[str, Any],
        captures: list[dict[str, Any]],
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = json.loads(json.dumps(manifest, ensure_ascii=False))
        plan_sha = _sha256_file(self.config.collection_plan)
        candidate["source_collection_plan"] = {
            "path": _relative_posix(self.config.manifest_path.parent, self.config.collection_plan),
            "sha256": plan_sha,
        }
        now = _now_iso()
        candidate.setdefault("groups", []).append({
            "planned_slot_id": None,
            "actual_group_id": self.config.actual_group_id,
            "run_session_id": self.config.run_session_id,
            "turn_index": self.config.turn_index,
            "source_state_key": self.config.source_state_key,
            "capture_started_at": now,
            "capture_completed_at": now,
            "operator_id": self.config.operator_id,
            "group_review_status": "pending",
            "reviewer_id": None,
            "reviewed_at": None,
            "planned_target_labels": [],
            "raw_files": [
                {
                    "relative_path": str(capture["relative_path"]).replace("\\", "/"),
                    "sha256": capture["sha256"],
                    "size_bytes": capture["size_bytes"],
                    "media_type": "image/png",
                    "original_extension": ".png",
                    "sequence": capture["sequence"],
                    "source_screen": f"{capture['tab'].value}_tab",
                    "captured_at": now,
                }
                for capture in captures
            ],
            "review_items": [],
            "conflict_records": [],
            "provenance": {
                "source": "capture_training_tabs_bounded_live",
                "initial_selected_tab": trace["initial_selected_tab"],
                "no_tap_capture_tab": trace["no_tap_capture_tab"],
                "expected_taps": trace["expected_taps"],
                "maximum_taps": trace["maximum_taps"],
                "tap_targets": trace["tap_targets"],
                "tap_count": trace["tap_count"],
                "capture_order": trace["capture_order"],
                "canonical_commit_order": trace["canonical_commit_order"],
                "post_tap_attempts_by_tab": trace["post_tap_attempts_by_tab"],
                "settled_frame_by_tab": trace["settled_frame_by_tab"],
            },
        })
        return candidate

    def _backup_authoritative(self) -> Path:
        before_sha = _sha256_file(self.config.manifest_path)
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_dir = self.config.execution_root / "backups" / self.config.run_session_id
        backup = backup_dir / f"execution_manifest.{timestamp}_local.{before_sha[:12]}.json"
        _write_file_exclusive(backup, self.config.manifest_path.read_bytes())
        if _sha256_file(backup) != before_sha:
            raise CaptureSafetyError("backup SHA mismatch")
        return backup

    def _commit_raws(self, captures: list[dict[str, Any]]) -> list[Path]:
        committed = []
        try:
            for capture in captures:
                target = self.config.execution_root / capture["relative_path"]
                _write_file_exclusive(target, Path(capture["path"]).read_bytes())
                committed.append(target)
            return committed
        except Exception:
            for path in reversed(committed):
                path.unlink(missing_ok=True)
            group_dir = self.config.execution_root / "incoming_raw" / self.config.actual_group_id
            if group_dir.exists() and not any(group_dir.iterdir()):
                group_dir.rmdir()
            raise

    def _append_operator_log(
        self,
        backup: Path,
        captures: list[dict[str, Any]],
        trace: dict[str, Any],
    ) -> None:
        log_dir = self.config.execution_root / "operator_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"session_{self.config.run_session_id}.md"
        lines = [
            "",
            "## Bounded five-tab capture",
            f"- actual_group_id: {self.config.actual_group_id}",
            f"- source_state_key: {self.config.source_state_key}",
            f"- backup_path: {backup}",
            f"- backup_sha256: {_sha256_file(backup)}",
            f"- replacement_method: os.replace",
            f"- authoritative_sha_after: {_sha256_file(self.config.manifest_path)}",
            f"- initial_selected_tab: {trace['initial_selected_tab']}",
            f"- no_tap_capture_tab: {trace['no_tap_capture_tab']}",
            f"- expected_taps: {trace['expected_taps']}",
            f"- maximum_taps: {trace['maximum_taps']}",
            f"- tap_targets: {','.join(trace['tap_targets'])}",
            f"- tap_count: {trace['tap_count']}",
            f"- capture_order: {','.join(trace['capture_order'])}",
            f"- canonical_commit_order: {','.join(trace['canonical_commit_order'])}",
            f"- post_tap_attempts_by_tab: {json.dumps(trace['post_tap_attempts_by_tab'], sort_keys=True)}",
            f"- settled_frame_by_tab: {json.dumps(trace['settled_frame_by_tab'], sort_keys=True)}",
        ]
        for capture in captures:
            lines.append(f"- raw_{capture['sequence']}: {capture['relative_path']} {capture['sha256']}")
        with log_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")


def _training_tab_points(config: BoundedCaptureConfig) -> dict[TrainingTab, tuple[int, int]]:
    coord = CoordinateConfig(config.coordinate_config)
    points = {}
    for tab in TRAINING_TAB_ORDER:
        try:
            points[tab] = coord.point("training_menu", tab.value)
        except KeyError as exc:
            raise CaptureSafetyError(f"missing coordinate for training tab: {tab.value}") from exc
    return points


def _raw_relative_path(group_id: str, sequence: int) -> Path:
    return Path("incoming_raw") / group_id / f"{group_id}__raw__{sequence:03d}.png"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_without_bom(path: Path, data: dict[str, Any]) -> None:
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    _write_file_exclusive(path, encoded)


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _write_file_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_posix(base: Path, target: Path) -> str:
    return os.path.relpath(target.resolve(), base.resolve()).replace("\\", "/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _run_formal_validator(
    manifest_path: Path,
    collection_plan: Path,
    output_dir: Path | None = None,
    pretty: bool = False,
) -> Any:
    from scripts.training_gain_collection_execution import run_execution_validation

    return run_execution_validation(manifest_path, collection_plan, output_dir, pretty)
