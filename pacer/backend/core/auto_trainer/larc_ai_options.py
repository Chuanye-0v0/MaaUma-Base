"""Non-secret AI decision preferences and immutable per-career snapshots."""
import json
import math
from pathlib import Path


DEFAULTS={'mode':'local','timeout_seconds':20,'close_score_gap':5.,'fallback':'local'}
MODES=('local','critical','full')
SCHEMA='uma.larc_ai_decision.v1'
RELATIVE_PATH='session/ai_decision.json'
RUNTIME_FILES=frozenset({'backend/core/auto_trainer/larc_ai_options.py',
    'backend/core/auto_trainer/larc_ai_runtime.py','backend/core/auto_trainer/larc_ai_nodes.py',
    'backend/desktop/larc_ai_client.py'})


def normalize(value=None):
    if value is None:value={}
    if not isinstance(value,dict) or set(value)-set(DEFAULTS):raise ValueError('AI决策设置包含未知项目。')
    result={**DEFAULTS,**value}
    if result['mode'] not in MODES:raise ValueError('AI决策模式请选择本地、关键时刻或每回合。')
    timeout=result['timeout_seconds']
    if type(timeout) is not int or not 1<=timeout<=120:raise ValueError('AI等待时间应为1～120秒的整数。')
    gap=result['close_score_gap']
    if type(gap) not in (int,float) or not math.isfinite(gap) or not 0<=gap<=1000:raise ValueError('候选分差应为0～1000之间的数字。')
    result['close_score_gap']=float(gap)
    if result['fallback'] not in ('local','pause'):raise ValueError('AI不可用时请选择沿用本地策略或暂停保留。')
    return result


def feature_available(root=None):
    development=root is None
    root=Path(root) if root is not None else Path(__file__).resolve().parents[2]
    if root.name.casefold()=='backend':root=root.parent;development=False
    manifest=root/'product-package-manifest.json'
    if not manifest.is_file():return development
    try:
        from desktop.product_updates import manifest_channel
        data=json.loads(manifest.read_text(encoding='utf-8-sig'))
        return manifest_channel(data)=='preview' and (data.get('features') or {}).get('ai_decision') is True
    except (OSError,ValueError,TypeError,AttributeError):return False


def schema(root=None):
    return {'available':feature_available(root),'defaults':dict(DEFAULTS),
            'modes':list(MODES),'limits':{'timeout_seconds':[1,120],'close_score_gap':[0,1000]},
            'fallbacks':['local','pause']}


def save_options(run,options,connection=None):
    from core.auto_trainer.cut_farming import atomic_json
    run=Path(run).resolve();path=run/RELATIVE_PATH
    if not path.resolve().is_relative_to(run):raise ValueError('AI设置路径不在当前局内。')
    value=normalize(options)
    connection={key:item for key,item in (connection or {}).items() if key in ('base_url','model')}
    if value['mode']!='local' and any(not isinstance(connection.get(key),str) or not connection[key].strip() for key in ('base_url','model')):
        raise ValueError('本局缺少已保存的AI接口信息。')
    if value['mode']=='local':connection={}
    if path.exists():
        saved=json.loads(path.read_text(encoding='utf-8-sig'))
        if load_options(run)!=value or saved.get('connection',{})!=connection:raise ValueError('本局已有固定AI设置，不能在恢复时替换。')
        return value
    atomic_json(path,{'schema_version':SCHEMA,'run_id':run.name,'options':value,'connection':connection})
    return value


def load_options(run):
    run=Path(run).resolve();path=run/RELATIVE_PATH
    if not path.resolve().is_relative_to(run):raise ValueError('AI设置路径不在当前局内。')
    if not path.exists():return normalize()
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    options=value.get('options') if isinstance(value,dict) else None
    if isinstance(options,dict) and options.get('mode')=='local':
        # The cut runner loads this before run_ai_scope. Do not let unused AI
        # metadata stop a local attempt at that earlier production boundary.
        try:return normalize(options)
        except ValueError:return normalize()
    if (not isinstance(value,dict) or value.get('schema_version')!=SCHEMA or value.get('run_id')!=run.name or not isinstance(value.get('options'),dict)):
        raise ValueError('AI设置与当前局不一致。')
    return normalize(value.get('options'))
