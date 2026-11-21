"""Submit one frozen stop report. No host, device, screenshot capture or game input."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

BACKEND=Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:sys.path.insert(0,str(BACKEND))


def failure_result(error):
    """Keep transport failure distinct from a BF acknowledgement; no raw body."""
    code=getattr(error,'code',None);status=None
    if isinstance(error,HTTPError):
        status=error.code
        code='bug_feedback_too_large'if status==413 else 'runtime_stop_report_rate_limited'if status==429 else 'runtime_stop_report_unavailable'
        try:
            response=json.loads(error.read(8192));candidate=response.get('code')if isinstance(response,dict)else None
            if isinstance(candidate,str)and re.fullmatch('[a-z][a-z0-9_]{0,100}',candidate)and candidate.startswith(('runtime_stop_report_','feedback_','bug_feedback_')):
                code=candidate
        except (ValueError,OSError,UnicodeError,TypeError,AttributeError):pass
    elif not isinstance(code,str):code=str(error)
    if (not isinstance(code,str)or not re.fullmatch('[a-z][a-z0-9_]{0,100}',code)
            or not code.startswith(('runtime_stop_report_','feedback_','bug_feedback_','invalid_response'))):
        code='runtime_stop_report_submit_failed'
    messages={'feedback_upload_unsupported':'服务尚未支持分段上传；原报告保留，请等待服务更新后重试。',
        'feedback_storage_unavailable':'反馈服务的存储暂时不可用；原报告和分段进度已保留，请稍后重试。',
        'feedback_upload_missing':'上传暂存已过期；原报告保留，再次提交可重新开始。',
        'feedback_upload_incomplete':'分段尚未传完；原报告和分段进度保留，可继续重试。',
        'bug_feedback_too_large':'报告或所选截图超过服务大小限制，本次提交未确认；原报告保留，可复制文字或稍后重试。',
        'runtime_stop_report_rate_limited':'反馈服务暂时限流，本次提交未确认；原报告保留，请稍后重试。'}
    result={'ok':False,'code':code,'message':messages.get(code,'报告及所选截图尚未确认提交；原报告和分段进度已保留，可重试同一份报告。'),
        'submission_confirmed':False,'report_preserved':True,'automatic_retry':False}
    if status is not None:result['http_status']=status
    return result

def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args(argv)
    root=args.root.resolve(strict=True)
    if root.name.casefold()=='pacer':root=root.parent
    if not(root/'maauma-suite.json').is_file() or BACKEND!=root/'pacer/backend':
        print(json.dumps({'ok':False,'code':'runtime_stop_report_root_invalid'}));return 1
    try:
        value=json.load(sys.stdin)
        if not isinstance(value,dict) or set(value)!={'action','report_id'} or value['action']!='submit_runtime_stop_report' or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',str(value['report_id'])):
            raise ValueError('runtime_stop_report_invalid')
        from desktop.product_updates import backend_process_lease
        with backend_process_lease(root/'pacer'):
            from desktop.runtime_stop_reports import submit_for_host
            result=submit_for_host(SimpleNamespace(umaa_root=root/'backend'),value)
    except Exception as error:
        result=failure_result(error)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result.get('ok') else 1

if __name__=='__main__':raise SystemExit(main())
