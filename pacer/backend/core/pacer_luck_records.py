"""Read compact luck inputs from already saved run records, without game I/O."""

from functools import lru_cache
from itertools import islice
import json
import re
from pathlib import Path

from core.auto_trainer.career_review_export import SCHEMA, TABS, _state, _training
from core.auto_trainer.cut_training_failure import completed_training_failure
from core.auto_trainer.larc_calendar import period_ordinal
from core.auto_trainer.pacer_storage import _io
from core.auto_trainer.run_support_profile import canonical_type


_SEGMENT = re.compile(r'autonomous_[A-Za-z0-9_]{1,64}')
_TURN = re.compile(r'turn_(\d{1,3})(?:_[A-Za-z0-9_]{1,64})?')
_COMPACT_GROUP = re.compile(r't(\d{3,})_[0-9a-fA-F]{4,16}')
_AFTER = re.compile(r'(?:route|event)_\d{2,3}')
_HASH = re.compile(r'[0-9a-fA-F]{64}')
_SIZE = 4 * 1024 * 1024
_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_OBSERVATIONS = 128


def _within_run(path, root):
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _read(path, root, remaining, *, limit=_SIZE):
    """Read one owned JSON object, with a shared bound even if the file grows."""
    if remaining[0] <= 0 or not _within_run(path, root):
        return {}
    try:
        with _io(path).open('rb') as source:
            allowed = min(remaining[0], limit)
            raw = source.read(allowed + 1)
        remaining[0] -= len(raw)
        if len(raw) > allowed:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def _directories(path, root, pattern, limit):
    if not _within_run(path, root):
        return []
    try:
        return sorted((path / item.name for item in islice(_io(path).iterdir(), limit)
                       if pattern.fullmatch(item.name) and item.is_dir()), reverse=True)
    except OSError:
        return []


def _stamp(path, run):
    if not _within_run(path, run):
        return None
    try:
        info = _io(path).stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


def _digest(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _valid_group_id(value, segment_name, turn, expected_group=None):
    """Accept legacy `{segment}_turn{N}` or 0.6.14 compact `t{N:03d}_{hex}` group IDs."""
    if not isinstance(value, str) or not value:
        return False
    if expected_group is not None:
        return value == expected_group
    if segment_name is not None and value == f'{segment_name}_turn{turn}':
        return True
    match = _COMPACT_GROUP.fullmatch(value)
    return match is not None and int(match.group(1)) == turn


def _ss_preview_summary(preview):
    preview = _mapping(preview)
    if preview.get('status') != 'accepted':
        return None
    kind = preview.get('kind')
    if kind not in ('SS', 'SSS'):
        return None
    ready_count = preview.get('ready_count')
    obs = _mapping(preview.get('participants_observation'))
    parts = obs.get('participants') if isinstance(obs.get('participants'), list) else preview.get('participants')
    parts = [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []
    if type(ready_count) is not int or not 1 <= ready_count <= 5:
        ready_count = len(parts) if 1 <= len(parts) <= 5 else None
    if ready_count is None:
        return None
    win_policy = _mapping(preview.get('win_policy'))
    guar_count = sum(1 for p in parts if p.get('win_guaranteed') is True)
    if type(win_policy.get('guaranteed_participants')) is int:
        guar_count = max(guar_count, win_policy['guaranteed_participants'])
    guaranteed = bool(win_policy.get('guaranteed') is True or (len(parts) == ready_count and guar_count == ready_count))
    member_ids = [
        str(_mapping(p.get('member') or p.get('member_ref')).get('member_id') or '')
        for p in parts
    ]
    return {
        'status': 'accepted',
        'kind': kind,
        'ready_count': ready_count,
        'guaranteed': guaranteed,
        'guaranteed_count': min(ready_count, guar_count),
        'member_signature': '|'.join(m for m in member_ids if m),
    }


def _preview(state, options, run_id, turn, group=None, *, segment_name=None):
    scenario = _mapping(state.get('scenario_state'))
    period = _mapping(scenario.get('larc_period'))
    date = _mapping(period.get('value'))
    if isinstance(options, dict):
        options = list(options.values())
    is_pre_debut = date.get('pre_debut') is True and date.get('year_index') == 1
    has_calendar = period_ordinal(date) is not None
    if (state.get('turn', state.get('turn_index')) != turn or scenario.get('scenario') != 'larc'
            or period.get('status') not in ('verified', 'accepted') or not (has_calendar or is_pre_debut)
            or not isinstance(options, list) or len(options) != len(TABS)
            or any(not isinstance(option, dict) or option.get('training_type') not in TABS for option in options)
            or {option.get('training_type') for option in options} != set(TABS)):
        return None
    seen_group = group
    for option in options:
        capture = _mapping(_mapping(option.get('raw_data')).get('capture'))
        actual_group = capture.get('actual_group_id')
        if (capture.get('run_session_id') != run_id or capture.get('turn_index') != turn
                or not _valid_group_id(actual_group, segment_name, turn, seen_group)
                or not _digest(capture.get('sha256'))):
            return None
        if seen_group is None:
            seen_group = actual_group
    try:
        ss_prev = _ss_preview_summary(scenario.get('larc_ss_preview'))
        period_dict = (
            {'stage': 'junior', 'year_index': 1, 'pre_debut': True}
            if is_pre_debut
            else {key: date[key] for key in ('year_index', 'month', 'half')}
        )
        return (
            {**_state(state), 'period': period_dict},
            [_training(option) for option in options],
            ss_prev,
        )
    except (ValueError, KeyError, AttributeError, TypeError, RecursionError):
        return None


def _ss_snapshot_data(folder, run, turn, remaining):
    snap = _read(folder / 'capture/ss_comparison_snapshot.json', run, remaining)
    if snap.get('schema_version') != 'larc_ss_comparison_snapshot.v1':
        return {}
    state = _mapping(snap.get('state'))
    src = _mapping(_mapping(snap.get('preview')).get('source'))
    if state.get('turn', state.get('turn_index')) != turn or src.get('run_session_id', run.name) != run.name:
        return {}
    return snap


def _pipeline_ss_preview(folder, run, remaining):
    report = _mapping(_read(folder / 'pipeline_report.json', run, remaining).get('report'))
    if any(report.get(key) not in (None, run.name) for key in ('run_id', 'run_session_id')):
        return None
    stages = _mapping(report.get('stages'))
    comparison = _mapping(_mapping(stages.get('larc_ss')).get('comparison'))
    return _ss_preview_summary(comparison.get('preview'))


def _outcome(folder, run, turn, target, observation_id, remaining):
    outcomes = set()
    for child in _directories(folder / 'after', run, _AFTER, 40):
        report = _read(child / 'report.json', run, remaining, limit=768 * 1024)
        if report.get('is_valid') is not True or not isinstance(report.get('step_reports'), list):
            continue
        if child.name.startswith('event_'):
            try:
                failure = completed_training_failure(report, run.name)
            except (AttributeError, TypeError):
                continue
            if failure and failure.get('action_turn') == turn and _digest(failure.get('source_sha256')):
                outcomes.add('failure')
            continue
        if _mapping(report.get('route_transition')).get('run_session_id') != run.name:
            continue
        for step in report.get('step_reports') or []:
            result = _mapping(_mapping(step).get('result'))
            frame = _mapping(result.get('frame_safety'))
            evidence = _mapping(_mapping(result.get('decision')).get('evidence'))
            graph = _mapping(evidence.get('screen_graph'))
            if graph.get('node_id') == 'training_success_result_dialogue' and _digest(frame.get('source_image_sha256')):
                outcomes.add('success')
    if len(outcomes) != 1:
        return None
    return {'schema_version': 'uma.training_outcome.v1', 'status': 'confirmed', 'basis': 'observed_training_result',
            'run_id': run.name, 'turn_index': turn, 'target': target, 'observation_id': observation_id,
            'outcome': next(iter(outcomes))}


def _observation(run, folder, turn, next_turn, remaining):
    observation_id = folder.relative_to(run).as_posix()
    segment_name = folder.parent.name
    state = _read(folder / 'decision/turns/turn_000/state.json', run, remaining)
    preview = _preview(state, state.get('training_options'), run.name, turn, segment_name=segment_name)
    basis = 'decision_state'
    ss_snap = None
    if preview is None:
        ss_snap = _ss_snapshot_data(folder, run, turn, remaining)
        if ss_snap:
            ss_state = _mapping(ss_snap.get('state'))
            preview = _preview(ss_state, ss_state.get('training_options'), run.name, turn, segment_name=segment_name)
            if preview is not None:
                basis = 'ss_comparison_snapshot'
    if preview is None:
        sidecar = _read(folder / 'capture/training_options.generated.json', run, remaining)
        sidecar_group = sidecar.get('actual_group_id')
        if (sidecar.get('schema_version') == 'ug_parent_training_options_sidecar.v1'
                and sidecar.get('is_valid') is True
                and _valid_group_id(sidecar_group, segment_name, turn)):
            preview = _preview(
                _mapping(sidecar.get('state_context')),
                sidecar.get('training_options'),
                run.name,
                turn,
                group=sidecar_group,
                segment_name=segment_name,
            )
            if preview is not None:
                basis = 'capture_sidecar'
    if preview is None:
        return None
    before, trainings, ss_prev = preview
    if ss_prev is None:
        if ss_snap is None:
            ss_snap = _ss_snapshot_data(folder, run, turn, remaining)
        if ss_snap:
            ss_prev = _ss_preview_summary(ss_snap.get('preview'))
    if ss_prev is None and basis == 'capture_sidecar':
        ss_prev = _pipeline_ss_preview(folder, run, remaining)
    row = {
        'observation_id': observation_id,
        'turn_index': turn,
        'consumed': False,
        'training_basis': basis,
        'state_before': before,
        'trainings': trainings,
    }
    if ss_prev is not None:
        row['ss_preview'] = ss_prev
    receipt = _read(folder / 'action_receipt.json', run, remaining, limit=128 * 1024)
    target = receipt.get('target')
    action = receipt.get('action')
    reason = receipt.get('reason')
    action_id = receipt.get('action_id')
    if turn < next_turn and receipt.get('run_session_id', run.name) == run.name and receipt.get('turn_index', turn) == turn:
        if receipt.get('performed') is True and reason == 'executed':
            row['consumed'] = True
            if target in TABS:
                row.update(kind='train', execution={'performed': True, 'reason': 'executed', 'target': target})
                outcome = _outcome(folder, run, turn, target, observation_id, remaining)
                if outcome is not None:
                    row['training_outcome'] = outcome
            elif action in ('ss', 'larc_ss') or target in ('ss', 'larc_ss'):
                row.update(kind='ss', execution={'performed': True, 'reason': 'executed', 'target': 'ss'})
        elif receipt.get('performed') is True and reason == 'executed_rest_confirmed':
            row['consumed'] = True
            row.update(kind='rest', execution={'performed': True, 'reason': reason, 'target': 'rest'})
        elif receipt.get('performed') is True and reason in ('executed_normal_outing', 'executed_mei_outing'):
            row['consumed'] = True
            row.update(kind='outing', execution={'performed': True, 'reason': reason, 'target': 'outing'})
        elif (action_id == 'safe_advance.larc_ss_confirm' and receipt.get('tap_performed') is True
                and _digest(receipt.get('source_sha256'))):
            row['consumed'] = True
            row.update(kind='ss', execution={'performed': True, 'tap_performed': True, 'reason': 'executed', 'target': 'ss'})
        elif (isinstance(action_id, str) and action_id.startswith('outing.satake_mei.')
                and receipt.get('tap_performed') is True):
            row['consumed'] = True
            row.update(kind='outing', execution={'performed': True, 'tap_performed': True, 'reason': 'executed_mei_outing', 'target': 'outing'})
    return row


_ANCHOR_LEVELS = (1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50)


@lru_cache(maxsize=1)
def _support_library_index():
    path = Path(__file__).resolve().parents[1] / 'data/support_knowledge/cn_larc_end/support_library.json'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        cards = payload.get('cards', []) if isinstance(payload, dict) else []
        return {
            str(card['card_id']): card
            for card in cards
            if isinstance(card, dict) and card.get('card_id') is not None
        }
    except (OSError, ValueError, TypeError):
        return {}


def _infer_card_level(level, limit_breaks, rarity):
    if type(level) is int and 1 <= level <= 50:
        return level
    if type(limit_breaks) is int and 0 <= limit_breaks <= 4:
        base = 20 if rarity == 'R' else 25 if rarity == 'SR' else 30
        return min(50, max(1, base + 5 * limit_breaks))
    return 40 if rarity == 'R' else 45 if rarity == 'SR' else 50


def _eval_effect_at_level(rows, effect_id, level):
    for row in rows:
        if not isinstance(row, list) or len(row) != 12 or row[0] != effect_id:
            continue
        anchors = [
            (lv, v)
            for lv, v in zip(_ANCHOR_LEVELS, row[1:])
            if type(v) is int and v >= 0
        ]
        if not anchors or level < anchors[0][0]:
            return 0
        if level >= anchors[-1][0]:
            return anchors[-1][1]
        for (l0, v0), (l1, v1) in zip(anchors, anchors[1:]):
            if l0 <= level <= l1:
                if l1 == l0:
                    return int(v1)
                return int(v0 + (v1 - v0) * (level - l0) // (l1 - l0))
    return 0


def _lookup_card_luck_params(card_id, *, level=None, limit_breaks=None, rarity=None, card_type=None):
    if card_id in (None, '', 'unknown'):
        return {}
    card = _support_library_index().get(str(card_id))
    if not card:
        return {}
    lv = _infer_card_level(level, limit_breaks, rarity or card.get('rarity'))
    effects = card.get('support_effects_reference') or []
    base_sp = _eval_effect_at_level(effects, 19, lv)
    init_bond = _eval_effect_at_level(effects, 14, lv)
    hint_freq = _eval_effect_at_level(effects, 18, lv)

    unique = card.get('unique_effect_reference')
    unique_sp = 0
    if isinstance(unique, dict) and lv >= int(unique.get('level') or 30):
        for ef in unique.get('effects') or []:
            if not isinstance(ef, dict):
                continue
            etype = ef.get('type')
            val = ef.get('value')
            if type(val) not in (int, float):
                continue
            if etype == 19:
                unique_sp += val
            elif etype == 14:
                init_bond += int(val)
            elif etype == 18:
                hint_freq += int(val)

    eff_sp = (
        round(((100.0 + base_sp) * (100.0 + unique_sp) / 100.0) - 100.0, 2)
        if unique_sp > 0
        else float(base_sp)
    )
    eff_type = card_type or canonical_type(card.get('type'))
    if eff_type in ('friend', 'group'):
        eff_sp = 0.0
    return {
        'specialty_priority': eff_sp,
        'initial_bond': int(init_bond),
        'hint_frequency': int(hint_freq),
        'resolved_level': lv,
    }


def _read_supports(run, goal, remaining):
    candidates = [
        _read(run / 'session/support_card_profile.json', run, remaining, limit=256 * 1024),
        _mapping(_mapping(_read(run / 'session/pacer_center_run.json', run, remaining, limit=256 * 1024).get('setup')).get('support_deck')),
        _mapping(goal.get('support_deck') or goal.get('support_card_profile')),
    ]
    for doc in candidates:
        if not doc or doc.get('run_session_id') not in (None, run.name):
            continue
        cards = doc.get('cards')
        if not isinstance(cards, list) or not cards:
            continue
        normalized = []
        for idx, item in enumerate(cards[:6]):
            if not isinstance(item, dict):
                continue
            ctype = canonical_type(item.get('type') or item.get('card_type'))
            slot = item.get('slot') if type(item.get('slot')) is int and 0 <= item.get('slot') <= 5 else idx
            card_id = item.get('card_id') if isinstance(item.get('card_id'), str) else None
            looked = _lookup_card_luck_params(
                card_id,
                level=item.get('level'),
                limit_breaks=item.get('limit_breaks'),
                rarity=item.get('rarity'),
                card_type=ctype,
            )
            normalized.append({
                'slot': slot,
                'type': ctype,
                'card_id': card_id,
                'character_id': item.get('character_id') if isinstance(item.get('character_id'), str) else None,
                'level': item.get('level') if type(item.get('level')) is int else looked.get('resolved_level'),
                'specialty_priority': item.get('specialty_priority')
                if type(item.get('specialty_priority')) in (int, float) else looked.get('specialty_priority'),
                'initial_bond': item.get('initial_bond')
                if type(item.get('initial_bond')) in (int, float) else looked.get('initial_bond'),
                'hint_frequency': item.get('hint_frequency')
                if type(item.get('hint_frequency')) in (int, float) else looked.get('hint_frequency'),
            })
        if normalized:
            return normalized
    return []


def _career_complete(run, segments, remaining):
    for segment in segments[:4]:
        for name in ('summary.final.json', 'summary.json'):
            summary = _read(segment / name, run, remaining, limit=256 * 1024)
            if not summary or summary.get('run_session_id') not in (None, run.name):
                continue
            if summary.get('career_complete') is True:
                return True
            return False
    return False


def _chronological_key(segment_name, folder_name, turn):
    base = f'turn_{turn:03d}'
    is_recollect = 0 if folder_name == base else 1
    return segment_name, is_recollect, folder_name


def read_current_review(run_root, *, cache=None):
    """Use only this run's existing owned files. Missing proof leaves fields unknown."""
    try:
        run = Path(run_root).resolve(strict=True)
        remaining = [_TOTAL_BYTES]
        progress = _read(run / 'progress.json', run, remaining, limit=128 * 1024)
        goal = _read(run / 'goal.json', run, remaining, limit=512 * 1024)
        next_turn = progress.get('next_turn')
        if (progress.get('run_session_id') != run.name or goal.get('scenario') != 'larc'
                or type(next_turn) is not int or next_turn < 1):
            return None
        if cache is not None and cache.get('run_root') != str(run):
            cache.clear()
            cache['run_root'] = str(run)
        previous = cache.get('turns', {}) if cache is not None else {}
        segments = _directories(run, run, _SEGMENT, 128)
        supports = _read_supports(run, goal, remaining)
        completed = _career_complete(run, segments, remaining)
        entries, observations = {}, {}
        for segment in segments:
            for folder in _directories(segment, run, _TURN, 256):
                if len(entries) >= _MAX_OBSERVATIONS or remaining[0] <= 0:
                    break
                turn = int(_TURN.fullmatch(folder.name)[1])
                if turn > next_turn:
                    continue
                relative = folder.relative_to(run).as_posix()
                stamp = (_stamp(folder / 'decision/turns/turn_000/state.json', run),
                         _stamp(folder / 'capture/ss_comparison_snapshot.json', run),
                         _stamp(folder / 'capture/training_options.generated.json', run),
                         _stamp(folder / 'pipeline_report.json', run),
                         _stamp(folder / 'action_receipt.json', run))
                old = previous.get(relative)
                if old is not None and old[0] == stamp and old[1] is not None:
                    row = old[1]
                    stable = True
                else:
                    row = _observation(run, folder, turn, next_turn, remaining)
                    stable = turn < next_turn - 1 or (
                        turn < next_turn and row is not None and row.get('consumed')
                        and ('training_outcome' in row or row.get('kind') != 'train')
                    )
                entries[relative] = (stamp, row) if stable else (None, None)
                if row is not None:
                    candidates = observations.setdefault(turn, [])
                    candidates.append((_chronological_key(segment.name, folder.name, turn), row))
            if len(entries) >= _MAX_OBSERVATIONS or remaining[0] <= 0:
                break
        if cache is not None:
            cache['turns'] = entries
        rows = [
            row
            for _, candidates in sorted(observations.items())
            for _, row in sorted(candidates, key=lambda item: item[0])
        ]
        return {
            'schema_version': SCHEMA,
            'run': {'run_id': run.name, 'career_complete': completed},
            'goal': {'scenario': 'larc'},
            'supports': supports,
            'observations': rows,
        }
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None
