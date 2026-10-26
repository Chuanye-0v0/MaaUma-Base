"""A single data/calculation router; no desktop or game operation endpoints."""
from importlib import import_module
from pathlib import Path
import time
import uuid

MODULES={'skill':'knowledge','entity':'knowledge','support':'knowledge','knowledge':'knowledge',
         'sp':'sp','parents':'parents','inventory':'parents','aptitude':'parents','races':'parents',
         'simulation':'simulation','chat':'chat','integration':'integration'}

def capabilities():
    folder=Path(__file__).resolve().parent
    return {'app_id':'maauma-knowledge-hub','name':'MaaUma 资料与规划工具箱','local_only':True,
        'modules':{name:{'available':(folder/(name+'.py')).is_file()}for name in sorted(set(MODULES.values()))},
        'supported_servers':['cn','tw','jp'],'account_data':'only explicitly imported local libraries',
        'game_actions_supported':False,'integration_protocol':1}

def dispatch(request,*,storage_root=None):
    started=time.perf_counter();identifier=uuid.uuid4().hex
    if not isinstance(request,dict):return {'ok':False,'error':'请求应为JSON对象。','request_id':identifier}
    action=request.get('action','knowledge.capabilities')
    try:
        if action in {'knowledge.capabilities','hub.status'}:value=capabilities()
        else:
            prefix=str(action).split('.',1)[0];name=MODULES.get(prefix)
            if name is None:return {'ok':False,'error':'未识别的工具。可先查询工具列表。','request_id':identifier}
            try:module=import_module('hub.'+name)
            except ModuleNotFoundError as error:
                if error.name=='hub.'+name:
                    return {'ok':False,'action':action,'error':'该模块仍在构建。','request_id':identifier}
                raise
            if name in {'parents','chat','integration'}:
                from hub.integration import storage_path
                value=module.dispatch(request,storage_root=storage_path(storage_root))
            else:value=module.dispatch(request)
        failed=isinstance(value,dict)and value.get('ok')is False
        packet={'ok':not failed,'action':action,'result':value,
            'request_id':identifier,'elapsed_ms':round((time.perf_counter()-started)*1000,2)}
        if failed:packet['error']=value.get('message')or value.get('error')or'本次工具未完成。'
        return packet
    except (ValueError,TypeError,KeyError)as error:
        return {'ok':False,'action':action,'error':str(error),'request_id':identifier}
