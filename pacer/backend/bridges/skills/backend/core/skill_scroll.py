"""One bounded, overlapping skill-list gesture shared by capture and selection."""
import cv2
import numpy as np
SKILL_OVERLAP_SWIPE = {'name': 'row-overlap-hold', 'start': [360, 980], 'end': [360, 490], 'duration_ms': 120, 'hold_ms': 240, 'start_nudge_px': 12, 'release_settle_ms': 60}
_SCROLL_STEP_FRACTIONS = (0.22, 0.44, 0.64, 0.80, 0.92, 1.00)

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
    if type(reverse) is not bool or type(travel_px) is not int or (not 12 <= travel_px <= 490) or (not reverse and travel_px != 490 and (not 12 <= travel_px <= 45)):
        raise ValueError('skill_overlap_gesture_invalid')
    if reverse:
        x, y, end_x, end_y = (end_x, end_y, x, end_y + travel_px)
    elif travel_px != 490:
        end_y = y - travel_px
    return _held_scroll_shell(x, y, end_x, end_y)

MAX_SKILL_NUDGE_PX = 220


def skill_nudge_shell(*, reverse=False, travel_px=140):
    """Micro-nudge the list smoothly to uncover an edge-clipped row."""
    if type(reverse) is not bool or type(travel_px) is not int or (not 12 <= travel_px <= MAX_SKILL_NUDGE_PX):
        raise ValueError('skill_nudge_gesture_invalid')
    if reverse:
        return _held_scroll_shell(360, 520, 360, 520 + travel_px)
    return _held_scroll_shell(360, 940, 360, 940 - travel_px)


def skill_rewind_shell():
    """Rewind at the observer's existing coordinates, with a held release."""
    return _held_scroll_shell(360, 610, 360, 900)

def _held_scroll_shell(x, y, end_x, end_y):
    nudge = SKILL_OVERLAP_SWIPE['start_nudge_px'] * (1 if end_y > y else -1)
    release = f'input touchscreen motionevent UP {end_x} {end_y}'
    commands = ['set -e', f"trap '{release}' EXIT", f'input touchscreen motionevent DOWN {x} {y}', f'input touchscreen motionevent MOVE {x} {y + nudge}']
    step_sleep = SKILL_OVERLAP_SWIPE['duration_ms'] / (1000 * len(_SCROLL_STEP_FRACTIONS))
    for frac in _SCROLL_STEP_FRACTIONS:
        commands.append(f'sleep {step_sleep:.3f}')
        commands.append(f'input touchscreen motionevent MOVE {x} {round(y + (end_y - y) * frac)}')
    commands.extend([f"sleep {SKILL_OVERLAP_SWIPE['hold_ms'] / 1000:.3f}", f'input touchscreen motionevent MOVE {end_x} {end_y}', f"sleep {SKILL_OVERLAP_SWIPE['release_settle_ms'] / 1000:.3f}", release, 'trap - EXIT'])
    return '; '.join(commands)

MAX_SKILL_OVERLAP_SHIFT = 450


def measure_skill_overlap(before, after):
    """Verify actual text translation, not the distance of the input gesture."""
    if before is None or after is None or before.shape != (1280, 720, 3) or (after.shape != before.shape):
        return dict(verified=False, reason='skill_scroll_image_invalid')
    before = cv2.cvtColor(before[468:1029, 40:520], cv2.COLOR_BGR2GRAY)
    after = cv2.cvtColor(after[468:1029, 40:520], cv2.COLOR_BGR2GRAY)
    before_ink, after_ink = (before[:, 110:] < 125, after[:, 110:] < 125)

    def agreement(shift):
        shift = round(float(shift))
        if shift <= 0 or shift >= before_ink.shape[0]:
            return 0.0
        pad = 6
        if before_ink.shape[0] - shift > 2 * pad + 32:
            a, b = (before_ink[shift + pad:-pad], after_ink[pad:-shift - pad])
            if min(a.sum(), b.sum()) < 100:
                a, b = (before_ink[shift:], after_ink[:-shift])
        else:
            a, b = (before_ink[shift:], after_ink[:-shift])
        if min(a.sum(), b.sum()) < 100:
            return 0.0
        kernel = np.ones((3, 3), np.uint8)
        near_a = cv2.dilate(a.astype(np.uint8), kernel).astype(bool)
        near_b = cv2.dilate(b.astype(np.uint8), kernel).astype(bool)
        return min(float((a & near_b).sum() / a.sum()), float((b & near_a).sum() / b.sum()))

    search_band = before[:, 110:]
    template_band = after[:, 110:]
    proposals: list[tuple[int, float]] = []
    for y0 in (20, 120, 220, 320):
        if y0 + 100 > template_band.shape[0] or after_ink[y0:y0 + 100].sum() < 50:
            continue
        res = cv2.matchTemplate(search_band, template_band[y0:y0 + 100], cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        cand_shift = int(max_loc[1]) - y0
        if max_val >= 0.75 and 1 <= cand_shift <= MAX_SKILL_OVERLAP_SHIFT:
            proposals.append((cand_shift, float(max_val)))
    if proposals:
        candidate_shifts = sorted({
            cand + delta
            for cand, _ in proposals
            for delta in (-1, 0, 1)
            if 1 <= cand + delta <= MAX_SKILL_OVERLAP_SHIFT
        })
        if candidate_shifts:
            winner = max(candidate_shifts, key=agreement)
            score = agreement(winner)
            strong_support = (
                any(val >= 0.85 and abs(cand - winner) <= 2 for cand, val in proposals)
                or sum(1 for cand, _ in proposals if abs(cand - winner) <= 2) >= 2
            )
            if score >= 0.95 and strong_support:
                max_corr = max(val for cand, val in proposals if abs(cand - winner) <= 2)
                return dict(
                    verified=True,
                    shift=round(float(winner), 2),
                    overlap=round(561 - float(winner), 2),
                    inliers=max(10, int(round(max_corr * 100))),
                    text_agreement=round(score, 4),
                )

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
    candidates = sorted({round(float(value)) for value in shifts})
    winner = max(candidates, key=agreement)
    inliers = shifts[abs(shifts - winner) < 2]
    shift = float(np.median(inliers))
    score = agreement(shift)
    verified = 1 <= shift <= MAX_SKILL_OVERLAP_SHIFT and len(inliers) >= 10 and (score >= 0.95)
    return dict(verified=verified, shift=round(shift, 2), overlap=round(561 - shift, 2), inliers=len(inliers), text_agreement=round(score, 4))
