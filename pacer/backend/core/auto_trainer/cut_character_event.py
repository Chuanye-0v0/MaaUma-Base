"""Completed, costume-bound small-cut events; title sightings never discard."""
from functools import lru_cache
from core.event_titles import title_key


@lru_cache(maxsize=128)
def character_events(variant):
    from core.auto_trainer.larc_event_catalog import event_catalog
    return tuple(row for row in event_catalog().values()
        if row.get('scope_kind')=='character' and row.get('trainee_variant_id')==variant
        and row.get('story_id') and 2<=row.get('option_count',0)<=5
        and any('才能出众' in option.get('effects','') for option in row['options']))


def completed_character_event(report, variant):
    candidates=character_events(variant)
    for row in report.get('step_reports',[]):
        result=row.get('result') or {};decision=result.get('decision') or {}
        if not (result.get('tap_performed') is True or result.get('performed') is True):continue
        evidence=decision.get('evidence') or {};selection=evidence.get('event_selection') or {}
        choice=selection.get('choice_index')
        if type(choice) is not int:continue
        text=(evidence.get('screen_text') or {}).get('normalized_text') or (result.get('frame_safety') or {}).get('visible_text','')
        lines={title_key(line) for line in text.splitlines() if line.strip()}
        count=(evidence.get('event_option_layout') or {}).get('option_count')
        expected=selection.get('expected_event')
        if count is None and isinstance(expected,list):count=len(expected)-1
        matches=[event for event in candidates if any(title_key(t) in lines for t in event['titles'])
                 and choice in range(1,event['option_count']+1)
                 and (count is None or count==event['option_count'])
                 and (count==event['option_count'] or selection.get('story_id')==event['story_id']
                      or selection.get('event_id')==event['key'])]
        if len(matches)==1:
            event=matches[0]
            return {'story_id':event['story_id'],'event_id':event['key'],'title':event['title'],
                'trainee_variant_id':variant,'selected_option':choice,
                'basis':'executed_choice_and_exact_decorated_title_for_current_costume'}
    return None
