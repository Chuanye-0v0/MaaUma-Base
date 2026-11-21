"""Bind event choices to this runner's observed needs and explicit card facts."""
from copy import deepcopy
from dataclasses import replace
import json
import os
import re
from pathlib import Path

from core.auto_trainer.config import load_goal_config
from core.auto_trainer.larc_event_choice import select_event_option


def event_selector(ctx):
    if getattr(ctx, "event_goal", None) is None:
        ctx.event_goal = load_goal_config(ctx.root / "goal.json")
    context = {"run_session_id":ctx.run_id,"state": deepcopy(getattr(ctx, "event_state", {})),
               "state_basis": "last_observed_menu_current_event_energy", "bonuses_by_owner": {},
               "event_library_enabled":True}
    context["trainee_id"] = (getattr(ctx, "trainee_id", None)
                             or (getattr(ctx, "progress", None) or {}).get("trainee_id"))
    profile = getattr(ctx, "support_profile", None) or {}
    context["support_profile"] = profile
    hook=getattr(ctx,'cut_farming_hook',None)
    context['cut_farming_active']=hook is not None
    context['restart_after_training_failure']=getattr(
        getattr(getattr(hook,'session',None),'config',None),'restart_after_training_failure',False) is True
    def _cut_restart_authority():
        if hook is None or bool(getattr(hook,'continuation_flow',False)):
            return False
        cont=getattr(ctx,'continuation_runtime',None)
        if cont is not None and callable(getattr(cont,'can_continue_junior',None)) and cont.can_continue_junior():
            return False
        session=getattr(hook,'session',None)
        if session is not None:
            try:
                attempts=(session.data or {}).get('attempts') or []
                if attempts and attempts[-1].get('cut_found'):
                    return False
            except (AttributeError,TypeError):
                return False
        return True
    context['cut_restart_authority']=_cut_restart_authority()
    context['mei_gamble_authority']=True if hook is None else context['cut_restart_authority']
    context['trainee_variant_id']=getattr(getattr(getattr(hook,'session',None),'config',None),'character_id',None)
    if hook is None:
        # A completed new-career owner binds the selected costume to this run.
        # Mutable setup preferences are not identity evidence when taking over
        # an already running career.
        try:
            owner=json.loads((ctx.root/'session/ordinary_opening_owner.json').read_text(encoding='utf-8'))
        except (OSError,ValueError,TypeError):
            owner={}
        if not isinstance(owner,dict):owner={}
        identity=owner.get('trainee_identity') or {}
        if not isinstance(identity,dict):identity={}
        variant=identity.get('variant_id')
        if (owner.get('run_session_id')==ctx.run_id and owner.get('device')
                and owner['device']==(getattr(ctx,'progress',None) or {}).get('device')
                and owner.get('status')=='complete' and owner.get('new_career_started') is True
                and isinstance(variant,str) and re.fullmatch(r'\d{6}',variant)
                and identity.get('character_id')==variant[:4]
                and context['trainee_id'] in {None,variant[:4]}):
            context.update(trainee_variant_id=variant,trainee_id=variant[:4],
                trainee_identity_basis='current_run_confirmed_ordinary_start')
    if context.get('trainee_id') is None and getattr(ctx, 'root', None) is not None:
        try:
            obs_trainee = json.loads((ctx.root / 'session/larc_observed_trainee.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            obs_trainee = {}
        if isinstance(obs_trainee, dict):
            cid = obs_trainee.get('character_id')
            if (obs_trainee.get('run_session_id') == ctx.run_id
                    and isinstance(cid, str) and re.fullmatch(r'\d{4}', cid)):
                context['trainee_id'] = cid
                context.setdefault('trainee_identity_basis', 'current_run_observed_character_event')
    chain=context['state'].get('scenario_state',{}).get('larc_support_chain_context',{})
    if chain.get('run_session_id')==ctx.run_id:
        context['learned_skill_ids']=chain.get('learned_skill_ids',[])
    context["max_vitality"] = profile.get("max_vitality")
    for card in profile.get("cards", []):
        effects = card.get("actual_effects", {})
        bonus = {"event": effects.get("event_effectiveness_percent"),
                 "recovery": effects.get("event_recovery_percent")}
        identifiers = [card.get("card_id"), card.get("support_chara_id")]
        if card.get("character_id") == "satake_mei":
            identifiers.append("9043")
        for identifier in identifiers:
            if identifier:
                context["bonuses_by_owner"][str(identifier)] = bonus
    path = ctx.root / "session/larc_friend_outing.json"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("run_session_id") == ctx.run_id:
            context["outing_unlocked"] = saved.get("unlock_event", {}).get("status") == "unlocked"
    def select(image, text):
        context['cut_restart_authority']=_cut_restart_authority()
        context['mei_gamble_authority']=True if hook is None else context['cut_restart_authority']
        observed=getattr(ctx,"event_training_action",None)
        progress=getattr(ctx,"progress",None) or {}
        expected=progress.get("next_turn")
        context['next_action_turn']=expected
        from core.auto_trainer.runtime_control import current
        context['control_epoch']=current().epoch if current() is not None else None
        state_turn=context["state"].get("turn")
        context["last_training_action"]=(deepcopy(observed) if isinstance(observed,dict)
            and observed.get("status")=="accepted" and observed.get("run_session_id")==ctx.run_id
            and type(expected) is int and observed.get("turn_index")==expected-1==state_turn else None)
        context["acupuncture_pending_choice"] = getattr(ctx, "acupuncture_pending_choice", None)
        from core.auto_trainer.larc_policy import training_balance_v2_enabled
        prefer_recovery=hook is not None or training_balance_v2_enabled(ctx.event_goal)
        goal_events=ctx.event_goal.event_preferences or {}
        preferences={**({'conserve_energy':True} if prefer_recovery else {}),**goal_events}
        # The standalone form stores local user preferences separately from
        # batch budgets. Explicit profile-level event_preferences take precedence
        # so switching profiles never leaks another profile's manual overrides.
        configured=os.environ.get('UMA_CUT_CONFIG_PATH')
        preference_read={'settings_source':'profile_goal_config' if goal_events else 'goal_config','status':'loaded'}
        if configured:
            try:
                saved=json.loads(Path(configured).read_text(encoding='utf-8-sig'))
                updated=saved.get('event_preferences') or {}
                if not isinstance(updated,dict):raise ValueError('event_preferences_invalid')
                ctx._last_event_preferences=deepcopy(updated)
                if not goal_events:
                    preferences.update(updated)
                    preference_read={'settings_source':'portable_settings','status':'loaded',
                        'modified_at_unix_ms':int(Path(configured).stat().st_mtime*1000)}
            except (OSError,ValueError,TypeError) as error:
                if not goal_events:
                    preferences.update(getattr(ctx,'_last_event_preferences',{}))
                    context['event_preferences_warning']=type(error).__name__
                    preference_read={'settings_source':'last_read_settings','status':'read_failed','reason':type(error).__name__}
        context['event_preferences']=preferences
        from core.auto_trainer.larc_event_library import normalize_preferences
        from core.support_card_knowledge import default_library
        normalized=normalize_preferences(preferences)
        values=dict(preferences.get('skill_hint_values',{}))
        for field,score in (('target_skills',12.),('ignored_skills',0.)):
            for identity in normalized[field]:
                skill=default_library().skills.get(identity)
                for key in (skill['skill_id'],skill['db_id'],skill['name_zh_cn'],skill.get('name_ja')):
                    if key:values[key]=score
        context['skill_hint_values']=values
        context['event_preferences']=normalized
        result = select_event_option(image,text,context,replace(ctx.event_goal,event_preferences=normalized))
        result['preference_read']=preference_read
        if result.get('status')=='selected' and not context.get('trainee_id') and result.get('event_id'):
            try:
                from core.auto_trainer.larc_event_catalog import event_catalog
                row = event_catalog().get(result['event_id'])
                cid = str(row.get('character_id') or '') if isinstance(row, dict) and row.get('scope_kind')=='character' else ''
                if re.fullmatch(r'\d{4}', cid):
                    context['trainee_id'] = cid
                    context.setdefault('trainee_identity_basis', 'current_run_observed_character_event')
                    if getattr(ctx, 'root', None) is not None:
                        session_file = ctx.root / 'session/larc_observed_trainee.json'
                        session_file.parent.mkdir(parents=True, exist_ok=True)
                        session_file.write_text(
                            json.dumps({
                                'schema_version': 'uma.observed_trainee.v1',
                                'run_session_id': ctx.run_id,
                                'character_id': cid,
                                'event_id': result['event_id'],
                            }, ensure_ascii=False, indent=2),
                            encoding='utf-8',
                        )
            except (OSError, ValueError, TypeError, KeyError):
                pass
        if context.get('trainee_identity_basis'):
            result['trainee_identity_basis']=context['trainee_identity_basis']
        result['effective_preferences']={key:deepcopy(normalized[key]) for key in
            ('conserve_energy','energy_cost_floor','target_skills','ignored_skills','manual_choices','mei_unlock_policy','acupuncture_policy','acupuncture_choice') if key in normalized}
        if context.get('last_training_action') is not None:
            result['last_training_action']=deepcopy(context['last_training_action'])
        if "acupuncture_pending_choice" in result:
            ctx.acupuncture_pending_choice = result["acupuncture_pending_choice"]
        elif result.get("status") == "selected":
            ctx.acupuncture_pending_choice = None
        return result
    return select
