"""One JSON request on stdin, one redacted JSON response; no device required."""
import argparse
import json
import sys

from desktop import notifications


def dispatch(request, *, root):
    if not isinstance(request, dict):
        raise notifications.NotificationError('invalid_request')
    action = request.get('action')
    if action == 'status':
        return notifications.status(root)
    if action == 'save':
        return notifications.save(root, request.get('token'))
    if action == 'disable':
        return notifications.disable(root)
    if action == 'use_legacy':
        return notifications.use_legacy(root)
    if action == 'set_events':
        return notifications.set_events(root, request.get('events'))
    if action == 'test':
        return notifications.test_notification(root)
    if action in {'send', 'emit'}:
        return notifications.send_event(root, event_id=request.get('event_id'),
            title=request.get('title'), content=request.get('content'), dedupe_key=request.get('dedupe_key'))
    raise notifications.NotificationError('invalid_request')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    arguments = parser.parse_args(argv)
    exit_code = 0
    try:
        raw = sys.stdin.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise notifications.NotificationError('invalid_request')
        request = json.loads(raw)
        result = dispatch(request, root=arguments.root)
    except notifications.NotificationError as error:
        code = str(error) if str(error) in notifications.MESSAGES else 'invalid_request'
        result = {'ok': False, 'status': 'error', 'code': code, 'message': notifications.MESSAGES[code]}
        exit_code = 2
    except Exception:
        result = {'ok': False, 'status': 'error', 'code': 'invalid_request',
                  'message': notifications.MESSAGES['invalid_request']}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
