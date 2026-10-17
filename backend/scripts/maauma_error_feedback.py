"""JSON stdin/stdout bridge for an explicitly clicked error report action."""
import argparse
import json
from pathlib import Path
import sys

from desktop.maauma_error_feedback import FeedbackError,MAX_REQUEST,dispatch
from desktop.product_updates import UpdateError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        raw=sys.stdin.read(MAX_REQUEST+1)
        if len(raw)>MAX_REQUEST:raise FeedbackError('error_feedback_invalid')
        value=json.loads(raw)
        answer=dispatch(args.root,value)
    except FeedbackError as error:answer={'ok':False,'code':str(error)}
    except (OSError,ValueError,TypeError,KeyError,UpdateError):answer={'ok':False,'code':'error_feedback_invalid'}
    print(json.dumps(answer,ensure_ascii=False));return 0 if answer.get('ok') else 1


if __name__=='__main__':raise SystemExit(main())
