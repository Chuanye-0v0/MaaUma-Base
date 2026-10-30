"""Video-observed special-race support, separate from ticket admissions."""
from copy import deepcopy
import re

import cv2

PAGES = {'loh_spectator_hub', 'loh_spectator_list', 'loh_spectator_reward'}
ACTIONS = {
    'loh_spectator_hub': ('spectator_open', 'back_loh'),
    'loh_spectator_list': ('spectator_vote', 'spectator_a', 'spectator_b', 'back_loh'),
    'loh_spectator_reward': ('spectator_close_reward',),
}


def complete(data):
    groups = data.get('spectator_groups', {})
    return all(groups.get(g, {}).get('verified') is True for g in ('A', 'B'))


def observe(page, result):
    text = ''.join(t['text'] for t in page.tokens)
    if any(w in text for w in ('购买', '消耗宝石', '消耗萝卜', '充值')):
        return None
    def home():
        token = page.find('主页', (.4, .95, .6, 1))
        if token is None:
            return {}
        x,y,r,b = token['bbox']
        return {'back_loh': dict(point=[(x+r)//2,(y+b)//2], bbox=token['bbox'],
                effect='navigation', resource_kind='free', basis='native_home_tab_caption')}
    reward_groups = [g for g in ('A', 'B') if '通过在'+g.lower()+'组点赞' in text and '获得了以上奖励' in text]
    if len(reward_groups) == 1 and page.has('领取奖励', (0, .25, 1, .4)):
        return result('loh_spectator_reward', 'named_spectator_like_reward',
            page.action('spectator_close_reward', '关闭', (.2, .55, .8, .75),
                        effect='navigation', resource_kind='free'),
            like_group=reward_groups[0], requires_loh_context=True)
    if ('两组的' in text and '奖励各可获得1次' in text
            and '每场比赛可为1位优俊少女' in text and '点赞' in text):
        return result('notice', 'named_spectator_tutorial',
            page.action('close_notice', '关闭', (.2, .88, .75, 1),
                        effect='navigation', resource_kind='free'), requires_loh_context=True)
    if (page.has('英杰集结战', (0, 0, 1, .1)) and '特别赛事' in text
            and '可进行点赞' in text and page.find('比赛一览', (0, .6, 1, .84))):
        actions = page.action('spectator_open', '比赛一览', (0, .6, 1, .84),
                              effect='navigation', resource_kind='free')
        actions.update(home())
        return result('loh_spectator_hub', 'native_like_period_hub', actions)
    if not (page.has('赛事一览', (0, 0, .5, .06)) and page.has('英杰集结战特别赛事', (0, .04, 1, .13))):
        return None
    groups = {g: page.find(g+'组', (0, .35, 1, .43)) for g in ('A', 'B')}
    selected = []
    for g, token in groups.items():
        if token is None:
            continue
        x,y,r,b = token['bbox']
        roi = page.image[max(0,y-5):b+5,max(0,x-12):min(page.width,r+12)]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        if ((hsv[:,:,0]>25)&(hsv[:,:,0]<95)&(hsv[:,:,1]>90)&(hsv[:,:,2]>170)).mean() > .3:
            selected.append(g)
    races = [re.fullmatch(r'第(\d+)场比赛', t['text']) for t in page.rows((0, .4, 1, .48))]
    races = [int(m[1]) for m in races if m]
    group = selected[0] if len(selected) == 1 else None
    race = races[0] if len(races) == 1 else None
    actions = {}
    for g in ('A', 'B'):
        actions.update(page.action('spectator_'+g.lower(), g+'组', (0, .35, 1, .43),
                                   effect='navigation', resource_kind='free'))
    actions.update(home())
    voted = page.has('已点赞', (.7, .5, 1, .8))
    key = str(race)+':'+group if race is not None and group else None
    votes = [t for t in page.rows((.7, .5, 1, .8)) if t['text'] in {'点赞!', '点赞'} and page.enabled(t)]
    if key and not voted and votes:
        token = min(votes, key=lambda t: (t['bbox'][1], t['bbox'][0]))
        x,y,r,b = token['bbox']
        actions['spectator_vote'] = dict(point=[(x+r)//2,(y+b)//2], bbox=token['bbox'],
            effect='vote', resource_kind='free', like_key=key, like_group=group, like_race=race)
    return result('loh_spectator_list', 'native_spectator_group', actions,
                  like_group=group, like_race=race, like_key=key, like_voted=voted,
                  requires_loh_context=True)


def authorize(action, observed, data):
    if action not in ACTIONS.get(observed.get('page'), ()):
        return None
    if data.get('pending'):
        return dict(ok=False, consuming=False, reason='loh_pending_admission_unresolved')
    if action != 'spectator_vote':
        return None
    candidate = observed.get('actions', {}).get(action, {})
    key = observed.get('like_key')
    pending = data.get('pending_like')
    reserved = (pending and pending.get('input_status') == 'reserved'
                and pending.get('dispatch_id') == (data.get('last_action') or {}).get('dispatch_id')
                and pending.get('like_key') == key)
    allowed = (key and observed.get('like_group') in ('A','B') and not observed.get('like_voted')
               and candidate.get('like_key') == key and candidate.get('effect') == 'vote'
               and candidate.get('resource_kind') == 'free'
               and not data.get('spectator_groups', {}).get(observed['like_group'], {}).get('verified')
               and (not pending or reserved))
    return dict(ok=bool(allowed), consuming=False,
                reason='loh_free_spectator_vote' if allowed else 'loh_like_submission_unresolved')


def choose(observed, data):
    page = observed.get('page')
    if page not in PAGES:
        return None
    def action(name):
        if name not in observed.get('actions', {}):
            return dict(kind='wait', action_id=None, consuming=False, reason='loh_like_controls_rendering')
        return dict(kind='action', action_id=name, consuming=False, reason='loh_spectator_support')
    if data.get('pending'):
        return dict(kind='finish', status='failed', action_id=None, consuming=False, reason='loh_pending_admission_unresolved')
    if page == 'loh_spectator_reward':
        return action('spectator_close_reward')
    if complete(data) and not data.get('pending_like'):
        return action('back_loh')
    if page == 'loh_spectator_hub':
        return action('spectator_open')
    group = observed.get('like_group')
    if group not in ('A','B') or observed.get('like_race') is None:
        return dict(kind='wait', action_id=None, consuming=False, reason='loh_like_group_unknown')
    pending = data.get('pending_like')
    if pending:
        if pending['like_group'] != group:
            return action('spectator_'+pending['like_group'].lower())
        return dict(kind='wait', action_id=None, consuming=False, reason='loh_like_submission_unresolved')
    if data.get('spectator_groups', {}).get(group, {}).get('verified'):
        return action('spectator_'+('b' if group == 'A' else 'a'))
    return action('spectator_vote')


def observe_state(data, observed):
    page = observed.get('page')
    if page not in PAGES or data.get('pending'):
        return
    data['spectator_active'] = True
    group = observed.get('like_group')
    groups = data.setdefault('spectator_groups', {})
    pending = data.get('pending_like')
    reward = page == 'loh_spectator_reward' and group in ('A','B')
    voted = (page == 'loh_spectator_list' and observed.get('like_voted') is True
             and group in ('A','B') and observed.get('like_key'))
    if not (reward or voted):
        return
    matched = (pending and pending.get('like_group') == group
               and (reward or pending.get('like_key') == observed.get('like_key')))
    if pending and not matched and voted and pending.get('like_group') == group:
        return
    prior = groups.get(group, {})
    evidence = dict(verified=True, reward_verified=bool(reward or prior.get('reward_verified')),
        basis='named_reward_receipt' if reward else 'native_already_liked',
        source_frame_sha256=observed.get('frame_sha256'), like_key=observed.get('like_key'))
    groups[group] = evidence
    if matched:
        data['records'].append(dict(kind='spectator_like_verified', operation=deepcopy(pending), evidence=deepcopy(evidence)))
        data['pending_like'] = None


def reserve(data, action, observed, reserved):
    if action == 'spectator_vote':
        data['pending_like'] = dict(dispatch_id=reserved['dispatch_id'], input_status='reserved',
            like_group=observed['like_group'], like_race=observed['like_race'],
            like_key=observed['like_key'], source_frame_sha256=observed.get('frame_sha256'))
        reserved['like_key'] = observed['like_key']


def receipt(data, action, zero_input, performed, uncertain):
    pending = data.get('pending_like')
    if not pending or pending.get('dispatch_id') != action.get('dispatch_id'):
        return
    if zero_input:
        data['records'].append(dict(kind='spectator_zero_input_release', operation=deepcopy(pending)))
        data['pending_like'] = None
    else:
        pending['input_status'] = 'submitted' if performed and not uncertain else 'uncertain'
