"""Completed common training-injury choices, not race losses or failure rates."""
from core.event_titles import title_key
from core.event_knowledge_lookup import normalize_text


FAMILIES = {
    title_key(title): family
    for family,titles in (
        ('minor',('保重身体！','保重身體！','お大事に！')),
        ('major',('不许逞强！','不許逞強！','無茶は厳禁！')),
    ) for title in titles
}
HEADERS = {title_key(title) for title in (
    '养成优俊少女事件','養成優駿少女事件','养成赛马娘事件','育成赛马娘事件',
    '育成賽馬娘事件','育成ウマ娘イベント',
)}


def training_failure_choice(text, count, context):
    """With the opt-in, pick one valid option without pricing lost training."""
    if (context.get('cut_restart_authority', context.get('cut_farming_active')) is not True
            or context.get('restart_after_training_failure') is not True or count!=2):
        return None
    lines=[line.strip() for line in str(text).splitlines() if line.strip()]
    titles=[line for index,line in enumerate(lines) if title_key(line) in FAMILIES
            and index>0 and title_key(lines[index-1]) in HEADERS]
    if len(titles)!=1:return None
    return {'status':'selected','reason':'cut_training_failure_choose_then_restart','choice_index':1,
        'expected_event':(''.join(normalize_text(titles[0]).split()),'option:1','option:2'),
        'effect_basis':'user_enabled_training_failure_restart','candidates':[]}


def completed_training_failure(report, run_id):
    for step in report.get('step_reports',[]):
        result=step.get('result') or {};decision=result.get('decision') or {}
        if (decision.get('action')!='tap' or result.get('tap_performed') is False
                or not (result.get('tap_performed') is True or result.get('performed') is True)):
            continue
        evidence=decision.get('evidence') or {};selection=evidence.get('event_selection') or {}
        choice=selection.get('choice_index')
        count=(evidence.get('event_option_layout') or {}).get('option_count')
        expected=selection.get('expected_event')
        if count is None and isinstance(expected,(list,tuple)):count=len(expected)-1
        if type(choice) is not int or choice not in (1,2) or count!=2:continue
        text=(evidence.get('screen_text') or {}).get('normalized_text') or (result.get('frame_safety') or {}).get('visible_text','')
        lines=[title_key(line) for line in str(text).splitlines() if line.strip()]
        training=selection.get('last_training_action') or {}
        if training.get('run_session_id') not in {None,run_id}:continue
        bound_training=(training.get('status')=='accepted' and training.get('run_session_id')==run_id
                        and type(training.get('turn_index')) is int)
        matches=[]
        for index,title in enumerate(lines):
            if title not in FAMILIES:continue
            header=index>0 and lines[index-1] in HEADERS
            matched_choice=(bound_training and isinstance(expected,(list,tuple)) and expected
                            and title_key(expected[0])==title)
            if header or matched_choice:matches.append(title)
        if len(set(matches))!=1:continue
        title=matches[0]
        return {'run_id':run_id,'title':title,'family':FAMILIES[title],
            'selected_option':choice,'action_turn':training.get('turn_index') if bound_training else None,
            'training_type':training.get('training_type') if bound_training else None,
            'basis':'executed_choice_on_known_training_failure_event',
            'source_sha256':(result.get('frame_safety') or {}).get('source_image_sha256')}
    return None


def training_failure_should_restart(failure, *, ignore_wisdom=True, stop_at_senior_expedition=True, period=None):
    """Return True only when an observed training failure qualifies for auto-restart."""
    if not isinstance(failure, dict):
        return False
    if ignore_wisdom:
        ttype = failure.get('training_type')
        if ttype == 'wisdom' or ttype is None:
            return False
    if stop_at_senior_expedition and isinstance(period, dict):
        year, month = period.get('year_index'), period.get('month')
        if type(year) is int and type(month) is int and (year > 3 or (year == 3 and month >= 7)):
            return False
    return True
