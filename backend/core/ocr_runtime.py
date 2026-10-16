"""Bounded CPU thread defaults for local OCR; every call owns a new engine.

Only inference thread counts change. Models, image geometry, confidence
thresholds and explicit caller options remain untouched.
"""
import os

def create_ocr(**options):
    from rapidocr_onnxruntime import RapidOCR
    options.setdefault('intra_op_num_threads', min(4, os.cpu_count() or 1))
    options.setdefault('inter_op_num_threads', 1)
    return RapidOCR(**options)
