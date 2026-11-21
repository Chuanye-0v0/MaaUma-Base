"""Three standalone panel configuration slots, separate from live batch state."""
from copy import deepcopy
import json
from pathlib import Path

from core.auto_trainer.cut_farming import CutFarmingConfig, atomic_json
from scripts.larc_umaa_cut import CUT_OPTIONS, checked_batch, cut_snapshot, new_batch_defaults, read_json


SCHEMA = 'uma.cut_panel_presets.v1'
SLOT_FIELDS = CUT_OPTIONS - {'setup_confirmed', 'template_root'}
IDENTITY_FIELDS = {'character_id','validation_mode','validation_character_name',
                   'support_deck','borrow_card_id','borrow_min_level','borrow_min_break'}


def slot_id(value):
    if type(value) is not int or value not in (1,2,3):
        raise ValueError('配置编号请选择1、2或3。')
    return value


def sanitize_settings(value, endpoint):
    """Copy permitted future-run options; no setup receipt or counters survive."""
    if not isinstance(value,dict) or not isinstance(value.get('cut',{}),dict):
        raise ValueError('配置应包含strategy及cut参数对象。')
    from scripts.larc_panel_setup import parse_strategy_options
    strategy,_=parse_strategy_options(value.get('strategy','baseline'),25)
    fields={**new_batch_defaults([])['cut'],
            **{key:item for key,item in value.get('cut',{}).items() if key in SLOT_FIELDS}}
    from core.auto_trainer.larc_user_preferences import normalize, feature_available
    preferences=fields.pop('decision_preferences',None)
    if ('decision_preferences' not in value.get('cut', {})
            and (value.get('larc_policy') or {}).get('revision') in ('1.9.7', '1.9.8', '1.9.9', '1.9.10', '1.9.11', '1.9.12', '2.0', '2.1', '2.2')):
        preferences = value['larc_policy'].get('decision_preferences')
    preferences=normalize(preferences) if feature_available() else None
    from core.auto_trainer import larc_ai_options
    ai_options=fields.pop('ai_options',None)
    ai_options=larc_ai_options.normalize(ai_options) if larc_ai_options.feature_available() else None
    for key in ('max_attempts','max_tp_items','max_carrots','fallback_interval_turns',
                'support_deck','borrow_card_id','borrow_min_level','borrow_min_break'):
        item=fields.get(key)
        if isinstance(item,str) and item.strip().isdigit():fields[key]=int(item.strip())
    for key in ('pushplus_token_file','validation_character_name'):
        item=fields.get(key)
        if item is not None and not isinstance(item,str):raise ValueError('通知文件路径和角色名应为文本。')
        if isinstance(item,str):fields[key]=item.strip() or None
    config=CutFarmingConfig.from_dict({**fields,'strategy':strategy,'device_port':int(endpoint.rsplit(':',1)[1])})
    result={'strategy':config.strategy,'cut':{key:item for key,item in config.public_dict().items() if key in SLOT_FIELDS}}
    from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
    from scripts.larc_panel_setup import parse_early_skill_allowlist
    result['mid_skill_learning']=normalize_mid_skill_learning(value.get('mid_skill_learning'))
    result['early_skill_allowlist']=parse_early_skill_allowlist(value.get('early_skill_allowlist',[]))
    if 'support_profile' in value:
        from core.auto_trainer.run_support_profile import normalize_declaration
        result['support_profile']=normalize_declaration(value['support_profile'])
    if 'larc_policy' in value:
        from core.auto_trainer.larc_policy import normalize_larc_policy
        result['larc_policy']=normalize_larc_policy(value['larc_policy'],'cut')
    else:
        from core.auto_trainer.larc_policy import normalize_larc_policy
        result['larc_policy']=normalize_larc_policy({'variant':config.strategy},'cut')
    if preferences is not None:result['cut']['decision_preferences']=preferences
    if ai_options is not None:result['cut']['ai_options']=ai_options
    from core.auto_trainer.race_retry_budget import normalize as normalize_retry
    if value.get('race_retry') is not None:result['race_retry']=normalize_retry(value['race_retry'])
    return result


def record_for_path(value, *, root, endpoint):
    path,data,config=checked_batch(value,root=root,endpoint=endpoint)
    cut=cut_snapshot(path,data,config)
    settings={'strategy':config.strategy,'cut':{key:item for key,item in config.public_dict().items() if key in CUT_OPTIONS}}
    options=read_json(path/'cut_panel_options.json')
    policy=options.pop('larc_policy',data.get('larc_policy'))
    declaration=options.pop('support_profile',data.get('support_profile'))
    from core.auto_trainer.larc_support_skill_startup import normalize_mid_skill_learning
    settings['mid_skill_learning']=normalize_mid_skill_learning(options.pop('mid_skill_learning',data.get('mid_skill_learning')))
    settings['early_skill_allowlist']=options.pop('early_skill_allowlist',data.get('early_skill_allowlist',[]))
    retry=options.pop('race_retry',data.get('race_retry'))
    if retry is not None:settings['race_retry']=deepcopy(retry)
    settings['cut'].update(options)
    if policy is not None:settings['larc_policy']=deepcopy(policy)
    if declaration is not None:settings['support_profile']=deepcopy(declaration)
    return {'path':str(path),'label':path.parent.name if path.name=='batch' else path.name,
            'settings':settings,'can_resume':cut['can_resume'],'resume_reason':cut['resume_reason'],'cut':cut}


class CutPanelPresets:
    def __init__(self, root, endpoint, records):
        self.root=Path(root).resolve()
        self.endpoint=endpoint
        self.records=records
        self.path=self.root/'runs/standalone-panel/settings.json'
        # Kept separately so an older stable client saving its slots cannot erase test preferences.
        self.preferences_path=self.path.with_name('decision-preferences.json')
        self.ai_options_path=self.path.with_name('ai-options.json')

    def _read(self):
        if not self.path.exists():return {'schema_version':SCHEMA,'selected_id':1,'slots':{}}
        try:data=json.loads(self.path.read_text(encoding='utf-8-sig'))
        except (OSError,ValueError) as exc:raise ValueError('面板配置记录无法读取。') from exc
        if (not isinstance(data,dict) or data.get('schema_version')!=SCHEMA
                or not isinstance(data.get('slots'),dict)):
            raise ValueError('面板配置记录格式不正确。')
        slot_id(data.get('selected_id'))
        return {key:data[key] for key in ('schema_version','selected_id','slots','pushplus_token') if key in data}

    def current_record(self, *, active_root=None):
        if active_root:
            try:return record_for_path(active_root,root=self.root,endpoint=self.endpoint)
            except (OSError,ValueError,TypeError,KeyError) as exc:return self._unreadable(str(exc))
        # Read-only legacy recovery chooses the newest overall valid record,
        # including terminal records. It never searches backward for a resume.
        records=self.records()
        if not records:return None
        latest=max(records,key=lambda row:row.get('modified',0))
        try:return record_for_path(latest['path'],root=self.root,endpoint=self.endpoint)
        except (OSError,ValueError,TypeError,KeyError) as exc:return self._unreadable(str(exc))

    @staticmethod
    def _unreadable(reason):
        return {'path':None,'label':'当前任务记录不可用','settings':None,'can_resume':False,
                'resume_reason':reason,'cut':{'status':'unreadable','can_resume':False,'resume_reason':reason}}

    def _slots(self,data,current):
        from core.auto_trainer.larc_user_preferences import feature_available
        preferences=read_json(self.preferences_path) if feature_available() else {}
        from core.auto_trainer import larc_ai_options
        ai_options=read_json(self.ai_options_path) if larc_ai_options.feature_available() else {}
        rows=[]
        for index in (1,2,3):
            saved=data['slots'].get(str(index))
            if isinstance(saved,dict):
                settings=sanitize_settings(saved.get('settings',{}),self.endpoint)
                is_saved=saved.get('saved') is True
            else:
                settings=new_batch_defaults([current] if current else []);is_saved=False
                from core.auto_trainer.larc_policy import normalize_larc_policy
                settings['larc_policy']=normalize_larc_policy(None,'cut')
                if index==1 and current and isinstance(current.get('settings'),dict):
                    previous=current['settings']
                    settings['strategy']=previous.get('strategy','baseline')
                    settings['cut'].update({key:item for key,item in previous.get('cut',{}).items() if key in IDENTITY_FIELDS})
                settings=sanitize_settings(settings,self.endpoint)
                # A new panel slot starts with an editable preference; legacy
                # slots and runtime normalization retain their neutral defaults.
                if feature_available() and str(index) not in data['slots'] and str(index) not in preferences:
                    settings['cut']['decision_preferences']['mei_bonus']=5.
            if str(index) in preferences:
                from core.auto_trainer.larc_user_preferences import normalize
                settings['cut']['decision_preferences']=normalize(preferences[str(index)])
            if str(index) in ai_options:
                settings['cut']['ai_options']=larc_ai_options.normalize(ai_options[str(index)])
            stored = saved.get('settings', {}) if isinstance(saved, dict) else {}
            from core.auto_trainer.larc_user_preferences import panel_preferences_saved
            preferences_saved = panel_preferences_saved(self.preferences_path,index,preferences,
                embedded=self.path.is_file() and ('decision_preferences' in (stored.get('cut') or {})
                    or 'decision_preferences' in (stored.get('larc_policy') or {})))
            rows.append({'id':index,'label':f'配置{index}','settings':settings,'saved':is_saved,
                         'decision_preferences_saved':preferences_saved})
        return rows

    def payload(self, *, active_root=None):
        current=self.current_record(active_root=active_root)
        try:data=self._read();warning=None
        except ValueError as exc:
            data={'schema_version':SCHEMA,'selected_id':1,'slots':{}};warning=str(exc)
        result={'slots':self._slots(data,current),'selected_id':data['selected_id'],'current_record':current}
        if warning:result['metadata_warning']=warning
        return result

    def _editable(self,active_root=None):
        data=deepcopy(self._read())
        # Materialize the read-only identity seed without authorizing spend.
        rows=self._slots(data,self.current_record(active_root=active_root))
        data['slots']={str(row['id']):{'settings':row['settings'],'saved':row['saved'],
            'decision_preferences_saved':row['decision_preferences_saved']} for row in rows}
        return data

    def save(self,index,settings,*,active_root=None):
        from scripts.larc_panel_setup import upgrade_panel_settings,save_policy_migration
        index=slot_id(index)
        previous=deepcopy(self._read().get('slots',{}).get(str(index),{}).get('settings',{}))
        if 'larc_policy' not in settings and not previous:
            from core.auto_trainer.larc_policy import normalize_larc_policy
            settings={**settings,'larc_policy':normalize_larc_policy(None,'cut')}
        settings,prepared=upgrade_panel_settings(settings,task_mode='cut',previous=previous)
        settings=sanitize_settings(settings,self.endpoint)
        save_policy_migration(self.path.parent,prepared,label=f'cut-slot-{index}',previous_settings=previous)
        data=self._editable(active_root)
        data['slots'][str(index)]={'settings':settings,'saved':True,'decision_preferences_saved':True}
        if 'decision_preferences' in settings['cut']:
            preferences=read_json(self.preferences_path)
            from core.auto_trainer.larc_user_preferences import save_panel_preferences
            save_panel_preferences(self.preferences_path,index,settings['cut']['decision_preferences'],preferences)
        if 'ai_options' in settings['cut']:
            ai_options=read_json(self.ai_options_path)
            ai_options[str(index)]=settings['cut']['ai_options']
            atomic_json(self.ai_options_path,ai_options)
        self._strip_preferences(data)
        atomic_json(self.path,data)
        return self.payload(active_root=active_root)

    def select(self,index,*,active_root=None):
        index=slot_id(index)
        data=self._editable(active_root);data['selected_id']=index
        self._strip_preferences(data)
        atomic_json(self.path,data)
        return self.payload(active_root=active_root)

    def _strip_preferences(self,data):
        from core.auto_trainer.larc_user_preferences import feature_available,save_panel_preferences
        enabled=feature_available()
        preferences=read_json(self.preferences_path) if enabled else {}
        for index,slot in data['slots'].items():
            value=slot['settings']['cut'].get('decision_preferences')
            if enabled and value is not None and index not in preferences:
                save_panel_preferences(self.preferences_path,index,value,preferences,
                    factory=slot.get('decision_preferences_saved',True) is False)
            slot['settings']['cut'].pop('decision_preferences',None)
            slot['settings']['cut'].pop('ai_options',None)
