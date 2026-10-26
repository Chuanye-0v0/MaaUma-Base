"""Local inheritance reference tools; data lookup and planning never operate the game.

The group-intersection engine is self-contained. Its deterministic operations
follow the independently implemented MaaUma 0.4.38 contracts; optional probability
output is a named mathematical reference model, not measured server odds.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from heapq import nsmallest
from itertools import combinations, product
import json
import hashlib
import math
import os
from pathlib import Path
import re
from threading import RLock
import unicodedata
import uuid

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/inheritance'
STORAGE=ROOT/'data/user-libraries'
NODE_KEYS=('target','parent_a','gp_a1','gp_a2','parent_b','gp_b1','gp_b2')
LABELS=('育成目标','亲代 A','A 的祖辈 1','A 的祖辈 2','亲代 B','B 的祖辈 1','B 的祖辈 2')
COMPONENTS=(('target_parent_a',(0,1)),('target_parent_b',(0,4)),('parents',(1,4)),
    ('ancestor_a1',(0,1,2)),('ancestor_a2',(0,1,3)),('ancestor_b1',(0,4,5)),('ancestor_b2',(0,4,6)))
EDGES=({'from':'gp_a1','to':'parent_a'},{'from':'gp_a2','to':'parent_a'},
       {'from':'gp_b1','to':'parent_b'},{'from':'gp_b2','to':'parent_b'},
       {'from':'parent_a','to':'target'},{'from':'parent_b','to':'target'})
STATUSES={'owned','borrowable','planned','unknown'}
RANKS=('G','F','E','D','C','B','A','S')
THRESHOLDS=(0,1,4,7,10)
STORE_LOCK=RLock()
ALIASES={
    'cn':'cn','国服':'cn','简中':'cn','zh-cn':'cn','china':'cn',
    'tw':'tw','台服':'tw','繁中':'tw','zh-tw':'tw','taiwan':'tw',
    'jp':'jp','日服':'jp','日本':'jp','ja':'jp','japan':'jp',
}
FACTOR_ALIASES={
    '泥地':'dirt','ダート':'dirt','dirt':'dirt','草地':'turf','芝':'turf','turf':'turf',
    '短距离':'short','短距離':'short','short':'short','sprint':'short',
    '英里':'mile','マイル':'mile','mile':'mile','中距离':'medium','中距離':'medium','middle':'medium','medium':'medium',
    '长距离':'long','長距離':'long','long':'long','领跑':'nige','領跑':'nige','逃':'nige','nige':'nige','front_runner':'nige',
    '跟前':'senko','先行':'senko','senko':'senko','pace_chaser':'senko',
    '居中':'sashi','差':'sashi','差し':'sashi','sashi':'sashi','late_surger':'sashi',
    '后追':'oikomi','後追':'oikomi','追':'oikomi','追込':'oikomi','追い込み':'oikomi','oikomi':'oikomi','end_closer':'oikomi',
}


def _normal(value):
    return ''.join(unicodedata.normalize('NFKC',str(value or '')).split()).casefold()


def _server(value):
    server=ALIASES.get(str(value or 'cn').strip().lower())
    if server is None:raise ValueError('服务器请选择国服、台服或日服。')
    return server


def _factor(value):
    value=str(value or 'dirt').split(':')[-1]
    return FACTOR_ALIASES.get(value,FACTOR_ALIASES.get(value.lower(),value))


def _identifier(value):
    if type(value)is int and value>0:return value
    if isinstance(value,str)and value.isascii()and value.isdigit()and int(value)>0:return int(value)
    return None


class Profile:
    def __init__(self,server):
        self.server=server
        self.path=DATA/f'relations-{server}.json'
        value=json.loads(self.path.read_text('utf-8-sig'))
        self.payload=value
        self.ids=frozenset(value['character_ids'])
        self.characters=[]
        self.by_id={}
        self.by_name={}
        for item in value['characters']:
            row=dict(item,name=item.get('reference_name')or item.get('japanese_name')or f"角色 #{item['character_id']}")
            row['id']=row['character_id']
            self.characters.append(row);self.by_id[row['character_id']]=row
            for name in (row['name'],row.get('japanese_name')):
                if name:self.by_name.setdefault(_normal(name),set()).add(row['character_id'])
        self.memberships={identifier:set()for identifier in self.ids}
        self.points={}
        for group in value['groups']:
            self.points[group['relation_type']]=group['points']
            for identifier in group['member_ids']:
                self.memberships.setdefault(identifier,set()).add(group['relation_type'])

    @lru_cache(maxsize=65536)
    def score(self,ids):
        if not ids or any(value not in self.ids for value in ids):return None
        if len(set(ids))!=len(ids):return 0
        common=self.memberships[ids[0]].intersection(*(self.memberships[value]for value in ids[1:]))
        return sum(self.points[group]for group in common)

    def resolve(self,value,name=None):
        identifier=_identifier(value)
        if identifier is not None:return identifier
        names=self.by_name.get(_normal(name if name else value),set())
        return next(iter(names))if len(names)==1 else None

    def metadata(self):
        return dict(server=self.server,dataset_version=self.payload['dataset_version'],
            character_count=len(self.characters),referenced_character_count=len(self.ids),group_count=len(self.points),source=deepcopy(self.payload['source']),
            coverage=deepcopy(self.payload.get('coverage',{})),source_status='community_reference',
            live_server_calibrated=False,
            limitations=['分服关系快照独立保存；页面更新时间不代表当前游戏版本已逐项校准。',
                '基础相性不包含胜场加成，不等于继承概率。'])


@lru_cache(maxsize=3)
def _profile(server):return Profile(server)


@lru_cache(maxsize=1)
def _races():
    value=json.loads((DATA/'race-catalog-reference.json').read_text('utf-8-sig'))
    return value,{row['race_id']:row for row in value['races']}


def _race_rows(value,*,planned=False):
    if isinstance(value,dict):value=value.get('wins',value.get('names',[]))
    if not isinstance(value,list):return []
    _,catalog=_races();result=[];seen=set()
    for raw in value:
        row=raw if isinstance(raw,dict)else{'name':str(raw)}
        race_id=str(row.get('race_id')or row.get('id')or row.get('event_key')or row.get('name')or row.get('raw_name')or '').strip()
        if not race_id or race_id in seen:continue
        seen.add(race_id);reference=catalog.get(race_id,{})
        status='planned'if planned else 'confirmed'if row.get('status')in{'confirmed','won'}or row.get('confirmed')is True else 'reported'
        result.append(dict(race_id=race_id,event_key=row.get('event_key')or reference.get('event_key')or race_id,
            name=row.get('name')or row.get('raw_name')or reference.get('name_cn')or reference.get('source_name')or race_id,
            grade=row.get('grade')or reference.get('grade'),status=status,
            source=row.get('source')or('user_plan'if planned else'user_import'),
            career_time=deepcopy(row.get('career_time')or reference.get('career_time'))))
    return result


def _red_fields(raw):
    result={};observed=raw.get('red_observed')is True
    values=raw.get('red_factors')
    if isinstance(values,dict):
        for name,stars in values.items():
            number=int(stars)if isinstance(stars,str)and stars.isdigit()else stars
            result[_factor(name)]=number if type(number)is int and 0<=number<=3 else None
        observed=bool(values)
    factors=raw.get('factors',[])
    if isinstance(factors,dict):factors=[dict(v,factor_key=k)if isinstance(v,dict)else{'factor_key':k,'stars':v}for k,v in factors.items()]
    if isinstance(factors,list):
        for factor in factors:
            if not isinstance(factor,dict):continue
            key=factor.get('factor_key')or factor.get('key')or factor.get('name')or factor.get('raw_name')
            kind=factor.get('kind')or factor.get('type')or str(key or '').split(':')[0]
            if kind not in {'red','pink'}and not str(key or '').startswith(('red:','pink:')):continue
            observed=True
            stars=factor.get('stars');number=int(stars)if isinstance(stars,str)and stars.isdigit()else stars
            if key:result[_factor(key)]=number if type(number)is int and 0<=number<=3 else None
    if raw.get('status')=='complete'and'red'in raw.get('scope',[]):observed=True
    return result,observed


def _node(raw,profile,*,default_status='unknown'):
    if raw is None:raw={}
    if isinstance(raw,(int,str)):raw={'character_id':raw}
    if not isinstance(raw,dict):raw={}
    own=raw.get('self')if isinstance(raw.get('self'),dict)else{}
    merged={**raw,**{key:value for key,value in own.items()if value is not None}}
    identifier=profile.resolve(merged.get('character_id',merged.get('chara_id')),merged.get('character_name')or merged.get('name'))
    reference=profile.by_id.get(identifier,{})
    availability=next((value for value in (raw.get('status'),raw.get('ownership'),raw.get('availability'),raw.get('source_kind'))if value in STATUSES),default_status)
    red,red_observed=_red_fields(merged)
    records=merged.get('race_records')
    wins=records.get('wins',[])if isinstance(records,dict)else merged.get('wins',merged.get('g1_wins',[]))
    plans=merged.get('planned_wins',merged.get('planned_races',[]))
    entry_id=str(raw.get('entry_id')or raw.get('trained_chara_id')or own.get('entry_id')or '')or None
    return dict(character_id=identifier,name=reference.get('name')or merged.get('character_name')or merged.get('name')or('未选择'if identifier is None else f'角色 #{identifier}'),
        character_name=reference.get('name')or merged.get('character_name')or merged.get('name'),
        japanese_name=reference.get('japanese_name'),name_status=reference.get('name_status','user_supplied'if merged.get('name')or merged.get('character_name')else'unknown'),
        outfit_id=str(merged.get('outfit_id')or merged.get('variant_id')or merged.get('card_id')or '')or None,
        outfit_name=merged.get('outfit_name')or merged.get('variant_name'),
        entry_id=entry_id,entry_character_id=_identifier(raw.get('entry_character_id'))or(identifier if entry_id else None),
        identity_status=raw.get('identity_status'),identity_reference=deepcopy(raw.get('identity_reference')),
        recipe_from_entry_id=raw.get('recipe_from_entry_id'),
        status=availability,source_kind=raw.get('source_kind')or availability,
        locked=raw.get('locked')is True,red_factors=red,red_observed=red_observed,
        factors=deepcopy(merged.get('factors')or[]),stats=deepcopy(merged.get('stats')or{}),
        red_target=deepcopy(raw.get('red_target')),wins=_race_rows(wins),planned_wins=_race_rows(plans,planned=True),
        wins_coverage=records.get('coverage','unknown')if isinstance(records,dict)else raw.get('wins_coverage','unknown'),
        supported=identifier in profile.ids,source=deepcopy(raw.get('source')),
        ancestry_bound_to_entry=raw.get('ancestry_bound_to_entry')is True,
        is_demo=raw.get('is_demo')is True,source_server=raw.get('source_server'),
        cross_server_reference=raw.get('cross_server_reference')is True)


def _safe_library_id(value):
    value=str(value or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',value):raise ValueError('马库编号无效；名称可另填中文。')
    return value


def _storage(storage_root):return Path(storage_root)if storage_root is not None else STORAGE


def _load_library(library_id,storage_root):
    folder=_storage(storage_root);path=folder/(_safe_library_id(library_id)+'.json')
    if not path.is_file():return None
    if path.is_symlink():raise ValueError('马库路径应为本地普通文件。')
    return json.loads(path.read_text('utf-8-sig'))


def _library(request,profile,storage_root):
    value=request.get('inventory')or request.get('library')
    if isinstance(value,dict):library=_normalize_library(value,profile,default_status=request.get('import_default_status','unknown'))
    elif isinstance(value,list):library=_normalize_library({'entries':value},profile,default_status=request.get('import_default_status','unknown'))
    elif request.get('library_id'):library=_load_library(request['library_id'],storage_root)
    else:library=None
    if not library or library.get('server')==profile.server:return library
    library=deepcopy(library)
    def reference(node):
        if not node:return
        node.update(status='unknown',source_server=library.get('server'),cross_server_reference=True,wins_coverage='unknown')
        for race in node.get('wins',[]):race['status']='reported'
    for entry in library['entries']:
        reference(entry)
        for ancestor in entry.get('parents',[]):reference(ancestor)
    library['cross_server_reference']=True
    return library


def _normalize_library(value,profile,*,default_status='unknown'):
    if isinstance(value.get('library'),dict)and not any(key in value for key in('entries','veterans','seeds')):value=value['library']
    if isinstance(value.get('results'),list)and value['results']and isinstance(value['results'][0],dict)and isinstance(value['results'][0].get('library'),dict):
        value=value['results'][0]['library']
    if default_status not in STATUSES:default_status='unknown'
    raw_entries=value.get('entries',value.get('veterans',value.get('seeds',[])))
    if not isinstance(raw_entries,list):raise ValueError('马库 JSON 应包含 entries、veterans 或 seeds 列表。')
    entries=[];seen=set();warnings=[]
    for index,raw in enumerate(raw_entries):
        if not isinstance(raw,dict):continue
        entry=_node(raw,profile,default_status=default_status)
        identifier=entry['entry_id']
        if not identifier:
            raw_identity=json.dumps(raw,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'#'+str(index)
            identifier='import-'+hashlib.sha256(raw_identity.encode('utf-8')).hexdigest()[:24]
            entry['identity_status']='generated_import_reference'
        if identifier in seen:
            original=identifier;suffix=index+1
            while identifier in seen:identifier=original+'-'+str(suffix);suffix+=1
            warnings.append(dict(code='duplicate_entry_id_preserved_separately',original_entry_id=original,new_entry_id=identifier,index=index))
        seen.add(identifier);entry['entry_id']=identifier;entry['entry_character_id']=entry['character_id']
        lineage=raw.get('when_used_as_parent')or{}
        parents=raw.get('parents')
        if not isinstance(parents,list):parents=[lineage.get('grandparent_1'),lineage.get('grandparent_2')]
        entry['parents']=[_node(item,profile)if item is not None else None for item in (parents+[None,None])[:2]]
        entry['imported']=deepcopy(raw.get('imported',raw))
        entry['imported_fields']=sorted(entry['imported'])if isinstance(entry['imported'],dict)else[]
        entry['imported_schema']=value.get('schema_version')or value.get('schema')or'generic_json'
        entries.append(entry)
    declared_server=str(value.get('server')or profile.server).strip().lower()
    declared_server=ALIASES.get(declared_server,declared_server)
    return dict(schema_version='uma.knowledge.inventory.v1',library_id=value.get('library_id'),
        name=str(value.get('name')or'本地马库'),server=declared_server,revision=int(value.get('revision')or 0),
        imported_schema=value.get('schema_version')or value.get('schema')or'generic_json',
        entries=entries,source=deepcopy(value.get('source')),import_warnings=warnings)


def _entry_view(entry,include_imported=False):
    return deepcopy(entry)if include_imported else {key:deepcopy(value)for key,value in entry.items()if key!='imported'}


def _resolve_nodes(request,profile,library=None):
    supplied=request.get('nodes')or{}
    if isinstance(supplied,list):supplied=dict(zip(NODE_KEYS,supplied))
    if not isinstance(supplied,dict):supplied={}
    if request.get('target')is not None and not supplied.get('target'):supplied=dict(supplied,target=request['target'])
    if request.get('target_character_id')is not None and not supplied.get('target'):supplied=dict(supplied,target=request['target_character_id'])
    entries={entry['entry_id']:entry for entry in (library or{}).get('entries',[])}
    resolved={};issues=[];detached_parents=set()
    for key in NODE_KEYS:
        raw=supplied.get(key)
        if isinstance(raw,dict):
            selected=_node(raw,profile)
            entry=entries.get(selected['entry_id'])
            bound_id=entry['character_id']if entry else _identifier(raw.get('entry_character_id'))
            explicit_id=next((field for field in('character_id','chara_id')if field in raw),None)
            requested_id=profile.resolve(raw[explicit_id])if explicit_id else selected['character_id']
            has_identity=explicit_id is not None or requested_id is not None
            mismatch=bound_id is not None and has_identity and requested_id!=bound_id
            new_recipe=selected['status']=='planned'and selected['entry_id']is not None and(not entry or entry['status']!='planned')
            if mismatch or new_recipe:
                # An entry identifies one finished horse, not a reusable bag of facts.
                # Replacing its character or designing a new horse detaches observed data.
                identifier=requested_id if has_identity else bound_id
                planned=selected['status']=='planned'
                reference=dict(character_id=identifier,status='planned'if planned else'unknown',locked=raw.get('locked')is True,
                    source_kind='planned_recipe'if planned else'reference_character',
                    identity_status='entry_character_mismatch'if mismatch else'new_recipe_from_entry',
                    identity_reference=dict(entry_id=selected['entry_id'],recorded_character_id=bound_id,requested_character_id=identifier))
                if planned:
                    reference['recipe_from_entry_id']=selected['entry_id']
                    for field in('red_target','planned_wins','planned_races'):
                        if field in raw:reference[field]=deepcopy(raw[field])
                    if not mismatch:
                        for field in('outfit_id','outfit_name'):
                            if selected.get(field)is not None:reference[field]=selected[field]
                if mismatch:
                    issues.append(dict(node=key,code='entry_character_mismatch',
                        message='角色与成品记录不一致；已移除原成品归属、因子、胜场和祖辈绑定，继续按角色参考计算。',
                        entry_id=selected['entry_id'],recorded_character_id=bound_id,requested_character_id=identifier))
                raw=reference;detached_parents.add(key)
            elif entry:
                raw={**entry,**raw}
        resolved[key]=_node(raw,profile)
    for side in('a','b'):
        parent_key='parent_'+side
        if parent_key not in detached_parents and resolved[parent_key]['entry_id']is not None:continue
        for index in(1,2):
            key=f'gp_{side}{index}';raw=supplied.get(key)
            if isinstance(raw,dict)and(raw.get('ancestry_bound_to_entry')is True or raw.get('source_kind')=='recorded_ancestor'):
                resolved[key]=_node({'identity_status':'detached_recorded_ancestor'},profile)
    for side in ('a','b'):
        parent=resolved['parent_'+side];entry=entries.get(parent['entry_id'])
        if not entry or parent['status']=='planned':continue
        for index,snapshot in enumerate(entry.get('parents')or[],1):
            if snapshot is None:continue
            key=f'gp_{side}{index}';actual=_node(snapshot,profile)
            if supplied.get(key)and resolved[key]['character_id']not in (None,actual['character_id']):
                issues.append(dict(node=key,code='finished_parent_ancestry_conflict',
                    message='已有成品的真实祖辈不能改写；要换祖辈请将亲代设为待培养。',requested_character_id=resolved[key]['character_id']))
            actual.update(locked=True,ancestry_bound_to_entry=True,source_kind='recorded_ancestor')
            resolved[key]=actual
    for key,label in zip(NODE_KEYS,LABELS):resolved[key].update(key=key,label=label)
    return resolved,issues


def _compatibility(nodes,profile):
    ids=[nodes[key]['character_id']for key in NODE_KEYS]
    detail=[]
    for key,indexes in COMPONENTS:
        selected=tuple(ids[index]for index in indexes)
        score=profile.score(selected)
        detail.append(dict(key=key,node_keys=[NODE_KEYS[index]for index in indexes],character_ids=list(selected),score=score,
            status='known'if score is not None else'unknown'))
    unknown=[dict(node=key,character_id=nodes[key]['character_id'],reason='not_selected'if nodes[key]['character_id']is None else'not_in_server_snapshot')
        for key in NODE_KEYS if not nodes[key]['supported']]
    known=sum(row['score']for row in detail if row['score']is not None)
    complete=not unknown
    return dict(status='complete'if complete else'partial',total=known if complete else None,known_subtotal=known,
        grade=('△'if known<51 else'〇'if known<151 else'◎')if complete else None,detail=detail,unresolved=unknown,
        model_id='base-group-intersection-v1',includes_race_bonus=False,inheritance_probability=None)


def _feasibility(nodes,extra=()):
    issues=list(extra)
    for left,right in (('target','parent_a'),('target','parent_b'),('parent_a','parent_b'),
                       ('parent_a','gp_a1'),('parent_a','gp_a2'),('parent_b','gp_b1'),('parent_b','gp_b2'),('gp_a1','gp_a2'),('gp_b1','gp_b2')):
        a,b=nodes[left]['character_id'],nodes[right]['character_id']
        if a is not None and a==b:issues.append(dict(code='same_character_selection',nodes=[left,right],message='同一次配种的这些位置不能选择同一角色。'))
    if all(nodes[key]['status']=='borrowable'for key in ('parent_a','parent_b')):
        issues.append(dict(code='two_borrowed_direct_parents',message='同一次配种只能借用一位直接亲代；未来不同分支分别计算。'))
    for side in ('a','b'):
        if nodes['parent_'+side]['status']=='planned'and all(nodes[f'gp_{side}{index}']['status']=='borrowable'for index in(1,2)):
            issues.append(dict(code='future_branch_two_borrowed_parents',node='parent_'+side,
                message='这位待培养亲代的一次配种不能同时借用两位祖辈；不同未来分支可分别借用。'))
    return dict(selectable=not issues,issues=issues,unknown_nodes=[key for key in NODE_KEYS if nodes[key]['character_id']is None],
        planned_nodes=[key for key in NODE_KEYS if nodes[key]['status']=='planned'],
        available_direct_parents=all(nodes[key]['status']in{'owned','borrowable'}and not nodes[key].get('cross_server_reference')for key in('parent_a','parent_b')),
        cross_server_reference=any(node.get('cross_server_reference')for node in nodes.values()))


def aptitude_gap(request,nodes=None):
    goal=request.get('aptitude_goal')or request.get('goal')or request
    factor=_factor(goal.get('factor_key')or goal.get('factor')or'dirt')
    baseline=str(goal.get('baseline_rank')or goal.get('base_rank')or'').upper()or None
    target=str(goal.get('target_rank')or'A').upper()
    if target not in RANKS:raise ValueError('目标适性请选择 G～S。')
    if baseline is not None and baseline not in RANKS[:-1]:raise ValueError('先天适性请选择 G～A；未知可留空。')
    unknown=0;planned=0;known=0;parts=[]
    if nodes is not None:
        for key in NODE_KEYS[1:]:
            node=nodes[key];stars=node['red_factors'].get(factor)
            if node.get('cross_server_reference'):
                unknown+=1;stars=None
            elif node['status']=='planned':
                target_red=node.get('red_target')or{}
                planned+=int(stars or(target_red.get('stars')if _factor(target_red.get('factor_key')or target_red.get('factor'))==factor else 0)or 0)
                unknown+=1;stars=None
            elif stars is None:
                if node['red_observed']and factor not in node['red_factors']:stars=0
                else:unknown+=1
            if stars is not None:known+=stars
            parts.append(dict(node=key,confirmed_stars=stars,availability=node['status']))
    else:
        value=goal.get('known_stars',0)
        if type(value)is int and 0<=value<=18:known=value
        else:known=None
        unknown=int(goal.get('unknown_nodes')or 0)
    required=None;maximum=None;initial=baseline
    if baseline is not None:
        b=RANKS.index(baseline);t=RANKS.index(target)
        maximum=RANKS[min(6,b+4)]
        if t<=b:required=0
        elif t<=6 and t-b<=4:required=THRESHOLDS[t-b]
        if known is not None:
            raises=max(index for index,threshold in enumerate(THRESHOLDS)if known>=threshold)
            initial=RANKS[min(6,b+raises)]
    gap=max(0,required-known)if required is not None and known is not None else None
    status='unknown_baseline'if baseline is None else'unreachable_at_start'if required is None else'satisfied'if gap==0 else'partial'if unknown or known is None else'missing'
    return dict(factor=factor,baseline_rank=baseline,target_rank=target,status=status,required_stars=required,known_stars=known,
        unknown_nodes=unknown,planned_target_stars=planned,gap=gap,additional_stars=gap,confirmed_start_rank_lower_bound=initial,max_start_rank=maximum,
        node_contributions=parts,model_id='starting-red-stars-reference-v1',inheritance_probability=None,
        message='开局红星为确定性参考，最多提升四级且最高A；计划目标星数不计作已有星数，S需后续继承。')


def _common_wins(nodes):
    links=[]
    for a,b in (('parent_a','parent_b'),('parent_a','gp_a1'),('parent_a','gp_a2'),('parent_b','gp_b1'),('parent_b','gp_b2')):
        actual=[];reported=[]
        left={row['event_key']:row for row in nodes[a]['wins']};right={row['event_key']:row for row in nodes[b]['wins']}
        for key in sorted(left.keys()&right.keys()):
            row=dict(event_key=key,name=left[key]['name'],grade=left[key]['grade']or right[key]['grade'])
            (actual if left[key]['status']==right[key]['status']=='confirmed'else reported).append(row)
        links.append(dict(left=a,right=b,confirmed_common=actual,reported_common=reported,
            confirmed_g1_count=sum(row['grade']=='G1'for row in actual),
            exact=nodes[a]['wins_coverage']==nodes[b]['wins_coverage']=='complete'))
    return dict(links=links,confirmed_g1_link_count=sum(row['confirmed_g1_count']for row in links),
        compatibility_bonus=None,inheritance_probability=None,scope='observed_separate_from_planned',
        message='只对已确认且身份对应的胜场取交集；未记录不等于零胜场，计划赛程不计作已赢。共同加成规则未逐服核准，暂不伪造精确加成。')


def _probability_reference(request,nodes,profile):
    model=request.get('probability_model')
    if not model:return None
    if model!='legacy-modern-reference-v1':
        return dict(status='unknown_model',model_id=str(model),probability_goal=None,
            message='请选择已列出的参考模型；不把未知模型替换成当前服官方概率。')
    goal=request.get('aptitude_goal')or request.get('goal')or{}
    factor=_factor(goal.get('factor_key')or goal.get('factor')or'dirt')
    a,b=nodes['parent_a']['character_id'],nodes['parent_b']['character_id'];target=nodes['target']['character_id']
    pair=profile.score((a,b))or 0
    affinity={}
    for side,parent in (('a',a),('b',b)):
        parent_key='parent_'+side
        triples=[profile.score((target,parent,nodes[f'gp_{side}{index}']['character_id']))or 0 for index in(1,2)]
        affinity[parent_key]=(profile.score((target,parent))or 0)+pair+sum(triples)
        for index,value in enumerate(triples,1):affinity[f'gp_{side}{index}']=value
    common=_common_wins(nodes)
    include_g1=request.get('include_reference_g1')is True
    if include_g1:
        for link in common['links']:
            bonus=3*link['confirmed_g1_count']
            affinity[link['left']]+=bonus;affinity[link['right']]+=bonus
    distribution=[1.0];carriers=[];known=0;unknown=0;conditional=False
    for key in NODE_KEYS[1:]:
        node=nodes[key];stars=_known_stars(node,factor)
        if stars is None and node['status']=='planned'and request.get('forecast_planned')is True:
            red_target=node.get('red_target')or{}
            stars=node['red_factors'].get(factor)
            if stars is None and _factor(red_target.get('factor_key')or red_target.get('factor'))==factor:stars=red_target.get('stars')
            conditional=conditional or stars is not None
        if type(stars)is not int or not 0<=stars<=3:stars=None;unknown+=1
        else:known+=stars
        base={0:0.0,1:.01,2:.03,3:.05}.get(stars,0.0)
        rate=min(1.0,base*(1+max(0,affinity[key])/100))
        carriers.append(dict(node=key,stars=stars,individual_affinity=affinity[key],base_rate=base,event_probability=rate,
            status='unknown_omitted'if stars is None else'planned_condition'if node['status']=='planned'else'reported_factor'))
        for _ in range(2):
            updated=[0.0]*(len(distribution)+1)
            for index,mass in enumerate(distribution):
                updated[index]+=mass*(1-rate);updated[index+1]+=mass*rate
            distribution=updated
    base_rank=str(goal.get('baseline_rank')or goal.get('base_rank')or'').upper()
    initial=None
    if base_rank in RANKS[:-1]:
        raises=max(index for index,threshold in enumerate(THRESHOLDS)if known>=threshold)
        initial=min(6,RANKS.index(base_rank)+raises)
    def at_least(rank):
        if initial is None:return None
        return sum(distribution[max(0,RANKS.index(rank)-initial):])
    target_rank=str(goal.get('target_rank')or'A').upper()
    return dict(model_id=model,status='conditional_reference'if conditional else'partial_reference_lower_bound'if unknown else'reference_estimate',
        server=profile.server,dataset_version=profile.payload['dataset_version'],live_server_calibrated=False,
        probability_A=at_least('A'),probability_S=at_least('S'),probability_goal=at_least(target_rank)if target_rank in RANKS else None,
        initial_rank=RANKS[initial]if initial is not None else None,target_rank=target_rank,known_stars=known,unknown_nodes=unknown,
        distribution=distribution,carrier_rates=carriers,includes_assumed_G1_bonus=include_g1,
        assumptions=['参考模型基础率1%/3%/5%，按每载体相性线性放大，最多1。',
            '两次继承，每因子每次独立，单次仅升一级；忽略罕见多级提升。',
            '未知因子不凭空补齐，部分资料只给已知部分的模型下界。',
            '计划目标仅在显式forecast_planned时给达成条件下的假设结果，不写为实际因子。',
            '共同G1只有显式启用时才按每链接+3的现代参考规则加入，并非已验证的当前服机制。'],
        source={'url':'https://github.com/Rom1foucher/UmaLegacyLinker/blob/c8df8fc953c041c4a194e2ec488cbfe6ab2d21c4/docs/SCORING.md',
            'scope':'mathematical reference model; independently implemented, not imported upstream code'})


def _evaluate(request,profile,library=None,nodes_override=None):
    nodes,issues=_resolve_nodes(dict(request,nodes=nodes_override)if nodes_override is not None else request,profile,library)
    result=dict(profile=profile.metadata(),tree=dict(nodes=[deepcopy(nodes[key])for key in NODE_KEYS],edges=deepcopy(EDGES)),
        nodes=deepcopy(nodes),compatibility=_compatibility(nodes,profile),feasibility=_feasibility(nodes,issues),
        common_wins=_common_wins(nodes),aptitude=aptitude_gap(request,nodes),probability=_probability_reference(request,nodes,profile),tap_or_purchase_authorized=False)
    result['status']=result['compatibility']['status']
    return result


def _save_library(request,profile,storage_root):
    value=request.get('inventory')or request.get('library')or request.get('data')or{}
    if not value and isinstance(request.get('entries'),list):value={'entries':request['entries'],'name':request.get('name')or'本地马库'}
    if isinstance(value,list):value={'entries':value}
    if not isinstance(value,dict):raise ValueError('请提供马库 JSON 对象或成品列表。')
    if request.get('demo')is True:value=json.loads((DATA/'demo-inventory.json').read_text('utf-8'))
    library=_normalize_library(value,profile,default_status=request.get('import_default_status','unknown'))
    identifier=_safe_library_id(request.get('library_id')or('library-'+uuid.uuid4().hex))
    folder=_storage(storage_root)
    if folder.exists()and folder.is_symlink():raise ValueError('马库目录不能是符号链接。')
    folder.mkdir(parents=True,exist_ok=True);path=folder/(identifier+'.json')
    with STORE_LOCK:
        prior=_load_library(identifier,storage_root)
        expected=request.get('expected_revision')
        if expected is not None and int(expected)!=(prior or{}).get('revision',0):
            return dict(ok=False,error='inventory_conflict',message='马库已更新，请刷新后保存。',current_revision=(prior or{}).get('revision',0))
        if request.get('mode')=='append'and prior:
            by_id={row['entry_id']:row for row in prior['entries']}
            by_id.update({row['entry_id']:row for row in library['entries']});library['entries']=list(by_id.values())
        library.update(library_id=identifier,revision=(prior or{}).get('revision',0)+1,
            updated_at=datetime.now(timezone.utc).isoformat(),imported_library_id=value.get('library_id'))
        temporary=folder/(identifier+'.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(library,ensure_ascii=False,indent=2)+'\n','utf-8');os.replace(temporary,path)
    return dict(library_id=identifier,revision=library['revision'],name=library['name'],server=library['server'],
        entries=[_entry_view(entry)for entry in library['entries']],entry_count=len(library['entries']),
        imported_schema=library['imported_schema'],imported_fields_preserved=True,import_warnings=library['import_warnings'],real_existing_account_library_read=False)


def _list_inventory(request,profile,storage_root):
    if request.get('library_id'):
        value=_load_library(request['library_id'],storage_root)
        if value is None:return dict(library_id=request['library_id'],entries=[],entry_count=0,status='not_found')
        return dict(value,entries=[_entry_view(entry,request.get('include_imported')is True)for entry in value['entries']],entry_count=len(value['entries']))
    folder=_storage(storage_root);rows=[]
    if folder.exists():
        for path in sorted(folder.glob('*.json')):
            if path.is_symlink():continue
            try:value=json.loads(path.read_text('utf-8-sig'))
            except (ValueError,OSError):continue
            if value.get('schema_version')!='uma.knowledge.inventory.v1':continue
            rows.append({key:value.get(key)for key in ('library_id','name','server','revision','updated_at')})
            rows[-1]['entry_count']=len(value.get('entries',[]))
    return dict(libraries=rows,library_count=len(rows),selected_profile=profile.metadata())


def _known_stars(node,factor):
    if node['status']=='planned'or node.get('cross_server_reference'):return None
    if factor in node['red_factors']:return node['red_factors'][factor]
    return 0 if node['red_observed']else None


def _suggest(request,profile,library=None):
    initial,initial_issues=_resolve_nodes(request,profile,library)
    replace=request.get('replace_unlocked')is True
    def mutable(key):
        node=initial[key]
        return key!='target'and not node['locked']and not node['ancestry_bound_to_entry']and(replace or node['character_id']is None)
    explicit=request.get('candidate_character_ids')
    ids=[profile.resolve(value)for value in explicit]if isinstance(explicit,list)else[row['character_id']for row in profile.characters]
    ids=sorted({identifier for identifier in ids if identifier in profile.ids})
    status_filter=request.get('statuses')or STATUSES
    statuses={status_filter}if isinstance(status_filter,str)else set(status_filter)
    entries=[entry for entry in (library or{}).get('entries',[])if entry['status']in statuses and entry['character_id']in profile.ids]
    if request.get('available_only')is True:entries=[entry for entry in entries if entry['status']in{'owned','borrowable'}]
    reference=[_node({'character_id':identifier,'status':'planned','source_kind':'reference_candidate'},profile)for identifier in ids]
    inventory_nodes=[_node(entry,profile)for entry in entries]
    use_library=request.get('mode')!='characters'and bool(inventory_nodes)
    parent_pool=inventory_nodes if use_library else reference
    target=initial['target']['character_id']
    goal=request.get('aptitude_goal')or request.get('goal')or{}
    factor=_factor(goal.get('factor_key')or goal.get('factor')or'dirt')
    availability={'owned':0,'borrowable':1,'unknown':2,'planned':3}
    by_entry={entry['entry_id']:entry for entry in entries}
    branch_cache={}

    def best_branch(side,parent):
        cache_key=(side,parent.get('entry_id'),parent['character_id'],parent['status'])
        if cache_key in branch_cache:return deepcopy(branch_cache[cache_key])
        a,b=f'gp_{side}1',f'gp_{side}2'
        entry=by_entry.get(parent.get('entry_id'))
        if entry and parent['status']!='planned':
            supplied=entry.get('parents')or[None,None]
            result=[]
            for key,snapshot in zip((a,b),(supplied+[None,None])[:2]):
                if snapshot is None:result.append(deepcopy(initial[key]));continue
                node=_node(snapshot,profile);node.update(locked=True,ancestry_bound_to_entry=True,source_kind='recorded_ancestor')
                result.append(node)
            branch_cache[cache_key]=result;return deepcopy(result)
        pool=inventory_nodes if use_library else reference
        # Recipes may use reported inventory material. If that pool cannot fill
        # two different characters, reference material remains an explicit plan.
        if len({node['character_id']for node in pool if node['character_id']!=parent['character_id']})<2:
            pool=pool+reference
        def choices(key):
            if not mutable(key):return[deepcopy(initial[key])]
            by_character={}
            for node in pool:
                identifier=node['character_id']
                if identifier==parent['character_id']:continue
                score=profile.score(tuple(sorted((target,parent['character_id'],identifier),key=lambda value:-1 if value is None else value)))
                rank=(-(int(_known_stars(node,factor)or 0))if goal else 0,-(score or 0),availability[node['status']],node.get('entry_id')or'')
                identity=(identifier,node['status']=='borrowable')
                if identity not in by_character or rank<by_character[identity][0]:by_character[identity]=(rank,node)
            return [deepcopy(row[1])for row in sorted(by_character.values(),key=lambda row:row[0])]
        left,right=choices(a),choices(b)
        def single_rank(node):
            score=profile.score(tuple(sorted((target,parent['character_id'],node['character_id']),key=lambda value:-1 if value is None else value)))or 0
            return (-int(_known_stars(node,factor)or 0)if goal else 0,-score,availability[node['status']])
        right=sorted(right,key=lambda node:(*single_rank(node),str(node['character_id']),node.get('entry_id')or''))
        non_borrowed=[node for node in right if node['status']!='borrowable']
        best=None
        for gp1 in left:
            eligible=non_borrowed if gp1['status']=='borrowable'else right
            gp2=next((node for node in eligible if gp1['character_id']is None or node['character_id']!=gp1['character_id']),None)
            if gp2 is None:continue
            r1,r2=single_rank(gp1),single_rank(gp2)
            rank=tuple(r1[index]+r2[index]for index in range(3))+(str(gp1['character_id']),str(gp2['character_id']))
            if best is None or rank<best[0]:best=(rank,gp1,gp2)
        if best:
            _,x,y=best;result=[x,y]
        else:result=[deepcopy(initial[a]),deepcopy(initial[b])]
        branch_cache[cache_key]=result;return deepcopy(result)

    def options(side):
        key='parent_'+side
        if not mutable(key):return[deepcopy(initial[key])]
        pool=[deepcopy(node)for node in parent_pool if node['character_id']!=target]
        if use_library and len({node['character_id']for node in pool})<2:
            pool+= [deepcopy(node)for node in reference if node['character_id']!=target]
        return pool or[deepcopy(initial[key])]

    left,right=options('a'),options('b')
    count=0;excluded=0
    max_pairs=max(1,int(request.get('max_pairs')or 250_000));truncated=False
    def values():
        nonlocal count,excluded,truncated
        for p1,p2 in product(left,right):
            if count>=max_pairs:truncated=True;break
            if p1['character_id']is not None and(p1['character_id']==p2['character_id']or p1['character_id']==target):excluded+=1;continue
            if p2['character_id']is not None and p2['character_id']==target:excluded+=1;continue
            if p1['status']==p2['status']=='borrowable':excluded+=1;continue
            gp1,gp2=best_branch('a',p1);gp3,gp4=best_branch('b',p2)
            nodes={'target':initial['target'],'parent_a':p1,'gp_a1':gp1,'gp_a2':gp2,'parent_b':p2,'gp_b1':gp3,'gp_b2':gp4}
            compatibility=_compatibility(nodes,profile);feasible=_feasibility(nodes,initial_issues)
            known_stars=sum(int(_known_stars(nodes[key],factor)or 0)for key in NODE_KEYS[1:])
            score=compatibility['known_subtotal'];unknown=len(compatibility['unresolved'])
            unavailable=sum(nodes[key]['status']not in{'owned','borrowable'}for key in('parent_a','parent_b'))
            rank=(not feasible['selectable'],-known_stars if goal else 0,unavailable if use_library else 0,-score,unknown,
                sum(availability[nodes[key]['status']]for key in NODE_KEYS[1:]),str(p1.get('entry_id')or p1['character_id']),str(p2.get('entry_id')or p2['character_id']))
            count+=1
            yield rank,nodes
    limit=max(1,min(100,int(request.get('limit')or 10)))
    selected=nsmallest(limit,values(),key=lambda row:row[0])
    suggestions=[]
    for _,nodes in selected:
        result=_evaluate(request,profile,library,nodes)
        result['ranking']=dict(scope='inventory_and_explicit_reference_recipes'if use_library else'base_relation_reference_candidates',
            confirmed_goal_stars=sum(int(_known_stars(node,factor)or 0)for key,node in result['nodes'].items()if key!='target'),
            global_inheritance_optimum_claimed=False)
        suggestions.append(result)
    return dict(profile=profile.metadata(),suggestions=suggestions,best_nodes=deepcopy(suggestions[0]['nodes'])if suggestions else{},
        evaluated_pairs=count,excluded_pairs=excluded,truncated=truncated,
        search_scope='current_inventory'if use_library else'server_reference_roster',
        locked_nodes_preserved=True,has_target=target is not None,
        message='先按已确认红星目标和基础相性比较；未保存的祖辈不伪装成已有成品，参考补祖会标为待培养。'+('本次达到组合预算，结果为已搜索范围内参考。'if truncated else''))


def _plan_races(request,profile,library=None):
    nodes,_=_resolve_nodes(request,profile,library)
    catalog,_=_races();raw=request.get('races')
    if not isinstance(raw,list):
        selected=set(str(value)for value in request.get('race_ids',[]))
        candidates=[deepcopy(row)for row in catalog['races']if row['grade']=='G1'and(not selected or row['race_id']in selected or row['event_key']in selected)]
    else:
        candidates=[]
        for value in raw:
            if not isinstance(value,dict):continue
            identifier=str(value.get('race_id')or value.get('id')or value.get('name')or'')
            if not identifier:continue
            candidates.append(dict(value,race_id=identifier,event_key=value.get('event_key')or identifier,
                source_name=value.get('name')or value.get('source_name')or identifier,grade=value.get('grade','G1')))
    grouped={}
    for race in candidates:
        if request.get('surface')and race.get('surface')!=request['surface']:continue
        if request.get('distance_category')and race.get('distance_category')!=request['distance_category']:continue
        if request.get('distance_m')and race.get('distance_m')!=int(request['distance_m']):continue
        if request.get('venue')and race.get('venue')!=request['venue']:continue
        grouped.setdefault(race['event_key'],[]).append(race)
    requested_nodes=request.get('plan_for_nodes')or list(NODE_KEYS[1:])
    requested_nodes=[key for key in requested_nodes if key in NODE_KEYS[1:]]
    rows=[]
    for key,variants in grouped.items():
        first=variants[0];confirmed=[];planned=[];unconfirmed=[]
        for node_key in requested_nodes:
            node=nodes[node_key]
            if any(win['event_key']==key and win['status']=='confirmed'for win in node['wins']):confirmed.append(node_key)
            else:unconfirmed.append(node_key)
            if any(win['event_key']==key for win in node['planned_wins']):planned.append(node_key)
        rows.append(dict(event_key=key,name=first.get('name_cn')or first.get('source_name')or first.get('name')or key,
            grade=first.get('grade'),confirmed_nodes=confirmed,not_confirmed_nodes=unconfirmed,already_planned_nodes=planned,
            target_nodes=list(requested_nodes),variants=variants,reference_only=True))
    rows.sort(key=lambda row:(not row['not_confirmed_nodes'],-len(row['confirmed_nodes']),len(row['not_confirmed_nodes']),row['event_key']))
    limit=max(1,min(200,int(request.get('limit')or 20)));rows=rows[:limit]
    calendar_allowed=profile.server=='cn'or request.get('allow_reference_calendar')is True or isinstance(raw,list)
    schedules={key:[]for key in requested_nodes};conflicts=[]
    for node_key in requested_nodes:
        occupied=set()
        for row in rows:
            if node_key in row['confirmed_nodes']:continue
            slots=[]
            if calendar_allowed:
                for variant in row['variants']:
                    time=variant.get('career_time')or{}
                    year=time.get('year_index',time.get('year'));month=time.get('month');half=time.get('half')
                    if year in(1,2,3)and type(month)is int and 1<=month<=12 and half in('first','second',1,2):
                        slots.append((year,month,1 if half in('first',1)else 2,variant))
            chosen=next((slot for slot in sorted(slots,key=lambda item:item[:3])if slot[:3]not in occupied),None)
            if chosen:
                occupied.add(chosen[:3]);schedules[node_key].append(dict(event_key=row['event_key'],name=row['name'],
                    year=chosen[0],month=chosen[1],half='first'if chosen[2]==1 else'second',race_id=chosen[3]['race_id'],status='planned_reference',
                    finished_seed_immutable=bool(nodes[node_key]['entry_id']and nodes[node_key]['status']!='planned'),
                    action_scope='verify_existing_then_new_training_recipe'if nodes[node_key]['entry_id']and nodes[node_key]['status']!='planned'else'planned_training'))
            elif slots:conflicts.append(dict(node=node_key,event_key=row['event_key'],reason='same_half_month_conflict'))
    return dict(profile=profile.metadata(),races=rows,plans=schedules,conflicts=conflicts,
        common_wins=_common_wins(nodes),calendar_source=deepcopy(catalog['source']),calendar_server='cn',
        calendar_applicable=calendar_allowed,compatibility_bonus=None,inheritance_probability=None,
        optimality_claimed=False,planned_wins_became_observed=False,
        message='共同G1候选与半月参考排程；缺确认记录不代表没赢。已有成品不能补跑，先核对胜场，确实缺少则另育成。这里不写入实际胜场，也不保证比赛获胜或角色目标赛程可行。'+
            (''if calendar_allowed else'当前仅有国服参考赛历，本服先列候选，不静默套用国服日期。'))


def dispatch(request,*,storage_root=None):
    if not isinstance(request,dict):return dict(ok=False,error='invalid_request',message='请求应为JSON对象。')
    try:
        action=str(request.get('action')or'parents.profiles');profile=_profile(_server(request.get('server')))
        if action=='parents.profiles':
            result=dict(profiles=[_profile(server).metadata()for server in ('cn','tw','jp')],selected_profile=profile.metadata(),characters=deepcopy(profile.characters),
                probability_models=[dict(model_id='legacy-modern-reference-v1',name='现代继承参考模型（非官方实测）',base_rates=[.01,.03,.05],default_enabled=False)])
        elif action in {'inventory.import','inventory.save'}:result=_save_library(request,profile,storage_root)
        elif action in {'inventory.list','inventory.export'}:result=_list_inventory(dict(request,include_imported=action=='inventory.export'or request.get('include_imported')is True),profile,storage_root)
        elif action=='inventory.demo':result=dict(inventory=json.loads((DATA/'demo-inventory.json').read_text('utf-8')),saved=False,is_demo=True)
        elif action=='aptitude.gap':
            nodes,_=_resolve_nodes(request,profile,_library(request,profile,storage_root))if request.get('nodes')else(None,[])
            result=aptitude_gap(request,nodes)
        elif action=='parents.evaluate':result=_evaluate(request,profile,_library(request,profile,storage_root))
        elif action=='parents.suggest':result=_suggest(request,profile,_library(request,profile,storage_root))
        elif action=='races.plan':result=_plan_races(request,profile,_library(request,profile,storage_root))
        else:return dict(ok=False,error='unknown_action',message='未识别的继承工具动作。')
        return {'ok':True,'action':action,**result}
    except (ValueError,TypeError,KeyError,OSError)as error:
        return dict(ok=False,error='parents_input_error',message=str(error))
