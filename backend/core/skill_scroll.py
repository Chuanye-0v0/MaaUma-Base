"""One bounded, overlapping skill-list gesture shared by capture and selection."""
import cv2
import numpy as np
SKILL_OVERLAP_SWIPE = {'name': 'row-overlap-hold', 'start': [360, 980], 'end': [360, 490], 'duration_ms': 300, 'hold_ms': 450, 'start_nudge_px': 12, 'release_settle_ms': 100}

def skill_list_frames_match(source, current):
    """Keep rows stable; animated footer buttons are not list identity.

    Callers separately require the skills page, current Pt and authorized
    controls/transaction evidence. Do not use this pixel comparison alone to
    authorize a purchase or a no-purchase return.
    """
    if source is None or current is None or source.shape != (1280, 720, 3) or (current.shape != source.shape):
        return False
    return bool(cv2.absdiff(source[470:1026, 30:690], current[470:1026, 30:690]).max() <= 10)

def skill_overlap_shell(*, reverse=False, travel_px=490):
    """Keep the touch down at the endpoint before releasing, in one ADB call."""
    x, y = SKILL_OVERLAP_SWIPE['start']
    end_x, end_y = SKILL_OVERLAP_SWIPE['end']
    if type(reverse) is not bool or type(travel_px) is not int or (not 12 <= travel_px <= 490) or (not reverse and travel_px != 490 and (not 12 <= travel_px <= 40)):
        raise ValueError('skill_overlap_gesture_invalid')
    if reverse:
        x, y, end_x, end_y = (end_x, end_y, x, end_y + travel_px)
    elif travel_px != 490:
        end_y = y - travel_px
    return _held_scroll_shell(x, y, end_x, end_y)

def skill_rewind_shell():
    """Rewind at the observer's existing coordinates, with a held release."""
    return _held_scroll_shell(360, 610, 360, 900)

def _held_scroll_shell(x, y, end_x, end_y):
    nudge = SKILL_OVERLAP_SWIPE['start_nudge_px'] * (1 if end_y > y else -1)
    release = f'input touchscreen motionevent UP {end_x} {end_y}'
    commands = ['set -e', f"trap '{release}' EXIT", f'input touchscreen motionevent DOWN {x} {y}', f'input touchscreen motionevent MOVE {x} {y + nudge}']
    for step in range(1, 5):
        commands.append(f"sleep {SKILL_OVERLAP_SWIPE['duration_ms'] / 4000:.3f}")
        commands.append(f'input touchscreen motionevent MOVE {x} {round(y + (end_y - y) * step / 4)}')
    commands.extend([f"sleep {SKILL_OVERLAP_SWIPE['hold_ms'] / 1000:.3f}", f'input touchscreen motionevent MOVE {end_x} {end_y}', f"sleep {SKILL_OVERLAP_SWIPE['release_settle_ms'] / 1000:.3f}", release, 'trap - EXIT'])
    return '; '.join(commands)

def measure_skill_overlap(before, after):
    """Verify actual text translation, not the distance of the input gesture."""
    if before is None or after is None or before.shape != (1280, 720, 3) or (after.shape != before.shape):
        return dict(verified=False, reason='skill_scroll_image_invalid')
    before = cv2.cvtColor(before[468:1029, 40:520], cv2.COLOR_BGR2GRAY)
    after = cv2.cvtColor(after[468:1029, 40:520], cv2.COLOR_BGR2GRAY)
    detector = cv2.SIFT_create(nfeatures=1800)
    key_before, desc_before = detector.detectAndCompute(before, None)
    key_after, desc_after = detector.detectAndCompute(after, None)
    if desc_before is None or desc_after is None:
        return dict(verified=False, reason='skill_scroll_features_missing')
    matches = cv2.BFMatcher().knnMatch(desc_before, desc_after, k=2)
    offsets = [np.array(key_before[a.queryIdx].pt) - np.array(key_after[a.trainIdx].pt) for pair in matches if len(pair) == 2 for a, b in [pair] if a.distance < 0.65 * b.distance]
    shifts = np.array([dy for dx, dy in offsets if abs(dx) < 2 and 1 <= dy < 561])
    if not len(shifts):
        return dict(verified=False, reason='skill_scroll_translation_missing')
    before_ink, after_ink = (before[:, 110:] < 125, after[:, 110:] < 125)

    def agreement(shift):
        shift = round(float(shift))
        a, b = (before_ink[shift:], after_ink[:-shift])
        if min(a.sum(), b.sum()) < 100:
            return 0.0
        kernel = np.ones((3, 3), np.uint8)
        near_a = cv2.dilate(a.astype(np.uint8), kernel).astype(bool)
        near_b = cv2.dilate(b.astype(np.uint8), kernel).astype(bool)
        return min(float((a & near_b).sum() / a.sum()), float((b & near_a).sum() / b.sum()))
    candidates = sorted({round(float(value)) for value in shifts})
    winner = max(candidates, key=agreement)
    inliers = shifts[abs(shifts - winner) < 2]
    shift = float(np.median(inliers))
    score = agreement(shift)
    verified = 1 <= shift <= 386 and len(inliers) >= 10 and (score >= 0.95)
    return dict(verified=verified, shift=round(shift, 2), overlap=round(561 - shift, 2), inliers=len(inliers), text_agreement=round(score, 4))
