"""Anti-noise game-font glyph correlation kernel for failure-rate pills.

Pure NumPy + OpenCV implementation with zero external runtime dependencies.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np


GRID_W = 24
GRID_H = 32


def _lines_to_u8(lines: list[str]) -> np.ndarray:
    return np.array([[1 if ch == "1" else 0 for ch in row] for row in lines], dtype=np.uint8)


def _zncc_normalize(arr: np.ndarray) -> np.ndarray:
    f = arr.astype(np.float32)
    mean = f.mean(axis=(-2, -1), keepdims=True)
    centered = f - mean
    norm = np.sqrt((centered * centered).sum(axis=(-2, -1), keepdims=True))
    return np.where(norm > 1e-5, centered / np.maximum(norm, 1e-5), 0.0)


@lru_cache(maxsize=1)
def load_failure_glyph_bank(asset_path: str | None = None) -> dict[str, Any]:
    path = (
        Path(asset_path)
        if asset_path is not None
        else Path(__file__).with_name("assets") / "failure_rate_glyphs_v1.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    digit_labels: list[str] = []
    digit_aspects: list[float] = []
    box_list: list[np.ndarray] = []
    canvas_list: list[np.ndarray] = []
    for d in sorted(payload["digits"].keys()):
        for entry in payload["digits"][d]:
            digit_labels.append(d)
            digit_aspects.append(float(entry["aspect"]))
            box_list.append(_lines_to_u8(entry["box_24x32"]))
            canvas_list.append(_lines_to_u8(entry["canvas_24x32"]))

    pct_list = [_lines_to_u8(entry["box_24x32"]) for entry in payload.get("percent", [])]

    digit_box_u8 = np.stack(box_list, axis=0)
    digit_canvas_u8 = np.stack(canvas_list, axis=0)
    pct_box_u8 = np.stack(pct_list, axis=0) if pct_list else np.zeros((0, GRID_H, GRID_W), dtype=np.uint8)

    return {
        "digit_labels": digit_labels,
        "digit_aspects": np.array(digit_aspects, dtype=np.float32),
        "digit_box_u8": digit_box_u8,
        "digit_box_sum": digit_box_u8.sum(axis=(1, 2)).astype(np.float32),
        "digit_box_zncc": _zncc_normalize(digit_box_u8),
        "digit_canvas_u8": digit_canvas_u8,
        "digit_canvas_sum": digit_canvas_u8.sum(axis=(1, 2)).astype(np.float32),
        "pct_box_u8": pct_box_u8,
        "pct_box_sum": pct_box_u8.sum(axis=(1, 2)).astype(np.float32),
        "pct_box_zncc": _zncc_normalize(pct_box_u8) if len(pct_list) else np.zeros((0, GRID_H, GRID_W), dtype=np.float32),
    }


def _shifts_3x3(arr: np.ndarray) -> np.ndarray:
    padded = np.pad(arr, ((1, 1), (1, 1)), mode="constant", constant_values=0)
    return np.stack(
        [padded[dy : dy + GRID_H, dx : dx + GRID_W] for dy in range(3) for dx in range(3)],
        axis=0,
    )


def _normalize_crop_box(crop_u8: np.ndarray) -> np.ndarray:
    resized = cv2.resize(
        (crop_u8 > 0).astype(np.uint8) * 255,
        (GRID_W, GRID_H),
        interpolation=cv2.INTER_LINEAR,
    )
    return (resized >= 115).astype(np.uint8)


def _normalize_crop_canvas(crop_u8: np.ndarray) -> np.ndarray:
    h, w = crop_u8.shape[:2]
    target_h = 28
    target_w = max(2, min(GRID_W - 2, int(round(w * target_h / max(h, 1)))))
    resized = cv2.resize(
        (crop_u8 > 0).astype(np.uint8) * 255,
        (target_w, target_h),
        interpolation=cv2.INTER_LINEAR,
    )
    canvas = np.zeros((GRID_H, GRID_W), dtype=np.uint8)
    x0 = (GRID_W - target_w) // 2
    canvas[2 : 2 + target_h, x0 : x0 + target_w] = (resized >= 115).astype(np.uint8)
    return canvas


def classify_failure_digit_glyph(
    crop_u8: np.ndarray,
    *,
    gray_patch: np.ndarray | None = None,
    asset_path: str | None = None,
) -> tuple[str | None, float]:
    """Classify a single isolated failure-rate digit mask via glyph correlation."""
    h, w = crop_u8.shape[:2]
    if h < 8 or w < 3:
        return None, 0.0
    aspect = w / max(h, 1)
    fill = float((crop_u8 > 0).mean())
    # Thin vertical stroke remnants (aspect < 0.28) and wide non-digits are rejected
    if aspect < 0.28 or aspect > 1.26 or fill < 0.16:
        return None, 0.0

    bank = load_failure_glyph_bank(asset_path)
    q_box = _normalize_crop_box(crop_u8)
    q_canvas = _normalize_crop_canvas(crop_u8)

    if gray_patch is not None and gray_patch.shape == crop_u8.shape:
        g_resized = cv2.resize(gray_patch.astype(np.float32), (GRID_W, GRID_H), interpolation=cv2.INTER_LINEAR)
        g_min, g_max = float(g_resized.min()), float(g_resized.max())
        if g_max - g_min > 1e-4:
            g_norm = (g_resized - g_min) / (g_max - g_min)
        else:
            g_norm = q_box.astype(np.float32)
        q_float = 0.65 * q_box.astype(np.float32) + 0.35 * g_norm
    else:
        q_float = q_box.astype(np.float32)

    q_box_s = _shifts_3x3(q_box)
    q_canvas_s = _shifts_3x3(q_canvas)
    q_zncc_s = _zncc_normalize(_shifts_3x3(q_float))

    inter_box = (q_box_s[:, None] & bank["digit_box_u8"][None]).sum(axis=(2, 3)).astype(np.float32)
    tot_box = q_box_s.sum(axis=(1, 2))[:, None].astype(np.float32) + bank["digit_box_sum"][None]
    dice_box = (2.0 * inter_box / np.maximum(tot_box, 1.0)).max(axis=0)

    zncc_box = (q_zncc_s[:, None] * bank["digit_box_zncc"][None]).sum(axis=(2, 3)).max(axis=0)

    inter_can = (q_canvas_s[:, None] & bank["digit_canvas_u8"][None]).sum(axis=(2, 3)).astype(np.float32)
    tot_can = q_canvas_s.sum(axis=(1, 2))[:, None].astype(np.float32) + bank["digit_canvas_sum"][None]
    dice_can = (2.0 * inter_can / np.maximum(tot_can, 1.0)).max(axis=0)

    aspect_pen = np.maximum(0.0, np.abs(bank["digit_aspects"] - aspect) - 0.14) * 0.70
    proto_scores = 0.45 * dice_box + 0.32 * np.maximum(0.0, zncc_box) + 0.23 * dice_can - aspect_pen

    best_by_digit: dict[str, float] = {}
    best_dice_by_digit: dict[str, float] = {}
    for idx, d in enumerate(bank["digit_labels"]):
        sc = float(proto_scores[idx])
        dc = float(max(dice_box[idx], dice_can[idx]))
        if sc > best_by_digit.get(d, -1.0):
            best_by_digit[d] = sc
            best_dice_by_digit[d] = dc

    _, hierarchy = cv2.findContours((crop_u8 > 0).astype(np.uint8) * 255, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = sum(1 for item in hierarchy[0] if int(item[3]) != -1) if hierarchy is not None else 0

    ul = float(q_box[4:14, 2:10].mean())
    ur = float(q_box[4:14, 14:22].mean())
    mid = float(q_box[13:19, 5:19].mean())
    ll = float(q_box[18:28, 2:10].mean())
    lr = float(q_box[18:28, 14:22].mean())
    bot = float(q_box[25:31, 4:20].mean())

    if aspect > 0.60:
        best_by_digit["1"] = -1.0
    else:
        beak = float(q_box[3:10, 1:10].mean())
        low_left_empty = float(q_box[15:30, 0:9].mean())
        right_stem = float(q_box[6:29, 11:22].mean())
        if low_left_empty > 0.30 or beak < 0.15 or right_stem < 0.45:
            best_by_digit["1"] -= 0.30

    if mid > 0.52:
        best_by_digit["0"] -= 0.18
    elif mid < 0.28 and holes >= 1:
        best_by_digit["8"] -= 0.12

    if holes >= 2:
        best_by_digit["8"] += 0.05
        best_by_digit["9"] -= 0.06
        best_by_digit["3"] -= 0.08
    if ll < 0.30 and ur > 0.55 and lr > 0.55:
        best_by_digit["8"] -= 0.06
        best_by_digit["6"] -= 0.10
    if ur < 0.35 and ul > 0.50 and mid > 0.45:
        best_by_digit["8"] -= 0.08
        best_by_digit["9"] -= 0.08
        if ll > 0.55 or holes >= 1:
            best_by_digit["6"] += 0.04
        else:
            best_by_digit["5"] += 0.04
    if ul < 0.35 and ll < 0.38 and ur > 0.50 and lr > 0.50 and bot > 0.45:
        best_by_digit["3"] += 0.04
        best_by_digit["8"] -= 0.06

    ranked = sorted(best_by_digit.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return None, 0.0
    best_digit, best_score = ranked[0]
    if best_score < 0.66 or best_dice_by_digit.get(best_digit, 0.0) < 0.68:
        # Check if a 1-3px horizontal halo bridge stub widened one side of an otherwise complete digit
        if h >= 14 and w >= 14 and aspect >= 0.82:
            col_counts = (crop_u8 > 0).sum(axis=0)
            max_stub = max(3, int(round(h * 0.25)))
            r_trim = 0
            while r_trim < 4 and (w - r_trim) >= 11 and int(col_counts[w - 1 - r_trim]) <= max_stub:
                r_trim += 1
            l_trim = 0
            while l_trim < 4 and (w - r_trim - l_trim) >= 11 and int(col_counts[l_trim]) <= max_stub:
                l_trim += 1
            if r_trim > 0 or l_trim > 0:
                trimmed = crop_u8[:, l_trim : w - r_trim]
                t_gray = gray_patch[:, l_trim : w - r_trim] if (gray_patch is not None and gray_patch.shape == crop_u8.shape) else None
                t_digit, t_score = classify_failure_digit_glyph(trimmed, gray_patch=t_gray, asset_path=asset_path)
                if t_digit is not None and t_score >= 0.74:
                    return t_digit, t_score
        return None, best_score
    return best_digit, best_score


def split_touching_failure_digits(
    labels: np.ndarray,
    label: int,
    x: int,
    y: int,
    w: int,
    h: int,
    classify_fn: Callable[[np.ndarray, int, int, int, int, int, int], str | None],
    *,
    gray_region: np.ndarray | None = None,
    asset_path: str | None = None,
) -> list[tuple[str, int, int, int, int, int]] | None:
    """Split a 2-digit component joined by a 1-2px glow/halo bridge.

    Both left and right halves MUST pass `classify_fn` and glyph confidence;
    if either half is unreadable, returns `None` (never a partial single digit).
    """
    aspect = w / max(h, 1)
    if not (1.25 < aspect <= 2.38) or w < 12 or h < 10:
        return None
    comp = (labels[y : y + h, x : x + w] == label)
    col_ink = comp.sum(axis=0)
    s_min = max(4, int(round(w * 0.28)))
    s_max = min(w - 4, int(round(w * 0.72)))
    if s_max <= s_min:
        return None

    valley_cols = sorted(range(s_min, s_max + 1), key=lambda c: (int(col_ink[c]), abs(c - w / 2.0)))
    valley_cols = [c for c in valley_cols if int(col_ink[c]) <= max(3, int(h * 0.58))][:4]
    if not valley_cols:
        return None

    best_pair: tuple[float, list[tuple[str, int, int, int, int, int]]] | None = None
    for s in valley_cols:
        left_mask = comp[:, :s]
        right_mask = comp[:, s:]
        lys, lxs = np.where(left_mask)
        rys, rxs = np.where(right_mask)
        if lys.size < 12 or rys.size < 12:
            continue
        ly0, ly1 = int(lys.min()), int(lys.max()) + 1
        lx0, lx1 = int(lxs.min()), int(lxs.max()) + 1
        ry0, ry1 = int(rys.min()), int(rys.max()) + 1
        rx0, rx1 = int(rxs.min()), int(rxs.max()) + 1
        lh, lw = ly1 - ly0, lx1 - lx0
        rh, rw = ry1 - ry0, rx1 - rx0
        if lh < int(h * 0.68) or rh < int(h * 0.68) or lw < 3 or rw < 3:
            continue

        l_crop = left_mask[ly0:ly1, lx0:lx1].astype(np.uint8) * 255
        r_crop = right_mask[ry0:ry1, rx0:rx1].astype(np.uint8) * 255
        l_gray = gray_region[y + ly0 : y + ly1, x + lx0 : x + lx1] if gray_region is not None else None
        r_gray = gray_region[y + ry0 : y + ry1, x + s + rx0 : x + s + rx1] if gray_region is not None else None

        _, sc_l = classify_failure_digit_glyph(l_crop, gray_patch=l_gray, asset_path=asset_path)
        _, sc_r = classify_failure_digit_glyph(r_crop, gray_patch=r_gray, asset_path=asset_path)
        if min(sc_l, sc_r) < 0.68:
            continue

        scratch = labels.copy()
        sub_view = scratch[y : y + h, x : x + w]
        sub_view[comp] = 0
        left_id = int(labels.max()) + 1
        right_id = left_id + 1
        sub_view[:, :s][left_mask] = left_id
        sub_view[:, s:][right_mask] = right_id

        l_area = int(left_mask.sum())
        r_area = int(right_mask.sum())
        d_left = classify_fn(scratch, left_id, x + lx0, y + ly0, lw, lh, l_area)
        d_right = classify_fn(scratch, right_id, x + s + rx0, y + ry0, rw, rh, r_area)
        if d_left is None or d_right is None:
            continue

        joint_score = min(sc_l, sc_r) + 0.25 * (sc_l + sc_r) - 0.02 * float(col_ink[s])
        pair = [
            (d_left, x + lx0, y + ly0, lw, lh, l_area),
            (d_right, x + s + rx0, y + ry0, rw, rh, r_area),
        ]
        if best_pair is None or joint_score > best_pair[0]:
            best_pair = (joint_score, pair)

    return best_pair[1] if best_pair is not None else None


def find_failure_percent_start_glyph(
    mask: np.ndarray,
    *,
    asset_path: str | None = None,
) -> int | None:
    """Fallback '%' locator when stream scaling or glow nicks one tiny ring open.

    Strictly requires ink in BOTH the upper-left ring zone AND lower-right ring
    zone plus the diagonal slash and '%' template match, so a mutilated '%'
    missing a ring still returns `None`.
    """
    bank = load_failure_glyph_bank(asset_path)
    if bank["pct_box_u8"].shape[0] == 0:
        return None
    h_full, w_full = mask.shape[:2]
    if h_full < 8 or w_full < 8:
        return None

    work = mask.copy()
    if h_full >= 32 and w_full >= 80:
        # Exclude outer pill-rim arc pixels so they cannot merge with '%' or shift rightmost_x
        work[: max(3, int(h_full * 0.22)), :] = False
        work[min(h_full - 3, int(h_full * 0.80)) :, :] = False
        work[:, min(w_full - 2, int(w_full * 0.93)) :] = False

    # Try unclosed first (so adjacent digits never merge horizontally into '%'),
    # then vertical/small close (2, 2) for stream-softened nicked '%' rings.
    work_u8 = work.astype(np.uint8) * 255
    passes = [
        work_u8,
        cv2.morphologyEx(work_u8, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))),
        cv2.morphologyEx(work_u8, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))),
    ]
    cols_any = np.where(work.any(axis=0))[0]
    if cols_any.size == 0:
        return None
    rightmost_x = int(cols_any[-1])

    for closed in passes:
        n_lb, _, stats, _ = cv2.connectedComponentsWithStats(closed, 8)
        candidates: list[tuple[int, int, int, int]] = []
        for lb in range(1, n_lb):
            x, y, w, h, area = map(int, stats[lb])
            if h < 8 or area < 16:
                continue
            if h_full >= 32 and h > int(h_full * 0.58):
                continue
            if not (0.68 <= w / max(h, 1) <= 1.55):
                continue
            if x + w < rightmost_x - max(8, int(w_full * 0.12)):
                continue
            candidates.append((x, y, w, h))

        candidates.sort(key=lambda item: item[0], reverse=True)
        for x, y, w, h in candidates:
            sub = work[y : y + h, x : x + w]
            ul_zone = sub[: max(2, int(round(h * 0.46))), : max(2, int(round(w * 0.46)))]
            lr_zone = sub[min(h - 2, int(round(h * 0.54))) :, min(w - 2, int(round(w * 0.54))) :]
            if float(ul_zone.mean()) < 0.18 or float(lr_zone.mean()) < 0.18:
                continue
            slash = np.zeros((h, w), np.uint8)
            cv2.line(slash, (round(w * 0.75), 0), (round(w * 0.20), h - 1), 1, 1)
            if float(sub[slash > 0].mean()) < 0.58:
                continue

            q_box = _normalize_crop_box(sub.astype(np.uint8) * 255)
            q_shifts = _shifts_3x3(q_box)
            inter = (q_shifts[:, None] & bank["pct_box_u8"][None]).sum(axis=(2, 3)).astype(np.float32)
            tot = q_shifts.sum(axis=(1, 2))[:, None].astype(np.float32) + bank["pct_box_sum"][None]
            best_dice = float((2.0 * inter / np.maximum(tot, 1.0)).max())
            if best_dice >= 0.62:
                return x
    return None


def extract_glow_resistant_foreground(region: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Extract pill-background-invariant bright glyph foreground & contrast map."""
    h, w = region.shape[:2]
    if h < 6 or w < 12:
        return np.zeros((h, w), dtype=bool), np.zeros((h, w), dtype=np.float32)

    reg_f = region.astype(np.float32)
    luma = 0.114 * reg_f[:, :, 0] + 0.587 * reg_f[:, :, 1] + 0.299 * reg_f[:, :, 2]

    iy0, iy1 = max(1, h // 5), max(2, (4 * h) // 5)
    ix0, ix1 = max(1, int(w * 0.35)), max(2, int(w * 0.94))
    core = reg_f[iy0:iy1, ix0:ix1]
    core_luma = luma[iy0:iy1, ix0:ix1]
    if core_luma.size >= 16:
        lo_q, hi_q = np.percentile(core_luma, [15, 58])
        bg_sel = (core_luma >= lo_q) & (core_luma <= hi_q)
        bg_bgr = np.median(core[bg_sel], axis=0) if bg_sel.any() else np.median(core.reshape(-1, 3), axis=0)
        bg_luma = float(np.median(core_luma[bg_sel])) if bg_sel.any() else float(np.median(core_luma))
    else:
        bg_bgr = np.median(reg_f.reshape(-1, 3), axis=0)
        bg_luma = float(np.median(luma))

    pos_excess = np.maximum(0.0, reg_f - bg_bgr[None, None, :])
    pos_dist = np.sqrt((pos_excess * pos_excess).sum(axis=2))
    luma_lift = luma - bg_luma

    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    color_plausible = (
        ((hsv[:, :, 1] < 150) & (hsv[:, :, 2] > 180))
        | ((hsv[:, :, 0] >= 6) & (hsv[:, :, 0] <= 46) & (hsv[:, :, 2] > 175) & (reg_f[:, :, 1] > 145.0))
    )
    fg = color_plausible & (luma >= 158.0) & (luma_lift >= 24.0) & (pos_dist >= 56.0)

    contrast = np.clip((0.6 * pos_dist + 0.8 * np.maximum(0.0, luma_lift)) / 180.0, 0.0, 1.0).astype(np.float32)
    return fg, contrast
