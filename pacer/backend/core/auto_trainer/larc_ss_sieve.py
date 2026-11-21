"""Read-only sieve decisions from the original L'Arc action owners."""

import hashlib
import json
from pathlib import Path


def decide(*, checkpoint, full5_ss, ready_count=None, config):
    if checkpoint not in {'pre_debut_end', 'year1_end'}:
        raise ValueError('unknown_ss_sieve_checkpoint')
    if type(full5_ss) is not int or full5_ss < 0:
        return {'decision': 'continue', 'reason': 'count_unknown', 'known': False}
    threshold = 1 if checkpoint == 'pre_debut_end' else config['year1_min']
    if full5_ss >= threshold:
        return {'decision': 'continue', 'reason': 'completed_full5_threshold', 'known': True}
    if needs_ready_count(checkpoint, full5_ss, config):
        if type(ready_count) is not int or not 0 <= ready_count <= 5:
            return {'decision': 'continue', 'reason': 'ready_count_unknown', 'known': False}
        if ready_count == 5:
            return {'decision': 'continue', 'reason': 'one_short_with_ready_five', 'known': True}
    return {'decision': 'screen_out', 'reason': 'below_full5_threshold', 'known': True}


def needs_ready_count(checkpoint, full5_ss, config):
    return (checkpoint == 'year1_end' and config['allow_ready_five']
            and type(full5_ss) is int and full5_ss == config['year1_min'] - 1)


def _read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _started_here(ctx):
    path = ctx.root / 'session/ordinary_opening_owner.json'
    if not path.is_file():
        return False
    owner = _read(path)
    return (owner.get('run_session_id') == ctx.run_id and owner.get('device') == ctx.progress['device']
            and owner.get('status') == 'complete' and owner.get('new_career_started') is True)


def _participant_count(entry, receipt):
    preview = entry.get('participants_observation') or {}
    source = preview.get('source') or {}
    frame = preview.get('current_frame') or {}
    rows = preview.get('participants') or []
    count = frame.get('expected_count')
    if (preview.get('schema_version') == 'larc_ss_participants.v1'
            and preview.get('layout') == 'ss_preview' and frame.get('status') == 'accepted'
            and type(count) is int and 1 <= count <= 5
            and frame.get('visible_portrait_count') == count
            and [row.get('slot') for row in rows] == list(range(count))
            and source.get('run_session_id') == receipt['run_session_id']
            and source.get('turn_index') == receipt['turn_index']
            and source.get('source_sha256') == receipt['source_sha256']):
        return count
    return None


def full5_prefix(ctx):
    """Count completed five-member actions only across a complete owned prefix."""
    from core.auto_trainer.larc_action_history import ss_deadline_history

    turn = ctx.progress['next_turn']
    try:
        if getattr(ctx, 'ss_sieve_player_takeover', False):
            return None, 'player_takeover_prefix_unverified'
        if not _started_here(ctx):
            return None, 'career_start_unverified'
        history = ss_deadline_history(ctx.root / 'session', ctx.run_id, turn)
        actions = history['actions']
        if (history['status'] != 'accepted'
                or [row['turn_index'] for row in actions] != list(range(1, turn))):
            return None, 'committed_main_prefix_incomplete'
        roster = _read(ctx.roster)
        if roster.get('run_session_id') != ctx.run_id:
            return None, 'ss_roster_run_mismatch'
        executed = roster.get('ss_executions') or []
        participants = roster.get('ss_execution_participants') or []
        seen = set()
        full = 0
        for row in actions:
            if row['action_type'] != 'ss':
                continue
            source = row['source']
            path = Path(source['path']).resolve()
            if not path.is_relative_to(ctx.root.resolve()):
                return None, 'ss_receipt_path_mismatch'
            raw = path.read_bytes()
            receipt = json.loads(raw)
            sha = receipt.get('source_sha256')
            if (hashlib.sha256(raw).hexdigest() != source['receipt_id']
                    or receipt.get('run_session_id') != ctx.run_id
                    or receipt.get('turn_index') != row['turn_index']
                    or receipt.get('action_id') != 'safe_advance.larc_ss_confirm'
                    or receipt.get('tap_performed') is not True or sha in seen
                    or not isinstance(sha, str) or len(sha) != 64 or executed.count(sha) != 1):
                return None, 'committed_ss_receipt_unverified'
            matches = [entry for entry in participants if entry.get('source_sha256') == sha]
            count = (_participant_count(matches[0], receipt)
                     if len(matches) == 1 and matches[0].get('receipt') == receipt else None)
            if count is None:
                return None, 'consumed_ss_member_count_unknown'
            seen.add(sha)
            full += count == 5
        if seen != set(executed):
            return None, 'unmatched_ss_execution_history'
        return full, 'complete_committed_ss_prefix'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, 'committed_ss_evidence_unavailable'


def read_ready_count(raw, *, run_id, turn):
    """Read the existing normal Wisdom capture once, without game input."""
    import cv2
    import numpy as np
    from core.auto_trainer.larc_ss_count import read_larc_ss_count
    from core.training_menu_precheck import TrainingMenuPrechecker

    try:
        if raw.get('run_session_id') != run_id or raw.get('turn_index') != turn:
            return None, None
        path = Path(raw['absolute_path'])
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != raw['sha256']:
            return None, None
        checked = TrainingMenuPrechecker(scenario='larc').check(path)
        if not checked.accepted or checked.selected_tab != 'wisdom':
            return None, None
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        read = read_larc_ss_count(image, stage='entry')
        count = read.get('count')
        if read.get('status') == 'accepted' and type(count) is int and 0 <= count <= 5:
            return count, {'path': str(path), 'sha256': sha, 'run_session_id': run_id,
                           'turn_index': turn, 'stage': 'entry', 'tab': 'wisdom'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, cv2.error):
        pass
    return None, None


def checkpoint_evidence(ctx, checkpoint, *, page_kind, path, sha256, period, unavailable=None):
    full5, reason = (None, unavailable) if unavailable else full5_prefix(ctx)
    turn = ctx.progress['next_turn']
    return {'schema_version': 'larc.ss_sieve_evidence.v1', 'checkpoint': checkpoint,
            'run_id': ctx.run_id, 'turn': turn, 'endpoint': ctx.progress['device'],
            'page_kind': page_kind, 'period': period, 'full5_ss': full5,
            'ready_count': None, 'ready_source': None, 'evidence_reason': reason,
            'source': {'path': str(path), 'sha256': sha256, 'run_session_id': ctx.run_id,
                       'turn_index': turn}}
