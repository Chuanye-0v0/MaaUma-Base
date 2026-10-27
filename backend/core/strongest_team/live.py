"""The existing SDK transport, one observed input per action boundary."""
import hashlib
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np

from core.action_recovery import action_boundary, await_candidate, ReobserveRequired, require_fresh_observation
from core.navigation_wait import wait_transition
from core.support_library.live import SupportDevice

from .observer import observe, clean
from .policy import authorize


def same_candidate(left, right):
    for field in ['effect','resource_kind','cost','quantity','stock_before','stock_after']:
        if left.get(field)!=right.get(field):
            return False
    key='swipe' if 'swipe' in left else 'point'
    if ('swipe' in left)!=('swipe' in right):return False
    a,b=left.get(key),right.get(key)
    return (isinstance(a,(list,tuple)) and isinstance(b,(list,tuple)) and len(a)==len(b)
            and all(type(x) in (int,float) and math.isfinite(x) for x in [*a,*b])
            and all(abs(x-y)<=4 for x,y in zip(a,b)))


class StrongestTeamDevice(SupportDevice):
    def __init__(self, endpoint, adb_path, output_dir, *, journal=None, options=None):
        self.journal=journal
        self.options=options or {}
        self._last_raw_sha=None
        self._last_raw_path=None
        from .journal import game_day
        self.day_clock=game_day
        super().__init__(endpoint, adb_path, output_dir)

    def _region(self,image,tokens,box):
        x,y,r,b=box
        rows,_=self.ocr(cv2.resize(image[y:b,x:r],None,fx=3,fy=3))
        for corners,text,confidence in rows or []:
            tokens.append(dict(text=text,confidence=float(confidence),bbox=[
                x+int(min(p[0] for p in corners)/3),y+int(min(p[1] for p in corners)/3),
                x+int(max(p[0] for p in corners)/3),y+int(max(p[1] for p in corners)/3)]))

    def _digits(self,image,tokens,box):
        x,y,r,b=box
        rows,_=self.ocr(cv2.resize(image[y:b,x:r],None,fx=3,fy=3),use_det=False,use_cls=False)
        if rows and isinstance(rows[0],(tuple,list)) and len(rows[0])==2:
            text,confidence=rows[0]
            if str(text).isdigit() and float(confidence)>=.9:
                tokens.append(dict(text=str(text),confidence=float(confidence),bbox=list(box)))

    def capture(self):
        raw=self.command('-s',self.endpoint,'exec-out','screencap','-p')
        captured=time.monotonic()
        image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        if image is None or image.shape[:2]!=(1280,720):
            raise RuntimeError('strongest_team_portrait_720_required')
        rows,_=self.ocr(image)
        tokens=[dict(text=text,confidence=float(confidence),bbox=[
            int(min(p[0] for p in box)),int(min(p[1] for p in box)),
            int(max(p[0] for p in box)),int(max(p[1] for p in box))])
            for box,text,confidence in rows or []]
        context=self.journal.data if self.journal is not None else {}
        frame=observe(image,tokens,context)
        if frame['page']=='st_grade_reward' and frame.get('team_total') is None:
            self._region(image,tokens,(200,440,520,560))
            self._digits(image,tokens,(235,480,490,550))
            frame=observe(image,tokens,context)
        if frame['page']=='unknown' and context.get('context_verified') and any(clean(t['text'])=='上一页' for t in tokens):
            self._region(image,tokens,(200,1160,520,1250))
            frame=observe(image,tokens,context)
        if frame['page']=='st_retry' and ('confirm_retry' not in frame['actions']):
            self._region(image,tokens,(400,735,495,795))
            self._region(image,tokens,(505,735,610,795))
            frame=observe(image,tokens,context)
        if frame['page']=='st_refill' and ('confirm_refill' not in frame['actions']):
            self._region(image,tokens,(320,730,455,800))
            self._region(image,tokens,(515,730,630,800))
            frame=observe(image,tokens,context)
        if ('home' not in frame['actions'] and frame['page'] in {'st_hub','st_closed','st_opponents'}):
            self._region(image,tokens,(290,1200,430,1280))
            frame=observe(image,tokens,context)
        digest=hashlib.sha256(raw).hexdigest()
        path=Path(self._last_raw_path) if self._last_raw_sha==digest and self._last_raw_path else None
        if path is None or not path.is_file() or path.parent.resolve()!=Path(self.output_dir).resolve():
            path=Path(self.output_dir)/(str(time.time_ns())+'.png')
            path.write_bytes(raw)
        self._last_raw_sha=digest;self._last_raw_path=str(path)
        frame.update(endpoint=self.endpoint,screenshot_path=str(path),captured_monotonic=captured,
                     frame_sha256=digest,screen_size=[720,1280])
        from .storage import register
        register(self.journal,frame)
        from .journal import atomic_write
        atomic_write(Path(self.output_dir)/'strongest-team-observation.json',frame)
        self._latest=frame
        return frame

    @action_boundary
    def step(self, action, stop, expected):
        if stop.is_set():raise RuntimeError('operator_stop')
        if action=='retry_network':self.connect()
        before=self.capture()
        if before.get('native_busy'):
            raise ReobserveRequired(action,before)
        if before.get('page') in {'unknown','loading'} or action not in before.get('actions',{}):
            before=await_candidate(self.capture,stop,action,before,expected.get('page'))
        if (before.get('page')!=expected.get('page')
                or not same_candidate(before['actions'].get(action,{}),expected['actions'].get(action,{}))):
            raise ReobserveRequired(action,before)
        ok,reason=authorize(action,before,self.journal.data,self.options)
        if not ok:raise RuntimeError(reason)
        require_fresh_observation(before,action)
        if action in {'start_normal','start_special','confirm_retry','confirm_refill','confirm_recruit','claim_all'}:
            if self.day_clock()!=self.journal.data['scope']['day']:
                raise RuntimeError('strongest_team_game_day_changed_before_submit')
        candidate=before['actions'][action]
        key='swipe' if 'swipe' in candidate else 'point'
        values=candidate[key]
        if (len(values)!=(5 if key=='swipe' else 2)
                or any(type(x) not in (int,float) or not math.isfinite(x) for x in values)
                or any(not 0<=values[i]<(720 if i%2==0 else 1280) for i in range(4 if key=='swipe' else 2))
                or key=='swipe' and not 0<values[4]<=1500):
            raise RuntimeError('strongest_team_target_invalid')
        if stop.is_set():raise RuntimeError('operator_stop')
        self.journal.save()  # Fresh input-source image ownership is durable too.
        sent=time.monotonic()
        self.command('-s',self.endpoint,'shell','input','swipe' if key=='swipe' else 'tap',
                     *[str(int(x)) for x in values])
        after=wait_transition(self.capture,stop,before)
        receipt=dict(action_id=action,performed=True,source_frame_sha256=before['frame_sha256'],
                     source_screenshot_path=before.get('screenshot_path'),input_sent_monotonic=sent,observation=after)
        path=Path(self.output_dir)/(str(time.time_ns())+'-receipt.json')
        path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf8')
        return receipt
