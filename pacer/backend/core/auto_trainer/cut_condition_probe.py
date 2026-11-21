"""Read cut from semantic condition cards, independent of row number/size."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import time
from threading import local

import cv2
import numpy as np

from core.auto_trainer.condition_probe import (
    ConditionProbeExecutor, analyze_ability_details_button_anchor,
    analyze_race_day_ability_details_button_anchor,
    _ability_button_circle,
)
from core.auto_trainer.frame_safety import LocalRapidOCRScreenTextReader
from core.execution_frames import ExecutionFrames
from core.native_capture_adb import adb_for_capture
from core.coordinates import CoordinateConfig,CoordinateScaler
from core.character_names import character_candidates
from core.name_match import is_confident, match_character


def compact(text):
    return re.sub(r'\s+', '', str(text))


_DEFAULT_OCR_READERS=local()


def _default_ocr_reader():
    """Reuse an engine in this control flow, never OCR text or another thread."""
    reader=getattr(_DEFAULT_OCR_READERS,'reader',None)
    if reader is None:
        reader=LocalRapidOCRScreenTextReader()
        _DEFAULT_OCR_READERS.reader=reader
    return reader


class StatusCoverage:
    """Keep overlap and readable rows from the verified list top to its tail."""
    def __init__(self):self.started=False;self.read=set();self.unresolved=set();self.previous=set();self.read_descriptions={}
    def add(self,result):
        if not self.started:
            if not result.get('top_verified'):return False
            self.started=True
        cards=result.get('cards',[])
        current={compact(c['title']).rstrip('○◎◯') for c in cards if c.get('complete')}
        if self.read and current and not (current&self.previous):
            self.unresolved.add('unverified_scroll_overlap')
        for index,card in enumerate(cards):
            title=compact(card.get('title','')).rstrip('○◎◯')
            if card.get('complete'):
                self.read.add(title);self.unresolved.discard(title)
                if card.get('description'):self.read_descriptions[title]=compact(card['description'])
            elif (index==0 and card.get('edge_clipped') and current&self.previous
                    and card.get('title') and card.get('description')
                    and any(description.endswith(compact(card['title'])+compact(card['description']))
                        for description in self.read_descriptions.values())):
                # A scrolled-out heading can make its already-read explanation
                # look like a new title. Require exact cached text and overlap;
                # never ignore an unknown complete card or an uncovered region.
                continue
            elif title and title not in self.read:
                self.unresolved.add(title)
            elif not card.get('edge_clipped'):
                self.unresolved.add('unreadable_interior_card')
        self.previous=current
        return result.get('tail_verified',False) and not self.unresolved


def ocr_tokens(image, reader=None):
    reader = reader or _default_ocr_reader().read_regions
    regions = reader(image) if callable(reader) else reader.read_regions(image)
    tokens = []
    for value in regions or []:
        try:
            box, text, confidence = value[:3]
            if float(confidence) < .9:
                continue
            xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
            tokens.append({'text': compact(text), 'bbox': [min(xs), min(ys), max(xs), max(ys)], 'confidence': float(confidence)})
        except (ValueError, TypeError, IndexError):
            continue
    return tokens


def locate_ability_button(png_bytes):
    """Use the existing locator, with semantic ink inside its verified circle.

    L'Arc's scenery can lower the old full-template score. This mode accepts
    the same circle geometry only when its own two labels are read clearly.
    """
    result=analyze_ability_details_button_anchor(png_bytes)
    if not result['abstained']:return result
    evidence=result.get('evidence',{})
    if not evidence.get('structure',{}).get('circle_verified') or not evidence.get('roi'):
        return result
    image=cv2.imdecode(np.frombuffer(png_bytes,np.uint8),cv2.IMREAD_COLOR)
    if image is None or image.shape[1::-1] not in {(720,1280),(1440,2560)}:return result
    image=cv2.resize(image,(720,1280));x0,y0,x1,y1=evidence['roi']
    tokens=ocr_tokens(image[y0:y1,x0:x1])
    if ''.join(t['text'] for t in tokens)!='能力详情':return result
    return {**result,'value':'ability_details','abstained':False,
        'confidence':min(t['confidence'] for t in tokens),
        'evidence':{**evidence,'acceptance':'verified_circle_and_exact_ability_labels'}}


def locate_race_ability_button(png_bytes):
    """Cut-only semantic fallback for the shifted L'Arc race-day circle."""
    result=analyze_race_day_ability_details_button_anchor(png_bytes,require_primary_action=False)
    if not result['abstained']:return result
    image=cv2.imdecode(np.frombuffer(png_bytes,np.uint8),cv2.IMREAD_COLOR)
    if image is None or image.shape[1::-1] not in {(720,1280),(1440,2560)}:return result
    image=cv2.resize(image,(720,1280))
    header=''.join(token['text'] for token in ocr_tokens(image[:190]))
    exchange_hub=False
    if header.replace(' ','').startswith('凯旋门代表交流赛'):
        from core.auto_trainer.larc_pages import is_larc_race_day
        text=''.join(token['text'] for token in ocr_tokens(image))
        exchange_hub=is_larc_race_day(image,{'screen_text':{'normalized_text':text}})
    if not exchange_hub and ('比赛日' not in header or '目标' not in header):return result
    coordinates=CoordinateConfig(Path(__file__).resolve().parents[2]/'config/coordinates.json')
    coordinates.require_verified('race_day_main_menu','ability_details')
    x,y=CoordinateScaler(coordinates.reference_size,(720,1280)).scale_point(*coordinates.point('race_day_main_menu','ability_details'))
    roi=[x-35,y-35,x+45,y+40];x0,y0,x1,y1=roi;crop=image[y0:y1,x0:x1]
    circle=_ability_button_circle(crop)
    if not circle['circle_verified']:return result
    tokens=ocr_tokens(crop)
    if ''.join(token['text'] for token in tokens)!='能力详情':return result
    return {**result,'value':'ability_details','abstained':False,'confidence':min(token['confidence'] for token in tokens),
        'evidence':{'acceptance':'verified_exchange_hub_and_ability_circle_labels' if exchange_hub
                    else 'verified_race_day_caption_and_ability_circle_labels',
            'source_sha256':hashlib.sha256(png_bytes).hexdigest(),'roi':roi,'point':[x,y],'circle':circle}}


def analyze_cut_details(image_path, *, reader=None,expected_name=None,title_reader=None):
    path = Path(image_path)
    result = {'state': 'unknown', 'reason': 'details_unverified', 'source': str(path)}
    raw = path.read_bytes()
    result['source_sha256'] = hashlib.sha256(raw).hexdigest()
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return result
    scale = image.shape[1] / 720
    image = cv2.resize(image, (720, round(image.shape[0] / scale)))
    tokens = ocr_tokens(image, reader)
    text = ''.join(t['text'] for t in tokens)
    titles = [t for t in tokens if t['text'] in {'优俊少女详情', '優駿少女詳情', '能力详情'}]
    status = [t for t in tokens if t['text'] in {'状态', '狀態'}]
    close = [t for t in tokens if t['text'] in {'关闭', '關閉'}]
    if len(titles) != 1 or len(status) != 1 or len(close) != 1:
        return result
    status_y, close_y = status[0]['bbox'][3], close[0]['bbox'][1]
    if not titles[0]['bbox'][1] < status_y < close_y:
        return result
    sx0,sy0,sx1,sy1=status[0]['bbox']
    status_crop=image[max(0,round(sy0-8)):round(sy1+8),max(0,round(sx0-20)):round(sx1+20)]
    green=cv2.inRange(cv2.cvtColor(status_crop,cv2.COLOR_BGR2HSV),(30,80,100),(85,255,255))
    if float(np.mean(green>0))<.3:
        return {**result,'page_verified':True,'reason':'status_tab_unselected',
            'close_center':[round((close[0]['bbox'][0]+close[0]['bbox'][2])/2*scale),round((close[0]['bbox'][1]+close[0]['bbox'][3])/2*scale)],
            'status_center':[round((sx0+sx1)/2*scale),round((sy0+sy1)/2*scale)]}
    candidates=character_candidates()
    name_rows=[]
    for token in tokens:
        if token['bbox'][1]>=status_y:continue
        identifier,score,basis=match_character(token['text'],candidates)
        if is_confident(score,basis):
            name_rows.append((identifier,token['text'],score,basis))
    awakening=[t for t in tokens if '觉醒等级' in t['text'] and t['bbox'][1]<status_y]
    if len(awakening)==1:
        above=[t for t in tokens if titles[0]['bbox'][3]<t['bbox'][1]<awakening[0]['bbox'][1]
            and t['bbox'][0]>180 and not t['text'].startswith(('[','【')) and not re.fullmatch(r'[\d☆★]+',t['text'])]
        if above:
            nearest=max(above,key=lambda t:t['bbox'][1])
            identifier,score,basis=match_character(nearest['text'],candidates)
            name_rows=[(identifier,nearest['text'],score,basis)] if is_confident(score,basis) else []
            result['trainee_name_raw']=nearest['text']
    identities={row[0] for row in name_rows}
    name_row=name_rows[0] if len(identities)==1 else None
    result['trainee_name']=next((row['name'] for row in candidates if name_row and row['character_id']==name_row[0]),None)
    if name_row is not None:
        result['trainee_name_raw']=name_row[1]
        result['trainee_name_match']={'character_id':name_row[0],'score':name_row[2],'basis':name_row[3]}
    # Both orange positive and blue negative state cards participate in list
    # coverage. Color locates a card; semantic title identifies its condition.
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (3, 100, 130), (35, 255, 255))
    mask |= cv2.inRange(hsv,(85,70,100),(135,255,255))
    mask[:round(status_y)] = 0
    mask[round(close_y):] = 0
    # Tap sparkles can connect adjacent cards at a narrow strip. A real card
    # spans most of the list width; preserve its horizontal separators.
    sparse=np.count_nonzero(mask,axis=1)<image.shape[1]*.45
    boundaries=np.flatnonzero(np.diff(np.pad(sparse.astype('int8'),(1,1))))
    for start,end in boundaries.reshape(-1,2):
        # The one-pixel title underline belongs to the same card.
        if end-start>=5:mask[start:end]=0
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    cards = []
    for x, y, width, height, area in stats[1:]:
        if width >= 500 and height >= 35 and area >= width * height * .6:
            cards.append([int(x), int(y), int(x+width), int(y+height)])
    cards.sort(key=lambda box: box[1])
    observations = []
    all_read = bool(cards)
    for box in cards:
        x0, y0, x1, y1 = box
        inside = [t for t in tokens if x0 <= (t['bbox'][0]+t['bbox'][2])/2 <= x1
                  and y0 <= (t['bbox'][1]+t['bbox'][3])/2 <= y1]
        title_rows = [t for t in inside if t['bbox'][1] <= y0 + min(38, (y1-y0)*.35)]
        descriptions = [t for t in inside if t not in title_rows]
        title, explanation = ''.join(t['text'] for t in title_rows), ''.join(t['text'] for t in descriptions)
        title_evidence=None
        maybe_cut=title!='才能出众' and any(fragment in compact(title) for fragment in ('才能','出众','出衆','出眾'))
        if not title or maybe_cut:
            region_reader=title_reader
            if region_reader is None and reader is None:
                from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
                region_reader=_CALENDAR_REGION_READER.read_line
            if region_reader is not None:
                roi=[x0+13,y0+6,min(x1-12,x0+328),min(y1,y0+43)]
                rx0,ry0,rx1,ry1=roi
                candidate,confidence=region_reader(image[ry0:ry1,rx0:rx1])
                candidate=compact(candidate)
                if confidence>=.9 and candidate:
                    title=candidate
                    title_evidence={'reader':'status_title_region','confidence':float(confidence),'roi':roi}
        maybe_cut=title!='才能出众' and any(fragment in compact(title) for fragment in ('才能','出众','出衆','出眾'))
        # Farming asks one question: is the target present? Any other readable
        # full card is enough; recognizing its game effect or maintaining a
        # catalogue of every positive/negative condition is unnecessary.
        complete=y1-y0>=85 and bool(title) and not maybe_cut
        observations.append({'title': title, 'description': explanation, 'bbox': box,'complete':complete,
            'edge_clipped':y1-y0<85 and (box==cards[0] or box==cards[-1]),
            **({'title_evidence':title_evidence} if title_evidence is not None else {})})
        if title == '才能出众':
            return {**result, 'state': 'present', 'reason': 'cut_title_in_verified_status_card',
                'cards': observations, 'page_verified': True}
        if not complete:
            all_read = False
    result.update(cards=observations, page_verified=True,
        list_region=[max(15, cards[0][0]-10) if cards else 20, round(status_y+20), 705, round(close_y-20)],
        close_center=[round((close[0]['bbox'][0]+close[0]['bbox'][2])/2*scale),
                      round((close[0]['bbox'][1]+close[0]['bbox'][3])/2*scale)])
    scroll_mask=cv2.inRange(hsv[:,688:710],(0,0,60),(179,65,185))
    scroll_mask[:round(status_y+15)]=0;scroll_mask[round(close_y-40):]=0
    _,_,scroll_stats,_=cv2.connectedComponentsWithStats(scroll_mask)
    thumbs=[(y,y+h) for x,y,w,h,area in scroll_stats[1:] if 2<=w<=12 and h>=50 and area>w*h*.65]
    top_verified=(min(t[0] for t in thumbs)<=status_y+55 if thumbs else not cards or cards[0][1]<=status_y+65 and observations[0]['complete'])
    result['top_verified']=bool(top_verified)
    if not cards and any(term in text for term in ('暂无内容', '暂无状态', '没有状态')):
        return {**result, 'state': 'absent', 'reason': 'explicit_empty_status','tail_verified':True,'top_verified':True}
    if not cards and close_y-status_y>160:
        empty=image[round(status_y+30):round(close_y-70),100:620]
        if empty.size and float(np.std(empty.astype(float),axis=(0,1)).max())<5:
            return {**result,'state':'absent','reason':'verified_blank_status_list','tail_verified':True,'top_verified':True}
    # A real blank tail plus every visible card read is required for absence.
    # A clipped last card, unread title/description, or a continued list is unknown.
    blank_tail = False
    if cards and cards[-1][3]-cards[-1][1]>=85 and cards[-1][3]+35 < close_y:
        crop = image[cards[-1][3]+10:round(min(close_y-20,cards[-1][3]+60)), 180:540]
        blank_tail = crop.size > 0 and float(np.std(crop.astype(float), axis=(0,1)).max()) < 9
    result['tail_verified']=bool(blank_tail)
    if all_read and blank_tail and top_verified:
        return {**result, 'state': 'absent', 'reason': 'complete_read_status_cards_and_blank_tail'}
    return {**result, 'reason': 'status_list_incomplete_or_unreadable', 'scroll_needed': bool(thumbs) or not blank_tail}


class CutConditionProbe:
    """Reuse the verified details entry. A positive result never closes/taps."""
    def __init__(self, *, adb_factory=None, analyzer=analyze_cut_details, sleep=time.sleep,
                 clock=time.monotonic,ready_timeout=5.):
        self.adb_factory = adb_factory or (lambda ctx: adb_for_capture(ctx.port))
        self.analyzer, self.sleep = analyzer, sleep
        self.clock,self.ready_timeout=clock,ready_timeout

    def _capture_ready(self,frames,path,predicate):
        """Bounded read-only handoff after one input; never repeat that input."""
        deadline=self.clock()+self.ready_timeout
        image=value=None
        for attempt in range(10):
            image=frames.screenshot(str(path))
            if image is not None:
                value=predicate(path,image)
                if value:return image,value
            # A slow OCR predicate may spend the entire time budget on the
            # first animation frame. Still give one subsequent fresh capture
            # a chance; the time budget and ten-read cap bound all later reads.
            if attempt>=1 and self.clock()>=deadline:break
            self.sleep(.15)
        return image,None

    def __call__(self, ctx, output):
        output = Path(output);output.mkdir(parents=True, exist_ok=True)
        frames = ExecutionFrames(self.adb_factory(ctx))
        helper = ConditionProbeExecutor()
        if not frames.connect():
            return {'state': 'unknown', 'reason': 'cut_probe_connect_failed', 'returned': False}
        before = output/'before.png'
        image = frames.screenshot(str(before))
        if image is None:
            return {'state': 'unknown', 'reason': 'cut_probe_capture_failed', 'returned': False}
        raw = before.read_bytes()
        normal = locate_ability_button(raw)
        race = locate_race_ability_button(raw) if normal['abstained'] else {'abstained':True}
        section = 'main_menu' if not normal['abstained'] else 'race_day_main_menu' if not race['abstained'] else None
        if section is None:
            return {'state': 'unknown', 'reason': 'ability_button_unverified', 'returned': False}
        tap = helper._scaled_tap(section, 'ability_details', image.shape[1::-1])
        if frames.tap(tap['x'],tap['y'],scaled=False) is not True:
            return {'state': 'unknown', 'reason': 'ability_open_input_unconfirmed', 'returned': False}
        self.sleep(.3)
        open_retries=0
        previous = None;tab_selected=False;coverage=StatusCoverage();seeking_top=False
        for index in range(14):
            details = output/f'details_{index:02d}.png'
            def details_ready(path,image):
                observed=(self.analyzer(path,expected_name=getattr(ctx,'cut_expected_character_name',None))
                    if self.analyzer is analyze_cut_details else self.analyzer(path))
                if not observed.get('page_verified'):return None
                if tab_selected and observed.get('reason')=='status_tab_unselected':return None
                return observed
            image,result=self._capture_ready(frames,details,details_ready)
            if result is None:
                if index==0 and section=='main_menu' and not tab_selected and not open_retries and image is not None:
                    # A successful transport receipt need not mean the game
                    # opened details. Retry only the same verified career home;
                    # never infer absent or tap this coordinate on a detail page.
                    current=locate_ability_button(details.read_bytes())
                    if not current['abstained']:
                        from core.auto_trainer.cut_training_transition_recovery import recovered_header
                        baseline=recovered_header(before);observed=recovered_header(details)
                        if baseline is not None and observed==baseline:
                            retry=helper._scaled_tap('main_menu','ability_details',image.shape[1::-1])
                            from core.auto_trainer.cut_farming import atomic_json
                            atomic_json(output/'ability_open_retry.json',{'retry_index':1,'before_identity':baseline,
                                'observed_identity':observed,'source':str(details),'tap':retry})
                            if frames.tap(retry['x'],retry['y'],scaled=False) is not True:
                                return {'state':'unknown','reason':'ability_open_input_unconfirmed','returned':False,'ability_open_retries':1}
                            open_retries=1;self.sleep(.3)
                            image,result=self._capture_ready(frames,details,details_ready)
                if result is None:
                    return {'state':'unknown','reason':'details_ready_timeout','source':str(details),'returned':False,'ability_open_retries':open_retries}
            result={**result,'ability_open_retries':open_retries}
            if result['state'] == 'present':
                return {**result, 'returned': False} # Stop all input here.
            if result.get('reason')=='status_tab_unselected' and not tab_selected:
                if frames.tap(*result['status_center'],scaled=False) is not True:
                    return {**result,'reason':'status_tab_input_unconfirmed','returned':False}
                tab_selected=True;self.sleep(.3);continue
            if not result.get('page_verified'):
                return {**result,'returned':False}
            if not coverage.started and not result.get('top_verified'):
                seeking_top=True
            else:
                seeking_top=False
                complete=coverage.add(result)
                if complete:result={**result,'state':'absent','reason':'complete_top_to_tail_status_coverage','covered_titles':sorted(coverage.read)}
                elif result['state']=='absent':result={**result,'state':'unknown','reason':'incomplete_status_coverage'}
            if result['state'] == 'absent' and coverage.started and not seeking_top:
                close = result.get('close_center')
                if close is None or frames.tap(*close,scaled=False) is not True:
                    return {**result, 'state': 'unknown', 'reason': 'details_close_unconfirmed', 'returned': False}
                self.sleep(.3)
                after=output/'returned.png'
                def menu_ready(path,image):
                    raw=path.read_bytes()
                    return (not locate_ability_button(raw)['abstained']
                        or not locate_race_ability_button(raw)['abstained'])
                image,verified=self._capture_ready(frames,after,menu_ready)
                return {**result,'returned':bool(verified),'source':str(after),
                    **({} if verified else {'reason':'details_return_ready_timeout'})}
            if not result.get('scroll_needed') or index == 13:
                return {**result, 'returned': False}
            if previous == result.get('cards'):
                return {**result, 'reason': 'status_scroll_no_progress', 'returned': False}
            previous = result.get('cards')
            x0,y0,x1,y1=result['list_region'];scale=image.shape[1]/720
            x=round((x0+x1)/2*scale)
            middle=(y0+y1)/2
            start,end=(middle-40,min(y1-20,middle+160)) if seeking_top else (middle+40,max(y0+20,middle-160))
            if frames.swipe(x,round(start*scale),x,round(end*scale),400,scaled=False) is not True:
                return {**result, 'reason': 'status_scroll_unconfirmed', 'returned': False}
            self.sleep(.3)
        return {'state':'unknown','reason':'status_scan_limit','returned':False}
