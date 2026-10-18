"""Private COS source + the existing receipt suffix; original inspectors still apply."""
import base64
import binascii
import hashlib
from http.client import IncompleteRead
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, build_opener

from desktop import maauma_service as service
from desktop import product_updates as updates
from desktop.update_connectivity import observed_phase, transport_failure

COS_HOST='maauma-downloads-1508966578.cos.ap-guangzhou.myqcloud.com'
MAX_APPEND=1024**2
SHA=re.compile(r'[a-f0-9]{64}\Z')
IDENTITY=('component_id','product_id','version','base_version','channel')
PATCH_IDENTITY=('patch_id','from_version','target_archive_sha256','from_manifest_sha256','target_manifest_sha256')

def invalid():raise service.ServiceError('direct_delivery_invalid')

def api_path(root,value):
    if hasattr(service,'api_path'):return service.api_path(root,value)
    base=service.config(root)['base_url']
    if isinstance(value,str)and value.startswith('/v1/')and not any(c in value for c in '\r\n#'):return value
    if isinstance(value,str)and value.startswith(base+'/v1/')and not any(c in value for c in '\r\n#'):return value[len(base):]
    raise service.ServiceError('service_origin_mismatch')

def source_url(value,entry,expires):
    if not isinstance(value,str)or any(c in value for c in '\r\n'):invalid()
    try:parsed=urlsplit(value)
    except ValueError:invalid()
    if parsed.scheme!='https'or parsed.netloc!=COS_HOST or parsed.username or parsed.password or parsed.fragment:invalid()
    if not re.fullmatch(r'/private/'+re.escape(entry['version'])+r'/[A-Za-z0-9][A-Za-z0-9._-]*\.zip',parsed.path):invalid()
    fields=parse_qsl(parsed.query,keep_blank_values=True);query=dict(fields)
    required={'q-sign-algorithm','q-ak','q-sign-time','q-key-time','q-header-list','q-url-param-list','q-signature'}
    if len(fields)!=7 or set(query)!=required or query['q-sign-algorithm']!='sha1'or query['q-header-list']!='host'or query['q-url-param-list']!='':invalid()
    if not re.fullmatch(r'[-_A-Za-z0-9]{1,128}',query['q-ak'])or not re.fullmatch(r'[a-f0-9]{40}',query['q-signature']):invalid()
    match=re.fullmatch(r'(\d{1,12});(\d{1,12})',query['q-sign-time'])
    if not match or query['q-key-time']!=query['q-sign-time']or type(expires)is not int or expires!=int(match[2])or not 0<int(match[2])-int(match[1])<=900:invalid()
    if expires<time.time()-30:raise service.ServiceError('download_link_expired')
    return value

def descriptor(root,entry):
    path=api_path(root,entry.get('direct_delivery_url'))
    parsed=urlsplit(path);patch=entry.get('transfer')=='file_patch'
    wanted=('/v1/suite/direct/patches/'+str(entry.get('patch_id'))if patch else
        '/v1/suite/direct/components/pacer/'+str(entry.get('version')))
    query=dict(parse_qsl(parsed.query,keep_blank_values=True))
    if parsed.path!=wanted or query!={'base_version':entry.get('base_version'),'channel':entry.get('channel')}:invalid()
    value=service.wire(root,path,product='uma-cut')
    if not isinstance(value,dict)or value.get('ok')is not True or value.get('schema_version')!='maauma.direct_delivery.v1'or value.get('transfer')!=('file_patch'if patch else'full'):invalid()
    if entry.get('product_id')!='uma-cut'or entry.get('component_id')!='pacer'or any(value.get(key)!=entry.get(key)for key in IDENTITY):invalid()
    if patch and any(value.get(key)!=entry.get(key)for key in PATCH_IDENTITY):invalid()
    size=entry.get('size_bytes');digest=entry.get('sha256');source=value.get('source');append=value.get('append')
    if (type(size)is not int or not 22<=size<=updates.MAX_ARCHIVE or not isinstance(digest,str)or not SHA.fullmatch(digest)
            or value.get('size_bytes')!=size or value.get('sha256')!=digest or value.get('integrity_scope')!='base_archive_prefix'
            or not isinstance(source,dict)or source.get('size_bytes')!=size or source.get('sha256')!=digest or not isinstance(append,dict)):
        invalid()
    url=source_url(source.get('url'),entry,source.get('expires_at'))
    count=append.get('size_bytes');encoded=append.get('data_base64')
    if type(count)is not int or not 1<=count<=MAX_APPEND or append.get('encoding')!='base64'or not isinstance(encoded,str)or len(encoded)>4*((MAX_APPEND+2)//3):invalid()
    try:suffix=base64.b64decode(encoded,validate=True)
    except(binascii.Error,ValueError):invalid()
    if len(suffix)!=count or hashlib.sha256(suffix).hexdigest()!=append.get('sha256')or base64.b64encode(suffix).decode('ascii')!=encoded:invalid()
    receipt=service.validate_receipt(value.get('receipt'),product='uma-cut',kind='package')
    if receipt.get('base_sha256')!=digest or receipt.get('version')!=entry['version']:invalid()
    trace=service.credentials(root,'uma-cut').get('distribution_receipt',{}).get('trace_id')
    if receipt.get('trace_id')!=trace:raise service.ServiceError('distribution_receipt_mismatch')
    if type(value.get('personal_size_bytes'))is not int or value['personal_size_bytes']!=size+count or size+count>updates.MAX_ARCHIVE:invalid()
    return value,url,suffix

def open_source(url,headers):
    return build_opener(service.NoRedirect()).open(Request(url,headers=headers,method='GET'),timeout=90)

@observed_phase('package_download')
def download(root,entry,target,*,progress=None,item_id=None,source_open=None):
    from desktop.update_progress import content_length,read_chunk
    value,url,suffix=descriptor(root,entry)
    if progress is not None:progress.start_item(item_id)
    target=Path(target);partial=target.with_name(target.name+'.direct-part');size=0
    try:
        response=(source_open or open_source)(url,{'Accept':'application/octet-stream','User-Agent':'MaaUma-Suite/1'})
        with response,partial.open('wb')as output:
            if getattr(response,'status',None)!=200:raise service.ServiceError('direct_download_failed')
            expected=content_length(response)
            if expected is not None and expected!=entry['size_bytes']:raise updates.UpdateError('package_download_incomplete')
            metadata=response.headers.get('x-cos-meta-sha256')
            if metadata is not None and metadata!=entry['sha256']:raise updates.UpdateError('package_integrity_failed')
            if progress is not None:progress.connected(entry['size_bytes'])
            while True:
                block=read_chunk(response)
                if not block:break
                size+=len(block)
                if size>entry['size_bytes']:raise updates.UpdateError('package_download_incomplete')
                output.write(block)
                if progress is not None:progress.received(len(block))
            if size!=entry['size_bytes']:raise updates.UpdateError('package_download_incomplete')
            output.write(suffix)
            if output.tell()!=value['personal_size_bytes']:invalid()
        partial.replace(target)
        if progress is not None:progress.transferred()
        # The caller retains source-prefix/ZIP metadata/receipt inspection,
        # local personal SHA ticket creation and separate apply-time revalidation.
    except HTTPError as error:
        raise transport_failure(service.ServiceError('direct_download_failed'),error,phase='package_download')from None
    except IncompleteRead:
        raise updates.UpdateError('package_download_incomplete')from None
    except OSError as error:
        raise transport_failure(service.ServiceError('service_unavailable'),error,phase='package_download')from None
    finally:
        partial.unlink(missing_ok=True)
