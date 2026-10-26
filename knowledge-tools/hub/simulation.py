"""Read-only local adapter to the source-built reference Node simulation."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'engine'

def _node_executable() -> str | None:
    configured=os.environ.get('MAAUMA_SIMULATION_NODE')
    if configured and Path(configured).is_file():
        return configured
    return shutil.which('node')

def dispatch(request:dict) -> dict:
    """simulation.courses / simulation.skills / simulation.run -> explicit model JSON."""
    if not isinstance(request,dict):
        return {'ok':False,'error':'invalid_simulation_request','message':'模拟请求需要JSON对象。'}
    payload=dict(request)
    action=payload.get('action')or payload.get('operation')or payload.get('method')
    if action not in {'simulation.courses','simulation.skills','simulation.run','courses','skills','run'}:
        return {'ok':False,'error':'unknown_simulation_action'}
    requested=str(payload.get('ruleset')or payload.get('server')or'cn').lower()
    ruleset='global'if requested in {'global','en','english','global-reference'}else'jp'
    if requested not in {'jp','ja','japan','global','en','english','global-reference'}:
        payload.setdefault('server',requested)
    else:
        payload.setdefault('server',ruleset)
    payload['action']=action
    executable=_node_executable()
    if not executable:
        return {'ok':False,'error':'simulation_runtime_unavailable',
            'message':'本机未找到Node运行时；资料查询和种马规划仍可使用。',
            'supported_runtime':'Node.js 20+; validated with24.15.0'}
    compiled=ENGINE/'dist'/f'simulation-{ruleset}.cjs'
    if not compiled.is_file():
        return {'ok':False,'error':'simulation_build_unavailable','message':'参考引擎尚未构建。',
            'build_entry':str(ENGINE/'build.mjs')}
    try:
        child=subprocess.run([executable,str(compiled)],input=json.dumps(payload,ensure_ascii=False),
            text=True,encoding='utf-8',capture_output=True,cwd=ENGINE,timeout=45,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        result=json.loads(child.stdout)
        if not isinstance(result,dict):raise ValueError('non-object response')
        if result.get('ok')is True and isinstance(result.get('result'),dict):
            return {'ok':True,**result['result'],'metadata':result.get('metadata',{}),'action':result.get('action',action)}
        return result
    except subprocess.TimeoutExpired:
        return {'ok':False,'error':'simulation_timeout','message':'本地参考模拟超时；请减少样本或技能数量后重试，其他资料功能不受影响。'}
    except (OSError,ValueError,json.JSONDecodeError) as error:
        return {'ok':False,'error':'simulation_runtime_error','message':str(error)}

def courses(query:str='',server:str='cn',**kwargs) -> dict:
    return dispatch({'action':'simulation.courses','query':query,'server':server,**kwargs})

def skills(query:str='',server:str='cn',**kwargs) -> dict:
    return dispatch({'action':'simulation.skills','query':query,'server':server,**kwargs})

def run(request:dict|None=None,**kwargs) -> dict:
    return dispatch({'action':'simulation.run',**(request or{}),**kwargs})
