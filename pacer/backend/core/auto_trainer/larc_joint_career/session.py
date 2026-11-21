"""Persist only original-owner-confirmed main intents, never money ledgers."""
from copy import deepcopy
import json
from pathlib import Path
from .binding import settled_context,SCHEMA

FILE='larc_career_joint_main.json'

def remember_verified(ctx,recommendation,receipt,receipt_path,*,verified):
    packet=settled_context(recommendation,run_id=ctx.run_id,turn=ctx.progress['next_turn'],
        receipt=receipt,receipt_path=receipt_path,verified=verified)
    if packet is None:
        if verified is True:
            previous=current_marker(ctx,ctx.progress['next_turn'])
            if previous is not None:
                # A later verified original-owner purchase supersedes this
                # main intent. This updates only our non-consuming metadata;
                # the real purchase ledger and receipts are never changed.
                from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
                previous.update(status='superseded_by_verified_purchase',
                    superseded_receipt_path=str(receipt_path))
                _atomic_write_json(Path(ctx.root)/'session'/FILE,previous)
                ctx.career_joint_main=deepcopy(previous)
        return False
    from scripts.ug_parent_one_turn_pipeline import _atomic_write_json
    _atomic_write_json(Path(ctx.root)/'session'/FILE,packet)
    ctx.career_joint_main=deepcopy(packet)
    return True

def current_marker(ctx,turn):
    packet=getattr(ctx,'career_joint_main',None)
    if not isinstance(packet,dict):
        try:packet=json.loads((Path(ctx.root)/'session'/FILE).read_text('utf8'))
        except(OSError,ValueError,TypeError):return None
    source=packet.get('source')or{}
    if (packet.get('schema_version')!=SCHEMA or packet.get('status')!='verified_purchase_same_turn'
        or source.get('run_session_id')!=ctx.run_id or source.get('turn_index')!=turn):return None
    return deepcopy(packet)
