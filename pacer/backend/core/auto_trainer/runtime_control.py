"""Opt-in run control. Normal device input checks memory, never OCR or disk."""
from contextlib import contextmanager
from functools import wraps
import threading
import time


class RunStopped(BaseException):
    """Not an ordinary device failure; do not let broad retry handlers swallow it."""
    def __init__(self, reason="user_stop"):
        self.reason = reason
        super().__init__(reason)


class Reobserve(BaseException):
    pass


class ReplanTurn(BaseException):
    pass


class ResumeFromCurrentPage(BaseException):
    """Leave a paused career stack; its caller reattaches to the live page."""


_registry = {}
_registry_lock = threading.RLock()
_local = threading.local()


def current():
    return getattr(_local, "control", None)


def for_device(address):
    with _registry_lock:
        return _registry.get(address)


def device_control(device):
    bound = getattr(device, "_runtime_control", None)
    active = for_device(getattr(device, "address", ""))
    if active is not None and (bound is None or current() is active):
        device._runtime_control = active
        return active
    # A delayed old worker may not become uncontrolled after its run unbinds,
    # nor borrow a subsequent run's permission for the same device.
    return bound


class RunControl:
    def __init__(self, *, changed=None, clock=time.monotonic, pause_steps=None):
        self._condition = threading.Condition(threading.RLock())
        self._command = "run"
        self.state = "starting"
        self.epoch = 0
        self.inputs = 0
        self.in_flight = 0
        self.step = None
        self.turn = None
        self.clock = clock
        self.started = clock()
        self.paused_at = None
        self.paused_seconds = 0.
        self.finished_at = None
        self.changed = changed or (lambda state: None)
        self.pause_steps = None if pause_steps is None else frozenset(pause_steps)
        self.note = None
        self.restart_on_resume = False

    def request(self, action):
        with self._condition:
            if self.finished_at is not None:
                return
            if action == "pause":
                if self._command == "run":
                    self._command = "pause"
            elif action == "resume":
                if self._command == "pause":
                    self._command = "run"
            elif action == "stop":
                self._command = "stop"
            else:
                raise ValueError("unknown_control_action")
            self._condition.notify_all()

    @property
    def stopping(self):
        with self._condition:
            return self._command == "stop"

    def snapshot(self):
        with self._condition:
            end = self.finished_at if self.finished_at is not None else self.clock()
            paused = self.paused_seconds + (max(0., end-self.paused_at) if self.paused_at is not None else 0.)
            elapsed = max(0., end-self.started)
            return {"state": self.state, "step": self.step, "turn": self.turn,
                    "elapsed_seconds": elapsed, "paused_seconds": paused,
                    "active_seconds": max(0., elapsed-paused), "input_in_flight": bool(self.in_flight),
                    "resume_epoch": self.epoch,"pause_requested":self._command=='pause','note':self.note}

    def checkpoint(self, step=None, *, on_paused=None):
        while True:
            publish = None
            reattach = False
            with self._condition:
                if step:
                    self.step = step
                if self._command == "stop":
                    raise RunStopped()
                if self._command == "pause" and (self.state=='paused' or self.pause_steps is None or self.step in self.pause_steps):
                    if self.in_flight:
                        self._condition.wait(.05)
                        continue
                    if self.state != "paused":
                        self.state = "paused"
                        self.paused_at = self.clock()
                        publish = "paused"
                    else:
                        self._condition.wait()
                        continue
                else:
                    if self.state == "paused":
                        self.paused_seconds += self.clock() - self.paused_at
                        self.paused_at = None
                        self.epoch += 1
                        self.state = "running"
                        publish = "running"
                        reattach = self.restart_on_resume
                    elif self.state == "starting":
                        self.state = "running"
                        publish = "running"
                    epoch = self.epoch
            if publish:
                self.changed(publish)
                if publish == "paused" and on_paused is not None:
                    on_paused(self.snapshot())
            if reattach:
                raise ResumeFromCurrentPage()
            if publish != "paused":
                return epoch

    def begin_input(self, observed_epoch):
        while True:
            epoch = self.checkpoint("before_input")
            with self._condition:
                # Serialize accepting a command with accepting an input. A pause
                # received after this point sees this one input as in flight.
                if self._command != "run" and not (self._command=='pause' and self.pause_steps is not None and self.step not in self.pause_steps):
                    continue
                if observed_epoch != epoch:
                    raise Reobserve()
                self.in_flight += 1
                return

    def end_input(self):
        with self._condition:
            self.in_flight -= 1
            self.inputs += 1
            self._condition.notify_all()

    def sleep(self, seconds):
        deadline = self.clock() + max(0., seconds)
        while True:
            before = self.clock()
            self.checkpoint("waiting")
            deadline += self.clock() - before
            with self._condition:
                remaining = deadline-self.clock()
                if remaining <= 0:
                    return
                self._condition.wait(min(remaining, .1))

    def finish(self):
        with self._condition:
            if self.finished_at is not None:
                return
            self.finished_at = self.clock()
            self.state = "stopped"
            self._command = "stop"
            self._condition.notify_all()


@contextmanager
def current_page_resume(control):
    """A career-level boundary owns recovery, never a stale nested click."""
    previous = control.restart_on_resume
    control.restart_on_resume = True
    try:
        yield
    finally:
        control.restart_on_resume = previous


@contextmanager
def bind(control, address):
    with _registry_lock:
        if address in _registry:
            raise RuntimeError("device_runner_already_active")
        _registry[address] = control
    previous = current()
    _local.control = control
    try:
        yield control
    finally:
        _local.control = previous
        with _registry_lock:
            if _registry.get(address) is control:
                del _registry[address]


def capture_checkpoint(device):
    control = device_control(device)
    if control:
        paused_before = control.paused_seconds
        # Legacy executors finish bookkeeping first. A live-page resume owner
        # can instead pause after the submitted input and later abandon that
        # old stack; it must not wait for the whole animation/result chain.
        transaction = getattr(_local, "transaction", None)
        finishing_input = (getattr(_local, "input_control", None) is control
                           or transaction is not None and control.inputs > transaction
                           and not control.restart_on_resume)
        epoch = control.epoch if finishing_input else control.checkpoint("capture")
        device._control_epoch = epoch
        return control.paused_seconds - paused_before
    return 0.


@contextmanager
def input_guard(device):
    control = device_control(device)
    if control is None:
        yield
        return
    if current() is not control:
        raise RunStopped("input_outside_owned_runner")
    if getattr(_local, "input_control", None) is control:
        yield
        return
    control.begin_input(getattr(device, "_control_epoch", 0))
    previous = getattr(_local, "input_control", None)
    _local.input_control = control
    try:
        yield
    finally:
        _local.input_control = previous
        control.end_input()


def guarded_input(fn):
    @wraps(fn)
    def wrapped(device, *args, **kwargs):
        with input_guard(device):
            return fn(device, *args, **kwargs)
    return wrapped


def controlled_sleep(seconds):
    control = current()
    if control:
        # Post-input bookkeeping must be allowed to finish. These short waits
        # do not authorize another click; the next input has its own gate.
        transaction = getattr(_local, "transaction", None)
        if (getattr(_local, "input_control", None) is control
                or transaction is not None and control.inputs > transaction and not control.restart_on_resume):
            time.sleep(seconds)
        else:
            control.sleep(seconds)
    else:
        time.sleep(seconds)


def reobserving_step(fn):
    """Retry only an observation interrupted before this step sent any input."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        control = current()
        if control is None:
            return fn(*args, **kwargs)
        while True:
            previous = getattr(_local, "transaction", None)
            start = control.inputs
            _local.transaction = start
            try:
                return fn(*args, **kwargs)
            except Reobserve:
                if control.inputs != start:
                    raise RunStopped("resume_partial_action_requires_reconciliation")
                # run_once captures and plans again; no old coordinates survive.
            finally:
                _local.transaction = previous
    return wrapped


def decision_scope(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        previous = getattr(_local, "decision_epoch", None)
        control = current()
        _local.decision_epoch = control.epoch if control else None
        try:
            return fn(*args, **kwargs)
        except Reobserve:
            raise ReplanTurn()
        finally:
            _local.decision_epoch = previous
    return wrapped


def turn_action(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        control = current()
        if control is None:
            return fn(*args, **kwargs)
        start = control.inputs
        previous = getattr(_local, "transaction", None)
        _local.transaction = start
        try:
            control.checkpoint(fn.__name__)
            if getattr(_local, "decision_epoch", control.epoch) not in (None, control.epoch):
                raise ReplanTurn()
            return fn(*args, **kwargs)
        except Reobserve:
            if control.inputs != start:
                raise RunStopped("resume_partial_action_requires_reconciliation")
            raise ReplanTurn()
        finally:
            _local.transaction = previous
    return wrapped
