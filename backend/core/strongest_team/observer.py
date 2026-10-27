"""Read-only same-frame UI parser. Every offered input has a measured target."""
import hashlib
from functools import lru_cache
from pathlib import Path
import re
import unicodedata

import cv2
import numpy as np

from core.page_identity import PageIdentity
from core.story_event.banner import carousel_identity, observe_banner


@lru_cache(maxsize=4)
def template(name):
    return cv2.imread(str(Path(__file__).with_name('assets')/(name+'.png')))


def clean(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text)))


def integer(text, suffix=True):
    text = clean(text).replace(',', '')
    if suffix:
        text = re.sub(r'(?:pt|积分|分)$', '', text, flags=re.I)
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', text):
        text = text.replace('.', '')
    return int(text) if text.isdigit() else None


class Page:
    def __init__(self, image, tokens):
        self.image = image
        self.tokens = []
        for raw in tokens or []:
            if raw.get('confidence', 0) < .8 or len(raw.get('bbox', [])) != 4:
                continue
            token = dict(raw, text=clean(raw['text']))
            x,y,r,b = token['bbox']
            if not 0 <= x < r <= 720 or not 0 <= y < b <= 1280:
                continue
            old = next((t for t in self.tokens if t['text'] == token['text'] and
                        abs(t['bbox'][0]+t['bbox'][2]-x-r) <= 8 and
                        abs(t['bbox'][1]+t['bbox'][3]-y-b) <= 8), None)
            if old is None:
                self.tokens.append(token)
            elif old['confidence'] < token['confidence']:
                self.tokens[self.tokens.index(old)] = token

    def rows(self, region=(0,0,720,1280)):
        x,y,r,b = region
        return [t for t in self.tokens if x <= t['bbox'][0] < t['bbox'][2] <= r
                and y <= t['bbox'][1] < t['bbox'][3] <= b]

    def has(self, text, region=(0,0,720,1280)):
        return any(clean(text) in t['text'] for t in self.rows(region))

    def find(self, words, region=(0,0,720,1280)):
        words = [words] if isinstance(words,str) else words
        choices = [t for t in self.rows(region) if t['text'] in {clean(w) for w in words}]
        return choices[0] if len(choices) == 1 else None

    def enabled(self, token):
        if token.get('enabled') is False:
            return False
        x,y,r,b = token['bbox']
        crop = self.image[max(0,y-10):min(1280,b+10),max(0,x-15):min(720,r+15)]
        hsv = cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
        bright = ((hsv[:,:,1]>70)&(hsv[:,:,2]>180)).mean()
        white = ((hsv[:,:,1]<65)&(hsv[:,:,2]>215)).mean()
        return bool(bright > .15 or white > .5 and crop.std() > 10)

    def button(self, name, words, region=(0,700,720,1280), **extra):
        token = self.find(words, region)
        if token is None or not self.enabled(token):
            return {}
        x,y,r,b = token['bbox']
        return {name:dict(point=[(x+r)//2,(y+b)//2], bbox=token['bbox'],
                          label=token['text'], **{'effect':'navigation', 'resource_kind':'free', **extra})}

    def tab(self,name,word,region):
        token=self.find(word,region)
        if token is None:return {}
        x,y,r,b=token['bbox']
        return {name:dict(point=[(x+r)//2,(y+b)//2],bbox=token['bbox'],
                         label=token['text'],effect='navigation',resource_kind='free',basis='native_footer_tab_caption')}

    def green(self, name, region, *, min_height=35, **extra):
        x,y,r,b = region
        hsv = cv2.cvtColor(self.image[y:b,x:r],cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv,np.array([25,95,175]),np.array([90,255,255]))
        mask = cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((7,7),np.uint8))
        contours,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours:
            a,c,w,h = cv2.boundingRect(contour)
            if w >= 130 and min_height <= h <= 140 and cv2.contourArea(contour) >= w*h*.45:
                boxes.append([x+a,y+c,x+a+w,y+c+h])
        if len(boxes) != 1:
            return {}
        a,c,d,e = boxes[0]
        return {name:dict(point=[(a+d)//2,(c+e)//2],bbox=boxes[0],
                          basis='named_page_primary_green',**{'effect':'navigation','resource_kind':'free',**extra})}

    def number(self, region, suffix=True):
        values = [integer(t['text'],suffix) for t in self.rows(region)]
        values = [v for v in values if v is not None]
        return values[0] if len(values) == 1 else None

    def tickets(self):
        rows = [re.fullmatch(r'([0-5])/5',t['text']) for t in self.rows((560,0,720,60))]
        values = [int(m[1]) for m in rows if m]
        return values[0] if len(values) == 1 else None

    def stock(self):
        before = self.number((380,720,495,800))
        after = self.number((505,720,620,800))
        return before, after

    def grid(self, *, recruitment=False):
        result = []
        markers = [t for t in self.rows((0,670,720,1010)) if any(s in t['text'] for s in ['队长','成员','招揽积分'])]
        for t in self.rows((20,710,700,1010)):
            value = integer(t['text'])
            if value is None or not 50 <= value <= 999999:
                continue
            if recruitment != ('积分' in t['text']):
                continue
            x,y,r,b = t['bbox'];cx=(x+r)//2
            labels = [m['text'] for m in markers if abs((m['bbox'][0]+m['bbox'][2])/2-cx)<65
                      and 40 < y-m['bbox'][1] < 170]
            if any('队长' in s for s in labels):
                continue
            if recruitment and any('成员' in s or '招揽积分' in s for s in labels):
                continue
            tag=self.image[max(0,y-145):max(1,y-95),max(0,cx-60):min(720,cx+65)]
            hsv=cv2.cvtColor(tag,cv2.COLOR_BGR2HSV)
            red=(((hsv[:,:,0]<12)|(hsv[:,:,0]>170))&(hsv[:,:,1]>150)&(hsv[:,:,2]>140)).mean()
            if red>.12:
                continue
            point = [cx,max(660,y-65)]
            px,py=point
            patch=self.image[max(0,py-70):min(1280,py+70),max(0,px-70):min(720,px+70)]
            yy,xx=np.indices(patch.shape[:2]);radius=((xx-patch.shape[1]/2)**2+(yy-patch.shape[0]/2)**2)**.5
            ring=(radius>=53)&(radius<=69)
            colors=cv2.cvtColor(patch,cv2.COLOR_BGR2HSV)
            selected=bool((((colors[:,:,0]>25)&(colors[:,:,0]<65)&(colors[:,:,1]>120)&(colors[:,:,2]>150))[ring]).mean()>.01)
            result.append(dict(value=value,point=point,bbox=[max(0,cx-55),y-125,min(720,cx+55),b],
                               captain=False,unavailable=False,selected=selected))
        return result


def observe(image, tokens, context=None):
    context = context or {}
    if not isinstance(image,np.ndarray) or image.shape[:2] != (1280,720):
        return dict(page='unknown',actions={},reason='portrait_720_required')
    page = Page(image,tokens)
    text = ''.join(t['text'] for t in page.tokens)
    result = dict(page='unknown', actions={}, reason='strongest_team_page_unknown',
                  tickets=page.tickets(), tokens=page.tokens, requires_context=True,
                  native_busy=page.find('连接中',(500,0,710,145)) is not None)

    def finish(name, reason, actions=None, **fields):
        return dict(result,page=name,reason=reason,actions=actions or {},**fields)

    home = page.tab('home','主页',(280,1200,430,1280))
    if not home:
        target=PageIdentity(image).find('主页',(290,1200,430,1280))
        if target:
            x,y,r,b=target['bbox'];home={'home':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free',basis='shared_home_caption_template')}
    cancel = page.button('cancel','取消',(0,700,350,1250))
    if any(s in text for s in ['账号在其他地方登录','账号已在其他设备','帐号在其他地方']):
        return finish('connection_conflict','strongest_team_account_conflict')
    if all(s in text for s in ['网络错误','重试']):
        return finish('st_network','native_network_error',page.button('retry_network','重试',(350,600,720,1150)))
    if (any(s in text for s in ['招揽赛','入场券']) and any(s in text for s in [
            '今日回复次数已达上限','今天的回复次数已达上限','已达到今日回复上限','今日可回复次数已用完'])):
        actions=page.button('close_refill_limit',('关闭','确定'),(200,600,650,1200))
        return finish('st_refill_limit','native_daily_refill_limit',actions,recovery_maxed_verified=True)
    if any(s in text for s in ['购买','充值','消耗宝石','消耗萝卜']):
        return finish('st_paid','paid_resource_not_allowed',cancel)
    if context.get('context_verified'):
        if page.find('上一页',(0,1130,200,1280)):
            tutorial_action=page.button('continue_notice',('继续','关闭'),(200,1130,520,1280))
            if not tutorial_action:
                footer=page.rows((290,1170,450,1260))
                partial=[t for t in footer if t['text']=='关' or t['text'].startswith('继')]
                forbidden=any(any(w in t['text'] for w in ['确定','确认','是否','购买','消耗','充值']) for t in footer)
                if len(partial)==1 and not forbidden and page.enabled(partial[0]):
                    x,y,r,b=partial[0]['bbox']
                    tutorial_action={'continue_notice':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',
                        resource_kind='free',basis='owned_paged_guide_partial_footer_caption')}
            if tutorial_action:return finish('st_notice','native_paged_tutorial_footer',tutorial_action)

    if (context.get('context_verified') and page.has('团队确认',(220,0,520,120))
            and page.has('确定要以此团队参加活动吗',(100,1020,650,1135))
            and page.has('队长',(0,450,150,550)) and page.has('成员',(0,650,150,740))):
        actions=dict(cancel)
        actions.update(page.button('confirm_team','确定',(380,1130,690,1250)))
        return finish('st_team_confirm','native_initial_team_confirmation',actions)
    if (context.get('context_verified') and page.has('队长适应性确认',(150,250,600,410))
            and page.has('适应性低于B',(70,580,670,720))
            and page.has('今后不再弹出该提示',(150,740,610,840))):
        entry=context.get('pending_entry') or {};operation=context.get('pending_operation') or {}
        actions={}
        if (entry.get('id') and operation.get('id')==entry['id']
                and operation.get('resource')==entry.get('kind') and not entry.get('progress_seen')):
            actions=page.button('confirm_adaptation',('参赛!','参赛'),(350,850,690,1000),
                effect='admission_continuation',resource_kind='event_ticket' if entry['kind']=='normal' else 'free_special',
                entry_id=entry['id'],parent_intent_id=operation['id'])
        return finish('st_adaptation_warning','native_captain_adaptation_warning',actions)
    entry=context.get('pending_entry') or {}
    if (context.get('context_verified') and entry.get('status')=='submitted'
            and entry.get('progress_seen')):
        outcomes=[t for t in page.rows((100,500,620,1000))
            if t['text'] in {'胜利','失败'} and t['confidence']>=.9
            and t['bbox'][2]-t['bbox'][0]>=200 and t['bbox'][3]-t['bbox'][1]>=80]
        if len(outcomes)==1:
            x,y,r,b=outcomes[0]['bbox']
            return finish('st_race','owned_result_presentation',
                {'skip_race':dict(point=[(x+r)//2,(y+b)//2],bbox=outcomes[0]['bbox'],
                    effect='progress',resource_kind='free',basis='owned_large_outcome_presentation')})
    update_title=template('score_update_title')
    if context.get('context_verified') and update_title is not None:
        _,score,_,location=cv2.minMaxLoc(cv2.matchTemplate(image[130:300,30:690],update_title,cv2.TM_CCOEFF_NORMED))
        if score>=.93:
            h,w=update_title.shape[:2]
            target=[30+location[0]+w//2,130+location[1]+h//2]
            return finish('st_grade_reward','native_team_score_reward',
                {'continue_grade':dict(point=target,effect='navigation',resource_kind='free',basis='named_score_update_title')},
                team_total=page.number((200,440,520,560)))

    # Match complete named dialogs before loading captions/background pages.
    if page.has('回复完毕') and page.has('已回复招揽赛入场券'):
        counts=[re.fullmatch(r'([0-5])/5',t['text']) for t in page.rows((300,630,470,750))]
        values=[int(m[1]) for m in counts if m]
        return finish('st_refill_done','native_ticket_refill_completed',
            page.button('close_refill_done','关闭',(200,750,520,930)),
            tickets=values[0] if len(values)==1 else None)
    if (page.has('招揽赛入场券回复') and page.has('迷你胡萝卜果冻',(120,500,460,650))
            and page.has('持有数',(120,550,250,650))):
        actions=page.button('close_refill_items','关闭',(200,850,520,1000))
        actions.update(page.button('select_jelly','使用',(530,520,690,660)))
        return finish('st_refill_items','native_existing_mini_jelly_selector',actions,
            item='mini_jelly',stock_before=page.number((250,570,400,615)))
    if (page.has('招揽赛入场券回复') and page.has('招揽赛入场券不足')
            and page.has('是否回复入场券')):
        actions=dict(cancel)
        actions.update(page.button('open_refill','确定',(350,750,690,930)))
        return finish('st_refill_prompt','native_insufficient_ticket_recovery_prompt',actions,
            recovery_needed_verified=True,tickets=0)
    if page.has('招揽赛入场券回复') and page.has('迷你胡萝卜果冻') and page.has('获得1枚入场券'):
        quantity_rows = [re.fullmatch(r'([1-5])/5',t['text']) for t in page.rows((250,580,450,700))]
        quantity = next((int(m[1]) for m in quantity_rows if m),None)
        before = page.number((320,720,455,800));after=page.number((515,720,630,800))
        actions = dict(cancel)
        actions.update(page.button('minimum_refill','最小',(20,580,160,700)))
        actions.update(page.button('maximum_refill','最大',(560,580,700,700)))
        plus=page.find('+',(450,580,580,700))
        if plus and quantity is not None and quantity<5:
            x,y,r,b=plus['bbox'];actions['increase_refill']=dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free')
        circles=cv2.HoughCircles(cv2.cvtColor(image[585:710,140:580],cv2.COLOR_BGR2GRAY),
            cv2.HOUGH_GRADIENT,1,100,param1=80,param2=22,minRadius=25,maxRadius=48)
        if circles is not None and quantity is not None:
            measured=[(float(x)+140,float(y)+585) for x,y,r in circles[0]
                      if 610<=float(y)+585<=680]
            for key,left,right,allowed in [('decrease_refill',150,280,quantity>1),('increase_refill',440,570,quantity<5)]:
                found=[(x,y) for x,y in measured if left<=x<=right]
                if len(found)==1 and allowed:
                    actions[key]=dict(point=[int(found[0][0]),int(found[0][1])],effect='navigation',
                                      resource_kind='free',basis='named_refill_measured_circle')
        if (quantity is not None and before is not None and after is not None
                and before-after == quantity and page.has('每天最多可回复5枚')):
            actions.update(page.green('confirm_refill',(360,840,710,990),
                effect='resource',resource_kind='mini_jelly',quantity=quantity,
                stock_before=before,stock_after=after))
        return finish('st_refill','native_mini_jelly_refill',actions,
                      quantity=quantity,stock_before=before,stock_after=after,item='mini_jelly')
    if page.has('是否使用闹钟进行重新挑战') and page.has('己方团队的优俊少女基础能力+'):
        before,after=page.stock()
        free_rows=[re.search(r'(?:免费剩余|免费.*剩余)([1-5])次',t['text']) for t in page.rows((250,690,650,840))]
        free=next((int(m[1]) for m in free_rows if m),None)
        if free is None:
            labels=page.rows((280,760,390,830));counts=page.rows((390,760,620,830))
            values=[int(m[1]) for t in counts if (m:=re.fullmatch(r'剩余([0-5])次',t['text']))
                and any(label['text']=='免费' and abs(sum(label['bbox'][1::2])-sum(t['bbox'][1::2]))<=24 for label in labels)]
            if len(values)==1:free=values[0]
        actions=dict(cancel)
        if before is not None and after is not None and before-after == 1:
            actions.update(page.green('confirm_retry',(360,840,710,990),effect='resource',
                resource_kind='existing_alarm',stock_before=before,stock_after=after,cost=1))
        elif type(free) is int and free>0 and before is not None and before==after:
            actions.update(page.green('confirm_retry',(360,840,710,990),effect='resource',
                resource_kind='free_retry',stock_before=before,stock_after=after,cost=0,free_remaining=free))
        return finish('st_retry','native_existing_alarm_retry',actions,stock_before=before,stock_after=after,free_remaining=free)
    if page.has('招揽结束确认') and page.has('是否确定结束招揽'):
        actions=dict(cancel);actions.update(page.button('finish_no_recruit','确定',(350,750,700,920)))
        return finish('st_no_recruit_confirm','native_no_recruit_confirmation',actions)
    if page.has('特别赛结束') and page.has('特别赛已结束'):
        actions=page.button('return_event','前往活动首页',(0,700,350,1000))
        actions.update(page.button('continue_special_done','前往招揽赛',(350,700,700,1000)))
        return finish('st_special_done','native_special_race_complete',actions)
    if page.has('招揽完毕') or page.has('已结束招揽') or page.has('招揽结束',(250,300,500,500)):
        actions=page.button('continue_recruit_done','前往招揽赛',(350,700,700,1000))
        actions.update(page.button('continue_recruit_done','继续',(230,700,520,1000)))
        actions.update(page.button('return_event','前往活动首页',(0,700,350,1000)))
        return finish('st_recruit_done','native_recruit_complete',actions,
                      recruited=page.has('招揽完毕'))
    if page.has('参赛确认',(200,0,520,130)):
        kind='special' if page.has('特别赛',(0,80,350,180)) else 'normal' if page.has('招揽赛',(0,80,350,180)) else None
        name='start_special' if kind=='special' else 'start_normal'
        actions=dict(cancel)
        costs=[re.search(r'消耗[:：]?([0-9]+)',t['text']) for t in page.rows((350,1100,700,1250))]
        known_costs=[int(m[1]) for m in costs if m]
        # The named ordinary confirmation has a fixed one-ticket contract.
        # A missed tiny HUD digit is metadata, never an invented zero or a
        # requirement to buy/refill. An explicit conflicting cost still blocks.
        allowed=(kind=='normal' and not any(n!=1 for n in known_costs)
                 or kind=='special' and not any(n!=0 for n in known_costs))
        if allowed:
            actions.update(page.green(name,(350,1130,710,1240),effect='admission',
                                      resource_kind='free_special' if kind=='special' else 'event_ticket'))
        counts=[re.fullmatch(r'([0-5])/5',t['text']) for t in page.rows((300,1070,445,1145))]
        count=next((int(m[1]) for m in counts if m),result['tickets'])
        if kind=='normal' and count==0 and 'start_normal' in actions:
            target=actions.pop('start_normal')
            actions['open_refill']=dict(target,effect='navigation',resource_kind='free')
        return finish('st_preview','native_entry_confirmation',actions,entry_kind=kind,tickets=count)
    if page.has('招揽确认',(200,0,520,130)) and page.has('是否确认招揽'):
        cost=page.number((210,1000,365,1080));owned=page.number((540,1000,700,1080))
        before=page.number((10,100,230,330));after=page.number((350,100,530,330))
        actions=dict(cancel)
        if cost is not None and owned is not None and cost<=owned and before is not None and after is not None and after>before:
            actions.update(page.button('confirm_recruit','确定',(350,1100,700,1250),
                                       effect='resource',resource_kind='event_points',cost=cost))
        return finish('st_recruit_confirm','native_recruit_confirmation',actions,
                      recruit_cost=cost,points=owned,team_before=before,team_after=after)
    if page.has('战术',(250,200,500,400)) and page.find('确定',(350,850,700,1000)):
        actions=page.button('confirm_tactics','确定',(350,850,700,1000))
        styles=[]
        ranks={'S':8,'A':7,'B':6,'C':5,'D':4,'E':3,'F':2,'G':1}
        for t in page.rows((0,700,720,850)):
            m=re.fullmatch(r'(领跑|跟前|居中|后追)([SABCDEFG])',t['text'])
            if not m:continue
            x,y,r,b=t['bbox'];crop=image[max(0,y-30):min(1280,b+30),max(0,x-25):min(720,r+25)]
            hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
            selected=float(((hsv[:,:,0]>25)&(hsv[:,:,0]<65)&(hsv[:,:,1]>150)&(hsv[:,:,2]>180)).mean())>.025
            key='style_'+m[1];actions[key]=dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free')
            styles.append(dict(action=key,grade=ranks[m[2]],selected=selected))
        return finish('st_tactics','native_captain_tactics',actions,styles=styles)
    if page.has('限定任务',(250,0,500,120)):
        daily=page.has('【日常】参与')
        tab='daily' if daily else 'period' if page.has('【期间限定任务】') else None
        actions=page.button('close_missions','关闭',(0,1100,350,1250))
        actions.update(page.button('tab_daily','日常',(0,80,360,170)))
        actions.update(page.button('tab_period','期间限定',(360,80,720,170)))
        claimable=len([t for t in page.rows((500,150,700,1080)) if t['text']=='领取'])
        claimed=len([t for t in page.rows((500,150,700,1080)) if t['text']=='已领取'])
        if claimable:
            actions.update(page.button('claim_all','一键领取',(350,1100,700,1250),
                                       effect='claim',resource_kind='earned_rewards'))
        return finish('st_missions','native_event_missions',actions,mission_tab=tab,
                      claimable=claimable,daily_ticket_claims=claimed if daily else None)
    if (page.has('领取成功',(200,0,520,150))
            or (context.get('pending_operation') or {}).get('resource')=='claim') and page.has('已获得以上奖励'):
        return finish('st_claimed','native_rewards_claimed',page.button('close_claimed','关闭',(200,1100,520,1250)))

    if page.has('请选择要更换的优俊少女') or page.has('请选择要招揽的优俊少女'):
        recruit=page.has('请选择要招揽的优俊少女')
        cards=page.grid(recruitment=recruit)
        actions=page.button('confirm_grid','确定',(200,1100,520,1250))
        actions.update(page.button('cancel_grid','返回',(0,1100,180,1250)))
        for index,card in enumerate(cards):
            actions['select_card_'+str(index)]=dict(point=card['point'],effect='navigation',resource_kind='free',card=card)
        return finish('st_recruit_select' if recruit else 'st_replace_select','native_scout_grid',actions,
                      cards=cards,points=page.number((540,1030,700,1110)),
                      team_after=page.number((350,100,530,200)))
    if page.has('选择队长',(0,0,250,80)):
        cards=page.grid()
        actions=page.button('confirm_captain','确定',(200,1000,520,1140))
        for index,card in enumerate(cards):
            actions['captain_'+str(index)]=dict(point=card['point'],effect='navigation',resource_kind='free',card=card)
        return finish('st_captain','native_own_captain_selection',actions,cards=cards)
    if page.has('招揽选择',(0,0,250,80)):
        actions=page.button('no_recruit',('不招揽','不招揽?'),(0,1100,350,1280))
        actions.update(page.button('request_recruit','招揽',(350,1100,700,1280)))
        for key,title,prompt in [('open_recruit','招揽的优俊少女','请从对方团队中选择'),
                                 ('open_replace','更换的优俊少女','请从己方团队中选择')]:
            marker=next((t for t in page.tokens if title in t['text']),None)
            if marker and page.has(prompt):
                y=marker['bbox'][3]
                crop=image[y:min(1020,y+260),80:285]
                hsv=cv2.cvtColor(crop,cv2.COLOR_BGR2HSV)
                mask=cv2.inRange(hsv,np.array([25,100,150]),np.array([90,255,255]))
                n,_,stats,centers=cv2.connectedComponentsWithStats(mask)
                boxes=[i for i in range(1,n) if 15<=stats[i,2]<=65 and 15<=stats[i,3]<=65 and .2<stats[i,4]/(stats[i,2]*stats[i,3])<.8]
                if len(boxes)==1:
                    cx,cy=centers[boxes[0]];actions[key]=dict(point=[80+int(cx),y+int(cy)],effect='navigation',resource_kind='free',basis='named_slot_measured_plus')
        return finish('st_scout','native_recruit_selection',actions,
            recruit_cost=page.number((210,1000,365,1100)),points=page.number((540,1000,700,1100)),
            team_before=page.number((10,100,230,200)),team_after=page.number((350,100,530,200)),
            recruit_selected=not page.has('请从对方团队中选择'),replace_selected=not page.has('请从己方团队中选择'))
    if page.has('选择对战对手',(0,0,250,90)):
        special=page.has('特别赛',(0,150,350,400))
        ratings=[]
        for t in page.rows((40,200,250,1000)):
            number=integer(t['text'],False)
            if number is not None and 10000<=number<=999999:
                x,y,r,b=t['bbox'];ratings.append(dict(rating=number,point=[(x+r)//2,y-80]))
        actions=dict(home)
        actions.update(page.button('back_event','返回',(0,1000,180,1150)))
        for index,card in enumerate(ratings):
            actions['opponent_'+str(index)]=dict(point=card['point'],effect='navigation',resource_kind='free',rating=card['rating'])
        return finish('st_opponents','native_special_opponent' if special else 'native_normal_opponents',actions,
                      entry_kind='special' if special else 'normal',opponents=ratings)
    if page.has('目标!最强团队',(0,0,350,80)) or page.has('目标！最强团队',(0,0,350,80)):
        actions=dict(home)
        actions.update(page.button('open_captain','选择队长',(200,850,550,1040)))
        actions.update(page.button('open_opponents',('招揽赛','特别赛'),(180,850,550,1030)))
        missions=[t for t in page.rows((560,1020,710,1140)) if t['text'].startswith('活动任')]
        if len(missions)==1:
            x,y,r,b=missions[0]['bbox'];actions['open_missions']=dict(point=[(x+r)//2,(y+b)//2-30],effect='navigation',resource_kind='free',basis='native_event_mission_icon_caption')
        closed=any(s in text for s in ['活动已结束','尚未开放','举办期间已结束'])
        return finish('st_closed' if closed else 'st_hub','named_strongest_team_hub',actions,
                      event_verified=True,requires_context=False)
    if page.has('招揽赛',(0,0,520,650)) or page.has('特别赛',(0,0,520,650)):
        actions=page.button('next_result',('下一页','下一步'),(250,1000,700,1280))
        actions.update(page.button('open_retry','重新挑战',(0,1000,350,1280)))
        outcome='win' if page.has('胜利',(0,150,500,450)) else 'loss' if page.has('失败',(0,150,500,450)) else None
        if outcome:
            if not actions:
                actions.update(page.green('next_result',(180,1180,560,1250),min_height=12,effect='progress'))
            return finish('st_result','native_race_result',actions,outcome=outcome)
        actions=page.button('view_result','查看结果',(150,1100,350,1250))
        actions.update(page.button('continue_race','前往赛事',(350,1100,650,1250)))
        if actions:
            return finish('st_race_detail','owned_entered_race_detail',actions)

    if PageIdentity(image).home():
        raw=cv2.imencode('.png',image)[1].tobytes()
        banner=observe_banner(raw)
        actions=page.tab('open_races','赛事',(430,1200,580,1280))
        if not actions:
            target=PageIdentity(image).find('赛事',(430,1200,580,1280))
            if target:
                x,y,r,b=target['bbox'];actions['open_races']=dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free',basis='shared_races_caption_template')
        if banner.get('banner_excluded')=='champions_team':
            actions['open_event']=dict(point=[550,245],effect='navigation',resource_kind='free',basis='verified_strongest_team_banner_art')
        dots=carousel_identity(image)
        if dots:
            actions['next_banner']=dict(swipe=[635,245,455,245,350],effect='navigation',resource_kind='free',basis='measured_home_carousel')
        return finish('home','shared_home_identity',actions,requires_context=False,carousel=dots)
    if page.has('赛事活动',(180,0,560,900)):
        cards=[t for t in page.rows((20,150,700,1170)) if '最强团队' in t['text'] or '最强队伍' in t['text']]
        captions=[t for t in cards if t['text'] in {'目标!最强团队','目标!最强队伍'}]
        if captions:cards=captions
        actions={}
        if len(cards)==1:
            x,y,r,b=cards[0]['bbox'];actions['open_event']=dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free',basis='named_strongest_team_event_card')
        return finish('events','named_event_menu',actions,requires_context=False)
    if page.has('赛事',(0,0,300,150)):
        actions=page.button('open_events','赛事活动',(200,650,700,1000))
        if actions:return finish('races','native_races_menu',actions,requires_context=False)

    from core.limited_legend.observer import observe as navigation_observe
    navigation=navigation_observe(cv2.imencode('.png',image)[1].tobytes(),image,page.tokens)
    if navigation.get('page')=='races' and 'open_events' in navigation.get('actions',{}):
        candidate=dict(navigation['actions']['open_events'],effect='navigation',resource_kind='free')
        return finish('races','shared_native_races_navigation',{'open_events':candidate},requires_context=False)

    if context.get('context_verified'):
        # Shared tutorials and owned presentation footers are neutral only.
        from core.passive_tutorial import detect
        tutorial=detect(image,page.tokens)
        if tutorial:
            return finish('st_notice','shared_native_tutorial',
                {'continue_notice':dict(point=tutorial['point'],effect='navigation',resource_kind='free')})
        neutral=page.button('continue_notice',('关闭','继续','下一步'),(200,700,650,1280))
        if neutral and not any(s in text for s in ['消耗','回复','重新挑战','是否确认招揽']):
            return finish('st_notice','owned_neutral_presentation',neutral)
        from core.passive_skip import detect as detect_skip
        skip=detect_skip(image,page.tokens)
        if skip and context.get('pending_entry'):
            return finish('st_race','owned_native_skip',
                {'skip_race':dict(point=skip['point'],effect='progress',resource_kind='free')})
    if not page.tokens or any(s in text for s in ['加载中','连接中','正在下载']):
        return finish('loading','native_loading')
    return result
