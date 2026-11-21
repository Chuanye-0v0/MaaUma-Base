"""Per-call timings only; no device reads, global patches, or decision changes."""
from contextvars import ContextVar
from functools import wraps
from time import perf_counter,process_time

_sink=ContextVar('uma_pipeline_timing',default=None)


def timed_call(name,func,*args,**kwargs):
    sink=_sink.get()
    if sink is None:return func(*args,**kwargs)
    started=perf_counter();cpu_started=process_time()
    try:return func(*args,**kwargs)
    finally:
        row=sink.setdefault(name,{'calls':0,'seconds':0.,'process_cpu_seconds':0.})
        row['calls']+=1;row['seconds']+=perf_counter()-started
        row['process_cpu_seconds']+=process_time()-cpu_started


def timed_stage(name):
    def decorate(func):
        @wraps(func)
        def wrapped(*args,**kwargs):return timed_call(name,func,*args,**kwargs)
        return wrapped
    return decorate


def profile_pipeline(func):
    @wraps(func)
    def wrapped(*args,**kwargs):
        timings={};token=_sink.set(timings);started=perf_counter();cpu_started=process_time()
        try:
            report,code=func(*args,**kwargs)
            report['pipeline_timing']={'total_seconds':round(perf_counter()-started,4),
                'total_process_cpu_seconds':round(process_time()-cpu_started,4),
                'CPU_scope':'current_process_including_its_threads_excluding_child_processes',
                'stage_seconds_are_inclusive':True,
                'stages':{name:{'calls':row['calls'],'seconds':round(row['seconds'],4),
                    'process_cpu_seconds':round(row['process_cpu_seconds'],4)}
                    for name,row in timings.items()}}
            return report,code
        finally:_sink.reset(token)
    return wrapped
