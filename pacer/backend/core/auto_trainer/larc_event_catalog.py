"""Search local event sources and apply explicit per-event user choices."""
from functools import lru_cache
import json
import re

from core.event_knowledge_lookup import (
    load_character_cn_overlay,
    load_reviewed_events,
    PERSONAL_PATH,
)
from core.support_card_knowledge import default_library
from core.auto_trainer.larc_event_library import title_key


def _titles(raw):
    values=[]
    for key,value in raw.items():
        if isinstance(value,str) and value:values.append(value)
        elif key=='aliases':values.extend(v['text'] for v in value if isinstance(v,dict) and v.get('text'))
    from core.auto_trainer.larc_additional_training_event import TITLES,family_title
    if any(family_title(value) for value in values):values.extend(TITLES)
    return list(dict.fromkeys(values))


def _position(index,count):
    if index==1:return '上'
    if index==count:return '下'
    if count==3:return '中'
    return f'第{index}项'


def _number(value):
    if type(value) in (int,float):return f'{value:+g}'
    if isinstance(value,dict) and 'min' in value and 'max' in value:return f"{value['min']}～{value['max']}"
    return '（数值未整理）'


def _effects(effects):
    names={'speed':'速度','stamina':'耐力','power':'力量','guts':'根性','wit':'智力','wisdom':'智力',
        'skill_points':'技能Pt','skill_pt':'技能Pt','energy':'体力','mood':'干劲','bond':'羁绊',
        'max_energy':'体力上限','max_vitality':'体力上限'}
    result=[]
    for effect in effects:
        kind=effect.get('kind');value=effect.get('value',effect.get('base_value'))
        if kind in names:result.append(names[kind]+_number(value))
        elif kind=='skill_hint':
            label=effect.get('name_zh_cn')
            if not label:
                try:label=default_library().skills.get(effect.get('skill_id') or effect.get('db_id'))['name_zh_cn']
                except ValueError:label='技能'
            result.append(f'{label}启发Lv{_number(value)}')
        elif kind=='chain_end':result.append('结束连续事件')
        elif kind=='outing_unlock':result.append('解锁外出')
        else:result.append('其他效果（参考资料未整理）')
    return '；'.join(result) or '收益资料未整理'


def _raw_effects(text):
    def skill(match):
        try:return '「'+default_library().skills.get(match[1])['name_zh_cn']+'」'
        except ValueError:return match[0]
    text=re.sub(r'『([^』]+)』',skill,text)
    for ja,cn in [('スキルPt','技能Pt'),('スピード','速度'),('スタミナ','耐力'),('パワー','力量'),
        ('やる気','干劲'),('賢さ','智力'),('絆ゲージ','羁绊'),('ヒントLv','启发Lv'),
        ('ランダムで','随机'),('切れ者','才能出众'),
        ('注目株','万众瞩目'),('愛嬌○','讨人喜欢○'),('練習上手○','擅长练习○'),('練習上手','擅长练习○')]:text=text.replace(ja,cn)
    return text[:1600]


def _additional_effects(event):
    from core.auto_trainer.larc_additional_training_event import family_title,reference_effects
    return reference_effects(event['options']) if any(family_title(value) for value in _titles(event['titles'])) else None


@lru_cache(maxsize=1)
def event_catalog():
    library=default_library();rows={};character_ids={};character_names={}
    for card in library.cards.values():
        name=re.sub(r'^\[[^\]]*\]','',card.get('name_ja','')).strip()
        character_ids[title_key(name)]=card['character_id']
        character_names[card['character_id']]=card.get('character_name_zh_cn') or name
    overlay=load_character_cn_overlay()
    for cid,info in (overlay.get('extra_characters') or {}).items():
        cid_str=str(cid)
        ja_name=info.get('name_ja') or ''
        cn_name=info.get('name_zh_cn') or ja_name
        if ja_name and title_key(ja_name) not in character_ids:
            character_ids[title_key(ja_name)]=cid_str
        if cid_str not in character_names and cn_name:
            character_names[cid_str]=cn_name
    def add(row):
        row['option_count']=len(row['options'])
        if not 2<=row['option_count']<=5:return
        row['search_text']=title_key(' '.join([row['title'],row['owner'],*row['titles'],*row.get('aliases',[]),
            str(row.get('story_id') or ''),row['key'],*[o['effects'] for o in row['options']]]))
        row['label']=row['owner']+' · '+row['title']
        for option in row['options']:option['position']=_position(option['index'],row['option_count'])
        rows[row['key']]=row
    for event in library.events.values():
        if len(event['options'])<2:continue
        card=library.cards[event['support_card_id']];options=[]
        for option in event['options']:
            branches=[]
            for branch in option['branches']:
                text=_effects(branch.get('effects',[]))
                if branch.get('unknown_rewards'):text+='；另有未整理收益'
                branches.append(text)
            effects=' / '.join(branches)
            if len(branches)>1:effects+='（分支结果，非全部同时获得）'
            if option.get('chain_transition')=='end' and '结束连续事件' not in effects:effects+='；结束连续事件'
            options.append({'index':option['index'],'text':option.get('texts',{}).get('zh_cn') or '', 'effects':effects})
        stage=event.get('stage');stage_text={1:'第一段',2:'第二段',3:'第三段',4:'第四段',5:'第五段'}.get(stage,'')
        types={'speed':'速度','stamina':'耐力','power':'力量','guts':'根性','intelligence':'智力','friend':'友人','group':'小组'}
        owner=f"{card.get('rarity','')} {types.get(card.get('type'),'')} {card['name_zh_cn']}".strip()
        add({'key':event['event_id'],'source':'support_library','title':event['titles'].get('zh_cn') or event['titles']['ja'],
            'titles':_titles(event['titles']),'owner':owner,'scope_kind':'support','support_card_id':event['support_card_id'],
            'character_id':event['owner_character_id'],'stage':stage,'story_id':event['story_id'],
            'aliases':[*card.get('aliases',[]),stage_text,*(['根成'] if event['support_card_id']=='30153' else [])],
            'scope_label':'对应支援卡的同名事件','options':options,'reference_basis':event.get('selected_history_period')})
    from core.auto_trainer.cut_farming import EVENT_OWNERS
    variants={title_key(v):k for k,v in EVENT_OWNERS.items()}
    overlay_events=overlay.get('events') or {}
    if PERSONAL_PATH.is_file():
        for event in json.loads(PERSONAL_PATH.read_text(encoding='utf8')).get('events',[]):
            if event.get('owner_type')!='character' or len(event.get('options',[]))<2:continue
            owner=event.get('owner_name_ja','');base=re.split(r'[（(]',owner)[0]
            character_id=character_ids.get(title_key(base))
            variant=variants.get(title_key(owner))
            if variant:character_id=variant[:4]
            ov=overlay_events.get(event.get('source_id'))
            raw_titles=event['titles']
            titles_dict={**raw_titles,'zh_cn':ov['zh_cn']} if (ov and ov.get('zh_cn') and not raw_titles.get('zh_cn')) else raw_titles
            ids=(event.get('cn_title_alignment') or {}).get('candidate_story_ids',[])
            if not ids and ov:ids=ov.get('candidate_story_ids',[])
            selected_ids=[v for v in ids if str(v).startswith('50'+str(character_id))] if character_id else []
            title=titles_dict.get('zh_cn') or titles_dict.get('zh_tw') or titles_dict.get('ja')
            if not title:continue
            ev_for_additional=event if titles_dict is raw_titles else {**event,'titles':titles_dict}
            add({'key':event['source_id'],'source':'character_reference','title':title,'titles':_titles(titles_dict),
                'owner':(character_names.get(character_id) or event.get('owner_name_zh_tw') or owner)+' · '+owner,
                'scope_kind':'character','character_id':character_id,'trainee_variant_id':variant,
                'scope_label':'对应服装的角色事件' if variant else '本角色的同名事件（服装差异请看参考效果）',
                'story_id':selected_ids[0] if len(selected_ids)==1 else None,
                'options':[{'index':o['index'],'text':o.get('texts',{}).get('zh_cn') or '',
                    'effects':_raw_effects(o.get('effect_text',''))} for o in event['options']],
                'selectable':bool(character_id),'reference_basis':'local_character_reference',
                'additional_training_effects':_additional_effects(ev_for_additional)})
    for event in load_reviewed_events():
        if len(event['options'])<2:continue
        owner=event.get('owner',{});namespace=owner.get('owner_id_namespace');oid=str(owner.get('owner_id',''))
        options=[]
        for option in event['options']:
            branches=option.get('conditional_branches') or [{}]
            effects=' / '.join(_effects(option.get('effects',[])+branch.get('effects',[])) for branch in branches)
            options.append({'index':option['index'],'text':option.get('texts',{}).get('zh_cn') or '', 'effects':effects})
        card=library.cards.get(oid,{})
        add({'key':'reviewed:'+event['event_id'],'source':'reviewed','title':event['titles'].get('zh_cn') or event['titles'].get('ja'),
            'titles':_titles(event['titles']),'owner':card.get('name_zh_cn') or character_names.get(oid) or '已整理事件',
            'scope_kind':'support' if namespace in {'support_card_id','support_chara_id'} else 'character' if namespace=='trainee_id' else 'scenario',
            'support_card_id':oid if namespace=='support_card_id' else None,
            'character_id':oid if namespace in {'trainee_id','support_chara_id'} else card.get('character_id'),
            'story_id':str(event.get('story_id') or event['event_id'].split(':',1)[1]) if event['event_id'].startswith('jp.single_mode_story:') else event.get('story_id'),
            'scope_label':'已整理的事件及选项','options':options,'reference_basis':'reviewed_local_catalog'})
    return rows


def search_events(query='',*,keys=None,limit=40):
    from core.auto_trainer.larc_event_library import ALIASES
    query=str(query)
    for alias,canonical in ALIASES.items():query=query.replace(alias,canonical)
    # Multiple words should narrow the list; title_key itself removes spaces.
    terms=[title_key(word) for word in str(query).split() if title_key(word)]
    allowed=set(keys) if isinstance(keys,list) else None
    rows=[r for r in event_catalog().values() if (allowed is None or r['key'] in allowed)
          and all(term in r['search_text'] for term in terms)]
    rows.sort(key=lambda r:(r['source']!='support_library',r['owner'],r['title']))
    return {'total':len(rows),'events':[{k:v for k,v in row.items() if k!='search_text'} for row in rows[:max(1,min(100,int(limit)))]]}


def _scope_matches(row,context,*,require_costume=False):
    if row.get('selectable') is False:return False
    if row['scope_kind']=='character':
        variant=str(context.get('trainee_variant_id') or '')
        base=str(context.get('trainee_id') or '')
        if variant and base and variant[:4]!=base:return False
        if row.get('trainee_variant_id'):return variant==row['trainee_variant_id']
        return bool(row.get('character_id')) and str(row['character_id']) in {base,variant[:4]}
    if row['scope_kind']=='support':
        from core.auto_trainer.larc_event_library import _card_confirmed_wild,deck_support_scope
        library=default_library()
        scope=deck_support_scope(context.get('screen_text') or context.get('ocr_text') or '',context,library=library)
        if not row.get('support_card_id'):
            owner=str(row.get('character_id') or '')
            if owner and owner in scope['deck_chars']:return True
            if owner and owner in scope['trainee_chars']:return False
            return not scope['complete']
        if str(row['support_card_id']) in scope['card_set']:return True
        owner=str(row.get('character_id') or (library.cards.get(str(row['support_card_id'])) or {}).get('character_id') or '')
        if owner and owner in scope['excluded_chars']:return False
        if _card_confirmed_wild(row,scope,library=library):return False
    return True


def _manual_scope_allows_wild(row,text,context):
    if row.get('selectable') is False or row.get('scope_kind')!='support':return False
    from core.auto_trainer.larc_event_library import _card_confirmed_wild,deck_support_scope
    library=default_library()
    scope=deck_support_scope(text,context,library=library)
    if not scope['has_support_header'] or not _card_confirmed_wild(row,scope,library=library):return False
    owner=str(row.get('character_id') or (library.cards.get(str(row.get('support_card_id') or '')) or {}).get('character_id') or '')
    if scope['wild_speaker'] is not None and owner!=scope['wild_speaker']:return False
    return True


def manual_event_choice(text,count,context,preferences):
    rules=preferences.get('manual_choices') or {}
    if not isinstance(rules,dict) or not rules:return None
    from core.auto_trainer.larc_additional_training_event import additional_training_identity
    identity=additional_training_identity(text,count,context)
    if identity is not None and identity['status']=='owner_conflict':return None
    scoped_context=context if 'screen_text' in context else {**context,'screen_text':text}
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    hits=[];wild_hits=[];has_wild_rules=False
    for key,index in rules.items():
        row=event_catalog().get(key)
        if not row or type(index) is not int or not 1<=index<=count or row['option_count']!=count:continue
        in_scope=_scope_matches(row,scoped_context,require_costume=True)
        wild_allowed=(not in_scope) and _manual_scope_allows_wild(row,text,scoped_context)
        if not in_scope and not wild_allowed:continue
        if wild_allowed:has_wild_rules=True
        titles={title_key(v) for v in row['titles']}
        visible=[line for line in lines if title_key(line) in titles]
        if len(visible)==1:(hits if in_scope else wild_hits).append((row,index,visible[0]))
    from core.auto_trainer.larc_event_matching import catalog_title_match,title_resolution,title_diagnostic
    resolved=title_resolution(text,count,scoped_context)
    # Equal normalized titles/indexes cannot collapse distinct story IDs.
    effective_resolved=resolved;used_hits=[]
    if resolved['status']=='matched':
        applicable={hit['row']['key'] for hit in resolved['matches']}
        used_hits=[hit for hit in hits if hit[0]['key'] in applicable]
        if not used_hits:
            for hit in resolved['matches']:
                row=hit['row'];index=rules.get(row['key'])
                if (hit['title_match']['mode']=='one_edit' and type(index) is int and 1<=index<=count):
                    used_hits.append((row,index,hit['title_match']['observed_title']))
    elif resolved['status']=='unmatched' and has_wild_rules:
        wild_resolved=catalog_title_match(text,count,scoped_context,allow_wild_support=True)
        if wild_resolved['status']!='matched':return None
        effective_resolved=wild_resolved
        applicable={hit['row']['key'] for hit in wild_resolved['matches']}
        used_hits=[hit for hit in wild_hits if hit[0]['key'] in applicable]
        if not used_hits:
            for hit in wild_resolved['matches']:
                row=hit['row'];index=rules.get(row['key'])
                if (hit['title_match']['mode']=='one_edit' and type(index) is int and 1<=index<=count
                        and _manual_scope_allows_wild(row,text,scoped_context)):
                    used_hits.append((row,index,hit['title_match']['observed_title']))
    else:
        return None
    if not used_hits or len({(index,title_key(title)) for _,index,title in used_hits})!=1:return None
    row,index,title=used_hits[0]
    from core.auto_trainer.larc_event_choice import _compact
    from core.event_titles import compare_title
    matched=next((hit['title_match'] for hit in effective_resolved['matches'] if hit['row']['key']==row['key']),None)
    if matched is None:
        matched=next(compare_title(title,value) for value in row['titles'] if title_key(title)==title_key(value))
    return {'status':'selected','reason':'user_event_choice','choice_index':index,
        'expected_event':(_compact(title),*[f'option:{i}' for i in range(1,count+1)]),
        'event_id':row['key'],'story_id':row.get('story_id'),'event_policy_version':'larc_events_v4',
        'matched_rule_ids':[r['key'] for r,_,_ in used_hits],'position':_position(index,count),
        'scope':row['scope_label'],'effect_basis':'explicit_user_preference; reference_rewards_not_observed',
        'non_deck_wild_support':_manual_scope_allows_wild(row,text,scoped_context),
        'title_match':matched,'title_resolution':title_diagnostic(effective_resolved),
        'candidates':[],'learned_skills_written':False}
