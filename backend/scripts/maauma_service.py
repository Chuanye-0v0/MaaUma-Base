"""JSON entry point for suite activation, component preparation and installation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

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
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--request-file',type=Path)
    args=parser.parse_args(argv)
    try:
        raw=args.request_file.read_text(encoding='utf8') if args.request_file else sys.stdin.read(65537)
        if len(raw)>65536:raise ServiceError('request_too_large')
        value=json.loads(raw)
        response=dispatch(args.root,value)
        if response.get('ok') and value.get('action')=='apply' and value.get('restart') is True:
            try:
                process=(relaunch or _relaunch)(args.root)
                response['restarted']=True
                if getattr(process,'pid',None):response['restart_pid']=process.pid
            except OSError:
                # The transaction already succeeded. A GUI startup failure is
                # reported separately and never described as an install failure.
                response.update(restarted=False,restart_code='gui_restart_failed')
    except (ServiceError,UpdateError) as error:response={'ok':False,'code':str(error)}
    except (OSError,ValueError,TypeError,KeyError):response={'ok':False,'code':'suite_operation_failed'}
    print(json.dumps(response,ensure_ascii=False));return 0 if response.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
