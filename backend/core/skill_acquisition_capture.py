"""Bounded screenshot/swipe-only capture for the skill acquisition list."""
from __future__ import annotations
import json
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import cv2
import numpy as np
from core.skill_acquisition_parser import LIST_VIEWPORT, inspect_skill_acquisition_page
CAPTURE_SAFETY_TOKEN = 'CAPTURE_SKILL_PAGES_SCREENSHOT_SWIPE_ONLY'
DEFAULT_SKILL_CAPTURE_TTL_MS = 30 * 60 * 1000

class SkillAcquisitionCaptureSafetyError(RuntimeError):
    """Raised when capture cannot stay inside its screenshot/swipe boundary."""

class SkillCaptureADBFacade:
    """Expose screenshots and bounded vertical list swipes, never taps."""
    __slots__ = ('_screenshot', '_swipe')

    def __init__(self, adb: Any) -> None:
        self._screenshot = adb.screenshot
        self._swipe = adb.swipe

    def screenshot(self, path: str) -> Any:
        return self._screenshot(path)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int=300, scaled: bool=False) -> Any:
        if scaled is not False:
            raise SkillAcquisitionCaptureSafetyError('capture swipe must use absolute pixels')
        if x1 != x2 or y1 <= y2:
            raise SkillAcquisitionCaptureSafetyError('capture swipe must be vertical and upward')
        if not 100 <= int(duration_ms) <= 2000:
            raise SkillAcquisitionCaptureSafetyError('capture swipe duration is outside bounds')
        return self._swipe(x1, y1, x2, y2, duration_ms=duration_ms, scaled=False)

    def tap(self, *_args: Any, **_kwargs: Any) -> None:
        raise SkillAcquisitionCaptureSafetyError('tap capability is forbidden during skill capture')

    def rewind_swipe(self, x1: int, y1: int, x2: int, y2: int, *, duration_ms: int=300, scaled: bool=False) -> Any:
        if scaled is not False:
            raise SkillAcquisitionCaptureSafetyError('capture swipe must use absolute pixels')
        if x1 != x2 or y1 >= y2:
            raise SkillAcquisitionCaptureSafetyError('rewind swipe must be vertical and downward')
        if not 100 <= int(duration_ms) <= 2000:
            raise SkillAcquisitionCaptureSafetyError('capture swipe duration is outside bounds')
        return self._swipe(x1, y1, x2, y2, duration_ms=duration_ms, scaled=False)

@dataclass(frozen=True)
class CaptureGateResult:
    allowed: bool
    reason: str

@dataclass(frozen=True)
class SkillAcquisitionCaptureConfig:
    output_dir: Path
    max_pages: int = 12
    repeated_page_limit: int = 1
    settle_seconds: float = 0.8
    list_roi: tuple[int, int, int, int] = LIST_VIEWPORT
    swipe_start: tuple[int, int] = (360, 900)
    swipe_end: tuple[int, int] = (360, 610)
    swipe_duration_ms: int = 450
    rewind_to_top: bool = False
    max_rewind_swipes: int = 12

def validate_capture_gate(live: bool, execute: bool, safety_token: str | None) -> CaptureGateResult:
    if not live:
        return CaptureGateResult(False, '--live is required')
    if not execute:
        return CaptureGateResult(False, '--execute is required')
    if safety_token != CAPTURE_SAFETY_TOKEN:
        return CaptureGateResult(False, 'safety token mismatch')
    return CaptureGateResult(True, 'screenshot_and_roi_swipe_only')

def _validate_config(config: SkillAcquisitionCaptureConfig) -> None:
    if config.max_pages < 1:
        raise ValueError('max_pages must be positive')
    if config.repeated_page_limit < 1:
        raise ValueError('repeated_page_limit must be positive')
    x1, y1, x2, y2 = config.list_roi
    for x, y in (config.swipe_start, config.swipe_end):
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            raise ValueError('swipe coordinates must remain inside the skill list ROI')
    if config.swipe_start[0] != config.swipe_end[0]:
        raise ValueError('only vertical skill-list swipes are allowed')
    if config.swipe_start[1] - config.swipe_end[1] < 80:
        raise ValueError('skill-list swipe must move upward by at least 80 reference pixels')
    if not 100 <= config.swipe_duration_ms <= 2000:
        raise ValueError('swipe_duration_ms must be between 100 and 2000')
    if type(config.rewind_to_top) is not bool:
        raise ValueError('rewind_to_top must be boolean')
    if type(config.max_rewind_swipes) is not int or not 1 <= config.max_rewind_swipes <= 12:
        raise ValueError('max_rewind_swipes must be between 1 and 12')

def build_capture_dry_run_plan(config: SkillAcquisitionCaptureConfig) -> dict[str, Any]:
    _validate_config(config)
    return {'dry_run': True, 'will_tap': False, 'will_execute_device_command': False, 'allowed_live_actions': ['screenshot', 'vertical_roi_swipe'], 'forbidden_actions': ['tap', 'plus', 'minus', 'confirm', 'reset', 'back'], 'output_dir': str(config.output_dir), 'max_pages': config.max_pages, 'repeated_page_limit': config.repeated_page_limit, 'list_roi': list(config.list_roi), 'swipe': {'x1': config.swipe_start[0], 'y1': config.swipe_start[1], 'x2': config.swipe_end[0], 'y2': config.swipe_end[1], 'duration_ms': config.swipe_duration_ms}, 'rewind_to_top': config.rewind_to_top, 'max_rewind_swipes': config.max_rewind_swipes, 'stop_conditions': ['list_bottom', 'repeated_page', 'max_pages'], 'required_live_gate': {'live': True, 'execute': True, 'safety_token': CAPTURE_SAFETY_TOKEN}}

def _normalized_roi(image: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    height, width = image.shape[:2]
    sx = width / 720.0
    sy = height / 1280.0
    x1, y1, x2, y2 = roi
    return image[round(y1 * sy):round(y2 * sy), round(x1 * sx):round(x2 * sx)]

def _page_signature(image: np.ndarray, roi: tuple[int, int, int, int]) -> np.ndarray:
    crop = _normalized_roi(image, roi)
    if crop.size == 0:
        raise SkillAcquisitionCaptureSafetyError('skill list ROI is empty')
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)

def _same_page(left: np.ndarray, right: np.ndarray) -> bool:
    if left.shape != right.shape:
        return False
    return float(cv2.absdiff(left, right).mean()) <= 1.0

def _is_at_bottom(image: np.ndarray) -> bool:
    """Conservatively detect a scroll thumb touching the list viewport bottom."""
    height, width = image.shape[:2]
    sx = width / 720.0
    sy = height / 1280.0
    x1, x2 = (round(694 * sx), round(709 * sx))
    y1, y2 = (round(476 * sy), round(1027 * sy))
    strip = image[y1:y2, x1:x2]
    if strip.size == 0:
        return False
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    thumb = ((hsv[:, :, 2] < 180) & (hsv[:, :, 1] > 15)).mean(axis=1) > 0.2
    indices = np.flatnonzero(thumb)
    if len(indices) < max(8, round(20 * sy)):
        return False
    return int(indices.max()) >= strip.shape[0] - max(5, round(10 * sy))

class SkillAcquisitionPageCapturer:
    """Capture pages with screenshot and vertical ROI swipe calls only."""

    def __init__(self, config: SkillAcquisitionCaptureConfig, *, adb: Any, sleep_fn: Callable[[float], None]=time.sleep, wall_clock_ms_fn: Callable[[], int]=lambda: time.time_ns() // 1000000, live: bool=False, execute: bool=False, safety_token: str | None=None) -> None:
        _validate_config(config)
        gate = validate_capture_gate(live, execute, safety_token)
        if not gate.allowed:
            raise SkillAcquisitionCaptureSafetyError(f'capture authorization rejected: {gate.reason}')
        self.config = config
        self.adb = adb
        self.sleep_fn = sleep_fn
        self.wall_clock_ms_fn = wall_clock_ms_fn

    def _capture(self, index: int) -> tuple[np.ndarray, Path]:
        path = self.config.output_dir / f'page_{index:02d}.png'
        image = self.adb.screenshot(str(path))
        if image is None:
            raise SkillAcquisitionCaptureSafetyError('screenshot failed')
        page_evidence = inspect_skill_acquisition_page(image)
        if not page_evidence['accepted']:
            raise SkillAcquisitionCaptureSafetyError(f'screenshot does not look like a skill acquisition page: {page_evidence}')
        return (image, path)

    def _swipe(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        sx = width / 720.0
        sy = height / 1280.0
        start_x, start_y = self.config.swipe_start
        end_x, end_y = self.config.swipe_end
        self.adb.swipe(round(start_x * sx), round(start_y * sy), round(end_x * sx), round(end_y * sy), duration_ms=self.config.swipe_duration_ms, scaled=False)

    def _rewind_swipe(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        sx = width / 720.0
        sy = height / 1280.0
        start_x, start_y = self.config.swipe_end
        end_x, end_y = self.config.swipe_start
        rewind = getattr(self.adb, 'rewind_swipe', None)
        if not callable(rewind):
            rewind = self.adb.swipe
        rewind(round(start_x * sx), round(start_y * sy), round(end_x * sx), round(end_y * sy), duration_ms=self.config.swipe_duration_ms, scaled=False)

    def _capture_rewind_probe(self, index: int) -> tuple[np.ndarray, Path]:
        path = self.config.output_dir / f'rewind_probe_{index:02d}.png'
        image = self.adb.screenshot(str(path))
        if image is None:
            raise SkillAcquisitionCaptureSafetyError('rewind screenshot failed')
        evidence = inspect_skill_acquisition_page(image)
        if evidence.get('accepted') is not True:
            raise SkillAcquisitionCaptureSafetyError('rewind screenshot is not a skill acquisition page')
        return (image, path)

    def _rewind_to_top(self) -> int:
        image, _path = self._capture_rewind_probe(0)
        previous = _page_signature(image, self.config.list_roi)
        for swipe_count in range(1, self.config.max_rewind_swipes + 1):
            self._rewind_swipe(image)
            self.sleep_fn(self.config.settle_seconds)
            image, _path = self._capture_rewind_probe(swipe_count)
            current = _page_signature(image, self.config.list_roi)
            if _same_page(previous, current):
                return swipe_count
            previous = current
        raise SkillAcquisitionCaptureSafetyError('skill list top was not verified')

    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        rewind_swipe_count = self._rewind_to_top() if self.config.rewind_to_top else 0
        captured_pages: list[str] = []
        page_evidence: list[dict[str, Any]] = []
        previous_signature: np.ndarray | None = None
        repeated_pages = 0
        stop_reason = 'max_pages'
        index = 0
        while index < self.config.max_pages:
            image, path = self._capture(index)
            signature = _page_signature(image, self.config.list_roi)
            if previous_signature is not None and _same_page(previous_signature, signature):
                repeated_pages += 1
                if repeated_pages >= self.config.repeated_page_limit:
                    stop_reason = 'repeated_page'
                    break
            else:
                repeated_pages = 0
                captured_pages.append(str(path))
                if not path.is_file():
                    raise SkillAcquisitionCaptureSafetyError('screenshot evidence file is missing')
                page_evidence.append({'page_index': len(captured_pages) - 1, 'path': str(path), 'raw_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'content_signature_sha256': hashlib.sha256(signature.tobytes()).hexdigest()})
            if _is_at_bottom(image):
                stop_reason = 'list_bottom'
                break
            if index + 1 >= self.config.max_pages:
                stop_reason = 'max_pages'
                break
            previous_signature = signature
            self._swipe(image)
            self.sleep_fn(self.config.settle_seconds)
            index += 1
        captured_at_unix_ms = self.wall_clock_ms_fn()
        if isinstance(captured_at_unix_ms, bool) or not isinstance(captured_at_unix_ms, int):
            raise SkillAcquisitionCaptureSafetyError('capture wall clock must return integer unix milliseconds')
        if captured_at_unix_ms <= 0:
            raise SkillAcquisitionCaptureSafetyError('capture wall clock must be positive')
        result = {'schema_version': 'uma.skill_acquisition_capture.v1', 'capture_mode': 'screenshot_and_vertical_roi_swipe_only', 'captured_pages': captured_pages, 'captured_page_count': len(captured_pages), 'unique_page_count': len(captured_pages), 'duplicate_tail_count': repeated_pages, 'stop_reason': stop_reason, 'tap_count': 0, 'rewound_to_top': self.config.rewind_to_top, 'rewind_swipe_count': rewind_swipe_count, 'list_roi': list(self.config.list_roi), 'page_evidence': page_evidence, 'captured_at_unix_ms': captured_at_unix_ms, 'expires_at_unix_ms': captured_at_unix_ms + DEFAULT_SKILL_CAPTURE_TTL_MS, 'elapsed_ms': max(0, round((time.perf_counter() - started) * 1000))}
        (self.config.output_dir / 'capture_manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
__all__ = ['CAPTURE_SAFETY_TOKEN', 'DEFAULT_SKILL_CAPTURE_TTL_MS', 'CaptureGateResult', 'SkillAcquisitionCaptureConfig', 'SkillAcquisitionCaptureSafetyError', 'SkillAcquisitionPageCapturer', 'SkillCaptureADBFacade', 'build_capture_dry_run_plan', 'validate_capture_gate']
