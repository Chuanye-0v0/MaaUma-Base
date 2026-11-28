"""Only the owned skill child receives this authenticated local control pipe."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from multiprocessing.connection import Listener

from core.auto_trainer.runtime_control import current, RunStopped, Reobserve


def _process_cpu_seconds(pid: int) -> float | None:
    """Return user + kernel CPU seconds for *pid* on Windows, or None."""
    if os.name != "nt" or not pid or int(pid) <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        kernel_100ns = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
        user_100ns = (user.dwHighDateTime << 32) | user.dwLowDateTime
        return (kernel_100ns + user_100ns) * 1e-7
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Terminate *process* and its descendants best-effort."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    else:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass


def _decode_stream(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _persist_stream_logs(diag_dir: Path, pid: int, stdout, stderr) -> None:
    diag_dir.mkdir(parents=True, exist_ok=True)
    for ext, content in (("stdout.log", stdout), ("stderr.log", stderr)):
        text = _decode_stream(content)
        if text:
            (diag_dir / f"worker_{pid}.{ext}").write_text(text, encoding="utf-8", errors="replace")


def _write_hang_diagnostics(diagnostics_dir: str | Path, payload: dict, stdout, stderr) -> Path | None:
    try:
        diag_dir = Path(diagnostics_dir)
        diag_dir.mkdir(parents=True, exist_ok=True)
        out_path = diag_dir / "worker_hang_diagnostics.json"
        enriched = dict(payload)
        enriched["stdout_tail"] = _decode_stream(stdout)[-2000:]
        enriched["stderr_tail"] = _decode_stream(stderr)[-2000:]
        tmp_path = diag_dir / f".worker_hang_diagnostics.{os.getpid()}.tmp"
        tmp_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, out_path)
        _persist_stream_logs(diag_dir, int(payload.get("pid") or 0), stdout, stderr)
        return out_path
    except Exception:
        return None


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
    kwargs = dict(kwargs)
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    startup_timeout = kwargs.pop("startup_timeout", None)
    startup_probe = kwargs.pop("startup_probe", None)
    total_timeout = kwargs.pop("total_timeout", None)
    diagnostics_dir = kwargs.pop("diagnostics_dir", None)

    control = current()
    if (
        control is None
        and startup_timeout is None
        and total_timeout is None
        and diagnostics_dir is None
    ):
        completed = subprocess.run(argv, **kwargs)
        completed.startup_stalled = False
        completed.timed_out = False
        completed.diagnostics_path = None
        return completed

    server = None
    if control is not None:
        control.checkpoint("skills_start")
        server = ChildControlServer(control)
        kwargs["env"] = server.environment(kwargs.get("env", os.environ))
    capture_output = kwargs.pop("capture_output", False)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    env_keys = sorted((kwargs.get("env") or os.environ).keys())
    stop_since = None
    started = time.monotonic()
    cpu_at_half = None
    cpu_now = None
    cpu_delta = None
    startup_stalled = False
    timed_out = False
    probe_ok = False
    stdout = None
    stderr = None
    child_pid = None
    try:
        with subprocess.Popen(argv, **kwargs) as process:
            child_pid = process.pid
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    elapsed = now - started
                    if control is not None and control.stopping:
                        stop_since = now if stop_since is None else stop_since
                        if now - stop_since >= 2:
                            _kill_process_tree(process)
                            try:
                                stdout, stderr = process.communicate(timeout=5)
                            except (subprocess.TimeoutExpired, OSError):
                                stdout, stderr = None, None
                            break
                        continue
                    if startup_timeout is not None and not probe_ok:
                        if startup_probe is not None:
                            try:
                                if startup_probe():
                                    probe_ok = True
                            except Exception:
                                pass
                        if not probe_ok:
                            if elapsed >= startup_timeout * 0.4 and cpu_at_half is None:
                                cpu_at_half = _process_cpu_seconds(process.pid)
                            if elapsed >= startup_timeout:
                                cpu_now = _process_cpu_seconds(process.pid)
                                cpu_delta = (
                                    None
                                    if (cpu_now is None or cpu_at_half is None)
                                    else max(0.0, cpu_now - cpu_at_half)
                                )
                                if cpu_now is not None and (
                                    cpu_now <= 0.05 or (cpu_delta is not None and cpu_delta < 0.1)
                                ):
                                    startup_stalled = True
                                    timed_out = True
                                    _kill_process_tree(process)
                                    try:
                                        stdout, stderr = process.communicate(timeout=5)
                                    except (subprocess.TimeoutExpired, OSError):
                                        stdout, stderr = None, None
                                    break
                    if total_timeout is not None and elapsed >= total_timeout:
                        timed_out = True
                        _kill_process_tree(process)
                        try:
                            stdout, stderr = process.communicate(timeout=5)
                        except (subprocess.TimeoutExpired, OSError):
                            stdout, stderr = None, None
                        break
            if control is not None and control.stopping:
                raise RunStopped()
            rc = process.returncode if process.returncode is not None else -1
            diag_path = None
            if diagnostics_dir is not None:
                if startup_stalled or timed_out:
                    diag_path = _write_hang_diagnostics(
                        diagnostics_dir,
                        {
                            "pid": child_pid,
                            "argv": [str(item) for item in argv],
                            "cpu_seconds": cpu_now,
                            "cpu_delta_seconds": cpu_delta,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "startup_timeout": startup_timeout,
                            "total_timeout": total_timeout,
                            "startup_stalled": startup_stalled,
                            "timed_out": timed_out,
                            "returncode": rc,
                            "env_keys": env_keys,
                        },
                        stdout,
                        stderr,
                    )
                elif rc != 0:
                    try:
                        _persist_stream_logs(Path(diagnostics_dir), int(child_pid or 0), stdout, stderr)
                    except Exception:
                        pass
            completed = subprocess.CompletedProcess(argv, rc, stdout, stderr)
            completed.startup_stalled = startup_stalled
            completed.timed_out = timed_out
            completed.diagnostics_path = str(diag_path) if diag_path is not None else None
            return completed
    finally:
        if server is not None:
            server.close()
