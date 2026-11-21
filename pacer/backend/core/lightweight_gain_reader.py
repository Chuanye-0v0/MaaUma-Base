"""Self-owned, fixed six-column gain reader; frozen V4 matching kernels.

No Paddle or external assets. Runtime templates ship with this module.
"""
from pathlib import Path
from copy import deepcopy
import json
import math
import os

import cv2
import numpy as np

FIELDS = ("speed", "stamina", "power", "guts", "wisdom", "skill_pt")


def white_outline_score(image, box, component):
    """Separate outlined game lettering from similarly sized capsule edges."""
    x,y,w,h=box
    padded=np.pad(component,3)
    contours,_=cv2.findContours(padded,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    filled=np.zeros_like(padded); cv2.drawContours(filled,contours,-1,1,-1)
    ring=cv2.dilate(filled,np.ones((5,5),np.uint8))-filled
    pixels=image[max(0,y-3):y+h+3,max(0,x-3):x+w+3].astype(np.int16)
    if pixels.shape[:2]!=ring.shape: return 0
    white=(pixels.min(axis=2)>180)&(pixels.max(axis=2)-pixels.min(axis=2)<55)
    score=float(white[ring>0].mean())
    if score<.65:
        # A one-pixel white outline can be diluted by the blue capsule in the
        # wider ring. Only an almost complete inner white outline recovers it.
        inner=cv2.dilate(filled,np.ones((3,3),np.uint8))-filled
        inner_score=float(white[inner>0].mean())
        if inner_score>=.90:
            return inner_score
    return score


def segment_gain_row(image, row_index, field_index, *, style='standard', outline_filter=True):
    """Pair a cross-shaped plus with baseline-aligned digits to its right."""
    y1,y2=[(755,792),(795,829)][row_index]
    edges=(0,140,254,368,482,596,720)
    x1,x2=edges[field_index:field_index+2]
    # Keep a small vertical halo so a cropped clothing edge cannot masquerade
    # as a digit-sized component when there is no paired plus.
    crop=image[y1-4:y2+4,x1:x2].astype(np.int16)
    b,g,r=cv2.split(crop)
    if style=='gold':
        full_mask=((r>70)&(r<235)&(g>30)&(b<120)&(r-g>25)&(g-b>25)).astype(np.uint8)
    else:
        full_mask=((r>160)&(r-g>45)&(r-b>60)).astype(np.uint8)
    mask=full_mask[4:-4]
    n,labels,stats,_=cv2.connectedComponentsWithStats(mask,8)
    pluses=[]; digits=[]; rejected=[]
    for k in range(1,n):
        x,y,w,h,area=map(int,stats[k])
        component=(labels[y:y+h,x:x+w]==k).astype(np.uint8)
        if 6<=w and (w<=26 or x+w==mask.shape[1]) and 24<=h<=31 and area>=50:
            if (not outline_filter or style=='gold' or
                    white_outline_score(image,(x1+x,y1+y,w,h),component)>=.65):
                digits.append((x,y,w,h,component))
            else:
                rejected.append((x,y,w,h))
        if not (14<=w<=26 and 14<=h<=24 and .75<=w/h<=1.35):
            continue
        contours,_=cv2.findContours(component,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        filled=np.zeros_like(component)
        cv2.drawContours(filled,contours,-1,1,-1)
        cross=np.zeros_like(component)
        cross[:,round(w*.32):round(w*.68)]=1
        cross[round(h*.32):round(h*.68),:]=1
        score=2*int((filled&cross).sum())/max(1,int(filled.sum()+cross.sum()))
        if score>=.8:
            pluses.append((x,y,w,h))
    if not pluses:
        if style=='standard':
            retained=[]
            for x,y,w,h,component in digits:
                pixels=crop[y+4:y+h+4,x:x+w][component>0]
                # Skin/background contours can pass the loose red mask and
                # white-outline test. A sparse contour with no saturated ink
                # core is not lettering. Keep coloured residual digits unknown;
                # this filter never applies to a row containing a plus.
                if component.mean()<.20 and not (
                        2*pixels.min(axis=1)<=pixels.max(axis=1)).any():
                    continue
                retained.append((x,y,w,h,component))
            digits=retained
        if any(y==0 or y+h==mask.shape[0] for x,y,w,h,component in digits):
            _,full_labels,full_stats,_=cv2.connectedComponentsWithStats(full_mask,8)
            retained=[]
            for x,y,w,h,component in digits:
                if y==0 or y+h==mask.shape[0]:
                    ys,xs=np.nonzero(component)
                    label=full_labels[y+int(ys[0])+4,x+int(xs[0])]
                    if full_stats[label,cv2.CC_STAT_HEIGHT]>31:
                        continue
                retained.append((x,y,w,h,component))
            digits=retained
        return {'status':'unresolved' if digits else 'absent','glyphs':[],
                'reason':'unpaired_digits' if digits else 'no_plus_or_digits'}
    if len(pluses)!=1:
        return {'status':'unresolved','glyphs':[],'reason':'multiple_pluses'}
    px,py,pw,ph=pluses[0]
    aligned=sorted((d for d in digits if d[0]>=px+pw and abs(d[1]+d[3]-py-ph)<=1),key=lambda d:d[0])
    paired=[]; cursor=px+pw
    for x,y,w,h,component in aligned:
        if not 1<=x-cursor<=12:
            return {'status':'unresolved','glyphs':[],'reason':'broken_digit_spacing'}
        if x+w>=mask.shape[1]:
            return {'status':'unresolved','glyphs':[],'reason':'clipped_digit'}
        scale=28/h
        resized=cv2.resize(component,(max(1,round(w*scale)),28),interpolation=cv2.INTER_NEAREST)
        normalized=np.zeros((32,32),np.uint8); left=(32-resized.shape[1])//2
        normalized[2:30,left:left+resized.shape[1]]=resized
        paired.append((x+x1,normalized,(x+x1,y+y1,w,h)))
        cursor=x+w
    if any(1<=x-cursor<=12 and abs(y+h-py-ph)<=1 for x,y,w,h in rejected):
        return {'status':'unresolved','glyphs':[],'reason':'unoutlined_trailing_digit'}
    if not paired or len(paired)>3:
        return {'status':'unresolved','glyphs':[],'reason':'orphan_plus_or_excess_digits'}
    return {'status':'present','glyphs':paired,'reason':'plus_baseline_paired',
            'plus_bbox':(px+x1,py+y1,pw,ph)}


_STACKED_BANK_CACHE: dict[tuple[int, int], tuple[object, tuple[list[str], np.ndarray, np.ndarray]]] = {}


def _get_stacked_bank(bank):
    key = (id(bank), len(bank))
    cached = _STACKED_BANK_CACHE.get(key)
    if cached is not None and cached[0] is bank:
        return cached[1]
    digits = [d for d, _, _ in bank]
    templates = np.stack([t for _, t, _ in bank]).astype(np.uint8)
    template_sums = templates.sum(axis=(1, 2)).astype(np.int32)
    res = (digits, templates, template_sums)
    if len(_STACKED_BANK_CACHE) >= 8:
        _STACKED_BANK_CACHE.clear()
    _STACKED_BANK_CACHE[key] = (bank, res)
    return res


def refine_classification(mask,bank,mode):
    """One-pixel alignment, optionally checking equally weighted local regions."""
    if not bank: return None,0
    _digits,templates,_tsums=_get_stacked_bank(bank)
    padded=np.pad(mask,1)
    shifts=np.stack([padded[y:y+32,x:x+32] for y in range(3) for x in range(3)])
    intersection=shifts[:,None]&templates[None]
    union=shifts[:,None]|templates[None]
    scores=intersection.sum(axis=(2,3))/np.maximum(1,union.sum(axis=(2,3)))
    best_shift=scores.argmax(axis=0)
    candidates={}
    for i,(digit,_template,_source) in enumerate(bank):
        shift=int(best_shift[i]); score=float(scores[shift,i])
        if digit in candidates and candidates[digit][0]>=score: continue
        if mode=='align':
            local=score
        else:
            grid=(4,8,1,32) if mode=='bands' else (2,16,2,16)
            common=intersection[shift,i].reshape(grid).sum(axis=(1,3))
            total=union[shift,i].reshape(grid).sum(axis=(1,3))
            local=float((common[total>0]/total[total>0]).mean()) if (total>0).any() else 0
        candidates[digit]=(score,local)
    ranked=sorted(candidates.items(),key=lambda item:item[1][0],reverse=True)
    digit,(score,_local)=ranked[0]
    margin=score-(ranked[1][1][0] if len(ranked)>1 else 0)
    if score>=.75 and margin>=.06: return digit,score
    if mode!='align' and score>=.75:
        local_ranked=sorted(candidates.items(),key=lambda item:item[1][1],reverse=True)
        local_digit,(_,local_score)=local_ranked[0]
        local_margin=local_score-(local_ranked[1][1][1] if len(local_ranked)>1 else 0)
        if local_digit==digit and local_score>=.75 and local_margin>=.06:
            return digit,score
    return None,score


def classify(mask,bank, *, score_mode='dice', refinement='none'):
    if not bank:
        return None,0
    digits,templates,template_sums=_get_stacked_bank(bank)
    common_arr=(templates & mask[None]).sum(axis=(1,2)).astype(np.int32)
    total_arr=int(mask.sum())+template_sums
    if score_mode=='iou':
        scores_arr=common_arr/np.maximum(1,total_arr-common_arr)
    else:
        scores_arr=(2*common_arr)/np.maximum(1,total_arr)
    best={}
    for idx,digit in enumerate(digits):
        sc=float(scores_arr[idx])
        if sc>best.get(digit,0.0):
            best[digit]=sc
    ranked=sorted(best.items(),key=lambda x:x[1],reverse=True)
    if not ranked:
        return None,0
    digit,score=ranked[0]
    margin=score-(ranked[1][1] if len(ranked)>1 else 0)
    if score>=.75 and margin>=.06: return digit,score
    if refinement!='none': return refine_classification(mask,bank,refinement)
    return None,score


def read_paired(image,banks, *, score_mode='dice', gold_banks=None, refinement='none'):
    values={}; evidence={}
    for j,field in enumerate(FIELDS):
        rows=[]
        for r in range(2):
            segment=segment_gain_row(image,r,j)
            predictions=[classify(g[1],banks[r],score_mode=score_mode,refinement=refinement) for g in segment['glyphs']]
            value=None; status=segment['status']
            if status=='absent':
                value=0
            elif status=='present':
                if predictions and all(p[0] is not None for p in predictions):
                    value=int(''.join(p[0] for p in predictions))
                    status='accepted'
                else:
                    status='unresolved'
            style='standard'
            if gold_banks is not None and status!='accepted':
                gold=segment_gain_row(image,r,j,style='gold')
                gold_predictions=[classify(g[1],gold_banks[r],score_mode=score_mode,refinement=refinement) for g in gold['glyphs']]
                if gold['status']=='present':
                    segment=gold; predictions=gold_predictions; style='gold'
                    if predictions and all(p[0] is not None for p in predictions):
                        value=int(''.join(p[0] for p in predictions)); status='accepted'
                    else:
                        value=None; status='unresolved'
                elif gold['status']=='unresolved':
                    value=None; status='unresolved'; segment=gold; predictions=[]; style='gold'
            rows.append({'value':value,'status':status,'reason':segment['reason'],
                         'style':style,'components':len(predictions),'scores':[p[1] for p in predictions],
                         'gain_layout_confirmed':segment['status']=='present' and 'plus_bbox'in segment,
                         'plus_bbox':segment.get('plus_bbox'),
                         'digit_bboxes':[glyph[2]for glyph in segment['glyphs']]})
        evidence[field]=rows
        values[field]=sum(r['value'] for r in rows) if all(r['value'] is not None for r in rows) else None
    return values,evidence


def _locate_unresolved_plus_digits(image, row_index, field_index):
    """Locate paired plus+digits on already-unresolved rows (e.g. L'Arc star capsules)."""
    y1, y2 = ((755, 792), (795, 829))[row_index]
    x1, x2 = (0, 140, 254, 368, 482, 596, 720)[field_index:field_index + 2]
    crop = image[y1 - 4:y2 + 4, x1:x2].astype(np.int16)
    b, g, r = cv2.split(crop)
    masks = (
        ((r > 160) & (r - g > 45) & (r - b > 60)).astype(np.uint8),
        ((r > 70) & (g > 30) & (b < 120) & (r - g > 20) & (g - b > 35)).astype(np.uint8),
    )
    for full_mask in masks:
        mask = full_mask[4:-4]
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        pluses = []
        digits = []
        for k in range(1, n):
            x, y, w, h, area = map(int, stats[k])
            if 6 <= w and (w <= 26 or x + w == mask.shape[1]) and 24 <= h <= 31 and area >= 50:
                digits.append((x, y, w, h))
            if not (14 <= w <= 26 and 14 <= h <= 24 and 0.75 <= w / h <= 1.35):
                continue
            component = (labels[y:y + h, x:x + w] == k).astype(np.uint8)
            contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            filled = np.zeros_like(component)
            cv2.drawContours(filled, contours, -1, 1, -1)
            cross = np.zeros_like(component)
            cross[:, round(w * 0.32):round(w * 0.68)] = 1
            cross[round(h * 0.32):round(h * 0.68), :] = 1
            score = 2 * int((filled & cross).sum()) / max(1, int(filled.sum() + cross.sum()))
            if score >= 0.8:
                pluses.append((x, y, w, h))
        if len(pluses) != 1:
            continue
        px, py, pw, ph = pluses[0]
        aligned = sorted(
            (d for d in digits if d[0] >= px + pw and abs(d[1] + d[3] - py - ph) <= 2),
            key=lambda d: d[0],
        )
        paired = []
        cursor = px + pw
        valid = True
        for x, y, w, h in aligned:
            if not 1 <= x - cursor <= 12 or x + w >= mask.shape[1]:
                valid = False
                break
            paired.append((x + x1, y + y1, w, h))
            cursor = x + w
        if valid and 1 <= len(paired) <= 3:
            return (px + x1, py + y1, pw, ph), paired
    return None, None


def _confirmed_gain_crop(image, field, index, row):
    if image is None or image.shape != (1280,720,3) or field not in FIELDS or index not in (0,1):
        return None,None
    field_index=FIELDS.index(field)
    plus=row.get('plus_bbox') if row.get('gain_layout_confirmed')is True else None
    digits=row.get('digit_bboxes')if row.get('gain_layout_confirmed')is True else None
    if plus is None or not digits:
        for style in ('standard','gold'):
            segment=segment_gain_row(image,index,field_index,style=style)
            if segment['status']=='present'and segment.get('plus_bbox')is not None:
                plus=segment['plus_bbox'];digits=[glyph[2]for glyph in segment['glyphs']];break
    if plus is None or not digits:
        plus,digits=_locate_unresolved_plus_digits(image,index,field_index)
    if not isinstance(plus,(list,tuple))or len(plus)!=4:return None,None
    y1,y2=((755,792),(795,829))[index]
    x1,x2=(0,140,254,368,482,596,720)[field_index:field_index+2]
    if not isinstance(digits,(list,tuple))or not digits:return None,None
    if any(not isinstance(box,(list,tuple))or len(box)!=4 or any(type(v)is not int for v in box)
            or not(x1<=box[0]<box[0]+box[2]<=x2 and y1<=box[1]<box[1]+box[3]<=y2)for box in digits):
        return None,None
    left=max(x1,int(plus[0])-2)
    # Existing segmentation already established the last digit. Including the
    # rest of the column also includes nearby artwork which is not gain text.
    right=min(x2,max(box[0]+box[2]for box in digits)+2)
    if not x1<=left<right<=x2:return None,None
    return image[y1:y2,left:right].copy(),[left,y1,right-left,y2-y1]


def recover_gain_rows(image,field,rows,*,secondary_reader=None,primary_engine='own_template_v4',source_frame_sha256=None):
    """Only unrecognized, positively located gain rows get another local reader."""
    from core import numeric_ocr_crosscheck as crosscheck
    enabled=crosscheck.crosscheck_enabled();minimum=crosscheck.second_opinion_min_score()
    output=[]
    for index,raw in enumerate(rows):
        row=deepcopy(raw)
        accepted=row.get('status')=='accepted';absent=row.get('occupancy')=='absent'
        primary=deepcopy(row.get('primary')) or {'status':row.get('status'),'value':row.get('value'),'raw_text':row.get('raw_text',''),
            'engine':row.get('engine',primary_engine),'confidence':row.get('confidence'),
            'reason':row.get('reason','')}
        row.setdefault('primary',primary);row.setdefault('recognition_route','primary')
        secondary=deepcopy(row.get('secondary'));roi=row.get('roi')
        if not accepted and not absent and enabled:
            crop,roi=_confirmed_gain_crop(image,field,index,row)
            if crop is not None:
                row['gain_layout_confirmed']=True
                try:
                    secondary=crosscheck.read_number(crop,reader=secondary_reader,minimum=0,maximum=999,
                        prefixes=('', '+'),suffixes=('',))
                except Exception:
                    secondary={'status':'abstained','value':None,'confidence':0.0,
                        'engine':'local_rapidocr_numeric','raw_text':'','reason':'secondary_reader_failed','min_score':minimum}
                score=secondary.get('confidence')if isinstance(secondary,dict)else None
                value=secondary.get('value')if isinstance(secondary,dict)else None
                if (isinstance(secondary,dict)and secondary.get('status')=='accepted'and type(value)is int
                        and 0<=value<=999 and type(score)in(int,float)and math.isfinite(score)and minimum<=score<=1
                        and secondary.get('engine')=='local_rapidocr_numeric'):
                    row.update(status='accepted',value=value,raw_text=f'+{value}',reason='gain_secondary_verified',
                        engine=secondary['engine'],confidence=float(score),recognition_route='secondary',
                        secondary=deepcopy(secondary),min_score=minimum,roi=roi)
        if secondary is not None and row.get('recognition_route')!='secondary':row['secondary']=deepcopy(secondary)
        decision_name = (
            'secondary_accepted' if row.get('recognition_route') == 'secondary'
            else 'primary_accepted' if accepted
            else 'absent' if absent
            else 'abstained'
        )
        if decision_name not in {'primary_accepted', 'absent'} or os.environ.get('UMA_NUMERIC_OCR_ARCHIVE_VERBOSE') == '1':
            try:
                crosscheck.archive('training_gain',decision=decision_name,
                    field=field,row_index=index,primary=primary,second=secondary,min_score=minimum,
                    source_frame_sha256=source_frame_sha256,source_status='provided'if source_frame_sha256 else'not_provided',
                    reason=row.get('reason',''),crosscheck_enabled=enabled,roi=roi)
            except Exception:pass
        output.append(row)
    return output


def gain_rows_total(rows):
    if not rows or any(row.get('status')not in {'accepted','absent'}for row in rows):return None
    values=[row.get('value')for row in rows if row.get('status')=='accepted']
    return sum(values)if all(type(value)is int and 0<=value<=999 for value in values)else None


def locate_gain_rows(image,field):
    """Existing plus/row layout only, for an unavailable upstream recognizer."""
    rows=[]
    for index in range(2):
        segments=[segment_gain_row(image,index,FIELDS.index(field),style=style)for style in('standard','gold')]
        segment=next((part for part in segments if part['status']=='present'),None)
        absent=all(part['status']=='absent'for part in segments)
        rows.append({'row_index':index,'occupancy':'absent'if absent else'present',
            'status':'absent'if absent else'unresolved','value':None,'raw_text':'',
            'reason':'no_plus_or_digits'if absent else'primary_reader_unavailable',
            'gain_layout_confirmed':segment is not None,'plus_bbox':segment.get('plus_bbox')if segment else None,
            'digit_bboxes':[glyph[2]for glyph in segment['glyphs']]if segment else[]})
    return rows


class LightweightGainReader:
    engine_name = "own_template_v4"
    source = "own_template_v4"

    def __init__(self,secondary_reader=None):
        self.secondary_reader=secondary_reader
        path = Path(__file__).with_name("assets") / "gain_templates_v4.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "own_gain_templates.v4":
            raise ValueError("unsupported gain template asset")

        def unpack(rows):
            return [[(item["digit"], np.unpackbits(
                np.frombuffer(bytes.fromhex(item["bits"]), dtype=np.uint8)
            ).reshape(32, 32), item["source"]) for item in bank] for bank in rows]

        self.banks = unpack(payload["standard"])
        self.gold_banks = unpack(payload["gold"])
        # The reviewed L'Arc glyph must not tighten another scenario's margins.
        supplement = unpack(payload["larc_standard_supplement"])
        self.larc_banks = [bank + extra for bank, extra in zip(self.banks, supplement)]
        gold_supplement = unpack(payload["larc_gold_supplement"])
        self.larc_gold_banks = [bank + extra for bank, extra in zip(self.gold_banks, gold_supplement)]

    def read_gains(self, image, *, strategy="lightweight", scenario=None):
        if image is None or image.shape != (1280, 720, 3):
            raise ValueError("lightweight gain reader requires 720x1280 BGR")
        if strategy != "lightweight":
            raise ValueError("unsupported lightweight strategy")
        values, evidence = read_paired(
            image, self.larc_banks if scenario == "larc" else self.banks, score_mode="iou",
            gold_banks=self.larc_gold_banks if scenario == "larc" else self.gold_banks,
            refinement="bands" if scenario == "larc" else "align",
        )
        candidates = {}
        for field, raw_rows in evidence.items():
            rows = []
            for index, raw in enumerate(raw_rows):
                absent = raw["status"] == "absent"
                accepted = raw["status"] == "accepted"
                value = raw["value"] if accepted else None
                rows.append({
                    "row_index": index,
                    "occupancy": "absent" if absent else "present",
                    "status": raw["status"],
                    "value": value,
                    "raw_text": f"+{value}" if accepted else "",
                    "reason": raw["reason"],
                    "style": raw["style"],
                    "scores": raw["scores"],
                    "engine": self.engine_name,
                    "confidence": min(raw['scores'])if accepted and raw['scores']else None,
                    "gain_layout_confirmed": raw.get('gain_layout_confirmed',False),
                    "plus_bbox": raw.get('plus_bbox'),
                    "digit_bboxes": raw.get('digit_bboxes',[]),
                })
            rows=recover_gain_rows(image,field,rows,secondary_reader=self.secondary_reader,primary_engine=self.engine_name)
            total = gain_rows_total(rows)
            # Formal gain maps contain positive values only. Absence remains
            # explicit row evidence, never a fabricated positive candidate.
            verified = type(total) is int and total > 0
            candidates[field] = {
                "engine": self.engine_name+'+local_rapidocr_numeric'if any(row.get('recognition_route')=='secondary'for row in rows)else self.engine_name,
                "raw_text": " ".join(row["raw_text"] for row in rows if row["raw_text"]),
                "value": total if verified else None,
                "reason": ("gain_rows_verified" if verified else
                           "gain_rows_not_found" if total == 0 else "gain_rows_unresolved"),
                "rows": rows,
            }
        return candidates
