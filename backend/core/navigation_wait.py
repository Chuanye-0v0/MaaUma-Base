"""Observe passive page transitions immediately; never repeat the submitted tap."""
import time

def same_controls(before, after, tolerance=4):
    """Compare control identity, ignoring only tiny OCR point drift at 720p.

    This is a passive-wait comparison, never action authorization.
    """
    if not isinstance(before, dict) or not isinstance(after, dict) or before.keys() != after.keys():
        return False
    for action, left in before.items():
        right = after[action]
        if not isinstance(left, dict) or not isinstance(right, dict):
            if left != right:
                return False
            continue
        if {k: v for k, v in left.items() if k != 'point'} != {k: v for k, v in right.items() if k != 'point'}:
            return False
        a, b = (left.get('point'), right.get('point'))
        if a == b:
            continue
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and (len(a) == len(b) == 2) and all((type(v) in (int, float) for v in [*a, *b])) and all((abs(x - y) <= tolerance for x, y in zip(a, b)))):
            return False
    return True

def wait_transition(capture, stop, before, timeout=20, *, same_page_ready=None):
    deadline = time.monotonic() + timeout
    while True:
        if stop.is_set():
            raise RuntimeError('operator_stop_after_submit')
        after = capture()
        if same_page_ready is not None and after.get('page') == before.get('page') and same_page_ready(after):
            if stop.is_set():
                raise RuntimeError('operator_stop_after_submit')
            return after
        if after.get('page') not in {before.get('page'), 'unknown', 'loading'}:
            return after
        if after.get('page') == before.get('page') and after.get('actions') and (not same_controls(after['actions'], before.get('actions'))):
            return after
        if after.get('page') == before.get('page') == 'notice' and after.get('actions'):
            labels = lambda frame: [t.get('text') for t in frame.get('ui_labels', [])]
            if labels(after) != labels(before):
                return after
        if time.monotonic() >= deadline:
            return after
        if stop.wait(0.2):
            raise RuntimeError('operator_stop_after_submit')
