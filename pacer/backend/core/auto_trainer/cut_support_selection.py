"""Private D1 support preparation bridge; no start or resource authority."""
from copy import deepcopy
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sys


def _leaf(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # The leaf's dataclasses need their own module.
    exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), module.__dict__)
    return module


@lru_cache(maxsize=1)
def support_api():
    root = Path(__file__).resolve().parents[2]
    from core.cut_portable_runtime import portable_root
    d1=portable_root()
    if d1 is None:
        driver = json.loads((root / 'config/larc_skill_driver.json').read_text(encoding='utf-8-sig'))
        d1_entry = Path(driver['d1_root'])
        if driver.get('path_base') == 'backend' and not d1_entry.is_absolute():
            d1 = (root / d1_entry).resolve(strict=True)
        else:
            d1 = d1_entry.resolve(strict=True)
    support_path = d1 / 'core/autonomous_training/plant_support.py'
    if not support_path.is_file():
        support_path = d1 / 'bridges/skills/backend/core/autonomous_training/plant_support.py'
    api = _leaf('_cut_d1_plant_support', support_path)
    if not api.CATALOG.is_file():
        catalog_candidates = [
            d1 / 'runs/support-card-library/catalog.json',
            d1 / 'bridges/skills/backend/runs/support-card-library/catalog.json',
            root / 'runs/support-card-library/catalog.json',
            root / 'bridges/skills/backend/runs/support-card-library/catalog.json',
        ]
        for ancestor in Path(__file__).resolve().parents:
            catalog_candidates.append(ancestor / 'backend/runs/support-card-library/catalog.json')
            catalog_candidates.append(ancestor / 'runs/support-card-library/catalog.json')
        for candidate in catalog_candidates:
            if candidate.is_file():
                api.CATALOG = candidate
                break
    @lru_cache(maxsize=1)
    def matcher():
        art_path = d1 / 'core/support_library/art_matcher.py'
        if not art_path.is_file():
            art_path = d1 / 'bridges/skills/backend/core/support_library/art_matcher.py'
        art = _leaf('_cut_d1_card_art', art_path)
        return art.CardArtMatcher.from_catalog(api.CATALOG)
    api.matcher = matcher
    return api


def support_options(config):
    values = {'support_deck': config.support_deck, 'borrow_card_id': config.borrow_card_id}
    if config.borrow_card_id:
        values.update(borrow_min_level=config.borrow_min_level, borrow_min_break=config.borrow_min_break)
    return support_api().validate(values) if any(values.values()) else {}


def _owned_deck_breaks(image, slot):
    """Four visible diamonds in the canonical deck's own-card level strip."""
    import cv2
    if slot not in range(5) or image.shape[:2]!=(1280,720):return None
    col=slot%3;row=slot//3
    x=(50,265,478)[col]+(18,15,14)[col]
    y=(260,535)[row]+(240 if row==0 else 238)
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV);marks=[]
    for index in range(4):
        cx=x+18*index;region=hsv[y-2:y+3,cx-2:cx+3]
        cyan=((region[:,:,0]>=78)&(region[:,:,0]<=108)&(region[:,:,1]>90)&(region[:,:,2]>130)).mean()
        gray=((region[:,:,1]<60)&(region[:,:,2]>60)&(region[:,:,2]<235)).mean()
        marks.append(1 if cyan>=.6 else 0 if gray>=.6 else None)
    return sum(marks) if None not in marks and marks==sorted(marks,reverse=True) else None


def read_deck_cards(image,tokens,ocr,*,api=None,borrowed=None):
    api=api or support_api();cards=[]
    for slot,(x,y) in enumerate(( (x,y) for y in (260,535) for x in (50,265,478))):
        details=(deepcopy(borrowed) if slot==5 and borrowed is not None else
            api.card_details(image,(x,y,x+196,y+260),tokens,ocr,friend_slot=slot==5))
        if slot<5:details['limit_break']=_owned_deck_breaks(image,slot)
        cards.append({'slot':slot,**details})
    return cards


class SupportPreparation:
    def __init__(self, config, *, api=None, ocr=None, filter_cache=None):
        self.api = api or support_api()
        self.options = support_options(config) if api is None else api.validate({
            'support_deck': config.support_deck, **({'borrow_card_id': config.borrow_card_id,
                'borrow_min_level': config.borrow_min_level, 'borrow_min_break': config.borrow_min_break}
                if config.borrow_card_id else {})})
        self.context = {'mode': 'start', 'support_options': self.options}
        self.filter_cache=filter_cache if filter_cache is not None else {}
        self.filter_signature=json.dumps(self.options,sort_keys=True)
        self.reused_filters=(self.filter_cache.get('verified') is True
                             and self.filter_cache.get('signature')==self.filter_signature)
        if self.reused_filters:self.context['support_filters_applied']=True
        self.pending = None
        self.last_observed = None
        self.ocr = ocr

    def analyze(self, image, tokens, *, scale=1., source=None):
        text = ''.join(t['text'] for t in tokens)
        if '最终确认' in text or '回复训练值' in text:
            return None  # The original ordinary-start/refill owner handles these.
        if '选择好友协助卡' in text:
            if not self.options.get('borrow_card_id'):
                return None  # Keep the original first-career borrow fallback.
            page = 'support_friend'
        elif '显示设置' in text and self.context.get('support_filter_open'):
            page = 'support_settings'
        elif any(t in text for t in ('协助卡编成', '辅助卡编成', '支援卡编成', '协助卡牌编成')) or any(
                t['text'].startswith('卡组') and 180 < t['bbox'][1] < 260 for t in tokens):
            page = 'support_deck'
        else:
            return None
        observed = {'page': page, 'actions': {}, 'support_full': False}
        if self.ocr is None:
            from core.auto_trainer.cut_condition_probe import _default_ocr_reader
            ocr = _default_ocr_reader()._get_engine()
        else:
            ocr = self.ocr
        self.api.enrich(observed, image, tokens, self.context, ocr)
        if observed['page'] == 'support_deck':
            if any(word in text for word in ('请选择协助卡','请选择辅助卡','请选择支援卡','自有卡未编成','未编成协助卡')):
                return {'state':'unknown','reason':'support_self_cards_missing'}
            from core.auto_trainer.cut_setup_observation import support_slot_observation
            slots=support_slot_observation(image,tokens)
            observed['support_slots']=slots
            observed['owned_support_full']=slots['owned_full']
            # A verified target portrait is complete even if its level required
            # a local OCR crop rather than the initial full-screen read.
            observed['support_full'] = (observed.get('owned_support_full') is True and
                (self.api.matches(observed.get('borrowed_support') or {}, self.options)
                 if self.options.get('borrow_card_id') else slots['full']))
        if self.pending:
            action, before = self.pending
            if observed['page'] not in self.api.NEXT_PAGES[action]:
                return {'state': 'wait', 'reason': 'support_preparation_transition', 'next_phase': 'setup'}
            try:
                self.api.after_action(action, before, observed, self.context)
            except RuntimeError:
                # The one input is submitted. Observe its destination; never
                # tap the previous row/filter/deck button again during animation.
                return {'state': 'wait', 'reason': 'support_preparation_transition', 'next_phase': 'setup'}
            if action=='support_filter_apply' and self.context.get('support_filters_applied'):
                self.filter_cache.update(verified=True,signature=self.filter_signature)
            self.pending = None
        if (observed['page'] == 'support_deck' and not self.options.get('borrow_card_id')
                and observed.get('support_deck_index') == self.options.get('support_deck', observed.get('support_deck_index'))
                and observed.get('owned_support_full') is True and not observed['support_full']):
            return None  # Original fallback can fill the sixth slot, no own edits.
        try:
            action = self.api.choose(observed, self.context)
            if action is None:
                return None
            self.api.authorize(action, observed, self.context)
        except RuntimeError as error:
            reason = str(error)
            if reason=='owned_support_incomplete':
                return {'state':'unknown','reason':'support_self_cards_unreadable',
                    'support_slots':observed.get('support_slots')}
            if reason=='requested_borrow_card_not_found' and self.reused_filters:
                # Game settings usually persist. If the remembered filter no
                # longer exposes a valid target, rebuild it once from this
                # fresh page; never weaken card/level/break requirements.
                self.reused_filters=False;self.filter_cache.clear()
                for key in ('support_filters_applied','support_filter_reset','support_scrolls','support_refreshes'):
                    self.context.pop(key,None)
                action=self.api.choose(observed,self.context)
                self.api.authorize(action,observed,self.context)
            elif reason in {'support_deck_index_unverified', 'support_friend_list_not_ready', 'support_filter_state_unverified'}:
                return {'state': 'wait', 'reason': reason, 'next_phase': 'setup'}
            else:
                return {'state': 'unknown', 'reason': reason}
        self.last_observed = observed
        if action == 'continue_support':
            borrowed = observed.get('borrowed_support')
            if borrowed is None:
                borrowed = self.api.card_details(image, (478,535,674,795), tokens, ocr, friend_slot=True)
            return {'state': 'support_ready', 'support_observation': {
                'deck': observed['support_deck_index'], 'borrowed_support': borrowed,
                'cards':read_deck_cards(image,tokens,ocr,api=self.api,borrowed=borrowed),
                'filters_reused':self.reused_filters,
                'source': str(source) if source else None}, 'next_phase': 'starting'}
        if action not in self.api.ACTION_PAGES or action not in observed['actions']:
            return {'state': 'unknown', 'reason': 'support_action_control_unverified'}
        control = observed['actions'][action]
        result = {'state': action, 'reason': action, 'support_action': action, 'next_phase': 'setup',
            'center': [round(v * scale) for v in control['point']]}
        if control.get('gesture') == 'swipe':
            # Keep rows overlapping and avoid the already-observed fast fling.
            result.update(gesture=[round(v * scale) for v in (*control['point'], 620, 570)], duration_ms=1100)
        return result

    def submitted(self, action):
        if action not in self.api.ACTION_PAGES or self.last_observed is None:
            raise ValueError('support_action_not_authorized')
        self.pending = (action, deepcopy(self.last_observed))
        # The settings destination needs this flag to recognize its own family.
        if action == 'support_settings':
            self.context['support_filter_open'] = True


def actual_borrow_profile(observation):
    """Persist observed levels only, never the configured minimums."""
    details = (observation or {}).get('borrowed_support') or {}
    return _actual_card_profile(details,5,observation)


def actual_deck_profile(observation, *, expected_deck=0):
    observation=observation or {}
    if expected_deck and observation.get('deck')!=expected_deck:return []
    result=[]
    for details in observation.get('cards',[]):
        slot=details.get('slot')
        if type(slot) is not int or not 0<=slot<=5:continue
        card=_actual_card_profile(details,slot,observation)
        if card is not None:result.append(card)
    if not any(c['slot']==5 for c in result):
        borrowed=actual_borrow_profile(observation)
        if borrowed is not None:result.append(borrowed)
    return result


def _actual_card_profile(details,slot,observation):
    identity = details.get('card_id')
    if type(identity) is not int:
        return None
    card = support_api().catalog().get(identity)
    if card is None:
        return None
    source = {'kind': 'cut_support_observation', 'source': observation.get('source'),
        'source_sha256': observation.get('source_sha256'), 'deck': observation.get('deck')}
    effects={};effect_source=source
    # Reuse the existing maximum-level Mei table only at its observed level.
    # A lower-level card is not silently treated as fully levelled.
    stage=details.get('limit_break');level=details.get('level')
    if identity==30160 and type(stage) is int and 0<=stage<=4 and level==30+5*stage:
        from scripts.larc_panel_setup import MEI_EVENT_BONUS,MEI_RULE_SOURCE
        effects={'event_recovery_percent':40+10*stage,'event_effectiveness_percent':MEI_EVENT_BONUS[stage]}
        effect_source={**source,'kind':'catalog_at_observed_max_level','rule_source':MEI_RULE_SOURCE}
    return {'slot': slot, 'card_id': str(identity),
        'character_id': 'satake_mei' if identity == 30160 or str(card.get('character_id')) == '9043'
            else str(card['character_id']) if card.get('character_id') is not None else None,
        'name': card.get('name_cn'), 'title': card.get('title_cn'),
        'type': 'wisdom' if card.get('type') == 'intelligence' else card.get('type'),
        'rarity': card.get('rarity'), 'level': details.get('level'), 'limit_breaks': details.get('limit_break'),
        'actual_effects': effects, 'source': source, 'actual_effects_source': effect_source}
