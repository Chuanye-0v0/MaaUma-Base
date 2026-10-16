"""Receipt and pixel-bound recovery; a pending admission is never submitted twice."""
import hashlib
import json
import re
from pathlib import Path
import cv2
import numpy as np
from core.champions_race.observer import observe
from core.champions_race.task_restart import round_phase

def verify_matching_resume_source(directory, endpoint, event):
    """One non-resource rematch keeps the original matching input and witness."""
    try:
        directory = Path(directory).resolve()
        read = lambda path: json.loads(path.read_text(encoding='utf8'))
        marker = read(directory / 'match_round-submitted.json')
        if marker.get('action_id') != 'match_round' or marker.get('event') != event or marker.get('horse') is not None or (not marker.get('source_frame_sha256')):
            raise ValueError('matching_scope')
        snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
        source = next((s for s in snapshots if s.get('frame_sha256') == marker['source_frame_sha256']))

        def parsed(packet):
            path = Path(packet['screenshot_path']).resolve()
            raw = path.read_bytes()
            if path.parent != directory or packet.get('endpoint') != endpoint or hashlib.sha256(raw).hexdigest() != packet.get('frame_sha256'):
                raise ValueError('frame_binding')
            return observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), packet.get('tokens', []))
        before = parsed(source)
        if before.get('page') != 'champions_board' or before.get('table_verified') is not True or before.get('unmatched') is not True or (before.get('completed_matches') != 0) or ('match_round' not in before.get('actions', {})):
            raise ValueError('unmatched_source')
        records = [read(p) for p in directory.glob('*-receipt.json')]
        receipt = next((r for r in records if r.get('action_id') == 'match_round' and r.get('source_frame_sha256') == marker['source_frame_sha256']))
        for proof in (receipt, receipt.get('action_outcome') or {}):
            if proof.get('execution_status') in {'uncertain', 'unknown', 'partial'}:
                raise ValueError('uncertain_input')
            if ('input_attempts' in proof or 'acknowledged_inputs' in proof) and (proof.get('input_attempts') != 1 or proof.get('acknowledged_inputs') != 1):
                raise ValueError('input_mismatch')
        after = parsed(receipt['observation'])
        if after.get('page') not in {'connection_interrupted', 'champions_matching', 'champions_board'}:
            raise ValueError('matching_result_unknown')
        return receipt
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as error:
        raise RuntimeError('champions_matching_resume_source_unverified') from error

def verify_continue_retry_source(directory, endpoint, event, index, *, source_action=None, presentation_page='champions_match_result', phase=None):
    """One presentation retry requires the original input's real same-page receipt."""
    try:
        directory = Path(directory).resolve()
        read = lambda path: json.loads(path.read_text(encoding='utf8'))
        action = source_action or f'continue_match_result_{index}'
        if presentation_page == 'champions_preview':
            allowed = {f'view_match_result_{index}'} | {f'retry_view_match_result_{attempt}_{index}' for attempt in range(1, 4)}
            control = 'view_match_result'
        elif presentation_page == 'champions_match_result':
            allowed = {f'continue_match_result_{index}'}
            control = 'continue_match_result'
        else:
            raise ValueError('presentation_role')
        if action not in allowed:
            raise ValueError('presentation_action')
        marker = read(directory / (action + '-submitted.json'))
        if type(index) is not int or not 1 <= index <= 5 or marker.get('action_id') != action or (marker.get('event') != event) or (marker.get('horse') is not None) or (not marker.get('source_frame_sha256')):
            raise ValueError('marker_scope')
        snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
        source = next((s for s in snapshots if s.get('frame_sha256') == marker['source_frame_sha256']), None)

        def same_result(observed):
            path = Path(observed['screenshot_path']).resolve()
            raw = path.read_bytes()
            if path.parent != directory or observed.get('endpoint') != endpoint or hashlib.sha256(raw).hexdigest() != observed.get('frame_sha256') or (observed.get('page') != presentation_page) or (observed.get('match_index') not in (None, index)):
                raise ValueError('frame_binding')
            parsed = observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), observed.get('tokens', []))
            if parsed.get('page') != presentation_page or control not in parsed.get('actions', {}):
                raise ValueError('result_role')
            if presentation_page == 'champions_preview' and (parsed.get('match_index') != index or parsed.get('preview', {}).get('view_result_locked') is not False or (phase is not None and parsed.get('event_phase') not in (None, phase))):
                raise ValueError('preview_changed')
        if source is None:
            raise ValueError('source_missing')
        same_result(source)
        receipts = [read(p) for p in directory.glob('*-receipt.json')]
        receipt = next((r for r in receipts if r.get('action_id') == action and r.get('source_frame_sha256') == marker['source_frame_sha256']), None)
        if receipt is None:
            raise ValueError('receipt_missing')

        def known_input(proof):
            if proof.get('execution_status') not in (None, 'executed'):
                raise ValueError('input_unknown')
            if ('input_attempts' in proof or 'acknowledged_inputs' in proof) and (proof.get('input_attempts') != 1 or proof.get('acknowledged_inputs') != 1):
                raise ValueError('input_mismatch')
        known_input(receipt)
        known_input(receipt.get('action_outcome') or {})
        same_result(receipt['observation'])
        last = directory / 'recovery/last-action.json'
        if last.exists():
            proof = read(last)
            if proof.get('execution_status') in {'uncertain', 'unknown', 'partial'}:
                raise ValueError('input_unknown')
            if proof.get('action_id') == action:
                if proof.get('source_frame_sha256') != marker['source_frame_sha256'] or proof.get('observed_page') != presentation_page:
                    raise ValueError('original_progress_changed')
                known_input(proof)
        return receipt
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError('champions_continue_retry_source_unverified') from error

def verify_preview_retry_source(directory, endpoint, event, index, phase, attempt):
    if type(attempt) is not int or not 1 <= attempt <= 3:
        raise RuntimeError('champions_preview_retry_source_unverified')
    try:
        source = f'view_match_result_{index}' if attempt == 1 else f'retry_view_match_result_{attempt - 1}_{index}'
        return verify_continue_retry_source(directory, endpoint, event, index, source_action=source, presentation_page='champions_preview', phase=phase)
    except RuntimeError as error:
        raise RuntimeError('champions_preview_retry_source_unverified') from error

def verify_preview_source(directory, endpoint, event, index, phase=None, *, viewed=False, dismissed=False, standing_action=None):
    """Only this match's native launch/roster receipts authorize its preview."""
    try:
        directory = Path(directory).resolve()
        read = lambda path: json.loads(path.read_text(encoding='utf8'))
        snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
        receipts = [read(p) for p in directory.glob('*-receipt.json')]

        def parsed(observed, *, outcome_expected=False):
            path = Path(observed['screenshot_path']).resolve()
            raw = path.read_bytes()
            if path.parent != directory or observed.get('endpoint') != endpoint or hashlib.sha256(raw).hexdigest() != observed.get('frame_sha256'):
                raise ValueError('frame_binding')
            result = observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), observed.get('tokens', []), outcome_expected=outcome_expected)
            if phase is not None and result.get('event_phase') not in (None, phase):
                raise ValueError('phase_changed')
            return result

        def action_proof(action, page):
            marker = read(directory / (action + '-submitted.json'))
            if marker.get('action_id') != action or marker.get('event') != event or marker.get('horse') is not None or (not marker.get('source_frame_sha256')):
                raise ValueError('marker_scope')
            source = next((s for s in snapshots if s.get('frame_sha256') == marker['source_frame_sha256']), None)
            if source is None:
                raise ValueError('source_missing')
            before = parsed(source, outcome_expected=page == 'champions_outcome')
            if before.get('page') != page:
                raise ValueError('source_role')
            if page == 'champions_board':
                if before.get('table_verified') is not True or before.get('next_match') != index:
                    raise ValueError('match_changed')
            elif page == 'champions_outcome':
                if 'dismiss_match_outcome' not in before.get('actions', {}) or source.get('match_index') not in (None, index):
                    raise ValueError('outcome_source')
            elif before.get('match_index') != index or (page == 'champions_standings' and 'continue_standings' not in before.get('actions', {})) or (page == 'champions_preview' and 'view_match_result' not in before.get('actions', {})):
                raise ValueError('presentation_changed')
            receipt = next((r for r in receipts if r.get('action_id') == action and r.get('source_frame_sha256') == marker['source_frame_sha256']), None)
            if receipt is None:
                raise ValueError('receipt_missing')
            if receipt.get('execution_status') in {'uncertain', 'unknown', 'not_executed', 'partial'}:
                raise ValueError('input_unverified')
            parsed(receipt['observation'], outcome_expected=page == 'champions_preview')
            return receipt
        if type(index) is not int or not 1 <= index <= 5:
            raise ValueError('match_index')
        action_proof(f'launch_match_{index}', 'champions_board')
        standing = standing_action or (f'resume_continue_standings_{index}' if (directory / f'resume_continue_standings_{index}-submitted.json').exists() else f'continue_standings_{index}')
        if standing not in {f'continue_standings_{index}', f'resume_continue_standings_{index}'}:
            raise ValueError('standing_action')
        if standing == f'resume_continue_standings_{index}':
            action_proof(f'resume_match_{index}', 'champions_board')
        receipt = action_proof(standing, 'champions_standings')
        if viewed or dismissed:
            receipt = action_proof(f'view_match_result_{index}', 'champions_preview')
        if dismissed:
            receipt = action_proof(f'dismiss_match_outcome_{index}', 'champions_outcome')
        return receipt
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError('champions_preview_source_unverified') from error

def verify_auto_lineup_source(directory, endpoint, event=None):
    """Prove this reset came from this round's actual incomplete lineup input."""
    try:
        directory = Path(directory).resolve()
        read = lambda path: json.loads(path.read_text(encoding='utf8'))
        marker = read(directory / 'auto_lineup-submitted.json')
        if marker.get('action_id') != 'auto_lineup' or not marker.get('source_frame_sha256') or (event is not None and marker.get('event') != event):
            raise ValueError('marker_scope')
        snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
        before = next((s for s in snapshots if s.get('frame_sha256') == marker['source_frame_sha256']), None)

        def parsed(observed):
            path = Path(observed['screenshot_path']).resolve()
            raw = path.read_bytes()
            if path.parent != directory or observed.get('endpoint') != endpoint or hashlib.sha256(raw).hexdigest() != observed.get('frame_sha256'):
                raise ValueError('frame_binding')
            return observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), observed.get('tokens', []))
        if before is None:
            raise ValueError('source_missing')
        source = parsed(before)
        if before.get('page') != 'champions_lineup' or source.get('page') != 'champions_lineup' or type(before.get('lineup_count')) is not int or (not 0 <= before['lineup_count'] < 3) or (source.get('lineup_count') != before['lineup_count']) or ('auto_lineup' not in source.get('actions', {})):
            raise ValueError('incomplete_lineup_unproven')
        receipts = [read(p) for p in directory.glob('*-receipt.json')]
        receipt = next((r for r in receipts if r.get('action_id') == 'auto_lineup' and r.get('source_frame_sha256') == marker['source_frame_sha256']), None)
        if receipt is None or receipt.get('execution_status') in {'uncertain', 'unknown'} or parsed(receipt['observation']).get('page') != 'champions_auto_confirm':
            raise ValueError('auto_receipt_missing')
        return receipt
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError('champions_auto_lineup_source_unverified') from error

def load_pending(source, endpoint, division=None):

    def read(path):
        data = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(data, dict):
            raise ValueError('object_required')
        return data
    try:
        source = Path(source).resolve()
        book = read(source / 'active.json')
        if read(source / 'context.json') != book:
            raise ValueError('context_changed')
        if book.get('kind') != 'champions_race' or book.get('endpoint') != endpoint or Path(book.get('run', '')).resolve() != source or (book.get('state') not in {'active', 'pending'}) or (division is not None and book.get('division') not in (None, division)) or (not isinstance(book.get('rounds'), list)) or (type(book.get('requested_rounds')) is not int) or (book.get('requested_rounds') not in (1, 2, 3, 4)):
            raise ValueError('journal_scope')

        def frame(data, legacy_auto=False, legacy_preview=False, legacy_outcome=False, legacy_result=False, legacy_active_hub=False, owned_outcome=False):
            path = Path(data['screenshot_path']).resolve()
            raw = path.read_bytes()
            if not path.is_relative_to(source) or data.get('endpoint') != endpoint or hashlib.sha256(raw).hexdigest() != data.get('frame_sha256'):
                raise ValueError('frame_binding')
            parsed = observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), data.get('tokens', []), outcome_expected=owned_outcome)
            if parsed['page'] != data.get('page') and (not (legacy_auto and data.get('page') == 'unknown' and (parsed['page'] == 'champions_auto_confirm') or (legacy_preview and data.get('page') == 'champions_standings' and (parsed['page'] == 'champions_preview')) or (legacy_outcome and data.get('page') == 'unknown' and (parsed['page'] == 'champions_outcome')) or (legacy_result and data.get('page') == 'unknown' and (parsed['page'] in {'champions_match_result', 'champions_promotion'})) or (legacy_active_hub and data.get('page') == 'unknown' and (parsed['page'] == 'champions_active_hub')))):
                raise ValueError('frame_page')
            return parsed

        def validate_round(number, active):
            directory = source / f'round-{number:03d}'
            if active.get('adopted'):
                witness = read(directory / 'adopted.json')
                parsed = frame(witness['source'])
                if witness.get('read_only') is not True or witness.get('endpoint') != endpoint or parsed['page'] not in {'champions_board', 'champions_lineup', 'champions_team_confirm'} or (parsed['page'] == 'champions_board' and parsed.get('table_verified') is not True):
                    raise ValueError('adopted_scope')
            else:
                marker = read(directory / 'confirm_entry-submitted.json')
                snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
                submitted = next((f for f in snapshots if f.get('frame_sha256') == marker.get('source_frame_sha256')), None)
                if submitted is None:
                    raise ValueError('entry_source_missing')
                parsed = frame(submitted)
                quote = parsed.get('entry_resource', {})
                saved = active.get('resource', {})
                if marker.get('action_id') != 'confirm_entry' or marker.get('event', {}).get('competition') != 'champions_race' or parsed['page'] != 'champions_entry_confirm' or any((quote.get(k) != saved.get(k) for k in ('kind', 'cost'))) or (quote.get('kind') == 'free' and quote.get('free_verified') is not True) or (quote.get('kind') == 'ticket' and quote.get('ticket_verified') is not True) or (quote.get('kind') == 'jewels' and (quote.get('jewels_verified') is not True or quote.get('cost') != 30)):
                    raise ValueError('entry_cost_source')
                receipts = [read(p) for p in directory.glob('*-receipt.json')]
                entry = next((r for r in receipts if r.get('action_id') == 'confirm_entry' and r.get('source_frame_sha256') == marker['source_frame_sha256']), None)
                if entry is None:
                    raise ValueError('entry_receipt_missing')
                frame(entry['observation'])
            records = [read(p) for p in directory.glob('*-receipt.json')]
            for receipt in records:
                legacy_auto = False
                legacy_preview = False
                legacy_outcome = False
                owned_outcome = False
                legacy_result = False
                legacy_active_hub = False
                if receipt.get('action_id') == 'open_champions' and receipt.get('observation', {}).get('page') == 'unknown':
                    after = receipt['observation']
                    path = Path(after['screenshot_path'])
                    raw = path.read_bytes()
                    parsed_hub = observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), after.get('tokens', []))
                    legacy_active_hub = parsed_hub.get('page') == 'champions_active_hub' and parsed_hub.get('round_registered') is True and (round_phase(book) is None or parsed_hub.get('event_phase') in (None, round_phase(book))) and (parsed_hub.get('division') in (None, book.get('division')))
                if receipt.get('action_id') == 'auto_lineup' and receipt.get('observation', {}).get('page') == 'unknown':
                    try:
                        proved = verify_auto_lineup_source(directory, endpoint, active.get('event'))
                    except RuntimeError as error:
                        raise ValueError('auto_source_unverified') from error
                    legacy_auto = proved == receipt
                presentation = re.fullmatch('(?:resume_)?continue_standings_([1-5])', receipt.get('action_id', ''))
                if presentation and receipt.get('observation', {}).get('page') == 'champions_standings':
                    after = receipt['observation']
                    path = Path(after['screenshot_path'])
                    raw = path.read_bytes()
                    if observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), after.get('tokens', []))['page'] == 'champions_preview':
                        try:
                            proved = verify_preview_source(directory, endpoint, active.get('event'), int(presentation.group(1)), round_phase(book), standing_action=receipt['action_id'])
                        except RuntimeError as error:
                            raise ValueError('preview_source_unverified') from error
                        legacy_preview = proved == receipt
                viewed = re.fullmatch('view_match_result_([1-5])', receipt.get('action_id', ''))
                retried_view = re.fullmatch('retry_view_match_result_([1-3])_([1-5])', receipt.get('action_id', ''))
                if retried_view and receipt.get('observation', {}).get('page') in {'unknown', 'champions_outcome'}:
                    after = receipt['observation']
                    path = Path(after['screenshot_path'])
                    raw = path.read_bytes()
                    if observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), after.get('tokens', []), outcome_expected=True)['page'] == 'champions_outcome':
                        try:
                            verify_preview_retry_source(directory, endpoint, active.get('event'), int(retried_view.group(2)), round_phase(book), int(retried_view.group(1)))
                        except RuntimeError as error:
                            raise ValueError('preview_retry_outcome_unverified') from error
                        legacy_outcome = True
                        owned_outcome = True
                if viewed and receipt.get('observation', {}).get('page') in {'unknown', 'champions_outcome'}:
                    after = receipt['observation']
                    path = Path(after['screenshot_path'])
                    raw = path.read_bytes()
                    if observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), after.get('tokens', []), outcome_expected=True)['page'] == 'champions_outcome':
                        try:
                            proved = verify_preview_source(directory, endpoint, active.get('event'), int(viewed.group(1)), round_phase(book), viewed=True)
                        except RuntimeError as error:
                            raise ValueError('outcome_source_unverified') from error
                        legacy_outcome = proved == receipt
                        owned_outcome = proved == receipt
                dismissed = re.fullmatch('dismiss_match_outcome_([1-5])', receipt.get('action_id', ''))
                if dismissed and receipt.get('observation', {}).get('page') == 'unknown':
                    after = receipt['observation']
                    path = Path(after['screenshot_path'])
                    raw = path.read_bytes()
                    if observe(raw, cv2.imdecode(np.frombuffer(raw, np.uint8), 1), after.get('tokens', []))['page'] in {'champions_match_result', 'champions_promotion'}:
                        try:
                            proved = verify_preview_source(directory, endpoint, active.get('event'), int(dismissed.group(1)), round_phase(book), dismissed=True)
                        except RuntimeError as error:
                            raise ValueError('result_source_unverified') from error
                        legacy_result = proved == receipt
                frame(receipt['observation'], legacy_auto=legacy_auto, legacy_preview=legacy_preview, legacy_outcome=legacy_outcome, legacy_result=legacy_result, legacy_active_hub=legacy_active_hub, owned_outcome=owned_outcome)
                retried = re.fullmatch('retry_continue_match_result_([1-5])', receipt.get('action_id', ''))
                if retried:
                    try:
                        verify_continue_retry_source(directory, endpoint, active.get('event'), int(retried.group(1)))
                    except RuntimeError as error:
                        raise ValueError('retry_source_unverified') from error
                view_retry = re.fullmatch('retry_view_match_result_([1-3])_([1-5])', receipt.get('action_id', ''))
                if view_retry:
                    try:
                        verify_preview_retry_source(directory, endpoint, active.get('event'), int(view_retry.group(2)), round_phase(book), int(view_retry.group(1)))
                    except RuntimeError as error:
                        raise ValueError('preview_retry_source_unverified') from error
                if receipt.get('execution_status') in {'uncertain', 'unknown'}:
                    raise ValueError('uncertain_receipt')
                if receipt.get('action_id') == 'resume_matching':
                    try:
                        verify_matching_resume_source(directory, endpoint, active.get('event'))
                    except RuntimeError as error:
                        raise ValueError('matching_resume_source') from error
            launched = []
            for marker_path in directory.glob('launch_match_*-submitted.json'):
                marker = read(marker_path)
                action = marker.get('action_id', '')
                if action not in {f'launch_match_{i}' for i in range(1, 6)}:
                    raise ValueError('launch_action')
                index = int(action[-1])
                snapshots = [read(p) for p in directory.glob('*.json') if p.stem.isdigit()]
                before = next((f for f in snapshots if f.get('frame_sha256') == marker.get('source_frame_sha256')), None)
                if before is None:
                    raise ValueError('launch_source_missing')
                parsed = frame(before)
                if parsed.get('table_verified') is not True or parsed.get('next_match') != index or (not any((r.get('action_id') == action and r.get('source_frame_sha256') == marker['source_frame_sha256'] for r in records))):
                    raise ValueError('launch_not_bound')
                launched.append(index)
            if launched:
                active['match_index'] = max(launched)
            if not active.get('adopted') and (not any((r.get('action_id') == 'confirm_entry' for r in records))):
                raise ValueError('entry_missing')
            if active.get('matches') == 5 and (not any((r.get('action_id') == 'claim_round' for r in records))):
                raise ValueError('claim_missing')
            last = directory / 'recovery/last-action.json'
            if last.exists() and read(last).get('execution_status') == 'uncertain':
                raise ValueError('uncertain_input')
        for index, record in enumerate(book['rounds'], 1):
            validate_round(index, record)
        if book.get('started'):
            active = book.get('active_round')
            if not isinstance(active, dict) or active.get('number') != len(book['rounds']) + 1:
                raise ValueError('active_round_scope')
            validate_round(active['number'], active)
        from core.champions_race.task_restart import carried_rounds
        carried_rounds(book, endpoint)
        return book
    except (OSError, ValueError, KeyError, TypeError, StopIteration, AttributeError) as error:
        raise RuntimeError('champions_race_resume_evidence_invalid') from error
