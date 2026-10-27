"""Add only the new task/options, preserving every existing setting/version."""
from core.strongest_team.options import DEFAULTS

NODE='UmaStrongestTeam'


def configure(interface,pipeline,*,live=False):
    if not any(row.get('entry')==NODE for row in interface['task']):
        interface['task'].append(dict(name='目标！最强团队',entry=NODE,default_check=False,
            description='多账号日活：每日最多15场耗票招揽赛，免费特别赛另计。只用已有迷你胡萝卜果冻最多5份、已有闹钟最多5次；招揽只花活动积分并保留队长，不买资源。',
            option=['最强团队招揽赛上限','最强团队额外补票','最强团队活动闹钟']))
    settings=interface.setdefault('option',{})
    settings.setdefault('最强团队招揽赛上限',dict(type='select',default_case='15场耗票赛',cases=[
        dict(name=f'{n}场耗票赛',pipeline_override={NODE:dict(custom_action_param={'max_entries':n})}) for n in [5,10,15]]))
    settings.setdefault('最强团队额外补票',dict(type='select',default_case='已有迷你胡萝卜果冻，最多5份',cases=[
        dict(name='仅普通票与任务票',pipeline_override={NODE:dict(custom_action_param={'allow_existing_jelly':False,'max_jellies':0})}),
        dict(name='已有迷你胡萝卜果冻，最多5份',pipeline_override={NODE:dict(custom_action_param={'allow_existing_jelly':True,'max_jellies':5})})]))
    settings.setdefault('最强团队活动闹钟',dict(type='select',default_case='每天最多5次已有闹钟',cases=[
        dict(name=f'每天最多{n}次已有闹钟',pipeline_override={NODE:dict(custom_action_param={'max_clocks':n})}) for n in range(6)]))
    for case in settings.get('执行方式',{}).get('cases',[]):
        overrides=case.get('pipeline_override',{})
        reference=(overrides.get('UmaJjc') or {}).get('custom_action','')
        if reference:
            overrides[NODE]=dict(custom_action='UmaStrongestTeamBackend'+('Live' if reference.endswith('Live') else ''))
    pipeline.setdefault(NODE,dict(recognition='DirectHit',action='Custom',
        custom_action='UmaStrongestTeamBackend'+('Live' if live else ''),
        custom_action_param=dict(DEFAULTS),next=[],focus={
            'Node.Action.Starting':'目标！最强团队：15场耗票赛＋免费特别赛',
            'Node.Action.Failed':'最强团队未完成，保留原账号与原场次，请查看停止原因'}))
    return interface,pipeline
