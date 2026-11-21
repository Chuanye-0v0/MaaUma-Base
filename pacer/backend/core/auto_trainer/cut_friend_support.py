"""Fixed borrowed card reselection; no own-card or social button changes."""
import re

import cv2
from core.auto_trainer.career_setup_friend_support import analyze_friend_support_structure
from core.auto_trainer.main_menu_state_provider import _CALENDAR_REGION_READER
from core.auto_trainer.safe_advance import SafeAdvancePlanner


def target_from_profile(ctx):
    from core.auto_trainer.run_support_profile import load_profile
    if not getattr(ctx,'root',None) or not getattr(ctx,'run_id',None):return None
    profile=load_profile(ctx.root,ctx.run_id)
    card=next((item for item in (profile or {}).get('cards',[]) if item.get('slot')==5),None)
    if not card or not all(isinstance(card.get(key),str) and card[key].strip() for key in ('name','title')) or type(card.get('level')) is not int:return None
    return {'name':card['name'].strip(),'title':card['title'].strip().strip('[]【】［］'),'level':card['level'],'card_id':card.get('card_id')}


def empty_friend_slot(image,tokens):
    point=SafeAdvancePlanner._support_deck_empty_friend_slot_button(image)
    if point is not None:return point
    labels=[token for token in tokens if token['text']=='好友' and token['bbox'][0]>450 and 500<token['bbox'][1]<850]
    if len(labels)!=1:return None
    x0,y0,x1,y1=labels[0]['bbox'];cx=round((x0+x1)/2);cy=round((y0+y1)/2)
    left=max(0,cx-100);top=max(0,cy-240);crop=image[top:cy+20,left:min(720,cx+100)]
    hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    white=(hsv[:,:,1]<65)&(hsv[:,:,2]>175)
    pink=(hsv[:,:,0]>=135)&(hsv[:,:,0]<=175)&(hsv[:,:,1]>60)
    if float(white.mean())<.45 or float(pink.mean())<.06:return None
    green=cv2.inRange(hsv,(38,100,100),(92,255,255));_,_,stats,centers=cv2.connectedComponentsWithStats(green)
    crosses=[centers[index] for index,(x,y,w,h,area) in enumerate(stats[1:],1)
        if 20<=w<=65 and 20<=h<=65 and .25<area/(w*h)<.85]
    if len(crosses)!=1:return None
    return left+round(float(crosses[0][0])),top+round(float(crosses[0][1]))


def _visible_friend_rows(image, reader):
    """Keep scrolled lists usable without requiring the first unscrolled card."""
    if image.shape[:2]!=(1280,720):return ()
    header,header_conf=reader(image[35:90,150:590])
    close,close_conf=reader(image[1150:1210,250:470])
    if str(header).replace(' ','')!='选择好友协助卡' or header_conf<.9 or str(close).replace(' ','')!='关闭' or close_conf<.9:return ()
    # Full white row panels move with scrolling, including already-followed
    # friends that have no pink Follow badge. Fixed unscrolled slots do not.
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    white=((hsv[:,:,1]<25)&(hsv[:,:,2]>250)).astype('uint8')
    _,_,panels,_=cv2.connectedComponentsWithStats(white)
    rows=[(32,int(y)+8,143,int(y)+153) for x,y,w,h,area in panels[1:]
        if 20<=x<=40 and 620<=w<=675 and 145<=h<=165 and y>=100 and y+h<=1078]
    if rows:return tuple(sorted(rows,key=lambda box:box[1]))
    # Follow badges anchor a visible row; inputs remain in its title/name area.
    crop=image[180:1060,510:680];hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
    pink=cv2.inRange(hsv,(140,100,130),(175,255,255))
    _,_,stats,_=cv2.connectedComponentsWithStats(pink)
    rows=[]
    for x,y,w,h,area in stats[1:]:
        top=int(y)+180-7
        if 120<=w<=180 and 18<=h<=40 and area/(w*h)>.65 and top>=180 and top+145<=1060:
            rows.append((32,top,143,top+145))
    return tuple(sorted(rows,key=lambda box:box[1]))


def choose_friend_support(image,target,*,line_reader=None):
    if target is None:return {'state':'unknown','reason':'borrow_support_target_missing'}
    reader=line_reader or _CALENDAR_REGION_READER.read_line
    rows=_visible_friend_rows(image,reader)
    if not rows:
        structure=analyze_friend_support_structure(image)
        rows=tuple(candidate.canonical_bbox for candidate in structure.candidates) if structure.page_detected else ()
    if not rows:return {'state':'wait','reason':'friend_support_content_loading','next_phase':'setup'}
    hsv=cv2.cvtColor(image[105:1108,687:702],cv2.COLOR_BGR2HSV)
    thumbs=[(int(y)+105,int(y)+105+int(h)) for x,y,w,h,area in
        cv2.connectedComponentsWithStats(cv2.inRange(hsv,(0,0,60),(179,65,185)))[2][1:]
        if 3<=w<=12 and h>=100]
    at_top=bool(thumbs and min(y for y,_ in thumbs)<=140)
    at_bottom=bool(thumbs and max(end for _,end in thumbs)>=1090)
    seen=[]
    for x0,y0,x1,y1 in rows:
        title,title_conf=reader(image[y1-95:y1-60,145:505])
        name,name_conf=reader(image[y1-63:y1-30,145:505])
        title=''.join(str(title).split()).strip('[]【】［］');name=''.join(str(name).split())
        if name==target['name'] and title==target['title'] and name_conf>=.9 and title_conf<.85:
            # Rounded row borders can shift a tight text crop by one pixel.
            aligned,confidence=reader(image[y1-94:y1-59,145:505])
            if ''.join(str(aligned).split()).strip('[]【】［］')==title and confidence>=.85:
                title_conf=confidence;y0+=1;y1+=1
        # Exact role AND card-version title already identify this card.
        # Correct CN names such as 美浦波旁 can score only .80-.84; another
        # confidence gate would skip the requested card and scroll forever.
        if name!=target['name'] or title!=target['title'] or title_conf<.85:continue
        # Tight text-only crop excludes limit-break diamonds and card framing.
        level_text,confidence=reader(image[y1-32:y1-15,x0+51:x1-9]);level_match=re.fullmatch(r'等级(\d+)',str(level_text).replace(' ',''))
        level=int(level_match[1]) if level_match and confidence>=.9 else None
        seen.append({'name':name,'title':title,'level':level,'level_confidence':confidence,'bbox':[x0,y0,x1,y1]})
        if level!=target['level']:continue
        return {'state':'select_friend_support','reason':'select_friend_support','next_phase':'setup',
            'center':[290,y1-77],'borrow_target':target,'candidate':seen[-1]}
    if at_top and not at_bottom and len(rows)<4:
        # The first recommendation screen can paint only two opaque rows
        # while lower rows are still loading. Do not fling past that interval.
        return {'state':'wait','reason':'friend_support_content_loading','next_phase':'setup'}
    return {'state':'scroll_friend_support','reason':'scroll_friend_support','next_phase':'setup',
        'gesture':[300,900,300,550],'duration_ms':1100,'seen_target_candidates':seen}
