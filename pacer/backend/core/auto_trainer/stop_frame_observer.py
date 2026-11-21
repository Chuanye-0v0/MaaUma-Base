"""Observe existing capture returns in one career segment; never request a frame."""
from pathlib import Path
import time


class StopFrameObserver:
    """One full frame, exact selected device and invocation, copied before reuse."""
    def __init__(self, ctx, *, clock=time.time):
        self.ctx = ctx
        self.run_id = ctx.run_id
        self.segment = Path(ctx.segment_root)
        self.address = getattr(ctx, 'progress', {}).get('device')
        self.clock = clock
        self.sequence = 0
        self.frame = None
        self.token = None

    def bind(self):
        from core.adb import bind_capture_listener
        self.token = bind_capture_listener(self.observe)
        return self

    def observe(self, device, method, image):
        import numpy as np
        if (not self.address or getattr(device, 'address', None) != self.address
                or self.ctx.run_id != self.run_id or Path(self.ctx.segment_root) != self.segment):
            return
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                or image.shape[2] != 3):
            return
        h, w = image.shape[:2]
        if w < 360 or h < 640 or w*h > 16_000_000 or not 1.6 <= h/w <= 2.3:
            return
        copied = image.copy()
        self.sequence += 1
        self.frame = {'frame': copied, 'run_session_id': self.run_id,
            'segment_id': self.segment.name,
            'captured_at_unix': self.clock(), 'capture_time_source': 'capture_return_time',
            'observation': {'schema_version': 'uma.stop_frame_observation.v1',
                'capture_method': method, 'sequence': self.sequence,
                'owner_turn_index': getattr(self.ctx, 'progress', {}).get('next_turn')}}

    def freeze(self):
        """The stop boundary fixes pixels and context before cleanup or a resume."""
        if self.frame is None:
            return None
        value = dict(self.frame)
        value['observation'] = dict(value['observation'],
            stop_owner_turn_index=getattr(self.ctx, 'progress', {}).get('next_turn'))
        return value

    def close(self):
        if self.token is not None:
            from core.adb import reset_capture_listener
            reset_capture_listener(self.token)
            self.token = None

