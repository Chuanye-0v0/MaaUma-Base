"""Reuse OCR only while the same run's SS text/control pixels are unchanged."""
import hashlib
import time

import cv2


def _key(image):
    if image is None or image.shape not in {(1280,720,3),(2560,1440,3)}:return None
    from scripts.capture_ug_parent_live_tabs import _selected_larc_tab_fast
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    frame=image if image.shape[1]==720 else cv2.resize(image,(720,1280),interpolation=cv2.INTER_AREA)
    if (_selected_larc_tab_fast(frame)!='ss'
            or SafeAdvancePlanner._looks_like_confirm_modal_panel(frame)):return None
    digest=hashlib.sha256()
    # Header, all five participant cards, totals, numeric previews and every
    # control are included. Only the framed 3D character area has no UI text.
    for region in (frame[:205],frame[205:738,:370],frame[630:]):
        digest.update(region.tobytes())
    return digest.hexdigest()


def cached_text(analyzer,image,run_id,*,now=None):
    key=_key(image);cache=getattr(analyzer,'_larc_ss_text_cache',None)
    clock=time.monotonic() if now is None else now
    if key is None:
        analyzer._larc_ss_text_cache=None
        return None
    if cache and cache[0]==run_id and cache[1]==key and 0<=clock-cache[3]<=15:
        return cache[2]
    return None


def remember_text(analyzer,image,run_id,packet,*,now=None):
    key=_key(image)
    if key is not None and packet.text_status=='ok' and not packet.dangerous and not packet.errors and not packet.resource_terms:
        analyzer._larc_ss_text_cache=(run_id,key,packet.visible_text,time.monotonic() if now is None else now)
    else:
        analyzer._larc_ss_text_cache=None
