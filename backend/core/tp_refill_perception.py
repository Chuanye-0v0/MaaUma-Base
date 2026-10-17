"""Foreground-only TP refill recognition from the user's 2026-10-01 videos."""
import re

import cv2
import numpy as np

from core.tp_refill_bridge import ocr_tokens


TITLES={'回复训练值','恢复训练值','回复TP','恢复TP'}


def energy_drink_30(text):
    """The authorized consumable is the 30-TP drink, not any energy item."""
    return re.search(r'(?:能量饮料|精力饮料)[（(]?30(?!\d)',str(text)) is not None


def _foreground(image,reader,titles=TITLES):
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    green=cv2.inRange(hsv,(30,80,110),(90,255,255))
    _,_,stats,_=cv2.connectedComponentsWithStats(green)
    candidates=[]
    for x,y,w,h,area in stats[1:]:
        if w>=650 and 35<=h<=100:
            text=''.join(t['text'] for t in ocr_tokens(image[y:y+h,x:x+w],reader))
            if text in titles:candidates.append((int(x),int(y),int(w),int(h)))
    if not candidates:return None
    x,y,w,h=max(candidates,key=lambda b:b[1])
    dark=(image[:,30:690].mean(axis=2)<220).mean(axis=1)>.7
    bottom=None
    for row in range(y+h+200,1271):
        if dark[row:row+10].all():bottom=row;break
    if bottom is None:return {'tokens':[],'reason':'refill_foreground_boundary_unknown'}
    tokens=ocr_tokens(image[y:bottom,x:x+w],reader)
    for token in tokens:
        token['bbox']=[token['bbox'][0]+x,token['bbox'][1]+y,token['bbox'][2]+x,token['bbox'][3]+y]
    return {'tokens':tokens,'bbox':[x,y,x+w,bottom],'header_bottom':y+h}


def _number(text):
    normalized=text.replace(',','').replace('，','')
    return int(normalized) if re.fullmatch(r'\d+',normalized) else None


def _brown_inventory_before(image, label):
    """Recover an omitted small stock value; never infer it from the blue value."""
    height,width=image.shape[:2]
    cy=round((label['bbox'][1]+label['bbox'][3])/2)
    x0=round(label['bbox'][2]+10);x1=round(width*.67)
    y0=max(0,cy-24)
    if x0>=x1:return None
    crop=image[y0:min(height,cy+24),x0:x1]
    if not crop.size:return None
    mask=cv2.inRange(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV),(0,65,60),(35,255,215))
    x,y,w,h=cv2.boundingRect(mask)
    if not 2<=w<=180 or not 8<=h<=38:return None
    tight=crop[max(0,y-5):y+h+5,max(0,x-5):x+w+5]
    from core.tp_refill_bridge import LINE_READER as _CALENDAR_REGION_READER
    text,confidence=_CALENDAR_REGION_READER.read_line(tight)
    # The adjacent arrow can share the digit crop. Only strip that separator,
    # not arbitrary non-digits or a second number.
    value=_number(re.sub(r'[▶►▸›»>→]+$','',str(text).replace(' ','')))
    if confidence<.95 or value is None:return None
    return {'text':str(value),'bbox':[x0+x,y0+y,x0+x+w,y0+y+h]}


def _blue_inventory_after(image, label, before):
    """Read the small blue balance when whole-modal detection omits it."""
    height,width=image.shape[:2]
    if before['bbox'][2] > width*.67:return None
    cy=round((label['bbox'][1]+label['bbox'][3])/2)
    x0=max(round(before['bbox'][2]+30),round(width*.64))
    crop=image[max(0,cy-24):min(height,cy+24),x0:round(width*.95)]
    if not crop.size:return None
    mask=cv2.inRange(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV),(80,65,80),(130,255,255))
    x,y,w,h=cv2.boundingRect(mask)
    if w<2 or not 8<=h<=38:return None
    tight=crop[max(0,y-5):y+h+5,max(0,x-5):x+w+5]
    from core.tp_refill_bridge import LINE_READER as _CALENDAR_REGION_READER
    text,confidence=_CALENDAR_REGION_READER.read_line(tight)
    return _number(str(text)) if confidence>=.95 else None


def _inventory_pair(tokens,image=None):
    labels=[t for t in tokens if t['text'] in {'持有数','持有','所持数','所持数量'}]
    for label in labels:
        cy=(label['bbox'][1]+label['bbox'][3])/2
        values=[t for t in tokens if _number(t['text']) is not None and t['bbox'][0]>label['bbox'][2]+10
            and abs((t['bbox'][1]+t['bbox'][3])/2-cy)<=18]
        values.sort(key=lambda t:t['bbox'][0])
        if len(values)==2:return _number(values[0]['text']),_number(values[1]['text'])
        if len(values)<2 and image is not None:
            width=image.shape[1]
            before=next((value for value in values if value['bbox'][2]<=width*.67),None)
            if before is None:before=_brown_inventory_before(image,label)
            if before is None:continue
            after_values=[value for value in values if value is not before and value['bbox'][0]>=width*.64]
            after=_number(after_values[0]['text']) if len(after_values)==1 else _blue_inventory_after(image,label,before)
            if after is not None:return _number(before['text']),after
    return None


def _green_enabled(image,center,boxes):
    containing=[b for b in boxes if b[0]<=center[0]<b[0]+b[2] and b[1]<=center[1]<b[1]+b[3]]
    if not containing:return False
    x,y,w,h=min(containing,key=lambda b:b[2]*b[3]);crop=image[y:y+h,x:x+w]
    green=cv2.inRange(cv2.cvtColor(crop,cv2.COLOR_BGR2HSV),(30,90,190),(90,255,255))
    return float(np.mean(green>0))>.35


def _plus_control(image,tokens,bbox):
    gauges=[t for t in tokens if re.fullmatch(r'\d+/\d+',t['text'])]
    if len(gauges)!=1:return None
    gauge=gauges[0];cy=round((gauge['bbox'][1]+gauge['bbox'][3])/2)
    x0=round(gauge['bbox'][2]+5);x1=bbox[2]-70;y0=max(bbox[1],cy-55);y1=min(bbox[3],cy+55)
    if x1<=x0:return None
    crop=image[y0:y1,x0:x1]
    gray=cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY)
    circles=cv2.HoughCircles(gray,cv2.HOUGH_GRADIENT,1,30,param1=70,param2=20,minRadius=25,maxRadius=43)
    if circles is None:return None
    candidates=[]
    for cx,yy,r in circles[0]:
        cx,yy,r=round(float(cx)),round(float(yy)),round(float(r))
        sx0,sx1=max(0,cx-18),min(crop.shape[1],cx+19)
        sy0,sy1=max(0,yy-22),min(crop.shape[0],yy+23)
        glyph=crop[sy0:sy1,sx0:sx1]
        brown=cv2.inRange(cv2.cvtColor(glyph,cv2.COLOR_BGR2HSV),(0,60,40),(35,255,210))
        _,_,stats,_=cv2.connectedComponentsWithStats(brown)
        cross=any(14<=w<=36 and 20<=h<=44 and .3<area/(w*h)<.85 for _,_,w,h,area in stats[1:])
        white=crop[max(0,yy-r+6):min(crop.shape[0],yy+r-6),max(0,cx-r+6):min(crop.shape[1],cx+r-6)]
        if cross and white.size:
            candidates.append({'center':[x0+cx,y0+yy],
                'enabled':float(np.mean(white.mean(axis=2)>220))>.45})
    return candidates[0] if len(candidates)==1 else None


def _plus_button(image,tokens,bbox):
    control=_plus_control(image,tokens,bbox)
    return control['center'] if control is not None and control['enabled'] else None


def _medicine_titles(tokens):
    titles=[]
    for token in tokens:
        if '巧克力' in token['text']:continue
        if energy_drink_30(token['text']):
            titles.append(token);continue
        if not re.search(r'(?:能量饮料|精力饮料)$',token['text']):continue
        cy=(token['bbox'][1]+token['bbox'][3])/2
        units=[other for other in tokens if other['text'].strip('（）()')=='30'
            and -4<=other['bbox'][0]-token['bbox'][2]<=70
            and abs((other['bbox'][1]+other['bbox'][3])/2-cy)<=18]
        if len(units)==1:
            unit=units[0]
            titles.append({'text':token['text']+'30','bbox':[token['bbox'][0],min(token['bbox'][1],unit['bbox'][1]),
                unit['bbox'][2],max(token['bbox'][3],unit['bbox'][3])]})
    return titles


def _row_stock(tokens):
    # Optional display only. Never invokes the small brown/blue stock OCR.
    labels=[t for t in tokens if t['text'] in {'持有数','持有','所持数','所持数量'}]
    if len(labels)!=1:return None
    label=labels[0];cy=(label['bbox'][1]+label['bbox'][3])/2
    values=[_number(t['text']) for t in tokens if _number(t['text']) is not None
        and t['bbox'][0]>label['bbox'][2] and abs((t['bbox'][1]+t['bbox'][3])/2-cy)<=18]
    return values[0] if len(values)==1 else None


def _use_enabled(image,center,boxes):
    containing=[b for b in boxes if b[0]<=center[0]<b[0]+b[2] and b[1]<=center[1]<b[1]+b[3]]
    if not containing:return False
    x,y,w,h=min(containing,key=lambda b:b[2]*b[3])
    crop=image[y+max(0,h//5):y+h-max(0,h//5),x+max(0,w//8):x+w-max(0,w//8)]
    if not crop.size:return False
    hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    # Enabled white Use buttons have brown lettering; disabled labels are gray.
    brown=cv2.inRange(hsv,(0,60,35),(35,255,215))
    return float(np.mean(brown>0))>.008 or _green_enabled(image,center,boxes)


def _minimum_is_set(image,tokens,boxes):
    from core.tp_refill_bridge import visible_control
    center=visible_control(tokens,boxes,{'最小'})
    if center is None:return False
    containing=[b for b in boxes if b[0]<=center[0]<b[0]+b[2] and b[1]<=center[1]<b[1]+b[3]]
    if not containing:return False
    x,y,w,h=min(containing,key=lambda b:b[2]*b[3])
    crop=image[y+5:y+h-5,x+5:x+w-5]
    if not crop.size:return False
    # The existing Min button is gray at the lower bound. An unchanged white
    # enabled Min after the tap is stale UI, not proof of a one-unit quantity.
    bright=float(np.mean((crop.min(axis=2)>220)&(crop.max(axis=2)-crop.min(axis=2)<40)))
    return bright<.25 or not _use_enabled(image,center,boxes)


def _normal_tp_stop(reason,**details):
    messages={
        'tp_refill_limit':'本次TP补给次数或消费额度已用完。',
        'tp_carrots_insufficient':'萝卜不足，无法完成本次TP补给。',
        'tp_insufficient_no_allowed_resources':'当前TP不足，没有可用药品或已授权的后备补给。'}
    return {'state':'tp_insufficient','reason':reason,'normal_stop':True,
        'resource_status':'insufficient','message':messages[reason],**details}


def tp_resource_shortage(tokens,boxes,*,selected_resource_kind):
    text=''.join(t['text'] for t in tokens)
    medicine=any(term in text for term in ('道具不足','药品不足','能量饮料不足','精力饮料不足','没有可使用的道具','没有持有该道具'))
    carrots=any(term in text for term in ('宝石不足','萝卜不足','胡萝卜不足','所持宝石不足','所持萝卜不足'))
    kind='carrots' if carrots else 'tp_items' if medicine else None
    if kind is None or selected_resource_kind not in (None,kind):return None
    from core.tp_refill_bridge import visible_control
    center=visible_control(tokens,boxes,{'取消','关闭','返回'})
    if center is None and not any(term in text for term in ('购买','商城','充值','支付')):
        center=visible_control(tokens,boxes,{'确定','确认','OK'})
    if center is None:return {'state':'unknown','reason':'tp_unavailable_close_control_unverified'}
    return {'state':'dismiss_tp_unavailable','reason':'tp_resource_unavailable','center':center,
        'next_phase':'refill','unavailable_resource_kind':kind}


def analyze_tp_refill(image, *, remaining, selected_resource_kind=None, returning=False,reader=None,
                      medicine_enabled=True,carrots_enabled=True,refill_selection=None,unavailable_resources=()):
    foreground=_foreground(image,reader)
    if foreground is None:return None
    return analyze_tp_controls(image,foreground=foreground,remaining=remaining,
        selected_resource_kind=selected_resource_kind,returning=returning,
        medicine_enabled=medicine_enabled,carrots_enabled=carrots_enabled,
        refill_selection=refill_selection,unavailable_resources=unavailable_resources)


def analyze_tp_controls(image,*,foreground,remaining,selected_resource_kind=None,returning=False,
                        medicine_enabled=True,carrots_enabled=True,refill_selection=None,unavailable_resources=(),boxes=None):
    tokens=foreground['tokens'];result={'state':'unknown','reason':foreground.get('reason','refill_page_unrecognized')}
    if not tokens:return result
    from core.tp_refill_bridge import button_boxes,visible_control
    x0,y0,x1,y1=foreground['bbox']
    boxes=[b for b in (button_boxes(image) if boxes is None else boxes)
        if x0<=b[0] and y0<=b[1] and b[0]+b[2]<=x1 and b[1]+b[3]<=y1]
    text=''.join(t['text'] for t in tokens);result['foreground_bbox']=foreground['bbox']
    shortage=tp_resource_shortage(tokens,boxes,selected_resource_kind=selected_resource_kind)
    if shortage:return {**result,**shortage}
    if any(t in text for t in ('充值','人民币','支付','购买商品','购买宝石','购买萝卜','前往商城','前往商店')):
        return {**result,'reason':'resource_action_outside_cut_scope'}
    def action(state,labels,next_phase,**details):
        center=visible_control(tokens,boxes,set(labels))
        if center is None:return {**result,'reason':state+'_control_unverified'}
        return {**result,'state':state,'reason':state,'center':center,'next_phase':next_phase,**details}
    def budget(kind):
        value=remaining.get(kind,0)
        return float('inf') if value is None else value
    unavailable=set(unavailable_resources)
    kinds=[]
    if _medicine_titles(tokens):kinds.append('tp_items')
    if any(term in text for term in ('宝石','萝卜','胡萝卜')):kinds.append('carrots')
    close=visible_control(tokens,boxes,{'关闭'})
    confirm=visible_control(tokens,boxes,{'确定','确认','OK'})
    uses=[t for t in tokens if t['text']=='使用' and visible_control([t],boxes,{'使用'}) is not None]
    # Inventory figures are optional receipt information, never authorization.
    pair=_inventory_pair(tokens)
    if close is not None and confirm is None and not uses and len(kinds)==1 and '使用' in text and any(term in text for term in ('回复','恢复')):
        kind=kinds[0]
        if selected_resource_kind not in (None,kind):return {**result,'reason':'refill_selected_resource_mismatch'}
        optional={}
        if pair is not None:
            before,after=pair
            optional.update(reported_resource_before=before,reported_resource_after=after,reported_resource_delta=before-after)
        gauges=sorted((t for t in tokens if re.fullmatch(r'\d+/\d+',t['text'])),key=lambda t:t['bbox'][0])
        if len(gauges)==2:
            optional.update(reported_training_points_before=int(gauges[0]['text'].split('/')[0]),
                reported_training_points_after=int(gauges[1]['text'].split('/')[0]))
        return action('close_tp_result',('关闭',),'refill_return',reported_resource_kind=kind,
            refill_receipt='success_result',**optional)
    if confirm is not None and len(kinds)==1 and close is None:
        kind=kinds[0]
        if selected_resource_kind not in (None,kind):return {**result,'reason':'refill_selected_resource_mismatch'}
        selection=refill_selection or {}
        if selection.get('kind') not in (None,kind):return {**result,'reason':'refill_selected_resource_mismatch'}
        stage=selection.get('stage')
        if stage=='confirmed':return {**result,'state':'wait','reason':'tp_refill_confirmation_pending','next_phase':'refill'}
        allowed=medicine_enabled if kind=='tp_items' else carrots_enabled
        if not allowed or budget(kind)<=0:return {**result,**_normal_tp_stop('tp_refill_limit')}
        if stage not in {'minimum','one'}:
            return action('minimize_tp_refill',('最小',),'refill',selected_resource_kind=kind,
                quantity_basis='normalize_to_minimum')
        enabled=_green_enabled(image,confirm,boxes)
        if stage=='minimum' and not _minimum_is_set(image,tokens,boxes):
            return {**result,'state':'wait','reason':'tp_refill_minimum_settling','next_phase':'refill'}
        if enabled:
            # Read this now-one-unit page's actual cost only AFTER quantity
            # normalization. A preselected multi-unit total is not a unit price.
            unit_match=re.search(r'(?:使用|消耗)(\d+)个?(?:宝石|胡萝卜|萝卜)',text) if kind=='carrots' else None
            unit=1 if kind=='tp_items' else int(unit_match[1]) if unit_match else None
            if unit is None or unit<1:return {**result,'reason':'refill_unit_cost_unknown'}
            if unit>budget(kind):return {**result,**_normal_tp_stop('tp_refill_limit')}
            optional={}
            if pair is not None:optional.update(preview_resource_before=pair[0],preview_resource_after=pair[1])
            return action('confirm_tp_refill',('确定','确认','OK'),'refill',resource_kind=kind,
                resource_amount=unit,selected_units=1,quantity_basis='minimum_then_at_most_one_increment',**optional)
        plus=_plus_control(image,tokens,foreground['bbox'])
        if stage=='minimum' and plus is not None and plus['enabled']:
            return {'state':'select_one_tp_refill','reason':'select_one_tp_refill','center':plus['center'],'next_phase':'refill',
                'selected_resource_kind':kind,'quantity_basis':'one_increment_after_minimum'}
        if any(term in text for term in ('不足','没有持有','持有数为0','无法使用')) or kind in unavailable:
            return action('dismiss_tp_unavailable',('取消','关闭'),'refill',unavailable_resource_kind=kind)
        if plus is None:return {**result,'reason':'refill_plus_control_unverified'}
        if stage=='one' and plus['enabled']:return {**result,'state':'wait','reason':'tp_refill_selection_settling','next_phase':'refill'}
        # A disabled plus and disabled confirm at minimum establish no usable
        # quantity; closing is non-consuming, and the owner chooses allowed backup.
        return action('dismiss_tp_unavailable',('取消','关闭'),'refill',unavailable_resource_kind=kind)
    if close is not None and confirm is None and uses:
        if returning:return action('close_tp_menu',('关闭',),'refill_return',refill_receipt='returned_refill_menu')
        medicine_titles=_medicine_titles(tokens)
        def row_for(title):
            top=title['bbox'][1]-10
            following=[t['bbox'][1] for t in tokens if t is not title and
                ('能量饮料' in t['text'] or '精力饮料' in t['text'] or '巧克力' in t['text'] or t['text'] in {'宝石','萝卜','胡萝卜'}) and t['bbox'][1]>top+20]
            bottom=min(following)-5 if following else top+140
            return [t for t in tokens if top<=(t['bbox'][1]+t['bbox'][3])/2<=bottom]
        medicine_available=False
        for title in medicine_titles if medicine_enabled and budget('tp_items')>0 and 'tp_items' not in unavailable else ():
            row=row_for(title);center=visible_control(row,boxes,{'使用'})
            stock=_row_stock(row)
            if center is not None and _use_enabled(image,center,boxes):
                return {**result,'state':'choose_tp_item','reason':'choose_tp_item','center':center,'next_phase':'refill',
                    'observed_item_stock':stock,'medicine_inventory':{'basis':'same_row_use_enabled','observed_item_stock':stock}}
            medicine_available=medicine_available or center is None
        # Unknown stock remains None. A readable usable entry, not the digit,
        # decides whether a medicine can be selected.
        diagnostic={'basis':'medicine_disabled_by_config' if not medicine_enabled else
            'medicine_budget_exhausted' if budget('tp_items')<=0 else
            'medicine_unavailable_feedback' if 'tp_items' in unavailable else
            'no_enabled_medicine_use','observed_item_stock':None}
        if medicine_available:return {**result,'reason':'tp_item_same_row_use_unverified','medicine_inventory':diagnostic}
        if carrots_enabled and budget('carrots')>0 and 'carrots' not in unavailable:
            for title in [t for t in tokens if t['text'] in {'宝石','萝卜','胡萝卜'}]:
                center=visible_control(row_for(title),boxes,{'使用'})
                if center is not None and _use_enabled(image,center,boxes):
                    return {**result,'state':'choose_tp_carrots','reason':'choose_tp_carrots','center':center,'next_phase':'refill',
                        'medicine_inventory':diagnostic}
        reason=('tp_carrots_insufficient' if 'carrots' in unavailable else
            'tp_refill_limit' if (medicine_enabled or carrots_enabled) and budget('tp_items')<=0 and budget('carrots')<=0
            else 'tp_insufficient_no_allowed_resources')
        return {**result,**_normal_tp_stop(reason,medicine_inventory=diagnostic)}
    return result
