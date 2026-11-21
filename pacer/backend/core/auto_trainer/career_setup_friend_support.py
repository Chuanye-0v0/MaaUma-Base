from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from core.auto_trainer.safe_advance import SafeAdvancePlanner


FRIEND_CARD_BBOXES = tuple(
    (32, y, 143, y + 145) for y in (225, 400, 575, 750, 925)
)
FRIEND_REFRESH_BBOX = (610, 960, 692, 1050)
MIN_INLIERS = 40
MIN_INLIER_RATIO = 0.80
MIN_INLIER_MARGIN = 25
MIN_PROJECTED_AREA_RATIO = 0.35
MAX_PROJECTED_AREA_RATIO = 1.15


@dataclass(frozen=True)
class FriendSupportCandidate:
    canonical_bbox: tuple[int, int, int, int]
    score: float
    good_matches: int = 0
    inliers: int = 0
    inlier_ratio: float = 0.0
    projected_area_ratio: float = 0.0
    homography_valid: bool = False


@dataclass(frozen=True)
class FriendSupportPerceptionResult:
    page_detected: bool
    candidates: tuple[FriendSupportCandidate, ...]
    unique_30137: FriendSupportCandidate | None
    refresh_bbox: tuple[int, int, int, int] | None
    errors: tuple[str, ...]


def _gray(image: np.ndarray) -> np.ndarray | None:
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return None


def _score_candidate(
    template_gray: np.ndarray,
    template_keypoints: tuple[cv2.KeyPoint, ...] | list[cv2.KeyPoint],
    template_descriptors: np.ndarray,
    crop: np.ndarray,
    bbox: tuple[int, int, int, int],
    orb: cv2.ORB,
) -> FriendSupportCandidate:
    crop_gray = _gray(crop)
    if crop_gray is None or crop_gray.size == 0:
        return FriendSupportCandidate(bbox, 0.0)
    candidate_keypoints, candidate_descriptors = orb.detectAndCompute(crop_gray, None)
    if candidate_descriptors is None or len(candidate_keypoints) < 4:
        return FriendSupportCandidate(bbox, 0.0)
    good_matches: list[cv2.DMatch] = []
    for pair in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
        template_descriptors,
        candidate_descriptors,
        k=2,
    ):
        if len(pair) == 2 and pair[0].distance < 0.72 * pair[1].distance:
            good_matches.append(pair[0])
    if len(good_matches) < 4:
        return FriendSupportCandidate(
            bbox,
            float(len(good_matches)),
            good_matches=len(good_matches),
        )
    source_points = np.float32(
        [template_keypoints[match.queryIdx].pt for match in good_matches]
    ).reshape(-1, 1, 2)
    target_points = np.float32(
        [candidate_keypoints[match.trainIdx].pt for match in good_matches]
    ).reshape(-1, 1, 2)
    homography, mask = cv2.findHomography(
        source_points,
        target_points,
        cv2.RANSAC,
        5.0,
    )
    if homography is None or mask is None:
        return FriendSupportCandidate(
            bbox,
            0.0,
            good_matches=len(good_matches),
        )
    inliers = int(mask.sum())
    inlier_ratio = inliers / len(good_matches)
    template_height, template_width = template_gray.shape[:2]
    corners = np.float32(
        [
            [[0, 0]],
            [[template_width - 1, 0]],
            [[template_width - 1, template_height - 1]],
            [[0, template_height - 1]],
        ]
    )
    projected = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    crop_height, crop_width = crop_gray.shape[:2]
    projected_area_ratio = abs(cv2.contourArea(projected)) / float(
        crop_height * crop_width
    )
    within_bounds = bool(
        np.all(projected[:, 0] >= -5)
        and np.all(projected[:, 0] <= crop_width + 5)
        and np.all(projected[:, 1] >= -5)
        and np.all(projected[:, 1] <= crop_height + 5)
    )
    homography_valid = bool(
        cv2.isContourConvex(projected.astype(np.float32))
        and within_bounds
        and MIN_PROJECTED_AREA_RATIO
        <= projected_area_ratio
        <= MAX_PROJECTED_AREA_RATIO
    )
    return FriendSupportCandidate(
        canonical_bbox=bbox,
        score=float(inliers),
        good_matches=len(good_matches),
        inliers=inliers,
        inlier_ratio=float(inlier_ratio),
        projected_area_ratio=float(projected_area_ratio),
        homography_valid=homography_valid,
    )


def analyze_friend_support_structure(
    frame: np.ndarray | None,
    target_30137_template: np.ndarray | None = None,
) -> FriendSupportPerceptionResult:
    if (
        not isinstance(frame, np.ndarray)
        or frame.ndim != 3
        or frame.shape[:2] != (1280, 720)
    ):
        return FriendSupportPerceptionResult(
            page_detected=False,
            candidates=(),
            unique_30137=None,
            refresh_bbox=None,
            errors=("friend_support_frame_invalid",),
        )
    first_card = SafeAdvancePlanner._friend_support_first_card_button(frame)
    if first_card is None:
        return FriendSupportPerceptionResult(
            page_detected=False,
            candidates=(),
            unique_30137=None,
            refresh_bbox=None,
            errors=("friend_support_page_geometry_mismatch",),
        )
    candidates = tuple(FriendSupportCandidate(bbox, 0.0) for bbox in FRIEND_CARD_BBOXES)
    if target_30137_template is None:
        return FriendSupportPerceptionResult(
            page_detected=True,
            candidates=candidates,
            unique_30137=None,
            refresh_bbox=FRIEND_REFRESH_BBOX,
            errors=("friend_support_identity_unverified",),
        )
    template_gray = _gray(target_30137_template)
    if template_gray is None or template_gray.size == 0:
        return FriendSupportPerceptionResult(
            page_detected=True,
            candidates=candidates,
            unique_30137=None,
            refresh_bbox=FRIEND_REFRESH_BBOX,
            errors=("friend_support_template_invalid",),
        )
    orb = cv2.ORB_create(nfeatures=700, scaleFactor=1.2, nlevels=8)
    template_keypoints, template_descriptors = orb.detectAndCompute(template_gray, None)
    if template_descriptors is None or len(template_keypoints) < MIN_INLIERS:
        return FriendSupportPerceptionResult(
            page_detected=True,
            candidates=candidates,
            unique_30137=None,
            refresh_bbox=FRIEND_REFRESH_BBOX,
            errors=("friend_support_template_invalid",),
        )
    scored = tuple(
        _score_candidate(
            template_gray,
            template_keypoints,
            template_descriptors,
            frame[y1:y2, x1:x2],
            bbox,
            orb,
        )
        for bbox in FRIEND_CARD_BBOXES
        for x1, y1, x2, y2 in (bbox,)
    )
    ranked = sorted(scored, key=lambda item: item.score, reverse=True)
    best = ranked[0]
    second_score = ranked[1].score if len(ranked) > 1 else 0.0
    qualified = tuple(
        candidate
        for candidate in scored
        if candidate.inliers >= MIN_INLIERS
        and candidate.inlier_ratio >= MIN_INLIER_RATIO
        and candidate.homography_valid
    )
    unique = (
        best
        if len(qualified) == 1
        and qualified[0] == best
        and best.score - second_score >= MIN_INLIER_MARGIN
        else None
    )
    return FriendSupportPerceptionResult(
        page_detected=True,
        candidates=scored,
        unique_30137=unique,
        refresh_bbox=FRIEND_REFRESH_BBOX,
        errors=() if unique is not None else ("friend_support_30137_not_unique",),
    )


__all__ = [
    "FriendSupportCandidate",
    "FriendSupportPerceptionResult",
    "analyze_friend_support_structure",
]
