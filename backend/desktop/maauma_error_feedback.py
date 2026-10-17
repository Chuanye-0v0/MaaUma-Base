"""Small opt-in private error reports; no screenshots, config dump or log scan."""
from __future__ import annotations

from datetime import datetime,timezone
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener
import uuid

from desktop.product_updates import contained,read_json,write_json

SCHEMA='maauma.error_feedback.v1'
DEFAULT_ENDPOINT='https://maauma-private.2234198378.workers.dev/v1/bug-feedback'
REPORT_FOLDER='logs/error-feedback'
MAX_REQUEST=256*1024
MAX_REPORT=64*1024
FEEDBACK_ID=re.compile(r'BF-[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}',re.I)
PAYLOAD_FIELDS={'submission_id','app_version','area','title','steps','expected','actual','contact'}
NOTICE='仅提交版本、当前异常和最近操作的脱敏文字；不附截图、配置或历史日志。'

_CREDENTIAL_KEY=re.compile(
    r'''(?ix)(["']?(?:activation[_ -]?code|invite[_ -]?code|access[_ -]?token|refresh[_ -]?token|
        authorization|password|passwd|api[_ -]?key|secret(?:[_ -]?(?:key|id))?|
        pushplus[_ -]?token|token|encrypted|device[_ -]?id|binding[_ -]?id|client[_ -]?id|public[_ -]?key|
        user[_ -]?name|account[_ -]?name|
        激活码|邀请码|密码|令牌|密钥|用户名)["']?\s*[:=]\s*)
        (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)''')
_TOKENS=re.compile(r'''(?ix)
    \bMI-[a-f0-9]{20,}\b|\b(?:md_|mc_)[A-Za-z0-9._~\-]{8,}|
    \bMU-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}\b|
    \b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{12,}|
    \bAKID[A-Za-z0-9]{16,}|\bsk-[A-Za-z0-9_\-]{16,}''')
_BEARER=re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+={0,2}')
_URL=re.compile(r'''https?://[^\s<>"']+''',re.I)
_WINDOWS_PATH=re.compile(r'''(?i)(?<!\w)(?:[a-z]:[\\/]|\\\\[^\\\r\n]+\\)[^\r\n<>"|]*''')
_UNIX_PATH=re.compile(r'''(?<![\w:])/(?:Users|home|root|mnt|tmp|var|opt|srv|workspace)(?:/[^\r\n<>"']*)?''')
_EMAIL=re.compile(r'(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b')


class FeedbackError(RuntimeError):
    pass


def _excerpt(value,limit):
    # The existing web endpoint counts JavaScript UTF-16 code units.
    raw=value.encode('utf-16-le')
    if len(raw)//2<=limit:return value
    marker='\n[已省略过长内容]\n'
    remaining=limit-len(marker);head=remaining*3//4
    return raw[:head*2].decode('utf-16-le',errors='ignore')+marker+raw[-(remaining-head)*2:].decode('utf-16-le',errors='ignore')


def _redacted_url(match):
    try:
        parsed=urlsplit(match[0])
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return parsed.scheme+'://'+(parsed.hostname or '[HOST]')+parsed.path+'[URL_DETAILS_REDACTED]'
    except ValueError:return '[URL_REDACTED]'
    return match[0]


def _redacted_path(match):
    line=re.search(r':(?:line\s*)?(\d+)\s*$',match[0],re.I)
    return '[PATH]'+(':line '+line[1] if line else '')


def redact(value,limit=4000):
    if not isinstance(value,str):raise FeedbackError('error_feedback_invalid')
    value=value[:MAX_REQUEST]
    value=_URL.sub(_redacted_url,value)
    value=_BEARER.sub('Bearer [REDACTED]',value)
    value=_CREDENTIAL_KEY.sub(lambda match:match[1]+'[REDACTED]',value)
    value=_TOKENS.sub('[REDACTED]',value)
    value=_WINDOWS_PATH.sub(_redacted_path,value)
    value=_UNIX_PATH.sub(_redacted_path,value)
    value=_EMAIL.sub('[EMAIL]',value)
    value=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]','',value)
    return _excerpt(value.strip(),limit)


def _report_id(value):
    if not isinstance(value,str):raise FeedbackError('error_feedback_invalid')
    try:parsed=uuid.UUID(value)
    except ValueError:raise FeedbackError('error_feedback_invalid') from None
    if str(parsed)!=value.lower():raise FeedbackError('error_feedback_invalid')
    return str(parsed)


def _path(root,report_id):
    return contained(Path(root).resolve(strict=True),REPORT_FOLDER+'/'+_report_id(report_id)+'.json')


def _payload(value,report_id):
    actions=value.get('recent_actions',[])
    if not isinstance(actions,list) or not all(isinstance(item,str) for item in actions):
        raise FeedbackError('error_feedback_invalid')
    kind=redact(value.get('exception_type','Exception'),140) or 'Exception'
    message=redact(value.get('message',''),800)
    stack=redact(value.get('stack_trace',''),4000)
    actual=_excerpt('\n'.join(part for part in (kind,message,stack) if part),4000)
    return {'submission_id':report_id,'app_version':redact(value.get('app_version','unknown'),100) or 'unknown',
        'area':'界面异常','title':_excerpt('界面异常：'+kind,160),
        'steps':'\n'.join(redact(item,300) for item in actions[-8:]),
        'expected':'界面正常响应并完成当前操作。','actual':actual,'contact':''}


def _load(root,report_id):
    path=_path(root,report_id)
    if not path.is_file():raise FeedbackError('error_feedback_not_found')
    if path.stat().st_size>MAX_REPORT:raise FeedbackError('error_feedback_invalid')
    value=read_json(path)
    if (not isinstance(value,dict) or value.get('schema_version')!=SCHEMA or value.get('report_id')!=report_id
            or not isinstance(value.get('payload'),dict) or set(value['payload'])!=PAYLOAD_FIELDS
            or value['payload'].get('submission_id')!=report_id):
        raise FeedbackError('error_feedback_invalid')
    # Reapply text redaction if a local report was edited before submission.
    for field,limit in {'app_version':100,'area':80,'title':160,'steps':4000,'expected':2000,'actual':4000,'contact':200}.items():
        value['payload'][field]=redact(value['payload'][field],limit)
    return value


def _preview(report):
    body=report['payload']
    return '\n'.join(('MaaUma 错误报告','版本：'+body['app_version'],body['title'],
        '最近操作：\n'+(body['steps'] or '未提供'), '异常详情：\n'+body['actual'],NOTICE))


def prepare(root,value):
    if not isinstance(value,dict):raise FeedbackError('error_feedback_invalid')
    if value.get('report_id') is not None:
        report_id=_report_id(value['report_id']);report=_load(root,report_id)
    else:
        report_id=str(uuid.uuid4())
        report={'schema_version':SCHEMA,'report_id':report_id,'created_at':datetime.now(timezone.utc).isoformat(),
                'payload':_payload(value,report_id)}
        write_json(_path(root,report_id),report)
    return {'ok':True,'report_id':report_id,'preview_text':_preview(report),'notice':NOTICE}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise FeedbackError('error_feedback_unavailable')


def submit(root,report_id,*,endpoint=DEFAULT_ENDPOINT,timeout=20):
    report_id=_report_id(report_id);report=_load(root,report_id)
    previous=report.get('acknowledgement')
    if (isinstance(previous,dict) and FEEDBACK_ID.fullmatch(str(previous.get('feedback_id','')))
            and previous.get('status') in {'open','triaged','fixed','closed'}):
        return {'ok':True,'feedback_id':previous['feedback_id'],'status':previous.get('status','open'),'idempotent':True}
    data=json.dumps(report['payload'],ensure_ascii=False,separators=(',',':')).encode('utf8')
    request=Request(endpoint,data=data,headers={'Content-Type':'application/json','Accept':'application/json',
                    'User-Agent':'MaaUma-ErrorFeedback/1'},method='POST')
    try:
        with build_opener(NoRedirect()).open(request,timeout=timeout) as response:
            raw=response.read(16*1024+1)
            if len(raw)>16*1024:raise FeedbackError('error_feedback_unavailable')
            answer=json.loads(raw.decode('utf-8-sig'))
    except HTTPError as error:
        if error.code==429:raise FeedbackError('error_feedback_rate_limited') from None
        if 400<=error.code<500:raise FeedbackError('error_feedback_rejected') from None
        raise FeedbackError('error_feedback_unavailable') from None
    except (OSError,ValueError):raise FeedbackError('error_feedback_unavailable') from None
    if (not isinstance(answer,dict) or answer.get('ok') is not True
            or not FEEDBACK_ID.fullmatch(str(answer.get('feedback_id','')))
            or answer.get('status') not in {'open','triaged','fixed','closed'}):
        raise FeedbackError('error_feedback_unavailable')
    ack={key:answer[key] for key in ('feedback_id','status')}
    report['acknowledgement']=ack;write_json(_path(root,report_id),report)
    return {'ok':True,**ack,'idempotent':answer.get('idempotent') is True}


def dispatch(root,value):
    if not isinstance(value,dict):raise FeedbackError('error_feedback_invalid')
    if value.get('action')=='prepare':return prepare(root,value)
    if value.get('action')=='submit':return submit(root,value.get('report_id'))
    raise FeedbackError('error_feedback_invalid')
