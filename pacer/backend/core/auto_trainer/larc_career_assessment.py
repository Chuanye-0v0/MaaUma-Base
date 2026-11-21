"""Advisory checkpoint assessment of an existing review; never a run command.

Forecasts are the recorded policy's scenarios, not confidence intervals. Each
checkpoint uses only its own snapshot and earlier observations. Thresholds are
visible prototype preferences, not calibrated minimum requirements for a horse.
"""
import math

STATS=('speed','stamina','power','guts','wisdom')
NAMES=dict(zip(STATS,('速度','耐力','力量','毅力','智力')))
CHECKPOINTS=(
    ('junior_end','第一年年底',23,24,'latest','第二年继承后'),
    ('classic_inheritance','第二年继承观察点',32,33,'first','首次远征结束'),
    ('first_arc_return','首次远征回国后',43,44,'first','第二次远征前'),
    ('second_expedition','第二次远征前',59,60,'latest','完整养成'),
)
THRESHOLDS={'continue_mean_target_ratio':.9,'continue_min_target_ratio':.75,
            'late_shortfall_high_ratio':.8,'repeated_shortfall_attributes':2,
            'startup_ready_share':.6,'startup_mean_target_ratio':.8}


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _date(period):
    p=period if isinstance(period,dict) else {}
    year,month,half=p.get('year_index'),p.get('month'),p.get('half')
    if (type(year) is not int or year not in (1,2,3) or type(month) is not int
            or not 1<=month<=12 or half not in ('first','second') or p.get('pre_debut')):
        return None
    return (year-1)*24+(month-1)*2+(1 if half=='first' else 2)


def date_label(period):
    p=period if isinstance(period,dict) else {}
    if p.get('pre_debut'):return '出道前'
    if _date(p) is None:return '日期未确认'
    return f"第{p['year_index']}年{p['month']}月{'上' if p['half']=='first' else '下'}"


def _snapshots(review):
    rows=[]
    for index,row in enumerate(review.get('observations') or []):
        if not isinstance(row,dict):continue
        state=row.get('state_before') or {};day=_date(state.get('period'))
        turn=row.get('turn_index')
        if day is None or type(turn) is not int or turn<1:continue
        rows.append({'day':day,'turn':turn,'index':index,'state':state,'row':row,
                     'source':'training_observation'})
    latest=review.get('latest_observed_state') or {}
    day=_date(latest.get('period'));turn=latest.get('turn_index',latest.get('turn'))
    if day is not None and type(turn) is int and turn>0:
        # A partial header must not erase the complete grid from the same day.
        complete=all(_number((latest.get('stats') or {}).get(k)) for k in STATS)
        same=[r for r in rows if r['day']==day and r['turn']==turn]
        if not same and (complete or not rows):
            rows.append({'day':day,'turn':turn,'index':len(review.get('observations') or []),
                         'state':latest,'row':{},'source':'latest_menu'})
    return rows


def _forecast(snapshot,targets):
    stats=snapshot['state'].get('stats') or {}
    for row in snapshot['row'].get('candidates') or []:
        plan=row.get('attribute_plan') or {};attributes=plan.get('attributes') or {}
        selected={}
        for key in STATS:
            item=attributes.get(key) or {};span=item.get('projected_final') or {}
            if (not _number(targets.get(key)) or targets[key]<=0
                    or not _number(stats.get(key)) or item.get('current')!=stats[key]
                    or item.get('target')!=targets[key]
                    or not all(_number(span.get(k)) and span[k]>=0 for k in ('low','estimate','high'))
                    or not span['low']<=span['estimate']<=span['high']):
                break
            selected[key]={'current':stats[key],'target':targets[key],
                           **{k:round(span[k],2) for k in ('low','estimate','high')}}
        if len(selected)==len(STATS):
            return {'status':'available','attributes':selected,
                    'training_budget':plan.get('estimated_training_budget'),
                    'basis':'recorded_same_snapshot_policy_scenarios'}
    return {'status':'unavailable','attributes':{},'training_budget':None,
            'basis':'no_complete_same_snapshot_forecast'}


def _startup(review,prefix,turn):
    cards=review.get('supports') or []
    profile_known=(review.get('support_profile_status')=='recorded' and len(cards)==6
                   and all(isinstance(c,dict) and c.get('card_id') for c in cards)
                   and len({str(c.get('card_id')) for c in cards})==6)
    owned={str(c['card_id']) for c in cards if isinstance(c,dict) and c.get('card_id')
           and c.get('type') not in ('friend','友人','group','团队')
           and str(c.get('character_id')) not in ('9043','satake_mei')}
    seen={}
    for row in prefix:
        if type(row.get('turn_index')) is not int or turn-row['turn_index']>4:continue
        for training in row.get('trainings') or []:
            for person in training.get('participants') or []:
                identity=person.get('identity') or {};bond=person.get('bond') or {}
                card=str(identity.get('matched_card_id'))
                if (card not in owned or identity.get('status')!='accepted' or bond.get('status')!='accepted'
                        or (person.get('role') or {}).get('kind')!='support'):continue
                ready=bond.get('is_max') is True or bond.get('color')=='orange'
                if card not in seen or row['turn_index']>=seen[card]['turn']:
                    seen[card]={'ready':ready,'turn':row['turn_index']}
    return {'profile_complete':profile_known,'training_cards':len(owned) if profile_known else None,
            'recently_observed_cards':len(seen),'ready_cards':sum(p['ready'] for p in seen.values()),
            'coverage_complete':profile_known and bool(owned) and len(seen)==len(owned),
            'basis':'same_run_owned_card_ids; last_four_turns; orange_or_max_bond'}


def _resources(review,prefix,snapshot):
    turn=snapshot['turn'];mei={'status':'unknown','stage':None}
    ss_turns=set()
    for row in prefix:
        action=row.get('selected') or {};t=row.get('turn_index')
        if type(t) is not int:continue
        for candidate in row.get('candidates') or []:
            identifier=str(candidate.get('action_id',''))
            if identifier.startswith('outing.satake_mei.') and identifier.rsplit('.',1)[-1] in ('1','2','3','4','5'):
                mei={'status':'observed_available','stage':int(identifier.rsplit('.',1)[-1]),'observed_turn':t}
        if t<turn and row.get('consumed') is True:
            if (row.get('kind') or action.get('action'))=='ss':ss_turns.add(t)
            if action.get('action')=='outing' and str(action.get('target','')).startswith('satake_mei:'):
                stage=str(action['target']).split(':')[-1]
                if stage in ('1','2','3','4','5'):
                    mei={'status':'used_all' if stage=='5' else 'observed_available',
                         'stage':None if stage=='5' else int(stage)+1,'observed_turn':t}
    # Final session state cannot retroactively unlock an earlier checkpoint.
    cut=any(c.get('state')=='present' and type(c.get('turn_index')) is int
            and c['turn_index']<=turn for c in review.get('cut_checks') or [])
    growth=snapshot['state'].get('growth') or {}
    return {'mei_outing':mei,'completed_ss_actions':len(ss_turns),
            'ss_count_is_not_win_count':True,'cut_confirmed':cut,
            'growth':{k:growth.get(k) if growth.get('status')=='accepted' and _number(growth.get(k)) else None
                      for k in ('supporter_points','adaptation_points','expectation_percent')}}


def _assess(review,snapshot,checkpoint,previous):
    key,label,_,_,_,next_label=checkpoint
    prefix=[r for i,r in enumerate(review.get('observations') or []) if isinstance(r,dict)
            and i<=snapshot['index'] and type(r.get('turn_index')) is int and r['turn_index']<=snapshot['turn']
            and (_date((r.get('state_before') or {}).get('period')) or 0)<=snapshot['day']]
    targets=(review.get('goal') or {}).get('target_stats') or {}
    forecast=_forecast(snapshot,targets);startup=_startup(review,prefix,snapshot['turn'])
    resources=_resources(review,prefix,snapshot)
    stats=snapshot['state'].get('stats') or {}
    missing=[];reasons=[];verdict='observe'
    if not startup['profile_complete']:missing.append('本局六张支援卡资料不完整；不据此扣分。')
    if not startup['coverage_complete']:missing.append('主力支援羁绊近期记录不全；未看到不等于尚未启动。')
    if resources['mei_outing']['status']=='unknown':missing.append('该时点没有已确认的黄帽外出段数。')
    estimates=[];short=[]
    if forecast['status']=='available':
        values=forecast['attributes']
        estimates=[min(1.,v['estimate']/v['target']) for v in values.values()]
        short=[k for k,v in values.items() if v['high']/v['target']<THRESHOLDS['late_shortfall_high_ratio']]
        mean=sum(estimates)/len(estimates)
        if mean>=THRESHOLDS['continue_mean_target_ratio'] and min(estimates)>=THRESHOLDS['continue_min_target_ratio']:
            verdict='continue';reasons.append('本时点的常规推演接近当前配置目标，各属性未出现特别突出的缺口。')
        elif (key in ('junior_end','classic_inheritance') and startup['coverage_complete']
              and startup['ready_cards']/startup['training_cards']>=THRESHOLDS['startup_ready_share']
              and mean>=THRESHOLDS['startup_mean_target_ratio']):
            verdict='continue';reasons.append('数值推演尚可，多数已确认主力支援达到友情启动条件，值得看下一阶段。')
        else:reasons.append('当前数值推演尚不足以确认好局，先保留观察空间。')
        repeated=set(short)&set((previous or {}).get('optimistic_shortfall_attributes',[]))
        if (key in ('first_arc_return','second_expedition') and startup['profile_complete']
                and len(repeated)>=THRESHOLDS['repeated_shortfall_attributes'] and not resources['cut_confirmed']):
            verdict='consider_stop'
            reasons=['连续两个已记录检查点存在相同的多项明显缺口；连理想推演也低于当前参考目标的80%。',
                     '这是数值方向的止损建议；还需核对稀有适性、关键技能及目标设置，不会执行弃养。']
    else:
        missing.append('缺少与本时点属性和目标一致的完整数值推演。')
        reasons.append('现有资料不足以判断后劲，保持观察，不把缺失信息当成差局。')
    if resources['cut_confirmed']:
        verdict='continue';reasons.insert(0,'此时已经确认出切，应继续保留；现有出切停机保护仍独立生效。')
    missing.append('适性S与已学关键技能尚未接入这版评估，未记录不等于没有。')
    return {'checkpoint':key,'label':label,'status':'assessed','verdict':verdict,
            'verdict_label':{'continue':'值得续养','observe':'继续观察','consider_stop':'建议止损（需复核）'}[verdict],
            'as_of_turn':snapshot['turn'],'as_of_period':snapshot['state'].get('period'),
            'as_of_label':date_label(snapshot['state'].get('period')),
            'snapshot_source':snapshot['source'],'observation_id':snapshot['row'].get('observation_id'),
            'next_checkpoint':next_label,'reasons':reasons,'unknowns':missing,
            'current_stats':{k:stats.get(k) if _number(stats.get(k)) else None for k in (*STATS,'skill_pt')},
            'reference_targets':{k:targets.get(k) if _number(targets.get(k)) else None for k in STATS},
            'vitality':snapshot['state'].get('vitality') if _number(snapshot['state'].get('vitality')) else None,
            'forecast':forecast,'startup':startup,
            'resources':resources,'optimistic_shortfall_attributes':short,
            'mean_estimated_target_ratio':round(sum(estimates)/len(estimates),3) if estimates else None,
            'automatic_action':None}


def assess_career_review(review):
    if not isinstance(review,dict) or review.get('schema_version')!='uma.career_review.v1':
        raise ValueError('需要一份有效的育成复盘。')
    rows=_snapshots(review);latest=max([r['day'] for r in rows],default=0)
    points=[];previous=None
    for checkpoint in CHECKPOINTS:
        key,label,start,end,mode,next_label=checkpoint
        eligible=[r for r in rows if start<=r['day']<=end]
        if not eligible:
            points.append({'checkpoint':key,'label':label,'status':'not_reached' if latest<start else 'missing',
                           'verdict_label':'尚未到达' if latest<start else '缺少该阶段记录',
                           'next_checkpoint':next_label,'automatic_action':None})
            continue
        day=(min if mode=='first' else max)(r['day'] for r in eligible)
        snapshot=max((r for r in eligible if r['day']==day),key=lambda r:(r['turn'],r['index']))
        point=_assess(review,snapshot,checkpoint,previous)
        points.append(point);previous=point
    last=max(rows,key=lambda r:(r['day'],r['turn'],r['index'])) if rows else None
    later=({'date':date_label(last['state'].get('period')),'turn':last['turn'],
            'stats':{k:(last['state'].get('stats') or {}).get(k) for k in (*STATS,'skill_pt')},
            'scope':'last_observed_state_not_a_final_result; not_used_to_grade_earlier_checkpoints'} if last else None)
    return {'schema_version':'uma.career_continuation_assessment.v1','mode':'advisory_only',
            'run_id':(review.get('run') or {}).get('run_id'),'thresholds':dict(THRESHOLDS),
            'threshold_status':'prototype_not_calibrated','checkpoints':points,
            'target_basis':'本局保存的属性目标，仅作参考，不代表大赛最低合格线。',
            'later_observed_for_comparison':later,
            'limitations':['未操作游戏，不改变原有截止、出切保护或失败重开设置。',
                           '推演是原策略的情景估计，不是实际成品或概率保证。',
                           '检查点建议只使用当时及更早的记录；后来的出切、外出和结算不会回填。',
                           '第二年继承观察点按4月下至5月上取样，适性与继承收益仍需已有记录确认。',
                           '后两年策略仍待打磨，建议止损须人工复核。'],
            'device_actions':0,'automatic_continuation_enabled':False,'automatic_abandon_enabled':False}
