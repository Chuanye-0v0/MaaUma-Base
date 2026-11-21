"""End-of-junior observations and compact metrics for controlled comparisons."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from core.auto_trainer.cut_farming import atomic_json


def read(path):
    try:return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return {}


def summarize_attempt(root):
    root=Path(root);turns={};total=0.;reasons=[];timings=[]
    for path in sorted(root.glob('autonomous_*/summary.json')):
        report=read(path);total+=report.get('elapsed_seconds',0.)
        reasons.append(report.get('stop_reason'))
        for turn in report.get('turns',[]):
            if turn.get('turn_consumed'):turns[turn['turn_index']]=turn
    actions=Counter();matches=[];starts=[]
    for turn in turns.values():
        kind=turn.get('kind');target=turn.get('execution',{}).get('target')
        actions[f'{kind}:{target or ""}']+=1
        starts.append(turn.get('before',{}))
        report=read(Path(turn['output'])/'pipeline_report.json').get('report',{})
        if report.get('pipeline_timing'):timings.append(report['pipeline_timing'])
        if kind=='ss':
            comparison=report.get('stages',{}).get('larc_ss',{}).get('comparison',{})
            matches.append({'turn':turn['turn_index'],'kind':comparison.get('kind'),
                'heads':comparison.get('ready_count')})
    return {'actions':dict(actions),'ss_matches':matches,'ss_count':sum(x['kind']=='SS' for x in matches),
        'sss_count':sum(x['kind']=='SSS' for x in matches),'ss_unknown_count':sum(x['kind'] not in {'SS','SSS'} for x in matches),
        'ss_total_heads':sum(x['heads'] for x in matches if type(x['heads']) is int),
        'initial_state':starts[0] if starts else None,'career_execution_seconds':round(total,3),
        'run_stop_reasons':reasons,'pipeline_timings':timings,'completed_actions':len(turns)}


def save_endpoint(ctx,source,provider,probe):
    state=deepcopy(provider.get('state_context',{}));period=provider.get('larc_period',{})
    growth=state.get('scenario_state',{}).get('larc_growth_observation',{})
    if type(growth.get('adaptation_points')) is not int:
        import cv2
        import re
        from core.larc_growth_reader import read_menu_growth
        returned=Path(str(probe.get('source','')))
        points_source=returned if returned.is_file() else Path(source)
        image=cv2.imread(str(points_source))
        if image is not None:
            image=cv2.resize(image,(720,1280))
            growth=read_menu_growth(image,getattr(ctx,'progress',{}).get('next_turn'),period=period.get('value',{}))
            if type(growth.get('adaptation_points')) is not int:
                from core.auto_trainer.cut_condition_probe import ocr_tokens
                roi=(350,1150,550,1225)
                tokens=ocr_tokens(image[roi[1]:roi[3],roi[0]:roi[2]],None)
                values=[t for t in tokens if re.fullmatch(r'\d{1,4}',t['text']) and t['confidence']>=.9]
                if len(values)==1:
                    growth={**growth,'status':'accepted','adaptation_points':int(values[0]['text']),
                        'end_balance_fallback':{'source':str(points_source),'roi':list(roi),
                            'text':values[0]['text'],'confidence':values[0]['confidence']}}
    stats=state.get('current_stats',{})
    result={'run_id':ctx.run_id,'period':period,'source':str(source),
        'stats':stats,'vitality':state.get('vitality'),
        'mood':state.get('scenario_state',{}).get('mood'),
        'adaptation_points':growth.get('adaptation_points'),'growth_observation':growth,
        'cut_state':probe.get('state'),'provider':provider}
    result['missing_metrics']=[name for name in ('speed','stamina','power','guts','wisdom','skill_pt') if type(stats.get(name)) not in (int,float)]
    if result['adaptation_points'] is None:result['missing_metrics'].append('adaptation_points')
    path=Path(ctx.root)/'benchmark_endpoint.json';atomic_json(path,result)
    return str(path)
