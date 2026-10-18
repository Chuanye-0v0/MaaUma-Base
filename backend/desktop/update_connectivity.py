"""Safe update-network diagnostics. No retry, routing or authorization policy."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import socket
import ssl
from urllib.error import HTTPError, URLError

SCHEMA='maauma.update_connectivity.v1'
PHASES={'public_catalog','authorized_catalog','patch_catalog','license_check','activation',
        'package_download','public_cos_download','service_request'}
CAUSES={'timeout','dns','tls_certificate','tls_connection','connection','invalid_response',
        'authorization_rejected','http_error','transport_error'}
STATUSES={'requesting','received','failed'}
_reporter=ContextVar('maauma_update_reporter',default=None)
_phase=ContextVar('maauma_update_network_phase',default=None)

@contextmanager
def bind_reporter(reporter):
    token=_reporter.set(reporter)
    try:yield
    finally:_reporter.reset(token)

def phase_for(path,binary=False):
    if binary:return 'package_download'
    if path.startswith('/v1/public/catalog?'):return 'public_catalog'
    if path.startswith('/v1/suite/catalog?'):return 'authorized_catalog'
    if '/patches?' in path:return 'patch_catalog'
    if path.startswith('/v1/license/check'):return 'license_check'
    if path.startswith('/v1/activate'):return 'activation'
    return 'service_request'

def classify(error):
    if isinstance(error,HTTPError):
        return 'authorization_rejected' if error.code in (401,403) else 'http_error'
    reason=error.reason if isinstance(error,URLError) else error
    if isinstance(reason,(TimeoutError,socket.timeout)):return 'timeout'
    if isinstance(reason,socket.gaierror):return 'dns'
    if isinstance(reason,ssl.SSLCertVerificationError):return 'tls_certificate'
    if isinstance(reason,ssl.SSLError):return 'tls_connection'
    if isinstance(reason,ConnectionError):return 'connection'
    if isinstance(reason,ValueError):return 'invalid_response'
    return 'transport_error'

def transport_failure(error,cause,*,phase=None):
    """Attach only enums/status; preserve the original authoritative error code."""
    selected=phase or _phase.get()
    if selected not in PHASES:return error
    error.network_diagnostics={'schema_version':SCHEMA,'phase':selected,'cause':classify(cause)}
    if isinstance(cause,HTTPError) and type(cause.code) is int:
        error.network_diagnostics['http_status']=cause.code
    return error

def failure_details(error):
    value=getattr(error,'network_diagnostics',None)
    if not isinstance(value,dict) or value.get('phase') not in PHASES or value.get('cause') not in CAUSES:return {}
    safe={'schema_version':SCHEMA,'phase':value['phase'],'cause':value['cause']}
    if type(value.get('http_status')) is int and 100<=value['http_status']<=599:
        safe['http_status']=value['http_status']
    return {'network':safe}

def _send(phase,status,error=None):
    reporter=_reporter.get()
    if reporter is None:return
    try:
        method=getattr(reporter,'network',None)
        details=failure_details(error).get('network',{})
        if method is not None:method(phase,status,**{key:details[key] for key in ('cause','http_status') if key in details})
    except Exception:
        # Reporting must not alter a transaction or a running task.
        pass

def observed_phase(phase):
    def decorate(function):
        @wraps(function)
        def call(*args,**kwargs):
            token=_phase.set(phase);_send(phase,'requesting')
            try:
                result=function(*args,**kwargs)
            except Exception as error:
                if str(error) in {'service_mismatch','service_redirect_rejected'} and not failure_details(error):
                    transport_failure(error,ValueError(),phase=phase)
                _send(phase,'failed',error);raise
            else:
                _send(phase,'received');return result
            finally:_phase.reset(token)
        return call
    return decorate

def observed_request(function):
    @wraps(function)
    def call(root,path,*args,**kwargs):
        phase=phase_for(path,kwargs.get('binary') is True)
        return observed_phase(phase)(function)(root,path,*args,**kwargs)
    return call

def diagnose(root):
    """Two anonymous public-control reads, and allowlisted COS HEAD if declared.

    This does not renew a license, change proxy/configuration, prepare a ticket,
    or select a download transport for subsequent update operations.
    """
    from concurrent.futures import ThreadPoolExecutor
    import json
    from urllib.parse import urlencode
    from urllib.request import Request,ProxyHandler,build_opener,getproxies
    from desktop import maauma_service as service,maauma_components as components
    cfg=service.config(root)
    path='/v1/public/catalog?'+urlencode({'channel':cfg['channel']})
    def probe(route):
        opener=build_opener(service.NoRedirect(),*([ProxyHandler({})] if route=='direct' else []))
        rows=[]
        try:
            request=Request(cfg['base_url']+path,headers={'Accept':'application/json','User-Agent':'MaaUma-Suite/1'},method='GET')
            with opener.open(request,timeout=8) as response:raw=response.read(2*1024**2+1)
            if len(raw)>2*1024**2:raise ValueError('invalid_response')
            value=json.loads(raw.decode('utf-8-sig'))
            if not isinstance(value,dict) or value.get('ok') is not True or value.get('schema_version')!='maauma.public_catalog.v1':
                raise ValueError('invalid_response')
            rows.append({'phase':'public_catalog','route':route,'ok':True})
        except (OSError,ValueError) as error:
            row={'phase':'public_catalog','route':route,'ok':False,'cause':classify(error)}
            if isinstance(error,HTTPError):row['http_status']=error.code
            return [row]
        entry=components._base_entry(value)
        url=components._public_download_url(entry,'umaa-public') if entry else None
        if url is None:return rows
        try:
            request=Request(url,headers={'Accept':'application/octet-stream','User-Agent':'MaaUma-Suite/1'},method='HEAD')
            with opener.open(request,timeout=8) as response:
                code=response.status
                if code!=200:raise ValueError('unexpected_http_status')
            rows.append({'phase':'public_cos_download','route':route,'ok':True})
        except (OSError,ValueError) as error:
            row={'phase':'public_cos_download','route':route,'ok':False,'cause':classify(error)}
            if isinstance(error,HTTPError):row['http_status']=error.code
            rows.append(row)
        return rows
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows=[row for group in executor.map(probe,('system','direct')) for row in group]
    return {'ok':True,'status':'connectivity_checked','schema_version':SCHEMA,
        'system_proxy_configured':any(getproxies().get(key) for key in ('http','https')),
        'checks':rows,'settings_modified':False,'license_checked':False,'downloaded_payload_bytes':0}
