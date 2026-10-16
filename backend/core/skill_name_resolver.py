from __future__ import annotations
import colorsys
import copy
import base64
import html
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from PIL import Image
AUTO_CONFIRM_SCORE = 0.88
AUTO_CONFIRM_MARGIN = 0.1
UNIQUE_FUZZY_SCORE = 0.84
UNIQUE_FUZZY_MARGIN = 0.35
TOP_K = 5
CURRENT_RUN_FUZZY_NAME_SCORE = 0.72
CURRENT_RUN_FUZZY_NAME_MARGIN = 0.15
CURRENT_RUN_UNIQUE_NOISY_NAME_SCORE = 0.54
CURRENT_RUN_UNIQUE_NOISY_NAME_MARGIN = 0.3
VERIFIED_IDENTITY_STATUSES = {'manual_verified', 'exact_local_db_id_join'}
VERIFIED_METADATA_STATUSES = {'manual_verified', 'external_cross_checked'}

def load_skill_catalog(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    skills = payload.get('skills')
    if not isinstance(skills, list) or not skills:
        raise ValueError('skill catalog must contain a non-empty skills list')
    candidates: list[dict[str, Any]] = []
    for item in skills:
        if not isinstance(item, dict):
            continue
        if item.get('name'):
            candidates.append(item)
            continue
        if item.get('identity_status') != 'manual_verified' or not item.get('name_zh_cn'):
            continue
        skill_type = item.get('type')
        icon_color = 'green' if skill_type == 'green' else 'blue' if skill_type == 'recovery' else 'yellow'
        candidate = {'skill_id': item['skill_id'], 'name': item['name_zh_cn'], 'aliases': [item['name_ja']], 'ocr_aliases': [alias for alias in item.get('ocr_aliases', []) if isinstance(alias, str) and alias] if isinstance(item.get('ocr_aliases', []), list) else [], 'rarity': item.get('rarity', 'unknown'), 'icon_color': icon_color, 'conditions': item.get('conditions', {}), 'relations': item.get('relations', {}), 'source_urls': item.get('source_urls', []), 'manual_status': item.get('metadata_status', 'needs_review'), 'identity_status': item.get('identity_status'), 'metadata_status': item.get('metadata_status'), 'external_truth': copy.deepcopy(item.get('external_truth'))}
        for key in ('description', 'description_keywords', 'base_cost', 'display_costs', 'display_costs_by_discount'):
            if key in item:
                candidate[key] = copy.deepcopy(item[key])
        candidates.append(candidate)
    if not candidates:
        raise ValueError('skill catalog contains no resolver candidates')
    return candidates

def _normalize_text(value: Any) -> str:
    text = str(value or '').lower()
    replacements = {'!': '！', 'x': '×', '○': '圈', '◎': '双圈'}
    for source, target in replacements.items():
        text = text.replace(source, target)
    return re.sub('[^0-9a-z\\u4e00-\\u9fff×！]+', '', text)

def _similarity(left: Any, right: Any) -> float:
    left_normalized = _normalize_text(left)
    right_normalized = _normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()

def _best_name_score(skill: dict[str, Any], candidate: dict[str, Any]) -> float:
    observed: list[str] = []
    if skill.get('name'):
        observed.append(str(skill['name']))
    for entry in skill.get('ocr_raw', {}).get('name_candidates', []):
        if entry.get('text'):
            observed.append(str(entry['text']))
    expected = [candidate['name']]
    expected.extend(candidate.get('aliases', []))
    expected.extend(candidate.get('ocr_aliases', []))
    return max((_similarity(left, right) for left in observed for right in expected), default=0.0)

def _description_score(skill: dict[str, Any], candidate: dict[str, Any]) -> float:
    observed = skill.get('description') or skill.get('ocr_raw', {}).get('description')
    if not observed:
        return 0.0
    scores = [_similarity(observed, candidate.get('description'))]
    normalized_observed = _normalize_text(observed)
    keywords = [_normalize_text(keyword) for keyword in candidate.get('description_keywords', []) if _normalize_text(keyword)]
    if keywords:
        matched = sum((keyword in normalized_observed for keyword in keywords))
        scores.append(matched / len(keywords))
    return max(scores)

def _expected_discounted_cost(candidate: dict[str, Any], discount: Any) -> int | None:
    base_cost = candidate.get('base_cost')
    if base_cost is None or discount is None:
        return None
    return int(round(float(base_cost) * (100 - int(discount)) / 100))

def _cost_score(skill: dict[str, Any], candidate: dict[str, Any]) -> float:
    observed = skill.get('cost')
    if observed is None:
        return 0.0
    layers = candidate.get('upgrade_cost_layers')
    if layers:
        extra = skill.get('verified_extra_discount', 0)
        expected = sum((round(cost * (100 - (skill.get('discount_percent') or 0) - extra) / 100) if hint is None else round(cost * (100 - hint - extra) / 100) for cost, hint in layers))
        return float(observed == expected)
    discount = skill.get('discount_percent')
    if type(discount) is int:
        discount += skill.get('verified_extra_discount', 0)
    discounted = _expected_discounted_cost(candidate, discount)
    if discounted is not None:
        expected = {discounted}
        expected.update(candidate.get('display_costs_by_discount', {}).get(str(skill['discount_percent']), []))
    else:
        expected = set(candidate.get('display_costs', []))
        if candidate.get('base_cost') is not None:
            expected.add(int(candidate['base_cost']))
    if not expected:
        return 0.0
    if int(observed) in expected:
        return 1.0
    return 0.0

def _resolve_local_path(path: str | Path | None, roots: Iterable[Path]) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    for root in roots:
        resolved = root / candidate
        if resolved.is_file():
            return resolved
    return None

def _classify_icon_color(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    with Image.open(path).convert('RGB') as image:
        width, height = image.size
        icon_region = image.crop((int(width * 0.12), int(height * 0.08), int(width * 0.88), int(height * 0.82)))
        get_pixels = getattr(icon_region, 'get_flattened_data', icon_region.getdata)
        pixels = get_pixels()
    counts = {'yellow': 0, 'green': 0, 'blue': 0}
    for red, green, blue in pixels:
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if saturation < 0.35 or value < 0.22:
            continue
        if hue < 0.16 or hue >= 0.94:
            counts['yellow'] += 1
        elif hue < 0.48:
            counts['green'] += 1
        elif hue < 0.75:
            counts['blue'] += 1
    color, count = max(counts.items(), key=lambda item: item[1])
    return color if count else None

def _manifest_roots(manifest_dir: Path | None) -> list[Path]:
    roots = [Path.cwd()]
    if manifest_dir is not None:
        roots.extend([manifest_dir, manifest_dir.parent, manifest_dir.parent.parent])
    return roots

def _has_trusted_display_cost(skill: dict[str, Any]) -> bool:
    cost = skill.get('cost')
    if type(cost) is not int or cost <= 0 or type(skill.get('hint_level')) is not int or (type(skill.get('discount_percent')) is not int):
        return False
    evidence = [skill.get('cost_evidence')]
    evidence.extend((row.get('cost_evidence') for row in skill.get('source_evidence', []) if isinstance(row, dict)))
    evidence = [item for item in evidence if isinstance(item, dict)]
    if any((type(item.get('value')) is not int or item['value'] != cost for item in evidence)):
        return False
    return any((item.get('source') in {'rapidocr', 'tesseract', 'tesseract_crosscheck'} and type(item.get('confidence')) in (int, float) and (0.9 <= item['confidence'] <= 1.0) for item in evidence))

def _score_candidate(skill: dict[str, Any], candidate: dict[str, Any], icon_color: str | None) -> dict[str, Any]:
    components: dict[str, float] = {}
    if skill.get('name') or skill.get('ocr_raw', {}).get('name_candidates'):
        components['name'] = _best_name_score(skill, candidate)
    if (skill.get('description') or skill.get('ocr_raw', {}).get('description')) and (candidate.get('description') or candidate.get('description_keywords')):
        components['description'] = _description_score(skill, candidate)
    if skill.get('cost') is not None and (candidate.get('base_cost') is not None or candidate.get('display_costs') or candidate.get('display_costs_by_discount')):
        components['cost'] = _cost_score(skill, candidate)
    observed_rarity = skill.get('rarity')
    if observed_rarity and observed_rarity != 'unknown':
        components['rarity'] = float(observed_rarity == candidate.get('rarity'))
    if icon_color:
        components['icon_color'] = float(icon_color == candidate.get('icon_color'))
    if len(skill.get('source_pages', [])) > 1:
        components['adjacent_page'] = 1.0
    weights = {'name': 0.36, 'description': 0.32, 'cost': 0.14, 'rarity': 0.09, 'icon_color': 0.09, 'adjacent_page': 0.05}
    denominator = sum((weights[key] for key in components))
    score = sum((weights[key] * value for key, value in components.items())) / denominator if denominator else 0.0
    strong_without_cost = components.get('name', 0.0) >= 0.75 and components.get('description', 0.0) >= 0.75 and (components.get('icon_color', 0.0) >= 0.75) and (components.get('rarity', 0.0) >= 0.75)
    if strong_without_cost and components.get('cost', 0.0) == 0.0 and (not _has_trusted_display_cost(skill)):
        components.pop('cost', None)
        denominator = sum((weights[key] for key in components))
        score = sum((weights[key] * value for key, value in components.items())) / denominator if denominator else 0.0
    conflicts = [key for key in ('cost', 'rarity', 'icon_color') if key in components and components[key] == 0.0]
    observed_distance = re.fullmatch('(非标准距离|标准距离)[○◎×]?', str(skill.get('name', '')).strip())
    candidate_distance = re.fullmatch('(非标准距离|标准距离)[○◎×]', str(candidate.get('name', '')))
    description = _normalize_text(skill.get('description'))
    confidence = skill.get('field_confidence') or {}
    if observed_distance and candidate_distance and (confidence.get('name', 0) >= 0.9) and (confidence.get('description', 0) >= 0.9):
        observed_condition = observed_distance.group(1)
        description_agrees = '非标准距离除400米的倍数外' in description if observed_condition == '非标准距离' else '标准距离400米的倍数' in description and '非标准距离' not in description
        if description_agrees and observed_condition != candidate_distance.group(1):
            conflicts.append('distance_multiple_semantics')
    score = max(0.0, score - 0.22 * len(conflicts))
    evidence_dimensions = sum((value >= 0.75 for key, value in components.items() if key != 'adjacent_page'))
    return {'skill_id': candidate['skill_id'], 'name': candidate['name'], 'score': round(score, 4), 'evidence': {key: round(value, 4) for key, value in components.items()}, 'evidence_dimensions': evidence_dimensions, 'hard_conflicts': conflicts, 'source_urls': candidate.get('source_urls', []), 'manual_status': candidate.get('manual_status', 'needs_review')}

def _candidate_local_db_id(candidate: dict[str, Any]) -> str | None:
    external_truth = candidate.get('external_truth')
    if isinstance(external_truth, dict):
        local_db_id = external_truth.get('local_db_id')
        if isinstance(local_db_id, (str, int)) and str(local_db_id).isdigit():
            return str(local_db_id)
    for source_url in candidate.get('source_urls', []):
        if not isinstance(source_url, str):
            continue
        match = re.fullmatch('local://data/apk_extracted/skill_database\\.json#([0-9]+)', source_url)
        if match:
            return match.group(1)
    return None

def _description_has_canonical_name(skill: dict[str, Any], candidate: dict[str, Any]) -> bool:
    observed = _normalize_text(skill.get('description') or skill.get('ocr_raw', {}).get('description'))
    expected = _normalize_text(candidate.get('name'))
    return bool(observed and expected and (len(expected) >= 2) and (expected in observed))

def _same_db_semantic_name(candidate: dict[str, Any]) -> str:
    return re.sub('(?:双圈|圈)$', '', _normalize_text(candidate.get('name')))

def _preferred_same_db_candidate(candidates: list[tuple[dict[str, Any], dict[str, Any], bool]]) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
    semantic_names = {_same_db_semantic_name(candidate) for candidate, _, _ in candidates}
    if len(semantic_names) != 1:
        return None
    return max(candidates, key=lambda item: (item[0].get('identity_status') == 'exact_local_db_id_join', item[0].get('metadata_status') == 'external_cross_checked', str(item[0].get('skill_id', '')).startswith('ura_db_'), item[1]['score'], str(item[0].get('skill_id', ''))))

def _current_run_multi_evidence_candidate(skill: dict[str, Any], catalog: list[dict[str, Any]], icon_color: str | None) -> dict[str, Any] | None:
    unknown_reasons = set(skill.get('unknown_reason') or [])
    if 'row_partially_visible' in unknown_reasons:
        return None
    if skill.get('cost') is None or not isinstance(skill.get('description'), str):
        return None
    if not skill['description'].strip() or icon_color is None:
        return None
    if skill.get('rarity') in (None, '', 'unknown'):
        return None
    by_db_id: dict[str, list[tuple[dict[str, Any], dict[str, Any], bool]]] = {}
    for candidate in catalog:
        db_id = _candidate_local_db_id(candidate)
        if db_id is None:
            continue
        if candidate.get('identity_status') not in VERIFIED_IDENTITY_STATUSES:
            continue
        if candidate.get('metadata_status') not in VERIFIED_METADATA_STATUSES:
            continue
        scored = _score_candidate(skill, candidate, icon_color)
        evidence = scored['evidence']
        description_anchor = _description_has_canonical_name(skill, candidate)
        if scored['hard_conflicts'] or evidence.get('cost') != 1.0 or evidence.get('rarity') != 1.0 or (evidence.get('icon_color') != 1.0) or (evidence.get('name', 0.0) < CURRENT_RUN_UNIQUE_NOISY_NAME_SCORE and (not description_anchor)):
            continue
        by_db_id.setdefault(db_id, []).append((candidate, scored, description_anchor))
    groups: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
    for group in by_db_id.values():
        preferred = _preferred_same_db_candidate(group)
        if preferred is None:
            continue
        groups.append(preferred)
    if not groups:
        return None
    description_groups = [group for group in groups if group[2]]
    contenders = description_groups or groups
    contenders.sort(key=lambda group: (-group[1]['evidence'].get('name', 0.0), -group[1]['score'], str(group[0].get('skill_id', ''))))
    top_candidate, top_scored, description_anchor = contenders[0]
    top_name = top_scored['evidence'].get('name', 0.0)
    runner_name = contenders[1][1]['evidence'].get('name', 0.0) if len(contenders) > 1 else 0.0
    name_margin = top_name - runner_name
    normal_name_gate = top_name >= CURRENT_RUN_FUZZY_NAME_SCORE and name_margin >= CURRENT_RUN_FUZZY_NAME_MARGIN
    unique_noisy_name_gate = top_name >= CURRENT_RUN_UNIQUE_NOISY_NAME_SCORE and name_margin >= CURRENT_RUN_UNIQUE_NOISY_NAME_MARGIN
    if not description_anchor and (not (normal_name_gate or unique_noisy_name_gate)):
        return None
    if description_anchor and len(contenders) > 1:
        if name_margin < CURRENT_RUN_FUZZY_NAME_MARGIN:
            return None
    top_scored['current_run_contract'] = {'version': 'current_run_multi_evidence.v1', 'local_db_id': _candidate_local_db_id(top_candidate), 'description_anchor_exact': description_anchor, 'name_score_threshold_met': normal_name_gate or unique_noisy_name_gate, 'identity_status': top_candidate.get('identity_status'), 'metadata_status': top_candidate.get('metadata_status')}
    return top_scored

def _remaining_upgrade_catalog(catalog, owned, observed_rows=()):
    """Catalog costs include the lower chain; only its unowned portion is displayed."""
    by_id = {item['skill_id']: item for item in catalog}
    hints = {row.get('name'): row.get('discount_percent') or 0 for row in observed_rows if row.get('acquired') is False}
    result = []
    for item in catalog:
        pending = list(item.get('relations', {}).get('lower', []))
        visited = set()
        paid = []
        while pending:
            lower_id = pending.pop()
            if lower_id in visited or lower_id not in by_id:
                continue
            visited.add(lower_id)
            lower = by_id[lower_id]
            if lower_id in owned and type(lower.get('base_cost')) is int:
                paid.append(lower['base_cost'])
            pending.extend(lower.get('relations', {}).get('lower', []))
        if paid and type(item.get('base_cost')) is int and (0 < max(paid) < item['base_cost']):
            item = {**item, 'base_cost': item['base_cost'] - max(paid), 'display_costs': [], 'display_costs_by_discount': {}}
        chain = sorted((by_id[key] for key in visited), key=lambda row: row.get('base_cost') or 0)
        if chain and all((type(row.get('base_cost')) is int for row in chain)):
            original = by_id[item['skill_id']]
            paid_total = max(paid, default=0)
            previous = 0
            layers = []
            for tier in [*chain, original]:
                total = tier.get('base_cost')
                if type(total) is not int or total <= previous:
                    layers = []
                    break
                if total > paid_total:
                    hint = None if tier is original else hints.get(tier['name'], 0)
                    layers.append((total - previous, hint))
                previous = total
            if layers:
                item = {**item, 'upgrade_cost_layers': layers}
        result.append(item)
    return result

def resolve_skill_manifest(manifest: dict[str, Any], catalog: list[dict[str, Any]], *, manifest_dir: str | Path | None=None) -> dict[str, Any]:
    resolved = copy.deepcopy(manifest)
    resolved['schema_version'] = 'uma.skill_acquisition_resolved_manifest.v1'
    directory = Path(manifest_dir) if manifest_dir is not None else None
    roots = _manifest_roots(directory)
    confirmed = 0
    discount_witnesses = []
    for row in resolved.get('skills', []):
        matches = [c for c in catalog if row.get('name') == c.get('name') and _description_score(row, c) >= 0.9 and (c.get('rarity') == row.get('rarity'))]
        if len(matches) == 1 and type(row.get('discount_percent')) is int and _has_trusted_display_cost(row) and (type(matches[0].get('base_cost')) is int) and (_expected_discounted_cost(matches[0], row['discount_percent']) != row.get('cost')) and (_expected_discounted_cost(matches[0], row['discount_percent'] + 10) == row.get('cost')):
            discount_witnesses.append(row['name'])
    if len(set(discount_witnesses)) >= 3:
        for row in resolved.get('skills', []):
            row['verified_extra_discount'] = 10
        resolved['extra_discount_evidence'] = dict(percent=10, witnesses=sorted(set(discount_witnesses)))
    owned = set()
    upgrade_catalog = None
    ordered = sorted(enumerate(resolved.get('skills', [])), key=lambda pair: pair[1].get('acquired') is not True)
    for index, skill in ordered:
        if skill.get('acquired') is not True and upgrade_catalog is None:
            upgrade_catalog = _remaining_upgrade_catalog(catalog, owned, manifest.get('skills', []))
        candidates = catalog if skill.get('acquired') is True else upgrade_catalog
        skill['item_id'] = f'skill_{index:03d}'
        skill['parser_name'] = skill.get('name')
        icon_path = _resolve_local_path(skill.get('icon_crop', ''), roots)
        icon_color = _classify_icon_color(icon_path)
        ranked = sorted((_score_candidate(skill, candidate, icon_color) for candidate in candidates), key=lambda item: (-item['score'], item['skill_id']))
        top = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else {'score': 0.0}
        margin = round(top['score'] - runner_up['score'], 4)
        text_evidence = max(top['evidence'].get('name', 0.0), top['evidence'].get('description', 0.0))
        standard_confirm = top['score'] >= AUTO_CONFIRM_SCORE and margin >= AUTO_CONFIRM_MARGIN and (top['evidence_dimensions'] >= 2) and (text_evidence >= 0.75) and (not top['hard_conflicts'])
        unique_fuzzy_confirm = top['score'] >= UNIQUE_FUZZY_SCORE and margin >= UNIQUE_FUZZY_MARGIN and (top['evidence'].get('name', 0.0) >= 0.72) and (top['evidence'].get('cost', 0.0) == 1.0) and (max(top['evidence'].get('icon_color', 0.0), top['evidence'].get('description', 0.0)) >= 0.75) and (top['evidence_dimensions'] >= 3) and (not top['hard_conflicts'])
        current_run_multi_evidence = None
        if not (standard_confirm or unique_fuzzy_confirm):
            current_run_multi_evidence = _current_run_multi_evidence_candidate(skill, candidates, icon_color)
            if current_run_multi_evidence is not None:
                selected_id = current_run_multi_evidence['skill_id']
                ranked = [current_run_multi_evidence] + [candidate for candidate in ranked if candidate['skill_id'] != selected_id]
                top = ranked[0]
                runner_up = ranked[1] if len(ranked) > 1 else {'score': 0.0}
                margin = round(top['score'] - runner_up['score'], 4)
        current_run_multi_evidence_confirm = current_run_multi_evidence is not None
        auto_confirm = standard_confirm or unique_fuzzy_confirm or current_run_multi_evidence_confirm
        binding_policy = 'standard' if standard_confirm else 'unique_fuzzy' if unique_fuzzy_confirm else 'current_run_multi_evidence' if current_run_multi_evidence_confirm else 'abstain'
        reasons: list[str] = []
        if not auto_confirm:
            reasons.append('insufficient_unique_evidence')
            if top['score'] < min(AUTO_CONFIRM_SCORE, UNIQUE_FUZZY_SCORE):
                reasons.append('top_candidate_score_below_threshold')
            if margin < min(AUTO_CONFIRM_MARGIN, UNIQUE_FUZZY_MARGIN):
                reasons.append('top_candidate_margin_too_small')
            if top['evidence_dimensions'] < 3:
                reasons.append('fewer_than_three_supporting_dimensions')
            if top['hard_conflicts']:
                reasons.append('hard_evidence_conflict')
        resolution_confidence = top['score']
        if current_run_multi_evidence_confirm:
            contract = top.get('current_run_contract', {})
            resolution_confidence = max(resolution_confidence, 0.95 if contract.get('description_anchor_exact') is True else 0.7)
        skill['icon_category'] = icon_color
        skill['name_resolution'] = {'status': 'auto_confirmed' if auto_confirm else 'needs_review', 'confidence': round(resolution_confidence, 4), 'margin': margin, 'evidence_dimensions': top['evidence_dimensions'], 'binding_policy': binding_policy, 'candidates': ranked[:TOP_K], 'source_urls': top['source_urls'], 'unknown_reason': reasons, 'policy': {'min_score': AUTO_CONFIRM_SCORE, 'min_margin': AUTO_CONFIRM_MARGIN, 'min_evidence_dimensions': 2, 'unique_fuzzy': {'min_score': UNIQUE_FUZZY_SCORE, 'min_margin': UNIQUE_FUZZY_MARGIN, 'requires_exact_cost': True, 'requires_icon_or_description': True}}}
        if auto_confirm:
            skill['name'] = top['name']
            skill['skill_id'] = top['skill_id']
            confirmed += 1
            if skill.get('acquired') is True:
                owned.add(top['skill_id'])
        else:
            skill['name'] = None
            skill['skill_id'] = None
    total = len(resolved.get('skills', []))
    resolved['resolution_summary'] = {'total': total, 'auto_confirmed': confirmed, 'needs_review': total - confirmed, 'execution_allowed': False}
    resolved.setdefault('planner_interface', {})['requires_manual_name_to_skill_id_mapping'] = bool(total - confirmed)
    resolved['planner_interface']['purchase_execution_allowed'] = False
    return resolved

def _find_source_image(source_page: str, sample_root: Path) -> Path | None:
    path = Path(source_page)
    candidates = [path, Path.cwd() / path, sample_root / path.name]
    return next((candidate for candidate in candidates if candidate.is_file()), None)

def _export_row_crop(skill: dict[str, Any], destination: Path, sample_root: Path) -> bool:
    evidence = next((item for item in skill.get('source_evidence', []) if not item.get('fragment_only') and item.get('row_bbox')), None)
    if evidence is None:
        return False
    source = _find_source_image(evidence['source_page'], sample_root)
    if source is None:
        return False
    with Image.open(source) as image:
        image.crop(tuple(evidence['row_bbox'])).save(destination)
    return True

def render_skill_review_html(resolved_manifest: dict[str, Any], *, output_path: str | Path, manifest_dir: str | Path, sample_root: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_dir = Path(manifest_dir)
    sample_root = Path(sample_root)
    crop_dir = output.parent / 'review_crops'
    crop_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for index, skill in enumerate(resolved_manifest.get('skills', [])):
        item_id = skill.get('item_id', f'skill_{index:03d}')
        crop_name = f'{item_id}_row.png'
        crop_path = crop_dir / crop_name
        crop_available = _export_row_crop(skill, crop_path, sample_root)
        crop_html = f'<img class="row-crop" src="review_crops/{crop_name}" alt="{item_id} row crop">' if crop_available else '<span class="missing">row crop unavailable</span>'
        icon_value = skill.get('icon_crop')
        icon_path = manifest_dir / icon_value if icon_value else None
        icon_data = base64.b64encode(icon_path.read_bytes()).decode('ascii') if icon_path and icon_path.is_file() else None
        icon_html = f'<img class="icon" src="data:image/png;base64,{icon_data}" alt="{item_id} icon">' if icon_data else ''
        resolution = skill['name_resolution']
        candidates = ''.join(('<li><strong>{}</strong> <code>{}</code> - {:.4f}<br><small>{}</small></li>'.format(html.escape(candidate['name']), html.escape(candidate['skill_id']), candidate['score'], html.escape(json.dumps(candidate['evidence'], ensure_ascii=False))) for candidate in resolution['candidates']))
        raw_ocr = html.escape(json.dumps(skill.get('ocr_raw', {}), ensure_ascii=False, indent=2))
        source_urls = html.escape(json.dumps(resolution.get('source_urls', []), ensure_ascii=False, indent=2))
        reasons = resolution.get('unknown_reason') or ['无，证据达到自动确认阈值']
        rows.append(f'''\n            <article class="skill-row" data-skill-row="{item_id}">\n              <header><span class="index">{index:02d}</span><h2>{html.escape(skill.get('name') or '未确认')}</h2>\n                <span class="status {resolution['status']}">{resolution['status']}</span></header>\n              <div class="visuals">{crop_html}{icon_html}</div>\n              <dl>\n                <dt>skill_id</dt><dd><code>{html.escape(str(skill.get('skill_id')))}</code></dd>\n                <dt>价格</dt><dd>{html.escape(str(skill.get('cost')))}</dd>\n                <dt>启发 / 折扣</dt><dd>{html.escape(str(skill.get('hint_level')))} / {html.escape(str(skill.get('discount_percent')))}%</dd>\n                <dt>稀有度 / 图标类别</dt><dd>{html.escape(str(skill.get('rarity')))} / {html.escape(str(skill.get('icon_category')))}</dd>\n                <dt>置信度</dt><dd>{resolution['confidence']:.4f}，margin {resolution['margin']:.4f}，证据维度 {resolution['evidence_dimensions']}</dd>\n                <dt>unknown_reason</dt><dd>{html.escape(', '.join(reasons))}</dd>\n              </dl>\n              <details open><summary>Top-k 候选</summary><ol>{candidates}</ol></details>\n              <details><summary>OCR 原文</summary><pre>{raw_ocr}</pre></details>\n              <details><summary>source_urls</summary><pre>{source_urls}</pre></details>\n            </article>\n            ''')
    summary = resolved_manifest.get('resolution_summary', {})
    document = f"""<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n<title>技能页识别核对</title>\n<style>\n:root {{ color-scheme: light; font-family: "Microsoft YaHei", sans-serif; background:#f4f6f8; color:#17202a; }}\nbody {{ margin:0; }} main {{ max-width:1180px; margin:auto; padding:24px; }}\n.summary {{ display:flex; gap:24px; padding:16px 0; border-bottom:2px solid #243746; margin-bottom:18px; }}\n.skill-row {{ background:white; border:1px solid #cbd3da; border-radius:6px; padding:16px; margin:0 0 14px; }}\nheader {{ display:flex; align-items:center; gap:12px; }} h1,h2 {{ margin:0; }} h2 {{ font-size:20px; }}\n.index {{ font:700 14px monospace; color:#5d6d7e; }} .status {{ margin-left:auto; font:700 12px monospace; padding:3px 7px; border-radius:4px; }}\n.auto_confirmed {{ background:#d9f2df; color:#196b2d; }} .needs_review {{ background:#fde2df; color:#9f2d22; }}\n.visuals {{ display:flex; gap:12px; align-items:flex-start; margin:12px 0; overflow:auto; }}\n.row-crop {{ width:min(660px, 82vw); border:1px solid #9ba8b2; }} .icon {{ width:76px; height:76px; object-fit:contain; border:1px solid #9ba8b2; }}\ndl {{ display:grid; grid-template-columns:180px 1fr; gap:6px 10px; }} dt {{ font-weight:700; }} dd {{ margin:0; }}\ndetails {{ margin-top:10px; }} pre {{ white-space:pre-wrap; overflow-wrap:anywhere; background:#f4f6f8; padding:10px; }}\ncode {{ font-family:Consolas, monospace; }} li {{ margin-bottom:7px; }}\n@media (max-width:700px) {{ main {{ padding:12px; }} dl {{ grid-template-columns:1fr; }} .summary {{ flex-wrap:wrap; }} }}\n</style></head><body><main>\n<h1>技能页识别核对</h1>\n<section class="summary"><span>总数 <strong>{summary.get('total', 0)}</strong></span><span>唯一确认 <strong>{summary.get('auto_confirmed', 0)}</strong></span><span>待核对 <strong>{summary.get('needs_review', 0)}</strong></span><span>只读：禁止购买执行</span></section>\n{''.join(rows)}\n</main></body></html>"""
    output.write_text(document, encoding='utf-8')
    return output

def _predicted_row_complete(item: dict[str, Any]) -> bool:
    return 'row_partially_visible' not in item.get('unknown_reason', [])

def evaluate_skill_recognition(resolved_manifest: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    predictions = {item.get('item_id', f'skill_{index:03d}'): item for index, item in enumerate(resolved_manifest.get('skills', []))}
    fields = ('name', 'skill_id', 'cost', 'hint_level', 'discount_percent', 'acquired', 'rarity', 'row_complete')
    metrics = {field: {'total': 0, 'correct': 0, 'unknown': 0, 'incorrect': 0, 'unknown_item_ids': [], 'incorrect_item_ids': []} for field in fields}
    for expected in truth.get('items', []):
        item_id = expected['item_id']
        predicted = predictions.get(item_id, {})
        for field in fields:
            metric = metrics[field]
            metric['total'] += 1
            actual = _predicted_row_complete(predicted) if field == 'row_complete' and predicted else predicted.get(field)
            wanted = expected.get(field)
            if actual == wanted:
                metric['correct'] += 1
            elif actual is None or actual == 'unknown':
                metric['unknown'] += 1
                metric['unknown_item_ids'].append(item_id)
            else:
                metric['incorrect'] += 1
                metric['incorrect_item_ids'].append(item_id)
    for metric in metrics.values():
        metric['accuracy'] = round(metric['correct'] / metric['total'] if metric['total'] else 0.0, 4)
    return {'schema_version': 'uma.skill_recognition_evaluation.v1', 'sample_count': len(truth.get('items', [])), 'metrics': metrics, 'notes': ['Metrics are measured against the 13-row visual reference, not unit-test count.', 'Auto-confirmed names remain offline suggestions and cannot execute purchases.']}
