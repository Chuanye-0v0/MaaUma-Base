"""The catalog's shared 10/30-energy meal, with the user's recovery preference."""
import math
import re

from core.auto_trainer.larc_event_matching import title_resolution,title_diagnostic


SMALL='体力+10技能Pt+5'
LARGE='成功時：体力+30技能Pt+10失敗時：体力+30技能Pt+10速度-5力量+5『太り気味』になる'


def _has_meal_effects(row):
    return (row['source']=='character_reference' and row['scope_kind']=='character'
            and [re.sub(r'\s+','',option['effects']) for option in row['options']]==[SMALL,LARGE])


def _exact_meal_family(text):
    from core.auto_trainer.larc_event_catalog import event_catalog
    from core.event_titles import compare_title,title_key
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    by_key={}
    for index,line in enumerate(lines):
        by_key.setdefault(title_key(line),[]).append((index,line))
    hits=[]
    for row in event_catalog().values():
        for title in row['titles']:
            for index,line in by_key.get(title_key(title),[]):
                hits.append({'row':row,'line_index':index,'title_match':compare_title(line,title),
                             'scope_basis':'exact_equivalent_meal_family'})
    if (not hits or len({hit['line_index'] for hit in hits})!=1
            or any(not _has_meal_effects(hit['row']) for hit in hits)):
        return None
    return {'hit':hits[0], 'resolution':{'status':'matched','matches':hits}, 'family_only':True}


def resolve_meal(text,count,context):
    if count!=2:return None
    resolved=title_resolution(text,count,context)
    if resolved['status']=='unmatched':
        return _exact_meal_family(text)
    if resolved['status']!='matched':return None
    matches=[hit for hit in resolved['matches'] if _has_meal_effects(hit['row'])]
    if not matches:return None
    # Same story's costume/common copies have the identical verified template.
    hit=next((hit for hit in matches if hit['row'].get('trainee_variant_id')),matches[0])
    return {'hit':hit,'resolution':resolved}


def choose_meal(match,state,context):
    from core.auto_trainer.larc_event_choice import _compact
    hit=match['hit'];row=hit['row'];body=state.vitality
    family_only=match.get('family_only') is True
    maximum=context.get('max_vitality')
    known_capacity=type(maximum) in (int,float) and math.isfinite(maximum) and 100<=maximum<=120
    known_body=type(body) in (int,float) and math.isfinite(body) and 0<=body<=100
    capacities=[maximum,maximum] if known_capacity else [100,120]
    gap=[round((100-body)*capacity/100,4) for capacity in capacities] if known_body else None
    lower=known_body and gap[0]>10
    reason=('meal_30_recovery_for_energy_gap' if lower else 'meal_current_energy_unreadable' if not known_body
            else 'meal_gap_capacity_unknown' if gap[1]>10 else 'meal_small_energy_gap')
    return {'status':'selected' if known_body else 'fallback','reason':reason,'choice_index':2 if lower else 1,
        'event_id':None if family_only else row['key'],'story_id':None if family_only else row.get('story_id'),
        **({'event_family':'equivalent_character_meal'} if family_only else {}),
        'event_policy_version':'larc_events_v6',
        'expected_event':(_compact(hit['title_match']['observed_title']),'option:1','option:2'),
        'title_match':hit['title_match'],'title_resolution':title_diagnostic(match['resolution']),
        'effect_basis':'existing_local_meal_template; reference_outcomes_not_observed',
        'vitality':body,'meal_preference':{'deficit_threshold_points':10,'deficit_points_bounds':gap,
            'capacity_basis':'profile' if known_capacity else '100_120_bounds',
            'lower_option_energy_points':30,'rule':'user_prefers_lower_when_deficit_greater_than_10',
            'uncertain_capacity_boundary':'lower_requires_minimum_possible_gap_greater_than_10'},
        'candidates':[{'index':1,'energy_points':10,'skill_pt':5},
            {'index':2,'energy_points':30,'skill_pt':10,'branch_probability':None,
             'failure_branch':{'speed':-5,'power':5,'condition_reference':'太り気味'},
             'risk_policy':'user_requested_meal_recovery_preference'}],
        'learned_skills_written':False}
