"""Owned A/B main-race viewing, independent of ticket entries and likes."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import re

import cv2
import numpy as np

GROUPS=('A','B')
WATCH_PAGES={'loh_watch_confirm','loh_watch_preview','loh_watch_settings','loh_watch_lineup',
    'loh_watch_race','loh_watch_intro','loh_watch_reward','loh_watch_result','loh_watch_list',
    'loh_watch_hub','loh_watch_help'}


def requirement(data):
    value=data.get('final_watch_requirement') or {}
    if (value.get('enabled') is True and value.get('groups')==['A','B'] and value.get('reward_each')==30):
        return value
    return None


def complete(data):
    req=requirement(data)
    records=data.get('final_watch_groups') or {}
    return bool(req and all(records.get(g,{}).get('verified') is True for g in GROUPS))


def reward_total(data):
    return sum(30 for g in GROUPS if (data.get('final_watch_groups') or {}).get(g,{}).get('reward_verified'))


@lru_cache(maxsize=8)
def _asset(name):
    return cv2.imread(str(Path(__file__).with_name('assets')/(name+'.png')))


def _match(image,name,box,threshold=.94,mask=None):
    x,y,r,b=box;source=_asset(name);roi=image[y:b,x:r]
    if source is None or roi.shape[0]<source.shape[0] or roi.shape[1]<source.shape[1]:return None
    scores=cv2.matchTemplate(roi,source,cv2.TM_CCOEFF_NORMED,mask=mask)
    scores=np.nan_to_num(scores,nan=0,posinf=0,neginf=0)
    _,score,_,point=cv2.minMaxLoc(scores)
    if score<threshold:return None
    a,c=point
    return dict(point=[x+a+source.shape[1]//2,y+c+source.shape[0]//2],score=float(score))


def _skip(image):
    source=_asset('watch-fast-forward')
    if source is None:return None
    # The video pointer covers the upper-right glyph. Retain both lower
    # arrows and the stop bar; the bounded native control remains measured.
    mask=np.full(source.shape[:2],255,np.uint8);mask[:26,24:]=0
    found=_match(image,'watch-fast-forward',(510,1160,610,1270),.95,mask)
    if found and 535<=found['point'][0]<=590 and 1190<=found['point'][1]<=1250:
        return dict(point=found['point'],effect='progress',resource_kind='free',basis='native_owned_watch_fast_forward')
    return None


def observe_fast_skip(image,data):
    """Read only the undimmed native skip for this already-started viewing."""
    pending=data.get('pending_watch') or {}
    if (not requirement(data) or not data.get('loh_context_verified')
            or data.get('pending') or data.get('pending_like')
            or pending.get('group') not in GROUPS or pending.get('key')!=pending.get('group')
            or pending.get('input_status') not in {'submitted','observed'}
            or not pending.get('progress_seen')
            or pending.get('start_dispatch',{}).get('status')!='submitted'
            or image.shape[:2]!=(1280,720)):
        return None
    skip=_skip(image)
    if skip is None:return None
    source=_asset('watch-fast-forward');h,w=source.shape[:2];x,y=skip['point']
    crop=image[y-h//2:y-h//2+h,x-w//2:x-w//2+w]
    white=source.min(axis=2)>210
    # Correlation alone also matches a dimmed background beneath a modal.
    # A live native white control must retain its measured brightness.
    if crop.shape!=source.shape or (crop[white].min(axis=1)>190).mean()<.65:
        return None
    skip.update(watch_group=pending['group'],watch_key=pending['key'],watch_race=pending.get('race'))
    return dict(page='loh_watch_race',reason='owned_main_race_skip',actions={'watch_skip':skip},
        requires_loh_context=True,watch_race=pending.get('race'))


def observe(image,tokens,data,base):
    req=requirement(data)
    if not req or data.get('pending') or data.get('pending_like') or image.shape[:2]!=(1280,720):return None
    from .observer import _Page,_tokens
    page=_Page(image,_tokens(tokens,720,1280));page.dialog=None
    text=''.join(t['text'] for t in page.tokens)
    pending=data.get('pending_watch') or {}
    def finish(name,reason,actions=None,**fields):
        return dict(page=name,reason=reason,actions=actions or {},requires_loh_context=True,**fields)
    def action(key,label,box,effect='navigation',**extra):
        return page.action(key,label,box,effect=effect,resource_kind='free',**extra)
    def owned(actions,group=None,race=None):
        for value in actions.values():
            value.update(watch_group=group or pending.get('group'),watch_race=race or pending.get('race'))
            if value.get('watch_group') in GROUPS:value['watch_key']=value['watch_group']
        return actions
    def home():
        token=page.find('主页',(.4,.95,.6,1))
        if not token:return {}
        x,y,r,b=token['bbox']
        return {'watch_home':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free',basis='native_home_footer_caption')}
    home_caption=page.find('主页',(.4,.95,.6,1))
    selected=cv2.cvtColor(image[1200:1280,280:440],cv2.COLOR_BGR2HSV)
    home_blue=((selected[:,:,0]>90)&(selected[:,:,0]<125)&(selected[:,:,1]>100)&(selected[:,:,2]>130)).mean()>.12
    if (home_caption and home_blue and page.find('菜单',(.8,0,1,.1))
            and page.has('tp',(0,0,.55,.1)) and page.has('rp',(.5,0,.9,.1))):
        race=page.find('赛事',(.6,.95,.85,1));actions={}
        if race:
            x,y,r,b=race['bbox'];actions['open_races']=dict(point=[(x+r)//2,(y+b)//2],effect='navigation',resource_kind='free')
        return dict(page='home',reason='native_selected_home_after_watch',actions=actions,requires_loh_context=False)
    prompt=page.find('比赛播放确认',(0,.28,1,.36))
    group_match=re.search(r'即将播放([ab])组',text)
    race_match=re.search(r'主要赛事\(第(\d+)场比赛\)',text)
    if prompt and page.has('英杰集结战特别赛事',(0,.4,1,.48)) and group_match and '主要赛事' in text:
        group=group_match[1].upper();race=int(race_match[1]) if race_match else None;actions={}
        actions.update(action('watch_confirm','确定',(.5,.61,1,.70),'watch'))
        actions.update(action('watch_cancel','取消',(0,.61,.5,.70)))
        return finish('loh_watch_confirm','named_main_race_watch_confirmation',owned(actions,group,race),watch_group=group,watch_race=race)
    if ('观看主要赛事' in text and '将播放a、b两组的主要赛事' in text
            and '以及我方团队正在参赛的比赛' in text and '观看主要赛事可获得奖励' in text):
        return finish('loh_watch_help','named_main_race_watch_help',owned(action('watch_close_help','关闭',(.2,.88,.72,1))))
    # Auto-awarded carrots belong to the already-started viewing intent. No
    # claim/purchase control is introduced: only its actual Close is exposed.
    title=page.find(('领取奖励','领取美助'),(.2,.28,.8,.37)) or _match(image,'watch-reward-title',(180,350,550,465),.94)
    amounts=[t for t in page.rows((.2,.42,.5,.51)) if t['text']=='30' and t['confidence']>=.85]
    gem=page.find('宝石',(.18,.39,.45,.45)) or _match(image,'watch-carrot-icon',(30,490,160,650),.90)
    if title and gem and len(amounts)==1 and not any(w in text for w in ('购买','充值','消耗萝卜','消耗宝石')):
        close=action('watch_close_reward','关闭',(.25,.61,.75,.70))
        return finish('loh_watch_reward','owned_main_race_carrot_reward',owned(close),reward_amount=30,
            named_watch_reward=('观看主要赛事' in text),watch_group=pending.get('group'),watch_race=pending.get('race'))
    if base.get('page')=='loh_spectator_list':
        group=base.get('like_group');race=base.get('like_race');actions={}
        for g in GROUPS:
            actions.update(action('watch_'+g.lower(),g+'组',(0,.35,1,.43)))
        actions.update(home())
        replay=page.find('回放',(.2,.79,.8,.89))
        ranked=sum(bool(re.search(r'第[123]名',t['text'])) for t in page.rows((0,.48,.28,.87)))>=2
        actions.update(action('watch_open_current',('回放','观看赛事','观看'),(.2,.79,.8,.89),'watch'))
        return finish('loh_watch_list','native_main_race_list',owned(actions,group,race),watch_group=group,watch_race=race,
            already_watched=bool(replay and ranked),ranked_results=ranked)
    if (page.has('英杰集结战',(0,0,.5,.1)) and '特别赛事进行中' in text
            and page.find('比赛一览',(0,.62,1,.83))):
        actions=action('watch_open_list','比赛一览',(0,.62,1,.83))
        actions.update(home())
        return finish('loh_watch_hub','native_final_main_races_hub',owned(actions))
    intros=[re.search(r'([ab])组第(\d+)场比赛',t['text']) for t in page.tokens]
    intros=[m for m in intros if m]
    if len(intros)==1 and '主要赛事' in text and not page.find('参赛!',(0,.85,1,1)):
        caption=page.find('点击',(.2,.78,.8,.89));advance={}
        if caption:
            x,y,r,b=caption['bbox']
            advance={'watch_continue_intro':dict(point=[(x+r)//2,(y+b)//2],effect='navigation',
                resource_kind='free',basis='named_watch_intro_click_caption')}
        return finish('loh_watch_intro','native_main_race_intro',
            owned(advance,intros[0][1].upper()),
            watch_group=intros[0][1].upper(),watch_race=int(intros[0][2]))
    result_titles=[re.fullmatch(r'特别赛事第(\d+)场比赛',t['text']) for t in page.rows((0,.37,1,.46))]
    result_titles=[int(m[1]) for m in result_titles if m]
    if len(result_titles)==1 and page.find('回放',(.75,.32,1,.42)):
        return finish('loh_watch_result','owned_main_race_result',
            owned(action('watch_next','下一页',(.2,.85,.8,.98),'progress')),watch_race=result_titles[0])
    titles=[re.fullmatch(r'特别赛事第(\d+)场比赛',t['text']) for t in page.rows((0,0,1,.12))]
    titles=[int(m[1]) for m in titles if m]
    race=titles[0] if len(titles)==1 else None
    if race is not None:
        preview=action('watch_open_race','前往赛事',(.2,.86,.75,.98),'progress')
        if preview:
            following_B=(pending.get('group')=='A' and pending.get('next_dispatch',{}).get('status')=='submitted'
                and _group(data,'A').get('verified'))
            group='B' if following_B else pending.get('group')
            return finish('loh_watch_preview','owned_main_race_preview',owned(dict(preview,**action('watch_back','返回',(0,.85,.3,1))),group),watch_group=group,watch_race=race)
        lineup=action('watch_start','参赛!',(.2,.85,.8,.97),'progress')
        if lineup and sum('人气第' in t['text'] for t in page.tokens)>=2:
            return finish('loh_watch_lineup','owned_main_race_lineup',owned(lineup),watch_race=race)
        next_result=action('watch_next','下一页',(.2,.85,.8,.98),'progress')
        if next_result and page.find('回放',(.75,.32,1,.42)):
            return finish('loh_watch_result','owned_main_race_result',owned(next_result),watch_race=race)
    if pending and prompt and not page.has('英杰集结战特别赛事',(0,.4,1,.48)):
        return finish('loh_watch_settings','owned_watch_playback_settings',
            owned(action('watch_play','确定',(.5,.60,1,.71),'progress')))
    if pending and pending.get('input_status') in {'submitted','uncertain','observed'}:
        skip=_skip(image)
        if skip:
            return finish('loh_watch_race','owned_main_race_skip',owned({'watch_skip':skip}),watch_race=pending.get('race'))
        # Native camera/menu captions establish a watch presentation while its
        # measured fast-forward control is still appearing/disappearing.
        if page.find('拍摄',(.55,.9,.75,1)) or page.has('1号机位',(0,.9,.35,1)):
            return finish('loh_watch_race','owned_main_race_presentation',watch_race=pending.get('race'))
    return None


def _group(data,group):
    return (data.get('final_watch_groups') or {}).get(group,{})


def _new_pending(group,race,status,**extra):
    return dict(group=group,race=race,key=group,input_status=status,progress_seen=False,**extra)


def observe_state(data,observed):
    req=requirement(data)
    if not req or not data.get('loh_context_verified') or data.get('pending') or data.get('pending_like'):return
    page=observed.get('page');pending=data.get('pending_watch');group=observed.get('watch_group');race=observed.get('watch_race')
    # A completed A result advances natively to B, as demonstrated. The
    # durable Next ACK and exact same main-race title bind this handoff.
    if (pending and pending.get('next_dispatch',{}).get('status')=='submitted'
            and _group(data,pending['group']).get('verified')):
        if pending['group']=='A' and page in {'loh_watch_intro','loh_watch_preview'} and group in (None,'B'):
            data['records'].append(dict(kind='final_watch_finished',operation=deepcopy(pending)))
            data['pending_watch']=pending=_new_pending('B',race or pending.get('race'),'submitted',basis='verified_A_result_next_to_B')
        elif pending['group']=='B' and page in {'loh_watch_list','loh_watch_hub','home'}:
            data['records'].append(dict(kind='final_watch_finished',operation=deepcopy(pending)))
            data['pending_watch']=pending=None
    # A public Replay button proves neither that this account watched nor
    # that its carrots arrived. Only an already-bound reward may settle a
    # lost neutral Next receipt when the real result list/Home appears.
    if (pending and _group(data,pending['group']).get('reward_verified')
            and ((page=='loh_watch_list' and group==pending['group'])
                 or page in {'home','races','events'} and complete(data)
                 or page in {'loh_watch_preview','loh_watch_intro','loh_watch_hub'} and complete(data)
                    and pending.get('next_dispatch',{}).get('status')=='submitted')):
        data['records'].append(dict(kind='final_watch_return_verified',operation=deepcopy(pending)))
        data['pending_watch']=pending=None
    if pending and page=='loh_watch_preview' and pending['input_status']=='uncertain':
        pending['input_status']='observed'
    if pending and page in {'loh_watch_lineup','loh_watch_race','loh_watch_result'}:
        pending['progress_seen']=True
        if page=='loh_watch_race':pending['race_seen']=True
        if pending['input_status']=='uncertain':pending['input_status']='observed'
    if (page=='loh_watch_reward' and pending and pending.get('progress_seen')
            and (pending.get('start_dispatch') or pending.get('race_seen'))
            and pending['group'] in GROUPS and observed.get('reward_amount')==30):
        g=pending['group'];prior=_group(data,g)
        if not prior.get('reward_verified'):
            evidence=dict(verified=True,race=pending.get('race'),reward_verified=True,reward_amount=30,
                basis='native_main_race_reward_popup',source_frame_sha256=observed.get('frame_sha256'))
            data.setdefault('final_watch_groups',{})[g]=evidence
            data['records'].append(dict(kind='final_watch_reward_verified',group=g,evidence=deepcopy(evidence)))
        pending['reward_seen']=True


def authorize(action,observed,data):
    if not action.startswith('watch_'):return None
    req=requirement(data);candidate=observed.get('actions',{}).get(action)
    def no(reason):return dict(ok=False,reason=reason,consuming=False)
    if not req or not data.get('loh_context_verified') or data.get('pending') or data.get('pending_like'):
        return no('loh_final_watch_owner_unverified')
    if not candidate or candidate.get('resource_kind')!='free' or candidate.get('effect') in {'purchase','admission'}:
        return no('loh_final_watch_action_unavailable')
    pending=data.get('pending_watch')
    if action in {'watch_confirm','watch_open_current'}:
        key=candidate.get('watch_key')
        reserved=bool(pending and pending.get('key')==key and pending.get('input_status')=='reserved'
            and pending.get('dispatch_id')==(data.get('last_action')or{}).get('dispatch_id'))
        if candidate.get('watch_group') not in GROUPS or _group(data,candidate['watch_group']).get('verified'):
            return no('loh_final_watch_group_already_verified')
        if pending and not reserved:return no('loh_final_watch_submission_unresolved')
    elif action in {'watch_open_race','watch_play','watch_start','watch_skip','watch_close_reward','watch_next'}:
        if not pending:
            return no('loh_final_watch_original_view_missing')
        if candidate.get('watch_key')!=pending.get('key'):return no('loh_final_watch_identity_changed')
        if action=='watch_close_reward' and not _group(data,pending['group']).get('reward_verified'):
            return no('loh_final_watch_reward_unverified')
        if action=='watch_next' and not _group(data,pending['group']).get('verified'):
            return no('loh_final_watch_result_unverified')
    return dict(ok=True,reason='loh_owned_final_viewing',consuming=False)


def reserve(data,action,observed,reserved):
    if not action.startswith('watch_'):return
    candidate=observed['actions'][action]
    reserved.update(watch_key=candidate.get('watch_key'),watch_group=candidate.get('watch_group'),watch_race=candidate.get('watch_race'))
    if action in {'watch_confirm','watch_open_current'}:
        data['pending_watch']=_new_pending(candidate['watch_group'],candidate['watch_race'],'reserved',dispatch_id=reserved['dispatch_id'])
    pending=data.get('pending_watch')
    field={'watch_open_race':'open_dispatch','watch_play':'play_dispatch','watch_start':'start_dispatch','watch_skip':'skip_dispatch','watch_next':'next_dispatch'}.get(action)
    if pending and field:pending[field]=dict(dispatch_id=reserved['dispatch_id'],status='reserved')


def receipt(data,action,zero,performed,uncertain,outcome):
    key=action.get('action_id','')
    if not key.startswith('watch_'):return
    pending=data.get('pending_watch')
    if not pending:return
    full=(performed and not uncertain and outcome.get('execution_status')=='executed' and type(outcome.get('input_attempts')) is int
          and type(outcome.get('acknowledged_inputs')) is int
          and outcome['input_attempts']==outcome['acknowledged_inputs'])
    if key in {'watch_confirm','watch_open_current'} and pending.get('dispatch_id')==action.get('dispatch_id'):
        if zero:
            data['records'].append(dict(kind='final_watch_zero_input_release',operation=deepcopy(pending)))
            data['pending_watch']=None
        else:pending['input_status']='submitted' if full else 'uncertain'
    field={'watch_open_race':'open_dispatch','watch_play':'play_dispatch','watch_start':'start_dispatch','watch_skip':'skip_dispatch','watch_next':'next_dispatch'}.get(key)
    if field and pending.get(field,{}).get('dispatch_id')==action.get('dispatch_id'):
        if zero:pending.pop(field,None)
        else:pending[field]['status']='submitted' if full else 'uncertain'


def choose(observed,data):
    req=requirement(data)
    if not req:return None
    page=observed.get('page');actions=observed.get('actions',{});pending=data.get('pending_watch')
    def finish(status,reason):return dict(kind='finish',status=status,reason=reason,action_id=None,consuming=False)
    def wait(reason):return dict(kind='wait',reason=reason,action_id=None,consuming=False)
    def act(name):
        if name not in actions:return wait('loh_final_watch_control_rendering:'+name)
        verdict=authorize(name,observed,data)
        return dict(kind='action',action_id=name,reason=verdict['reason'],consuming=False) if verdict['ok'] else finish('failed',verdict['reason'])
    if data.get('pending') or data.get('pending_like'):
        if page in WATCH_PAGES:return finish('failed','loh_original_submission_preserved')
        return None
    if page=='home':
        if complete(data) and not pending:
            return finish('done' if reward_total(data) else 'skipped','loh_final_main_races_home_verified')
        if 'open_races' in actions:
            return dict(kind='action',action_id='open_races',reason='loh_final_watch_original_navigation',consuming=False)
        return wait('loh_final_watch_original_home_navigation_loading')
    if page=='events':
        if complete(data) and not pending:
            return dict(kind='action',action_id='back_loh',reason='loh_final_watch_return_home',consuming=False) if 'back_loh' in actions else wait('loh_final_watch_home_caption_loading')
        if 'open_loh' in actions:
            return dict(kind='action',action_id='open_loh',reason='loh_final_watch_named_entry',consuming=False)
        return None
    if page=='loh_watch_confirm':
        g=observed.get('watch_group')
        if _group(data,g).get('verified') or complete(data):
            return act('watch_cancel')
        if pending:return wait('loh_final_watch_confirmation_submitted')
        return act('watch_confirm')
    if page=='loh_watch_help':return act('watch_close_help')
    if page=='loh_watch_intro':
        return act('watch_continue_intro') if 'watch_continue_intro' in actions else wait('loh_final_watch_intro')
    if page=='loh_watch_preview':
        if not pending and complete(data):return act('watch_back')
        if not pending:return finish('failed','loh_final_watch_preview_owner_unverified')
        if pending.get('open_dispatch'):return wait('loh_final_watch_preview_submitted')
        return act('watch_open_race')
    if page=='loh_watch_settings':
        if not pending:return finish('failed','loh_final_watch_playback_owner_unverified')
        if pending.get('play_dispatch'):return wait('loh_final_watch_settings_submitted')
        return act('watch_play')
    if page=='loh_watch_lineup':
        if not pending:return finish('failed','loh_final_watch_lineup_owner_unverified')
        if pending.get('start_dispatch'):return wait('loh_final_watch_lineup_submitted')
        return act('watch_start')
    if page=='loh_watch_race':
        if not pending:return finish('failed','loh_final_watch_race_owner_unverified')
        return act('watch_skip')
    if page=='loh_watch_reward':return act('watch_close_reward')
    if page=='loh_watch_result':
        if not pending:return finish('failed','loh_final_watch_result_owner_unverified')
        if pending.get('next_dispatch'):return wait('loh_final_watch_result_next_submitted')
        if not _group(data,pending['group']).get('reward_verified'):
            return wait('loh_final_watch_reward_rendering')
        return act('watch_next')
    if page=='loh_watch_list':
        if complete(data) and not pending:return act('watch_home')
        if pending:return finish('failed','loh_final_watch_submission_unresolved_at_list')
        missing=next(g for g in GROUPS if not _group(data,g).get('verified'))
        if observed.get('watch_group')!=missing:return act('watch_'+missing.lower())
        return act('watch_open_current')
    if page=='loh_watch_hub':return act('watch_home' if complete(data) and not pending else 'watch_open_list')
    return None
