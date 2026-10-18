"""Per-request activity progress and a positively observed carousel sweep."""
from desktop.umaa_public_context import endpoint as _public_endpoint
from copy import deepcopy
import json
from pathlib import Path
from core.story_event.banner import WHITELIST, EXCLUDED_BANNERS

def pending(context):
    return bool(context.get('pending_spin') or context.get('pending_setting') or context.get('action_outcome', {}).get('execution_status') == 'uncertain' or (context.get('execution_status') == 'uncertain'))

def read_resume(source, endpoint):
    source = Path(source).resolve()
    try:
        prior = json.loads(source.read_text(encoding='utf8'))
        context = json.loads((source.parent / 'context.json').read_text(encoding='utf8'))
    except (OSError, ValueError) as error:
        raise RuntimeError('event_resume_source_unverified') from error
    cycle = context.get('event_cycle') if isinstance(context, dict) else None
    observed = prior.get('last_observation', {}) if isinstance(prior, dict) else {}
    if endpoint != _public_endpoint() or not isinstance(prior, dict) or prior.get('ok') is not False or (not isinstance(cycle, dict)) or (cycle.get('version') != 1) or (cycle.get('endpoint') != endpoint) or (not isinstance(cycle.get('events'), dict)) or (cycle.get('active_event_id') not in {*WHITELIST, None}) or (prior.get('endpoint', endpoint) != endpoint) or (observed.get('endpoint', endpoint) != endpoint) or (prior.get('output_dir') and Path(prior['output_dir']).resolve() != source.parent):
        raise RuntimeError('event_resume_source_unverified')
    for event_id, event in cycle['events'].items():
        if event_id not in WHITELIST or not isinstance(event, dict) or type(event.get('ok')) is not bool or (event.get('ok') is True and event.get('reason') != 'event_home_verified'):
            raise RuntimeError('event_resume_progress_unverified')
    if prior.get('action_outcome', {}).get('execution_status') == 'uncertain':
        context['action_outcome'] = deepcopy(prior['action_outcome'])
    receipt_path = source.parent / 'recovery/last-action.json'
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf8'))
        except (OSError, ValueError) as error:
            raise RuntimeError('event_resume_receipt_unverified') from error
        if not isinstance(receipt, dict):
            raise RuntimeError('event_resume_receipt_unverified')
        if receipt.get('execution_status') == 'uncertain':
            context['action_outcome'] = receipt
    return context

class CarouselScan:
    """Two stable frames per slot, all slots covered, then return to first slot."""

    def __init__(self):
        self.count = None
        self.first = None
        self.departed = False
        self.previous = None
        self.slots = {}
        self.complete = False

    def observe(self, state):
        dots = state.get('carousel')
        event_id = state.get('banner_id')
        kind = event_id if event_id in WHITELIST else 'excluded:' + state['banner_excluded'] if state.get('banner_excluded') in EXCLUDED_BANNERS else 'unrecognized' if state.get('banner_scores') and max(state['banner_scores'].values()) < 0.5 else None
        if state.get('page') != 'home' or not dots or kind is None:
            self.previous = None
            return None
        sample = (dots['slot_count'], dots['selected_slot'], kind)
        if self.count is not None and sample[0] != self.count:
            raise RuntimeError('event_carousel_layout_changed')
        if sample != self.previous:
            self.previous = sample
            return None
        self.count = sample[0]
        slot = sample[1]
        if slot in self.slots and self.slots[slot] != kind:
            raise RuntimeError('event_carousel_slot_changed')
        self.slots[slot] = kind
        if self.first is None:
            self.first = slot
        if slot != self.first:
            self.departed = True
        self.complete = self.departed and slot == self.first and (len(self.slots) == self.count)
        return kind

    def report(self):
        return dict(complete=self.complete, slot_count=self.count, slots={str(k): v for k, v in sorted(self.slots.items())}, unrecognized_slots=sorted((k for k, v in self.slots.items() if v == 'unrecognized')))
