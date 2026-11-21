"""Local preset/schema/AI setup actions, independent of emulator binding."""
LOCAL_ACTIONS=frozenset({'configuration_snapshot','cut_presets','career_presets',
    'save_cut_preset','save_career_preset','select_cut_preset','select_career_preset',
    'decision_preferences_schema','validate_decision_preferences','preview_decision_preferences','continuation_schema',
    'ai_schema','ai_status','ai_test','ai_configure','ai_clear'})


def handle(host,request,*,root,center_enabled):
    if request.get('action')=='preview_decision_preferences':
        from desktop.scoring_preview import preview
        return preview(request)
    from scripts.larc_umaa_host import ACTIVE
    action=request.get('action')
    control=host.service.snapshot() if host.service else {'state':'idle','runtime':None}
    state=control.get('state','idle')
    mode=request.get('mode','career' if center_enabled else 'cut')
    if mode not in (('career','cut') if center_enabled else ('cut',)):
        raise ValueError('当前安装版本不提供所选模式。')
    if action=='configuration_snapshot':
        from desktop.larc_ai_status import for_host
        snapshot={'mode':mode,'active_mode':host.mode if state in ACTIVE else None,
            'control':control,'record':{},'endpoint':None,
            'run_root':str(host.run_root) if state in ACTIVE and host.run_root else None,
            'account':{},'cut':None,'device_owner':None,'daily_pending':False,
            'pacer_center':center_enabled,'closing':host.closing,'ai_status':for_host(host,root)}
        store=host._career_presets if mode=='career' else host._panel_presets
        active=host.run_root if state in ACTIVE and host.mode==mode else getattr(host,
            '_successful_career' if mode=='career' else '_successful_batch',None)
        return {'snapshot':snapshot,mode+'_presets':store.payload(active_root=active),
            'local_configuration':True}
    if action in {'cut_presets','career_presets','save_cut_preset','save_career_preset',
            'select_cut_preset','select_career_preset'}:
        career='career' in action
        if career and (not center_enabled or mode!='career'):
            raise ValueError('普通育成配置仅在育成中心可用。')
        if action.startswith(('save_','select_')) and state in ACTIVE:
            raise ValueError('请先结束当前任务，再修改或切换配置槽；暂停局仍沿用原设置。')
        store=host._career_presets if career else host._panel_presets
        active=getattr(host,'_successful_career' if career else '_successful_batch',None)
        if action.startswith('save_'):
            return store.save(request.get('slot_id'),request.get('settings'),active_root=active)
        if action.startswith('select_'):return store.select(request.get('slot_id'),active_root=active)
        return store.payload(active_root=active)
    if action=='decision_preferences_schema':
        from core.auto_trainer.larc_user_preferences import feature_available,DEFAULTS
        from core.auto_trainer.larc_score_tuning import schema
        return {'available':feature_available(),'defaults':DEFAULTS,'tuning_schema':schema()}
    if action=='validate_decision_preferences':
        from core.auto_trainer.larc_user_preferences import feature_available,normalize
        if not feature_available():raise ValueError('高级评分设置仅在测试版可用。')
        if 'profile' in request:
            from desktop.scoring_exchange import checked_profile
            if 'preferences' in request:raise ValueError('请提供完整评分方案或本机设置，不要同时提供两份。')
            profile=checked_profile(request['profile'])
            return {'profile':profile,'preferences':profile['preferences'],'device_actions':0}
        return {'preferences':normalize(request.get('preferences')),'device_actions':0}
    if action=='continuation_schema':
        from core.auto_trainer.larc_continuation import defaults,feature_available,KINDS
        return {'available':feature_available(root),'defaults':defaults(),'condition_kinds':sorted(KINDS),
            'automatic_limit':'first_expedition_before_departure'}
    if action.startswith('ai_'):
        from core.auto_trainer.larc_ai_options import schema
        from desktop import larc_ai_client
        from desktop.larc_ai_status import for_host
        result=schema(root)
        if action in ('ai_configure','ai_clear','ai_test') and not result['available']:
            raise ValueError('当前版本未启用 AI 接口设置。')
        if action in ('ai_configure','ai_clear'):
            if state not in {'idle','stopped'}:
                raise ValueError('请先结束本轮，再修改 AI 接口。暂停后继续仍沿用本局接口。')
            try:
                if action=='ai_configure':larc_ai_client.configure(root,request.get('base_url'),request.get('model'),request.get('api_key'))
                else:larc_ai_client.clear(root)
            except larc_ai_client.AIClientError as error:raise ValueError(error.message) from None
        test=larc_ai_client.test_connection(root) if action=='ai_test' else None
        status=for_host(host,root)
        if test is not None:status['connection']['test']=test
        result.update(provider=status['connection'],ai_status=status)
        return result
    raise ValueError('local_configuration_action_unknown')
