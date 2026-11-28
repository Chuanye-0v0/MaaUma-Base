"""JSON entry point for suite activation, component preparation and installation."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def _relaunch(root):
    from desktop.product_updates import contained
    root=Path(root).resolve(strict=True)
    executable=contained(root,'MFAAvalonia.exe')
    if not executable.is_file():raise OSError('suite_gui_missing')
    environment=dict(os.environ,DOTNET_ROOT=str(root/'dotnet'),DOTNET_MULTILEVEL_LOOKUP='0')
    return subprocess.Popen([str(executable)],cwd=root,env=environment,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def main(argv=None,*,relaunch=None):
    from desktop.maauma_service import dispatch,ServiceError
    from desktop.product_updates import UpdateError
    from desktop.update_connectivity import bind_reporter,failure_details
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--request-file',type=Path)
    parser.add_argument('--progress-jsonl',action='store_true',help='Emit credential-free update progress JSON lines to stderr.')
    args=parser.parse_args(argv)
    try:
        raw=args.request_file.read_text(encoding='utf8') if args.request_file else sys.stdin.read(65537)
        if len(raw)>65536:raise ServiceError('request_too_large')
        value=json.loads(raw)
    except (ServiceError,UpdateError) as error:
        print(json.dumps({'ok':False,'code':str(error)},ensure_ascii=False));return 1
    except (OSError,ValueError,TypeError,KeyError):
        print(json.dumps({'ok':False,'code':'suite_operation_failed'},ensure_ascii=False));return 1
    long_operation=isinstance(value,dict) and value.get('action') in {'activate','prepare_pacer','prepare_update'}
    if args.progress_jsonl and long_operation:
        from desktop.update_progress import ProgressReporter,jsonl_sink
        progress_context=ProgressReporter(jsonl_sink(sys.stderr))
    else:progress_context=nullcontext(None)
    with progress_context as progress,bind_reporter(progress):
        try:
            response=dispatch(args.root,value,progress=progress) if progress is not None else dispatch(args.root,value)
        except (ServiceError,UpdateError) as error:
            response={'ok':False,'code':str(error),**failure_details(error)}
            if isinstance(getattr(error,'identity_diagnostic',None),dict) and error.identity_diagnostic:
                response['identity_diagnostic']=error.identity_diagnostic
            if getattr(error,'error_info',None) is not None:response['error']=error.error_info
            for key in ('failed_path','ticket','log_path'):
                extra=getattr(error,key,None)
                if extra is not None:response[key]=extra
        except (OSError,ValueError,TypeError,KeyError):response={'ok':False,'code':'suite_operation_failed'}
        if isinstance(value,dict) and value.get('action')=='apply':
            try:
                from desktop.maauma_components import record_apply_result
                record_apply_result(args.root,value,response)
            except Exception:
                pass
            if value.get('restart') is True and response.get('code')!='application_running':
                try:
                    time.sleep(0.3)
                    process=(relaunch or _relaunch)(args.root)
                    response['restarted']=True
                    if getattr(process,'pid',None):response['restart_pid']=process.pid
                except OSError:
                    # A GUI startup failure is reported separately and never masked.
                    response.update(restarted=False,restart_code='gui_restart_failed')
        if progress is not None:progress.finish(response)
    print(json.dumps(response,ensure_ascii=False));return 0 if response.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
