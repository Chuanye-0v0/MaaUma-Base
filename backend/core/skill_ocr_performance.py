"""Skill-operation-local OCR reuse; never supplies state or action authority."""
from contextlib import contextmanager
from contextvars import ContextVar
import csv
import copy
import hashlib
import io
import os
import subprocess
import cv2
_ACTIVE = ContextVar('harvest_skill_ocr', default=None)

@contextmanager
def harvest_skill_ocr():
    existing = _ACTIVE.get()
    if existing is not None:
        yield existing
        return
    state = dict(cache={}, engine=None, rapid_calls=0, cache_hits=0, tesseract_calls=0)
    token = _ACTIVE.set(state)
    try:
        yield state
    finally:
        _ACTIVE.reset(token)

def active_harvest_ocr():
    return _ACTIVE.get()

def tesseract_stdin(image, runtime, languages, psm, whitelist):
    """Avoid pytesseract's full shared-TEMP glob on every recognized field."""
    state = active_harvest_ocr()
    key = ('tesseract', str(runtime.executable), str(runtime.tessdata_dir), languages, psm, whitelist, image.shape, str(image.dtype), hashlib.sha256(image.tobytes()).digest()) if state is not None else None
    if state is not None and key in state['cache']:
        state['cache_hits'] += 1
        return copy.deepcopy(state['cache'][key])
    encoded, png = cv2.imencode('.png', image)
    if not encoded:
        raise ValueError('ocr_image_encode_failed')
    command = [str(runtime.executable), 'stdin', 'stdout', '--tessdata-dir', str(runtime.tessdata_dir), '-l', languages, '--psm', str(psm)]
    if whitelist:
        command += ['-c', 'tessedit_char_whitelist=' + whitelist]
    command += ['-c', 'tessedit_create_tsv=1']
    if state is not None:
        state['tesseract_calls'] += 1
    result = subprocess.run(command, input=png.tobytes(), capture_output=True, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    rows = list(csv.DictReader(io.StringIO(result.stdout.decode('utf-8')), delimiter='\t'))
    data = {field: [row[field] for row in rows] for field in ('text', 'conf')}
    if state is not None:
        if len(state['cache']) >= 2048:
            state['cache'].pop(next(iter(state['cache'])))
        state['cache'][key] = copy.deepcopy(data)
    return data
