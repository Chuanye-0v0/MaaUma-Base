"""Choose a measured ADB frame encoding for one selected portable endpoint.

No input, disk frames, configuration writes, or native SDK assumptions.
The first bounded comparison is reused across route-local ADB connections.
"""
from copy import deepcopy
from statistics import median
import time

_choices = {}
_protocols = {}


def reset_adb_frame_probe(key):
    _choices.pop(key, None)
    _protocols.pop(key, None)


def raw_protocol(key, sdk_reader):
    if key not in _protocols:
        version=sdk_reader()
        version=int(version.strip()) if isinstance(version,str) and version.strip().isdigit() else None
        header=(16 if version is not None and version>=27 else
                12 if version is not None and 1<=version<=26 else None)
        _protocols[key]={'android_sdk':version,'header_bytes':header}
    return dict(_protocols[key])


def read_adb_frame(key, read_raw, read_png, *, deadline=None, clock=time.monotonic, paused_seconds=lambda:0.):
    probe_deadline=None
    def read(mode):
        nonlocal deadline,probe_deadline
        started=clock()
        limits=[v for v in (deadline,probe_deadline) if v is not None]
        limit=min(limits) if limits else None
        if limit is not None and started>=limit:return None,0.
        before_pause=paused_seconds()
        image=(read_raw if mode=='adb-raw' else read_png)(deadline=limit)
        paused=max(0.,paused_seconds()-before_pause)
        if deadline is not None:deadline+=paused
        if probe_deadline is not None:probe_deadline+=paused
        if limit is not None:limit+=paused
        elapsed=max(0.,clock()-started-paused)
        if limit is not None and clock()>=limit:return None,elapsed
        return image,elapsed

    choice=_choices.get(key)
    if choice is not None:
        image,_=read(choice['selected'])
        if image is not None:return image,choice['selected'],deepcopy(choice)
        if deadline is not None and clock()>=deadline:return None,choice['selected'],deepcopy(choice)
        if choice['selected']=='adb-raw':
            choice={**choice,'selected':'adb-png','basis':'raw_read_failed_returned_to_png'}
            _choices[key]=choice
            image,_=read('adb-png')
        return image,'adb-png',deepcopy(choice)

    # Short page-settling reads must not acquire a benchmark delay. The next
    # ordinary capture/check with enough time can perform the comparison.
    if deadline is not None and deadline-clock()<4.:
        image,_=read('adb-png')
        return image,'adb-png',{'selected':'adb-png','basis':'comparison_deferred_short_deadline'}

    measurements={'adb-raw':[],'adb-png':[]}
    probe_deadline=clock()+2.5
    images={}
    last_mode=last_image=None
    raw_supported=True
    for _ in range(2):
        for mode in ('adb-raw','adb-png'):
            if mode=='adb-raw' and not raw_supported:continue
            image,elapsed=read(mode)
            last_mode,last_image=mode,image
            if image is not None:
                images[mode]=image
                measurements[mode].append(round(elapsed*1000,3))
            elif mode=='adb-raw':
                raw_supported=False
            if clock()>=probe_deadline:break
        if not raw_supported or clock()>=probe_deadline or deadline is not None and deadline-clock()<.5:break
    exhausted=clock()>=probe_deadline
    probe_deadline=None
    if exhausted:
        choice={'selected':'adb-png','basis':'comparison_budget_exceeded','sample_ms':measurements}
        _choices[key]=choice
        image,_=read('adb-png')
        return image,'adb-png',deepcopy(choice)
    same_size=('adb-raw' in images and 'adb-png' in images
               and images['adb-raw'].shape==images['adb-png'].shape)
    raw_complete=raw_supported and len(measurements['adb-raw'])==2
    faster=(same_size and raw_complete and len(measurements['adb-png'])==2
            and median(measurements['adb-raw'])<.85*median(measurements['adb-png']))
    selected='adb-raw' if faster else 'adb-png'
    basis=('raw_measured_at_least_15_percent_faster' if faster else
           'raw_unavailable' if not raw_supported else 'png_retained_after_comparison')
    if raw_complete and 'adb-png' not in images:
        selected,basis='adb-raw','png_unavailable_raw_valid'
    choice={'selected':selected,'basis':basis,'sample_ms':measurements,'same_size':same_size,
            'minimum_improvement_ratio':.15}
    if images and (deadline is None or clock()<deadline):_choices[key]=choice
    # PNG is the last sampled frame. If raw won, read a fresh raw frame rather
    # than returning a sample that predates the final PNG observation.
    image=last_image if last_mode=='adb-png' else None
    if selected=='adb-raw':
        image,_=read('adb-raw')
        if deadline is not None and clock()>=deadline:return None,selected,deepcopy(choice)
        if image is None:
            choice={**choice,'selected':'adb-png','basis':'raw_read_failed_returned_to_png'}
            _choices[key]=choice;selected='adb-png'
            image,_=read('adb-png')
    if deadline is not None and clock()>=deadline:image=None
    return image,selected,deepcopy(choice)
