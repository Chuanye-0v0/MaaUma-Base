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
        engine = RapidOCR(intra_op_num_threads=min(2, os.cpu_count() or 1), inter_op_num_threads=1)
        _ENGINES.engine = engine
    return engine
