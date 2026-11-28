"""Ace career-cycle configuration, post-settlement inspection, and fail-closed graduation checks."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from core.career_workflow import normalize_ss_sieve

ACE_CYCLE_MODE = 'ace_career_cycle'
STATS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
DISTANCES = ('short', 'mile', 'medium', 'long')
SURFACES = ('turf', 'dirt')
RANKS = 'GFEDCBAS'
MAX_TARGET_GRADUATES = 50


def _coerce_int_opt(value):
    if type(value) is int:
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def normalize_ace_cycle(value=None):
    """Validate and normalize ace career-cycle graduation and repetition rules."""
    if value is None:
        return {
            'target_graduates': 1,
            'graduation': {'enabled': False, 'min_stats': {}, 'min_stat_sum': None, 'distances': {}, 'surfaces': {}},
            'ss_sieve': None,
            'restart_after_training_failure': False,
            'training_failure_ignore_wisdom': True,
            'training_failure_stop_at_senior_expedition': True,
        }
    allowed_keys = {
        'target_graduates', 'graduation', 'ss_sieve',
        'restart_after_training_failure',
        'training_failure_ignore_wisdom',
        'training_failure_stop_at_senior_expedition',
    }
    if not isinstance(value, dict) or set(value) - allowed_keys:
        raise ValueError('成品马循环配置包含未知参数。')
    tf_restart = value.get('restart_after_training_failure', False)
    tf_ignore_wisdom = value.get('training_failure_ignore_wisdom', True)
    tf_stop_senior = value.get('training_failure_stop_at_senior_expedition', True)
    if (type(tf_restart) is not bool or type(tf_ignore_wisdom) is not bool
            or type(tf_stop_senior) is not bool):
        raise ValueError('训练失败自动重开开关必须是布尔值。')
    target_graduates = _coerce_int_opt(value.get('target_graduates', 1))
    if target_graduates is None or not 1 <= target_graduates <= MAX_TARGET_GRADUATES:
        raise ValueError(f'目标成品数量请填 1～{MAX_TARGET_GRADUATES} 的整数。')
    raw_grad = value.get('graduation')
    if raw_grad is None:
        raw_grad = {}
    if not isinstance(raw_grad, dict) or set(raw_grad) - {'enabled', 'min_stats', 'min_stat_sum', 'distances', 'surfaces'}:
        raise ValueError('成品毕业筛选配置包含未知参数。')
    enabled = raw_grad.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('成品毕业筛选启用开关必须是布尔值。')
    raw_min_stats = raw_grad.get('min_stats') or {}
    if not isinstance(raw_min_stats, dict) or set(raw_min_stats) - set(STATS):
        raise ValueError('成品毕业五维门槛包含不支持的属性键。')
    min_stats = {}
    for stat, val in raw_min_stats.items():
        if val is None:
            continue
        parsed_stat = _coerce_int_opt(val)
        if parsed_stat is None or not 0 <= parsed_stat <= 3000:
            raise ValueError(f'成品毕业门槛 {stat} 应为 0～3000 的整数。')
        min_stats[stat] = parsed_stat
    raw_sum = raw_grad.get('min_stat_sum')
    if raw_sum is None:
        min_stat_sum = None
    else:
        min_stat_sum = _coerce_int_opt(raw_sum)
        if min_stat_sum is None or not 0 <= min_stat_sum <= 15000:
            raise ValueError('成品毕业五维总和门槛应为 0～15000 的整数或留空。')
    raw_distances = raw_grad.get('distances') or {}
    if not isinstance(raw_distances, dict) or set(raw_distances) - set(DISTANCES):
        raise ValueError('成品毕业距离适性门槛包含不支持的距离键。')
    distances = {}
    for dist, rank in raw_distances.items():
        if not isinstance(rank, str) or len(rank) != 1 or rank not in RANKS:
            raise ValueError(f'成品毕业距离适性 {dist} 等级应为 G～S。')
        distances[dist] = rank
    raw_surfaces = raw_grad.get('surfaces') or {}
    if not isinstance(raw_surfaces, dict) or set(raw_surfaces) - set(SURFACES):
        raise ValueError('成品毕业场地适性门槛包含不支持的场地键。')
    surfaces = {}
    for surf, rank in raw_surfaces.items():
        if not isinstance(rank, str) or len(rank) != 1 or rank not in RANKS:
            raise ValueError(f'成品毕业场地适性 {surf} 等级应为 G～S。')
        surfaces[surf] = rank
    if enabled and not (min_stats or min_stat_sum is not None or distances or surfaces):
        raise ValueError('成品毕业筛选开启后请至少设置一项五维、距离或场地适性门槛。')
    ss_sieve = normalize_ss_sieve(value['ss_sieve']) if value.get('ss_sieve') is not None else None
    return {
        'target_graduates': target_graduates,
        'graduation': {
            'enabled': enabled,
            'min_stats': min_stats,
            'min_stat_sum': min_stat_sum,
            'distances': distances,
            'surfaces': surfaces,
        },
        'ss_sieve': ss_sieve,
        'restart_after_training_failure': tf_restart,
        'training_failure_ignore_wisdom': tf_ignore_wisdom,
        'training_failure_stop_at_senior_expedition': tf_stop_senior,
    }


def _valid_five_stats(candidate) -> dict | None:
    if not isinstance(candidate, dict):
        return None
    extracted = {}
    for stat in STATS:
        val = candidate.get(stat)
        if type(val) is not int or not 0 <= val <= 3000:
            return None
        extracted[stat] = val
    return extracted


_TRAINED_DETAIL_TITLE_TOKENS = {'能力详情', '能力詳情', '优俊少女详情', '優駿少女詳情', '育成优俊少女详情'}
_STAT_HEADER_TOKENS = {'速度', '耐力', '力量', '毅力', '智力'}


def _extract_stats_from_summary(summary: dict) -> dict | None:
    return _valid_five_stats(summary.get('final_stats'))


def _resolve_inside_root(raw_path, root: Path) -> Path | None:
    if not raw_path:
        return None
    try:
        resolved = Path(raw_path).resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_relative_to(root) else None


def _trained_detail_has_layout(small, tokens=None) -> bool:
    import cv2

    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)

    def _green_ratio(region) -> float:
        mask = (
            (region[:, :, 0] >= 30)
            & (region[:, :, 0] <= 80)
            & (region[:, :, 1] >= 80)
            & (region[:, :, 2] >= 120)
        )
        return float(mask.mean())

    top_green = _green_ratio(hsv[15:78, 180:540])
    hdr_green = _green_ratio(hsv[302:336, 24:695])
    tok_list = tokens or []
    has_title = any(
        re.sub(r'\s+', '', str(t.get('text') or '')) in _TRAINED_DETAIL_TITLE_TOKENS
        for t in tok_list
        if isinstance(t, dict)
    )
    stat_hdr_hits = len({
        re.sub(r'\s+', '', str(t.get('text') or ''))
        for t in tok_list
        if isinstance(t, dict)
        and re.sub(r'\s+', '', str(t.get('text') or '')) in _STAT_HEADER_TOKENS
        and isinstance(t.get('bbox'), (list, tuple))
        and len(t['bbox']) == 4
        and 295 <= t['bbox'][1] <= 345
    })
    return (top_green >= 0.40 or has_title) and (hdr_green >= 0.35 or stat_hdr_hits >= 2)


def _pad_and_upscale_stat_crop(crop):
    import cv2

    padded = cv2.copyMakeBorder(crop, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=(255, 255, 255))
    return cv2.resize(padded, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)


def _isolate_stat_brown_ink(crop):
    out = crop.copy()
    ink_mask = (out[:, :, 0] < 130) & (out[:, :, 1] < 135) & (out[:, :, 2] < 155)
    out[~ink_mask] = (255, 255, 255)
    return _pad_and_upscale_stat_crop(out)


def read_trained_detail_stats(image, tokens=None, *, stat_reader=None) -> dict | None:
    """Extract all 5 final stats from the fixed pure-numeric ROIs on trained_detail."""
    import cv2

    if image is None or getattr(image, 'ndim', 0) != 3:
        return None
    if image.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
        return None
    small = image if image.shape[:2] == (1280, 720) else cv2.resize(image, (720, 1280))
    if not _trained_detail_has_layout(small, tokens):
        return None

    from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER, _confidence_is_valid

    reader = stat_reader or _CALENDAR_REGION_READER
    x_start = 24.0
    col_w = (695.0 - x_start) / 5.0
    stats = {}
    for idx, stat in enumerate(STATS):
        cx0 = int(round(x_start + idx * col_w))
        cx1 = int(round(x_start + (idx + 1) * col_w))
        primary_crop = small[338:384, cx0 + 54:cx1 - 5]
        wider_crop = small[338:384, cx0 + 50:cx1 - 3]
        if primary_crop.size == 0 or wider_crop.size == 0:
            return None
        variants = (
            _pad_and_upscale_stat_crop(primary_crop),
            _pad_and_upscale_stat_crop(wider_crop),
            _isolate_stat_brown_ink(primary_crop),
        )
        value = None
        for variant in variants:
            try:
                text, conf = reader.read_line(variant)
            except Exception:
                continue
            if not _confidence_is_valid(conf) or float(conf) < 0.85:
                continue
            cleaned = re.sub(r'\s+', '', text or '')
            if not re.fullmatch(r'[0-9]{2,4}', cleaned):
                continue
            parsed = int(cleaned)
            if 0 <= parsed <= 3000:
                value = parsed
                break
        if value is None:
            return None
        stats[stat] = value
    return stats


def inspect_completed_career(run_root, *, token_reader=None, rank_reader=None, stat_reader=None) -> dict:
    """Extract final 5-stat, distance-aptitude, and surface-aptitude facts from verified run artifacts."""
    root = Path(run_root).resolve()
    stats = {}
    distances = {}
    surfaces = {}
    sources = {}

    injected_path = root / 'session/ace_final_facts.json'
    if injected_path.is_file():
        injected = _read_json(injected_path)
        if injected.get('run_session_id') == root.name:
            if isinstance(injected.get('stats'), dict):
                valid_stats = {
                    k: v for k, v in injected['stats'].items()
                    if k in STATS and type(v) is int and 0 <= v <= 3000
                }
                if valid_stats:
                    stats = valid_stats
                    sources['stats'] = str(injected_path)
            if isinstance(injected.get('distances'), dict):
                valid_dist = {
                    k: v for k, v in injected['distances'].items()
                    if k in DISTANCES and isinstance(v, str) and len(v) == 1 and v in RANKS
                }
                if valid_dist:
                    distances = valid_dist
                    sources['distances'] = str(injected_path)
            if isinstance(injected.get('surfaces'), dict):
                valid_surf = {
                    k: v for k, v in injected['surfaces'].items()
                    if k in SURFACES and isinstance(v, str) and len(v) == 1 and v in RANKS
                }
                if valid_surf:
                    surfaces = valid_surf
                    sources['surfaces'] = str(injected_path)

    def _merge_frame_aptitudes(image, tokens, frame_path: Path, need_dist: bool, need_surf: bool):
        import cv2
        from core.auto_trainer.cut_condition_probe import ocr_tokens
        from scripts.larc_continuation_inspection import read_aptitudes, read_surface_aptitudes

        base_small = cv2.resize(image, (720, 1280))
        retry_inputs = None

        def _sharpened_retry_inputs():
            nonlocal retry_inputs
            if retry_inputs is None:
                sharpened = cv2.addWeighted(
                    base_small,
                    1.5,
                    cv2.GaussianBlur(base_small, (0, 0), 1.0),
                    -0.5,
                    0,
                )
                retry_inputs = (sharpened, ocr_tokens(sharpened, token_reader))
            return retry_inputs

        if need_dist:
            parsed_dist = read_aptitudes(image, tokens, rank_reader=rank_reader)
            valid_parsed = {
                k: v for k, v in (parsed_dist or {}).items()
                if k in DISTANCES and isinstance(v, str) and len(v) == 1 and v in RANKS
            }
            if len(valid_parsed) < len(DISTANCES):
                sharpened, retry_tokens = _sharpened_retry_inputs()
                retry_dist = read_aptitudes(sharpened, retry_tokens, rank_reader=rank_reader)
                for k, v in (retry_dist or {}).items():
                    if k in DISTANCES and isinstance(v, str) and len(v) == 1 and v in RANKS:
                        valid_parsed.setdefault(k, v)
            if valid_parsed:
                for k, v in valid_parsed.items():
                    distances.setdefault(k, v)
                sources.setdefault('distances', str(frame_path))
        if need_surf:
            parsed_surf = read_surface_aptitudes(image, tokens, rank_reader=rank_reader)
            valid_surf = {
                k: v for k, v in (parsed_surf or {}).items()
                if k in SURFACES and isinstance(v, str) and len(v) == 1 and v in RANKS
            }
            if len(valid_surf) < len(SURFACES):
                sharpened, retry_tokens = _sharpened_retry_inputs()
                retry_surf = read_surface_aptitudes(sharpened, retry_tokens, rank_reader=rank_reader)
                for k, v in (retry_surf or {}).items():
                    if k in SURFACES and isinstance(v, str) and len(v) == 1 and v in RANKS:
                        valid_surf.setdefault(k, v)
            if valid_surf:
                for k, v in valid_surf.items():
                    surfaces.setdefault(k, v)
                sources.setdefault('surfaces', str(frame_path))

    # Priority 2: Post-settlement trained_detail frames (do NOT require complete status;
    # pending/interrupted settlements that already captured trained_detail remain valid).
    if not stats or not distances or not surfaces:
        settle_state_path = root / 'session/larc_terminal_settlement.json'
        settle_state = _read_json(settle_state_path) if settle_state_path.is_file() else {}
        report_path = None
        if settle_state.get('run_session_id') in (None, root.name) and settle_state.get('report_path'):
            report_path = _resolve_inside_root(settle_state['report_path'], root)
        if report_path is None:
            candidate_report = root / 'z/report.json'
            if candidate_report.is_file():
                candidate_doc = _read_json(candidate_report)
                if candidate_doc.get('run_session_id') in (None, root.name):
                    report_path = candidate_report
        if report_path is not None:
            report = _read_json(report_path)
            if report.get('run_session_id') in (None, root.name):
                import cv2
                from core.auto_trainer.cut_condition_probe import ocr_tokens

                for row in reversed(report.get('records') or []):
                    if not isinstance(row, dict) or row.get('source_page') != 'trained_detail':
                        continue
                    frame_path = _resolve_inside_root(row.get('source_frame'), root)
                    if frame_path is None:
                        continue
                    image = cv2.imread(str(frame_path))
                    if image is None or image.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
                        continue
                    base_small = cv2.resize(image, (720, 1280))
                    tokens = ocr_tokens(base_small, token_reader)
                    if not stats:
                        parsed_stats = read_trained_detail_stats(image, tokens, stat_reader=stat_reader)
                        if parsed_stats is not None:
                            stats = parsed_stats
                            sources['stats'] = str(frame_path)
                    _merge_frame_aptitudes(
                        image,
                        tokens,
                        frame_path,
                        need_dist=not (set(DISTANCES) <= set(distances)),
                        need_surf=not (set(SURFACES) <= set(surfaces)),
                    )
                    if (
                        stats
                        and set(DISTANCES) <= set(distances)
                        and set(SURFACES) <= set(surfaces)
                    ):
                        break

    # Priority 3: Post-race finish_ready screenshot from s/report.json.
    if not stats or not (set(DISTANCES) <= set(distances)) or not (set(SURFACES) <= set(surfaces)):
        finish_state_path = root / 'session/larc_terminal_finish.json'
        finish_state = _read_json(finish_state_path) if finish_state_path.is_file() else {}
        skills_report_path = None
        if finish_state.get('run_session_id') in (None, root.name) and finish_state.get('skills_report'):
            skills_report_path = _resolve_inside_root(finish_state['skills_report'], root)
        if skills_report_path is None:
            candidate_skills = root / 's/report.json'
            if candidate_skills.is_file():
                candidate_doc = _read_json(candidate_skills)
                if candidate_doc.get('run_session_id') in (None, root.name):
                    skills_report_path = candidate_skills
        if skills_report_path is not None:
            skills_report = _read_json(skills_report_path)
            after = skills_report.get('after') if isinstance(skills_report.get('after'), dict) else {}
            if after.get('page') == 'finish_ready':
                finish_path = _resolve_inside_root(after.get('screenshot_path'), root)
                if finish_path is not None:
                    import cv2
                    from core.auto_trainer.cut_condition_probe import ocr_tokens

                    finish_img = cv2.imread(str(finish_path))
                    if finish_img is not None and finish_img.shape[1::-1] in {(720, 1280), (1440, 2560)}:
                        finish_tokens = ocr_tokens(cv2.resize(finish_img, (720, 1280)), token_reader)
                        _merge_frame_aptitudes(
                            finish_img,
                            finish_tokens,
                            finish_path,
                            need_dist=not (set(DISTANCES) <= set(distances)),
                            need_surf=not (set(SURFACES) <= set(surfaces)),
                        )
                        if not stats:
                            parsed_stats = read_trained_detail_stats(
                                finish_img, finish_tokens, stat_reader=stat_reader
                            )
                            if parsed_stats is not None:
                                stats = parsed_stats
                                sources['stats'] = str(finish_path)
                            else:
                                try:
                                    from core.auto_trainer.main_menu_state_provider import provide_main_menu_state

                                    provided = provide_main_menu_state(finish_path, include_ocr=False)
                                    parsed = _valid_five_stats(
                                        (provided.get('state_context') or {}).get('current_stats')
                                        if isinstance(provided, dict)
                                        else None
                                    )
                                    if parsed is not None:
                                        stats = parsed
                                        sources['stats'] = str(finish_path)
                                except Exception:
                                    pass

    # Priority 4: Summary final_stats only (NEVER fall back to pre-L'Arc menu_060.json / turns).
    if not stats:
        summaries = sorted(
            root.glob('autonomous_*/summary.json'),
            key=lambda p: (p.stat().st_mtime, p.parent.name),
            reverse=True,
        )
        for summary_path in summaries:
            extracted = _extract_stats_from_summary(_read_json(summary_path))
            if extracted is not None:
                stats = extracted
                sources['stats'] = str(summary_path)
                break

    return {
        'run_session_id': root.name,
        'stats': stats,
        'distances': distances,
        'surfaces': surfaces,
        'sources': sources,
    }


def evaluate_graduation(ace_config, facts) -> dict:
    """Evaluate graduation criteria conservatively (fail-closed -> unknown preserves horse)."""
    cfg = normalize_ace_cycle(ace_config)['graduation']
    facts_copy = deepcopy(facts or {})
    if not cfg['enabled']:
        return {'status': 'graduated', 'enabled': False, 'checks': [], 'facts': facts_copy}

    stats = facts_copy.get('stats') or {}
    distances = facts_copy.get('distances') or {}
    checks = []

    for stat, minimum in cfg['min_stats'].items():
        actual = stats.get(stat)
        passed = (actual >= minimum) if (type(actual) is int and 0 <= actual <= 3000) else None
        checks.append({'kind': 'min_stat', 'stat': stat, 'expected': minimum, 'actual': actual, 'passed': passed})

    if cfg['min_stat_sum'] is not None:
        if all(type(stats.get(stat)) is int and 0 <= stats[stat] <= 3000 for stat in STATS):
            actual_sum = sum(stats[stat] for stat in STATS)
            passed = actual_sum >= cfg['min_stat_sum']
        else:
            actual_sum = None
            passed = None
        checks.append({'kind': 'min_stat_sum', 'expected': cfg['min_stat_sum'], 'actual': actual_sum, 'passed': passed})

    for dist, req_rank in cfg['distances'].items():
        actual_rank = distances.get(dist)
        if isinstance(actual_rank, str) and len(actual_rank) == 1 and actual_rank in RANKS:
            passed = RANKS.index(actual_rank) >= RANKS.index(req_rank)
        else:
            actual_rank = actual_rank if actual_rank in tuple(RANKS) else None
            passed = None
        checks.append({'kind': 'distance_min', 'distance': dist, 'expected': req_rank, 'actual': actual_rank, 'passed': passed})

    surfaces = facts_copy.get('surfaces') or {}
    for surf, req_rank in cfg['surfaces'].items():
        actual_rank = surfaces.get(surf)
        if isinstance(actual_rank, str) and len(actual_rank) == 1 and actual_rank in RANKS:
            passed = RANKS.index(actual_rank) >= RANKS.index(req_rank)
        else:
            actual_rank = actual_rank if actual_rank in tuple(RANKS) else None
            passed = None
        checks.append({'kind': 'surface_min', 'surface': surf, 'expected': req_rank, 'actual': actual_rank, 'passed': passed})

    if any(row['passed'] is False for row in checks):
        status = 'unqualified'
    elif any(row['passed'] is None for row in checks):
        status = 'unknown'
    else:
        status = 'graduated'

    return {'status': status, 'enabled': True, 'checks': checks, 'facts': facts_copy}
