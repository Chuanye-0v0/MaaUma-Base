"""Self-contained, read-only skill/entity/card knowledge for every front end."""
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
import importlib.util
import json
import re
import unicodedata

DATA=Path(__file__).resolve().parents[1]/'data/knowledge'
LOCALES={'cn':'zh_cn','tw':'zh_tw','jp':'ja'}
SERVER_ALIASES={'国服':'cn','简中':'cn','简中服':'cn','zh_cn':'cn','繁中':'tw','繁中服':'tw','台服':'tw','zh_tw':'tw','日服':'jp','ja':'jp','global':'global','国际服':'global'}
RARITIES={1:'normal',2:'gold',3:'unique',4:'unique',5:'unique',6:'evolved'}
REFERENCE_NOTICE='资料存在不表示该服当前已实装或本局必定可学；效果与费用保留各自快照来源。'

def _load(name):return json.loads((DATA/name).read_text('utf-8-sig'))
def normalize(value):return ''.join(unicodedata.normalize('NFKC',str(value or'')).split()).casefold()
def server_id(value):return SERVER_ALIASES.get(str(value),str(value or'cn').lower())
def _limit(value,default=30):
    try:return max(1,min(200,int(value)))
    except(ValueError,TypeError):return default
def _id(value):return str(value).strip()if value is not None else''
def _int(value,default=0):
    try:return int(value)
    except(ValueError,TypeError):return default

@lru_cache(maxsize=1)
def _conditions():
    path=DATA/'vendor/skill_conditions.py'
    spec=importlib.util.spec_from_file_location('_uma_hub_conditions',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class Catalog:
    def __init__(self):
        self.reference=_load('skills-reference.json');self.rich=_load('skills-rich.json')
        self.support=_load('support-library.json');self.wiki=_load('wiki-skills.json')
        self.entities=_load('entities.json');self.jp_names=_load('jp-skillnames.json');self.umas=_load('jp-umas.json')
        self.engine_skills=_load('jp-engine-skills.json');self.engine_meta=_load('jp-engine-meta.json')
        self.manifest=_load('manifest.json')
        self.ref={str(r['db_id']):r for r in self.reference['skills']}
        self.rich_by_db={str(r['db_id']):r for r in self.rich['skills']}
        self.internal={r['skill_id']:str(r['db_id'])for r in self.rich['skills']}
        self.internal.update({r['skill_id']:str(r['db_id'])for r in self.reference['skills']})
        self.characters={r['character_id']:r for r in self.entities['characters']}
        self.costumes={r['costume_id']:r for e in self.characters.values()for r in e['costumes']}
        self.ids=set(self.ref)|set(self.rich_by_db)|set(self.jp_names)
        for row in self.wiki.values():self.ids|=set(row['data'])|set(row['names'])
        self.effect_kinds={}
        for row in self.rich_by_db.values():
            for branch in row.get('activation_branches',[]):
                for effect in branch['effects']:self.effect_kinds[effect['ability_type']]=effect['kind']
        self.holders=defaultdict(list);self.relations=defaultdict(lambda:defaultdict(set))
        for sid,row in self.rich_by_db.items():
            for kind,targets in row.get('relations',{}).items():
                self.relations[sid][kind].update(self.internal.get(v,v)for v in targets)
            for source in row.get('character_sources',[]):
                self.holders[sid].append(dict(character_id=str(source['chara_id']),costume_id=str(source['card_id']),source_kind=source.get('source_kind','awakening'),source='historical_skill_character_sources'))
        for cid,uma in self.umas.items():
            for oid in uma.get('outfits',{}):
                # This exact ID mapping is used by the cited external project's actual setUma caller.
                index,variant=int(oid[1:-2]),int(oid[-2:]);sid=str(100000+10000*(variant-1)+index*10+1)
                if sid in self.ids:self.holders[sid].append(dict(character_id=cid,costume_id=oid,source_kind='native_unique_formula_reference',source='https://github.com/kachi-dev/uma-tools/blob/4401e25987d9c78707f2d3f8a9dcd02c1c56529c/components/HorseDef.tsx#L256'))
        for server,row in self.wiki.items():
            for sid,raw in row['data'].items():
                for key in ('unique_skill_id_1','unique_skill_id_2'):
                    native=raw.get(key)
                    if native and native!='0'and native in self.ids:
                        self.relations[sid]['inherits_from'].add(native);self.relations[native]['inherited_as'].add(sid)
        for sid,rels in list(self.relations.items()):
            for native in rels.get('inherits_from',[]):
                for holder in self.holders.get(native,[]):self.holders[sid].append(dict(holder,source_kind='inherited_unique_relation',relation_source=native))
        for sid,rows in list(self.holders.items()):
            self.holders[sid]=list({(r['character_id'],r['costume_id'],r['source_kind']):r for r in rows}.values())
        self.cards={str(r['card_id']):r for r in self.support['cards']}
        for server,names in self.entities.get('support_names',{}).items():
            for cid in names:self.cards.setdefault(cid,dict(card_id=cid,training_hint_skills=[],event_reward_skills=[],sources=[],coverage='names_only'))
        self.opportunities=defaultdict(list)
        for cid,card in self.cards.items():
            for skill in card.get('training_hint_skills',[]):
                self.opportunities[str(skill['db_id'])].append(dict(card_id=cid,source_kind='training_hint_pool',event_occurrence_probability=None,guaranteed=False))
        for event in self.support['events']:
            for option in event['options']:
                for branch in option['branches']:
                    for skill in branch.get('skills',[]):
                        if skill.get('db_id')and skill.get('operation')in{'hint','grant_skill','random_hint','random_hint_related'}:
                            self.opportunities[str(skill['db_id'])].append(dict(card_id=str(event['support_card_id']),source_kind='event_reward',event_id=event['event_id'],event_title=event.get('titles',{}),option_index=option['index'],branch_id=branch['branch_id'],branch_probability_if_event_and_option=branch.get('probability'),event_occurrence_probability=None,guaranteed=False,operation=skill['operation'],hint_level_gain=skill.get('hint_level_gain'),effect_region=event.get('effect_region'),sources=event.get('sources',[])))
        self.card_skill_ids=defaultdict(set)
        for sid,rows in self.opportunities.items():
            for row in rows:self.card_skill_ids[row['card_id']].add(sid)

    def metadata(self):
        return dict(kind='versioned_local_knowledge',notice=REFERENCE_NOTICE,
            frozen_reference_version=self.reference.get('dataset_version'),historical_boundary=self.rich.get('boundary'),
            wiki_snapshots={k:deepcopy(v['source'])for k,v in self.wiki.items()},
            jp_name_source=dict(project='kachi-dev/uma-tools',commit='4401e25987d9c78707f2d3f8a9dcd02c1c56529c',includes_obsolete_and_variants=True))

    def resolve_skills(self,value):
        key=_id(value)
        if key in self.ids:return[key]
        if key in self.internal:return[self.internal[key]]
        wanted=normalize(key)
        return[sid for sid in self.ids if wanted and wanted in{normalize(v)for v in[*self.skill_names(sid).values(),*self.skill_aliases(sid)]if v}]

    def skill_aliases(self,sid):
        return sorted({name for row in self.wiki.values()for name in row.get('aliases',{}).get(sid,[])})

    def skill_names(self,sid):
        old=self.rich_by_db.get(sid)or self.ref.get(sid)or{}
        ja=self.jp_names.get(sid,[None])[0]if sid in self.jp_names else old.get('name_ja')
        return dict(zh_cn=self.wiki['cn']['names'].get(sid)or old.get('name_zh_cn'),zh_tw=self.wiki['tw']['names'].get(sid),ja=ja)

    def skill_kind(self,sid,server='cn'):
        old=self.rich_by_db.get(sid)
        if self.relations[sid].get('inherits_from'):return'inherited_unique'
        raw=self.wiki.get(server,{}).get('data',{}).get(sid)or next((r['data'][sid]for r in self.wiki.values()if sid in r['data']),{})
        if old:return old['skill_kind']
        if sid in self.engine_skills:
            if sid.startswith('9'):return'inherited_unique'
            rarity=RARITIES.get(self.engine_skills[sid].get('rarity'))
            if rarity=='unique':return'native_unique'
            if rarity=='evolved':return'evolved'
        if _int(raw.get('group_rate'))<0:return'negative'
        rarity=RARITIES.get(_int(raw.get('rarity')))
        if rarity=='unique':return'native_unique'
        if rarity=='evolved':return'evolved'
        if rarity in{'normal','gold'}:return'purchasable'
        return'unknown'

    def skill_item(self,sid,server='cn'):
        names=self.skill_names(sid);locale=LOCALES.get(server,'zh_cn')
        name=names.get(locale)or names.get('zh_cn')or names.get('ja')or names.get('zh_tw')or('技能 '+sid)
        display_locale=next((key for key in(locale,'zh_cn','ja','zh_tw')if names.get(key)),None)
        raw=self.wiki.get(server,{}).get('data',{}).get(sid)
        return dict(id=sid,db_id=sid,skill_id=(self.rich_by_db.get(sid)or self.ref.get(sid)or{}).get('skill_id',sid),name=name,names=names,
            kind=self.skill_kind(sid,server),server=server,display_locale=display_locale,selected_locale_name_available=bool(names.get(locale)),mechanics_in_selected_snapshot=raw is not None,aliases=self.skill_aliases(sid),
            description=self.wiki.get(server,{}).get('descriptions',{}).get(sid)or((self.ref.get(sid)or{}).get('description','')if server=='cn'else''),
            name_sources=dict(zh_cn='bwiki_cn_text_data_47 / existing_cn_reference',zh_tw='bwiki_tw_text_data_47',ja='kachi_jp_skillnames / historical_master'),
            holders=deepcopy(self.holders.get(sid,[])))

    def holder_ids(self,query):
        key=normalize(query)
        found=[cid for cid,e in self.characters.items()if key and key in{normalize(v)for v in[e['character_id'],*e['names'].values(),*e['aliases']]}]
        if found:return found
        return[oid for oid,o in self.costumes.items()if key and(key==oid or any(key==normalize(title)or key==normalize(title+name)for title in o['names'].values()for name in self.characters[o['character_id']]['names'].values()))]

    def skill_search(self,request):
        server=server_id(request.get('server'));query=str(request.get('query',request.get('q',''))or'').strip()
        holder=_id(request.get('holder_id')or request.get('character_id'));holders=[holder]if holder else[]
        unique_query=any(word in query for word in('固有','unique'))
        holder_query=re.sub(r'固有(?:技能)?|unique','',query,flags=re.I).strip()
        if not holders and unique_query:holders=self.holder_ids(holder_query)
        if not holders and not unique_query:holders=self.holder_ids(query)
        if holders:
            selected=[sid for sid in self.ids if any(h['character_id']in holders or h['costume_id']in holders for h in self.holders.get(sid,[]))]
            if unique_query or request.get('unique_only',False):selected=[sid for sid in selected if self.skill_kind(sid,server)in{'native_unique','inherited_unique'}]
        else:
            terms=[normalize(t)for t in query.split()if normalize(t)]
            selected=[]
            for sid in self.ids:
                item=self.skill_item(sid,server)
                haystack=normalize(' '.join([sid,item['skill_id'],*filter(None,item['names'].values()),*item['aliases'],item['description']]))
                if all(term in haystack for term in terms):selected.append(sid)
        kind=request.get('kind')
        if kind:selected=[sid for sid in selected if self.skill_kind(sid,server)==kind]
        selected.sort(key=lambda sid:(normalize(query)not in{normalize(v)for v in self.skill_names(sid).values()if v},sid not in self.wiki.get(server,{}).get('data',{}),int(sid)if sid.isdigit()else sid))
        warning=[]
        if holders and not selected:warning.append('该角色的技能归属关系尚未收录，不能仅按名字或相近编号猜其固有。')
        missing_relations=[dict(native_skill_id=sid,relation='inherited_as')for sid in selected if self.skill_kind(sid,server)=='native_unique'and not self.relations[sid].get('inherited_as')]
        if holders and missing_relations:warning.append('部分原生固有的继承对应关系尚未收录，未按相似名称或编号猜配。')
        return dict(ok=True,items=[self.skill_item(sid,server)for sid in selected[:_limit(request.get('limit'))]],total=len(selected),holder_ids=holders,missing_relations=missing_relations if holders else[],warnings=warning,dataset=self.metadata())

    def skill_detail(self,sid,server='cn',target=None):
        item=self.skill_item(sid,server);raw=self.wiki.get(server,{}).get('data',{}).get(sid)
        old=self.rich_by_db.get(sid)or self.ref.get(sid)or{}
        branches=[]
        if raw:
            for b in(1,2):
                code=raw.get(f'condition_{b}','');effects=[]
                for slot in(1,2,3):
                    kind=_int(raw.get(f'ability_type_{b}_{slot}'))
                    if not kind:continue
                    value=raw.get(f'float_ability_value_{b}_{slot}')
                    effects.append(dict(ability_type=kind,kind=self.effect_kinds.get(kind,'unknown'),raw_value=value,value=_int(value)/10000 if value is not None else None,value_usage=raw.get(f'ability_value_usage_{b}_{slot}'),target_type=raw.get(f'target_type_{b}_{slot}')))
                if b==2 and not code and not effects:continue
                ast=_conditions().parse_condition(code)
                branches.append(dict(branch=b,condition_code=code,precondition_code=raw.get(f'precondition_{b}',''),condition_ast=ast,
                    condition_explanation=_conditions().describe_condition(ast,(target or{}).get('distance_m')),
                    effects=effects,duration=dict(base_seconds=_int(raw.get(f'float_ability_time_{b}'))/10000,usage=raw.get(f'ability_time_usage_{b}'),race_distance_scaling_evaluated=False),
                    cooldown_seconds=_int(raw.get(f'float_cooldown_time_{b}'))/10000))
        elif server=='jp'and sid in self.engine_skills:
            for index,b in enumerate(self.engine_skills[sid].get('alternatives',[]),1):
                ast=_conditions().parse_condition(b.get('condition',''))
                branches.append(dict(branch=index,condition_code=b.get('condition',''),precondition_code=b.get('precondition',''),condition_ast=ast,
                    condition_explanation=_conditions().describe_condition(ast,(target or{}).get('distance_m')),
                    effects=[dict(ability_type=e['type'],kind=self.effect_kinds.get(e['type'],'unknown'),raw_value=e['modifier'],value=e['modifier']/10000,target_type=e.get('target'))for e in b.get('effects',[])],
                    duration=dict(base_seconds=b.get('baseDuration',0)/10000,usage=None,race_distance_scaling_evaluated=False)))
        elif server=='cn'and old.get('activation_branches'):branches=deepcopy(old['activation_branches'])
        item.update(activation_branches=branches,effects=[e for b in branches for e in b.get('effects',[])],
            mechanics_status='selected_wiki_snapshot'if raw else'external_jp_engine_reference'if server=='jp'and branches else'historical_cn_reference'if branches else'missing_for_selected_server',
            relations={k:sorted(v)for k,v in self.relations[sid].items()},
            historical_cost=deepcopy(old.get('cost')),source_urls=list(dict.fromkeys([self.wiki[server]['source']['url']]if server in self.wiki and raw else old.get('source_urls',[]))),
            simulation_supported_here=False,activation_probability=None)
        if server=='jp'and branches and not raw:
            item['source_urls'].append('https://github.com/kachi-dev/uma-tools/blob/4401e25987d9c78707f2d3f8a9dcd02c1c56529c/uma-skill-tools/data/skill_data.json')
        item['holder_relation_status']='recorded_reference'if item['holders']else'not_recorded'
        if target:item['target_inputs']=deepcopy(target)
        return item

    def entity_items(self,request):
        server=server_id(request.get('server'));locale=LOCALES.get(server,'zh_cn')
        query=normalize(request.get('query',request.get('q',request.get('character_id',request.get('id','')))))
        kind=request.get('kind');rows=[]
        for e in self.characters.values():
            name=e['names'].get(locale)or e['names'].get('zh_cn')or e['names'].get('ja')or e['character_id']
            base=dict(id=e['character_id'],character_id=e['character_id'],kind='character',name=name,names=deepcopy(e['names']),name_sources=deepcopy(e['name_sources']),aliases=deepcopy(e['aliases']),costumes=deepcopy(e['costumes']),server=server,live_availability_verified=False)
            haystack=normalize(' '.join([e['character_id'],*e['names'].values(),*e['aliases']]))
            if kind!='costume'and(not query or query in haystack):rows.append(base)
            if kind!='character':
                for outfit in e['costumes']:
                    text=normalize(' '.join([outfit['costume_id'],*outfit['names'].values()]))
                    if not query or query in haystack or query in text:
                        title=outfit['names'].get(locale)or outfit['names'].get('ja')or outfit['names'].get('zh_cn')or''
                        rows.append(dict(id=outfit['costume_id'],character_id=e['character_id'],costume_id=outfit['costume_id'],kind='costume',name=(title+' '+name).strip(),names=deepcopy(e['names']),costume_names=deepcopy(outfit['names']),name_sources=deepcopy(e['name_sources']),costume_name_sources=deepcopy(outfit['name_sources']),server=server,live_availability_verified=False))
        return dict(ok=True,items=rows[:_limit(request.get('limit'))],total=len(rows),dataset=self.metadata())

    def card_item(self,cid,server='cn'):
        row=self.cards[cid];names=dict(zh_cn=self.entities['support_names'].get('cn',{}).get(cid)or row.get('name_zh_cn'),zh_tw=self.entities['support_names'].get('tw',{}).get(cid),ja=row.get('name_ja'))
        name=names.get(LOCALES.get(server,'zh_cn'))or names.get('zh_cn')or names.get('ja')or names.get('zh_tw')or('支援卡 '+cid)
        return dict(id=cid,card_id=cid,name=name,names=names,rarity=row.get('rarity'),type=row.get('type'),coverage=row.get('coverage','historical_opportunities'),cn_release_status=row.get('cn_release_status','unknown'),sources=row.get('sources',[]),skill_opportunities=None if row.get('coverage')=='names_only'else len(self.card_skill_ids[cid]),guaranteed_skills=False)

    def support_search(self,request):
        server=server_id(request.get('server'));query=normalize(request.get('query',request.get('q','')));rows=[]
        for cid in self.cards:
            item=self.card_item(cid,server)
            if not query or query in normalize(' '.join([cid,*filter(None,item['names'].values())])):rows.append(item)
        rows.sort(key=lambda r:(r['coverage']=='names_only',r['card_id']))
        return dict(ok=True,items=rows[:_limit(request.get('limit'))],total=len(rows),dataset=self.metadata())

    def support_sources(self,request):
        server=server_id(request.get('server'));selection=request.get('card_ids',request.get('cards'))
        selected=None;unresolved=[]
        if selection is not None:
            if not isinstance(selection,list):return dict(ok=False,error='cards_must_be_list',message='卡组应为支援卡编号列表。')
            selected=set()
            for value in selection:
                cid=_id(value.get('card_id',value.get('id'))if isinstance(value,dict)else value)
                if cid in self.cards:selected.add(cid)
                else:unresolved.append(cid)
        identity=request.get('skill_id',request.get('db_id'));skills=self.resolve_skills(identity)if identity is not None else sorted(self.opportunities)
        rows=[]
        for sid in skills:
            found=[deepcopy(r)for r in self.opportunities.get(sid,[])if selected is None or r['card_id']in selected]
            if not found:continue
            for row in found:row['card']=self.card_item(row['card_id'],server)
            rows.append(dict(**self.skill_item(sid,server),sources=found,opportunity=True,guaranteed=False))
        return dict(ok=True,items=rows[:_limit(request.get('limit'),200)],total=len(rows),selected_cards=None if selected is None else sorted(selected),unresolved_cards=unresolved,
            unconfirmed_card_ids=[]if selected is None else sorted(cid for cid in selected if self.cards[cid].get('coverage')=='names_only'),
            coverage='partial'if unresolved or selected is not None and any(self.cards[cid].get('coverage')=='names_only'for cid in selected)else'known_historical_snapshot',
            full_six_card_deck=selected is not None and len(selected)==6 and not unresolved,
            message='列出提示池和事件技能机会；实际提示、事件发生及分支选择未确认，不能当作必得清单。',dataset=self.metadata())

@lru_cache(maxsize=1)
def catalog():return Catalog()

def dispatch(request):
    if not isinstance(request,dict):return dict(ok=False,error='request_must_be_object')
    action=request.get('action','knowledge.summary');c=catalog();server=server_id(request.get('server'))
    if action=='knowledge.summary':
        return dict(ok=True,counts=dict(skills=len(c.ids),rich_skills=len(c.rich_by_db),reference_skills=len(c.ref),characters=len(c.characters),costumes=len(c.costumes),support_cards=len(c.cards),support_with_opportunities=len(c.support['cards']),events=len(c.support['events'])),
            snapshots={k:dict(mechanics=len(v['data']),names=len(v['names']),source=v['source'])for k,v in c.wiki.items()},
            dataset=c.metadata(),actions=['skill.search','skill.detail','entity.search','entity.names','support.search','support.skill_sources','support.deck'])
    if action in{'skill.search','skill.by_holder'}:return c.skill_search(request)
    if action in{'skill.detail','skill.names'}:
        identity=request.get('skill_id',request.get('db_id',request.get('id',request.get('query',''))));ids=c.resolve_skills(identity)
        if len(ids)!=1:return dict(ok=True,status='ambiguous'if ids else'not_found',items=[c.skill_item(sid,server)for sid in ids],total=len(ids),message='同名的原生固有、继承与其他版本按编号分别选择。',dataset=c.metadata())
        skill=c.skill_detail(ids[0],server,request.get('target'))
        return dict(ok=True,skill=skill,items=[skill],total=1,dataset=c.metadata())
    if action in{'entity.search','entity.names'}:return c.entity_items(request)
    if action=='support.search':return c.support_search(request)
    if action=='support.detail':
        cid=_id(request.get('card_id',request.get('id')))
        if cid not in c.cards:return dict(ok=True,status='not_found',items=[],total=0)
        result=c.support_sources(dict(request,card_ids=[cid]));result['support']=c.card_item(cid,server);return result
    if action in{'support.skill_sources','support.deck','support.opportunities','support.skills'}:return c.support_sources(request)
    return dict(ok=False,error='unknown_knowledge_action',message='不支持的知识查询动作。')
