"""Only the owned skill child receives this authenticated local control pipe."""
import json
import os
import secrets
import subprocess
import threading
import time
from multiprocessing.connection import Listener

from core.auto_trainer.runtime_control import current, RunStopped, Reobserve


class ChildControlServer:
    def __init__(self, control):
        self.control = control
        self.key = secrets.token_bytes(24)
        self.family = "AF_PIPE" if os.name == "nt" else "AF_INET"
        address = r"\\.\pipe\uma-control-" + secrets.token_hex(8) if os.name == "nt" else ("127.0.0.1", 0)
        self.listener = Listener(address, family=self.family, authkey=self.key)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def environment(self, env):
        return dict(env, UMA_RUN_CONTROL=json.dumps({"address":self.listener.address,
                    "family":self.family, "key":self.key.hex()}))

    def _serve(self):
        while not self.closed.is_set():
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                break
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection):
        in_flight = False
        invalidated = False
        try:
            while not self.closed.is_set():
                request = connection.recv()
                operation = request.get("operation")
                try:
                    if invalidated:
                        connection.send({"error":"skill_resume_requires_replan"})
                        continue
                    if operation == "capture":
                        value = {"epoch": self.control.checkpoint("skills_capture")}
                    elif operation == "begin_input":
                        if in_flight:
                            raise ValueError("input_already_in_flight")
                        self.control.begin_input(request["epoch"])
                        in_flight, value = True, {"ok": True}
                    elif operation == "end_input" and in_flight:
                        self.control.end_input()
                        in_flight, value = False, {"ok": True}
                    else:
                        raise ValueError("invalid_child_control_operation")
                except RunStopped:
                    value = {"error":"user_stop"}
                except Reobserve:
                    # D1 purchase plans cannot be replayed at an arbitrary cart
                    # position. Stop that batch, preserving its transaction files.
                    value = {"error":"skill_resume_requires_replan"}
                    invalidated = True
                connection.send(value)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            if in_flight:
                self.control.end_input()
            connection.close()

    def close(self):
        self.closed.set()
        self.listener.close()


def run_controlled_process(argv, **kwargs):
    # Skill workers are background console applications launched by a GUI.
    # Pipes redirect output, but alone do not suppress Windows console creation.
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    control = current()
    if control is None:
        return subprocess.run(argv, **kwargs)
    control.checkpoint("skills_start")
    server = ChildControlServer(control)
    kwargs["env"] = server.environment(kwargs.get("env", os.environ))
    capture_output = kwargs.pop("capture_output", False)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stop_since = None
    try:
        with subprocess.Popen(argv, **kwargs) as process:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    if control.stopping:
                        stop_since = time.monotonic() if stop_since is None else stop_since
                        if time.monotonic()-stop_since >= 2:
                            process.terminate()
                            stdout, stderr = process.communicate(timeout=5)
                            break
            if control.stopping:
                raise RunStopped()
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        server.close()
