"""Re-score saved current-turn observations; never simulate missing future turns."""
from core.auto_trainer.rejection_policy import is_hard_rejected
from dataclasses import replace
from datetime import datetime
from html import escape
import json
import math
from pathlib import Path
import uuid

from core.auto_trainer.career_review_export import checked_review_run, _read, _score
from core.auto_trainer.config import UGParentGoal, validate_goal_config
from core.auto_trainer.decision import DecisionEngine
from core.auto_trainer.larc_ss_choice import choose_larc_ss
from core.auto_trainer.larc_user_preferences import normalize, feature_available
from core.auto_trainer.state import TrainingTurnState


def _explain(choice):
    """Summarize real scored candidates, without filling missing observations."""
    from core.auto_trainer.larc_score_tuning import CATALOG
    candidates=[row for row in choice['candidates'] if not is_hard_rejected(row)
        and type(row.get('score')) in (int,float) and math.isfinite(row['score'])]
    from core.auto_trainer.larc_timeline_workflow import selection_key
    top=sorted(candidates,key=selection_key)[:2]
    unknown=[]
    for row in choice['candidates']:
        for field in (row.get('score_workflow') or {}).get('unknown_fields') or []:
            unknown.append('工作流条件：'+str(field))
        for field in row.get('unknown_fields') or []:
            unknown.append(str(row.get('action_id',''))+'：'+str(field))
        for key,feature in (row.get('features') or {}).items():
            if isinstance(feature,dict) and feature.get('unknown_slots'):
                unknown.append(str(row.get('action_id',''))+'：'+key+' 部分成员未读清')
    def detail(row):
        parts=[(key,value) for key,value in (row.get('components') or {}).items()
            if type(value) in (int,float) and math.isfinite(value) and value]
        parts=sorted(parts,key=lambda item:abs(item[1]),reverse=True)[:4]
        return {'action_id':row.get('action_id'),'score':row['score'],
            'components':[{'key':key,'label':CATALOG.get(key,{}).get('label',
                '自定义总调整' if key=='custom_tuning_adjustment' else
                '工作流总调整' if key=='custom_workflow_adjustment' else key),'value':value} for key,value in parts],
            'score_workflow':row.get('score_workflow')}
    return {**choice,'top_candidates':[detail(row) for row in top],
        'score_gap':round(top[0]['score']-top[1]['score'],4) if len(top)==2 else None,
        'unknown_fields':list(dict.fromkeys(unknown))}


def _choice(state,goal,ss):
    if ss:
        result=choose_larc_ss(state,goal,ss['preview'],ss_executions=ss.get('ss_executions'))
        action='ss' if result.get('selected') else result.get('local_action')
        target=None if action=='ss' else result.get('local_target')
        rows=[_score(row) for row in result.get('training_candidates',[])]
        if result.get('score') is not None:rows.append({'action_id':'ss','score':result['score'],'components':result.get('components'),
            'score_tuning':result.get('score_tuning'),'score_workflow':result.get('score_workflow')})
        return _explain({'action':action,'target':target,'reason':result.get('reason'),'candidates':rows})
    result=DecisionEngine(goal).decide(state)
    return _explain({'action':result['action'],'target':result.get('target'),'reason':result.get('reason'),
            'candidates':[_score(row) for row in result.get('scored_candidates',[])]})


def compare_run(run,preferences):
    run=Path(run);preferences=normalize(preferences)
    rows=[];skipped=[]
    for folder in sorted(run.glob('autonomous_*/turn_*')):
        if not folder.is_dir():continue
        ss=_read(folder/'capture/ss_comparison_snapshot.json')
        raw=ss.get('state') if ss else _read(folder/'decision/turns/turn_000/state.json')
        label=folder.relative_to(run).as_posix()
        options=raw.get('training_options') if isinstance(raw,dict) else None
        if (not isinstance(options,list) or len(options)!=5 or
                {c.get('training_type') for c in options if isinstance(c,dict)}!={'speed','stamina','power','guts','wisdom'}):
            skipped.append({'observation':label,'reason':'缺少完整五页结构化快照'});continue
        try:
            captures=[c.get('raw_data',{}).get('capture',{}) for c in raw['training_options']]
            if any(c.get('run_session_id')!=run.name or c.get('turn_index')!=raw.get('turn') for c in captures):
                skipped.append({'observation':label,'reason':'采集记录与本局回合不一致'});continue
            original=ss.get('goal') if ss else _read(folder/'decision/goal.json')
            original=original or _read(run/'goal.json')
            goal=UGParentGoal.from_dict(original);validate_goal_config(goal)
            state=TrainingTurnState.from_dict(raw)
            from core.auto_trainer.config import replace_goal
            changed_goal=replace_goal(goal,larc_policy={**goal.larc_policy,'decision_preferences':preferences})
            before=_choice(state,goal,ss);after=_choice(state,changed_goal,ss)
            rows.append({'observation':label,'turn':state.turn,'before':before,'after':after,
                'changed':(before['action'],before['target'])!=(after['action'],after['target'])})
        except (ValueError,TypeError,KeyError,AttributeError):
            skipped.append({'observation':label,'reason':'快照字段不完整，无法重算'})
    return {'schema_version':'uma.decision_preferences_review.v1','run_id':run.name,'preferences':preferences,
        'basis':'same_saved_state_and_current_policy; original_run_preferences_vs_requested_preferences',
        'observations':rows,'changed_count':sum(row['changed'] for row in rows),'skipped':skipped,
        'tap_or_purchase_authorized':False}


def render(result):
    names={'train':'训练','speed':'速度','stamina':'耐力','power':'力量','guts':'毅力','wisdom':'智力',
        'rest':'休息','outing':'外出','ss':'SS','stop':'停止'}
    def label(choice):return ' · '.join(names.get(v,v) for v in (choice.get('action'),choice.get('target')) if v)
    cells=[]
    for row in result['observations']:
        details=[]
        for key,title in (('before','本局原参数'),('after','待比较方案')):
            scores=[]
            for c in row[key]['candidates']:
                bonus=(c.get('planning') or {}).get('preferred_heads') or {}
                text=f"{c.get('action_id','')}：{c.get('score','未知')}"+('（不可选）' if c.get('rejected') else '')
                if bonus.get('requested'):text+=f"；人头额外分 {bonus.get('total',0)}"
                tuning=c.get('score_tuning') or {}
                if tuning.get('status')=='applied':text+=f"；自定义调整 {tuning.get('adjustment',0):+g}（原分 {tuning['base_score']}）"
                elif tuning.get('status')=='fallback':text+='；自定义计算失败，本回合使用原评分'
                workflow=c.get('score_workflow') or {}
                if workflow.get('status')=='applied':
                    text+=f"；工作流 {workflow.get('stage_label')} → {workflow.get('rule_label')}，调整 {workflow.get('adjustment',0):+g}，行动优先序 {workflow.get('priority_rank',0)+1}"
                elif workflow.get('active'):text+='；工作流未命中，使用原策略'
                if workflow.get('unknown_fields'):text+='；条件未取得：'+ '、'.join(workflow['unknown_fields'])
                scores.append(escape(text))
            gap=row[key].get('score_gap');explanation=[]
            if gap is not None:explanation.append(f'当前前两名分数差（可能属于不同优先行动）：{gap:g}')
            for candidate in row[key].get('top_candidates',[]):
                explanation.append(str(candidate['action_id'])+' 关键分项：'+ '；'.join(
                    f"{p['label']} {p['value']:+g}" for p in candidate['components']))
            explanation.extend('未读清：'+field for field in row[key].get('unknown_fields',[]))
            details.append(f'<strong>{title}</strong><br>'+ '<br>'.join(scores+[escape(s) for s in explanation]))
        cells.append(f'<tr><td>{row["turn"]}</td><td>{escape(label(row["before"]))}</td><td>{escape(label(row["after"]))}</td>'
            f'<td>{"改选" if row["changed"] else "不变"}</td><td><details><summary>查看评分</summary>'+ '<hr>'.join(details)+'</details></td></tr>')
    prefs=result['preferences']
    tuning=prefs.get('tuning') or {}
    config_note=('公式：'+str(tuning.get('formula')) if tuning.get('mode')=='formula' else
        '细项倍率：'+str(tuning.get('factors',{})) if tuning.get('mode')=='detailed' else '高级模式：保持原策略')
    if (prefs.get('workflow') or {}).get('enabled'):
        config_note='时间轴工作流已启用；高级公式／细项配置已保留，关闭工作流后恢复。'
    skipped=''.join(f'<li>{escape(row["observation"])}：{escape(row["reason"])}</li>' for row in result['skipped'])
    return '<!doctype html><meta charset="utf-8"><title>取舍参数对比</title><style>body{font-family:system-ui;max-width:1100px;margin:35px auto;padding:20px;color:#243748}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #ddd;text-align:left}details{max-width:500px}p{line-height:1.7}</style>'\
        f'<h1>取舍参数对比</h1><p>对比 {len(cells)} 份回合快照，其中 {result["changed_count"]} 份会改选；跳过 {len(result["skipped"])} 份。</p>'\
        '<p>使用当前版本策略，在同一份已保存现场上分别代入本局原参数和待比较方案。这里只比较当时这一回合，不模拟后续随机结果，也不改动正在运行的育成。多次采集会分行列出。前两名先按工作流行动优先序、再按可选评分排序；分差只表示分数差，实际选择仍受原有规则约束。</p>'\
        f'<p>数值 {prefs["numeric"]:.0%} · 羁绊 {prefs["bond"]:.0%} · 充电 {prefs["charge"]:.0%} · 黄金船 +{prefs["gold_ship_bonus"]:g} · 黄帽 +{prefs["mei_bonus"]:g}</p>'\
        f'<p>{escape(config_note)}</p>'\
        '<table><tr><th>回合</th><th>本局原参数</th><th>待比较方案</th><th>变化</th><th>详情</th></tr>'+''.join(cells)+'</table>'+\
        ('<h2>未参与对比</h2><ul>'+skipped+'</ul>' if skipped else '')


def export_comparison(root,identifier,preferences):
    if not feature_available():raise ValueError('本版本未启用自定义取舍预览。')
    run=checked_review_run(root,identifier);result=compare_run(run,preferences)
    root=Path(root);folder=root.parent/'exports' if root.name.lower()=='backend' else root/'runs/review_exports'
    folder.mkdir(parents=True,exist_ok=True)
    base=folder/f'取舍对比_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}'
    base.with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    path=base.with_suffix('.html');path.write_text(render(result),encoding='utf8')
    summary={**result,'observations':[{**row,**{key:{k:v for k,v in row[key].items() if k!='candidates'}
        for key in ('before','after')}} for row in result['observations']]}
    return {'path':str(path),'compared':len(result['observations']),'changed':result['changed_count'],
        'skipped':len(result['skipped']),'comparison':summary}
