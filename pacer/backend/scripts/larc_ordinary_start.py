"""Reuse U2's ordinary setup only: one start, no cut checks or abandonment."""
from functools import partial
from pathlib import Path

from core.auto_trainer.cut_farming import CutFarmingConfig, CutSession
from core.auto_trainer.cut_restart_route import CutRestartRoute, analyze_restart_frame


def observe_current_support_deck(image, tokens, source):
    """Read the six already-visible cards; this helper performs no navigation."""
    try:
        import re
        from core.auto_trainer.cut_condition_probe import _default_ocr_reader
        from core.auto_trainer.cut_support_selection import read_deck_cards
        reader = _default_ocr_reader()._get_engine()
        cards = read_deck_cards(image, tokens, reader)
        deck = next((int(match[1]) for token in tokens
                     if (match := re.fullmatch(r'卡组\s*(\d+)', token.get('text', '')))), None)
        return {'cards': cards, 'deck': deck, 'source': str(source),
                'kind': 'ordinary_visible_support_deck'}
    except Exception:
        # Optional deck facts must never turn an otherwise valid start into
        # a stopped career or change the user's currently selected cards.
        # Preserve an unknown observation so stale static slots cannot be
        # reused as a complete deck after this fresh visible-page read failed.
        return {'cards': [], 'deck': None, 'source': str(source),
                'kind': 'ordinary_visible_support_deck', 'status': 'unknown',
                'reason': 'current_support_cards_unreadable'}


def start_preselected_ordinary_career(ctx, output_dir, *, trainee_name, trainee_variant,
                                    max_tp_items=1, reference_dir=None, route_factory=CutRestartRoute,
                                    setup=None, d1_root=None, batch_session=None):
    output = Path(output_dir)
    config = CutFarmingConfig.from_dict({
        "character_id": str(trainee_variant), "validation_mode": True,
        "validation_character_name": trainee_name, "device_port": ctx.port,
        "strategy": "beta", "max_attempts": 1, "max_tp_items": max_tp_items,
        "max_carrots": 0, "setup_reference_dir": str(reference_dir) if reference_dir else None,
        **{key:(setup or {}).get(key,default) for key,default in (
            ('support_deck',0),('borrow_card_id',0),('borrow_min_level',1),('borrow_min_break',0))}})
    session = CutSession(output / "preparation", config) if batch_session is None else batch_session
    if batch_session is not None:
        fields = ('character_id', 'validation_character_name', 'device_port', 'support_deck',
                  'borrow_card_id', 'borrow_min_level', 'borrow_min_break')
        if any(getattr(session.config, field) != getattr(config, field) for field in fields):
            raise ValueError('ordinary_setup_batch_configuration_changed')
    selection = None
    if d1_root and str(trainee_variant).isdigit():
        from core.auto_trainer.ordinary_trainee_selection import OrdinaryTraineeSelection
        selection = OrdinaryTraineeSelection(str(trainee_variant), d1_root)
    scenario_scans=0
    from scripts.larc_ordinary_setup import deck_profile_observation_enabled
    observe_deck = deck_profile_observation_enabled(ctx)
    def perceive(source, **kwargs):
        if setup is not None:
            import cv2
            from core.perception import Perception
            from core.auto_trainer.safe_advance import SafeAdvancePlanner
            from core.auto_trainer.cut_condition_probe import ocr_tokens
            from core.frame_io import read_image_with_retry
            image=read_image_with_retry(source)
            if image is None or image.shape[:2] not in {(1280,720),(2560,1440)}:
                return {'state':'unknown','reason':'ordinary_entry_frame_unreadable'}
            scale=image.shape[1]/720
            canonical=cv2.resize(image,(720,1280))
            if SafeAdvancePlanner._continue_training_modal_button(Perception().analyze_image(canonical),canonical):
                return {'state':'existing_career_handoff','reason':'existing_career_visible'}
            tokens=ocr_tokens(canonical)
            text=''.join(token['text'] for token in tokens)
            base=analyze_restart_frame(source,reader=lambda _:tokens,**kwargs)
            # The shared owner identifies foreground inheritance/support/paid
            # modals before any trainee header left behind by a transition.
            if selection and (base.get('state')=='continue_trainee' or base.get('reason') in {
                    'retained_character_version_unverified','trainee_select_content_loading'}):
                result=selection.analyze(source,canonical,tokens,scale)
                if result is not None:return result
            if selection and not selection.confirmed and any(label in text for label in (
                    '继承技能','契合度','协助卡编成','辅助卡编成','支援卡编成','最终确认')):
                from core.auto_trainer.cut_restart_route import button_boxes,visible_control
                # Resume an interrupted setup from its live page, but verify
                # the exact target outfit before allowing its final TP input.
                point=visible_control(tokens,button_boxes(canonical),{'返回'})
                if point is not None and base.get('reason')!='resource_action_outside_cut_scope':
                    return {'state':'ordinary_back_to_trainee','reason':'verify_configured_costume',
                        'center':[round(value*scale) for value in point],
                        'next_phase':'setup','trainee_action':True}
                if base.get('ordinary_start_verified'):
                    return {'state':'unknown','reason':'ordinary_trainee_version_unverified'}
            if base.get('reason')=='selected_scenario_not_larc':
                from core.auto_trainer.cut_restart_route import button_boxes,visible_control
                # Same carousel control as UMAA's preparation-only route.
                if scenario_scans>=8:
                    return {'state':'unknown','reason':'larc_scenario_not_found_after_bounded_scan'}
                if visible_control(tokens,button_boxes(canonical),{'下一步','继续'}) is not None:
                    return {'state':'next_scenario','reason':'select_larc_scenario',
                        'center':[round(691*scale),round(584*scale)],'next_phase':'setup',
                        'trainee_action':True}
            if (observe_deck and base.get('state') == 'continue_support'
                    and not base.get('support_observation')):
                observed = observe_current_support_deck(canonical, tokens, source)
                if observed is not None:
                    base = {**base, 'support_observation': observed}
            return base
        return analyze_restart_frame(source,**kwargs)
    def submitted(observed):
        nonlocal scenario_scans
        if selection:selection.submitted(observed)
        if observed['state']=='next_scenario':scenario_scans+=1
    route = route_factory(session, perceptor=perceive if setup is not None
                          else partial(analyze_restart_frame, check_start_tp=False),
                          **({'on_submitted':submitted} if setup is not None else {}))
    # No CutFarmingRunner or training hook is created. Return after the
    # ordinary start; normal L'Arc opening/event handling owns the next page.
    result=route(ctx, output / "pages", start_phase="setup")
    if session.data.get('support_setup_observation'):
        result={**result,'support_setup_observation':session.data['support_setup_observation']}
    return result
