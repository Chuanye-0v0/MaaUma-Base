"""Inspection-page navigation; the live page may have advanced during a pause."""
from pathlib import Path
import time

from core.auto_trainer.cut_condition_probe import analyze_cut_details,locate_ability_button,locate_race_ability_button
from core.auto_trainer.cut_training_transition_recovery import recovered_header
from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
from core.auto_trainer.runtime_control import current
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.auto_trainer.larc_ss_information_probe import read_navigation
from core.auto_trainer.transition_wait import TransitionBudget
from core.execution_frames import ExecutionFrames
from core.native_capture_adb import adb_for_capture


NOTES={'resume_unknown_page':'当前页面未识别，请回到育成主页后再点继续。',
    'resume_resource_prompt':'当前有确认窗口，请手动取消并回主页后再继续。'}


def return_from_inspection(ctx,expected,output,*,adb=None,safety=None,header_reader=recovered_header,
        details_reader=analyze_cut_details,ss_reader=read_navigation,sleep=time.sleep,clock=time.monotonic,
        allow_handoff=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    frames=ExecutionFrames(adb or adb_for_capture(ctx.port));safety=safety or getattr(ctx,'screen_reader',None) or FrameSafetyAnalyzer()
    if not frames.connect():return {'ok':False,'reason':'resume_unknown_page'}
    submitted=set();budget=TransitionBudget(clock=clock)
    for index in range(30):
        path=output/f'frame_{index:02d}.png';image=frames.screenshot(str(path))
        if image is None:return {'ok':False,'reason':'resume_unknown_page'}
        raw=path.read_bytes()
        if not locate_ability_button(raw)['abstained'] or not locate_race_ability_button(raw)['abstained']:
            observed=header_reader(path)
            return {'ok':True,'source':str(path),'identity':observed,
                    'manual_progress_observed':expected is not None and observed!=expected}
        detail=details_reader(path)
        if detail.get('page_verified'):
            action='details_close';center=detail.get('close_center')
            if not center:return {'ok':False,'reason':'resume_unknown_page'}
        else:
            packet=safety.analyze(path)
            if packet.dangerous:
                if allow_handoff:return {'ok':True,'source':str(path),'handoff':True}
                return {'ok':False,'reason':'resume_resource_prompt'}
            ss=ss_reader(image)
            if ss.get('page_verified') and ss.get('close'):
                action='ss_information_close';center=ss['close']
            else:
                decision=SafeAdvancePlanner().plan(path,scenario='larc',visible_text=packet.visible_text,
                    allow_return_main_menu_from_training=True)
                if decision.get('reason')=='return_main_menu_from_training':
                    action='training_back';center=(decision['tap']['x'],decision['tap']['y'])
                elif submitted and decision.get('action')=='wait':
                    budget.observe(key='inspection_return');
                    if budget.expired:return {'ok':False,'reason':'resume_unknown_page'}
                    sleep(.15);continue
                elif allow_handoff:
                    # Events/races/dialogue belong to the normal live router.
                    return {'ok':True,'source':str(path),'handoff':True}
                else:return {'ok':False,'reason':'resume_unknown_page'}
        if action in submitted:
            budget.observe(key='inspection_return')
            if budget.expired:return {'ok':False,'reason':'resume_unknown_page'}
            sleep(.15);continue
        if frames.tap(*center,scaled=False) is not True:return {'ok':False,'reason':'resume_unknown_page'}
        submitted.add(action);sleep(.3)
    return {'ok':False,'reason':'resume_unknown_page'}


def pause_at_home(ctx,source,output,*,restorer=return_from_inspection):
    control=current()
    if control is None or control.pause_steps is None or not control.snapshot()['pause_requested']:return None
    expected=recovered_header(Path(source))
    original_epoch=control.epoch
    while True:
        control.checkpoint('cut_safe_boundary')
        if control.epoch==original_epoch:return None # A pending pause was cancelled before it took effect.
        control.note='正在重新识别当前页面…'
        result=restorer(ctx,expected,Path(output)/f'epoch_{control.epoch}')
        if result.get('ok'):
            control.note=None
            if getattr(ctx,'cut_farming_hook',None) is not None:
                ctx.cut_farming_hook.sampling_fault(('pause_resume',control.epoch))
            return {**result,'pause_identity':expected}
        control.note=NOTES.get(result.get('reason'),NOTES['resume_unknown_page'])
        control.request('pause')
