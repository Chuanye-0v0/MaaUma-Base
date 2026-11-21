"""One lazy OCR model per calling thread, with bounded inference parallelism.

Only model weights and inference sessions are reused. Images and recognition
results are never cached here. RapidOCR changes internal per-call state, so a
model is not shared between independent control threads.
"""
import os
from threading import local

_ENGINES = local()


def default_rapidocr_engine():
    engine = getattr(_ENGINES, 'engine', None)
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR
        # Respect explicit single-thread limits (e.g. OMP_NUM_THREADS=1) while
        # capping default ONNX thread pools to at most 2 threads.
        try:
            env_threads = int(os.environ.get("OMP_NUM_THREADS") or 0)
        except ValueError:
            env_threads = 0
        default_cap = min(2, os.cpu_count() or 1)
        intra_threads = max(1, min(env_threads, default_cap)) if env_threads > 0 else default_cap
        engine = RapidOCR(intra_op_num_threads=intra_threads,
                          inter_op_num_threads=1)
        _ENGINES.engine = engine
    return engine
