"""Exchange only validated scoring preferences; never import a whole app config."""
import json
from pathlib import Path
import re
from urllib.parse import urlencode

from core.auto_trainer.larc_user_preferences import normalize
from core.auto_trainer import larc_score_tuning as tuning
from desktop.product_updates import read_json

SCHEMA=tuning.PROFILE_SCHEMA
LEGACY_SCHEMA='uma.scoring_profile.v1'
IDENTIFIER=re.compile(r'SP-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}')
REQUEST_ID=re.compile(r'[a-f0-9]{32}')


def checked_profile(value):
    if not isinstance(value,dict) or not isinstance(value.get('preferences'),dict):
        raise ValueError('请使用仅包含评分设置的权重方案，不能上传整份面板配置。')
    if value.get('schema_version')==LEGACY_SCHEMA and set(value)=={'schema_version','preferences'}:
        semantics=tuning.LEGACY_SCORING_SEMANTICS
    elif value.get('schema_version')==tuning.SCORING_ONLY_PROFILE_SCHEMA and set(value)=={'schema_version','scoring_semantics','preferences'}:
        semantics=value['scoring_semantics']
    elif value.get('schema_version')==SCHEMA and set(value)=={'schema_version','scoring_semantics','workflow_semantics','preferences'}:
        from core.auto_trainer.larc_timeline_workflow import SEMANTICS
        workflow_semantics=value['workflow_semantics']
        if (not isinstance(workflow_semantics,dict) or set(workflow_semantics)!={'model','version'}
                or type(workflow_semantics.get('version')) is not int or workflow_semantics!=SEMANTICS):
            raise ValueError('工作流模型或语义版本不兼容，未导入此方案。')
        semantics=value['scoring_semantics']
    else:
        raise ValueError('评分方案格式未知，请使用兼容的纯评分配置。')
    if value.get('schema_version')!=SCHEMA and value['preferences'].get('workflow') is not None:
        raise ValueError('包含时间轴的方案需要v3及明确的工作流语义版本。')
    if (not isinstance(semantics,dict) or set(semantics)!={'model','version'}
            or not isinstance(semantics.get('model'),str) or type(semantics.get('version')) is not int
            or semantics['version']<1 or semantics!=tuning.SCORING_SEMANTICS):
        raise ValueError('评分模型或语义版本不兼容，未导入此方案。请使用适配当前评分模型的方案。')
    checked=tuning.scoring_profile(normalize(value['preferences']))
    workflow=checked['preferences'].get('workflow') or {}
    has_graph=isinstance(workflow.get('editor_graph'),dict)
    limit=1024*1024 if has_graph else 64*1024
    if len(json.dumps(checked,ensure_ascii=False).encode('utf-8'))>limit:
        raise ValueError('带节点画布的本地方案最多1MiB。' if has_graph else '评分方案内容过大。')
    return checked


def _text(value,limit,*,empty=False):
    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 and c not in '\n\r\t' for c in value):
        raise ValueError('方案名称、昵称或说明格式不正确。')
    value=value.strip()
    if not value and not empty:raise ValueError('请填写方案名称和公开昵称。')
    return value


def _id(value):
    if not isinstance(value,str) or not IDENTIFIER.fullmatch(value):raise ValueError('请选择有效的权重方案。')
    return value


def _revision(value):
    if type(value) is not int or value<1:raise ValueError('方案版本未知，请刷新列表后再试。')
    return value


def _summary(value):
    if not isinstance(value,dict):raise ValueError('服务返回的方案格式不正确。')
    _id(value.get('id'));_revision(value.get('revision'))
    result={key:value.get(key) for key in ('id','title','author_name','description','app_version',
        'revision','status','created_at','updated_at','mine')}
    for key,limit in (('title',60),('author_name',40),('description',2000),('app_version',100)):
        result[key]=_text(result[key],limit,empty=key=='description')
    if result['status'] not in ('published','withdrawn','blocked') or type(result['mine']) is not bool:
        raise ValueError('服务返回的方案状态不正确。')
    return result


def dispatch(root,value):
    from desktop.cut_support_client import request,result,SupportError
    action=value.get('action')
    try:
        if action=='scoring_list':
            query=_text(value.get('query',''),80,empty=True)
            mine=value.get('mine',False);offset=value.get('offset',0)
            if type(mine) is not bool or type(offset) is not int or not 0<=offset<=10000:
                raise ValueError('列表条件不正确。')
            response=request(root,'/v1/scoring-profiles?'+urlencode({'q':query,'mine':'1' if mine else '0','offset':offset}))
            rows=response.get('profiles')
            if not isinstance(rows,list) or len(rows)>40:raise ValueError('服务返回的方案列表不正确。')
            cursor=response.get('next_offset')
            if cursor is not None and (type(cursor) is not int or not offset<cursor<=10000):
                raise ValueError('服务返回的下一页位置不正确。')
            return result('scoring_list',profiles=[_summary(row) for row in rows],next_offset=cursor)
        if action=='scoring_get':
            identifier=_id(value.get('id'))
            row=request(root,'/v1/scoring-profiles/'+identifier).get('scheme')
            info=_summary(row)
            if info['id']!=identifier:raise ValueError('服务返回的方案编号不一致。')
            info['profile']=checked_profile(row.get('profile'))
            return result('scoring_loaded',scheme=info)
        if action not in ('scoring_publish','scoring_withdraw'):
            raise SupportError('unsupported_action')
        request_id=value.get('request_id')
        if not isinstance(request_id,str) or not REQUEST_ID.fullmatch(request_id):raise ValueError('提交标识无效，请重新打开发布窗口。')
        identifier=value.get('id')
        payload={'request_id':request_id}
        if identifier is not None:
            identifier=_id(identifier);payload['expected_revision']=_revision(value.get('expected_revision'))
        if action=='scoring_withdraw':
            if identifier is None:raise ValueError('请先选中自己的方案。')
            row=request(root,'/v1/scoring-profiles/'+identifier+'/withdraw',payload).get('scheme')
            return result('scoring_withdrawn',scheme=_summary(row))
        checked=checked_profile(value.get('profile'))
        if checked['schema_version']==SCHEMA:
            raise ValueError('时间轴方案可导出文件分享；在线服务尚未验收v3，暂不提交，配置未降级。')
        version=read_json(Path(root)/'product-package-manifest.json').get('version','unknown')
        payload.update(title=_text(value.get('title'),60),author_name=_text(value.get('author_name'),40),
            description=_text(value.get('description',''),2000,empty=True),app_version=_text(version,100),
            profile=checked)
        row=request(root,'/v1/scoring-profiles'+('/'+identifier if identifier else ''),payload).get('scheme')
        info=_summary(row);info['profile']=checked_profile(row.get('profile'))
        return result('scoring_published',scheme=info)
    except ValueError as error:
        return {'ok':False,'status':'error','code':'score_profile_invalid',
            'message':'权重方案未通过校验：'+str(error)}
