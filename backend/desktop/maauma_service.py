"""Small device-bound suite client. No Pacer strategy or game code is imported."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from desktop.product_updates import contained, read_json, write_json, installation, UpdateError

CONFIG='config/private-service.json'
AUTH='config/private-service-auth.json'
RECEIPT='config/distribution-receipt.json'
PRODUCTS={'umaa-public','uma-cut'}


class ServiceError(RuntimeError):
    pass


def service_base(value):
    value=str(value).strip().rstrip('/')
    parsed=urlsplit(value)
    local=parsed.scheme=='http' and parsed.hostname in {'127.0.0.1','localhost','::1'}
    if (not(local or parsed.scheme=='https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ServiceError('invalid_service_address')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ServiceError('service_redirect_rejected')


def identity_helper(root,action,**fields):
    executable=Path(root)/'MaaUmaIdentity.exe'
    if os.name!='nt' or not executable.is_file():raise ServiceError('device_identity_unavailable')
    try:
        environment=dict(os.environ,DOTNET_ROOT=str(Path(root)/'dotnet'),DOTNET_MULTILEVEL_LOOKUP='0')
        completed=subprocess.run([str(executable),'--device-identity'],cwd=root,
            input=json.dumps({'action':action,**fields},ensure_ascii=False),encoding='utf8',
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=8,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env=environment)
        if completed.returncode or len(completed.stdout)>4*1024**2:raise ValueError()
        value=json.loads(completed.stdout)
    except (OSError,ValueError,subprocess.TimeoutExpired):raise ServiceError('device_identity_unavailable') from None
    if not isinstance(value,dict) or value.get('ok') is not True:
        code=value.get('code') if isinstance(value,dict) else None
        raise ServiceError(code if code in {'device_changed','device_key_unreadable','device_credentials_unreadable'}
                           else 'device_identity_unavailable')
    return value


def identity(root):
    value=identity_helper(root,'identity')
    if not re.fullmatch('[a-f0-9]{64}',str(value.get('device_id',''))) or not isinstance(value.get('public_key'),str):
        raise ServiceError('device_identity_unavailable')
    return {key:value[key] for key in ('device_id','public_key')}


def signed_headers(root,method,path,data=b'',token=''):
    if method not in {'GET','POST'} or not path.startswith('/v1/') or any(c in path for c in '\r\n'):
        raise ServiceError('invalid_service_path')
    timestamp=str(int(time.time()));nonce=uuid.uuid4().hex
    canonical='\n'.join(('MaaUma-Device-v1',timestamp,nonce,method,path,
        hashlib.sha256(data).hexdigest(),hashlib.sha256(token.encode('utf8')).hexdigest()))
    value=identity_helper(root,'sign',message_base64=base64.b64encode(canonical.encode('utf8')).decode('ascii'))
    signature=value.get('signature')
    try:
        if len(base64.b64decode(signature,validate=True))!=64:raise ValueError()
    except (TypeError,ValueError):raise ServiceError('device_identity_unavailable') from None
    return {'X-MaaUma-Time':timestamp,'X-MaaUma-Nonce':nonce,'X-MaaUma-Signature':signature}


def seal(root,value):
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf8')
    response=identity_helper(root,'seal',payload_base64=base64.b64encode(raw).decode('ascii'))
    return {'schema_version':'uma.private_service_auth.v2','protection':'windows-user-dpapi-v1','encrypted':response['encrypted']}


def unseal(root,value):
    if value.get('schema_version')!='uma.private_service_auth.v2' or value.get('protection')!='windows-user-dpapi-v1':
        raise ServiceError('device_credentials_unreadable')
    response=identity_helper(root,'unseal',encrypted=value.get('encrypted'))
    try:
        value=json.loads(base64.b64decode(response['payload_base64'],validate=True).decode('utf8'))
        if not isinstance(value,dict):raise ValueError()
    except (KeyError,TypeError,ValueError):raise ServiceError('device_credentials_unreadable') from None
    return value


def config(root):
    path=contained(root,CONFIG)
    value=read_json(path) if path.is_file() else {}
    if not value.get('base_url'):raise ServiceError('service_not_configured')
    value['base_url']=service_base(value['base_url'])
    if value.get('channel') not in {'preview','stable'}:raise ServiceError('invalid_channel')
    return value


def configure(root,base_url,channel='preview'):
    base=service_base(base_url)
    if channel not in {'stable','preview'}:raise ServiceError('invalid_channel')
    path=contained(root,CONFIG);previous=read_json(path) if path.exists() else {}
    write_json(path,{'schema_version':'uma.private_service_config.v1','base_url':base,'channel':channel})
    if previous.get('base_url')!=base:
        for name in (AUTH,RECEIPT):contained(root,name).unlink(missing_ok=True)
    return {'ok':True,'status':'configured','base_url':base,'channel':channel}


def credentials(root,product='umaa-public'):
    if product not in PRODUCTS:raise ServiceError('wrong_product')
    cfg=config(root);path=contained(root,AUTH)
    if not path.is_file():raise ServiceError('activation_required')
    value=unseal(root,read_json(path));current=identity(root)
    if value.get('base_url')!=cfg['base_url'] or product not in value.get('products',[]):raise ServiceError('activation_required')
    if value.get('device_id')!=current['device_id']:raise ServiceError('device_changed')
    if value.get('public_key')!=current['public_key']:raise ServiceError('device_key_mismatch')
    if not re.fullmatch(r'md_[A-Za-z0-9._~-]{32,252}',str(value.get('access_token',''))):
        raise ServiceError('device_credentials_unreadable')
    return value


def wire(root,path,body=None,*,authenticated=True,product='umaa-public',timeout=45,binary=False):
    cfg=config(root)
    token=credentials(root,product)['access_token'] if authenticated else ''
    data=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode('utf8') if body is not None else b''
    method='POST' if body is not None else 'GET'
    headers={'Accept':'application/json','User-Agent':'MaaUma-Suite/1',**signed_headers(root,method,path,data,token)}
    if token:headers['Authorization']='Bearer '+token
    if body is not None:headers['Content-Type']='application/json'
    request=Request(cfg['base_url']+path,data=data if body is not None else None,headers=headers,method=method)
    try:
        response=build_opener(NoRedirect()).open(request,timeout=timeout)
        if binary:return response
        with response:raw=response.read(2*1024**2+1)
        if len(raw)>2*1024**2:raise ServiceError('service_mismatch')
        value=json.loads(raw.decode('utf-8-sig'))
    except HTTPError as error:
        try:code=json.loads(error.read(8192)).get('code')
        except (ValueError,OSError):code=None
        raise ServiceError(code if isinstance(code,str) and re.fullmatch('[a-z0-9_]{1,80}',code) else 'service_unavailable') from None
    except (OSError,ValueError):raise ServiceError('service_unavailable') from None
    if not isinstance(value,dict) or value.get('ok') is not True:raise ServiceError('service_mismatch')
    return value


def public_wire(root,path,*,timeout=45,binary=False):
    """Read the explicitly public base feed without a device or a grant."""
    if not re.fullmatch(r'/v1/public/(?:catalog\?channel=(?:stable|preview)|downloads/[1-9][0-9]{0,19})',str(path)):
        raise ServiceError('invalid_public_service_path')
    cfg=config(root)
    request=Request(cfg['base_url']+path,headers={'Accept':'application/octet-stream' if binary else 'application/json',
                    'User-Agent':'MaaUma-Suite/1'},method='GET')
    try:
        response=build_opener(NoRedirect()).open(request,timeout=timeout)
        if binary:return response
        with response:raw=response.read(2*1024**2+1)
        if len(raw)>2*1024**2:raise ServiceError('service_mismatch')
        value=json.loads(raw.decode('utf-8-sig'))
    except HTTPError as error:
        try:code=json.loads(error.read(8192)).get('code')
        except (ValueError,OSError):code=None
        raise ServiceError(code if isinstance(code,str) and re.fullmatch('[a-z0-9_]{1,80}',code) else 'service_unavailable') from None
    except (OSError,ValueError):raise ServiceError('service_unavailable') from None
    if not isinstance(value,dict) or value.get('ok') is not True:raise ServiceError('service_mismatch')
    return value


def public_catalog(root):
    cfg=config(root)
    result=public_wire(root,'/v1/public/catalog?'+urlencode({'channel':cfg['channel']}))
    if (result.get('schema_version')!='maauma.public_catalog.v1' or 'base' not in result or result.get('components')!=[]
            or result.get('base') is not None and (not isinstance(result['base'],dict)
                or result['base'].get('product_id')!='umaa-public')):
        raise ServiceError('service_mismatch')
    return result


def validate_receipt(value,*,product=None,kind=None):
    if (not isinstance(value,dict) or value.get('schema_version')!='uma.distribution_receipt.v1'
            or value.get('product_id') not in PRODUCTS or product and value.get('product_id')!=product
            or value.get('kind') not in {'activation','package'} or kind and value.get('kind')!=kind
            or not re.fullmatch(r'MU-(?:[0-9A-F]{4}-){3}[0-9A-F]{4}',str(value.get('trace_id','')))
            or not re.fullmatch('[0-9a-f]{64}',str(value.get('signature','')))):
        raise ServiceError('distribution_receipt_invalid')
    try:uuid.UUID(value['receipt_id'])
    except (KeyError,ValueError,TypeError):raise ServiceError('distribution_receipt_invalid') from None
    if kind=='package' and (not value.get('version') or not re.fullmatch('[0-9a-f]{64}',str(value.get('base_sha256','')))):
        raise ServiceError('distribution_receipt_invalid')
    return value


def save_activation(root,response,current,product='umaa-public'):
    if (product not in response.get('products',[]) or not response.get('binding_id')
            or response.get('device_id')!=current['device_id']
            or not re.fullmatch(r'md_[A-Za-z0-9._~-]{32,252}',str(response.get('access_token','')))):
        raise ServiceError('service_mismatch')
    receipt=validate_receipt(response.get('distribution_receipt'),product=product,kind='activation')
    value={key:response[key] for key in ('access_token','client_id','products','allow_preview','binding_id','device_id')}
    value.update(schema_version='uma.private_service_auth_payload.v2',base_url=config(root)['base_url'],
                 public_key=current['public_key'],distribution_receipt=receipt)
    write_json(contained(root,AUTH),seal(root,value));write_json(contained(root,RECEIPT),receipt)
    return value


def activate(root,invite_code,*,automatic=True):
    if not isinstance(invite_code,str) or not 12<=len(invite_code.strip())<=160:raise ServiceError('invite_invalid')
    info=wire(root,'/v1/info',authenticated=False)
    if info.get('schema_version')!='uma.private_service.v1' or info.get('device_binding')!='one-computer-v1':
        raise ServiceError('service_mismatch')
    current=identity(root)
    response=wire(root,'/v1/activate',{'invite_code':invite_code.strip(),'product_id':'umaa-public',**current},authenticated=False)
    value=save_activation(root,response,current)
    cfg=config(root)
    if value.get('allow_preview') is not True and cfg['channel']=='preview':
        # Keep the successful same-origin activation. The next catalog request
        # must use the permitted channel, rather than only changing a UI label.
        cfg['channel']='stable';write_json(contained(root,CONFIG),cfg)
    answer={'ok':True,'status':'activated','products':value['products'],
            'allow_preview':value.get('allow_preview') is True,'channel':cfg['channel'],
            'trace_id':value['distribution_receipt']['trace_id']}
    if automatic and 'uma-cut' in value['products']:
        from desktop.maauma_components import prepare
        try:answer['component']=prepare(root,mode='pacer')
        except (ServiceError,UpdateError,OSError,ValueError) as error:
            answer['component']={'ok':False,'status':'download_failed','code':str(error)
                                 if isinstance(error,(ServiceError,UpdateError)) else 'component_prepare_failed'}
    return answer


def license_check(root,product='umaa-public'):
    auth=credentials(root,product)
    response=wire(root,'/v1/license/check?'+urlencode({'product_id':product}),product=product)
    if any(response.get(key)!=auth.get(key) for key in ('client_id','binding_id','device_id')) or response.get('product_id')!=product:
        raise ServiceError('service_mismatch')
    duration=response.get('valid_for_seconds')
    if type(duration) is not int or duration<=0:raise ServiceError('service_mismatch')
    return {'ok':True,'status':'license_valid','product_id':product,'valid_for_seconds':min(300,duration)}


def catalog(root,*,base_version=None):
    if base_version is None:base_version=installation(root)[2]['version']
    cfg=config(root)
    result=wire(root,'/v1/suite/catalog?'+urlencode({'base_version':base_version,'channel':cfg['channel']}))
    if not isinstance(result.get('components'),list):raise ServiceError('service_mismatch')
    return result


def verify_receipt(root,receipt):
    receipt=validate_receipt(receipt,kind='package')
    result=wire(root,'/v1/suite/receipts/verify',{'receipt':receipt},product=receipt['product_id'])
    if result.get('verified') is not True or any(result.get(k)!=receipt.get(k) for k in ('trace_id','product_id','version','base_sha256')):
        raise ServiceError('distribution_receipt_unverified')
    return True


def info(root):
    try:cfg=config(root)
    except ServiceError:return {'ok':True,'status':'service_info','configured':False,'activated':False,
                               'products':[],'allow_preview':False}
    try:auth=credentials(root);active=True
    except (ServiceError,UpdateError,OSError,ValueError):auth={};active=False
    state=contained(root,'config/maauma-modules.json')
    return {'ok':True,'status':'service_info','configured':True,'activated':active,
            'base_url':cfg['base_url'],'channel':cfg['channel'],'products':auth.get('products',[]),
            'allow_preview':active and auth.get('allow_preview') is True,
            'trace_id':auth.get('distribution_receipt',{}).get('trace_id'),
            'modules':read_json(state) if state.is_file() else {'components':{}},'authorization_root':str(Path(root).resolve())}


def dispatch(root,value):
    root=Path(root).resolve(strict=True)
    if not isinstance(value,dict):raise ServiceError('invalid_request')
    action=value.get('action')
    if action=='info':return info(root)
    if action=='configure':return configure(root,value.get('base_url'),value.get('channel','preview'))
    if action=='activate':return activate(root,value.get('invite_code'))
    if action=='catalog':return catalog(root)
    if action=='license':return license_check(root,value.get('product_id','umaa-public'))
    from desktop import maauma_components as components
    if action in {'prepare_pacer','prepare_update'}:return components.prepare(root,mode='pacer' if action=='prepare_pacer' else 'update')
    if action=='install':return components.launch_install(root,value.get('ticket'),parent_pid=value.get('parent_pid'))
    if action=='apply':return components.apply_prepared(root,value.get('ticket'),parent_pid=value.get('parent_pid'))
    if action=='rollback':return components.rollback(root,value.get('ticket'),parent_pid=value.get('parent_pid'))
    raise ServiceError('invalid_action')
