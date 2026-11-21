"""Bounded multipart transport; explicit callers own authorization and consent."""
from __future__ import annotations
import base64
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import uuid

PART_BYTES = 256 * 1024
LEGACY_PART_BYTES = 32 * 1024
ALLOWED_PART_SIZES = (PART_BYTES, LEGACY_PART_BYTES)
MAX_ASSEMBLE_PART_BYTES = 1024 * 1024
SAFE_INTEGER = 2 ** 53 - 1
PROTOCOL = 'maauma.feedback_upload.v1'


class UploadError(RuntimeError):
    def __init__(self, code, *, detail=None):
        self.code, self.detail = code, detail or {}
        super().__init__(code)


def _descriptor(stream, part_bytes=PART_BYTES):
    digest=hashlib.sha256();size=0
    while raw:=stream.read(part_bytes):
        digest.update(raw);size+=len(raw)
        if size>SAFE_INTEGER:raise UploadError('feedback_upload_size_not_representable')
    if not size:raise UploadError('feedback_invalid')
    return {'raw_sha256':digest.hexdigest(),'size_bytes':size,
            'part_size':part_bytes,'part_count':math.ceil(size/part_bytes)}


def _write_upload_state(state_path, upload_id, raw_sha256, client_nonce, part_size):
    state_path.parent.mkdir(parents=True,exist_ok=True)
    temporary=state_path.with_name(state_path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps({'schema_version':PROTOCOL,'upload_id':upload_id,
        'raw_sha256':raw_sha256,'client_nonce':client_nonce,'part_size':part_size}),encoding='utf8')
    temporary.replace(state_path)


def _should_retry_legacy_part_size(exc):
    code = getattr(exc, 'code', None)
    if code == 'feedback_upload_invalid' or code == 400:
        return True
    return 'feedback_upload_invalid' in str(exc)


def upload_stream(open_stream, upload_id, send, *, state_path=None, client_nonce=None, progress=None):
    """A retry reopens one frozen source and resumes the same server session."""
    saved_part_size = None
    if state_path is not None:
        state_path = Path(state_path)
        if state_path.is_file():
            saved_state = json.loads(state_path.read_text(encoding='utf-8-sig'))
            raw_ps = saved_state.get('part_size')
            saved_part_size = raw_ps if raw_ps in ALLOWED_PART_SIZES else LEGACY_PART_BYTES
    active_part_bytes = saved_part_size or PART_BYTES
    with open_stream() as stream: descriptor=_descriptor(stream, active_part_bytes)
    if state_path is not None:
        if state_path.is_file():
            state=json.loads(state_path.read_text(encoding='utf-8-sig'))
            if state.get('upload_id')!=upload_id or state.get('raw_sha256')!=descriptor['raw_sha256']:
                raise UploadError('feedback_upload_source_changed')
            client_nonce=state.get('client_nonce')
        else:
            client_nonce=uuid.uuid4().hex+uuid.uuid4().hex
            _write_upload_state(state_path, upload_id, descriptor['raw_sha256'], client_nonce, active_part_bytes)
    if not isinstance(client_nonce,str) or not re.fullmatch('[a-f0-9]{64}',client_nonce):
        raise UploadError('feedback_upload_state_required')
    common={'schema_version':PROTOCOL,'upload_id':upload_id,**descriptor}
    if progress:progress(0,descriptor['part_count'])
    try:
        begin=send({**common,'operation':'upload_begin','client_nonce':client_nonce})
    except Exception as exc:
        if active_part_bytes != LEGACY_PART_BYTES and saved_part_size is None and _should_retry_legacy_part_size(exc):
            active_part_bytes = LEGACY_PART_BYTES
            with open_stream() as stream:
                descriptor = _descriptor(stream, LEGACY_PART_BYTES)
            common = {'schema_version': PROTOCOL, 'upload_id': upload_id, **descriptor}
            if state_path is not None:
                _write_upload_state(state_path, upload_id, descriptor['raw_sha256'], client_nonce, LEGACY_PART_BYTES)
            if progress:progress(0,descriptor['part_count'])
            begin=send({**common,'operation':'upload_begin','client_nonce':client_nonce})
        else:
            raise
    if not isinstance(begin,dict) or begin.get('ok') is not True:
        raise UploadError('feedback_upload_protocol_invalid')
    if begin.get('feedback_id'):return begin
    token=begin.get('upload_token');start=begin.get('next_index')
    if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{64}',token):
        raise UploadError('feedback_upload_protocol_invalid')
    if type(start) is not int or not 0<=start<=descriptor['part_count']:
        raise UploadError('feedback_upload_protocol_invalid')
    with open_stream() as stream:
        for index in range(descriptor['part_count']):
            raw=stream.read(active_part_bytes)
            expected=min(active_part_bytes,descriptor['size_bytes']-index*active_part_bytes)
            if len(raw)!=expected:raise UploadError('feedback_upload_source_changed')
            if index<start:continue
            packed=gzip.compress(raw,compresslevel=6,mtime=0)
            answer=send({**common,'operation':'upload_part','upload_token':token,
                'part_index':index,'raw_part_sha256':hashlib.sha256(raw).hexdigest(),
                'packed_sha256':hashlib.sha256(packed).hexdigest(),
                'payload_gzip_base64':base64.b64encode(packed).decode('ascii')})
            if not isinstance(answer,dict) or answer.get('ok') is not True or answer.get('next_index')!=index+1:
                raise UploadError('feedback_upload_protocol_invalid')
            if progress:progress(index+1,descriptor['part_count'])
        if stream.read(1):raise UploadError('feedback_upload_source_changed')
    return send({**common,'operation':'upload_complete','upload_token':token})


def upload_gzip(path, upload_id, send, *, state_path=None, client_nonce=None, progress=None):
    path=Path(path)
    return upload_stream(lambda:gzip.open(path,'rb'),upload_id,send,state_path=state_path,client_nonce=client_nonce,progress=progress)


def upload_bytes(raw, upload_id, send, *, state_path=None, client_nonce=None, progress=None):
    return upload_stream(lambda:io.BytesIO(raw),upload_id,send,state_path=state_path,client_nonce=client_nonce,progress=progress)


def assemble_admin_manifest(manifest, fetch_part, output, *, attachment=False):
    """Actual maintainer consumer: write atomically and verify the original SHA."""
    if not isinstance(manifest,dict) or manifest.get('schema_version')!='maauma.feedback_download.v1':
        raise UploadError('feedback_download_invalid')
    count=manifest.get('part_count');size=manifest.get('size_bytes');sha=manifest.get('sha256')
    if type(count) is not int or count<1 or type(size) is not int or size<0 or not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):
        raise UploadError('feedback_download_invalid')
    raw_ps = manifest.get('part_size')
    part_limit = min(MAX_ASSEMBLE_PART_BYTES, max(PART_BYTES, raw_ps if type(raw_ps) is int and raw_ps > 0 else PART_BYTES))
    output=Path(output);temporary=output.with_name(output.name+'.multipart-tmp')
    output.parent.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256();seen=0
    try:
        with temporary.open('wb') as stream:
            for index in range(count):
                value=fetch_part(index)
                if not isinstance(value,dict) or value.get('schema_version')!='maauma.feedback_download_part.v1' or value.get('part_index')!=index:
                    raise UploadError('feedback_download_invalid')
                packed=base64.b64decode(value.get('payload_gzip_base64',''),validate=True)
                if hashlib.sha256(packed).hexdigest()!=value.get('packed_sha256'):
                    raise UploadError('feedback_download_changed')
                with gzip.GzipFile(fileobj=io.BytesIO(packed)) as decoded:
                    raw=decoded.read(part_limit+1)
                if len(raw)>part_limit or len(raw)!=value.get('size_bytes') or hashlib.sha256(raw).hexdigest()!=value.get('sha256'):
                    raise UploadError('feedback_download_changed')
                stream.write(raw);digest.update(raw);seen+=len(raw)
        if seen!=size or digest.hexdigest()!=sha:raise UploadError('feedback_download_changed')
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {'ok':True,'path':str(output),'size_bytes':seen,'sha256':digest.hexdigest()}
