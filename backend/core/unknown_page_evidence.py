"""Local, bounded-by-capture-retention evidence for unrecognized login/navigation pages."""
import hashlib
import json
from pathlib import Path
import time
import cv2

def save_unknown_page(device, image, observed):
    if observed.get('page') != 'unknown':
        return
    from core.screenshot_retention import daily_no_archive_active
    if daily_no_archive_active():
        try:
            value = observed.get('screenshot_path') or observed.get('screenshot')
            path = Path(value) if isinstance(value, str) else None
            if path is not None and path.stem.isdigit() and (path.suffix.lower() == '.png') and (not path.is_symlink()) and (path.resolve().parent == Path(device.output_dir).resolve()) and path.is_file():
                height, width = image.shape[:2]
                region = [round(width * 20 / 720), round(height * 250 / 1280), round(width * 700 / 720), round(height * 1050 / 1280)]
                masked = image.copy()
                x0, y0, x1, y1 = region
                masked[y0:y1, x0:x1] = 0
                if cv2.imwrite(str(path), masked):
                    observed.update(screenshot_redacted=True, screenshot_redacted_regions=[region], screenshot_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        except (OSError, ValueError, cv2.error):
            observed['screenshot_diagnostic_error'] = 'unknown_page_evidence_save_failed'
        observed['screenshot_archive'] = 'disabled_daily'
        return
    try:
        root = Path(device.output_dir)
        root.mkdir(parents=True, exist_ok=True)
        source_hash = observed.get('frame_sha256')
        cached = getattr(device, '_unknown_page_evidence', None)
        if cached and cached['frame_sha256'] == source_hash and Path(cached['screenshot_path']).is_file():
            observed.update(cached)
            return
        height, width = image.shape[:2]
        region = [round(width * 20 / 720), round(height * 250 / 1280), round(width * 700 / 720), round(height * 1050 / 1280)]
        masked = image.copy()
        x0, y0, x1, y1 = region
        masked[y0:y1, x0:x1] = 0
        path = root / (str(time.time_ns()) + '-unknown.png')
        if not cv2.imwrite(str(path), masked):
            raise OSError('unknown_page_image_not_saved')
        fields = dict(frame_sha256=source_hash, screenshot_path=str(path), screenshot_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), screenshot_redacted=True, screenshot_redacted_regions=[region], screenshot_size=[width, height])
        path.with_suffix('.json').write_text(json.dumps(dict(fields, page='unknown', endpoint=device.endpoint, captured_monotonic=observed.get('captured_monotonic'), captured_at_unix_ms=int(time.time() * 1000), actions=list(observed.get('actions', {}))), ensure_ascii=False, indent=2), encoding='utf8')
        device._unknown_page_evidence = fields
        observed.update(fields)
    except (OSError, cv2.error):
        observed['screenshot_diagnostic_error'] = 'unknown_page_evidence_save_failed'
