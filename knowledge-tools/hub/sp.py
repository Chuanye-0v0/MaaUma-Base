"""Deterministic, explained SP quotes. No learning or game operations."""
from copy import deepcopy
from . import knowledge

HINT_DISCOUNTS={0:0,1:10,2:20,3:30,4:35,5:40}

def _hint(value):
    if isinstance(value,str)and value.isdigit():value=int(value)
    return value if type(value)is int and value in HINT_DISCOUNTS else None

def _step_price(c,sid,server):
    if server=='jp'and sid in c.engine_meta:
        value=c.engine_meta[sid].get('baseCost')
        if type(value)is int:return value,'kachi_jp_step_cost_reference'
    row=c.rich_by_db.get(sid)or c.ref.get(sid)or{}
    value=row.get('upgrade_cost')
    if type(value)is int:
        return value,'historical_cn_step_cost'if server=='cn'else'historical_cn_cost_cross_server_reference'
    if server=='jp'and sid in c.wiki['jp']['costs']:return c.wiki['jp']['costs'][sid],'bwiki_jp_step_cost_reference'
    if sid in c.engine_meta:
        value=c.engine_meta[sid].get('baseCost')
        if type(value)is int:return value,'jp_cost_cross_server_reference'
    return None,'price_missing'

def _chain(c,sid,server):
    row=c.rich_by_db.get(sid)
    if row and row.get('cost',{}).get('chain'):
        return[c.internal.get(value,value)for value in row['cost']['chain']],'historical_verified_id_chain'
    kind=c.skill_kind(sid,server)
    if kind=='inherited_unique':return[sid],'standalone_inherited_unique'
    raw=c.wiki.get(server,{}).get('data',{}).get(sid)
    if raw and kind=='purchasable':
        group=raw.get('group_id');rate=knowledge._int(raw.get('group_rate'))
        rows=[r for r in c.wiki[server]['data'].values()if r.get('group_id')==group and 0<knowledge._int(r.get('group_rate'))<=rate and knowledge._int(r.get('rarity'))in{1,2}]
        rates=[knowledge._int(r.get('group_rate'))for r in rows]
        if len(rates)==len(set(rates))and rows:return[r['id']for r in sorted(rows,key=lambda r:knowledge._int(r.get('group_rate')))],'selected_wiki_group_rate_chain'
    meta=c.engine_meta.get(sid)
    if meta and kind=='purchasable':
        group=meta.get('groupId');same=[key for key,item in c.engine_meta.items()if item.get('groupId')==group and c.skill_kind(key,'jp')=='purchasable']
        if len(same)==1:return[sid],'single_known_group_member_reference'
    return None,'lower_skill_chain_missing'

def dispatch(request):
    if not isinstance(request,dict):return dict(ok=False,error='request_must_be_object')
    action=request.get('action','sp.quote')
    if action not in{'sp.quote','sp.rules'}:return dict(ok=False,error='unknown_sp_action')
    if action=='sp.rules':return dict(ok=True,hint_discounts=HINT_DISCOUNTS,cutting_edge_discount_percent=10,
        discount_combination='additive_per_step',lower_skills_have_individual_hint_levels=True,
        displayed_final_cost_not_discounted_again=True,noninteger_rounding='preserve_floor_ceil_interval')
    c=knowledge.catalog();server=knowledge.server_id(request.get('server'));wanted=request.get('skills',[])
    if not isinstance(wanted,list):return dict(ok=False,error='skills_must_be_list',message='技能清单应为列表。')
    warnings=[];learned=set();learned_unresolved=[]
    explicit_learned=request.get('learned',request.get('owned',[]))
    if not isinstance(explicit_learned,list):explicit_learned=[];warnings.append('已学技能列表格式不明，未据此减价。')
    for identity in explicit_learned:
        ids=c.resolve_skills(identity)
        if len(ids)==1:learned.add(ids[0])
        else:learned_unresolved.append(identity)
    rows=[];selected_ids=set();hints={}
    for value in wanted:
        row=deepcopy(value)if isinstance(value,dict)else dict(skill_id=value)
        identity=row.get('skill_id',row.get('db_id',row.get('id',row.get('name',''))));ids=c.resolve_skills(identity)
        row['_identity']=identity;row['_ids']=ids
        if len(ids)==1:
            row['_sid']=ids[0];selected_ids.add(ids[0])
            if row.get('learned')is True:learned.add(ids[0])
            hint=_hint(row.get('hint_level',0))
            if hint is not None:hints[ids[0]]=hint
        rows.append(row)
    for identity,hint in (request.get('hint_levels')or{}).items()if isinstance(request.get('hint_levels',{}),dict)else[]:
        ids=c.resolve_skills(identity);hint=_hint(hint)
        if len(ids)==1 and hint is not None:hints[ids[0]]=hint
        else:warnings.append('一个提示等级或技能编号无法确认，未用于折扣。')
    cut=request.get('cutting_edge',request.get('sharp',False))
    if type(cut)is not bool:warnings.append('切者开关不明确，未追加折扣。');cut=False
    extra=request.get('extra_discount_percent',0)
    if type(extra)is not int or not 0<=extra<=60:warnings.append('额外折扣不明确，未追加折扣。');extra=0
    extra+=10 if cut else 0
    superseded=set()
    for sid in learned:
        superseded.update(c.relations[sid].get('supersedes',[]))
        chain,_=_chain(c,sid,server)
        if chain:superseded.update(chain[:-1])
    selected_chains={sid:_chain(c,sid,server)[0]for sid in selected_ids}
    covered_selected={part for sid,chain in selected_chains.items()if chain for part in chain[:-1]if part in selected_ids}
    charged_steps=set();seen_targets=set();items=[];known_low=known_high=0;unknown=False
    for row in rows:
        ids=row['_ids'];identity=row['_identity']
        if len(ids)!=1:
            unknown=True;items.append(dict(input=identity,status='ambiguous'if ids else'not_found',candidates=[c.skill_item(sid,server)for sid in ids],cost=None,message='请按技能编号区分同名原生／继承或其他技能。'));continue
        sid=ids[0];item=c.skill_item(sid,server);item.update(hint_level=row.get('hint_level',0),cost=None,steps=[])
        if sid in seen_targets:item.update(status='duplicate_selection',cost=dict(low=0,high=0),message='同一技能编号仅计费一次。');items.append(item);continue
        seen_targets.add(sid)
        if sid in learned or sid in superseded:
            item.update(status='already_learned'if sid in learned else'covered_by_learned_upper',cost=dict(low=0,high=0));items.append(item);continue
        if sid in covered_selected:
            item.update(status='included_in_selected_upper_chain',cost=dict(low=0,high=0),message='该下位已在所选上位的费用链中计算。');items.append(item);continue
        hint=_hint(row.get('hint_level',hints.get(sid,0)))
        if hint is None:
            unknown=True;item.update(status='invalid_hint_level',message='提示等级应为0～5；未猜测该项折扣。');items.append(item);continue
        displayed=row.get('displayed_final_cost',row.get('final_cost'))
        if displayed is not None:
            if type(displayed)is not int or displayed<0:
                unknown=True;item.update(status='invalid_displayed_price',message='当前画面最终价应为非负整数。');items.append(item);continue
            item.update(status='observed_final_price',cost=dict(low=displayed,high=displayed),cost_basis='observed_final_not_discounted_again',discount_applied_again=False)
            known_low+=displayed;known_high+=displayed
            chain=selected_chains.get(sid)
            if chain:charged_steps.update(chain)
            items.append(item);continue
        if item['kind']in{'native_unique','evolved','event_bonus','story','automatic_special'}:
            item.update(status='not_a_pt_purchase',cost=dict(low=0,high=0),message='该版本不是普通Pt购买项；不把固有／进化条件当作免费学习。');items.append(item);continue
        if item['kind']=='negative':
            unknown=True;item.update(status='negative_removal_not_quoted',message='负面技能的移除不是普通购买，当前清单未指定移除操作。');items.append(item);continue
        chain,basis=_chain(c,sid,server)
        if not chain:
            unknown=True;item.update(status='lower_chain_unknown',chain_basis=basis,message='下位费用链未确认，保留未知，不只拿上位单级价当总价。');items.append(item);continue
        low=high=0;steps=[];missing=[];step_pending=[]
        for part in chain:
            if part in learned or part in superseded or part in charged_steps:continue
            price,source=_step_price(c,part,server)
            if price is None:missing.append(part);continue
            part_hint=hint if part==sid else hints.get(part,0)
            discount=HINT_DISCOUNTS[part_hint]+extra
            if discount>100:missing.append(part);warnings.append('总折扣超过100%，该项未作负价格计算。');continue
            numerator=price*(100-discount);a,b=numerator//100,(numerator+99)//100
            low+=a;high+=b;step_pending.append(part)
            steps.append(dict(db_id=part,name=c.skill_item(part,server)['name'],base_step_pt=price,hint_level=part_hint,discount_percent=discount,cost=dict(low=a,high=b),unrounded_pt=numerator/100,source=source))
        item.update(steps=steps,chain_basis=basis,missing_price_ids=missing)
        if missing:
            unknown=True;item.update(status='price_unknown',known_subtotal=dict(low=low,high=high),message='部分单级价格未知，未给出假精确总价。')
        else:
            item.update(status='reference_estimate',cost=dict(low=low,high=high),rounding_verified=low==high,
                message='按每级提示和切者相加估算；非整数保留上下界，实际画面价优先。')
            if any('cross_server'in step['source']for step in steps):
                item['status']='cross_server_reference';item['message']='该项包含跨服历史／日服参考价格，不代表所选服务器当前价格或可学状态。'
                warnings.append(item['name']+'：使用了跨服参考费用，请以该服实际画面价格为准。')
            charged_steps.update(step_pending);known_low+=low;known_high+=high
        items.append(item)
    if learned_unresolved:warnings.append('部分已学名称不唯一或未收录，未擅自扣除对应费用。')
    if server!='cn':warnings.append('报价保留所用日服／历史参考来源，不代表已用该服当前现场价格校准。')
    total=dict(low=None if unknown else known_low,high=None if unknown else known_high,exact=not unknown and known_low==known_high,
        status='partial_unknown'if unknown else'empty'if not items else'reference_estimate',known_subtotal=dict(low=known_low,high=known_high))
    result=dict(ok=True,items=items,total=total,server=server,cutting_edge=cut,warnings=warnings,learned_unresolved=learned_unresolved,
        assumptions=dict(hint_discounts=HINT_DISCOUNTS,cutting_edge_extra_percent=10,per_step_additive_discount=True,
            unspecified_lower_hint_level=0,current_prices_or_actual_learnability_not_inferred=True,total_exact_means_arithmetic_not_live_price_verification=True),dataset=c.metadata(),
        message='此为费用计算，不代表当前已获得全部技能提示或自动执行技能学习。')
    if request.get('card_ids')is not None or request.get('cards')is not None:
        result['card_opportunities']=c.support_sources(dict(action='support.deck',server=server,card_ids=request.get('card_ids',request.get('cards')),limit=200))
    return result
