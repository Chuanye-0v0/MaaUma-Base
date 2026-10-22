"""Offline, parser-backed capture session for skill-purchase evidence.

This adapter owns frame identity and captures only facts that can be derived
from the PNG and existing parser. It deliberately reports selection evidence
as unavailable until a real selected-state fixture/parser exists.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping
import cv2
import numpy as np
from core.image_io import read_image
from core.perception import Perception
from core.skill_batch_purchase import _canonical_plan_sha256, skill_selection_groups
from core.skill_acquisition_parser import _normalize_image, _ocr_data, read_current_skill_points, inspect_skill_acquisition_page, parse_skill_acquisition_pages
from core.skill_purchase_dialog import inspect_skill_batch_purchase_confirmation_dialog, inspect_skill_multi_purchase_success_dialog, inspect_skill_multi_page_purchase_confirmation_dialog, inspect_skill_purchase_confirmation_dialog, inspect_skill_purchase_success_dialog, _outlined_enabled_control, _rapid_confirmation_candidates

def _points_mismatch_recheck(image, evidence, parsed_points):
    """Resolve threshold OCR errors only with an independent raw-crop reading."""
    if evidence.get('accepted') is not True or type(parsed_points) is not int or evidence.get('current_skill_points') == parsed_points:
        return evidence
    normalized, _ = _normalize_image(image)
    for scale in (3, 5):
        crop = cv2.resize(normalized[400:440, 520:650], None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        text, confidence = _ocr_data(crop, lang='eng', psm=6, whitelist='0123456789')
        if confidence >= 0.7 and re.fullmatch('\\d+', text.strip()) and (int(text.strip()) == parsed_points):
            return {**evidence, 'current_skill_points': parsed_points, 'points_recheck': {'text': text, 'confidence': confidence, 'psm': 6, 'scale': scale}}
    return evidence

class SkillPurchaseLiveCaptureSession:
    """Bind real skill-page PNGs to one promoted execution capture batch."""

    def __init__(self, *, review: Mapping[str, Any], root: Path, plan_id: str, sample_id: str, session_id: str, capture_batch_id: str, selection_plan: Mapping[str, Any] | None=None) -> None:
        _validate_review_session_binding(review, plan_id=plan_id, sample_id=sample_id, session_id=session_id, capture_batch_id=capture_batch_id)
        self.review = review
        self._selection_reviews = _selection_order_reviews(review, selection_plan)
        self.root = root
        self.plan_id = plan_id
        self.sample_id = sample_id
        self.session_id = session_id
        self.capture_batch_id = capture_batch_id
        self._sequence = 0
        self._seen_sha256: set[str] = set()
        self._confirmed_item_ids: tuple[str, ...] | None = None
        self._verified_purchase_item_ids: tuple[str, ...] | None = None

    def bind_verified_purchase_item_ids(self, item_ids: list[str]) -> None:
        """Resume after an independently verified exact success closure."""
        reviewed_rows = _required_mapping(self.review, 'reviewed_rows')
        reviews = sorted((review for review in reviewed_rows.values() if isinstance(review, Mapping)), key=lambda review: review.get('expected_selected_total', -1))
        expected = tuple((review.get('item_id') for review in reviews))
        supplied = tuple(item_ids)
        if not reviews or any((not _semantic_review_enabled(review) for review in reviews)) or any((not isinstance(item_id, str) or not item_id for item_id in supplied)) or (supplied != expected):
            raise ValueError('verified purchase item ids mismatch')
        if self._verified_purchase_item_ids not in (None, supplied):
            raise ValueError('verified purchase item ids already bound')
        self._verified_purchase_item_ids = supplied

    def capture(self, image_path: str | Path, *, caller_claims: Mapping[str, Any] | None=None, active_item_ids: set[str] | frozenset[str] | None=None, post_purchase_return: bool=False, navigation_only: bool=False) -> dict[str, Any]:
        """Parse a new PNG; caller-supplied runtime state is never accepted."""
        if caller_claims:
            raise ValueError('caller metadata is forbidden')
        path = Path(image_path)
        if not path.is_file():
            raise ValueError(f'image_path is not a file: {path}')
        frame_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if not navigation_only and frame_sha256 in self._seen_sha256:
            raise ValueError('frame SHA already seen')
        image = read_image(str(path))
        if image is None:
            raise ValueError(f'image cannot be decoded: {path}')
        if navigation_only:
            if active_item_ids != set() or post_purchase_return:
                raise ValueError('navigation_capture_cannot_authorize_selection')
            evidence = inspect_skill_acquisition_page(image)
            evidence = _points_mismatch_recheck(image, evidence, read_current_skill_points(image, backend='rapidocr'))
            scrollbar = cv2.cvtColor(image[480:1026, 697:704], cv2.COLOR_BGR2GRAY).mean(axis=1)
            thumb_rows = np.flatnonzero(scrollbar < 180)
            groups = np.split(thumb_rows, np.where(np.diff(thumb_rows) > 1)[0] + 1) if len(thumb_rows) else []
            groups = [group for group in groups if len(group) >= 20]
            self._sequence += 1
            # A read-only navigation sample has not authorized an action.
            return dict(fresh=True, frame_sha256=frame_sha256, page_kind='skill_acquisition' if evidence.get('accepted') else 'unknown', screen_kind='skill_acquisition' if evidence.get('accepted') else 'unknown', currency_scope='skill_points_only', dangerous_resource_flags=[], current_skill_points=evidence.get('current_skill_points'), list_at_top=bool(groups[0][0] <= 10) if len(groups) == 1 else None, list_at_bottom=bool(groups[0][-1] >= len(scrollbar) - 18) if len(groups) == 1 else None, rows=[], selected_item_ids=[], parser_manifest={'content_signature_sha256': hashlib.sha256(image[468:1029, 30:704].tobytes()).hexdigest()})
        reviewed_rows = _required_mapping(self.review, 'reviewed_rows')
        scoped_reviewed_rows = _scope_reviewed_rows(reviewed_rows, active_item_ids=active_item_ids)
        dialog_state = inspect_skill_batch_purchase_confirmation_dialog(image, reviewed_rows=scoped_reviewed_rows) if len(scoped_reviewed_rows) > 1 else inspect_skill_purchase_confirmation_dialog(image, reviewed_rows=scoped_reviewed_rows)
        if len(scoped_reviewed_rows) == 3:
            reviews = sorted(scoped_reviewed_rows.values(), key=lambda row: row.get('expected_selected_total', -1))
            expected_rows = [dict(item_id=row.get('item_id'), name=row.get('observed_name'), description=row.get('observed_description'), kind='planned') for row in reviews]
            dialog_state = inspect_skill_multi_page_purchase_confirmation_dialog([image], expected_rows=expected_rows)
        if dialog_state.get('accepted') is True:
            self._confirmed_item_ids = tuple(dialog_state['confirmed_item_ids'])
            self._sequence += 1
            self._seen_sha256.add(frame_sha256)
            return {'fresh': True, 'plan_id': self.plan_id, 'sample_id': self.sample_id, 'session_id': self.session_id, 'capture_batch_id': self.capture_batch_id, 'frame_capture_id': f'{self.capture_batch_id}:{self._sequence}:{frame_sha256[:16]}', 'frame_sequence': self._sequence, 'frame_sha256': frame_sha256, **dialog_state}
        ordered_review_item_ids = [str(review['item_id']) for review in sorted(scoped_reviewed_rows.values(), key=lambda review: review.get('expected_selected_total', -1)) if isinstance(review, Mapping) and isinstance(review.get('item_id'), str) and review.get('item_id')]
        success_state = inspect_skill_multi_purchase_success_dialog(image, confirmed_item_ids=ordered_review_item_ids) if len(scoped_reviewed_rows) > 1 else inspect_skill_purchase_success_dialog(image, reviewed_rows=scoped_reviewed_rows)
        if success_state.get('accepted') is True:
            success_item_ids = tuple(success_state['confirmed_item_ids'])
            if len(scoped_reviewed_rows) > 1 and self._confirmed_item_ids is not None and (success_item_ids != self._confirmed_item_ids):
                success_state = {**success_state, 'accepted': False, 'page_kind': 'unknown', 'screen_kind': 'unknown', 'confirmed_item_ids': [], 'close_control': None}
            else:
                if len(scoped_reviewed_rows) > 1:
                    success_state = {**success_state, 'planned_item_ids': ordered_review_item_ids, 'auto_included_item_ids': []}
                self._verified_purchase_item_ids = success_item_ids
        if success_state.get('accepted') is True:
            self._sequence += 1
            self._seen_sha256.add(frame_sha256)
            return {'fresh': True, 'plan_id': self.plan_id, 'sample_id': self.sample_id, 'session_id': self.session_id, 'capture_batch_id': self.capture_batch_id, 'frame_capture_id': f'{self.capture_batch_id}:{self._sequence}:{frame_sha256[:16]}', 'frame_sequence': self._sequence, 'frame_sha256': frame_sha256, **success_state}
        page_evidence = inspect_skill_acquisition_page(image)
        manifest = parse_skill_acquisition_pages([path], ocr_backend=_review_ocr_backend(self.review))
        page_evidence = _points_mismatch_recheck(image, page_evidence, manifest.get('current_skill_points'))
        source_key = _repo_relative(path, self.root)
        self._sequence += 1
        self._seen_sha256.add(frame_sha256)
        page_is_skill_acquisition = page_evidence.get('accepted') is True and page_evidence.get('current_skill_points') == manifest.get('current_skill_points')
        green_confirm_candidate = _enabled_skill_green_confirm_control(image) if page_is_skill_acquisition else None
        terminal_state = _derive_reviewed_acquired_terminal_state(manifest, scoped_reviewed_rows, verified_purchase_item_ids=self._verified_purchase_item_ids) if page_is_skill_acquisition else None
        selection_reviews = _scope_reviewed_rows(self._selection_reviews, active_item_ids=active_item_ids)
        selection = terminal_state if terminal_state is not None else _derive_reviewed_selected_state(manifest, selection_reviews, confirm_enabled=green_confirm_candidate is not None) if page_is_skill_acquisition else _unavailable_selection_state()
        if terminal_state is None and selection.get('remaining_points_semantics') == 'unavailable' and page_is_skill_acquisition and (green_confirm_candidate is not None):
            selection = _derive_reviewed_selected_state_from_points(manifest, selection_reviews, confirm_enabled=True)
        verified_return_targets_absent = post_purchase_return is True and self._verified_purchase_item_ids is not None and (terminal_state is None) and _verified_purchase_targets_absent(manifest, scoped_reviewed_rows)
        geometry_alignment_candidates: list[dict[str, Any]] = []
        if terminal_state is not None or verified_return_targets_absent:
            rows = []
        elif not page_is_skill_acquisition:
            rows = []
        else:
            try:
                rows = _adapt_reviewed_rows_after_selection(manifest, reviewed_rows, source_key, active_item_ids=active_item_ids, selection=selection)
            except ValueError as error:
                if str(error) != 'multi review target missing':
                    raise
                geometry_alignment_candidates = _semantic_review_geometry_alignment_candidates(manifest, scoped_reviewed_rows)
                if not geometry_alignment_candidates:
                    raise
                rows = []
            if not rows and (not geometry_alignment_candidates):
                geometry_alignment_candidates = _semantic_review_geometry_alignment_candidates(manifest, scoped_reviewed_rows)
        target_candidates = [] if terminal_state is not None or verified_return_targets_absent else _semantic_review_abstentions(manifest, scoped_reviewed_rows) + geometry_alignment_candidates if page_is_skill_acquisition else []
        if terminal_state is None and selection['remaining_points_semantics'] == 'unavailable':
            selection = _derive_unselected_state(manifest, page_is_skill_acquisition)
        green_confirm_control = green_confirm_candidate if terminal_state is None and selection['selected_item_ids'] else None
        post_purchase_back_control = _skill_acquisition_back_control(image) if page_is_skill_acquisition else None
        return {'fresh': True, 'page_kind': 'skill_acquisition' if page_is_skill_acquisition else 'unknown', 'screen_kind': 'skill_acquisition' if page_is_skill_acquisition else 'unknown', 'plan_id': self.plan_id, 'sample_id': self.sample_id, 'session_id': self.session_id, 'capture_batch_id': self.capture_batch_id, 'frame_capture_id': f'{self.capture_batch_id}:{self._sequence}:{frame_sha256[:16]}', 'frame_sequence': self._sequence, 'frame_sha256': frame_sha256, 'current_skill_points': manifest.get('current_skill_points'), 'current_skill_points_confidence': 1.0 if page_is_skill_acquisition else 0.0, 'currency_scope': 'skill_points_only' if page_is_skill_acquisition else 'unknown', 'observed_currency': 'skill_points' if page_is_skill_acquisition else 'unknown', 'dangerous_resource_flags': [], 'remaining_points_semantics': selection['remaining_points_semantics'], 'remaining_skill_points': selection['remaining_skill_points'], 'selected_item_ids': selection['selected_item_ids'], 'selected_total': selection['selected_total'], 'purchase_terminal_state': 'acquired' if terminal_state is not None else 'unavailable', 'acquired_item_ids': terminal_state['acquired_item_ids'] if terminal_state is not None else [], 'green_confirm_control': green_confirm_control, 'selection_confirm_candidate': green_confirm_candidate, 'post_purchase_back_control': post_purchase_back_control, 'verified_return_targets_absent': verified_return_targets_absent if post_purchase_return is True else None, 'rows': rows, 'target_candidates': target_candidates, 'parser_manifest': {'schema_version': manifest.get('schema_version'), 'source_page': source_key, 'skill_count': len(manifest.get('skills', [])), 'content_signature_sha256': _skill_page_content_signature(manifest), 'page_detector': page_evidence}}

def _skill_page_content_signature(manifest: Mapping[str, Any]) -> str:
    skills = manifest.get('skills')
    if not isinstance(skills, list):
        skills = []
    rows: list[dict[str, Any]] = []
    for raw_skill in skills:
        if not isinstance(raw_skill, Mapping):
            continue
        source_evidence = raw_skill.get('source_evidence')
        row_bbox = None
        fragment_only = False
        if isinstance(source_evidence, list) and source_evidence:
            last = source_evidence[-1]
            if isinstance(last, Mapping):
                row_bbox = last.get('row_bbox')
                fragment_only = last.get('fragment_only') is True
        rows.append({'row_fingerprint': raw_skill.get('row_fingerprint'), 'row_bbox': row_bbox, 'fragment_only': fragment_only, 'cost': raw_skill.get('cost'), 'acquired': raw_skill.get('acquired'), 'hint_level': raw_skill.get('hint_level'), 'discount_percent': raw_skill.get('discount_percent'), 'description': raw_skill.get('description')})
    payload = {'current_skill_points': manifest.get('current_skill_points'), 'rows': rows}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def _derive_reviewed_acquired_terminal_state(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any], *, verified_purchase_item_ids: tuple[str, ...] | None) -> dict[str, Any] | None:
    if verified_purchase_item_ids is None:
        return None
    reviews = sorted((review for review in reviewed_rows.values() if isinstance(review, Mapping)), key=lambda review: review.get('expected_selected_total', -1))
    item_ids = tuple((review.get('item_id') for review in reviews))
    if not reviews or any((not _semantic_review_enabled(review) for review in reviews)) or item_ids != verified_purchase_item_ids:
        return None
    final_remaining = reviews[-1].get('expected_remaining_skill_points')
    if not isinstance(final_remaining, int) or isinstance(final_remaining, bool) or manifest.get('current_skill_points') != final_remaining:
        return None
    skills = [skill for skill in manifest.get('skills', []) if isinstance(skill, Mapping) and skill.get('acquired') is True and (skill.get('plus_available') is False) and (skill.get('cost') is None)]
    used_skill_indexes: set[int] = set()
    acquired_item_ids: list[str] = []
    for review in reviews:
        token = _compact_text(review['required_description_token'])
        matches = [skill_index for skill_index, skill in enumerate(skills) if skill_index not in used_skill_indexes and isinstance(skill.get('description'), str) and (token in _compact_text(skill['description']))]
        if len(matches) != 1:
            return None
        used_skill_indexes.add(matches[0])
        acquired_item_ids.append(str(review['item_id']))
    return {'remaining_points_semantics': 'post_purchase', 'remaining_skill_points': final_remaining, 'selected_item_ids': [], 'selected_total': 0, 'acquired_item_ids': acquired_item_ids}

def _verified_purchase_targets_absent(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any]) -> bool:
    """Return only when no current row can still prove an unpurchased target."""
    skills = manifest.get('skills')
    if not isinstance(skills, list):
        return False
    reviews = [review for review in reviewed_rows.values() if isinstance(review, Mapping) and _semantic_review_enabled(review)]
    return bool(reviews) and (not _semantic_review_abstentions(manifest, reviewed_rows)) and (not any((_semantic_review_confidence(skill, review) is not None for skill in skills if isinstance(skill, Mapping) for review in reviews)))

def _skill_acquisition_back_control(image: np.ndarray) -> dict[str, int] | None:
    """Return one outlined, enabled, exactly labelled back control.

    Page/Pt and submitted-purchase authority are owned by capture()/executor.
    This observer does not authorize returning from an unfinished purchase.
    """
    height, width = image.shape[:2]
    if width > height:
        return None
    control = _outlined_enabled_control(image, (round(width * 10 / 720), round(height * 1128 / 1280), round(width * 145 / 720), round(height * 1224 / 1280)))
    if control is None:
        return None
    label = image[round(height * 1150 / 1280):round(height * 1198 / 1280), round(width * 32 / 720):round(width * 123 / 720)]
    candidates = _rapid_confirmation_candidates(label)
    if not any((candidate.get('text') == '返回' and isinstance(candidate.get('confidence'), (int, float)) and (candidate['confidence'] >= 0.9) for candidate in candidates)):
        return None
    return control

def _adapt_reviewed_rows(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any], source_key: str, *, active_item_ids: set[str] | frozenset[str] | None=None) -> list[dict[str, Any]]:
    reviewed_rows = _scope_reviewed_rows(reviewed_rows, active_item_ids=active_item_ids)
    rows: list[dict[str, Any]] = []
    skills = [skill for skill in manifest.get('skills', []) if isinstance(skill, Mapping)]
    used_review_keys: set[str] = set()
    used_skill_indexes: set[int] = set()
    seen_item_ids: set[str] = set()
    for skill_index, skill in enumerate(skills):
        fingerprint = skill.get('row_fingerprint')
        if fingerprint not in reviewed_rows:
            continue
        review = reviewed_rows[fingerprint]
        if not isinstance(review, Mapping):
            raise ValueError(f'reviewed row is invalid: {fingerprint}')
        if _semantic_review_enabled(review):
            confidence = _semantic_review_confidence(skill, review)
            if confidence is None:
                continue
            review = dict(review)
            review['name_binding_confidence'] = review.get('name_resolution_confidence')
            review['name_resolution_confidence'] = confidence
        item_id = review.get('item_id')
        if not isinstance(item_id, str) or not item_id or item_id in seen_item_ids:
            raise ValueError('reviewed item id is missing or duplicated')
        rows.append(_adapt_row(skill, review, source_key))
        used_review_keys.add(str(fingerprint))
        used_skill_indexes.add(skill_index)
        seen_item_ids.add(item_id)
    for review_key, raw_review in reviewed_rows.items():
        if str(review_key) in used_review_keys:
            continue
        if not isinstance(raw_review, Mapping) or not _semantic_review_enabled(raw_review):
            continue
        matches: list[tuple[int, Mapping[str, Any], float]] = []
        for skill_index, skill in enumerate(skills):
            if skill_index in used_skill_indexes:
                continue
            confidence = _semantic_review_confidence(skill, raw_review)
            if confidence is not None:
                matches.append((skill_index, skill, confidence))
        if len(matches) > 1:
            raise ValueError('current run semantic target is ambiguous')
        if len(matches) == 1:
            skill_index, skill, confidence = matches[0]
            review = dict(raw_review)
            review['name_binding_confidence'] = review.get('name_resolution_confidence')
            review['name_resolution_confidence'] = confidence
            item_id = review.get('item_id')
            if not isinstance(item_id, str) or not item_id or item_id in seen_item_ids:
                raise ValueError('reviewed item id is missing or duplicated')
            rows.append(_adapt_row(skill, review, source_key))
            used_review_keys.add(str(review_key))
            used_skill_indexes.add(skill_index)
            seen_item_ids.add(item_id)
    if len(reviewed_rows) > 1 and len(used_review_keys) != len(reviewed_rows):
        raise ValueError('multi review target missing')
    return rows

def _adapt_reviewed_rows_after_selection(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any], source_key: str, *, active_item_ids: set[str] | frozenset[str] | None, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Adapt only rows that the same frame still proves are unselected."""
    raw_selected = selection.get('selected_item_ids')
    if raw_selected is None:
        return _adapt_reviewed_rows(manifest, reviewed_rows, source_key, active_item_ids=active_item_ids)
    if not isinstance(raw_selected, list) or any((not isinstance(item_id, str) or not item_id for item_id in raw_selected)) or len(set(raw_selected)) != len(raw_selected):
        raise ValueError('selected reviewed item scope is invalid')
    selected = set(raw_selected)
    if active_item_ids is None:
        active = {raw_review.get('item_id') for raw_review in reviewed_rows.values() if isinstance(raw_review, Mapping)}
        if any((not isinstance(item_id, str) or not item_id for item_id in active)):
            raise ValueError('reviewed item id is missing')
    else:
        active = set(active_item_ids)
    if not selected.issubset(active):
        raise ValueError('selected reviewed item is outside active scope')
    remaining = active - selected
    if not remaining:
        return []
    return _adapt_reviewed_rows(manifest, reviewed_rows, source_key, active_item_ids=frozenset(remaining))

def _scope_reviewed_rows(reviewed_rows: Mapping[str, Any], *, active_item_ids: set[str] | frozenset[str] | None) -> dict[str, Any]:
    if active_item_ids is None:
        return dict(reviewed_rows)
    if isinstance(active_item_ids, (set, frozenset)) and (not active_item_ids):
        return {}
    if not isinstance(active_item_ids, (set, frozenset)) or any((not isinstance(item, str) or not item for item in active_item_ids)):
        raise ValueError('active reviewed item scope is invalid')
    scoped: dict[str, Any] = {}
    seen_item_ids: set[str] = set()
    for review_key, raw_review in reviewed_rows.items():
        if not isinstance(raw_review, Mapping):
            raise ValueError(f'reviewed row is invalid: {review_key}')
        item_id = raw_review.get('item_id')
        if item_id not in active_item_ids:
            continue
        if not isinstance(item_id, str) or item_id in seen_item_ids:
            raise ValueError('active reviewed item id is duplicated')
        scoped[str(review_key)] = raw_review
        seen_item_ids.add(item_id)
    if seen_item_ids != set(active_item_ids):
        raise ValueError('active reviewed item missing')
    return scoped

def _review_ocr_backend(review: Mapping[str, Any]) -> str:
    if not isinstance(review, Mapping):
        return 'none'
    if review.get('schema_version') != 'uma.skill_purchase_current_run_review.v1':
        return 'none'
    reviewed_rows = review.get('reviewed_rows')
    if not isinstance(reviewed_rows, Mapping):
        return 'none'
    if any((isinstance(item, Mapping) and _semantic_review_enabled(item) for item in reviewed_rows.values())):
        return 'auto'
    return 'none'

def _selection_order_reviews(review: Mapping[str, Any], plan: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Derive list-only PT expectations; never reorder the confirmation review."""
    if plan is None or 'reverse_scroll_shifts' not in plan:
        return review.get('reviewed_rows', {})
    rows = _required_mapping(review, 'reviewed_rows')
    if plan.get('plan_sha256') != review.get('batch_plan_sha256') or plan.get('plan_sha256') != _canonical_plan_sha256(plan):
        raise ValueError('selection_plan_binding_mismatch')
    by_id = {row['item_id']: (key, row) for key, row in rows.items()}
    ordered = {}
    total = 0
    for group in skill_selection_groups(plan):
        for item in group['items']:
            key, row = by_id[item['action_id']]
            if row['observed_final_cost'] != item['display_cost'] or row['expected_initial_skill_points'] != plan['initial_skill_points'] or key in ordered:
                raise ValueError('selection_plan_economics_mismatch')
            total += item['display_cost']
            ordered[key] = dict(row, expected_selected_total=total, expected_remaining_skill_points=plan['initial_skill_points'] - total)
    if set(ordered) != set(rows) or total != plan['spent_skill_points']:
        raise ValueError('selection_plan_items_mismatch')
    return ordered

def _validate_review_session_binding(review: Mapping[str, Any], *, plan_id: str, sample_id: str, session_id: str, capture_batch_id: str) -> None:
    if not isinstance(review, Mapping):
        raise ValueError('review is invalid')
    if review.get('schema_version') != 'uma.skill_purchase_current_run_review.v1':
        return
    expected = {'plan_id': plan_id, 'sample_id': sample_id, 'session_id': session_id, 'capture_batch_id': capture_batch_id}
    if any((not isinstance(value, str) or not value or review.get(field) != value for field, value in expected.items())):
        raise ValueError('review binding mismatch')
    reviewed_rows = review.get('reviewed_rows')
    if not isinstance(reviewed_rows, Mapping) or not reviewed_rows:
        raise ValueError('reviewed rows are missing')
    item_ids: set[str] = set()
    source_shas: set[str] = set()
    for row in reviewed_rows.values():
        if not isinstance(row, Mapping) or row.get('current_run_scope_only') is not True:
            raise ValueError('reviewed row scope is invalid')
        item_id = row.get('item_id')
        source_sha = row.get('source_report_sha256')
        if not isinstance(item_id, str) or not item_id or item_id in item_ids or (not isinstance(source_sha, str)) or (not re.fullmatch('[0-9a-f]{64}', source_sha)):
            raise ValueError('reviewed row binding is invalid')
        item_ids.add(item_id)
        source_shas.add(source_sha)
    if len(source_shas) != 1:
        raise ValueError('reviewed row source reports differ')

def _semantic_review_enabled(review: Mapping[str, Any]) -> bool:
    source_sha = review.get('source_report_sha256')
    return review.get('current_run_scope_only') is True and isinstance(review.get('observed_name'), str) and bool(review.get('observed_name')) and isinstance(review.get('observed_final_cost'), int) and (not isinstance(review.get('observed_final_cost'), bool)) and isinstance(review.get('required_description_token'), str) and bool(review.get('required_description_token')) and isinstance(source_sha, str) and bool(re.fullmatch('[0-9a-f]{64}', source_sha))

def _semantic_review_confidence(skill: Mapping[str, Any], review: Mapping[str, Any]) -> float | None:
    return _semantic_review_match_confidence(skill, review, allow_partial=False)

def _shift_tolerant_current_run_review_enabled(skill: Mapping[str, Any], review: Mapping[str, Any], *, allow_geometry_mismatch: bool=False) -> bool:
    """Bind one current-run row after a small deterministic scroll shift.

    This is deliberately narrower than fuzzy name matching: exact economics,
    rarity, row order, near-identical geometry, and the meaningful description
    suffix must all agree.
    """
    confidence = review.get('name_resolution_confidence')
    margin = review.get('name_resolution_margin')
    evidence = review.get('name_resolution_evidence')
    source_binding = review.get('source_binding')
    source_evidence = skill.get('source_evidence')
    token = review.get('required_description_token')
    description = skill.get('description')
    binding_source = review.get('name_binding_source')
    multi_evidence = binding_source == 'current_run_multi_evidence'
    direct_parser = binding_source == 'direct_parser_name'
    required_margin = 0.08 if multi_evidence else 0.25
    if binding_source not in {'direct_parser_name', 'strong_identity_binding_fallback', 'unique_high_margin_resolver', 'current_run_multi_evidence'} or review.get('current_run_scope_only') is not True or (not _valid_confidence(confidence)) or (float(confidence) < 0.85) or (not _valid_confidence(margin)) or (float(margin) < required_margin) or (not isinstance(evidence, Mapping)) or (not _valid_confidence(evidence.get('name'))) or (float(evidence['name']) < 0.7) or any((evidence.get(key) != 1.0 for key in (('description', 'rarity', 'icon_color') if direct_parser else ('cost', 'rarity', 'icon_color')))) or (not isinstance(source_binding, Mapping)) or (not isinstance(source_evidence, list)) or (len(source_evidence) != 1) or (not isinstance(source_evidence[0], Mapping)) or (skill.get('cost') != review.get('observed_final_cost')) or (skill.get('rarity') != review.get('rarity_or_icon_type')) or (skill.get('discount_percent') != review.get('discount_percent')) or (skill.get('acquired') is not False) or (skill.get('plus_available') is not True) or (not isinstance(token, str)) or (not isinstance(description, str)):
        return False
    if direct_parser:
        candidates = skill.get('ocr_raw', {}).get('name_candidates')
        observed_name = review.get('observed_name')
        if not isinstance(candidates, list) or not isinstance(observed_name, str) or (not any((isinstance(candidate, Mapping) and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.8) and (_name_similarity(candidate.get('text'), observed_name) >= 0.75) for candidate in candidates))):
            return False
    expected_bbox = source_binding.get('row_bbox')
    actual_bbox = source_evidence[0].get('row_bbox')
    if not _valid_bbox(expected_bbox) or not _valid_bbox(actual_bbox):
        return False
    dx0 = actual_bbox[0] - expected_bbox[0]
    dy0 = actual_bbox[1] - expected_bbox[1]
    dx1 = actual_bbox[2] - expected_bbox[2]
    dy1 = actual_bbox[3] - expected_bbox[3]
    expected_row_index = source_binding.get('row_index')
    actual_row_index = source_evidence[0].get('row_index')
    geometry_matches = not (type(expected_row_index) is not int or type(actual_row_index) is not int or dx0 != 0 or (dx1 != 0) or (dy0 != dy1))
    if geometry_matches:
        row_delta = actual_row_index - expected_row_index
        if direct_parser:
            if row_delta == 0:
                geometry_matches = abs(dy0) <= 32
            else:
                geometry_matches = abs(row_delta) == 1 and 1 <= abs(dy0) <= 80 and ((row_delta > 0) == (dy0 > 0))
        else:
            max_vertical_shift = 64 if multi_evidence else 32
            geometry_matches = row_delta == 0 and abs(dy0) <= max_vertical_shift
    if not geometry_matches and (not allow_geometry_mismatch):
        return False
    if binding_source == 'strong_identity_binding_fallback':
        if '|' not in token:
            return False
        suffix = _compact_text(token.rsplit('|', 1)[1])
        return len(suffix) >= 6 and suffix in _compact_text(description)
    if multi_evidence:
        contract = review.get('current_run_contract')
        if review.get('name_resolution_evidence_dimensions') != 4 or not isinstance(contract, Mapping) or contract.get('version') != 'current_run_multi_evidence.v1' or (not isinstance(contract.get('local_db_id'), str)) or (not contract['local_db_id'].isdigit()) or (contract.get('identity_status') != 'exact_local_db_id_join') or (contract.get('metadata_status') != 'external_cross_checked'):
            return False
        if '|' in token:
            suffix = _compact_text(token.rsplit('|', 1)[1])
            return len(suffix) >= 6 and suffix in _compact_text(description)
        observed_description = review.get('observed_description')
        if not isinstance(observed_description, str):
            return False
        expected = _compact_text(observed_description)
        actual = _compact_text(description)
        return min(len(expected), len(actual)) >= 10 and SequenceMatcher(None, expected, actual).ratio() >= 0.9
    dimensions = review.get('name_resolution_evidence_dimensions')
    if type(dimensions) is not int or (dimensions < 4 if direct_parser else dimensions != 4):
        return False
    observed_description = review.get('observed_description')
    if not isinstance(observed_description, str):
        return False
    expected = _compact_text(observed_description)
    actual = _compact_text(description)
    suffix_length = min(8, len(expected), len(actual))
    return suffix_length == 8 and expected[-suffix_length:] == actual[-suffix_length:]

def _semantic_review_geometry_alignment_candidates(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose complete current rows that fail only immutable source geometry."""
    candidates: list[dict[str, Any]] = []
    skills = manifest.get('skills')
    if not isinstance(skills, list):
        return candidates
    for raw_review in reviewed_rows.values():
        if not isinstance(raw_review, Mapping) or not _semantic_review_enabled(raw_review):
            continue
        for skill in skills:
            if not isinstance(skill, Mapping):
                continue
            unknown = skill.get('unknown_reason')
            if unknown != []:
                continue
            if _shift_tolerant_current_run_review_enabled(skill, raw_review):
                continue
            if not _shift_tolerant_current_run_review_enabled(skill, raw_review, allow_geometry_mismatch=True):
                continue
            source_evidence = skill.get('source_evidence')
            source_binding = raw_review.get('source_binding')
            if not isinstance(source_evidence, list) or len(source_evidence) != 1 or (not isinstance(source_binding, Mapping)):
                continue
            row_bbox = source_evidence[0].get('row_bbox')
            source_row_bbox = source_binding.get('row_bbox')
            if not _valid_bbox(row_bbox) or not _valid_bbox(source_row_bbox):
                continue
            delta_y = row_bbox[1] - source_row_bbox[1]
            if delta_y == 0 or row_bbox[3] - source_row_bbox[3] != delta_y or row_bbox[0] != source_row_bbox[0] or (row_bbox[2] != source_row_bbox[2]):
                continue
            candidates.append({'item_id': raw_review.get('item_id'), 'reason': 'source_geometry_alignment_needed', 'row_bbox': list(row_bbox), 'source_row_bbox': list(source_row_bbox), 'geometry_delta_y': delta_y})
    return candidates

def _semantic_review_match_confidence(skill: Mapping[str, Any], review: Mapping[str, Any], *, allow_partial: bool) -> float | None:
    if not _semantic_review_enabled(review):
        return None
    unknown = skill.get('unknown_reason')
    if not isinstance(unknown, list):
        return None
    if not allow_partial and 'row_partially_visible' in unknown:
        return None
    exact_fields = [('cost', 'observed_final_cost'), ('rarity', 'rarity_or_icon_type'), ('discount_percent', 'discount_percent')]
    high_margin_resolver = _high_margin_resolver_review_enabled(review)
    catalog_alias_resolver = _catalog_alias_review_enabled(review)
    direct_parser_resolver = _direct_parser_review_enabled(review)
    current_run_fuzzy_resolver = _current_run_fuzzy_review_enabled(review)
    current_run_multi_evidence_resolver = _current_run_multi_evidence_review_enabled(review)
    strong_current_run_resolver = _strong_current_run_unselected_binding(skill, review)
    shift_tolerant_current_run_resolver = _shift_tolerant_current_run_review_enabled(skill, review)
    resolver_bound = high_margin_resolver or catalog_alias_resolver or direct_parser_resolver or current_run_fuzzy_resolver or current_run_multi_evidence_resolver or strong_current_run_resolver or shift_tolerant_current_run_resolver
    if not resolver_bound:
        exact_fields.append(('name', 'observed_name'))
    if any((skill.get(skill_key) != review.get(review_key) for skill_key, review_key in exact_fields)):
        return None
    if not allow_partial and skill.get('hint_level') is not None and (review.get('hint_level') is not None) and (skill.get('hint_level') != review.get('hint_level')):
        return None
    if skill.get('acquired') is not False or skill.get('plus_available') is not True:
        return None
    field_confidence = skill.get('field_confidence')
    if not isinstance(field_confidence, Mapping) or not _valid_confidence(field_confidence.get('cost')):
        return None
    if float(field_confidence['cost']) < 0.9:
        return None
    description = skill.get('description')
    token = review['required_description_token']
    description_matches = isinstance(description, str) and _compact_text(token) in _compact_text(description)
    if shift_tolerant_current_run_resolver:
        description_matches = True
    if not description_matches and (not (high_margin_resolver or direct_parser_resolver)):
        return None
    ocr_raw = skill.get('ocr_raw')
    if not isinstance(ocr_raw, Mapping):
        return None
    name_candidates = ocr_raw.get('name_candidates')
    if not isinstance(name_candidates, list):
        return None
    if resolver_bound:
        allowed_unknowns = {'name_low_confidence_requires_review'}
        if allow_partial:
            allowed_unknowns.add('row_partially_visible')
        if any((reason not in allowed_unknowns for reason in unknown)):
            return None
        if shift_tolerant_current_run_resolver:
            return min(float(review['name_resolution_confidence']), float(field_confidence['cost']))
        raw_aliases = review.get('observed_name_aliases')
        if not isinstance(raw_aliases, list):
            return None
        if not raw_aliases and (not direct_parser_resolver) and (not current_run_multi_evidence_resolver):
            return None
        current_run_contract = review.get('current_run_contract')
        exact_multi_evidence_description = current_run_multi_evidence_resolver and isinstance(current_run_contract, Mapping) and (current_run_contract.get('description_anchor_exact') is True) and isinstance(review.get('observed_description'), str) and (_compact_text(description) == _compact_text(review['observed_description']))
        if exact_multi_evidence_description:
            return min(float(review['name_resolution_confidence']), float(field_confidence['cost']))
        direct_description_bound = direct_parser_resolver and description_matches and isinstance(review.get('name_resolution_evidence_dimensions'), int) and (review['name_resolution_evidence_dimensions'] >= 4) and isinstance(review.get('name_resolution_evidence'), Mapping) and _valid_confidence(review['name_resolution_evidence'].get('cost')) and (float(review['name_resolution_evidence']['cost']) >= 0.95)
        direct_high_margin_name_bound = direct_parser_resolver and _valid_confidence(review.get('name_resolution_margin')) and (float(review['name_resolution_margin']) >= 0.5)
        minimum_alias_confidence = (0.25 if direct_description_bound else 0.6 if direct_high_margin_name_bound else 0.9) if direct_parser_resolver else 0.25 if current_run_fuzzy_resolver or current_run_multi_evidence_resolver else 0.45 if catalog_alias_resolver else 0.6
        trusted_aliases = {alias.get('text') for alias in raw_aliases if isinstance(alias, Mapping) and isinstance(alias.get('text'), str) and alias.get('text') and _valid_confidence(alias.get('confidence')) and (float(alias['confidence']) >= minimum_alias_confidence)}
        canonical_name = review.get('observed_name')
        confidences = [float(candidate['confidence']) for candidate in name_candidates if isinstance(candidate, Mapping) and (description_matches and candidate.get('text') in trusted_aliases or (isinstance(canonical_name, str) and (candidate.get('text') == canonical_name and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.8) or (description_matches and _name_similarity(candidate.get('text'), canonical_name) >= 0.75) or (direct_high_margin_name_bound and _name_similarity(candidate.get('text'), canonical_name) >= 0.75)))) and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= minimum_alias_confidence)]
        return max(confidences) if confidences else None
    confidences = [float(candidate['confidence']) for candidate in name_candidates if isinstance(candidate, Mapping) and candidate.get('text') == review['observed_name'] and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.9)]
    return max(confidences) if confidences else None

def _high_margin_resolver_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get('name_binding_confidence', review.get('name_resolution_confidence'))
    margin = review.get('name_resolution_margin')
    dimensions = review.get('name_resolution_evidence_dimensions')
    return review.get('name_binding_source') == 'unique_high_margin_resolver' and _valid_confidence(confidence) and (float(confidence) >= 0.82) and _valid_confidence(margin) and (float(margin) >= 0.5) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= 3)

def _direct_parser_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get('name_binding_confidence', review.get('name_resolution_confidence'))
    margin = review.get('name_resolution_margin')
    dimensions = review.get('name_resolution_evidence_dimensions')
    evidence = review.get('name_resolution_evidence')
    required = {'name', 'rarity', 'icon_color'}
    corroborated_borderline_name = isinstance(evidence, Mapping) and _valid_confidence(evidence.get('name')) and (0.94 <= float(evidence['name']) < 0.95) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= 4) and _valid_confidence(evidence.get('cost')) and (float(evidence['cost']) >= 0.95)
    return review.get('name_binding_source') == 'direct_parser_name' and _valid_confidence(confidence) and (float(confidence) >= 0.95) and _valid_confidence(margin) and (float(margin) >= 0.25) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= len(required)) and isinstance(evidence, Mapping) and all((_valid_confidence(evidence.get(key)) and (float(evidence[key]) >= 0.95 or (key == 'name' and corroborated_borderline_name)) for key in required))

def _catalog_alias_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get('name_binding_confidence', review.get('name_resolution_confidence'))
    margin = review.get('name_resolution_margin')
    dimensions = review.get('name_resolution_evidence_dimensions')
    evidence = review.get('name_resolution_evidence')
    required = {'name', 'description', 'cost', 'rarity', 'icon_color'}
    return review.get('name_binding_source') == 'catalog_corroborated_ocr_alias' and _valid_confidence(confidence) and (float(confidence) >= 0.95) and _valid_confidence(margin) and (float(margin) >= 0.25) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= len(required)) and isinstance(evidence, Mapping) and all((_valid_confidence(evidence.get(key)) and float(evidence[key]) >= 0.95 for key in required))

def _current_run_fuzzy_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get('name_binding_confidence', review.get('name_resolution_confidence'))
    margin = review.get('name_resolution_margin')
    dimensions = review.get('name_resolution_evidence_dimensions')
    evidence = review.get('name_resolution_evidence')
    required = {'name', 'cost', 'rarity', 'icon_color'}
    return review.get('name_binding_source') == 'current_run_fuzzy_name_description' and review.get('current_run_scope_only') is True and _valid_confidence(confidence) and (float(confidence) >= 0.95) and _valid_confidence(margin) and (float(margin) >= 0.25) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= len(required)) and isinstance(evidence, Mapping) and all((_valid_confidence(evidence.get(key)) and float(evidence[key]) >= 0.94 for key in required))

def _current_run_multi_evidence_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get('name_binding_confidence', review.get('name_resolution_confidence'))
    dimensions = review.get('name_resolution_evidence_dimensions')
    evidence = review.get('name_resolution_evidence')
    contract = review.get('current_run_contract')
    local_db_id = contract.get('local_db_id') if isinstance(contract, Mapping) else None
    return review.get('name_binding_source') == 'current_run_multi_evidence' and review.get('current_run_scope_only') is True and _valid_confidence(confidence) and (float(confidence) >= 0.7) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions in {3, 4}) and isinstance(evidence, Mapping) and (evidence.get('cost') == 1.0) and (evidence.get('rarity') == 1.0) and (evidence.get('icon_color') == 1.0) and isinstance(contract, Mapping) and (contract.get('version') == 'current_run_multi_evidence.v1') and isinstance(local_db_id, str) and local_db_id.isdigit() and (contract.get('identity_status') in {'manual_verified', 'exact_local_db_id_join'}) and (contract.get('metadata_status') in {'manual_verified', 'external_cross_checked'}) and (contract.get('description_anchor_exact') is True or contract.get('name_score_threshold_met') is True)

def _strong_current_run_review_enabled(review: Mapping[str, Any]) -> bool:
    confidence = review.get('name_binding_confidence', review.get('name_resolution_confidence'))
    margin = review.get('name_resolution_margin')
    dimensions = review.get('name_resolution_evidence_dimensions')
    evidence = review.get('name_resolution_evidence')
    source_binding = review.get('source_binding')
    required = {'name', 'description', 'cost', 'rarity', 'icon_color'}
    return review.get('name_binding_source') == 'strong_identity_binding_fallback' and review.get('current_run_scope_only') is True and _valid_confidence(confidence) and (float(confidence) >= 0.95) and _valid_confidence(margin) and (float(margin) >= 0.25) and isinstance(dimensions, int) and (not isinstance(dimensions, bool)) and (dimensions >= len(required)) and isinstance(evidence, Mapping) and all((_valid_confidence(evidence.get(key)) and float(evidence[key]) >= 0.9 for key in required)) and isinstance(review.get('source_row_fingerprint'), str) and bool(review.get('source_row_fingerprint')) and isinstance(source_binding, Mapping) and _valid_bbox(source_binding.get('row_bbox'))

def _same_reviewed_row_bbox(skill: Mapping[str, Any], review: Mapping[str, Any]) -> bool:
    source_evidence = skill.get('source_evidence')
    source_binding = review.get('source_binding')
    return isinstance(source_evidence, list) and len(source_evidence) == 1 and isinstance(source_binding, Mapping) and (source_evidence[0].get('row_bbox') == source_binding.get('row_bbox'))

def _strong_current_run_unselected_binding(skill: Mapping[str, Any], review: Mapping[str, Any]) -> bool:
    return _strong_current_run_review_enabled(review) and skill.get('row_fingerprint') == review.get('source_row_fingerprint') and _same_reviewed_row_bbox(skill, review)

def _name_similarity(left: Any, right: Any) -> float:
    if not isinstance(left, str) or not isinstance(right, str):
        return 0.0
    normalized_left = re.sub('[^0-9A-Za-z\\u4e00-\\u9fff]+', '', left)
    normalized_right = re.sub('[^0-9A-Za-z\\u4e00-\\u9fff]+', '', right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()

def _semantic_review_abstentions(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    skills = manifest.get('skills')
    if not isinstance(skills, list):
        return candidates
    for raw_review in reviewed_rows.values():
        if not isinstance(raw_review, Mapping) or not _semantic_review_enabled(raw_review):
            continue
        for skill in skills:
            if not isinstance(skill, Mapping):
                continue
            unknown = skill.get('unknown_reason')
            if not isinstance(unknown, list) or 'row_partially_visible' not in unknown:
                continue
            confidence = _semantic_review_match_confidence(skill, raw_review, allow_partial=True)
            if confidence is None:
                continue
            source_evidence = skill.get('source_evidence')
            if not isinstance(source_evidence, list) or len(source_evidence) != 1:
                continue
            row_bbox = source_evidence[0].get('row_bbox')
            if not _valid_bbox(row_bbox):
                continue
            reason = 'row_partially_visible_bottom' if row_bbox[3] >= 1029 else 'row_partially_visible_other'
            candidates.append({'item_id': raw_review.get('item_id'), 'reason': reason, 'row_bbox': list(row_bbox), 'row_fingerprint': skill.get('row_fingerprint'), 'name_candidate_confidence': confidence, 'observed_final_cost': skill.get('cost'), 'hint_level': skill.get('hint_level'), 'discount_percent': skill.get('discount_percent')})
    return candidates

def _compact_text(value: str) -> str:
    return re.sub('[\\s，,。．.!！?？:：;；]+', '', value)

def _derive_unselected_state(manifest: Mapping[str, Any], page_is_skill_acquisition: bool) -> dict[str, Any]:
    current_points = manifest.get('current_skill_points')
    available_rows = [skill for skill in manifest.get('skills', []) if skill.get('acquired') is False]
    all_minus_disabled = available_rows and all((skill.get('minus_state') == 'disabled' for skill in available_rows))
    if page_is_skill_acquisition and isinstance(current_points, int) and all_minus_disabled:
        return {'remaining_points_semantics': 'post_selection', 'remaining_skill_points': current_points, 'selected_item_ids': [], 'selected_total': 0}
    return _unavailable_selection_state()

def _unavailable_selection_state() -> dict[str, Any]:
    return {'remaining_points_semantics': 'unavailable', 'remaining_skill_points': None, 'selected_item_ids': None, 'selected_total': None}

def _derive_reviewed_selected_state_from_points(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any], *, confirm_enabled: bool=False) -> dict[str, Any]:
    """Accept a transformed selected prefix only from exact reviewed economics."""
    if not confirm_enabled or not isinstance(reviewed_rows, Mapping) or (not reviewed_rows):
        return _unavailable_selection_state()
    reviews = list(reviewed_rows.values())
    if not all((isinstance(review, Mapping) and _semantic_review_enabled(review) for review in reviews)):
        return _unavailable_selection_state()
    current_points = manifest.get('current_skill_points')
    if not isinstance(current_points, int) or isinstance(current_points, bool):
        return _unavailable_selection_state()
    initial: int | None = None
    cumulative: int | None = None
    matches: list[tuple[list[str], int]] = []
    selected_ids: list[str] = []
    for review in reviews:
        economics = (review.get('expected_initial_skill_points'), review.get('expected_remaining_skill_points'), review.get('expected_selected_total'), review.get('observed_final_cost'))
        if any((not isinstance(value, int) or isinstance(value, bool) for value in economics)):
            return _unavailable_selection_state()
        row_initial, row_remaining, selected_total, observed_cost = economics
        item_id = review.get('item_id')
        if not isinstance(item_id, str) or not item_id or item_id in selected_ids or (observed_cost <= 0):
            return _unavailable_selection_state()
        if initial is None:
            initial = row_initial
            cumulative = selected_total - observed_cost
            if cumulative < 0:
                return _unavailable_selection_state()
        if row_initial != initial:
            return _unavailable_selection_state()
        assert cumulative is not None
        cumulative += observed_cost
        if selected_total != cumulative or initial - cumulative != row_remaining:
            return _unavailable_selection_state()
        selected_ids.append(item_id)
        if row_remaining == current_points:
            matches.append((list(selected_ids), selected_total))
    if len(matches) != 1:
        return _unavailable_selection_state()
    matched_ids, matched_total = matches[0]
    return {'remaining_points_semantics': 'post_selection', 'remaining_skill_points': current_points, 'selected_item_ids': matched_ids, 'selected_total': matched_total}

def _derive_reviewed_selected_state(manifest: Mapping[str, Any], reviewed_rows: Mapping[str, Any], *, confirm_enabled: bool=False) -> dict[str, Any]:
    """Derive an exact visible selected prefix without inferring hidden rows."""
    if not isinstance(reviewed_rows, Mapping) or not reviewed_rows:
        return _unavailable_selection_state()
    reviews = list(reviewed_rows.values())
    if not all((isinstance(review, Mapping) and _semantic_review_enabled(review) for review in reviews)):
        return _unavailable_selection_state()
    skills = manifest.get('skills')
    if not isinstance(skills, list):
        return _unavailable_selection_state()
    initial: int | None = None
    cumulative: int | None = None
    baseline_selected_total: int | None = None
    remaining: int | None = None
    selected_ids: list[str] = []
    used_skill_indexes: set[int] = set()
    reached_unselected_suffix = False
    for raw_review in reviews:
        review = raw_review
        economics = (review.get('expected_initial_skill_points'), review.get('expected_remaining_skill_points'), review.get('expected_selected_total'), review.get('observed_final_cost'))
        if any((not isinstance(value, int) or isinstance(value, bool) for value in economics)):
            return _unavailable_selection_state()
        row_initial, row_remaining, selected_total, observed_cost = economics
        if initial is None:
            initial = row_initial
            baseline_selected_total = selected_total - observed_cost
            if baseline_selected_total < 0:
                return _unavailable_selection_state()
            cumulative = baseline_selected_total
        if row_initial != initial:
            return _unavailable_selection_state()
        assert cumulative is not None
        cumulative += observed_cost
        if selected_total != cumulative or initial - cumulative != row_remaining:
            return _unavailable_selection_state()
        selected_matches = [index for index, skill in enumerate(skills) if index not in used_skill_indexes and isinstance(skill, Mapping) and (_selected_review_match(skill, review, allow_position_binding=confirm_enabled) or (confirm_enabled and _selected_upgrade_review_match(skill, review)))]
        unselected_matches = [index for index, skill in enumerate(skills) if index not in used_skill_indexes and isinstance(skill, Mapping) and (_semantic_review_match_confidence(skill, review, allow_partial=False) is not None)]
        if selected_matches and unselected_matches:
            return _unavailable_selection_state()
        if len(selected_matches) == 1:
            if reached_unselected_suffix:
                return _unavailable_selection_state()
            used_skill_indexes.add(selected_matches[0])
            item_id = review.get('item_id')
            if not isinstance(item_id, str) or not item_id or item_id in selected_ids:
                return _unavailable_selection_state()
            selected_ids.append(item_id)
            remaining = row_remaining
        elif len(unselected_matches) == 1:
            reached_unselected_suffix = True
            used_skill_indexes.add(unselected_matches[0])
        else:
            return _unavailable_selection_state()
    if initial is None or baseline_selected_total is None:
        return _unavailable_selection_state()
    selected_total = baseline_selected_total + sum((int(reviews[index]['observed_final_cost']) for index in range(len(selected_ids))))
    expected_remaining = initial - selected_total
    if remaining is not None and remaining != expected_remaining:
        return _unavailable_selection_state()
    if manifest.get('current_skill_points') != expected_remaining:
        return _unavailable_selection_state()
    return {'remaining_points_semantics': 'post_selection', 'remaining_skill_points': expected_remaining, 'selected_item_ids': selected_ids, 'selected_total': selected_total}

def _selected_review_match(skill: Mapping[str, Any], review: Mapping[str, Any], *, allow_position_binding: bool=False) -> bool:
    unknown = skill.get('unknown_reason')
    if not isinstance(unknown, list) or 'row_partially_visible' in unknown:
        return False
    if skill.get('acquired') is not True or skill.get('plus_available') is not False or skill.get('cost') is not None or (skill.get('discount_percent') != review.get('discount_percent')) or (skill.get('hint_level') is not None and review.get('hint_level') is not None and (skill.get('hint_level') != review.get('hint_level'))):
        return False
    source_evidence = skill.get('source_evidence')
    if not isinstance(source_evidence, list) or len(source_evidence) != 1:
        return False
    if not _valid_bbox(source_evidence[0].get('row_bbox')):
        return False
    ocr_raw = skill.get('ocr_raw')
    candidates = ocr_raw.get('name_candidates') if isinstance(ocr_raw, Mapping) else None
    if not isinstance(candidates, list):
        return False
    high_margin_resolver = _high_margin_resolver_review_enabled(review)
    direct_parser_resolver = _direct_parser_review_enabled(review)
    current_run_fuzzy_resolver = _current_run_fuzzy_review_enabled(review)
    current_run_multi_evidence_resolver = _current_run_multi_evidence_review_enabled(review)
    strong_current_run = _strong_current_run_review_enabled(review) and _same_reviewed_row_bbox(skill, review)
    exact_name_threshold = 0.8 if high_margin_resolver or direct_parser_resolver else 0.9
    exact_name_match = any((isinstance(candidate, Mapping) and candidate.get('text') == review.get('observed_name') and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= exact_name_threshold) for candidate in candidates))
    if high_margin_resolver and exact_name_match:
        return True
    if direct_parser_resolver and exact_name_match:
        return True
    if current_run_fuzzy_resolver:
        description = skill.get('description')
        token = review.get('required_description_token')
        observed_description = review.get('observed_description')
        allowed_overlay_unknowns = {'name_low_confidence_requires_review', 'rarity_acquired_frame_ambiguous'}
        if isinstance(description, str) and isinstance(observed_description, str) and (_compact_text(description) == _compact_text(observed_description)) and set(unknown).issubset(allowed_overlay_unknowns):
            return True
        if isinstance(description, str) and isinstance(token, str) and (_compact_text(token) in _compact_text(description)) and set(unknown).issubset(allowed_overlay_unknowns) and any((isinstance(candidate, Mapping) and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.25) and (_name_similarity(candidate.get('text'), review.get('observed_name')) >= 0.75) for candidate in candidates)):
            return True
    if current_run_multi_evidence_resolver:
        description = skill.get('description')
        observed_description = review.get('observed_description')
        contract = review.get('current_run_contract')
        raw_aliases = review.get('observed_name_aliases')
        trusted_aliases = {alias.get('text') for alias in raw_aliases if isinstance(alias, Mapping) and isinstance(alias.get('text'), str) and _valid_confidence(alias.get('confidence')) and (float(alias['confidence']) >= 0.25)} if isinstance(raw_aliases, list) else set()
        exact_description = isinstance(contract, Mapping) and contract.get('description_anchor_exact') is True and isinstance(description, str) and isinstance(observed_description, str) and (_compact_text(description) == _compact_text(observed_description))
        exact_alias = any((isinstance(candidate, Mapping) and candidate.get('text') in trusted_aliases and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.25) for candidate in candidates))
        if exact_description or exact_alias:
            return True
    if strong_current_run:
        raw_aliases = review.get('observed_name_aliases')
        trusted_aliases = {alias.get('text') for alias in raw_aliases if isinstance(alias, Mapping) and isinstance(alias.get('text'), str) and _valid_confidence(alias.get('confidence')) and (float(alias['confidence']) >= 0.6)} if isinstance(raw_aliases, list) else set()
        if any((isinstance(candidate, Mapping) and candidate.get('text') in trusted_aliases and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.6) for candidate in candidates)):
            return True
    description = skill.get('description')
    token = review.get('required_description_token')
    if not isinstance(description, str) or not isinstance(token, str) or _compact_text(token) not in _compact_text(description):
        return False
    if direct_parser_resolver:
        exact_full_description = _compact_text(description) == _compact_text(token)
        exact_overlay_name = any((isinstance(candidate, Mapping) and candidate.get('text') == review.get('observed_name') and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.55) for candidate in candidates))
        if exact_full_description and exact_overlay_name:
            return True
    allowed_overlay_unknowns = {'name_low_confidence_requires_review', 'rarity_acquired_frame_ambiguous'}
    if direct_parser_resolver and _compact_text(description) == _compact_text(token) and set(unknown).issubset(allowed_overlay_unknowns):
        return True
    if allow_position_binding and _same_reviewed_row_bbox(skill, review) and (_catalog_alias_review_enabled(review) or _strong_current_run_review_enabled(review)) and set(unknown).issubset(allowed_overlay_unknowns):
        return True
    if high_margin_resolver:
        exact_full_description = _compact_text(description) == _compact_text(token)
        exact_low_confidence_name = any((isinstance(candidate, Mapping) and candidate.get('text') == review.get('observed_name') and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.6) for candidate in candidates))
        if exact_full_description and exact_low_confidence_name:
            return True
    if exact_name_match:
        return True
    if not _semantic_review_enabled(review):
        return False
    unknowns = set(unknown)
    if 'name_low_confidence_requires_review' not in unknowns or not unknowns.issubset(allowed_overlay_unknowns):
        return False
    expected_name = _compact_text(str(review['observed_name']))
    if direct_parser_resolver and _compact_text(description) == _compact_text(token):
        direct_overlay_matches = {_compact_text(str(candidate['text'])) for candidate in candidates if isinstance(candidate, Mapping) and isinstance(candidate.get('text'), str) and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.35) and _compact_text(str(candidate['text'])).startswith(expected_name) and (len(_compact_text(str(candidate['text']))) == len(expected_name) + 1)}
        if len(direct_overlay_matches) == 1:
            return True
    strong_candidate_names = {_compact_text(str(candidate['text'])) for candidate in candidates if isinstance(candidate, Mapping) and isinstance(candidate.get('text'), str) and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.6)}
    return len(strong_candidate_names) == 1 and next(iter(strong_candidate_names)).startswith(expected_name) and (len(next(iter(strong_candidate_names))) == len(expected_name) + 1)

def _selected_upgrade_review_match(skill: Mapping[str, Any], review: Mapping[str, Any]) -> bool:
    """Recognize a selected base skill that now displays its next tier.

    Some skill families replace the selected row in place with the next
    purchasable tier.  This path is only called when the page-wide confirm
    control is independently enabled; economics are still checked by the
    caller against the reviewed plan.
    """
    unknown = skill.get('unknown_reason')
    if not isinstance(unknown, list) or 'row_partially_visible' in unknown:
        return False
    observed_cost = review.get('observed_final_cost')
    next_cost = skill.get('cost')
    catalog_alias_review = _catalog_alias_review_enabled(review)
    direct_parser_review = _direct_parser_review_enabled(review)
    current_run_multi_evidence_review = _current_run_multi_evidence_review_enabled(review)
    observed_name = skill.get('name')
    name_matches = observed_name == review.get('observed_name') or (catalog_alias_review and observed_name is None) or (direct_parser_review and observed_name is None) or (current_run_multi_evidence_review and observed_name is None)
    if skill.get('acquired') is not False or skill.get('plus_available') is not True or (not isinstance(observed_cost, int)) or isinstance(observed_cost, bool) or (not isinstance(next_cost, int)) or isinstance(next_cost, bool) or (next_cost <= observed_cost) or (not name_matches) or (skill.get('rarity') != review.get('rarity_or_icon_type')) or (skill.get('discount_percent') != review.get('discount_percent')) or (skill.get('hint_level') is not None and review.get('hint_level') is not None and (skill.get('hint_level') != review.get('hint_level'))):
        return False
    field_confidence = skill.get('field_confidence')
    if not isinstance(field_confidence, Mapping) or not _valid_confidence(field_confidence.get('cost')) or float(field_confidence['cost']) < 0.9:
        return False
    description = skill.get('description')
    original_description = review.get('observed_description')
    token = review.get('required_description_token')
    description_token_matches = isinstance(description, str) and isinstance(token, str) and (_compact_text(token) in _compact_text(description))
    if not isinstance(description, str) or not isinstance(original_description, str) or (not isinstance(token, str)) or (not description_token_matches and (not current_run_multi_evidence_review) and (not direct_parser_review)) or (_compact_text(description) == _compact_text(original_description)):
        return False
    source_evidence = skill.get('source_evidence')
    if not isinstance(source_evidence, list) or len(source_evidence) != 1:
        return False
    if not _valid_bbox(source_evidence[0].get('row_bbox')):
        return False
    ocr_raw = skill.get('ocr_raw')
    candidates = ocr_raw.get('name_candidates') if isinstance(ocr_raw, Mapping) else None
    if not isinstance(candidates, list):
        return False
    if current_run_multi_evidence_review:
        raw_aliases = review.get('observed_name_aliases')
        trusted_aliases = {alias.get('text') for alias in raw_aliases if isinstance(alias, Mapping) and isinstance(alias.get('text'), str) and _valid_confidence(alias.get('confidence')) and (float(alias['confidence']) >= 0.45)} if isinstance(raw_aliases, list) else set()
        return any((isinstance(candidate, Mapping) and candidate.get('text') in trusted_aliases and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.45) for candidate in candidates))
    if catalog_alias_review:
        raw_aliases = review.get('observed_name_aliases')
        trusted_aliases = {alias.get('text') for alias in raw_aliases if isinstance(alias, Mapping) and isinstance(alias.get('text'), str) and _valid_confidence(alias.get('confidence')) and (float(alias['confidence']) >= 0.45)} if isinstance(raw_aliases, list) else set()
        return any((isinstance(candidate, Mapping) and candidate.get('text') in trusted_aliases and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.45) for candidate in candidates))
    return any((isinstance(candidate, Mapping) and candidate.get('text') == review.get('observed_name') and _valid_confidence(candidate.get('confidence')) and (float(candidate['confidence']) >= 0.82) for candidate in candidates))

def _enabled_skill_green_confirm_control(image: Any) -> dict[str, int] | None:
    """Return the enabled skill-page footer confirm control for canonical 720p."""
    if not isinstance(image, np.ndarray) or image.shape != (1280, 720, 3):
        return None
    candidates = []
    for button in Perception().detect_green_buttons(image):
        x, y, width, height = button.bbox
        center_x, center_y = button.center
        if button.kind == 'primary_green' and button.score >= 0.9 and (190 <= x <= 250) and (1020 <= y <= 1065) and (250 <= width <= 320) and (60 <= height <= 90) and (340 <= center_x <= 380) and (1060 <= center_y <= 1100):
            candidates.append(button)
    if len(candidates) != 1:
        return None
    button = candidates[0]
    x, y, width, height = button.bbox
    roi = image[y:y + height, x:x + width]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    bright_green = (hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 85) & (hsv[:, :, 1] >= 130) & (hsv[:, :, 2] >= 150)
    if float(np.mean(bright_green)) < 0.75 or float(np.median(hsv[:, :, 2])) < 190:
        return None
    return {'x': int(button.center[0]), 'y': int(button.center[1])}

def _adapt_row(skill: Mapping[str, Any], review: Mapping[str, Any], source_key: str) -> dict[str, Any]:
    if skill.get('cost') is None or skill.get('plus_available') is not True:
        raise ValueError('reviewed row is not purchasable')
    if skill.get('rarity') != _required(review, 'rarity_or_icon_type'):
        raise ValueError('reviewed rarity does not match parser')
    source_evidence = skill.get('source_evidence')
    if not isinstance(source_evidence, list) or len(source_evidence) != 1:
        raise ValueError('reviewed row source evidence is ambiguous')
    evidence = copy.deepcopy(source_evidence[0])
    row_bbox = evidence.get('row_bbox')
    if not _valid_bbox(row_bbox):
        raise ValueError('reviewed row bbox is invalid')
    if 'row_partially_visible' in skill.get('unknown_reason', []):
        raise ValueError('reviewed row is partial')
    hint_confidence = _reviewed_hint_confidence(skill, review)
    hint_level = skill.get('hint_level')
    if hint_level is None and _semantic_review_enabled(review) and isinstance(review.get('hint_level'), int) and (not isinstance(review.get('hint_level'), bool)):
        hint_level = review.get('hint_level')
    evidence['parser_source_page'] = evidence.get('source_page')
    evidence['source_page'] = source_key
    return {'item_id': _required(review, 'item_id'), 'row_complete': True, 'name_resolution_status': 'auto_confirmed', 'name_resolution_confidence': _required(review, 'name_resolution_confidence'), 'observed_name': _required(review, 'observed_name'), 'observed_final_cost': skill['cost'], 'cost_confidence': skill.get('field_confidence', {}).get('cost'), 'hint_level': hint_level, 'discount_percent': skill.get('discount_percent'), 'hint_confidence': hint_confidence, 'plus_available': skill['plus_available'], 'skill_type': _required(review, 'skill_type'), 'rarity_or_icon_type': skill['rarity'], 'page_kind': 'skill_acquisition', 'screen_kind': 'skill_acquisition', 'plus_control': {'x': row_bbox[2] - 30, 'y': (row_bbox[1] + row_bbox[3]) // 2}, 'source_evidence': [evidence]}

def _required(mapping: Mapping[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValueError(f'required field missing: {key}')
    return mapping[key]

def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = _required(mapping, key)
    if not isinstance(value, Mapping):
        raise ValueError(f'required mapping is invalid: {key}')
    return value

def _valid_confidence(value: Any) -> bool:
    return isinstance(value, (int, float)) and (not isinstance(value, bool)) and (0 <= value <= 1)

def _reviewed_hint_confidence(skill: Mapping[str, Any], review: Mapping[str, Any]) -> float:
    actual = skill.get('field_confidence', {}).get('hint')
    if _valid_confidence(actual):
        if review.get('hint_level') is not None and skill.get('hint_level') is not None and (review['hint_level'] != skill.get('hint_level')):
            raise ValueError('reviewed hint level does not match parser')
        if 'discount_percent' in review and review['discount_percent'] != skill.get('discount_percent'):
            raise ValueError('reviewed discount does not match parser')
        return float(actual)
    source_sha = review.get('source_report_sha256')
    if _semantic_review_enabled(review) and skill.get('hint_level') is None and isinstance(review.get('hint_level'), int) and (not isinstance(review.get('hint_level'), bool)) and (review.get('discount_percent') == skill.get('discount_percent')) and (review.get('discount_percent') in {10, 20, 30, 35, 40}) and isinstance(source_sha, str) and re.fullmatch('[0-9a-f]{64}', source_sha):
        return 0.0
    if review.get('current_run_scope_only') is not True or (review.get('hint_level') is not None and skill.get('hint_level') is not None and (review.get('hint_level') != skill.get('hint_level'))) or review.get('discount_percent') != skill.get('discount_percent') or (review.get('discount_percent') not in {None, 10, 20, 30, 35, 40}) or (review.get('hint_confidence') != 0.0) or (not isinstance(source_sha, str)) or (not re.fullmatch('[0-9a-f]{64}', source_sha)):
        raise ValueError('reviewed hint confidence is unavailable')
    return 0.0

def _valid_bbox(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 4 and all((isinstance(item, int) for item in value)) and (value[0] < value[2]) and (value[1] < value[3])

def _repo_relative(path: Path, root: Path) -> str:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    try:
        return resolved_path.relative_to(resolved_root).as_posix()
    except ValueError:
        primary_root = _primary_checkout_root(resolved_root)
        if primary_root is None:
            raise
        return resolved_path.relative_to(primary_root).as_posix()

def _primary_checkout_root(root: Path) -> Path | None:
    marker = root / '.git'
    if marker.is_dir():
        return root
    if not marker.is_file():
        return None
    try:
        line = marker.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    prefix = 'gitdir:'
    if not line.lower().startswith(prefix):
        return None
    gitdir = Path(line[len(prefix):].strip()).resolve()
    for ancestor in (gitdir, *gitdir.parents):
        if ancestor.name == '.git':
            return ancestor.parent.resolve()
    return None
__all__ = ['SkillPurchaseLiveCaptureSession']
