"""UMAA-facing adapter for the existing dashboard start/pause/resume/stop API."""
from pathlib import Path
import re

from core.auto_trainer.dashboard_control import ControlProfile, DashboardControlService
from core.auto_trainer.dashboard_runner import RunnerHeartbeatWorker
from core.auto_trainer.runtime_control import (
    RunControl, RunStopped, ResumeFromCurrentPage, current_page_resume, bind,
)


def _annotate_wait_recovery(report, recovery):
    """Resolve the latest matching wait episode, without changing its timing."""
    rows = report.get('technical_wait_stops', [])
    for row in reversed(rows):
        if (row.get('schema_version') == 'uma.technical_wait_stop.v1'
                and row.get('reason') == report.get('stop_reason')
                and row.get('recovery', {}).get('status') == 'not_attempted'):
            row['recovery'] = {
                'status': 'recovered' if recovery.get('ok') is True else
                    'failed' if recovery.get('handled') else 'not_applicable',
                'considered': True, 'automatic': True,
                'handled': recovery.get('handled') is True,
                'reason': recovery.get('reason'), 'evidence': recovery.get('evidence'),
                'elapsed_seconds': recovery.get('elapsed_seconds'),
                'training_outcome': recovery.get('training_outcome'),
            }
            from scripts.larc_autonomous_run import _wait_stop_counts
            report['technical_wait_stop_counts'] = _wait_stop_counts(rows)
            return True
    return False


def _persist_wait_recovery(report, run_root):
    """Update only this segment's diagnostic summary, never an action ledger."""
    from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
    try:
        root = Path(run_root).resolve(strict=True)
        output = Path(report.get('output', '')).resolve(strict=True)
        if output == root or not output.is_relative_to(root):
            return
        _atomic_write_json(output / 'summary.json', report)
    except (OSError, ValueError) as error:
        # Diagnostics must not stop, authorize, or retry game input.
        report['wait_stop_write_warning'] = f'{type(error).__name__}:{error}'


def make_larc_runner_factory(run=None, *, umaa_root=None,control_factory=None,lease=None):
    def factory(profile, port):
        from scripts.larc_autonomous_run import parse_args, run as run_career
        args = parse_args(list(profile.runner_config["argv"]))
        if args.execute:
            try:
                from core.auto_trainer.pacer_storage import maintain
                maintain(Path(__file__).resolve().parents[1],exclude_run=args.run_root)
            except Exception as error:
                import logging
                logging.getLogger(__name__).warning("截图维护未完成：%s",type(error).__name__)
        control = (control_factory or RunControl)(changed=port.runtime_state)
        port.attach_runtime(control)
        args._run_control = control
        from contextlib import nullcontext
        from scripts.larc_live_takeover import enabled as live_takeover_enabled
        resume_current_page = live_takeover_enabled(args)
        if resume_current_page:
            # The panel owns this control's whole lifetime, including all
            # current-page reattachments; one segment must not stop it.
            args._keep_run_control = True
        remaining = args.max_actions
        resume_history = []
        heartbeat = None
        try:
            try:
                heartbeat = RunnerHeartbeatWorker(port=port)
                with heartbeat:
                    if control_factory is not None:
                        # Private ordinary careers use the very same device-bound
                        # license control as cut runs, before taking any device lease.
                        control.checkpoint('career_license_start')
                    scope = nullcontext()
                    if lease is not None or run is None or umaa_root is not None:
                        from core.auto_trainer.larc_skill_driver import load_driver
                        from scripts.larc_umaa_common import occupancy
                        driver = load_driver(args.run_root) or {}
                        shared_root = Path(umaa_root or driver.get('d1_root', ''))
                        device_lease = lease or occupancy(shared_root)
                        scope = device_lease.hold(shared_root / 'runs/device-occupancy',
                                           f'127.0.0.1:{args.adb_port}', 'pacer')
                    with scope:
                        while True:
                            try:
                                with current_page_resume(control) if resume_current_page else nullcontext():
                                    report, code = (run or run_career)(args)
                            except ResumeFromCurrentPage:
                                report, code = {"stop_reason":"player_resume_current_page",
                                    "consumed_turns_this_run":0}, 2
                            recovered = None
                            if (resume_current_page and args.execute
                                    and args.confirm_token == 'LARC_AUTONOMY_TEST'
                                    and getattr(args, 'ordinary_career_opening', False) is True
                                    and not getattr(args, '_cut_farming_hook', None)
                                    and report.get('stop_reason') == 'career_transition_no_progress_timeout'):
                                from scripts.larc_career_freeze_recovery import recover_career_training_freeze
                                # The failed segment returned, but this panel still owns
                                # its control and lease. Restore only the same career;
                                # the next segment reobserves rather than replaying input.
                                with bind(control, f'127.0.0.1:{args.adb_port}'):
                                    recovered = recover_career_training_freeze(args.run_root, report,
                                        adb_port=args.adb_port, authorized=True,
                                        ordinary_career_opening=True, control=control)
                                if _annotate_wait_recovery(report, recovered):
                                    _persist_wait_recovery(report, args.run_root)
                                if recovered.get('handled') and recovered.get('ok') is not True:
                                    report = dict(report, stop_reason='career_freeze_recovery_unverified',
                                        automatic_recovery=recovered)
                                    code = 2
                            automatic_resume = recovered is not None and recovered.get('ok') is True
                            if not automatic_resume and (not resume_current_page
                                    or report.get("stop_reason") != "player_resume_current_page"):
                                break
                            if control.stopping:
                                raise RunStopped()
                            consumed = max(0, int(report.get("consumed_turns_this_run") or 0))
                            if automatic_resume and recovered.get('training_outcome') == 'unapplied':
                                consumed = max(0, consumed-1)
                            remaining = max(0, remaining-consumed)
                            entry = {"resume_epoch":control.epoch,
                                "previous_output":report.get("output"), "script_turns_consumed":consumed}
                            if automatic_resume:
                                entry.update(automatic_recovery=recovered['evidence'],
                                    previous_segment_elapsed_seconds=report.get('elapsed_seconds'),
                                    recovery_elapsed_seconds=recovered.get('elapsed_seconds'),
                                    training_outcome=recovered.get('training_outcome'))
                                if report.get('technical_wait_stops'):
                                    from copy import deepcopy
                                    # Preserve the resolved old episode even if a busy
                                    # diagnostic summary could not be refreshed. These
                                    # are history, not stops in the next segment count.
                                    entry['technical_wait_stops'] = deepcopy(report['technical_wait_stops'])
                            resume_history.append(entry)
                            if remaining == 0:
                                report = dict(report, status="batch_limit_reached", stop_reason="batch_limit_reached")
                                code = 0
                                break
                            # Same run, same control, same device lease. Only the old
                            # execution stack is discarded; manual turns aren't added
                            # to script consumption and the original limit isn't reset.
                            args.max_actions = remaining
                            args._player_live_takeover = True
                            args._panel_player_resume_history = list(resume_history)
                        if (args.execute and not report.get('workflow_batch_root')
                                and report.get('run_session_id') == Path(args.run_root).name):
                            try:
                                from core.auto_trainer.pacer_storage import maintain
                                root = Path(args.run_root).absolute()
                                maintain(Path(__file__).resolve().parents[1],min_interval=0,
                                    exclude_run=root,
                                    runtime_owner={'run_root':root,
                                        'batch_root':root.parent.parent if root.parent.name=='attempts' else None,
                                        'endpoint':f'127.0.0.1:{args.adb_port}','pins':[]})
                            except Exception as error:
                                import logging
                                logging.getLogger(__name__).warning("截图维护未完成：%s",type(error).__name__)
            except RunStopped as exc:
                report, code = {"stop_reason": "runner_heartbeat_failed" if heartbeat is not None
                    and heartbeat.failure is not None else exc.reason}, 2
            except Exception as error:
                reason = ("runner_heartbeat_failed" if heartbeat is not None and heartbeat.failure is not None
                    else "device_busy" if str(error) == "device_busy" else "runner_error")
                port.finish(outcome="stopped", reason=reason, evidence_sha256=None)
                raise
            finally:
                control.finish()
            port.finish(outcome="completed" if code == 0 else "stopped",
                        reason=re.sub(r"[^a-z0-9_.:-]", "_", str(report.get("stop_reason", "runner_finished")).lower())[:96],
                        evidence_sha256=None)
        finally:
            if args.execute:
                try:
                    from core.auto_trainer.pacer_storage import maintain
                    maintain(Path(__file__).resolve().parents[1],min_interval=0,exclude_run=args.run_root)
                except Exception as error:
                    import logging
                    logging.getLogger(__name__).warning("截图维护未完成：%s",type(error).__name__)
    return factory


def create_larc_control_service(state_dir, profiles, *, run=None, umaa_root=None,control_factory=None,lease=None):
    """Profiles contain approved CLI arguments; button requests contain no shell."""
    service=DashboardControlService(state_dir=Path(state_dir),
        profiles={name: ControlProfile(name, {"argv": list(argv)}) for name, argv in profiles.items()},
        runner_factory=make_larc_runner_factory(run, umaa_root=umaa_root,control_factory=control_factory,lease=lease))
    if control_factory is not None and (service.snapshot()['state'] not in {'idle','stopped'} or (Path(state_dir)/'runner.lock').exists()):
        from scripts.larc_umaa_common import occupancy
        if len(profiles)!=1:raise ValueError('career_reconnect_profile_ambiguous')
        argv=list(next(iter(profiles.values())));port=int(argv[argv.index('--adb-port')+1])
        device_lease=lease or occupancy(umaa_root)
        with device_lease.hold(Path(umaa_root)/'runs/device-occupancy',f'127.0.0.1:{port}','pacer'):
            service.recover_detached_runner()
    return service


def submit_button(service, button, *, request_id, profile_id=None):
    """Three UI buttons; start becomes resume when the acknowledged state is paused."""
    if button not in {"start", "pause", "stop"}:
        raise ValueError("unknown_panel_button")
    snapshot = service.snapshot()
    action = "resume" if button == "start" and snapshot["state"] == "paused" else button
    payload = {"request_id": request_id, "expected_state_version": snapshot["state_version"]}
    payload.update({"profile_id": profile_id} if action == "start" else {"run_id": snapshot["run_id"]})
    return service.submit(action, payload)
