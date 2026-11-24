"""Thin ordinary-opening adapter around the authoritative UMAA recognizer."""
import json
from pathlib import Path
import subprocess
import sys

from core.auto_trainer.cut_restart_route import button_boxes, visible_control
from scripts.ug_parent_one_turn_pipeline import _atomic_write_json


class OrdinaryTraineeSelection:
    def __init__(self, variant, d1_root, *, observer=None):
        self.variant, self.d1_root=variant,Path(d1_root)
        self.observer=observer or self.observe
        self.inspected=set()
        self.scans=0
        self.actions=0
        self.scroll_before=None
        self.reverse=False
        self.confirmed=False

    def submitted(self, observed):
        if not observed.get('trainee_action'):
            return
        self.actions+=1
        if observed['state']=='continue_trainee':
            self.confirmed=True
        elif observed['state']=='inspect_trainee':
            self.inspected.add(tuple(observed['candidate_point']))
        elif observed['state']=='scroll_trainees':
            self.scans+=1
            self.scroll_before=observed['trainee_observation'].get('signature')

    def observe(self, source, tokens):
        token_file=Path(source).with_suffix('.trainee-tokens.json')
        _atomic_write_json(token_file,tokens)
        worker=Path(__file__).resolve().parents[2]/'scripts/larc_d1_trainee_observer.py'
        result=subprocess.run([sys.executable,str(worker),'--d1-root',str(self.d1_root),
            '--image',str(source),'--tokens',str(token_file),'--variant',self.variant],
            capture_output=True,text=True,encoding='utf-8',timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise ValueError('ordinary_trainee_observer_failed')
        return json.loads(result.stdout)

    def analyze(self, source, image, tokens, scale=1):
        text=''.join(token['text'] for token in tokens)
        if not any(label in text for label in ('育成优俊少女选择','选择育成优俊少女',
                                               '养成优俊少女选择','选择养成优俊少女')):
            return None
        observed=self.observer(source,tokens)
        if (observed.get('selected_variant') != self.variant
                and self.observer == self.observe
                and image is not None and image.shape[:2] == (1280, 720)
                and not any(0 <= t['bbox'][0] < 380 and 380 <= t['bbox'][1] < 415 for t in tokens)):
            import cv2
            from core.auto_trainer.cut_condition_probe import ocr_tokens
            strip = image[382:422, 30:380]
            if strip.size and float(strip.std()) >= 5.0:
                strip_2x = cv2.resize(strip, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                strip_tokens = [
                    {**token, 'bbox': [
                        30.0 + token['bbox'][0] / 2.0, 382.0 + token['bbox'][1] / 2.0,
                        30.0 + token['bbox'][2] / 2.0, 382.0 + token['bbox'][3] / 2.0,
                    ]}
                    for token in ocr_tokens(strip_2x)
                ]
                if strip_tokens:
                    observed = self.observer(source, [*strip_tokens, *tokens])
        common={'next_phase':'setup','trainee_action':True,'trainee_observation':observed}
        if observed.get('selected_variant')==self.variant:
            point=visible_control(tokens,button_boxes(image),{'继续','下一步'})
            if point is None:
                return {**common,'state':'wait','reason':'trainee_select_content_loading'}
            return {**common,'state':'continue_trainee','reason':'exact_ordinary_trainee_selected',
                'center':[round(value*scale) for value in point]}
        signature=observed.get('signature')
        if self.scroll_before is not None and signature:
            unchanged=(len(signature)==len(self.scroll_before) and
                sum(abs(a-b) for a,b in zip(signature,self.scroll_before))/len(signature)<2)
            self.scroll_before=None
            if unchanged:
                if self.reverse:
                    return {**common,'state':'unknown','reason':'ordinary_trainee_not_found_after_bounded_scan'}
                self.reverse=True
            else:
                self.inspected.clear()
        if self.actions>=180:
            return {**common,'state':'unknown','reason':'ordinary_trainee_not_found_after_bounded_scan'}
        # Face scores only prioritize inspection. Like UMAA, also inspect the
        # other visible card-shaped candidates; only an exact outfit header
        # may authorize Continue, never the face hint alone.
        candidates=[item for item in observed.get('candidates',[])
            if item.get('confidence',0)>=.90 and tuple(item['point']) not in self.inspected]
        if candidates:
            candidate=max(candidates,key=lambda item:item['portrait_score'])
            return {**common,'state':'inspect_trainee','reason':'inspect_ordinary_trainee_hint',
                'candidate_point':candidate['point'],
                'center':[round(value*scale) for value in candidate['point']]}
        if self.scans>=12:
            return {**common,'state':'unknown','reason':'ordinary_trainee_not_found_after_bounded_scan'}
        if self.scans>=6:self.reverse=True
        start,end=((360,690),(360,920)) if self.reverse else ((360,920),(360,690))
        return {**common,'state':'scroll_trainees','reason':'search_ordinary_trainee',
            'gesture':[round(value*scale) for point in (start,end) for value in point], 'duration_ms':400}
