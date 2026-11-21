"""Ace career-cycle configuration, post-settlement inspection, and fail-closed graduation checks."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from core.career_workflow import normalize_ss_sieve

ACE_CYCLE_MODE = 'ace_career_cycle'
STATS = ('speed', 'stamina', 'power', 'guts', 'wisdom')
DISTANCES = ('short', 'mile', 'medium', 'long')
RANKS = 'GFEDCBAS'
MAX_TARGET_GRADUATES = 50


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
            'graduation': {'enabled': False, 'min_stats': {}, 'min_stat_sum': None, 'distances': {}},
            'ss_sieve': None,
        }
    if not isinstance(value, dict) or set(value) - {'target_graduates', 'graduation', 'ss_sieve'}:
        raise ValueError('成品马循环配置包含未知参数。')
    target_graduates = value.get('target_graduates', 1)
    if type(target_graduates) is not int or not 1 <= target_graduates <= MAX_TARGET_GRADUATES:
        raise ValueError(f'目标成品数量请填 1～{MAX_TARGET_GRADUATES} 的整数。')
    raw_grad = value.get('graduation')
    if raw_grad is None:
        raw_grad = {}
    if not isinstance(raw_grad, dict) or set(raw_grad) - {'enabled', 'min_stats', 'min_stat_sum', 'distances'}:
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
        if type(val) is not int or not 0 <= val <= 3000:
            raise ValueError(f'成品毕业门槛 {stat} 应为 0～3000 的整数。')
        min_stats[stat] = val
    min_stat_sum = raw_grad.get('min_stat_sum')
    if min_stat_sum is not None and (type(min_stat_sum) is not int or not 0 <= min_stat_sum <= 15000):
        raise ValueError('成品毕业五维总和门槛应为 0～15000 的整数或留空。')
    raw_distances = raw_grad.get('distances') or {}
    if not isinstance(raw_distances, dict) or set(raw_distances) - set(DISTANCES):
        raise ValueError('成品毕业距离适性门槛包含不支持的距离键。')
    distances = {}
    for dist, rank in raw_distances.items():
        if not isinstance(rank, str) or len(rank) != 1 or rank not in RANKS:
            raise ValueError(f'成品毕业距离适性 {dist} 等级应为 G～S。')
        distances[dist] = rank
    if enabled and not (min_stats or min_stat_sum is not None or distances):
        raise ValueError('成品毕业筛选开启后请至少设置一项五维或距离适性门槛。')
    ss_sieve = normalize_ss_sieve(value['ss_sieve']) if value.get('ss_sieve') is not None else None
    return {
        'target_graduates': target_graduates,
        'graduation': {
            'enabled': enabled,
            'min_stats': min_stats,
            'min_stat_sum': min_stat_sum,
            'distances': distances,
        },
        'ss_sieve': ss_sieve,
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


def _extract_stats_from_summary(summary: dict) -> dict | None:
    direct = _valid_five_stats(summary.get('final_stats'))
    if direct is not None:
        return direct
    for turn in reversed(summary.get('turns') or []):
        if not isinstance(turn, dict):
            continue
        for candidate in (
            turn.get('stats'),
            turn.get('current_stats'),
            (turn.get('main_menu_state') or {}).get('current_stats'),
            (turn.get('main_menu_state') or {}).get('stats'),
            (turn.get('provider') or {}).get('stats'),
            ((turn.get('provider') or {}).get('state_context') or {}).get('current_stats'),
        ):
            parsed = _valid_five_stats(candidate)
            if parsed is not None:
                return parsed
    return None


def inspect_completed_career(run_root, *, token_reader=None, rank_reader=None) -> dict:
    """Extract final 5-stat and distance-aptitude facts from verified run artifacts."""
    root = Path(run_root).resolve()
    stats = {}
    distances = {}
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

    if not stats:
        menus = sorted(
            root.glob('autonomous_*/menu_*.json'),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )
        for menu_path in menus:
            menu = _read_json(menu_path)
            parsed = _valid_five_stats((menu.get('state_context') or {}).get('current_stats'))
            if parsed is not None:
                stats = parsed
                sources['stats'] = str(menu_path)
                break

    if not distances:
        settle_state_path = root / 'session/larc_terminal_settlement.json'
        settle_state = _read_json(settle_state_path) if settle_state_path.is_file() else {}
        if settle_state.get('status') == 'complete' and settle_state.get('report_path'):
            try:
                report_path = Path(settle_state['report_path']).resolve(strict=True)
            except OSError:
                report_path = None
            if report_path is not None and report_path.is_relative_to(root):
                report = _read_json(report_path)
                for row in reversed(report.get('records') or []):
                    if not isinstance(row, dict):
                        continue
                    if row.get('source_page') not in {'trained_detail', 'training_complete'}:
                        continue
                    frame_raw = row.get('source_frame')
                    if not frame_raw:
                        continue
                    try:
                        frame_path = Path(frame_raw).resolve(strict=True)
                    except OSError:
                        continue
                    if not frame_path.is_relative_to(root):
                        continue
                    import cv2
                    from core.auto_trainer.cut_condition_probe import ocr_tokens
                    from scripts.larc_continuation_inspection import read_aptitudes

                    image = cv2.imread(str(frame_path))
                    if image is None or image.shape[1::-1] not in {(720, 1280), (1440, 2560)}:
                        continue
                    tokens = ocr_tokens(cv2.resize(image, (720, 1280)), token_reader)
                    parsed_dist = read_aptitudes(image, tokens, rank_reader=rank_reader)
                    valid_parsed = {
                        k: v for k, v in (parsed_dist or {}).items()
                        if k in DISTANCES and isinstance(v, str) and len(v) == 1 and v in RANKS
                    }
                    if valid_parsed:
                        distances.update(valid_parsed)
                        sources['distances'] = str(frame_path)
                        if set(DISTANCES) <= set(distances):
                            break

    return {
        'run_session_id': root.name,
        'stats': stats,
        'distances': distances,
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

    if any(row['passed'] is False for row in checks):
        status = 'unqualified'
    elif any(row['passed'] is None for row in checks):
        status = 'unknown'
    else:
        status = 'graduated'

    return {'status': status, 'enabled': True, 'checks': checks, 'facts': facts_copy}
