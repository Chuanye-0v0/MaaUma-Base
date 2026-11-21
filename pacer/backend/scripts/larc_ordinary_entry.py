"""Current-career takeover first; configured ordinary setup from home/setup."""
import json
from pathlib import Path
import re

import cv2

from core.auto_trainer.global_home_layout import is_global_home
from core.auto_trainer.safe_advance import SafeAdvancePlanner
from core.frame_io import read_image_with_retry
from core.perception import Perception
from scripts.larc_ordinary_setup import parse_ordinary_setup
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


SETUP_TITLES = ('育成剧本', '选择剧本', '剧本选择', '选择养成剧本', '选择育成剧本',
    '选择育成优俊少女', '育成优俊少女选择', '选择养成优俊少女', '养成优俊少女选择',
    '协助卡编成', '辅助卡编成', '支援卡编成', '协助卡牌编成', '选择好友协助卡', '最终确认')
CONSUMING_INPUTS = frozenset({'confirm_ordinary_start', 'request_tp_refill', 'confirm_tp_refill'})


def _opening_already_submitted(ctx, owner):
    if owner.get('run_session_id') != ctx.run_id or owner.get('device') != ctx.progress['device']:
        raise ValueError('ordinary_opening_context_mismatch')
    if owner.get('new_career_started'):
        return True
    preparation = owner.get('preparation_root')
    if not preparation:
        # Old pending owner records had no input ledger. Do not infer a tap.
        return owner.get('status') == 'pending'
    path = Path(preparation).resolve()
    if not path.is_relative_to(ctx.root.resolve()):
        raise ValueError('ordinary_opening_path_outside_run')
    ledger = path/'cut_farming.json'
    if not ledger.is_file():
        return False  # No route/input was created; navigation can resume.
    data = json.loads(ledger.read_text(encoding='utf-8'))
    return bool(data.get('ordinary_start_submitted') or data.get('tp_items_reserved')
        or data.get('carrots_reserved') or (data.get('pending_action') or {}).get('state') in CONSUMING_INPUTS)


def prepare_ordinary_entry(ctx, scene, output, *, starter=None, reconnect=None):
    config_path=ctx.root/'session/ordinary_setup.json'
    if not config_path.is_file():
        return {'ok':True,'reason':'current_career_only','changed':False}
    data=json.loads(config_path.read_text(encoding='utf-8'))
    if (data.get('run_session_id')!=ctx.run_id or data.get('device')!=ctx.progress['device']):
        raise ValueError('ordinary_setup_context_mismatch')
    # Known in-career pages belong to the existing owners. No extra screenshot
    # or setup OCR is needed, even when the configured character is different.
    if scene.get('owner') == 'race' or scene.get('state') in {
            'main_menu', 'training_menu', 'dialogue', 'event_choice', 'skill_acquisition'}:
        return {'ok':True,'reason':'current_career_takeover','changed':False}
    source=Path(scene['source'])
    image=read_image_with_retry(source)
    if image is None or image.shape[:2] not in {(1280,720),(2560,1440)}:
        return {'ok':False,'reason':'ordinary_entry_frame_unreadable','changed':False}
    image=cv2.resize(image,(720,1280))
    modal=SafeAdvancePlanner._continue_training_modal_button(Perception().analyze_image(image),image)
    from core.auto_trainer.frame_safety import FrameSafetyAnalyzer
    text=FrameSafetyAnalyzer().analyze(source).visible_text
    home=is_global_home(image)
    setup_page=any(title in text for title in SETUP_TITLES) or all(
        label in text for label in ('第1人','第2人','契合度','继承技能'))
    if not home and modal is None and not setup_page:
        return {'ok':True,'reason':'current_career_takeover','changed':False}
    if any(term in text for term in ('充值','人民币','支付','购买商品','购买宝石',
                                    '购买萝卜','选择账号','更换账号','输入密码','手机号登录')):
        return {'ok':False,'reason':'ordinary_entry_outside_scope','changed':False}
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    if reconnect is None:
        from scripts.larc_same_account_reconnect import reconnect_existing_career
        reconnect=reconnect_existing_career
    if modal is not None or home and re.search(r'(?:初级|经典级|高级)[\s\S]*?(?:出道前|月|远征)',text):
        result=reconnect(ctx,output/'existing',title_start_submitted=True,require_existing_career=True)
        result={**result,'changed':result.get('ok') is True,'new_career_started':False}
    else:
        owner_path=ctx.root/'session/ordinary_opening_owner.json'
        if owner_path.is_file():
            owner=json.loads(owner_path.read_text(encoding='utf-8'))
            if _opening_already_submitted(ctx,owner):
                return {'ok':False,'reason':'ordinary_opening_input_unknown_not_replayed','changed':False}
        if ctx.progress.get('actions_completed',0)>0:
            return {'ok':False,'reason':'ordinary_opening_requires_new_local_record','changed':False}
        setup=parse_ordinary_setup(data.get('setup'))
        if setup['character_id'] in {'current','validation'}:
            return {'ok':False,'reason':'ordinary_opening_requires_selected_costume','changed':False}
        d1_root=getattr(ctx,'d1_skill_root',None)
        if starter is None and (not d1_root or not (Path(d1_root)/'core/autonomous_training/trainee.py').is_file()):
            return {'ok':False,'reason':'ordinary_trainee_library_missing','changed':False}
        owner={'run_session_id':ctx.run_id,'device':ctx.progress['device'],'status':'pending',
            'preparation_root':str(output/'new/preparation')}
        _atomic_write_json(owner_path,owner)
        if starter is None:
            from scripts.larc_ordinary_start import start_preselected_ordinary_career
            starter=start_preselected_ordinary_career
        result=starter(ctx,output/'new',trainee_name=setup['character_name'],
            trainee_variant=setup['character_id'],setup=setup,d1_root=d1_root,max_tp_items=0)
        if result.get('reason')=='existing_career_handoff':
            result=reconnect(ctx,output/'existing',title_start_submitted=True,require_existing_career=True)
            result={**result,'new_career_started':False}
        else:
            result={**result,'new_career_started':result.get('ok') is True}
        if result.get('ok'):
            owner.update(status='complete',new_career_started=result.get('new_career_started',False))
            if result.get('new_career_started'):
                owner['trainee_identity']={'variant_id':setup['character_id'],
                    'character_id':setup['character_id'][:4],
                    'basis':'ordinary_start_confirmed_with_selected_costume'}
            _atomic_write_json(owner_path,owner)
            if result.get('new_career_started'):
                from scripts.larc_ordinary_setup import save_opening_support_observation
                save_opening_support_observation(ctx,setup,result.get('support_setup_observation'))
        result['changed']=result.get('ok') is True
    _atomic_write_json(output/'report.json',result)
    return result
