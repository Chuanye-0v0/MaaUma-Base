"""Existing-ticket LOH loop; a persisted entry is never submitted twice."""
from __future__ import annotations

from copy import deepcopy
import json
import time

from core.action_recovery import ReobserveBudget, ReobserveRequired
from core.navigation_wait import same_controls, wait_transition

from .policy import choose, normalize_options
from .resume import StateBook


class LohRunner:
    def __init__(self, device, emit, stop_event, *, options=None, resume_from=None,
                 clock=time.monotonic):
        self.device, self.emit, self.stop = device, emit, stop_event
        requested = normalize_options(options)
        self.book = StateBook(device.output_dir, device.endpoint, resume_from=resume_from)
        self.options = normalize_options(self.book.data.get('options', requested))
        self.options['fill_empty_team'] = requested['fill_empty_team']
        self.book.data['options'] = self.options
        # User defaults: native short-mode toggle plus existing hero-linked items.
        self.book.data['prefer_short_mode'] = True
        self.book.data['auto_hero_items'] = True
        self.book.save()
        self.device.book, self.device.options = self.book.data, self.options
        self.device.state_path = str(self.book.path)
        self.device.output_dir = self.book.root
        self.clock = clock
        self.current = {}
        self.wait_started = None
        self.wait_reads = 0
        self.loading_seen = False

    def _capture(self):
        if self.stop.is_set():
            raise RuntimeError('operator_stop')
        self.current = self.device.capture()
        self.book.observe(self.current)
        return self.current

    def _wait(self):
        # Reuse the normal passive transition reader. Neither animated pixels
        # nor repeated OCR results extend this owner's finite wait budget.
        if self.wait_started is None:
            self.wait_started = self.clock()
        if self.current.get('page') == 'loading':
            self.loading_seen = True

        def capture():
            self.wait_reads += 1
            budget = 90 if self.loading_seen else 30
            if self.clock() - self.wait_started >= budget or self.wait_reads > 300:
                raise RuntimeError('loh_transition_wait_exhausted')
            observed = self._capture()
            if observed.get('page') == 'loading':
                self.loading_seen = True
            return observed

        remaining = max(.1, (90 if self.loading_seen else 30) - (self.clock() - self.wait_started))
        self.current = wait_transition(capture, self.stop, self.current, timeout=remaining,
            same_page_ready=lambda value: choose(value, self.book.data, self.options)['kind'] != 'wait')

    def _reset_wait(self):
        self.wait_started, self.wait_reads, self.loading_seen = None, 0, False

    def _result(self, status, reason):
        pending = bool(self.book.data.get('pending'))
        if pending and status in {'done', 'skipped'}:
            status, reason = 'failed', 'loh_pending_admission_unresolved'
        self.book.data['status'], self.book.data['reason'] = status, reason
        storage_error = None
        try:
            self.book.save()
        except (OSError, ValueError, TypeError) as error:
            # The input intent was already journalled before dispatch. A disk
            # failure at handoff must not hide that intent or look completed.
            storage_error = str(error)
            if status != 'cancelled':
                status, reason = 'failed', 'loh_state_save_failed'
        result = dict(kind='loh', status=status, reason=reason,
            ok=status in {'done', 'skipped', 'needs_configuration'},
            game_task_complete=status in {'done', 'skipped'}, skipped=status == 'skipped',
            pending=pending, state_path=str(self.book.path), output_dir=str(self.book.root),
            effective_options=deepcopy(self.options), last_observation=deepcopy(self.current),
            records=deepcopy(self.book.data.get('records', [])))
        if storage_error is not None:
            result['state_persistence_error'] = storage_error
        for key in ('entries_started', 'entries_completed', 'adopted_entries'):
            result[key] = self.book.data.get(key, 0)
        report = self.book.root/'loh-result.json'
        try:
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            result['report_path'] = str(report)
        except (OSError, ValueError, TypeError) as error:
            # The result report is not the admission journal. Do not re-enter a
            # finished game action merely to regenerate this optional copy.
            result['report_error'] = str(error)
        return result

    def run(self):
        action = None
        repeated = {}
        actions_since_completion = 0
        completed = self.book.data.get('entries_completed', 0)
        replans = ReobserveBudget()
        try:
            self._capture()
            while True:
                if self.stop.is_set():
                    return self._result('cancelled', 'operator_stop')
                decision = choose(self.current, self.book.data, self.options)
                if decision['kind'] == 'finish':
                    return self._result(decision['status'], decision['reason'])
                if decision['kind'] == 'wait':
                    self._wait()
                    continue
                action = decision['action_id']
                before = self.current
                signature = (before.get('page'), action)
                repeated[signature] = repeated.get(signature, 0) + 1
                if repeated[signature] > 3:
                    raise RuntimeError('loh_repeated_action_no_progress')
                actions_since_completion += 1
                if actions_since_completion > 200:
                    raise RuntimeError('loh_flow_progress_exhausted')
                self.emit(dict(type='progress', phase=before.get('page'),
                    message='LOH：' + action))
                # Atomic reservation must succeed before the executor sees an
                # admission. Neutral actions use the same journal, not a loop
                # of copied markers or a second resource ledger.
                self.book.reserve(decision, before)
                try:
                    receipt = self.device.step(action, self.stop, before)
                except ReobserveRequired as error:
                    self.book.record_failure(error, action)
                    self.current = replans.accept(error)
                    self.book.observe(self.current)
                    repeated[signature] -= 1
                    continue
                self.book.apply_receipt(receipt)
                if receipt.get('execution_status') == 'executed':
                    replans.executed()
                after = receipt.get('observation')
                self.current = after if isinstance(after, dict) else self._capture()
                # apply_receipt already observes and durably saves its frame;
                # _capture also observes. Repeating that write here delays the
                # short-result skip window without adding a new observation.
                if (self.current.get('page') != before.get('page') or
                        not same_controls(self.current.get('actions', {}), before.get('actions', {}))):
                    repeated.clear()
                    self._reset_wait()
                if self.book.data.get('entries_completed', 0) != completed:
                    completed = self.book.data['entries_completed']
                    actions_since_completion = 0
                    repeated.clear()
                    self._reset_wait()
        except Exception as error:
            # Preserve the original pending entry and action-boundary outcome;
            # failure is task-local and never permission for another ticket.
            try:
                self.book.record_failure(error, action)
            except (OSError, ValueError, TypeError):
                pass
            reason = str(error) if isinstance(error, RuntimeError) else 'loh_execution_failed'
            return self._result('cancelled' if reason.startswith('operator_stop') else 'failed', reason)
