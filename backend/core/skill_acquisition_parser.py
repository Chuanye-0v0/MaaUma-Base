"""Offline parser for Uma Musume skill-acquisition screenshots.

The parser is deliberately conservative.  UI geometry and numeric fields are
read locally, while uncertain text remains low-confidence or unknown for human
review.  This module never performs device interaction or purchases.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import cv2
import numpy as np
from core.image_io import read_image, write_image
from core.skill_ocr_performance import active_harvest_ocr, tesseract_stdin
SCHEMA_VERSION = 'uma.skill_acquisition_manifest.v1'
REFERENCE_WIDTH = 720
REFERENCE_HEIGHT = 1280
LIST_VIEWPORT = (30, 468, 690, 1029)
ROW_STRIDE = 185
ROW_HEIGHT = 165
MIN_USABLE_ROW_HEIGHT = 75
SUPPORTED_OCR_BACKENDS = frozenset({'none', 'tesseract', 'rapidocr', 'auto'})
_RAPIDOCR_ENGINE: Any | None = None

class SkillAcquisitionOcrRuntimeError(RuntimeError):
    """Raised when the local OCR runtime cannot satisfy OCR requirements."""

@dataclass(frozen=True)
class TesseractRuntime:
    executable: Path
    tessdata_dir: Path
    available_languages: frozenset[str]

def _tessdata_candidates(executable: Path) -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get('TESSDATA_PREFIX')
    if configured:
        prefix = Path(configured)
        candidates.extend((prefix, prefix / 'tessdata'))
    candidates.extend((Path(sys.prefix) / 'share' / 'tessdata', executable.parents[2] / 'share' / 'tessdata', executable.parent / 'tessdata'))
    return list(dict.fromkeys((candidate.resolve() for candidate in candidates)))

def _trained_languages(directory: Path) -> frozenset[str]:
    return frozenset((path.stem for path in directory.glob('*.traineddata')))

def _tessdata_cli_path(directory: Path) -> str:
    """Return a Windows-safe Tesseract CLI path without mutating env vars."""
    text = str(directory.resolve()).replace('\\', '/')
    if os.name != 'nt' or ' ' not in text:
        return text
    import ctypes
    required = ctypes.windll.kernel32.GetShortPathNameW(text, None, 0)
    if required <= 0:
        raise SkillAcquisitionOcrRuntimeError('tessdata_short_path_unavailable')
    buffer = ctypes.create_unicode_buffer(required)
    if ctypes.windll.kernel32.GetShortPathNameW(text, buffer, required) <= 0:
        raise SkillAcquisitionOcrRuntimeError('tessdata_short_path_unavailable')
    return buffer.value.replace('\\', '/')

def resolve_tesseract_runtime(*, tessdata_dir: str | Path | None=None) -> TesseractRuntime:
    """Choose an installed, non-APK tessdata directory for offline OCR."""
    executable_text = shutil.which('tesseract')
    if not executable_text:
        raise SkillAcquisitionOcrRuntimeError('tesseract_executable_missing')
    executable = Path(executable_text).resolve()
    candidates = [Path(tessdata_dir).resolve()] if tessdata_dir is not None else _tessdata_candidates(executable)
    if tessdata_dir is not None and (not candidates[0].is_dir()):
        raise SkillAcquisitionOcrRuntimeError(f'tessdata_directory_missing:{candidates[0]}')
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        languages = _trained_languages(candidate)
        if 'eng' in languages:
            return TesseractRuntime(executable, candidate, languages)
    rendered = ';'.join((str(candidate) for candidate in candidates))
    raise SkillAcquisitionOcrRuntimeError(f'tessdata_eng_missing:{rendered}')

def _read_image(path: Path) -> np.ndarray:
    image = read_image(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f'cannot read skill acquisition image: {path}')
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f'expected BGR image with three channels: {path}')
    return image

def _normalize_image(image: np.ndarray) -> tuple[np.ndarray, tuple[float, float]]:
    height, width = image.shape[:2]
    if width < 360 or height < 640:
        raise ValueError(f'skill acquisition image is too small: {width}x{height}')
    scale = (width / REFERENCE_WIDTH, height / REFERENCE_HEIGHT)
    if (width, height) == (REFERENCE_WIDTH, REFERENCE_HEIGHT):
        return (image, scale)
    normalized = cv2.resize(image, (REFERENCE_WIDTH, REFERENCE_HEIGHT), interpolation=cv2.INTER_AREA)
    return (normalized, scale)

def _contiguous_true_segments(mask: np.ndarray, min_length: int) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(mask.tolist()):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_length:
                segments.append((start, index - 1))
            start = None
    if start is not None and len(mask) - start >= min_length:
        segments.append((start, len(mask) - 1))
    return segments

def detect_skill_row_boxes(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Detect usable row cards in a normalized skill page.

    The current UI uses a stable 165 px card and 20 px gap.  We anchor the
    sequence on the first clean horizontal gap, which also handles a clipped
    row at the top after scrolling.
    """
    return _detect_skill_row_layout(image)['usable_boxes']

def _detect_skill_row_layout(image: np.ndarray) -> dict[str, Any]:
    normalized, _scale = _normalize_image(image)
    x1, viewport_top, x2, viewport_bottom = LIST_VIEWPORT
    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    horizontal_mean = gray[:, x1 + 10:x2 - 20].mean(axis=1)
    gap_mask = horizontal_mean[viewport_top:viewport_bottom] > 254.5
    gaps = _contiguous_true_segments(gap_mask, min_length=10)
    if not gaps:
        short_gaps = _contiguous_true_segments(gap_mask, min_length=5)
        if not short_gaps:
            return {'usable_boxes': [], 'top_fragment': None, 'bottom_fragment': None}
        core_start, core_end = short_gaps[0]
        search_start = viewport_top + core_end + 1
        search_stop = min(search_start + 25, viewport_bottom)
        dark_boundaries = np.flatnonzero(horizontal_mean[search_start:search_stop] < 220.0)
        if len(dark_boundaries) == 0:
            return {'usable_boxes': [], 'top_fragment': None, 'bottom_fragment': None}
        gap_end_absolute = search_start + int(dark_boundaries[0]) - 1
        gaps = [(core_start, gap_end_absolute - viewport_top)]
    gap_start = viewport_top + gaps[0][0]
    gap_end = viewport_top + gaps[0][1]
    boxes: list[tuple[int, int, int, int]] = []
    top_partial_height = gap_start - viewport_top
    top_fragment = (x1, viewport_top, x2, gap_start) if top_partial_height > 0 else None
    if top_partial_height >= MIN_USABLE_ROW_HEIGHT:
        boxes.append(top_fragment)
    row_start = gap_end + 1
    bottom_fragment: tuple[int, int, int, int] | None = None
    while row_start < viewport_bottom:
        row_end = min(row_start + ROW_HEIGHT, viewport_bottom)
        candidate = (x1, row_start, x2, row_end)
        if row_end - row_start >= MIN_USABLE_ROW_HEIGHT:
            boxes.append(candidate)
        if row_end - row_start < ROW_HEIGHT:
            bottom_fragment = candidate
        row_start += ROW_STRIDE
    return {'usable_boxes': boxes, 'top_fragment': top_fragment, 'bottom_fragment': bottom_fragment}

def _tesseract_module(runtime: TesseractRuntime):
    try:
        import pytesseract
    except ImportError:
        return None
    pytesseract.pytesseract.tesseract_cmd = str(runtime.executable)
    return pytesseract

def rapidocr_available() -> bool:
    """Return whether the RapidOCR package can be imported."""
    try:
        import rapidocr_onnxruntime
    except ImportError:
        return False
    return True

def resolve_ocr_backend(requested: str) -> tuple[str, bool]:
    """Resolve a requested backend to the effective backend to use.

    ``auto`` prefers RapidOCR when it is installed and falls back to
    Tesseract otherwise.  The selected backend reads both text and numbers.
    """
    if requested not in SUPPORTED_OCR_BACKENDS:
        raise ValueError(f'unsupported OCR backend: {requested}')
    if requested == 'none':
        return ('none', rapidocr_available())
    if requested == 'auto':
        return ('rapidocr' if rapidocr_available() else 'tesseract', rapidocr_available())
    return (requested, rapidocr_available())

def _rapidocr_engine():
    global _RAPIDOCR_ENGINE
    harvest = active_harvest_ocr()
    if harvest is not None:
        if harvest['engine'] is None:
            from rapidocr_onnxruntime import RapidOCR
            harvest['engine'] = RapidOCR(det_limit_type='max', det_limit_side_len=960, intra_op_num_threads=1, inter_op_num_threads=1)
        return harvest['engine']
    if _RAPIDOCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            from core.ocr_runtime import create_ocr
        except ImportError as exc:
            raise SkillAcquisitionOcrRuntimeError('rapidocr_module_missing') from exc
        try:
            _RAPIDOCR_ENGINE = create_ocr()
        except Exception as exc:
            raise SkillAcquisitionOcrRuntimeError(f'rapidocr_engine_init_failed:{exc}') from exc
    return _RAPIDOCR_ENGINE

def _harvest_fixed_text_lines(image, engine, upscale, *, minimum=0.98):
    """Recognize ink-bounded lines in known field crops; uncertain cases fall back."""
    if not image.size or image.shape[0] > 150 or image.shape[1] > 700:
        return None
    if image.ndim == 2:
        mask = image > 127
    else:
        blue, green, red = (image[:, :, i].astype(np.int16) for i in range(3))
        mask = (red > blue + 35) & (red > green + 20) & (red < 220) & (green < 180)
    rows = np.flatnonzero(mask.sum(axis=1) >= 2)
    if not len(rows):
        return None
    groups = np.split(rows, np.where(np.diff(rows) > 4)[0] + 1)
    if not 1 <= len(groups) <= 4 or any((not 8 <= group[-1] - group[0] + 1 <= 42 for group in groups)):
        return None
    items = []
    for group in groups:
        top, bottom = (max(0, int(group[0]) - 2), min(image.shape[0], int(group[-1]) + 3))
        columns = np.flatnonzero(mask[top:bottom].any(axis=0))
        left, right = (max(0, int(columns[0]) - 2), min(image.shape[1], int(columns[-1]) + 3))
        result, _ = engine(image[top:bottom, left:right], use_det=False, use_cls=False)
        active_harvest_ocr()['rapid_calls'] += 1
        if not result or len(result) != 1 or float(result[0][1]) < minimum:
            return None
        text = str(result[0][0]).strip()
        if not text:
            return None
        items.append(dict(text=text, confidence=float(result[0][1]), box=[[left * upscale, top * upscale], [right * upscale, top * upscale], [right * upscale, bottom * upscale], [left * upscale, bottom * upscale]]))
    return items

def _rapidocr_ocr_data(image: np.ndarray, *, upscale: int=2, force_detection: bool=False, full_resolution: bool=False) -> list[dict[str, Any]]:
    harvest = active_harvest_ocr()
    cache_key = (image.shape, str(image.dtype), upscale, force_detection, full_resolution, hashlib.sha256(image.tobytes()).digest()) if harvest is not None else None
    if harvest is not None and cache_key in harvest['cache']:
        harvest['cache_hits'] += 1
        return copy.deepcopy(harvest['cache'][cache_key])
    if harvest is not None and len(harvest['cache']) >= 2048:
        harvest['cache'].pop(next(iter(harvest['cache'])))
    engine = _rapidocr_engine()
    if full_resolution and harvest is not None:
        if 'full_resolution_engine' not in harvest:
            from core.ocr_runtime import create_ocr
            harvest['full_resolution_engine'] = create_ocr()
        engine = harvest['full_resolution_engine']
    if harvest is not None and (not force_detection):
        lines = _harvest_fixed_text_lines(image, engine, upscale)
        if lines is not None:
            harvest['cache'][cache_key] = copy.deepcopy(lines)
            return lines
    prepared = image
    if upscale > 1:
        prepared = cv2.resize(image, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    try:
        result, _elapse = engine(prepared, use_det=True, use_cls=False) if harvest is not None and (not full_resolution) else engine(prepared)
        if harvest is not None:
            harvest['rapid_calls'] += 1
    except Exception as exc:
        raise SkillAcquisitionOcrRuntimeError(f'rapidocr_ocr_failed:{exc}') from exc
    items: list[dict[str, Any]] = []
    for entry in result or []:
        text = str(entry[1]).strip()
        if not text:
            continue
        try:
            score = float(entry[2])
        except (TypeError, ValueError):
            score = 0.0
        box = entry[0] if len(entry) > 0 and entry[0] else None
        items.append({'text': text, 'confidence': score, 'box': box})
    if harvest is not None:
        harvest['cache'][cache_key] = copy.deepcopy(items)
    return items

def _rapidocr_box_center(box: Any) -> tuple[float, float]:
    if not box:
        return (0.0, 0.0)
    try:
        points = [[float(coord) for coord in point] for point in box]
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if not points:
        return (0.0, 0.0)
    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    return (sum(y_values) / len(y_values), sum(x_values) / len(x_values))

def _rapidocr_join_text(items: list[dict[str, Any]], *, min_confidence: float=0.5, multi_line: bool=False) -> tuple[str, float]:
    kept = [item for item in items if item['confidence'] >= min_confidence]
    if not kept:
        return ('', 0.0)
    if multi_line:
        kept.sort(key=lambda item: (round(_rapidocr_box_center(item['box'])[0] / 8.0), _rapidocr_box_center(item['box'])[1]))
    else:
        kept.sort(key=lambda item: _rapidocr_box_center(item['box'])[1])
    text = ' '.join((item['text'] for item in kept)).strip()
    confidence = sum((item['confidence'] for item in kept)) / len(kept)
    return (text, confidence)

def _rapidocr_numeric_tokens(crop: np.ndarray) -> list[int]:
    """Read a numeric field with RapidOCR and abstain on non-digit text."""
    items = _rapidocr_ocr_data(crop, upscale=3)
    if len(items) != 1:
        return []
    item = items[0]
    if item['confidence'] < 0.9 or not re.fullmatch('\\d+', item['text']):
        return []
    return [int(item['text'])]

def _rapidocr_cost_tokens(crop: np.ndarray) -> list[int]:
    """Read the single price token while ignoring an adjacent hint label."""
    value, state, _confidence = _rapidocr_cost_evidence(crop)
    return [value] if state == 'accepted' and value is not None else []

def _is_top_discount_hint(item: dict[str, Any], scaled_height: int) -> bool:
    """Recognize an explicit discount label above the price band."""
    if not re.fullmatch('\\d{1,2}%[O0]F[FP]', item.get('text', '').upper()):
        return False
    box = item.get('box')
    if not isinstance(box, list) or len(box) != 4:
        return False
    try:
        y_values = [float(point[1]) for point in box]
    except (IndexError, TypeError, ValueError):
        return False
    return min(y_values) >= 0 and max(y_values) <= scaled_height * 0.25

def _rapidocr_cost_evidence(crop: np.ndarray) -> tuple[int | None, str, float | None]:
    """Keep absent, weak, and conflicting RapidOCR prices distinct."""
    scaled_height = crop.shape[0] * 3
    items = [item for item in _rapidocr_ocr_data(crop, upscale=3) if not _is_top_discount_hint(item, scaled_height)]
    if not items:
        return (None, 'empty', None)
    if len(items) != 1 or not re.fullmatch('\\d+', items[0]['text']):
        return (None, 'untrusted', None)
    confidence = float(items[0]['confidence'])
    return (int(items[0]['text']), 'accepted' if confidence >= 0.9 else 'weak', confidence)

def _read_name_rapidocr(crop: np.ndarray) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for upscale in (2, 3):
        items = _rapidocr_ocr_data(crop, upscale=upscale)
        text, confidence = _rapidocr_join_text(items)
        normalized = _normalize_ocr_text(text)
        if normalized:
            candidates.append({'variant': f'rapidocr_up{upscale}', 'text': normalized, 'confidence': round(min(1.0, confidence), 4)})
            if active_harvest_ocr() is not None and confidence >= 0.98 and re.search('[\\u3400-\\u9fff]', normalized):
                break
    if candidates and max((item['confidence'] for item in candidates)) < 0.9 and (crop.shape[0] >= 40):
        for upscale in (2, 3):
            text, confidence = _rapidocr_join_text(_rapidocr_ocr_data(crop[6:40], upscale=upscale))
            normalized = _normalize_ocr_text(text)
            if normalized:
                candidates.append({'variant': f'rapidocr_name_inset_up{upscale}', 'text': normalized, 'confidence': round(min(1.0, confidence), 4)})
        inset = crop[6:46]
        hsv = cv2.cvtColor(inset, cv2.COLOR_BGR2HSV)
        ink = ((hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 30) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] < 210)).astype(np.uint8)
        stats = cv2.connectedComponentsWithStats(ink)[2][1:]
        glyphs = [box for box in stats if box[3] >= 10 and box[4] >= 30]
        if len(glyphs) >= 2:
            right = min(inset.shape[1], max((int(box[0] + box[2]) for box in glyphs)) + 4)
            if right < inset.shape[1] - 20:
                for upscale in (2, 3):
                    text, confidence = _rapidocr_join_text(_rapidocr_ocr_data(inset[:, :right], upscale=upscale, force_detection=True, full_resolution=True))
                    normalized = _normalize_ocr_text(text)
                    if normalized:
                        candidates.append(dict(variant=f'rapidocr_name_glyphs_up{upscale}', text=normalized, confidence=round(min(1.0, confidence), 4)))
    if not candidates:
        return {'text': None, 'confidence': 0.0, 'accepted': False, 'candidates': []}
    groups = Counter((item['text'] for item in candidates))
    candidates.sort(key=lambda item: (groups[item['text']], item['confidence'], len(item['text'])), reverse=True)
    best = candidates[0]
    support_count = groups[best['text']]
    consensus = support_count / len(candidates)
    confidence = best['confidence'] * (0.65 + 0.35 * consensus)
    contains_cjk = bool(re.search('[\\u3400-\\u9fff]', best['text']))
    latin_length = len(re.sub('[^0-9A-Za-z]', '', best['text']))
    accepted = support_count >= 2 and (contains_cjk or latin_length >= 5) or (best['confidence'] >= 0.9 and contains_cjk and (len(best['text']) >= 2)) or (best['confidence'] >= 0.9 and latin_length >= 5)
    return {'text': best['text'] if accepted else None, 'confidence': round(min(1.0, confidence), 4), 'accepted': accepted, 'candidates': candidates}

def _read_description_rapidocr(crop: np.ndarray) -> dict[str, Any]:
    items = _rapidocr_ocr_data(crop, upscale=2)
    text, confidence = _rapidocr_join_text(items, multi_line=True)
    cleaned = re.sub('\\s+', ' ', text).strip()
    return {'text': cleaned or None, 'confidence': round(min(1.0, confidence), 4), 'raw': text}

def _ocr_data(image: np.ndarray, *, lang: str='chi_sim+eng', psm: int=7, whitelist: str | None=None, runtime: TesseractRuntime | None=None) -> tuple[str, float]:
    runtime = runtime or resolve_tesseract_runtime()
    requested_languages = lang.split('+')
    available_languages = [language for language in requested_languages if language in runtime.available_languages]
    if not available_languages:
        raise SkillAcquisitionOcrRuntimeError(f"ocr_language_missing:{lang}:available={','.join(sorted(runtime.available_languages))}")
    pytesseract = _tesseract_module(runtime)
    if pytesseract is None:
        raise SkillAcquisitionOcrRuntimeError('pytesseract_module_missing')
    safe_tessdata_dir = _tessdata_cli_path(runtime.tessdata_dir)
    config = f'--tessdata-dir {safe_tessdata_dir} --psm {psm}'
    if whitelist:
        config += f' -c tessedit_char_whitelist={whitelist}'
    try:
        harvest = active_harvest_ocr()
        if harvest is not None:
            data = tesseract_stdin(image, runtime, '+'.join(available_languages), psm, whitelist)
        else:
            data = pytesseract.image_to_data(image, lang='+'.join(available_languages), config=config, output_type=pytesseract.Output.DICT)
    except Exception as exc:
        raise SkillAcquisitionOcrRuntimeError(f'tesseract_ocr_failed:{exc}') from exc
    tokens: list[str] = []
    confidences: list[float] = []
    for token, raw_confidence in zip(data['text'], data['conf']):
        token = token.strip()
        if not token:
            continue
        tokens.append(token)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            continue
        if confidence >= 0:
            confidences.append(confidence / 100.0)
    return (' '.join(tokens).strip(), sum(confidences) / len(confidences) if confidences else 0.0)

def _prepare_text_variants(crop: np.ndarray) -> list[tuple[str, np.ndarray]]:
    enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    variants = [('raw', enlarged)]
    for threshold in (130, 150, 170):
        _value, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
        variants.append((f'binary_{threshold}', cv2.resize(binary, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)))
    return variants

def _normalize_ocr_text(text: str) -> str:
    compact = re.sub('\\s+', '', text).strip('|_-')
    return re.sub('[\\u3007\\u25cb\\u25ce，,。．.!！?？<>（）()]+$', '', compact)

def _read_name(crop: np.ndarray, backend: str, runtime: TesseractRuntime | None=None) -> dict[str, Any]:
    if backend == 'none':
        return {'text': None, 'confidence': 0.0, 'accepted': False, 'candidates': []}
    if backend == 'rapidocr':
        return _read_name_rapidocr(crop)
    candidates: list[dict[str, Any]] = []
    for variant_name, variant in _prepare_text_variants(crop):
        text, confidence = _ocr_data(variant, psm=7, runtime=runtime)
        normalized = _normalize_ocr_text(text)
        if normalized:
            candidates.append({'variant': variant_name, 'text': normalized, 'confidence': round(confidence, 4)})
    if not candidates:
        return {'text': None, 'confidence': 0.0, 'accepted': False, 'candidates': []}
    groups = Counter((item['text'] for item in candidates))
    candidates.sort(key=lambda item: (groups[item['text']], item['confidence'], len(item['text'])), reverse=True)
    best = candidates[0]
    support_count = groups[best['text']]
    consensus = support_count / len(candidates)
    confidence = best['confidence'] * (0.65 + 0.35 * consensus)
    contains_cjk = bool(re.search('[\\u3400-\\u9fff]', best['text']))
    latin_length = len(re.sub('[^0-9A-Za-z]', '', best['text']))
    accepted = support_count >= 2 and (contains_cjk or latin_length >= 5) or (best['confidence'] >= 0.9 and contains_cjk and (len(best['text']) >= 2))
    return {'text': best['text'] if accepted else None, 'confidence': round(min(1.0, confidence), 4), 'accepted': accepted, 'candidates': candidates}

def _read_description(crop: np.ndarray, backend: str, runtime: TesseractRuntime | None=None) -> dict[str, Any]:
    if backend == 'none':
        return {'text': None, 'confidence': 0.0, 'raw': ''}
    if backend == 'rapidocr':
        return _read_description_rapidocr(crop)
    enlarged = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    text, confidence = _ocr_data(enlarged, psm=6, runtime=runtime)
    cleaned = re.sub('\\s*\\n\\s*', ' ', text).strip()
    return {'text': cleaned or None, 'confidence': round(confidence, 4), 'raw': text}

def _read_numeric_tokens(crop: np.ndarray, psm: int=6, runtime: TesseractRuntime | None=None, backend: str='tesseract') -> list[int]:
    if backend == 'rapidocr':
        return _rapidocr_numeric_tokens(crop)
    enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    text, _confidence = _ocr_data(enlarged, lang='eng', psm=psm, whitelist='0123456789', runtime=runtime)
    return [int(token) for token in re.findall('\\d+', text)]

def read_current_skill_points(image: np.ndarray, *, runtime: TesseractRuntime | None=None, backend: str='tesseract') -> int | None:
    normalized, _scale = _normalize_image(image)
    crop = normalized[400:440, 520:650]
    if backend == 'rapidocr':
        items = _rapidocr_ocr_data(normalized[390:450, 520:670], upscale=3, force_detection=active_harvest_ocr() is not None)
        values = [int(item['text']) for item in items if re.fullmatch('\\d+', item['text'])]
        plausible = [value for value in values if 0 <= value <= 99999]
        if len(items) == 1 and len(plausible) == 1:
            return plausible[0]
        if len(items) > 1 and len(plausible) == len(items):
            try:
                return read_current_skill_points(normalized, runtime=runtime, backend='tesseract')
            except SkillAcquisitionOcrRuntimeError:
                return None
        return None
    enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    threshold_values: list[int] = []
    for threshold in (170, 190, 200):
        _ignored, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
        text, _confidence = _ocr_data(binary, lang='eng', psm=7, whitelist='0123456789', runtime=runtime)
        tokens = [int(token) for token in re.findall('\\d+', text) if 0 <= int(token) <= 99999]
        if len(tokens) == 1 and _confidence >= 0.9:
            threshold_values.append(tokens[0])
    if len(threshold_values) >= 2:
        return threshold_values[0] if len(set(threshold_values)) == 1 else None
    raw_values = _read_numeric_tokens(crop, psm=7, runtime=runtime)
    raw_plausible = [value for value in raw_values if 0 <= value <= 99999]
    if len(threshold_values) == 1 and len(raw_plausible) == 1 and (threshold_values[0] == raw_plausible[0]):
        return threshold_values[0]
    return None

def inspect_skill_acquisition_page(image: np.ndarray, *, runtime: TesseractRuntime | None=None) -> dict[str, Any]:
    """Return structural evidence that a frame is the skill acquisition page."""
    normalized, _scale = _normalize_image(image)
    title_crop = cv2.resize(normalized[3:38, 20:135], None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    title_text, title_confidence = _ocr_data(title_crop, psm=7, runtime=runtime)
    normalized_title = re.sub('\\s+', '', title_text)
    title_region = normalized[0:45, 0:180]
    title_hsv = cv2.cvtColor(title_region, cv2.COLOR_BGR2HSV)
    title_purple = float(np.mean((title_hsv[:, :, 0] >= 115) & (title_hsv[:, :, 0] <= 160) & (title_hsv[:, :, 1] > 40) & (title_hsv[:, :, 2] > 60)))
    title_white = float(np.mean((title_hsv[:, :, 1] < 60) & (title_hsv[:, :, 2] > 175)))
    title_visual_matches = title_purple > 0.35 and title_white > 0.3
    title_matches = '技能' in normalized_title and '获取' in normalized_title
    current_points = read_current_skill_points(normalized, runtime=runtime)
    if current_points is None:
        current_points = read_current_skill_points(normalized, runtime=runtime, backend='rapidocr')
    row_count = len(detect_skill_row_boxes(normalized))
    accepted = (title_matches or title_visual_matches) and current_points is not None and (row_count > 0)
    return {'accepted': accepted, 'title_ocr': title_text, 'title_confidence': round(title_confidence, 4), 'title_matches': title_matches, 'title_visual_matches': title_visual_matches, 'title_visual_evidence': {'purple_ratio': round(title_purple, 4), 'white_ratio': round(title_white, 4)}, 'current_skill_points': current_points, 'detected_usable_rows': row_count}

def _read_cost(row: np.ndarray, *, top_partial: bool=False, runtime: TesseractRuntime | None=None, backend: str='tesseract') -> int | None:
    return _read_cost_evidence(row, top_partial=top_partial, runtime=runtime, backend=backend)['value']

def _read_tesseract_cost_evidence(crop: np.ndarray, runtime: TesseractRuntime | None) -> tuple[int | None, float | None, str]:
    try:
        enlarged = cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        text, confidence = _ocr_data(enlarged, lang='eng', psm=6, whitelist='0123456789', runtime=runtime)
    except SkillAcquisitionOcrRuntimeError:
        return (None, None, 'unavailable')
    confidence = round(confidence, 4)
    if not re.fullmatch('\\d+', text) or confidence < 0.9:
        return (None, confidence, 'rejected')
    value = int(text)
    if not 10 <= value <= 999:
        return (None, confidence, 'rejected')
    return (value, confidence, 'accepted')

def _read_cost_evidence(row: np.ndarray, *, top_partial: bool=False, runtime: TesseractRuntime | None=None, backend: str='tesseract') -> dict[str, int | float | str | None]:
    height = row.shape[0]
    start_y = 0 if top_partial else 70
    crop = row[start_y:min(height, 130), 515:595]
    if backend == 'rapidocr':
        rapid_value, rapid_state, rapid_confidence = _rapidocr_cost_evidence(crop)
        if rapid_state == 'accepted':
            return {'value': rapid_value, 'source': 'rapidocr', 'confidence': rapid_confidence, 'rapidocr_confidence': rapid_confidence, 'tesseract_confidence': None}
        if rapid_state == 'untrusted':
            return {'value': None, 'source': 'rapidocr_untrusted', 'confidence': None, 'rapidocr_confidence': rapid_confidence, 'tesseract_confidence': None}
        tesseract_value, tesseract_confidence, tesseract_state = _read_tesseract_cost_evidence(crop, runtime)
        if tesseract_state == 'unavailable':
            return {'value': None, 'source': 'tesseract_unavailable', 'confidence': None, 'rapidocr_confidence': rapid_confidence, 'tesseract_confidence': None}
        if rapid_state == 'empty' and tesseract_state == 'accepted':
            return {'value': tesseract_value, 'source': 'tesseract_fallback', 'confidence': tesseract_confidence, 'rapidocr_confidence': None, 'tesseract_confidence': tesseract_confidence}
        if rapid_state == 'weak' and tesseract_state == 'accepted' and (rapid_value == tesseract_value):
            return {'value': tesseract_value, 'source': 'tesseract_crosscheck', 'confidence': tesseract_confidence, 'rapidocr_confidence': rapid_confidence, 'tesseract_confidence': tesseract_confidence}
        return {'value': None, 'source': 'tesseract_rejected', 'confidence': tesseract_confidence, 'rapidocr_confidence': rapid_confidence, 'tesseract_confidence': tesseract_confidence}
    values = _read_numeric_tokens(crop, psm=6, runtime=runtime, backend=backend)
    plausible = [value for value in values if 10 <= value <= 999]
    return {'value': plausible[-1] if plausible else None, 'source': backend, 'confidence': None, 'rapidocr_confidence': None, 'tesseract_confidence': None}

def parse_hint_ocr_text(raw_text: str) -> tuple[int | None, int | None]:
    """Parse hint level and discount independently from OCR text."""
    compact = re.sub('\\s+', '', raw_text).upper().replace('O', '0').replace('°', '')
    discount_match = re.search('(10|20|30|35|40)%', compact)
    discount = int(discount_match.group(1)) if discount_match else None
    prefix = compact[:discount_match.start()] if discount_match else compact
    level_match = re.search('(?:\\u7b49\\u7ea7|[A-Z]{1,4}R)([1-5])\\D*$', prefix)
    level = int(level_match.group(1)) if level_match else None
    return (level, discount)

def _read_hint(row: np.ndarray, *, runtime: TesseractRuntime | None=None, backend: str='tesseract') -> tuple[int | None, int | None, str]:
    height = row.shape[0]
    if active_harvest_ocr() is not None:
        bubble = row[:min(height, 78), 492:608]
        if bubble.size:
            hsv = cv2.cvtColor(bubble, cv2.COLOR_BGR2HSV)
            orange = (hsv[:, :, 0] >= 5) & (hsv[:, :, 0] <= 25) & (hsv[:, :, 1] > 180) & (hsv[:, :, 2] > 200)
            if not np.any(orange):
                return (None, None, '')
    raw_attempts: list[str] = []
    discount_only: int | None = None
    for crop_height in (50, 60, 78):
        bubble = row[0:min(height, crop_height), 492:608]
        if bubble.size == 0:
            continue
        hsv = cv2.cvtColor(bubble, cv2.COLOR_BGR2HSV)
        white_text = ((hsv[:, :, 1] < 90) & (hsv[:, :, 2] > 190)).astype(np.uint8) * 255
        variants = (white_text, cv2.dilate(white_text, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1))
        for variant in variants:
            if backend == 'rapidocr':
                raw_text, _confidence = _rapidocr_join_text(_rapidocr_ocr_data(variant, upscale=3), multi_line=True)
                raw_attempts.append(raw_text)
                level, discount = parse_hint_ocr_text(raw_text)
                if level is not None and discount is not None:
                    return (level, discount, ' | '.join(raw_attempts))
                if discount is not None and discount_only is None:
                    discount_only = discount
                continue
            prepared = cv2.resize(255 - variant, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
            raw_text, _confidence = _ocr_data(prepared, lang='eng', psm=6, runtime=runtime)
            raw_attempts.append(raw_text)
            level, discount = parse_hint_ocr_text(raw_text)
            if level is not None and discount is not None:
                return (level, discount, ' | '.join(raw_attempts))
            if discount is not None and discount_only is None:
                discount_only = discount
    return (None, discount_only, ' | '.join(raw_attempts))

def _green_ratio(roi: np.ndarray) -> float:
    if roi.size == 0:
        return 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 90) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 100)
    return float(mask.mean())

def _pink_ratio(roi: np.ndarray) -> float:
    if roi.size == 0:
        return 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 0] >= 145) & (hsv[:, :, 0] <= 179) & (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 150)
    return float(mask.mean())

def _row_frame(row: np.ndarray, acquired: bool) -> tuple[str, str]:
    if acquired:
        return ('unknown', 'acquired_gradient')
    height = row.shape[0]
    sample = row[min(100, max(0, height - 20)):min(145, height), 270:450]
    if sample.size == 0:
        return ('unknown', 'unknown')
    mean_saturation = float(cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)[:, :, 1].mean())
    if mean_saturation > 50:
        return ('gold', 'gold')
    return ('normal', 'normal')

def _perceptual_fingerprint(image: np.ndarray) -> str:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (128, 64), interpolation=cv2.INTER_AREA)
    binary = resized > float(resized.mean())
    packed = np.packbits(binary.astype(np.uint8)).tobytes()
    return hashlib.sha256(packed).hexdigest()[:24]

def _scaled_bbox(bbox: tuple[int, int, int, int], scale: tuple[float, float]) -> list[int]:
    x1, y1, x2, y2 = bbox
    sx, sy = scale
    return [round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy)]

def _parse_row(normalized: np.ndarray, bbox: tuple[int, int, int, int], *, source_page: Path, page_index: int, row_index: int, source_scale: tuple[float, float], output_dir: Path | None, ocr_backend: str, runtime: TesseractRuntime | None) -> dict[str, Any]:
    x1, y1, x2, y2 = bbox
    row = normalized[y1:y2, x1:x2]
    row_height = row.shape[0]
    top_partial = y1 == LIST_VIEWPORT[1] and row_height < ROW_HEIGHT
    name_crop = row[0:min(50, row_height), 100:470]
    description_crop = row[0:row_height, 100:470] if top_partial else row[45:min(132, row_height), 100:470]
    name_result = {'text': None, 'confidence': 0.0, 'accepted': False, 'candidates': []} if top_partial else _read_name(name_crop, ocr_backend, runtime)
    description_result = _read_description(description_crop, ocr_backend, runtime)
    acquired = _pink_ratio(row[:, 490:650]) > 0.01
    plus_available = not acquired and _green_ratio(row[:, 590:650]) > 0.01
    cost_evidence = {'value': None, 'source': 'not_applicable', 'confidence': None, 'rapidocr_confidence': None, 'tesseract_confidence': None} if acquired else _read_cost_evidence(row, top_partial=top_partial, runtime=runtime, backend=ocr_backend)
    cost = cost_evidence['value']
    hint_level, discount_percent, hint_ocr_raw = _read_hint(row, runtime=runtime, backend=ocr_backend)
    rarity, frame = _row_frame(row, acquired)
    minus_state = 'not_applicable' if acquired else 'available' if _green_ratio(row[:, 465:525]) > 0.01 else 'disabled'
    icon_crop_relative: str | None = None
    icon = row[35:min(125, row_height), 12:98]
    if output_dir is not None and icon.size:
        icon_dir = output_dir / 'icons'
        icon_dir.mkdir(parents=True, exist_ok=True)
        icon_name = f'page_{page_index:02d}_row_{row_index:02d}_icon.png'
        icon_path = icon_dir / icon_name
        if not write_image(str(icon_path), icon):
            raise OSError(f'failed to write icon crop: {icon_path}')
        icon_crop_relative = str(Path('icons') / icon_name).replace('\\', '/')
    unknown_reason: list[str] = []
    if ocr_backend == 'none':
        unknown_reason.extend(['name_ocr_unavailable', 'description_ocr_unavailable'])
    else:
        if top_partial:
            unknown_reason.append('name_not_visible_in_top_partial')
        elif name_result['text'] is None:
            unknown_reason.append('name_low_confidence_requires_review' if name_result['candidates'] else 'name_ocr_empty')
        elif name_result['confidence'] < 0.75:
            unknown_reason.append('name_low_confidence_requires_review')
        if description_result['text'] is None:
            unknown_reason.append('description_ocr_empty')
        elif description_result['confidence'] < 0.6:
            unknown_reason.append('description_low_confidence_requires_review')
    if not acquired and cost is None:
        unknown_reason.append('cost_unreadable')
    if rarity == 'unknown':
        unknown_reason.append('rarity_acquired_frame_ambiguous')
    if row_height < ROW_HEIGHT:
        unknown_reason.append('row_partially_visible')
    effective_name_confidence = name_result['confidence'] if name_result['accepted'] else 0.0
    field_confidences = [0.95, effective_name_confidence, description_result['confidence']]
    if acquired or cost is not None:
        field_confidences.append(0.95)
    if hint_level is not None:
        field_confidences.append(0.9)
    confidence = round(sum(field_confidences) / len(field_confidences), 4)
    scaled_bbox = _scaled_bbox(bbox, source_scale)
    evidence = {'source_page': str(source_page), 'page_index': page_index, 'row_index': row_index, 'row_bbox': scaled_bbox, 'normalized_row_bbox': list(bbox), 'icon_crop': icon_crop_relative, 'cost_evidence': dict(cost_evidence), 'name_ocr_candidates': name_result['candidates'], 'description_ocr_raw': description_result['raw'], 'hint_ocr_raw': hint_ocr_raw}
    return {'name': name_result['text'], 'description': description_result['text'], 'rarity': rarity, 'frame': frame, 'acquired': acquired, 'hint_level': hint_level, 'discount_percent': discount_percent, 'cost': cost, 'cost_evidence': cost_evidence, 'plus_available': plus_available, 'minus_state': minus_state, 'icon_crop': icon_crop_relative, 'source_pages': [str(source_page)], 'source_evidence': [evidence], 'confidence': confidence, 'field_confidence': {'structure': 0.95, 'name': round(effective_name_confidence, 4), 'description': description_result['confidence'], 'cost': cost_evidence['confidence'] if cost_evidence['source'] in {'tesseract_fallback', 'tesseract_crosscheck'} else 0.95 if acquired or cost is not None else 0.0, 'hint': 0.9 if hint_level is not None else None}, 'unknown_reason': unknown_reason, 'row_fingerprint': _perceptual_fingerprint(row), 'ocr_raw': {'name_candidates': name_result['candidates'], 'description': description_result['raw'], 'hint': hint_ocr_raw}}

def _dedupe_key(entry: dict[str, Any]) -> tuple[str, str]:
    name = entry.get('name')
    if name:
        normalized_name = re.sub('[^0-9A-Za-z\\u3400-\\u9fff]+', '', str(name)).lower()
        if normalized_name:
            return ('name', normalized_name)
    return ('fingerprint', str(entry.get('row_fingerprint', '')))

def _semantic_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub('\\s+', '', value)
    return text or None

def _continuation_entries_match(previous_entry: dict[str, Any], current_entry: dict[str, Any]) -> bool:
    """Require explicit identity before joining two visible fragments."""
    previous_name = _semantic_text(previous_entry.get('name'))
    current_name = _semantic_text(current_entry.get('name'))
    previous_description = _semantic_text(previous_entry.get('description'))
    current_description = _semantic_text(current_entry.get('description'))
    return previous_name is not None and current_name is not None and (previous_description is not None) and (current_description is not None) and (previous_name == current_name) and (previous_description == current_description)

def _entry_has_complete_row(entry: dict[str, Any]) -> bool:
    for evidence in entry.get('source_evidence', []):
        bbox = evidence.get('normalized_row_bbox')
        if not evidence.get('fragment_only') and isinstance(bbox, list) and (len(bbox) == 4) and all((type(value) is int for value in bbox)) and (bbox[3] - bbox[1] >= ROW_HEIGHT):
            return True
    return False

def _entry_field_value(entry: dict[str, Any], field: str) -> Any:
    value = entry.get(field)
    if field in {'name', 'description'}:
        return _semantic_text(value)
    return value

def _partial_agrees_with_complete(partial: dict[str, Any], complete: dict[str, Any]) -> bool:
    for field in ('name', 'description', 'cost', 'hint_level', 'discount_percent'):
        partial_value = _entry_field_value(partial, field)
        if partial_value is not None and partial_value != _entry_field_value(complete, field):
            return False
    return all((partial.get(field) == complete.get(field) for field in ('plus_available', 'minus_state', 'acquired')))

def _complete_entries_agree(entries: list[dict[str, Any]]) -> bool:
    canonical = entries[0]
    return all((_partial_agrees_with_complete(entry, canonical) and _partial_agrees_with_complete(canonical, entry) for entry in entries[1:]))

def deduplicate_skill_entries(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate rows while retaining every source evidence item."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for raw_entry in entries:
        entry = copy.deepcopy(raw_entry)
        key = _dedupe_key(entry)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(entry)
    merged: list[dict[str, Any]] = []
    for key in order:
        group = grouped[key]
        complete_entries = [entry for entry in group if _entry_has_complete_row(entry)]
        partial_entries = [entry for entry in group if not _entry_has_complete_row(entry)]
        complete_corroborates_partials = bool(complete_entries) and bool(partial_entries) and _complete_entries_agree(complete_entries) and all((any((_partial_agrees_with_complete(partial, complete) for complete in complete_entries)) for partial in partial_entries))
        representative_pool = complete_entries if complete_corroborates_partials else group
        representative = max(representative_pool, key=lambda item: float(item.get('confidence', 0.0) or 0.0))
        combined = copy.deepcopy(representative)
        combined['source_pages'] = list(dict.fromkeys((page for entry in group for page in entry.get('source_pages', []))))
        combined['source_evidence'] = [evidence for entry in group for evidence in entry.get('source_evidence', [])]
        reasons = list(dict.fromkeys((reason for entry in group for reason in entry.get('unknown_reason', []))))
        if complete_corroborates_partials:
            reasons = [reason for reason in reasons if reason != 'row_partially_visible']
            if type(representative.get('cost')) is int and representative['cost'] > 0 and all(('cost_unreadable' not in entry.get('unknown_reason', []) for entry in complete_entries)):
                reasons = [reason for reason in reasons if reason != 'cost_unreadable']
        combined['unknown_reason'] = reasons
        merged.append(combined)
    return merged

def _has_current_run_multi_evidence_binding(skill: dict[str, Any], resolution: Any) -> bool:
    if not isinstance(resolution, dict):
        return False
    candidates = resolution.get('candidates')
    if not isinstance(candidates, list) or not candidates:
        return False
    top = candidates[0]
    if not isinstance(top, dict) or top.get('skill_id') != skill.get('skill_id'):
        return False
    evidence = top.get('evidence')
    contract = top.get('current_run_contract')
    if not isinstance(evidence, dict) or not isinstance(contract, dict):
        return False
    return resolution.get('status') == 'auto_confirmed' and resolution.get('binding_policy') == 'current_run_multi_evidence' and (float(resolution.get('confidence', 0.0) or 0.0) >= 0.7) and (int(resolution.get('evidence_dimensions', 0) or 0) >= 3) and (not resolution.get('unknown_reason')) and (not top.get('hard_conflicts')) and (evidence.get('cost') == 1.0) and (evidence.get('rarity') == 1.0) and (evidence.get('icon_color') == 1.0) and (contract.get('version') == 'current_run_multi_evidence.v1') and isinstance(contract.get('local_db_id'), str) and contract['local_db_id'].isdigit() and (contract.get('identity_status') in {'manual_verified', 'exact_local_db_id_join'}) and (contract.get('metadata_status') in {'manual_verified', 'external_cross_checked'}) and (contract.get('description_anchor_exact') is True or contract.get('name_score_threshold_met') is True) and ('row_partially_visible' not in set(skill.get('unknown_reason') or []))

def build_skill_planner_observation(manifest: dict[str, Any], *, name_to_skill_id: dict[str, str], min_confidence: float=0.75) -> dict[str, Any]:
    """Convert reviewed recognition output into planner-safe observations.

    Exact, externally supplied name mappings are mandatory.  This adapter does
    not fuzzy-match OCR text and never creates a purchase request.
    """
    available_skills: list[dict[str, Any]] = []
    owned_skills: list[str] = []
    observed_hint_levels: dict[str, int] = {}
    skipped: list[dict[str, Any]] = []
    for skill in manifest.get('skills', []):
        name = skill.get('name')
        evidence = skill.get('source_evidence', [])
        if not name:
            skipped.append({'name': None, 'reason': 'name_unknown', 'source_evidence': evidence})
            continue
        skill_id = name_to_skill_id.get(str(name))
        if not skill_id:
            skipped.append({'name': name, 'reason': 'skill_id_unmapped', 'source_evidence': evidence})
            continue
        field_confidence = dict(skill.get('field_confidence', {}))
        name_confidence = float(field_confidence.get('name', 0.0) or 0.0)
        resolution = skill.get('name_resolution')
        strong_resolution = isinstance(resolution, dict) and resolution.get('status') == 'auto_confirmed' and (float(resolution.get('confidence', 0.0) or 0.0) >= min_confidence) and (float(resolution.get('margin', 0.0) or 0.0) >= 0.1) and (int(resolution.get('evidence_dimensions', 0) or 0) >= 3)
        current_run_resolution = _has_current_run_multi_evidence_binding(skill, resolution)
        trusted_resolution = strong_resolution or current_run_resolution
        if not trusted_resolution and name_confidence < min_confidence:
            skipped.append({'name': name, 'skill_id': skill_id, 'reason': 'name_confidence_low', 'source_evidence': evidence})
            continue
        if 'row_partially_visible' in skill.get('unknown_reason', []):
            skipped.append({'name': name, 'skill_id': skill_id, 'reason': 'partial_row_not_planner_safe', 'source_evidence': evidence})
            continue
        if trusted_resolution:
            structure_confidence = float(field_confidence.get('structure', 1.0) or 0.0)
            cost_confidence = float(field_confidence.get('cost', 0.0) or 0.0)
            confidence = min(structure_confidence, cost_confidence)
            if not current_run_resolution:
                confidence = min(float(resolution['confidence']), confidence)
        else:
            confidence = float(skill.get('confidence', 0.0))
        if confidence < min_confidence:
            skipped.append({'name': name, 'skill_id': skill_id, 'reason': 'recognition_confidence_low', 'source_evidence': evidence})
            continue
        if skill.get('acquired'):
            if skill_id not in owned_skills:
                owned_skills.append(skill_id)
            continue
        cost = skill.get('cost')
        if cost is None:
            skipped.append({'name': name, 'skill_id': skill_id, 'reason': 'display_cost_unknown', 'source_evidence': evidence})
            continue
        hint_level = skill.get('hint_level')
        available_skills.append({'skill_id': skill_id, 'display_cost': int(cost), 'recognition_confidence': confidence, 'cost_basis': 'observed_final', 'observed_hint_level': hint_level, 'observed_discount_percent': skill.get('discount_percent'), 'source_evidence': evidence, **({'identity_binding': {'status': resolution['status'], 'confidence': float(resolution['confidence']), 'margin': float(resolution['margin']), 'evidence_dimensions': int(resolution['evidence_dimensions']), 'binding_policy': resolution.get('binding_policy'), **({'evidence': dict(resolution['candidates'][0]['evidence'])} if isinstance(resolution.get('candidates'), list) and resolution['candidates'] and isinstance(resolution['candidates'][0], dict) and isinstance(resolution['candidates'][0].get('evidence'), dict) else {}), **({'current_run_contract': dict(resolution['candidates'][0]['current_run_contract'])} if current_run_resolution else {})}} if trusted_resolution else {})})
        if hint_level is not None:
            observed_hint_levels[skill_id] = int(hint_level)
    return {'schema_version': 'uma.skill_planner_observation.v1', 'current_skill_pt': manifest.get('current_skill_points'), 'owned_skills': owned_skills, 'available_skills': available_skills, 'hint_levels': {}, 'observed_hint_levels': observed_hint_levels, 'pricing_contract': 'display_cost_is_observed_final; do_not_apply_hint_discount_again', 'skipped': skipped, 'purchase_execution_allowed': False}

def _write_review_pack(manifest: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    todo = {'schema_version': 'uma.skill_acquisition_truth_todo.v1', 'source_manifest': 'manifest.json', 'instructions': 'Confirm or correct OCR fields. Do not infer unknown skills without visual evidence.', 'items': [{'item_id': f'skill_{index:03d}', 'review_status': 'todo', 'predicted': {key: skill.get(key) for key in ('name', 'description', 'rarity', 'acquired', 'hint_level', 'discount_percent', 'cost')}, 'confirmed': None, 'source_evidence': skill.get('source_evidence', []), 'unknown_reason': skill.get('unknown_reason', [])} for index, skill in enumerate(manifest['skills'])]}
    (output_dir / 'truth.todo.json').write_text(json.dumps(todo, ensure_ascii=False, indent=2), encoding='utf-8')

def parse_skill_acquisition_pages(page_paths: Iterable[str | Path], *, output_dir: str | Path | None=None, ocr_backend: str='auto', assume_continuous_scroll: bool=False, tessdata_dir: str | Path | None=None) -> dict[str, Any]:
    """Parse and deduplicate one or more skill-acquisition screenshots."""
    effective_backend, rapidocr_available = resolve_ocr_backend(ocr_backend)
    paths = [Path(path) for path in page_paths]
    if not paths:
        raise ValueError('at least one skill acquisition page is required')
    runtime = None if effective_backend == 'rapidocr' else resolve_tesseract_runtime(tessdata_dir=tessdata_dir)
    destination = Path(output_dir) if output_dir is not None else None
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
    page_results: list[dict[str, Any]] = []
    all_entries: list[dict[str, Any]] = []
    point_values: list[int] = []
    warnings: list[str] = []
    continuation_pages: list[dict[str, Any]] = []
    for page_index, path in enumerate(paths):
        source = _read_image(path)
        normalized, source_scale = _normalize_image(source)
        current_points = read_current_skill_points(normalized, runtime=runtime, backend=effective_backend)
        if current_points is not None:
            point_values.append(current_points)
        else:
            warnings.append(f'current_skill_points_unreadable:{path}')
        layout = _detect_skill_row_layout(normalized)
        row_boxes = layout['usable_boxes']
        page_entries = [_parse_row(normalized, bbox, source_page=path, page_index=page_index, row_index=row_index, source_scale=source_scale, output_dir=destination, ocr_backend=effective_backend, runtime=runtime) for row_index, bbox in enumerate(row_boxes)]
        all_entries.extend(page_entries)
        entry_by_bbox = {tuple(entry['source_evidence'][0]['normalized_row_bbox']): entry for entry in page_entries}
        continuation_pages.append({'path': path, 'page_index': page_index, 'scale': source_scale, 'top_fragment': layout['top_fragment'], 'bottom_fragment': layout['bottom_fragment'], 'top_entry': entry_by_bbox.get(tuple(layout['top_fragment'])) if layout['top_fragment'] else None, 'bottom_entry': entry_by_bbox.get(tuple(layout['bottom_fragment'])) if layout['bottom_fragment'] else None})
        page_results.append({'source_page': str(path), 'width': int(source.shape[1]), 'height': int(source.shape[0]), 'current_skill_points': current_points, 'detected_usable_rows': len(page_entries), 'row_bboxes': [_scaled_bbox(box, source_scale) for box in row_boxes], 'continuation_fragments': {key: _scaled_bbox(layout[key], source_scale) if layout[key] else None for key in ('top_fragment', 'bottom_fragment')}})
    if assume_continuous_scroll:
        for previous, current in zip(continuation_pages, continuation_pages[1:]):
            previous_fragment = previous['bottom_fragment']
            current_fragment = current['top_fragment']
            if previous_fragment is None or current_fragment is None:
                continue
            previous_entry = previous['bottom_entry']
            current_entry = current['top_entry']
            fragment_evidence = {'source_page': str(current['path']), 'page_index': current['page_index'], 'row_index': None, 'row_bbox': _scaled_bbox(current_fragment, current['scale']), 'normalized_row_bbox': list(current_fragment), 'icon_crop': None, 'fragment_only': True, 'continuation_position': 'top'}
            if previous_entry is not None and current_entry is not None and _continuation_entries_match(previous_entry, current_entry):
                previous_entry['source_pages'] = list(dict.fromkeys(previous_entry['source_pages'] + [str(current['path'])]))
                previous_entry['source_evidence'].append(fragment_evidence)
    point_counts = Counter(point_values)
    current_skill_points = point_counts.most_common(1)[0][0] if point_counts else None
    if len(point_counts) > 1:
        warnings.append('current_skill_points_inconsistent_across_pages')
    skills = deduplicate_skill_entries(all_entries)
    manifest = {'schema_version': SCHEMA_VERSION, 'parser_mode': 'offline_review_only', 'ocr_backend': effective_backend, 'requested_ocr_backend': ocr_backend, 'ocr_runtime': {'executable': str(runtime.executable) if runtime is not None else None, 'tessdata_dir': str(runtime.tessdata_dir) if runtime is not None else None, 'available_languages': sorted(runtime.available_languages) if runtime is not None else [], 'missing_requested_languages': [language for language in ('chi_sim', 'eng') if runtime is None or language not in runtime.available_languages], 'tesseract': None if runtime is None else {'executable': str(runtime.executable), 'tessdata_dir': str(runtime.tessdata_dir)}, 'rapidocr': {'available': rapidocr_available, 'used_for_names': effective_backend == 'rapidocr'}}, 'assume_continuous_scroll': assume_continuous_scroll, 'current_skill_points': current_skill_points, 'pages': page_results, 'skills': skills, 'warnings': warnings, 'planner_interface': {'available_skills_source': 'skills', 'adapter': 'build_skill_planner_observation', 'requires_manual_name_to_skill_id_mapping': True, 'purchase_execution_allowed': False}}
    if destination is not None:
        _write_review_pack(manifest, destination)
    return manifest
__all__ = ['LIST_VIEWPORT', 'SCHEMA_VERSION', 'SUPPORTED_OCR_BACKENDS', 'SkillAcquisitionOcrRuntimeError', 'TesseractRuntime', 'build_skill_planner_observation', 'deduplicate_skill_entries', 'detect_skill_row_boxes', 'inspect_skill_acquisition_page', 'parse_hint_ocr_text', 'parse_skill_acquisition_pages', 'rapidocr_available', 'read_current_skill_points', 'resolve_ocr_backend', 'resolve_tesseract_runtime']
