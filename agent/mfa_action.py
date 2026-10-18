import json

from maa.custom_action import CustomAction
from harvest_adapter import HarvestOptions, MaaStopSignal, run_preview


def configuration_failure_can_continue(result):
    """Only a proved pre-device option rejection may fail one queue item."""
    if (not isinstance(result, dict) or result.get('ok') is not False
            or (result.get('reason'), result.get('failure_stage')) not in {
                ('task_options_invalid', 'options_validation'), ('task_assets_invalid', 'asset_validation')}
            or result.get('game_task_complete') is not False
            or type(result.get('device_actions')) is not int or result['device_actions'] != 0
            or result.get('device_action_count_complete') is not True):
        return False
    if any(result.get(key) is not None and result.get(key) is not False for key in (
            'stop_queue', 'pending', 'pending_race', 'pending_exchange', 'pending_spin',
            'pending_setting', 'start_submitted', 'start_confirmed', 'account_conflict',
            'account_paused', 'tap_in_flight', 'uncertain')):
        return False
    outcome = result.get('action_outcome')
    return outcome is None or (isinstance(outcome, dict)
        and outcome.get('execution_status') == 'not_executed'
        and type(outcome.get('input_attempts')) is int and outcome['input_attempts'] == 0
        and type(outcome.get('acknowledged_inputs')) is int and outcome['acknowledged_inputs'] == 0
        and outcome.get('decision') != 'pause_account')


class HarvestAction(CustomAction):
    def run(self, context, argv):
        self.last_result = None
        try:
            options = HarvestOptions.parse(json.loads(argv.custom_action_param))
            def emit(event):
                detail = context.run_task('UmaReceipt', {'UmaReceipt': {
                    'focus': {'Node.Action.Succeeded': event['message']}}})
                if detail is None:
                    raise RuntimeError('receipt_delivery_failed')
            self.last_result = run_preview(options, emit, MaaStopSignal(context.tasker))
            return self.last_result['ok']
        except (ValueError, RuntimeError) as error:
            self.last_result = dict(ok=False, simulated=True, game_task_complete=False,
                                    device_actions=0, reason=str(error))
            return False


class BackendTaskAction(CustomAction):
    """Injected backend for source integration; not registered by the preview entrypoint."""
    def __init__(self, backend, task, *, execute=False):
        super().__init__()
        self.backend, self.task, self.execute = backend, task, execute
        self.last_result = None

    def run(self, context, argv):
        from desktop.receipts import receipt_text
        self.last_result = None
        stop = MaaStopSignal(context.tasker)
        read_generation = getattr(self.backend, 'queue_generation', lambda: None)
        generation = read_generation()
        if generation and generation != getattr(self.backend, 'stopped_generation', None):
            self.backend.queue_stopped = False
        if getattr(self.backend, 'queue_stopped', False):
            self.last_result = dict(ok=False, game_task_complete=False, reason='queue_stopped_restart_required')
            context.tasker.post_stop()
            return False

        def emit(event):
            if stop.is_set():
                return
            text = receipt_text(event, account='当前登录')
            if not text:
                return
            detail = context.run_task('UmaReceipt', {'UmaReceipt': {
                'focus': {'Node.Action.Succeeded': text}}})
            if detail is None:
                raise RuntimeError('receipt_delivery_failed')

        try:
            options = json.loads(argv.custom_action_param)
            execute = options.pop('_execute', self.execute) if isinstance(options, dict) else self.execute
            if type(execute) is not bool:
                raise ValueError('invalid_execution_mode')
            emit(dict(type='started', message=self.task))
            self.last_result = self.backend.run(self.task, options, emit, stop, execute=execute)
            emit(dict(type='finished', message=self.last_result))
            # Never block waiting for a stop Job inside its own action callback.
            if (stop.is_set() or self.last_result.get('stop_queue')
                    or (not self.last_result.get('ok')
                        and not configuration_failure_can_continue(self.last_result))):
                self.backend.queue_stopped = True
                self.backend.stopped_generation = generation
                context.tasker.post_stop()
            return self.last_result.get('ok') is True and (
                not stop.is_set() or self.last_result.get('stop_queue') is True)
        except (ValueError, RuntimeError, OSError) as error:
            if str(error) != 'receipt_delivery_failed' and not stop.is_set():
                emit(dict(type='error', message=str(error)))
            self.backend.queue_stopped = True
            self.backend.stopped_generation = generation
            self.last_result = dict(ok=False, game_task_complete=False,
                                    reason=str(error), device_action_count_complete=False)
            context.tasker.post_stop()
            return False
