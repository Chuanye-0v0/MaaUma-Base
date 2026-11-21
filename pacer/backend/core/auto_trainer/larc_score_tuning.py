"""User scoring overlays. Pure arithmetic, no files, Python execution or game input."""
import ast
from copy import deepcopy
from functools import lru_cache
import math
from core.auto_trainer.larc_score_examples import DETAILED_EXAMPLES, FORMULA_EXAMPLES, QUICK_GUIDE
from core.auto_trainer.rejection_policy import is_hard_rejected


# Independent of the app release: increment when a factor, formula variable,
# unit/group or overlay rule changes meaning. Additive UI/runtime fixes do not.
# The original unversioned profile v1 is permanently bound to semantics 1.
PROFILE_SCHEMA='uma.scoring_profile.v3'
SCORING_ONLY_PROFILE_SCHEMA='uma.scoring_profile.v2'
SCORING_SEMANTICS={'model':'uma.larc.final_score','version':1}
LEGACY_SCORING_SEMANTICS={'model':'uma.larc.final_score','version':1}


def scoring_profile(preferences):
    result={'schema_version':SCORING_ONLY_PROFILE_SCHEMA,'scoring_semantics':deepcopy(SCORING_SEMANTICS),
            'preferences':deepcopy(preferences)}
    if preferences.get('workflow') is not None:
        from core.auto_trainer.larc_timeline_workflow import SEMANTICS
        result.update(schema_version=PROFILE_SCHEMA,workflow_semantics=deepcopy(SEMANTICS))
    return result


# Factors change the named final score contributions, not observed game gains.
COMPONENTS=[
 ('stats','属性数值','numeric','已读到的五维训练或 SS 增益；提高后更重视眼前属性。'),
 ('dynamic_attribute_gap','属性缺口','numeric','离目标较远的属性得到补偿；不是额外实际收益。'),
 ('skill_pt','技能点','numeric','本次获得的技能点价值。'),
 ('first_arc_speed_preparation','首次凯旋门速度准备','numeric','首次远征期间速度不足时的准备价值。'),
 ('high_stat_target_scarcity','高属性目标余量','numeric','高属性阶段继续接近目标的价值。'),
 ('effective_bond','普通羁绊','bond','未达到友情启动线的支援卡接触价值；已经达到启动线的普通羁绊不重复加分。'),
 ('training_hint_bond','叹号羁绊','bond','训练页红叹号带来的额外羁绊机会估值。'),
 ('friend_startup','友人初期启动','bond','旧策略的友人启动分；当前平衡策略主要使用下面两项。'),
 ('friend_coverage_preparation','黄帽羁绊推进','bond','推进黄帽羁绊、接近后续效果启动的价值。'),
 ('mei_initial_contact_preference','首次接触黄帽','bond','本局尚未记录黄帽训练接触时的一次性偏好；出道前与尚未兑现爱娇的黄金船同页时加强。不保证外出马上解锁。'),
 ('net_charge','净充电','charge','本次实际能增加的电池格数；满电不算重复充电。'),
 ('newly_full','新充满成员','charge','本次从未满变为满电的成员价值。'),
 ('ss_preparation','凑齐五人','charge','可见充电能使就绪人数达到五人时的准备分。'),
 ('cycle_reward_preparation','当前奖励准备','charge','向当前可见 SS 奖励推进的估值。'),
 ('charming_charge_progress','黄金船爱娇启动','charge','为尚未兑现的爱娇奖励充电；后期剩余羁绊需求少时会降低。'),
 ('future_recovery_cycle_progress','后续恢复奖励','charge','已知奖励循环中未来恢复的折扣价值，不当作立即回血。'),
 ('mei_expected_charge','黄帽额外充电预期','charge','按现有触发先验估计的额外充电；不是保证触发。'),
 ('full_group_preference','五人 SS 偏好','charge','完整 SS 或 SSS 的软偏好；修改后参与实际 SS 与训练比较。'),
 ('battery_release_opportunity','SS 腾出电池','charge','兑现 SS 后释放电池容量、减少后续溢出的价值。'),
 ('first_charming_preference','SS 兑现爱娇','charge','当前 SS 能兑现未获得爱娇时的价值。'),
 ('vitality','训练体力变化','energy','训练回血为正、消耗为负；提高会同时更重视回血和更厌恶消耗。'),
 ('recovery','休息／外出回复','energy','恢复行动的有效回血，已扣除溢出。'),
 ('known_recovery','SS 已知回复','energy','已读清的 SS 恢复价值。'),
 ('rule_recovery','SS 规则估计回复','energy','图标规则估计的 SS 恢复，仍保留估计标记。'),
 ('energy_continuity','后续回合安排','energy','根据现有资料估计后续训练、恢复顺序的价值差；可能为负。'),
 ('recovery_timing_preference','提前休息时机','energy','弱训练后很快仍要恢复时，给提前休息或外出的软补偿；出道前也会考虑少量智力回血后还能连续训练多久。'),
 ('opening_preparation_preference','开局状态准备','energy','充电解锁前保存体力、提升心情的时机偏好。'),
 ('healing_charge_tie_preference','接近时优先回血','energy','训练分差很小且进度没有损失时，轻微偏向能回血的训练。'),
 ('failure_penalty','失败率罚分','risk','按失败率扣分；提高倍率会更保守。失败率上限另行生效。'),
 ('failure_expected_loss','失败损失预期','risk','对失败可能损失的收益扣分；提高倍率会更保守。'),
 ('risk_aversion_preference','风险厌恶偏好','risk','v4 在失败预期损失之外的主观保守扣分，原值为失败率每个百分点扣0.5分；倍率越高越保守，×0只取消该偏好，不取消失败损失或行动资格检查。'),
 ('mood_recovery','心情恢复','other','恢复心情对后续训练的价值。'),
 ('outing_chain_progress','外出事件推进','other','使用黄帽外出时推进事件链的价值。'),
 ('outing_completion_increment','外出完走收益','other','接近外出完走时的额外完成价值。'),
 ('outing_skill_hint_option','外出技能机会','other','外出链带来的技能机会估值。'),
 ('conditional_fan_route','后续积分路线','other','与预期度和粉丝路线相关的条件价值。'),
 ('expectation_training_progress','预期度训练增益','other','SS 提高预期度后对后续训练的估值。'),
 ('adaptation_early_use','适应性提前使用','other','SS 资源能较早用于适应性时的价值。'),
 ('phase_strong_training_opportunity','强训练机会成本','other','当前强训练与 SS 的阶段取舍补偿，可能为负。'),
 ('supporter_deadline_preference','协助积分期限','other','接近协助者积分期限时的软紧迫度；硬期限规则不由此取消。'),
 ('baseline_support','基础支援人数','other','缺少明确阶段时的基础人数估值。'),
 ('user_gold_ship_bonus','黄金船额外分','other','基础设置中填写的黄金船在场额外分。'),
 ('user_mei_bonus','黄帽额外分','other','基础设置中填写的黄帽在场额外分。'),
]
CATALOG={key:{'key':key,'label':label,'group':group,'description':description,'default_factor':1.0}
         for key,label,group,description in COMPONENTS}
CONTEXT_VARIABLES={
 'base_score':'当前原策略总分，已包含基础倍率与人头额外分。',
 'numeric_score':'数值、属性缺口与技能点得分合计。','bond_score':'羁绊与友人启动得分合计。',
 'charge_score':'充电、奖励准备与 SS 兑现得分合计。','energy_score':'体力与后续安排得分合计。',
 'risk_score':'失败风险罚分合计，通常为负。','other_score':'其他得分；六组得分之和等于 base_score。',
 'vitality':'当前体力百分比。','turn':'本局行动编号。','year':'年份：1、2、3；未知为0。',
 'month':'月份；出道前或未知为0。','remaining_turns':'当前目标剩余回合，未知为0。',
 'is_training':'训练为1，其他为0。','is_rest':'普通休息为1。','is_outing':'外出为1。',
 'is_ss':'SS/SSS 为1。','is_speed':'速度训练为1。','is_stamina':'耐力训练为1。',
 'is_power':'力量训练为1。','is_guts':'毅力训练为1。','is_wisdom':'智力训练为1。',
 'gold_ship':'当前训练确认识别到黄金船为1；否则为0。','mei':'当前训练确认识别到黄帽为1；否则为0。',
 'failure_rate':'训练失败率百分比；非训练为0。','charge_cells':'本页已确认的净充电格数。',
 'newly_full':'本页已确认的新满电人数。','bond_units':'本页已确认的普通有效羁绊单位。',
 **{'gain_'+key:'本次已确认的'+label+'增量，未读到为0。' for key,label in
    [('speed','速度'),('stamina','耐力'),('power','力量'),('guts','毅力'),('wisdom','智力'),('skill_pt','技能点')]},
 **{'cur_'+key:'当前'+label+'属性，未读到为0。' for key,label in
    [('speed','速度'),('stamina','耐力'),('power','力量'),('guts','毅力'),('wisdom','智力'),('skill_pt','技能点')]},
 **{'gap_'+key:'当前距离目标的'+label+'缺口（目标-当前，下限为0），未提供Goal为0。' for key,label in
    [('speed','速度'),('stamina','耐力'),('power','力量'),('guts','毅力'),('wisdom','智力')]},
 'ready_count':'当前SS已充能人数（0~5），未读到为0。',
 'ready_count_known':'当前SS已充能人数是否已确认（0或1）。',
 'clocks_remaining':'本局已确认剩余可用闹钟额度，未读到为0。',
 'clocks_remaining_known':'本局剩余闹钟额度是否已确认（0或1）。',
}
VARIABLES={**CONTEXT_VARIABLES,**{'s_'+key:'评分项：'+value['label']+'。'+value['description'] for key,value in CATALOG.items()}}
DEFAULT={'mode':'default','factors':{},'formula':'base_score'}
FUNCTIONS={'min':lambda *xs:min(xs),'max':lambda *xs:max(xs),'abs':abs,'clamp':lambda x,lo,hi:min(hi,max(lo,x))}
TEMPLATES=FORMULA_EXAMPLES


def schema():
    from core.auto_trainer.larc_timeline_workflow import schema as workflow_schema, SEMANTICS
    result={'schema_version':'uma.scoring_tuning.v1','defaults':deepcopy(DEFAULT),'components':list(CATALOG.values()),
        'profile_schema_version':SCORING_ONLY_PROFILE_SCHEMA,'workflow_profile_schema_version':PROFILE_SCHEMA,
        'scoring_semantics':deepcopy(SCORING_SEMANTICS),
        'scoring_only_profile_schema_version':SCORING_ONLY_PROFILE_SCHEMA,
        'workflow_semantics':deepcopy(SEMANTICS),'workflow_schema':workflow_schema(),
        'legacy_profile_semantics':deepcopy(LEGACY_SCORING_SEMANTICS),
        'variables':[{'name':name,'description':desc} for name,desc in VARIABLES.items()],
        'templates':deepcopy(TEMPLATES),'detailed_templates':deepcopy(DETAILED_EXAMPLES),
        'quick_guide':list(QUICK_GUIDE),'factor_min':0,'factor_max':10,
        'scope':'细项倍率与公式调整最终评分，不改游戏收益和原模型内部预测；执行限制继续生效。'}
    try:
        from core.auto_trainer.larc_score_reference import attach_references
    except ImportError:
        return result  # References are optional; old runtimes keep the scoring schema.
    return attach_references(result)


@lru_cache(maxsize=64)
def parse_formula(text):
    if not isinstance(text,str) or not text.strip() or len(text)>4096:raise ValueError('公式不能为空，长度最多4096字。')
    try:tree=ast.parse(text.strip(),mode='eval')
    except (SyntaxError,RecursionError) as error:raise ValueError('公式语法不正确，请检查括号和运算符。') from error
    nodes=list(ast.walk(tree))
    call_names={id(node.func) for node in nodes if isinstance(node,ast.Call)}
    if len(nodes)>200:raise ValueError('公式过长，请简化表达式。')
    allowed=(ast.Expression,ast.Constant,ast.Name,ast.Load,ast.BinOp,ast.UnaryOp,ast.IfExp,ast.Compare,
        ast.BoolOp,ast.Call,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Mod,ast.USub,ast.UAdd,ast.Not,
        ast.And,ast.Or,ast.Eq,ast.NotEq,ast.Lt,ast.LtE,ast.Gt,ast.GtE)
    for node in nodes:
        if not isinstance(node,allowed):raise ValueError('公式只支持数值运算、条件表达式和 min/max/abs/clamp。')
        if isinstance(node,ast.Constant) and (type(node.value) not in (int,float,bool)
                or not math.isfinite(node.value) or abs(node.value)>1_000_000):raise ValueError('公式常数必须是范围内的有限数字。')
        if isinstance(node,ast.Name) and node.id not in VARIABLES and node.id not in FUNCTIONS:
            raise ValueError('未知公式变量：'+node.id)
        if isinstance(node,ast.Name) and node.id in FUNCTIONS and id(node) not in call_names:
            raise ValueError('函数后需要括号：'+node.id)
        if isinstance(node,ast.Call):
            if not isinstance(node.func,ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ValueError('公式函数仅支持 min/max/abs/clamp，不接受命名参数。')
            count=len(node.args);name=node.func.id
            if not (count==1 if name=='abs' else count==3 if name=='clamp' else 1<=count<=8):
                raise ValueError('公式函数参数数量不正确：'+name)
    return tree.body


def _finite(value):
    value=float(value)
    if not math.isfinite(value) or abs(value)>1_000_000:raise ValueError('公式结果超出允许范围。')
    return value


def evaluate_formula(text,context):
    def read(node):
        if isinstance(node,ast.Constant):return float(node.value)
        if isinstance(node,ast.Name):return _finite(context[node.id])
        if isinstance(node,ast.UnaryOp):
            value=read(node.operand)
            return -value if isinstance(node.op,ast.USub) else value if isinstance(node.op,ast.UAdd) else float(not value)
        if isinstance(node,ast.BinOp):
            left,right=read(node.left),read(node.right)
            operations={ast.Add:lambda:left+right,ast.Sub:lambda:left-right,ast.Mult:lambda:left*right,
                ast.Div:lambda:left/right,ast.Mod:lambda:left%right}
            return _finite(operations[type(node.op)]())
        if isinstance(node,ast.IfExp):return read(node.body if read(node.test) else node.orelse)
        if isinstance(node,ast.BoolOp):
            for value in node.values:
                result=read(value)
                if isinstance(node.op,ast.And) and not result or isinstance(node.op,ast.Or) and result:return result
            return result
        if isinstance(node,ast.Compare):
            left=read(node.left)
            for op,next_node in zip(node.ops,node.comparators):
                right=read(next_node)
                checks={ast.Eq:left==right,ast.NotEq:left!=right,ast.Lt:left<right,ast.LtE:left<=right,ast.Gt:left>right,ast.GtE:left>=right}
                if not checks[type(op)]:return 0.
                left=right
            return 1.
        if isinstance(node,ast.Call):return _finite(FUNCTIONS[node.func.id](*[read(arg) for arg in node.args]))
        raise ValueError('不支持的公式结构。')
    try:return _finite(read(parse_formula(text)))
    except (ZeroDivisionError,OverflowError,KeyError,TypeError) as error:raise ValueError('公式计算失败：请检查除数和函数参数。') from error


def normalize(value=None):
    value={} if value is None else value
    if not isinstance(value,dict) or set(value)-set(DEFAULT):raise ValueError('高级权重包含未知字段。')
    result={**deepcopy(DEFAULT),**value}
    if result['mode'] not in ('default','detailed','formula'):raise ValueError('请选择原策略、细项倍率或评分公式。')
    factors=result['factors']
    if not isinstance(factors,dict) or set(factors)-set(CATALOG):raise ValueError('细项倍率包含未知评分项。')
    for key,amount in factors.items():
        if type(amount) not in (int,float) or not math.isfinite(amount) or not 0<=amount<=10:
            raise ValueError(CATALOG[key]['label']+'的倍率应在0～10之间。')
    result['factors']={key:float(amount) for key,amount in factors.items() if amount!=1}
    if not isinstance(result['formula'],str) or len(result['formula'])>4096:raise ValueError('公式最多4096字。')
    if result['mode']=='formula':parse_formula(result['formula'])
    return result


BUILTIN_2_3_PRO_FACTORS = {
    'stats': 1.083,
    'dynamic_attribute_gap': 1.109,
    'skill_pt': 1.032,
    'effective_bond': 1.136,
    'net_charge': 1.052,
    'newly_full': 1.011,
    'ss_preparation': 1.107,
    'full_group_preference': 1.123,
    'vitality': 0.891,
    'energy_continuity': 0.897,
    'recovery': 0.918,
    'recovery_timing_preference': 0.881,
    'outing_chain_progress': 0.865,
    'outing_completion_increment': 0.896,
    'risk_aversion_preference': 0.885,
}
BUILTIN_2_3_PRO_TUNING = {'mode': 'detailed', 'factors': BUILTIN_2_3_PRO_FACTORS, 'formula': 'base_score'}


def settings(goal):
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_user_preferences import values, builtin_preset_revision
    explicit = values(goal).get('tuning')
    if explicit:
        return explicit
    rev = builtin_preset_revision(goal)
    if rev in ('2.3', '2.3_pro'):
        return {'mode': 'detailed', 'factors': dict(BUILTIN_2_3_PRO_FACTORS), 'formula': 'base_score'}
    ext_tuning = active_provider().builtin_score_tuning(rev)
    if ext_tuning is not None:
        return ext_tuning
    return DEFAULT


def without_tuning(goal):
    from dataclasses import replace
    from core.auto_trainer.larc_strategy_extension import active_provider
    from core.auto_trainer.larc_user_preferences import builtin_preset_revision
    had_builtin=builtin_preset_revision(goal) is not None
    policy=deepcopy(goal.larc_policy);preferences=policy.setdefault('decision_preferences',{})
    preferences.pop('tuning',None)
    preferences.pop('workflow',None)
    rev = policy.get('revision')
    if had_builtin or rev in ('2.3','2.3_pro') or active_provider().has_builtin_score_tuning(rev):
        preferences['tuning']=deepcopy(DEFAULT)
    from core.auto_trainer.config import replace_goal
    return replace_goal(goal, larc_policy=policy)


def variables(state,row,preview=None,goal=None):
    from core.auto_trainer.larc_recovery import period_value
    from core.auto_trainer.larc_strategy_rules import resolve_clocks_remaining
    from core.auto_trainer.larc_user_preferences import current_training_people
    parts=row.get('components') or {};period=period_value(state)
    ctx=dict.fromkeys(VARIABLES,0.)
    ctx.update({'base_score':row['score'],'vitality':state.vitality,'turn':state.turn,
        'year':period.get('year_index') or 0,'month':period.get('month') or 0,
        'remaining_turns':state.scenario_state.get('larc_remaining_turns') or 0})
    action=row.get('action') or ('train' if row.get('training_type') else 'ss' if row.get('action_id')=='ss' else '')
    ctx.update(is_training=float(action=='train'),is_rest=float(action=='rest'),is_outing=float(action=='outing'),is_ss=float(action=='ss'))
    groups={group:0. for group in ('numeric','bond','charge','energy','risk')}
    for key,value in parts.items():
        if key in CATALOG:
            ctx['s_'+key]=value;group=CATALOG[key]['group']
            if group in groups:groups[group]+=value
    ctx.update({group+'_score':value for group,value in groups.items()})
    ctx['other_score']=row['score']-sum(groups.values())
    candidate=next((c for c in state.training_options if c.training_type==row.get('training_type')),None)
    gains=(preview or {}).get('stats_gain',{}) if action=='ss' else candidate.stats_gain if candidate else {}
    for key,value in gains.items():
        if 'gain_'+key in ctx and type(value) in (int,float):ctx['gain_'+key]=value
    if candidate:
        ctx['is_'+candidate.training_type]=1.;ctx['failure_rate']=candidate.failure_rate or 0.
        people=current_training_people(state,candidate,candidate.raw_data.get('capture')) or []
        ids={p.get('identity',{}).get('character_id') for p in people if p.get('identity',{}).get('status')=='known'}
        ctx['gold_ship']=float('gold_ship' in ids);ctx['mei']=float('satake_mei' in ids)
        features=row.get('features') or {}
        for key,field in [('charge_cells','net_charge'),('newly_full','newly_full'),('bond_units','effective_support_bond')]:
            ctx[key]=(features.get(field) or {}).get('known_value') or 0.
    cur_stats=getattr(state,'current_stats',None) or {}
    targets=(getattr(goal,'target_stats',None) or {}) if goal is not None else {}
    for key in ('speed','stamina','power','guts','wisdom','skill_pt'):
        cur_val=cur_stats.get(key) if isinstance(cur_stats,dict) else getattr(cur_stats,key,None)
        if type(cur_val) in (int,float):
            ctx['cur_'+key]=float(cur_val)
            tgt_val=targets.get(key) if isinstance(targets,dict) else None
            if 'gap_'+key in ctx and type(tgt_val) in (int,float):
                ctx['gap_'+key]=max(0.,float(tgt_val)-float(cur_val))
    sc_state=getattr(state,'scenario_state',{}) or {}
    ss_source=preview if isinstance(preview,dict) else (sc_state.get('larc_ss_preview') or sc_state.get('larc_ss_entry'))
    rc=(ss_source or {}).get('ready_count') if isinstance(ss_source,dict) else None
    if type(rc) is int and 0<=rc<=5:
        ctx['ready_count']=float(rc);ctx['ready_count_known']=1.
    clocks,clocks_known=resolve_clocks_remaining(state)
    ctx['clocks_remaining']=float(clocks)
    ctx['clocks_remaining_known']=1. if clocks_known else 0.
    return {key:_finite(value) if type(value) in (int,float,bool) else 0. for key,value in ctx.items()}


def _tunable(row):
    return (not is_hard_rejected(row) and row.get('allowed') is not False
            and row.get('eligible') is not False)


def apply(state,goal,rows,preview=None):
    # Re-evaluation may reuse scored candidates. Always start from model scores.
    for row,original in zip(rows,original_rows(rows)):
        if row is not original:
            row.clear();row.update(original)
    from core.auto_trainer.larc_timeline_workflow import enabled as workflow_enabled, apply as apply_workflow
    if workflow_enabled(goal):
        try:return apply_workflow(state,goal,rows)
        except ValueError as error:
            for row in rows:
                if _tunable(row):
                    row['score_tuning']={'mode':'workflow','status':'fallback','reason':str(error),'base_score':row['score']}
            return {'active':True,'changed':False,'status':'fallback','reason':str(error)}
    config=settings(goal)
    if config['mode']=='default':return {'active':False,'changed':False}
    planned=[]
    try:
        for row in rows:
            if not _tunable(row):continue
            base=row['score'];parts=row.get('components') or {}
            if config['mode']=='detailed':
                changes=[{'key':key,'base':parts[key],'factor':factor,'adjusted':round(parts[key]*factor,4)}
                         for key,factor in config['factors'].items() if parts.get(key)]
                score=_finite(base+sum(parts.get(key,0)*(factor-1) for key,factor in config['factors'].items()))
                detail={'changes':changes}
            else:
                context=variables(state,row,preview,goal=goal);score=evaluate_formula(config['formula'],context)
                used={n.id for n in ast.walk(parse_formula(config['formula'])) if isinstance(n,ast.Name) and n.id in context}
                detail={'formula':config['formula'],'variables':{k:context[k] for k in sorted(used)}}
            planned.append((row,round(score,4),{'mode':config['mode'],'status':'applied','base_score':base,
                'base_rejected':row.get('rejected',False),**detail}))
    except ValueError as error:
        for row in rows:
            if _tunable(row):row['score_tuning']={'mode':config['mode'],'status':'fallback','reason':str(error),'base_score':row['score']}
        return {'active':True,'changed':False,'status':'fallback','reason':str(error)}
    changed=False
    for row,score,detail in planned:
        adjustment=round(score-row['score'],4);changed|=bool(adjustment)
        row['components']['custom_tuning_adjustment']=adjustment
        row['score']=score;row['score_tuning']={**detail,'adjustment':adjustment}
    return {'active':True,'changed':changed,'status':'applied'}


def original_rows(rows):
    result=[]
    for row in rows:
        workflow=row.get('score_workflow') or {}
        if workflow.get('status')=='applied':
            row={**row,'score':workflow['base_score'],'rejected':workflow['base_rejected'],
                 'components':{k:v for k,v in row.get('components',{}).items() if k!='custom_workflow_adjustment'}}
        tuning=row.get('score_tuning') or {}
        if tuning.get('status')=='applied':
            row={**row,'score':tuning['base_score'],'rejected':tuning['base_rejected'],
                 'components':{k:v for k,v in row.get('components',{}).items() if k!='custom_tuning_adjustment'}}
        if 'score_tuning' in row or 'score_workflow' in row:
            row={k:v for k,v in row.items() if k not in ('score_tuning','score_workflow')}
        result.append(row)
    return result


def example_profiles():
    from core.auto_trainer.larc_user_preferences import DEFAULTS
    profiles={
        '权重示例-细项.json':{'schema_version':'uma.scoring_profile.v1','preferences':{**DEFAULTS,
            'tuning':{'mode':'detailed','factors':{'mei_initial_contact_preference':1.1,'skill_pt':1.1},'formula':'base_score'}}},
        '权重示例-公式.json':{'schema_version':'uma.scoring_profile.v1','preferences':{**DEFAULTS,
            'tuning':{'mode':'formula','factors':{},'formula':'base_score'}}},
    }
    for mode,examples in [('detailed',DETAILED_EXAMPLES),('formula',TEMPLATES)]:
        for row in examples:
            kind='细调' if mode=='detailed' else '公式'
            filename='权重示例-'+kind+'-'+row['name'].replace(' · ','-')+'.json'
            profiles[filename]={'schema_version':'uma.scoring_profile.v1','preferences':{**DEFAULTS,
                'tuning':{'mode':mode,'factors':row.get('factors',{}),'formula':row.get('formula','base_score')}}}
    return {name:scoring_profile(value['preferences']) for name,value in profiles.items()}


def help_text():
    lines=['MaaUma 自定义评分说明（测试版）','',
        '横向时间轴：每回合按行动前日期和已读状态从左到右找第一条成立规则；未知条件跳过并记录，不按0处理。',
        '时间轴开启时保留基础五项偏好，旧高级倍率和公式暂存；关闭时间轴后恢复。时间轴未命中时沿原策略。',
        '优先行动先比较已有可执行行动类别，再比较类别内分数；阶段与规则顺序不叠加，倍率不解禁不可执行动作。','',
        '入口一：详细调整内置权重。每个最终评分项可单独设置0～10倍，1保持原值；负分提高倍率会扣得更多。',
        '入口二：自写公式／导入配置。填写一个算术表达式作为最终评分，或导入导出JSON。',
        '两种模式任选其一。两者都在基础设置后作用于最终评分，不修改游戏实际收益或原模型内部预测。',
        '配置按面板的三组配置保存，新一轮生效；暂停后继续保留本轮原配置。',
        '先在复盘页选择一局并点击“对比当前取舍参数”，核对哪些回合会改选。缺少完整快照的回合跳过。',
        '训练失败率上限、数据可靠性、五人SS要求和比赛期限继续生效；权重不能解禁不可执行动作。',
        '语法：+ - * / %，比较，and/or/not，a if condition else b，min/max/abs/clamp。没有导入、循环、文件或网络访问。',
        '公式运行出错时，本次比较整体使用原评分并记录错误，不混用两套分数。',
        '原始收益和人数变量只提供已确认部分，未读到按0输入，并不证明真实值为0。',
        'JSON格式：纯高级评分保持uma.scoring_profile.v2；含时间轴使用uma.scoring_profile.v3及workflow_semantics，preferences保留完整工作流和旧公式。文件只含评分配置。',
        '时间轴v3支持本地文件导入导出；在线服务尚未验收v3时明确拒绝提交，不降级丢弃工作流。',
        '旧v1文件固定按历史评分语义1解释；未知或不兼容语义不可导入。应用版本不同但评分语义一致时仍可分享。','',
        '先用例子理解：']
    lines+=QUICK_GUIDE
    for title,examples in [('内置细调的 7 个例子',DETAILED_EXAMPLES),('自写公式的 8 个例子',TEMPLATES)]:
        lines+=['',title]
        for row in examples:
            lines+=['',row['name'],row['description']]
            if 'formula' in row:lines.append('填入公式：'+row['formula'])
            else:lines.append('填写倍率：'+'；'.join(CATALOG[k]['label']+' = '+str(v) for k,v in row['factors'].items()))
            lines+=['算一遍：'+row['calculation'],'如何影响选择：'+row['comparison'],'注意：'+row['notice']]
    lines+=['','细项权重（preferences.tuning.factors 的键）：']
    for key,row in CATALOG.items():lines.append(f"{key} | {row['label']} | 默认倍率1 | {row['description']}")
    lines+=['','公式变量：']
    for name,description in VARIABLES.items():lines.append(name+' | '+description)
    lines+=['','示例公式：']
    for row in TEMPLATES:lines.append(row['name']+'：'+row['formula'])
    return '\n'.join(lines)+'\n'
