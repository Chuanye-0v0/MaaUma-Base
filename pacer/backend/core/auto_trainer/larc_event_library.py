"""Use the existing offline event/skill graph in local event decisions.

Scoped visible title and option count bind the current event. Reference effects
remain estimates; resolving an event never asserts a card level, an acquired
skill, a settled event, or a permission to tap.
"""
from copy import deepcopy
import difflib
from functools import lru_cache
import json
import math
import re
import unicodedata
from pathlib import Path

from core.support_card_knowledge import default_library
from core.skill_knowledge import skill_name_key


ROOT=Path(__file__).resolve().parents[2]
ALIASES={
    '斗志昂扬':'斗志高昂',
    '王手':'将军',
    '弧线的教授':'弧线大师',
    '弯道教授':'弧线大师',
    '教授':'弧线大师',
    '地固':'打基础',
    '出闸':'专注力',
    '金出闸':'专心一意',
    '先手必胜':'先发制人',
    '好转一息':'喘口气',
    '金弯回':'圆弧艺术家',
    '弯回':'弯道恢复○',
    '直回':'直线恢复',
    '垂马':'回避失速优俊少女',
}


def title_key(text):
    from core.event_titles import title_key as normalized
    return normalized(text)


def normalize_preferences(value):
    if not isinstance(value,dict):raise ValueError('事件目标应为配置对象。')
    result=deepcopy(value)
    library=default_library()
    for field in ('target_skills','ignored_skills'):
        values=result.get(field,[])
        if isinstance(values,str):values=values.splitlines()
        if not isinstance(values,list) or any(not isinstance(v,(str,int)) or isinstance(v,bool) for v in values):
            raise ValueError('技能清单请每行填写一个技能名或编号。')
        resolved=[]
        for raw in values:
            key=str(raw).strip()
            if not key:continue
            lookup=next((name for alias,name in ALIASES.items() if skill_name_key(alias)==skill_name_key(key)),key)
            try:identity=library.skills.get(lookup)['skill_id']
            except ValueError as error:
                if str(error)=='skill_identity_ambiguous':
                    raise ValueError(f'技能名有多个匹配：{key}；请填写完整技能名或编号。') from None
                raise ValueError(f'技能库未找到：{key}；请使用国服技能名或编号。') from None
            if identity not in resolved:resolved.append(identity)
        result[field]=resolved
    overlap=set(result['target_skills'])&set(result['ignored_skills'])
    if overlap:raise ValueError('同一技能不能同时设为目标和不需要。')
    for field,default in (('conserve_energy',False),('prioritize_target_routes',True)):
        item=result.get(field,default)
        if type(item) is not bool:raise ValueError('事件取舍开关应为true或false。')
        result[field]=item
    choices=result.get('manual_choices',{})
    if not isinstance(choices,dict) or any(not isinstance(key,str) or not key or type(index) is not int or not 1<=index<=5 for key,index in choices.items()):
        raise ValueError('事件指定请选择上、中、下或对应序号；恢复自动请移除此项。')
    result['manual_choices']=dict(choices)
    from core.auto_trainer.larc_mei_unlock_policy import normalize_mei_unlock_policy
    result['mei_unlock_policy']=normalize_mei_unlock_policy(result.get('mei_unlock_policy'))
    if 'acupuncture_policy' in result:
        from core.auto_trainer.larc_acupuncture_policy import normalize_policy
        result['acupuncture_policy']=normalize_policy(result['acupuncture_policy'])
    if 'with_priority' in result:
        from core.auto_trainer.larc_with_priority import normalize_priority
        result['with_priority']=normalize_priority(result['with_priority'])
    return result


@lru_cache(maxsize=1)
def _support_index():
    index={}
    for event in default_library().events.values():
        for key in {title_key(v) for v in event['titles'].values() if isinstance(v,str) and v}:
            index.setdefault(key,[]).append(event)
    return index



def _outcome_key(event):
    """Collapse only same-story variants with identical rewards/continuations."""
    value=deepcopy(event['options'])
    def strip(item):
        if isinstance(item,list):return [strip(v) for v in item]
        if isinstance(item,dict):return {k:strip(v) for k,v in item.items()
            if k not in {'owner_id','option_id','texts','raw_rewards'}}
        return item
    return json.dumps([event['story_id'],event['owner_character_id'],strip(value),event['next_event_ids']],sort_keys=True,ensure_ascii=False)


def _visible_speaker_character_id(text,*,header_marker):
    lines=[line.strip() for line in str(text).splitlines() if line.strip()]
    markers=[i for i,line in enumerate(lines) if line in {'选项效果','選項效果'}]
    if header_marker not in lines or len(markers)!=1 or markers[0]+1>=len(lines):
        return None
    name=lines[markers[0]+1]
    owners={str(card['character_id']) for card in default_library().cards.values()
        if card.get('character_name_zh_cn')==name and card.get('character_id')}
    if not owners:
        from core.event_knowledge_lookup import load_character_cn_overlay
        owners={str(cid) for cid,info in (load_character_cn_overlay().get('extra_characters') or {}).items()
            if info.get('name_zh_cn')==name}
    return next(iter(owners)) if len(owners)==1 else None


def visible_trainee_character_id(text):
    return _visible_speaker_character_id(text,header_marker='养成优俊少女事件')


def deck_support_scope(text,context,*,library=None):
    library=library or default_library()
    profile=context.get('support_profile') or {}
    raw_cards=profile.get('cards') or []
    cards=[str(c['card_id']) for c in raw_cards if isinstance(c,dict) and c.get('card_id')]
    complete=len(raw_cards)==6 and len(cards)==6
    lines_stripped=[line.strip() for line in str(text).splitlines() if line.strip()]
    deck_chars={str(library.cards[cid]['character_id']) for cid in cards
        if cid in library.cards and library.cards[cid].get('character_id')}
    trainee_chars={str(context.get('trainee_id') or ''),str(context.get('trainee_variant_id') or '')[:4],
        str(visible_trainee_character_id(text) or '')}-{''}
    excluded_chars=deck_chars|trainee_chars
    wild_speaker=_visible_speaker_character_id(text,header_marker='协助卡事件')
    return {'cards':cards,'card_set':set(cards),'complete':complete,'lines_stripped':lines_stripped,
        'has_support_header':'协助卡事件' in lines_stripped,'deck_chars':deck_chars,
        'trainee_chars':trainee_chars,'excluded_chars':excluded_chars,'wild_speaker':wild_speaker}


def _card_confirmed_wild(event_or_row,scope,*,library=None):
    library=library or default_library()
    card_id=str(event_or_row.get('support_card_id') or '')
    if card_id and card_id in scope['card_set']:return False
    owner=str(event_or_row.get('owner_character_id') or event_or_row.get('character_id')
        or (library.cards.get(card_id) or {}).get('character_id') or '')
    if owner and owner in scope['excluded_chars']:return False
    return bool(scope['complete'] and (card_id or (owner and owner not in scope['deck_chars'])))


_CN_SUPPORT_TITLE_ALIASES={
    title_key('将火焰般的爱意献给你'):title_key('将如火般的爱意献给你'),
    title_key('你是速度精确的行板'):title_key('你是节奏恰到好处的行板'),
    title_key('从高十五度恢复原位但是'):title_key('从十五度音程转回原位......但是'),
    title_key('从高十五度恢复原位但是一'):title_key('从十五度音程转回原位......但是'),
    title_key('理应挥洒的汗水'):title_key('应该流的这滴汗'),
    title_key('理应追逐的背影'):title_key('应该追逐的背影'),
    title_key('理应追赶的背影'):title_key('应该追逐的背影'),
}


def _levenshtein(a,b):
    if a==b:return 0
    if not a:return len(b)
    if not b:return len(a)
    prev=list(range(len(b)+1))
    for i,ca in enumerate(a,1):
        curr=[i]
        for j,cb in enumerate(b,1):
            curr.append(min(prev[j]+1,curr[j-1]+1,prev[j-1]+(ca!=cb)))
        prev=curr
    return prev[-1]


def _resolve_known_deck_support_drift(scope,count,library):
    if not (scope['has_support_header'] and scope['card_set'] and scope['wild_speaker'] is not None):
        return None
    speaker_cards={cid for cid in scope['card_set']
        if str((library.cards.get(cid) or {}).get('character_id') or '')==scope['wild_speaker']}
    if not speaker_cards:return None
    lines=scope['lines_stripped']
    hdr_indices=[i for i,l in enumerate(lines) if l=='协助卡事件']
    eff_indices=[i for i,l in enumerate(lines) if l in {'选项效果','選項效果'}]
    if len(hdr_indices)!=1:return None
    start=hdr_indices[0]+1
    end=next((j for j in eff_indices if j>hdr_indices[0]),len(lines))
    obs_lines=lines[start:end]
    if not obs_lines:return None
    obs_line=obs_lines[0]
    obs_key=title_key(obs_line)
    if len(obs_key)<4:return None
    by_group={}
    for event in library.events.values():
        if str(event.get('support_card_id') or '') not in speaker_cards or len(event['options'])!=count:
            continue
        titles=event.get('titles') or {}
        best_pair=None
        for field in ('zh_cn','zh_tw'):
            raw=titles.get(field)
            if not isinstance(raw,str) or not raw:continue
            cand_key=title_key(raw)
            if len(cand_key)<4:continue
            ratio=difflib.SequenceMatcher(None,obs_key,cand_key).ratio()
            dist=_levenshtein(obs_key,cand_key)
            if ratio>=0.72 and dist<=max(2,len(cand_key)//3):
                pair=(ratio,-dist)
                if best_pair is None or pair>best_pair:
                    best_pair=pair
        if best_pair is None:continue
        ratio,neg_dist=best_pair
        dist=-neg_dist
        gk=_outcome_key(event)
        prev=by_group.get(gk)
        if prev is None or (ratio,-dist)>(prev[0],-prev[1]):
            by_group[gk]=(ratio,dist,event)
    if not by_group:
        vol_m = re.match(r'^(vol\d+)', obs_key)
        if vol_m:
            vol_prefix = vol_m.group(1)
            vol_groups = {}
            for event in library.events.values():
                if str(event.get('support_card_id') or '') not in speaker_cards or len(event['options']) != count:
                    continue
                titles = event.get('titles') or {}
                if any(
                    isinstance(titles.get(f), str) and title_key(titles[f]).startswith(vol_prefix)
                    for f in ('zh_cn', 'zh_tw', 'ja')
                ):
                    vol_groups.setdefault(_outcome_key(event), event)
            if len(vol_groups) == 1:
                event = next(iter(vol_groups.values()))
                return (
                    obs_line,
                    event,
                    'known_deck_card_vol_prefix_unique',
                    {'mode': 'known_deck_card_vol_prefix', 'observed_title': obs_line, 'vol_prefix': vol_prefix},
                )
        return None
    ranked=sorted(by_group.values(),key=lambda item:(item[0],-item[1]),reverse=True)
    if len(ranked)>1 and ranked[0][0]-ranked[1][0]<0.15:
        return None
    best_ratio,best_dist,event=ranked[0]
    title_match={'mode':'known_deck_card_drift','observed_title':obs_line,
        'ratio':round(best_ratio,4),'edit_distance':best_dist}
    return obs_line,event,'known_deck_card_cn_title_drift',title_match


def resolve_support(text,count,context):
    library=default_library()
    scope=deck_support_scope(text,context,library=library)
    hits=[]
    idx=_support_index()
    for line in scope['lines_stripped']:
        key=title_key(line)
        alias_key=_CN_SUPPORT_TITLE_ALIASES.get(key)
        raw_matches=idx.get(key,[]) or (idx.get(alias_key,[]) if alias_key else [])
        matches=[e for e in raw_matches if len(e['options'])==count]
        known=[e for e in matches if e['support_card_id'] in scope['card_set']]
        wild=False
        if known:matches=known
        else:
            matches=[e for e in matches if str(e.get('owner_character_id') or '') not in scope['excluded_chars']]
            if scope['complete']:
                if not scope['has_support_header']:continue
                wild=True
            if scope['wild_speaker'] is not None:
                matches=[e for e in matches if str(e.get('owner_character_id') or '')==scope['wild_speaker']]
        if not matches:continue
        groups={_outcome_key(e) for e in matches}
        if len(groups)!=1:return None
        event=matches[0]
        basis=('known_deck_card' if known else 'non_deck_wild_support_unique_outcome'
            if wild else 'unique_story_and_identical_options')
        hits.append((line,event,basis,wild))
    title_match=None
    if not hits:
        from core.auto_trainer.larc_event_matching import catalog_title_match,title_resolution
        resolved=title_resolution(text,count,context)
        if resolved['status']=='unmatched' and scope['complete'] and scope['has_support_header']:
            resolved=catalog_title_match(text,count,context,allow_wild_support=True)
        if resolved['status']=='matched':
            candidates=[hit for hit in resolved['matches'] if hit['row']['source']=='support_library'
                        and hit['title_match']['mode']=='one_edit']
            filtered=[]
            for hit in candidates:
                event=library.events[hit['row']['key']]
                in_deck=str(event.get('support_card_id') or '') in scope['card_set']
                owner=str(event.get('owner_character_id') or '')
                if not in_deck:
                    if owner in scope['excluded_chars']:continue
                    if scope['wild_speaker'] is not None and owner!=scope['wild_speaker']:continue
                    if scope['complete'] and not scope['has_support_header']:continue
                filtered.append((hit,event))
            matches=[event for _,event in filtered]
            if not matches or len({_outcome_key(event) for event in matches})!=1:return None
            hit,event=filtered[0];title_match=hit['title_match']
            wild_one_edit=all(_card_confirmed_wild(item,scope,library=library) for item in matches)
            basis=('one_title_edit_with_visible_owner_and_option_count' if hit['scope_basis']=='visible_support_owner_and_option_count'
                   else 'one_title_edit_with_current_card_and_option_count')
            hits.append((title_match['observed_title'],event,basis,wild_one_edit))
    if not hits:
        drift=_resolve_known_deck_support_drift(scope,count,library)
        if drift is None:return None
        obs_line,event,basis,title_match=drift
        hits.append((obs_line,event,basis,False))
    if len(hits)!=1:return None
    title,event,basis,wild_match=hits[0]
    return {'event':event,'title':title,'kind':'support','identity_basis':basis,'title_match':title_match,
        'non_deck_wild_support':wild_match,
        'reference_basis':event.get('selected_history_period'),'effects_are_observed':False}


def resolve_cut_character(text,count,context):
    # Reuse the cut module's exact costume/story binding and its local U3 data.
    from core.auto_trainer.cut_farming import CutFarmingConfig,load_event_references,CHARACTERS
    identity=str(context.get('trainee_variant_id') or '')
    if identity not in CHARACTERS:return None
    references=_cut_references(identity)
    # This structured adapter covers the user-confirmed MELTY GIFT choice.
    # Other unstructured JP effects remain references, never invented outcomes.
    if identity!='103102' or count!=2:return None
    event_id='501031802'
    rows=references.get(event_id,[])
    keys={title_key(value) for row in rows for value in row['titles'].values() if value}
    titles=[line.strip() for line in text.splitlines() if title_key(line) in keys]
    if len(titles)!=1:return None
    chance={'kind':'condition_chance','name':'才能出众','probability':None}
    options=[{'index':1,'branches':[{'effects':[{'kind':'skill_hint','value':2,'skill_id':'cn:777'},chance],
                                   'probability':1,'unknown_rewards':[]}],'chain_transition':'continue'},
             {'index':2,'branches':[{'effects':[{'kind':'guts','value':20},chance],
                                   'probability':1,'unknown_rewards':[]}],'chain_transition':'continue'}]
    return {'kind':'character','title':titles[0],'event':{'event_id':'character:103102:'+event_id,
        'story_id':event_id,'options':options},'identity_basis':'exact_cut_costume_title_and_option_count',
        'reference_basis':rows[0]['source_id'],'effects_are_observed':False}


@lru_cache(maxsize=3)
def _cut_references(identity):
    from core.auto_trainer.cut_farming import CutFarmingConfig,load_event_references
    return load_event_references(CutFarmingConfig(identity))


def _char_ref_row_matches(row,*,variant,effective_char):
    row_var=str(row.get('trainee_variant_id') or '')
    row_char=str(row.get('character_id') or '') or row_var[:4]
    if variant:
        return row_var==variant or (not row_var and row_char==effective_char)
    return row_char==effective_char


def _character_reference_matches(text,count,context,*,allow_fuzzy=False):
    """Match current costume or base character via context/visible speaker without mutating context."""
    from core.auto_trainer.larc_event_catalog import event_catalog
    variant=str(context.get('trainee_variant_id') or '')
    character=str(context.get('trainee_id') or '')
    if variant and character and character!=variant[:4]:return []
    if not character and variant:character=variant[:4]
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if '协助卡事件' in lines and '养成优俊少女事件' not in lines:return []
    visible_char=visible_trainee_character_id(text)
    effective_char=character if character else (visible_char or '')
    matches=[]
    if effective_char:
        for row in event_catalog().values():
            if (row['source']!='character_reference' or row['option_count']!=count
                    or not _char_ref_row_matches(row,variant=variant,effective_char=effective_char)):
                continue
            keys={title_key(value) for value in row['titles']}
            titles=[line for line in lines if title_key(line) in keys]
            if len(titles)==1:matches.append((row,titles[0]))
        if not matches and allow_fuzzy:
            from core.auto_trainer.larc_event_matching import title_resolution
            local_context=context if context.get('trainee_id') else {**context,'trainee_id':effective_char}
            resolved=title_resolution(text,count,local_context)
            if resolved['status']=='matched':
                matches=[(hit['row'],hit['title_match']['observed_title']) for hit in resolved['matches']
                    if hit['row']['source']=='character_reference'
                    and _char_ref_row_matches(hit['row'],variant=variant,effective_char=effective_char)
                    and hit['title_match']['mode']=='one_edit']
    if not matches and '养成优俊少女事件' in lines and not character:
        global_matches=[]
        for row in event_catalog().values():
            if row['source']!='character_reference' or row['option_count']!=count:
                continue
            keys={title_key(value) for value in row['titles']}
            titles=[line for line in lines if title_key(line) in keys]
            if len(titles)==1:
                global_matches.append((row,titles[0]))
        cids={str(row.get('character_id') or '') or str(row.get('trainee_variant_id') or '')[:4]
              for row,_ in global_matches}
        tkeys={title_key(title) for _,title in global_matches}
        if len(cids)==1 and '' not in cids and len(tkeys)==1:
            matches=global_matches
    if variant and any(row.get('trainee_variant_id')==variant for row,_ in matches):
        matches=[(row,title) for row,title in matches if row.get('trainee_variant_id')==variant]
    return matches


_POSITIVE_CONDITION_CHANCES={
    '才能出众':'才能出众',
    '切れ者':'才能出众',
    '注目株':'万众瞩目',
    '万众瞩目':'万众瞩目',
    '愛嬌○':'讨人喜欢○',
    '爱娇○':'讨人喜欢○',
    '讨人喜欢○':'讨人喜欢○',
    '練習上手○':'擅长练习○',
    '練習上手':'擅长练习○',
    '擅长练习○':'擅长练习○',
}
_CONDITION_CHANCE_RE=re.compile(r'^(?:随机|ランダムで)[『「]([^』」]+)[』」](?:になる|取得)$')


def _character_reference_effects(text):
    """Parse only explicit effects displayed by the existing local catalog."""
    kinds={'速度':'speed','耐力':'stamina','力量':'power','根性':'guts','智力':'wit','技能Pt':'skill_points'}
    effects=[]
    for raw_line in text.splitlines():
        line=raw_line.strip()
        if not line:continue
        if (re.fullmatch(r'シナリオ(?:に応じた)?NPCの(?:羁绊|絆ゲージ)[+-]\d+',line)
                or re.fullmatch(r'(?:URA|アオハル)\s+.+',line)):
            continue
        numeric=re.fullmatch(r'(速度|耐力|力量|根性|智力|技能Pt)([+-]\d+)',line)
        energy=re.fullmatch(r'体力([+-]\d+)',line)
        max_energy=re.fullmatch(r'(?:最大体力|体力最大値)([+-]\d+)',line)
        mood=re.fullmatch(r'(?:干劲|やる気)([+-]\d+)',line)
        all_stats=re.fullmatch(r'5種ステータスを([+-]\d+)',line)
        hint=re.fullmatch(r'「([^」]+)」の启发Lv([+-]?\d+)',line)
        if numeric:
            effects.append({'kind':kinds[numeric[1]],'op':'add','value':int(numeric[2])})
        elif energy:
            effects.append({'kind':'energy','op':'add','value':int(energy[1])})
        elif max_energy:
            effects.append({'kind':'max_energy','op':'add','value':int(max_energy[1])})
        elif mood:
            effects.append({'kind':'mood','op':'add','value':int(mood[1])})
        elif all_stats:
            val=int(all_stats[1])
            for stat in ('speed','stamina','power','guts','wit'):
                effects.append({'kind':stat,'op':'add','value':val})
        elif hint:
            try:skill=default_library().skills.get(hint[1])
            except ValueError:
                effects.append({'kind':'unparsed_reference','text':line})
            else:effects.append({'kind':'skill_hint','op':'add','value':int(hint[2]),
                'skill_id':skill['skill_id'],'db_id':skill['db_id'],'name_zh_cn':skill['name_zh_cn']})
        elif (cond_match:=_CONDITION_CHANCE_RE.fullmatch(line)) and cond_match[1] in _POSITIVE_CONDITION_CHANCES:
            effects.append({
                'kind':'condition_chance',
                'name':_POSITIVE_CONDITION_CHANCES[cond_match[1]],
                'probability':None,
            })
        else:
            effects.append({'kind':'unparsed_reference','text':line})
    return effects


def resolve_character_reference(text,count,context):
    matches=_character_reference_matches(text,count,context,allow_fuzzy=True)
    if not matches or len({title_key(title) for _,title in matches})!=1:return None
    parsed=[]
    for row,_ in matches:
        options=[{'index':option['index'],'branches':[{'effects':_character_reference_effects(option['effects']),
            'probability':1,'unknown_rewards':[]}]} for option in row['options']]
        if (not options or any(not option['branches'][0]['effects']
                or any(e['kind']=='unparsed_reference' for e in option['branches'][0]['effects'])
                for option in options)):
            return None
        parsed.append(options)
    if len({json.dumps(opts,sort_keys=True,ensure_ascii=False) for opts in parsed})!=1:
        return None
    row,title=matches[0];options=parsed[0]
    exact=title_key(title) in {title_key(value) for value in row['titles']}
    if row.get('trainee_variant_id'):
        basis='exact_current_costume_title_and_option_count' if exact else 'current_costume_unique_one_edit_title_and_option_count'
    else:
        basis='exact_character_title_and_option_count' if exact else 'character_unique_one_edit_title_and_option_count'
    return {'kind':'character','title':title,'event':{'event_id':row['key'],'story_id':row.get('story_id') or row['key'],'options':options},
        'identity_basis':basis,
        'title_match':{'mode':'exact' if exact else 'one_edit','observed_title':title,'catalog_title':row['title']},
        'reference_basis':'existing_local_character_reference; JP_effects_not_CN_observed','effects_are_observed':False}


def unsupported_character_reference(text,count,context):
    matches=_character_reference_matches(text,count,context,allow_fuzzy=True)
    if not matches or len({title_key(title) for _,title in matches})!=1:return None
    signatures={json.dumps([o['effects'] for o in row['options']],ensure_ascii=False) for row,_ in matches}
    if len(signatures)!=1:return None
    row,title=matches[0]
    return {'status':'matched_reference_no_automatic_effect_adapter','event_id':row['key'],
        'story_id':row.get('story_id'),'title':title,'trainee_variant_id':row['trainee_variant_id'],
        'option_count':count,'effects_are_observed':False,'options':deepcopy(row['options'])}


def resolve_library_event(text,count,context):
    from core.auto_trainer.larc_additional_training_event import resolve_additional_training
    return (resolve_additional_training(text,count,context) or resolve_cut_character(text,count,context)
        or resolve_support(text,count,context) or resolve_character_reference(text,count,context))


def score_library_event(match,state,goal,context,mood):
    if match["kind"]=="additional_training":
        from core.auto_trainer.larc_additional_training_event import score_additional_training
        return score_additional_training(match,state,goal,context)
    from core.auto_trainer.larc_event_choice import (_effects_value,_compact,
        _branch_recovery,_guaranteed_recovery_choice,_positive_unknown_dominance)
    library=default_library()
    preferences=normalize_preferences(context.get('event_preferences') or goal.event_preferences)
    targets=preferences['target_skills'];ignored=set(preferences['ignored_skills'])
    learned=context.get('learned_skill_ids') or []
    learned_ids=set()
    for item in learned:
        try:learned_ids.add(library.skills.get(item)['skill_id'])
        except ValueError:pass
    event=match['event']
    is_wild=bool(match.get('non_deck_wild_support'))
    deck_card_ids={str(c['card_id']) for c in ((context.get('support_profile') or {}).get('cards') or [])
        if isinstance(c,dict) and c.get('card_id')}
    paths=None if is_wild else (library.option_consequences(event,targets=targets,learned=learned) if match['kind']=='support' else None)
    path_rows={r['option_index']:r for r in paths['options']} if paths else {}
    bonus={} if is_wild else context.get('bonuses_by_owner',{}).get(str(event.get('support_card_id')), {})
    rows=[]
    rank_by_index={}
    for option in event['options']:
        scenarios=[]
        for branch in option['branches']:
            effects=deepcopy(branch.get('effects',[]))
            # Chain loss is valued from actual routes below, not a universal
            # prohibition against every early finish/recovery option.
            # Non-deck wild support bond/chain/outing progress has zero future value.
            effects=[e for e in effects if e.get('kind')!='chain_end'
                and not (is_wild and e.get('kind') in {'outing_unlock','outing_unavailable'})
                and not (is_wild and e.get('kind')=='bond' and str(e.get('owner_id') or '') not in deck_card_ids)]
            values={};skill_notes=[]
            for effect in effects:
                if effect['kind']!='skill_hint':continue
                key=effect.get('skill_id') or effect.get('db_id') or effect.get('name_zh_cn')
                try:skill=library.skills.get(key)
                except ValueError:continue
                sid=skill['skill_id'];level=effect.get('value',1)
                value=(0. if sid in ignored else 12. if sid in targets else 2.)*max(1,level if type(level) in (int,float) else 1)
                for name in (sid,skill['db_id'],skill['name_zh_cn'],skill.get('name_ja')):
                    if name:values[name]=value
                skill_notes.append({'skill_id':sid,'score':value,'basis':'ignored' if sid in ignored else 'target' if sid in targets else 'generic_hint_estimate'})
            scoring_context={**context,'event_preferences':preferences,'skill_hint_values':{**values,**context.get('skill_hint_values',{})}}
            result=_effects_value(effects,state,goal,scoring_context,mood,bonus)
            if preferences['conserve_energy']:
                losses=sum(min(0,e.get('value',0)) for e in effects if e['kind']=='energy' and type(e.get('value')) in (int,float))
                if losses<0:
                    usable=min(-losses,state.vitality*(context.get('max_vitality') or 110)/100) if state.vitality is not None else -losses
                    # Extra event consumption competes with future paid training.
                    # A configurable floor is a policy estimate, not game energy.
                    floor=preferences.get('energy_cost_floor',1.6)
                    floor=floor if type(floor) in (int,float) and math.isfinite(floor) and floor>=0 else 1.6
                    desired=-usable*floor
                    old=result['components'].get('energy',0)
                    if desired<old:
                        result['score']+=desired-old;result['components']['energy_opportunity_floor']=desired-old
                    if state.vitality is None:
                        result['unknown_effects']=[e for e in result['unknown_effects']
                            if not (e.get('kind')=='energy' and type(e.get('value')) in (int,float) and e['value']<0)]
                        result['energy_basis']='configured_conservation_estimate; current_energy_unknown'
            result['unknown_effects']+=branch.get('unknown_rewards',[])
            result['recovery_safe']=result['recovery_safe'] and not branch.get('unknown_rewards')
            result['skill_values']=skill_notes
            scenarios.append(result)
        if not scenarios:return None
        probabilities=[b.get('probability') for b in option['branches']]
        if (all(type(p) in (int,float) and p>=0 for p in probabilities)
                and abs(sum(probabilities)-1)<1e-6):
            selected=deepcopy(scenarios[0])
            selected['score']=sum(s['score']*p for s,p in zip(scenarios,probabilities))
            selected['components']={key:sum(s['components'].get(key,0)*p for s,p in zip(scenarios,probabilities))
                for key in {k for s in scenarios for k in s['components']}}
            selected['unknown_effects']=[e for s in scenarios for e in s['unknown_effects']]
            branch_basis='reference_probabilities'
        else:
            selected=deepcopy(min(scenarios,key=lambda s:(s['priority'],s['score'])))
            branch_basis='worst_known_branch_probability_unknown'
        path=path_rows.get(option['index'],{})
        retained=path.get('target_paths_retained',[])
        direct=set.intersection(*[{note['skill_id'] for note in scenario['skill_values']
            if note['basis']=='target'} for scenario in scenarios])-learned_ids
        matched_targets=set(retained)|direct
        priority=len(matched_targets) if preferences['prioritize_target_routes'] and not path.get('target_path_uncertain') else 0
        target_order_rank=(tuple(sorted((-targets.index(sid) for sid in matched_targets if sid in targets),reverse=True))
            if priority>0 else ())
        rank_by_index[option['index']]=(priority,target_order_rank)
        rows.append({**selected,'index':option['index'],'target_priority':priority,'branch_basis':branch_basis,
            **_branch_recovery(scenarios),
            'score':round(selected['score'],4),'target_paths_retained':retained,
            'lost_target_skill_routes':path.get('lost_target_skill_routes',[]),
            'target_hints_now':sorted(set(path.get('target_skill_hints_now',[]))|direct),
            'chain_transition':option.get('chain_transition'),'future_rewards_are_guaranteed':False})
    recovery=_guaranteed_recovery_choice(rows,goal=goal)
    best_target_key=max(rank_by_index[x['index']] for x in rows)
    eligible=[r for r in rows if rank_by_index[r['index']]==best_target_key]
    # Equal unknown rewards (e.g. both branches' unquantified cut chance)
    # cancel. Do not invent probabilities or claim a reward was acquired.
    unknown=lambda row:json.dumps(row['unknown_effects'],sort_keys=True,ensure_ascii=False)
    unknown_comparison=None
    if recovery is None and len(eligible)>1 and len({unknown(r) for r in eligible})>1:
        unknown_comparison=_positive_unknown_dominance(eligible,goal)
        if unknown_comparison is None:
            return {'status':'fallback','reason':'library_unknown_effect_difference','choice_index':1,
                'expected_event':None,'event_id':event['event_id'],'candidates':rows,
                'non_deck_wild_support':is_wild,'chain_consequences_ignored':is_wild}
    best=recovery if recovery is not None else max(eligible,key=lambda r:(r['priority'],r['score'],-r['index']))
    return {'status':'selected','reason':'usable_recovery_saves_future_turn' if recovery is not None else 'positive_unknown_dominance' if unknown_comparison else 'target_skill_route' if best['target_priority'] else 'library_current_and_future_value',
        'choice_index':best['index'],'event_id':event['event_id'],'story_id':event['story_id'],
        'expected_event':(_compact(match['title']),*[f'option:{i}' for i in range(1,len(rows)+1)]),
        'effect_basis':'existing_offline_library_reference_not_observed','identity_basis':match['identity_basis'],
        'reference_basis':match['reference_basis'],'candidates':rows,'event_preferences':preferences,
        'title_match':match.get('title_match'),
        'non_deck_wild_support':is_wild,'chain_consequences_ignored':is_wild,
        'event_policy_version':'larc_events_v5','learned_skills_written':False,
        'recovery_priority_basis':'real_body_deficit; safe_recovery_in_every_reference_branch; manual_choices_first' if recovery is not None else None,
        **({'unknown_effect_comparison':unknown_comparison} if unknown_comparison is not None else {})}
