"""Standalone skill-process control client; safe to load beside D1's packages."""
import json
import os
from multiprocessing.connection import Client


def portable_sitecustomize():
    return '''# Portable paths; isolated D1 workers must not preload the main core.
import os
from pathlib import Path
if os.environ.get("UMA_CUT_PORTABLE_ROOT"):
    root=Path(os.environ["UMA_CUT_PORTABLE_ROOT"])
    if root.name.casefold()=="backend":root=root.parent
    ocr=root/"ocr"
    if (ocr/"tesseract.exe").is_file():
        os.environ["PATH"]=str(ocr)+os.pathsep+os.environ.get("PATH","")
        os.environ["TESSDATA_PREFIX"]=str(ocr/"tessdata")
    if os.environ.get("UMA_ISOLATED_SKILL_WORKER")=="1":
        import importlib.util
        spec=importlib.util.spec_from_file_location("standalone_image_paths",root/"backend/scripts/larc_worker_control.py")
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module.enable_unicode_image_paths()
    else:
        from core.cut_portable_runtime import enable_unicode_image_paths
        enable_unicode_image_paths()
'''


def enable_unicode_image_paths():
    """Same process-local OpenCV path support without importing either core."""
    from pathlib import Path
    import cv2
    import numpy as np
    if getattr(cv2.imread,'_cut_unicode_paths',False):return
    original_read,original_write=cv2.imread,cv2.imwrite
    def read(filename,flags=cv2.IMREAD_COLOR):
        path=os.fsdecode(filename)
        if path.isascii():return original_read(path,flags)
        try:data=Path(path).read_bytes()
        except OSError:return None
        return cv2.imdecode(np.frombuffer(data,dtype=np.uint8),flags) if data else None
    def write(filename,img,params=None):
        path=os.fsdecode(filename)
        if path.isascii():return original_write(path,img,params or [])
        ok,data=cv2.imencode(Path(path).suffix,img,params or [])
        if not ok:return False
        try:Path(path).write_bytes(data.tobytes())
        except OSError:return False
        return True
    read._cut_unicode_paths=True
    cv2.imread,cv2.imwrite=read,write


class WorkerStopped(RuntimeError):
    pass


class WorkerControl:
    def __init__(self, configuration):
        data = json.loads(configuration)
        if isinstance(data["address"], list):
            data["address"] = tuple(data["address"])
        self.connection = Client(data["address"], family=data["family"], authkey=bytes.fromhex(data["key"]))
        self.epoch = 0

    def call(self, operation, **data):
        try:
            self.connection.send({"operation": operation, **data})
            result = self.connection.recv()
        except (EOFError, OSError) as exc:
            raise WorkerStopped("control_connection_lost") from exc
        if result.get("error"):
            raise WorkerStopped(result["error"])
        return result


def controlled_device_class(base):
    configuration = os.environ.get("UMA_RUN_CONTROL")
    if not configuration:
        return base
    class ControlledDevice(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._run_control = WorkerControl(configuration)

        def command(self, *args):
            capture = args == ("-s", self.endpoint, "exec-out", "screencap", "-p")
            is_input = args[:4] == ("-s", self.endpoint, "shell", "input")
            if capture:
                self._run_control.epoch = self._run_control.call("capture")["epoch"]
            if is_input:
                self._run_control.call("begin_input", epoch=self._run_control.epoch)
            try:
                return super().command(*args)
            finally:
                if is_input:
                    self._run_control.call("end_input")
    return ControlledDevice
