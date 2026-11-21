"""A race-owned retry, using a free attempt or an existing clock only."""
import re
import unicodedata

import cv2

from core.larc_growth_reader import _line


ACTION_ID = "race.clock_retry"
ENTRY_ACTION_ID = "race.clock_retry_open"
RESULT_RANK_ROI = (150, 250, 250, 370)
RESULT_HEADER_ROI = (80, 245, 315, 380)
RESULT_TITLE_ROIS = ((100, 505, 560, 545), (20, 495, 560, 550))


def _compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _race_title(text):
    value = _compact(text)
    return re.sub(r"^(?:GI{1,3}|G[123]|OP)(?=[\u4e00-\u9fff])", "", value, flags=re.I)


def _result_text_titles(text):
    titles = []
    for line in (text or "").splitlines():
        name = _race_title(line)
        # The title precedes course/standings and the result controls. In
        # particular the button 重新挑战 is not a race name ending in 战.
        if (name in {'重新挑战', '继续挑战', '下一页', '前往演出'}
                or retry_prompt_visible(name)
                or re.search(r'(?:草地|泥地|ダート)[0-9]{3,4}米', name)):
            break
        if (re.fullmatch(r'.{2,78}(?:赛|战|杯|赏|锦标|奖|德比|纪念)', name)
                and not name.startswith(('参加', '前往', '查看', '是否'))):
            titles.append(name)
    return tuple(dict.fromkeys(titles))


def retry_prompt_visible(text):
    """One retry-question vocabulary shared by routing and cost parsing."""
    return re.search(r"是否使用闹钟(?:进行)?(?:重新|继续)挑战", _compact(text)) is not None


def result_entry_text(text, *, race_name=None):
    normalized = _compact(text)
    name = race_name or '初级新人战'
    if (not isinstance(name,str) or not name or len(name)>80
            or not all(word in normalized for word in (name, "重新挑战", "下一页"))
            or "是否使用闹钟" in normalized
            or any(word in normalized for word in ("萝卜", "购买", "充值", "钻石", "使用宝石",
                "支付", "付费", "预览", "参赛确认", "出赛确认", "前往赛事", "查看结果"))):
        return None
    # The large rank glyphs may be reordered, or OCR may omit 第 entirely.
    # Only use the header above the verified race title; never a later row.
    # read_result_entry still requires that title and the real retry button.
    header = normalized.split(name, 1)[0]
    ranks = {int(value) for match in re.finditer(
        r"第([1-9][0-9]*)名|(?<![0-9A-Za-z.+-])([1-9][0-9]*)(?:名第|第名|名)", header)
        for value in match.groups() if value is not None}
    if len(ranks) != 1:
        return None
    rank = ranks.pop()
    if rank <= 1:return None
    return {'rank':rank,**({'race_name':name} if race_name is not None else {})}


def _confident(value):
    return type(value) in (int,float) and .9<=value<=1


def _ordinary_nonwinner(rank):
    return type(rank) is int and 2 <= rank <= 18


def _ordinary_nonwinner_proof(proof):
    candidates = proof.get('rank_candidates')
    if not isinstance(candidates, list) or not candidates:
        return False
    if not all(_ordinary_nonwinner(rank) for rank in candidates):
        return False
    header = proof.get('rank_header_ocr') or {}
    local = re.fullmatch(r'(?:第)?([1-9][0-9]*)(?:名)?', _compact(header.get('text')))
    exact = candidates[0] if len(candidates) == 1 else None
    return (local is not None and int(local[1]) in candidates
            and proof.get('rank_outcome') == 'nonwinner' and proof.get('rank') == exact)


def _ordinary_result_blocked(text):
    normalized = _compact(text)
    return (retry_prompt_visible(text) or any(word in normalized for word in (
        '萝卜', '购买', '充值', '钻石', '使用宝石', '支付', '付费', '预览',
        '参赛确认', '出赛确认', '前往赛事', '查看结果')))


def _header_text_rank(text):
    """Cross-check only text above the course/standings, never popularity."""
    lines = []
    titles = _result_text_titles(text)
    for line in (text or '').splitlines():
        value = _compact(line)
        if (re.search(r'(?:草地|泥地|ダート)[0-9]{3,4}米', value)
                or value in ('排名表', '重新挑战', '下一页')
                or value in titles):
            break
        lines.append(value)
    header = ''.join(lines)
    ranks = {int(value) for match in re.finditer(
        r'第([1-9][0-9]*)名|(?<![0-9A-Za-z.+-])([1-9][0-9]*)(?:名第|第名|名)', header)
        for value in match.groups() if value is not None}
    return ranks


def _result_controls(image):
    """Known two-button result layouts, including their disabled retry state."""
    edges = cv2.Canny(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), 40, 100)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    retry = []; next_buttons = []
    for x,y,w,h in {cv2.boundingRect(c) for c in contours}:
        if not (1100 <= y <= 1220 and 180 <= w <= 330 and 40 <= h <= 110):
            continue
        hsv = cv2.cvtColor(image[y+5:y+h-5,x+5:x+w-5], cv2.COLOR_BGR2HSV)
        white = float(((hsv[:,:,1] < 55) & (hsv[:,:,2] > 185)).mean())
        neutral = float((hsv[:,:,1] < 55).mean())
        if 30 <= x <= 130 and x+w < 370 and neutral > .5:
            # A dark/grey retry still identifies this result, but is not an
            # enabled control. The label's brown ink is checked separately.
            retry.append(([x,y,w,h], white > .5))
        green = float(((hsv[:,:,0] >= 35) & (hsv[:,:,0] <= 95)
            & (hsv[:,:,1] > 90) & (hsv[:,:,2] > 90)).mean())
        if 370 <= x <= 440 and x+w < 700 and green > .7:
            next_buttons.append([x,y,w,h])
    pairs = [(left, enabled, right) for left,enabled in retry for right in next_buttons
        if abs(left[1]-right[1]) <= 15 and abs(left[3]-right[3]) <= 20
        and 10 <= right[0]-left[0]-left[2] <= 85]
    return max(pairs, key=lambda pair:(pair[0][2]*pair[0][3],pair[2][2]*pair[2][3])) if pairs else None


def _ordinary_result_structure(image, text, reader=None, *, allow_text_rank=False):
    """Current result evidence; a race-name OCR suffix is not a click gate."""
    if (image is None or len(image.shape) != 3 or image.shape[0] <= image.shape[1]
            or _ordinary_result_blocked(text)):
        return None
    normalized = cv2.resize(image, (720,1280)) if image.shape[:2] != (1280,720) else image
    controls = _result_controls(normalized)
    if controls is None:
        return None
    # The result's white course band plus standings distinguish it from a
    # pre-race preview or an unrelated pair of bottom buttons.
    hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
    def white(roi):
        x1,y1,x2,y2 = roi
        crop = hsv[y1:y2,x1:x2]
        return float(((crop[:,:,1] < 65) & (crop[:,:,2] > 185)).mean())
    if white((15,505,705,595)) < .45 or white((25,620,695,1070)) < .45:
        return None
    ranks = _header_text_rank(text)
    if any(not _ordinary_nonwinner(rank) for rank in ranks):
        return None
    rank_reading = None
    for roi in (RESULT_HEADER_ROI, RESULT_RANK_ROI):
        label, confidence = _line(normalized, roi, reader)
        label = _compact(label)
        match = re.fullmatch(r'(?:第)?([1-9][0-9]*)(?:名)?', label)
        if match and _confident(confidence):
            rank = int(match[1])
            if not _ordinary_nonwinner(rank):
                return None
            rank_reading = dict(text=label, confidence=confidence, roi=list(roi))
            break
    if rank_reading is None:
        if not allow_text_rank or len(ranks) != 1:
            return None
        # Observation-only fallback: this keeps a losing result from silently
        # taking Next when its local rank OCR is temporarily unavailable.
        rank = next(iter(ranks))
        rank_reading = dict(source='current_header_text', text=str(rank), confidence=None)
    retry_box, enabled, next_box = controls
    candidates = sorted(ranks | {rank})
    # Retry needs only a losing result. Keep disagreement out of exact placing.
    return dict(rank=candidates[0] if len(candidates) == 1 else None,
        rank_candidates=candidates, rank_outcome='nonwinner', box=retry_box, next_box=next_box,
        retry_enabled=enabled, rank_header_ocr=rank_reading,
        race_name=next(iter(_result_text_titles(text)), None),
        layout='paired_result_controls', screen_size=[image.shape[1],image.shape[0]])


def _ordinary_result_entry(image, text, reader=None):
    return read_result_entry_and_candidate(image, text, reader=reader, ordinary=True)[0]


def read_result_entry_and_candidate(image, text, *, reader=None, ordinary=False):
    """Read this frame once; a candidate never grants entry permission.

    Entry still needs a confident local nonwinner read and the retry label.
    Text-only rank fallback belongs only to the observation candidate.
    """
    if not ordinary:
        entry = read_result_entry(image, text, reader=reader, ordinary=False)
        candidate = result_retry_candidate(image, text, reader=reader, ordinary=False) if entry is None else None
        return entry, candidate
    proof = _ordinary_result_structure(image, text, reader, allow_text_rank=True)
    if proof is None:
        return None, None
    candidate = dict(proof, kind='current_result_retry_candidate',
        source='current_result_layout_and_header', retry_box=proof['box'], authorizes_input=False)
    if not proof['retry_enabled'] or not _confident(proof['rank_header_ocr'].get('confidence')):
        return None, candidate
    normalized = cv2.resize(image, (720,1280)) if image.shape[:2] != (1280,720) else image
    x,y,w,h = proof['box']
    roi = (x+round(w*.30), y+round(h*.18), x+round(w*.93), y+round(h*.83))
    label, confidence = _line(normalized, roi, reader)
    if _compact(label) != '重新挑战' or not _confident(confidence):
        return None, candidate
    x1,y1,x2,y2 = roi
    ink = cv2.cvtColor(normalized[y1:y2,x1:x2], cv2.COLOR_BGR2HSV)
    brown = float(((ink[:,:,0] >= 5) & (ink[:,:,0] <= 30) & (ink[:,:,1] > 65)
        & (ink[:,:,2] > 50) & (ink[:,:,2] < 225)).mean())
    if brown < .03:
        return None, candidate
    if '下一页' not in _compact(text):
        x,y,w,h = proof['next_box']
        label_next, confidence_next = _line(normalized,
            (x+round(w*.15),y+round(h*.12),x+round(w*.85),y+round(h*.88)),reader)
        if _compact(label_next) != '下一页' or not _confident(confidence_next):
            return None, candidate
    proof.update(retry_label='重新挑战', retry_label_confidence=confidence,
        source='current_result_layout_and_header')
    return proof, candidate


def read_result_entry(image, text, *, reader=None, ordinary=False):
    """Locate the enabled, labeled result-page button, not a next-page tap."""
    if ordinary:
        return _ordinary_result_entry(image, text, reader)
    if image.shape[0] <= image.shape[1] or retry_prompt_visible(text):return None
    normalized=cv2.resize(image,(720,1280)) if image.shape[:2]!=(1280,720) else image
    proof = result_entry_text(text)
    if proof is None:return None
    edges=cv2.Canny(cv2.cvtColor(normalized,cv2.COLOR_BGR2GRAY),40,100)
    contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    boxes={cv2.boundingRect(c) for c in contours}
    for x,y,w,h in sorted(boxes,key=lambda box:box[2]*box[3],reverse=True):
        if not (30<=x<=130 and y>=1100 and 180<=w<=330 and 40<=h<=110 and x+w<370):continue
        hsv=cv2.cvtColor(normalized[y+5:y+h-5,x+5:x+w-5],cv2.COLOR_BGR2HSV)
        if float(((hsv[:,:,1]<55)&(hsv[:,:,2]>185)).mean())<.5:continue
        label,confidence=_line(normalized,(x+round(w*.30),y+round(h*.18),x+round(w*.93),y+round(h*.83)),reader)
        if re.sub(r'\s+','',label)!='重新挑战' or confidence<.9:continue
        proof.update(box=[x,y,w,h],screen_size=[image.shape[1],image.shape[0]])
        return proof
    return None


def result_retry_candidate(image, text, *, reader=None, ordinary=False):
    """A current losing result worth re-reading, never input/spending permission.

    Does not require title/retry-label OCR. Current header evidence plus paired
    result controls can stop an unintended Next while the owner re-observes.
    """
    if ordinary:
        proof = _ordinary_result_structure(image, text, reader, allow_text_rank=True)
        if proof is None:return None
        return dict(proof, kind='current_result_retry_candidate',
            source='current_result_layout_and_header', retry_box=proof['box'], authorizes_input=False)
    if image is None or image.shape[0] <= image.shape[1]:return None
    names = ('初级新人战',)
    proofs = [proof for name in names if (proof := result_entry_text(text, race_name=name))]
    if len(proofs) != 1:return None
    normalized = cv2.resize(image, (720,1280)) if image.shape[:2] != (1280,720) else image
    edges = cv2.Canny(cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY), 40, 100)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    retry_boxes = []; next_boxes = []
    for x,y,w,h in {cv2.boundingRect(c) for c in contours}:
        if not (y >= 1100 and 180 <= w <= 330 and 40 <= h <= 110):continue
        hsv = cv2.cvtColor(normalized[y+5:y+h-5,x+5:x+w-5], cv2.COLOR_BGR2HSV)
        if (30 <= x <= 130 and x+w < 370
                and float(((hsv[:,:,1] < 55) & (hsv[:,:,2] > 185)).mean()) > .5):
            retry_boxes.append([x,y,w,h])
        if (370 <= x <= 440 and x+w < 700
                and float(((hsv[:,:,0] >= 35) & (hsv[:,:,0] <= 95)
                    & (hsv[:,:,1] > 90) & (hsv[:,:,2] > 90)).mean()) > .7):
            next_boxes.append([x,y,w,h])
    if not retry_boxes or not next_boxes:return None
    return dict(proofs[0], kind='current_result_retry_candidate',
        source='current_header_title_text_and_result_controls',
        retry_box=max(retry_boxes,key=lambda b:b[2]*b[3]),
        next_box=max(next_boxes,key=lambda b:b[2]*b[3]), authorizes_input=False)


def entry_point(proof):
    x,y,w,h=proof['box'];width,height=proof['screen_size']
    return round((x+w/2)*width/720),round((y+h/2)*height/1280)


def authorized_entry(policy,session,decision,text, *, image=None):
    proof=decision.get('evidence',{}).get('clock_retry_entry',{})
    ordinary=getattr(policy,'allow_result_retry_entry',False) is True
    if ordinary:
        # Reuse the executor's current frame. A fabricated decision or text-only
        # claim cannot grant entry permission; the existing source SHA gate
        # still binds this frame to the action and receipt before input.
        observed = read_result_entry(image, text, ordinary=True) if image is not None else None
        facts = ('rank_outcome','box','next_box','retry_enabled','layout','screen_size','retry_label','source')
        if (observed is None or not _ordinary_nonwinner_proof(proof)
                or any(proof.get(key) != observed.get(key) for key in facts)
                or not _confident(proof.get('rank_header_ocr',{}).get('confidence'))
                or not _confident(proof.get('retry_label_confidence'))):
            return False
        return bool(policy.scenario=='larc' and policy.allow_clock_retry and not getattr(policy,'clock_retry_unresolved',False) and policy.post_entry_only
            and proof.get('screen_size') in ([720,1280],[1440,2560])
            and not policy.inspection_only and not policy.count_reserved_only
            and session is not None and session.clock_retries_used<5 and not session.clock_retry_entry_pending
            and decision.get('action')=='tap' and decision.get('reason')=='open_lost_race_retry'
            and decision.get('tap')=={'coordinate_name':ENTRY_ACTION_ID,'x':entry_point(proof)[0],
                'y':entry_point(proof)[1],'scaled':False})
    observed=result_entry_text(text)
    if proof.get('screen_size') not in ([720,1280],[1440,2560]) or len(proof.get('box',[]))!=4:return False
    return bool(policy.scenario=='larc' and policy.allow_clock_retry and not getattr(policy,'clock_retry_unresolved',False) and policy.allow_debut_retry_entry
        and policy.post_entry_only and not policy.inspection_only and not policy.count_reserved_only
        and session is not None and session.clock_retries_used<5 and not session.clock_retry_entry_pending
        and observed and proof.get('rank')==observed['rank']
        and decision.get('action')=='tap' and decision.get('reason')=='open_lost_race_retry'
        and decision.get('tap')=={'coordinate_name':ENTRY_ACTION_ID,'x':entry_point(proof)[0],'y':entry_point(proof)[1],'scaled':False})


def retry_text(text):
    text = _compact(text)
    if not retry_prompt_visible(text):
        return None
    if any(word in text for word in ("萝卜", "宝石", "购买", "充值", "钻石", "训练失败")):
        return None
    rank = re.search(r"第([1-9][0-9]*)名", text)
    remaining = re.search(r"重新挑战次数还剩([0-9])次", text)
    if not remaining or (rank and int(rank[1]) <= 1) or int(remaining[1]) < 1:
        return None
    return {"rank": int(rank[1]) if rank else None, "remaining": int(remaining[1]),
            "free": any(phrase in text for phrase in (
                "每天1次免费机会", "每天1次免费", "今日免费1次", "今日1次免费"))}


def valid_cost(proof):
    before, after = proof.get("before"), proof.get("after")
    return (type(before) is int and type(after) is int and before >= 0 and after >= 0
            and ((proof.get("free") is True and before == after)
                 or (before > 0 and after == before - 1)))


def read_retry(image, text, *, reader=None):
    proof = retry_text(text)
    if proof is None:
        return None
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    normalized = cv2.resize(image, (720, 1280)) if image.shape[:2] != (1280, 720) else image
    layout = SafeAdvancePlanner._final_loss_recovery_decision(image, {})
    if layout and layout.get("reason") == "cancel_clock_retry":
        proof["layout"] = "modal"
        rois = ((408, 738, 481, 790), (511, 738, 587, 790))
    elif _full_page(normalized):
        # L'Arc has a full-page retry with supporter points above the clock.
        proof["layout"] = "full_page"
        rois = ((408, 863, 481, 918), (511, 863, 587, 918))
        if proof["rank"] is None:
            rank, confidence = _line(normalized, (60, 140, 106, 185), reader)
            if re.fullmatch(r"[1-9][0-9]*", rank) and confidence >= .9:
                proof["rank"] = int(rank)
    else:
        return None
    if proof["rank"] is None or proof["rank"] <= 1:
        return None
    for key, roi in zip(("before", "after"), rois):
        value, confidence = _line(normalized, roi, reader)
        if not re.fullmatch(r"[0-9]{1,5}", value) or confidence < .9:
            return None
        proof[key] = int(value)
    # These two values are the same ready confirmation's cost preview, not
    # inventory sampled before/after a submitted retry. A verified zero-cost
    # preview can establish no stock consumption without an exact free banner.
    proof['free_basis'] = 'daily_free_text' if proof['free'] else None
    if proof['before'] == proof['after']:
        proof['free'] = True
        proof['free_basis'] = 'zero_stock_cost_preview'
    proof["screen_size"] = [image.shape[1], image.shape[0]]
    return proof if valid_cost(proof) else None


def _full_page(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    def ratio(roi, color):
        x1,y1,x2,y2 = roi
        return float(color(hsv[y1:y2,x1:x2]).mean())
    green = lambda r: (r[:,:,0] >= 35) & (r[:,:,0] <= 90) & (r[:,:,1] > 90) & (r[:,:,2] > 100)
    white = lambda r: (r[:,:,1] < 50) & (r[:,:,2] > 185)
    pink = lambda r: (r[:,:,0] >= 145) & (r[:,:,0] <= 178) & (r[:,:,1] > 35)
    return (ratio((20,35,700,90), green) > .7 and ratio((35,490,690,595), white) > .75
            and ratio((180,850,267,935), pink) > .15
            and ratio((65,1150,337,1210), white) > .5
            and ratio((385,1150,650,1210), green) > .7)


def retry_point(proof):
    width, height = proof["screen_size"]
    y = 1182 / 1280 if proof["layout"] == "full_page" else .717
    return round(width * .72), round(height * y)


def authorized_retry(policy, session, decision, text):
    """Executor permission; a decision cannot grant its own spending authority."""
    proof = decision.get("evidence", {}).get("clock_retry", {})
    observed = retry_text(text)
    tap = decision.get("tap", {})
    size = proof.get("screen_size", [])
    if size not in ([720, 1280], [1440, 2560]) or proof.get("layout") not in ("modal", "full_page"):
        return False
    x, y = retry_point(proof)
    return bool(
        policy.scenario == "larc" and policy.allow_clock_retry and not getattr(policy,"clock_retry_unresolved",False) and policy.post_entry_only
        and not policy.inspection_only and not policy.count_reserved_only
        and session is not None and session.clock_retries_used < 5
        and decision.get("action") == "tap" and decision.get("reason") == "retry_lost_race"
        and tap == {"coordinate_name": ACTION_ID, "x": x, "y": y, "scaled": False}
        and observed and all(proof.get(key) == value for key, value in observed.items()
            if value is not None and key != 'free')
        and (proof.get('free') == observed.get('free')
             or (proof.get('free') is True and proof.get('free_basis') == 'zero_stock_cost_preview'
                 and proof.get('before') == proof.get('after')))
        and type(proof.get("rank")) is int and proof["rank"] > 1
        and valid_cost(proof)
        and (getattr(policy,"clock_retry_remaining",None) is None or proof.get("free") is True or policy.clock_retry_remaining > 0)
        and (session.last_clock_retry_remaining is None
             or proof["remaining"] < session.last_clock_retry_remaining)
    )



def decline_retry(image, text):
    """Recognize the same existing cancel button; no resource authorization."""
    if image is None or not retry_prompt_visible(text):
        return None
    from core.auto_trainer.safe_advance import SafeAdvancePlanner
    decision = SafeAdvancePlanner._final_loss_recovery_decision(image, {})
    if decision and decision.get('reason') == 'cancel_clock_retry':
        return {'layout':'modal','screen_size':[image.shape[1],image.shape[0]]}
    normalized=cv2.resize(image,(720,1280)) if image.shape[:2]!=(1280,720) else image
    if _full_page(normalized):
        return {'layout':'full_page','screen_size':[image.shape[1],image.shape[0]]}
    return None


def decline_point(proof):
    width,height=proof['screen_size']
    return round(width*.28),round(height*(1182/1280 if proof['layout']=='full_page' else .717))


def authorized_decline(policy, decision, text, image):
    proof=decline_retry(image,text)
    if proof is None or policy.scenario!='larc' or not policy.post_entry_only or getattr(policy,"clock_retry_remaining",None) is None:
        return False
    observed=read_retry(image,text) if policy.allow_clock_retry else None
    exhausted=(not policy.allow_clock_retry or observed is None
        or observed.get('free') is not True and policy.clock_retry_remaining<=0)
    x,y=decline_point(proof)
    return (exhausted and decision.get('action')=='tap' and decision.get('reason')=='cancel_clock_retry'
        and decision.get('evidence',{}).get('clock_retry_decline')==proof
        and decision.get('tap')=={'coordinate_name':'safe_advance.clock_retry_cancel','x':x,'y':y,'scaled':False})
