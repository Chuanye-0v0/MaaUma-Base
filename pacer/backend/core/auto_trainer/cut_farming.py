"""Dedicated cut farming rules and durable stop-before-notify ownership."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import time
from time import sleep


CHARACTERS = {
    '101001': {'name': '原皮大树快车', 'events': {'501010515': 'TO THE NO.1！', '501010523': '交给牛仔女孩吧☆'}},
    '103102': {'name': '情人节风神', 'events': {'501031802': '尽管交给姐姐吧！', '501031523': '新的挑战！'}},
    '106501': {'name': '原皮大拓太阳神', 'events': {'501065515': "言语即灵魂！It's show time！", '501065523': '爆速领跑赞爆了☆'}},
}
EVENT_OWNERS={'101001':'タイキシャトル（ワイルド・フロンティア）',
    '103102':'アイネスフウジン（MELTYGIFT）','106501':'ダイタクヘリオス（Fun☆Fun☆ぱりない）'}
EVENT_CACHE = str(Path(__file__).resolve().parents[2]/'.cache/event_knowledge/umaassistant_personal_v1.json')
TERMINAL_STATES = {'found', 'unknown', 'stopped', 'limit_reached', 'route_failed', 'handoff'}
CHARACTER_ROSTER = Path(__file__).resolve().parents[1]/'autonomous_training/plant_roster.json'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    # The native panel can have this file open for a snapshot on Windows.
    # Retry only publication of the already-written state, never a game action.
    try:
        for delay in (0.02, 0.05, 0.1, 0.2, 0.4, None):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if getattr(exc, 'winerror', None) not in {5, 32, 33} or delay is None:
                    raise
                sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)


def normalize_title(value):
    from core.event_titles import title_key
    return title_key(value)


@dataclass(frozen=True)
class CutFarmingConfig:
    character_id: str
    device_port: int = 16384
    strategy: str = 'baseline'
    max_attempts: int = 20
    max_tp_items: int = 0
    max_carrots: int | None = 0  # None is an explicit unlimited carrot budget.
    refill_operation_limit: int | None = None
    event_cache: str = EVENT_CACHE
    pushplus_token_file: str | None = None
    setup_reference_dir: str | None = None
    validation_mode: bool = False
    validation_character_name: str | None = None
    check_mode: str = 'event_triggered'
    fallback_interval_turns: int = 4
    debut_clock_retry: bool = True
    discard_found_for_validation: bool = False
    benchmark_until_cutoff: bool = False
    restart_after_character_cut_miss: bool = False
    restart_after_training_failure: bool = False
    retain_screenshots: bool = True
    support_deck: int = 0
    borrow_card_id: int = 0
    borrow_min_level: int = 1
    borrow_min_break: int = 0
    continuation: dict | None = None

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError('刷切配置应为对象。')
        allowed = set(cls.__dataclass_fields__) | {'schema_version'}
        if set(value) - allowed:
            raise ValueError('刷切配置包含未知参数。')
        parsed = {key: item for key, item in value.items() if key != 'schema_version'}
        # Missing strategy in a saved legacy record meant alpha; reading it
        # must not adopt the new constructor default or rewrite that record.
        parsed.setdefault('strategy', 'alpha')
        config = cls(**parsed)
        if config.continuation is not None:
            from core.auto_trainer.larc_continuation import normalize, feature_available
            object.__setattr__(config, 'continuation', normalize(config.continuation))
            if config.continuation['enabled'] and not feature_available(Path(__file__).resolve().parents[2]):
                raise ValueError('当前版本未启用续养；稳定版不会执行。')
            if config.continuation['enabled'] and (config.discard_found_for_validation or config.benchmark_until_cutoff):
                raise ValueError('续养不能与弃切耐久测试或截止采集模式同时启用。')
        if type(config.validation_mode) is not bool:
            raise ValueError('validation_mode必须明确填写true或false。')
        if config.validation_mode:
            if not isinstance(config.validation_character_name,str) or not config.validation_character_name.strip():
                raise ValueError('任意角色验证模式需要当前所选角色的确切中文名。')
            if not isinstance(config.character_id,str) or not config.character_id:
                raise ValueError('验证模式请填写角色ID或validation。')
        elif config.character_id not in CHARACTERS:
            raise ValueError('刷切目前只支持原皮大树快车、情人节风神和原皮大拓太阳神。')
        from core.cut_portable_runtime import device_ports
        if config.device_port not in device_ports() or config.strategy not in {'alpha', 'beta', 'baseline', 'baseline_v118', 'resource_v119'}:
            raise ValueError('请选择支持的设备和统一基线。')
        if config.check_mode not in {'event_triggered','every_turn'}:
            raise ValueError('查切模式请选择事件触发或每回合对照。')
        if type(config.debut_clock_retry) is not bool:
            raise ValueError('出道闹钟重赛开关应为true或false。')
        if type(config.restart_after_character_cut_miss) is not bool:
            raise ValueError('角色专属切事件后重开开关应为true或false。')
        if type(config.restart_after_training_failure) is not bool:
            raise ValueError('训练失败后重开开关应为true或false。')
        if type(config.retain_screenshots) is not bool:
            raise ValueError('保留截图开关应为true或false。')
        if type(config.discard_found_for_validation) is not bool or (config.discard_found_for_validation and not config.validation_mode):
            raise ValueError('通知后弃养有切育成仅可在明确的验证模式中启用。')
        if type(config.benchmark_until_cutoff) is not bool or (config.benchmark_until_cutoff and not config.validation_mode):
            raise ValueError('整年采集仅可在明确的验证模式中启用。')
        if type(config.fallback_interval_turns) is not int or config.fallback_interval_turns<0:
            raise ValueError('保底检查间隔应为非负整数；0表示只保留事件和关键检查。')
        for key in ('max_attempts', 'max_tp_items', 'max_carrots'):
            item = getattr(config, key)
            if key=='max_carrots' and item is None:continue
            if type(item) is not int or item < (1 if key == 'max_attempts' else 0):
                raise ValueError('尝试次数、药和萝卜上限必须是非负整数，尝试次数至少为1。')
        if config.refill_operation_limit is not None and (type(config.refill_operation_limit) is not int or config.refill_operation_limit<0):
            raise ValueError('补TP次数限制应为非负整数或留空。')
        for key, low, high in (('support_deck', 0, 10), ('borrow_card_id', 0, 999999),
                ('borrow_min_level', 1, 50), ('borrow_min_break', 0, 4)):
            item = getattr(config, key)
            if type(item) is not int or not low <= item <= high:
                raise ValueError('卡组、借卡ID、最低等级或突破范围不正确。')
        if config.support_deck or config.borrow_card_id:
            from core.auto_trainer.cut_support_selection import support_options
            support_options(config)
        return config

    def public_dict(self):
        result={'schema_version': 'uma.cut_farming_config.v1', **asdict(self)}
        # Disabled preview features must not make ordinary batches unreadable
        # after switching back to an older stable package.
        if self.continuation is None:result.pop('continuation',None)
        return result


@lru_cache(maxsize=1)
def _character_identities():
    """Exact local names/aliases, plus the user's confirmed CN name for 1076."""
    try:
        records=json.loads(CHARACTER_ROSTER.read_text(encoding='utf-8-sig')).get('records',[])
    except (OSError,ValueError,TypeError):
        records=[]
    cards,names={},{}
    for row in records:
        card=str(row.get('id',''))
        identifier=row.get('character_id')
        if identifier is None and re.fullmatch(r'\d{6}',card):identifier=int(card[:4])
        if type(identifier) is not int:continue
        cards[card]=identifier
        for name in [row.get('name'),*row.get('aliases',[])]:
            if isinstance(name,str) and name.strip():
                names.setdefault(re.sub(r'\s+','',name),set()).add(identifier)
    # 2026-10-20: the user supplied the same 107601 configuration and game
    # screenshots. This is an exact alias, never a fuzzy OCR substitution.
    for name in ('樱花劳蕾尔','樱花桂冠'):names.setdefault(name,set()).add(1076)
    return cards,names


def _configured_character_id(config):
    cards,names=_character_identities()
    identifier=cards.get(config.character_id)
    if identifier is not None:return identifier
    name=(config.validation_character_name or '').strip() if config.validation_mode else ''
    matches=names.get(re.sub(r'\s+','',name),set())
    return next(iter(matches)) if len(matches)==1 else None


def expected_character_name(config):
    if config.validation_mode:
        if _configured_character_id(config)==1076:return '樱花桂冠'
        return config.validation_character_name.strip()
    return {'101001':'大树快车','103102':'艾尼风神','106501':'大拓太阳神'}[config.character_id]


def character_identity_evidence(config, observed):
    expected=expected_character_name(config)
    expected_id=_configured_character_id(config)
    _,names=_character_identities()
    matches=names.get(re.sub(r'\s+','',observed),set()) if isinstance(observed,str) else set()
    observed_id=next(iter(matches)) if len(matches)==1 else None
    if expected_id is not None and observed_id is not None:
        status='matched' if expected_id==observed_id else 'mismatched'
        basis='canonical_character_id'
    elif isinstance(observed,str) and observed.strip() and re.sub(r'\s+','',observed)==re.sub(r'\s+','',expected):
        status,basis='matched','exact_name'
    else:
        status,basis='unknown','identity_not_resolved'
    return {'status':status,'basis':basis,'expected_character_id':expected_id,
        'observed_character_id':observed_id,'expected_game_name':expected,
        'configured_character_name':config.validation_character_name if config.validation_mode else expected,
        'observed_name':observed}


def character_label(config):
    return expected_character_name(config) if config.validation_mode else CHARACTERS[config.character_id]['name']


def load_event_references(config):
    """Reuse U3 evidence; matching a title schedules a probe, never grants cut."""
    if config.validation_mode:return {}
    data = json.loads(Path(config.event_cache).read_text(encoding='utf-8-sig'))
    targets = CHARACTERS[config.character_id]['events']
    found = {event_id: [] for event_id in targets}
    for event in data.get('events', []):
        if normalize_title(event.get('owner_name_ja',''))!=normalize_title(EVENT_OWNERS[config.character_id]):
            continue
        ids = event.get('cn_title_alignment', {}).get('candidate_story_ids', [])
        titles={normalize_title(t) for t in event.get('titles',{}).values()}
        linked={event_id for event_id,title in targets.items() if event_id in ids or normalize_title(title) in titles}
        for event_id in linked:
            found[event_id].append({'source_id': event.get('source_id'),
                'titles': event.get('titles', {}), 'options': event.get('options', []),
                'source_url': event.get('source_url'), 'cut_probability': None})
    if any(not rows for rows in found.values()):
        raise ValueError('U3事件缓存缺少本角色的目标事件。')
    return found


def at_cutoff(period):
    return period.get('year_index') == 1 and period.get('month') == 12 and period.get('half') == 'second'


class CutSession:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        from core.cut_portable_runtime import screenshot_output_allowed,portable_root
        if not config.retain_screenshots and not screenshot_output_allowed(self.root):
            raise ValueError('不保留截图模式的运行目录必须位于包内runs。' if portable_root()
                else '不保留截图模式的运行目录必须位于E盘。')
        self.path = self.root / 'cut_farming.json'
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if CutFarmingConfig.from_dict(self.data.get('config')).public_dict() != config.public_dict():
                raise ValueError('当前刷切记录与配置不一致。')
        else:
            self.data = {'schema_version': 'uma.cut_farming_session.v1', 'config': config.public_dict(),
                'status': 'ready', 'attempts': [], 'tp_items_reserved': 0, 'carrots_reserved': 0,
                'notification_attempted': False, 'pending_action': None}
            self.save()

    def save(self):
        atomic_json(self.path, self.data)

    def stop(self, status, **evidence):
        if self.data['status']=='found' and status!='found':
            return
        self.data.update(status=status, stop=evidence, stopped_at_unix_ms=int(time.time() * 1000))
        self.finish_attempt(evidence.get('reason',status))
        self.save()

    def finish_attempt(self,reason):
        if not self.data['attempts']:return
        attempt=self.data['attempts'][-1]
        if 'stopped_at_unix_ms' not in attempt:
            now=int(time.time()*1000)
            attempt.update(stopped_at_unix_ms=now,stop_reason=reason)
            start=attempt.get('started_at_unix_ms')
            if type(start) is int:attempt['elapsed_seconds']=max(0.,(now-start)/1000)
        self.save()

    def resume_attempt(self):
        if self.data['status'] not in {'route_failed','stopped','running','unknown'} or self.data.get('pending_action') or not self.data['attempts']:
            raise ValueError('不能续跑已发现切、已结束或存在未确认消费的刷切记录。')
        attempt=self.data['attempts'][-1]
        if attempt['status'] in {'abandoned','abandoned_and_restarted'}:raise ValueError('原育成已放弃，不能续跑原局。')
        root=Path(attempt['run_root']).resolve(strict=True)
        progress=json.loads((root/'progress.json').read_text(encoding='utf-8'))
        if root.name!=attempt['run_id'] or progress.get('run_session_id')!=root.name or progress.get('device')!=f'127.0.0.1:{self.config.device_port}':
            raise ValueError('续跑局号或设备不匹配。')
        # Start/continue reattaches to the current page. Old career intentions
        # are archived by live takeover, never replayed or treated as a clock.
        attempt.setdefault('resume_history',[]).append({'resumed_at_unix_ms':int(time.time()*1000),
            'previous_stop_reason':attempt.get('stop_reason'),'previous_elapsed_seconds':attempt.get('elapsed_seconds')})
        if self.data['status']=='unknown':
            # Keep the failed probe as evidence. Continue starts a new hook,
            # whose initial fresh status check must resolve it before farming.
            attempt['resume_history'][-1]['previous_unknown']=self.data.get('stop')
        for key in ('stopped_at_unix_ms','stop_reason','elapsed_seconds'):attempt.pop(key,None)
        attempt['status']='running';self.data['status']='running';self.data.pop('stop',None);self.data.pop('stopped_at_unix_ms',None)
        self.save()
        return root

    def begin_attempt(self, run_root):
        if self.data['status'] in TERMINAL_STATES or self.data.get('pending_action'):
            raise ValueError('本刷切任务已停止或存在未确认动作，不能自动重开。')
        if len(self.data['attempts']) >= self.config.max_attempts:
            self.stop('limit_reached', reason='attempt_limit')
            return False
        root = Path(run_root)
        if any(item['run_id'] == root.name for item in self.data['attempts']):
            raise ValueError('每轮刷切必须使用独立局号。')
        self.data['attempts'].append({'run_id': root.name, 'run_root': str(root), 'status': 'running', 'events_seen': [],
            'started_at_unix_ms':int(time.time()*1000)})
        self.data['status'] = 'running'
        self.save()
        return True

    def reserve_resource(self, kind, amount):
        if kind not in {'tp_items', 'carrots'} or type(amount) is not int or amount < 1:
            raise ValueError('刷切资源数量未读清。')
        key = kind + '_reserved'
        maximum = self.config.max_tp_items if kind == 'tp_items' else self.config.max_carrots
        if maximum is not None and self.data[key] + amount > maximum:
            raise ValueError('刷切资源消耗达到上限。')
        if (self.config.refill_operation_limit is not None
                and self.data.get('refill_operations_reserved',0)>=self.config.refill_operation_limit):
            raise ValueError('本次补TP次数达到限制。')
        self.data[key] += amount
        self.data['refill_operations_reserved']=self.data.get('refill_operations_reserved',0)+1
        self.save()

    def remaining_resources(self):
        if (self.config.refill_operation_limit is not None
                and self.data.get('refill_operations_reserved',0)>=self.config.refill_operation_limit):
            return {'tp_items':0,'carrots':0}
        return {'tp_items':self.config.max_tp_items-self.data['tp_items_reserved'],
            'carrots':float('inf') if self.config.max_carrots is None else self.config.max_carrots-self.data['carrots_reserved']}


def pushplus_notify(config, content, *, title='Uma 刷切：已确认才能出众', opener=None, token_override=None):
    """Compatibility entry point; shared settings and event switches are authoritative."""
    try:
        from desktop.notifications import send_event
        return send_event(event_id='cut_hit', title=title, content=content,
            legacy_token_file=config.pushplus_token_file,
            token_override=token_override, opener=opener)
    except Exception:
        # Notification failure never changes a retained hit or exposes credentials.
        return {'status': 'failed'}


class CutFarmingHook:
    def __init__(self, session, *, probe, notify=pushplus_notify, local_alert=None):
        self.session, self.probe, self.notify = session, probe, notify
        self.local_alert = local_alert or (lambda message: None)
        self.references = load_event_references(session.config)
        self.pending = True # First stable home checks pre-existing cut.
        self.pending_reasons={'initial'}
        self.last_event_key=None
        self.sampling_faults=set()
        self.probe_sequence = 0
        self.last_probe_turn = None
        self.last_probe_key = None
        self.last_seen_key=None
        self.turns_since_probe=0
        self.supporter_deadline_failure=None
        self.continuation_flow=False

    def observe_supporter_deadline(self, ctx, report):
        """Arm the existing restart checkpoint only after a known last-turn miss.

        This is an in-memory intention for this owner, not a saved intention
        to replay after a user stops and takes over the career again.
        """
        if self.continuation_flow:
            return False
        from core.auto_trainer.larc_supporter_goal import deadline_larc_supporter_goal
        if self.session.data['status'] in TERMINAL_STATES or self.session.data.get('pending_action'):
            return False
        attempt=self.session.data['attempts'][-1]
        if attempt.get('run_id')!=ctx.run_id or attempt.get('cut_found'):
            return False
        goal=(report.get('goal_deadline') or {}).get('supporter_goal')
        resolution=((report.get('stages') or {}).get('larc_ss') or {}).get('comparison',{}).get('deadline_resolution',{})
        if (report.get('is_valid') is not True or report.get('status')!='goal_deadline_unresolved'
                or report.get('turn_consumed') is not False or not deadline_larc_supporter_goal(goal)
                or resolution.get('status')!='unreachable'):
            return False
        source=goal.get('source') or {}
        if (source.get('run_session_id')!=ctx.run_id or type(source.get('turn_index')) is not int
                or source['turn_index']!=ctx.progress.get('next_turn')
                or not source.get('path') or not source.get('sha256')):
            return False
        count=resolution.get('ready_count');points=resolution.get('points_if_all_win')
        rates=resolution.get('win_rates') or []
        known_risk=(resolution.get('reason')=='no_safe_full_group' and count==5
            and len(rates)==5 and [row.get('slot') for row in rates]==list(range(5))
            and all(row.get('band') in {'blue','green'} for row in rates)
            and any(row.get('band')=='green' for row in rates))
        if not (resolution.get('reason')=='no_full_group' and type(count) is int and 0<=count<5
                or resolution.get('reason')=='full_ss_points_insufficient' and count==5
                and type(points) is int and 0<=points<goal['remaining_points'] or known_risk):
            return False
        self.supporter_deadline_failure={'supporter_goal':goal,'resolution':resolution}
        attempt['supporter_deadline_failure']=self.supporter_deadline_failure
        self.pending=True;self.pending_reasons.add('supporter_deadline')
        self.session.save()
        return True

    def sampling_fault(self, key):
        if self.session.data['status'] in TERMINAL_STATES:return
        key=str(key)
        if key not in self.sampling_faults:
            self.sampling_faults.add(key)
            self.pending=True;self.pending_reasons.add('capture_gap')

    def observe_event(self, report):
        if self.session.data['status'] in TERMINAL_STATES:return
        if not self.continuation_flow and self.session.config.restart_after_training_failure and self.session.data['attempts']:
            from core.auto_trainer.cut_training_failure import completed_training_failure
            attempt=self.session.data['attempts'][-1]
            failure=completed_training_failure(report,attempt['run_id'])
            if failure is not None and not attempt.get('training_failure_event'):
                attempt['training_failure_event']=failure
                self.session.save()
        if not self.continuation_flow and self.session.config.restart_after_character_cut_miss and self.session.data['attempts']:
            from core.auto_trainer.cut_character_event import completed_character_event
            event=completed_character_event(report,self.session.config.character_id)
            if event is not None:
                self.session.data['attempts'][-1]['character_cut_event_pending']=event
                self.pending=True;self.pending_reasons.add('character_cut_event')
                self.session.save()
        if self.session.data['attempts']:
            from core.auto_trainer.larc_mei_unlock_policy import confound_mei_gamble
            attempt=self.session.data['attempts'][-1]
            confound_mei_gamble(report,attempt['run_root'],attempt['run_id'])
        if report.get('error') and report.get('scope')=='title_only_not_reward_confirmation':
            self.sampling_fault(('event_ocr',report.get('source'),report['error']))
        texts=[report[key] for key in ('title','event_title') if isinstance(report.get(key),str)]
        for row in report.get('step_reports',[]):
            result=row.get('result',{});safety=result.get('frame_safety',{})
            if safety.get('text_status')=='ok':
                texts.append(safety.get('visible_text',''))
                texts.append(result.get('decision',{}).get('evidence',{}).get('screen_text',{}).get('normalized_text',''))
        text = normalize_title(' '.join(texts))
        targets = {} if self.session.config.validation_mode else CHARACTERS[self.session.config.character_id]['events']
        matched=None
        for event_id, title in targets.items():
            names={normalize_title(title)}
            for item in self.references[event_id]:names.update(normalize_title(t) for t in item.get('titles',{}).values() if isinstance(t,str) and t.strip())
            if any(name and name in text for name in names):matched=(event_id,title);break
        if matched is None and '才能出众' in text:matched=('cut_text','才能出众')
        if matched is None:
            if texts:self.last_event_key=None
            return
        key=(report.get('action_turn'),matched[0])
        if key==self.last_event_key:return
        self.last_event_key=key;self.pending=True;self.pending_reasons.add('event')
        self.session.data['attempts'][-1]['events_seen'].append({'event_id':matched[0],'title':matched[1],'cut_probability':None})
        self.session.save()

    def before_training(self, ctx, source, provider, output):
        if self.session.data['status'] in TERMINAL_STATES:
            return {'stop_reason': 'cut_farming_stopped', 'status': self.session.data['status']}
        workflow_root=getattr(ctx,'root',None)
        workflow_bound=workflow_root is not None and (Path(workflow_root)/'session/career_workflow_context.json').is_file()
        period = provider.get('larc_period', {}).get('value', {})
        continuation = getattr(ctx, 'continuation_runtime', None)
        if (continuation is not None and provider.get('larc_period', {}).get('status') == 'verified'
                and callable(getattr(continuation, 'sync_career_flow', None))):
            continuation.sync_career_flow(period)
        full_career = self.continuation_flow or bool(continuation and continuation.can_continue_junior())
        if period.get('year_index') not in (None,1) and continuation is None:
            self.session.stop('unknown',reason='cut_career_outside_first_year',run_id=ctx.run_id,period=period)
            return {'stop_reason':'cut_career_outside_first_year','state':'unknown'}
        attempt=self.session.data['attempts'][-1]
        deadline_failure=None if full_career else self.supporter_deadline_failure
        if deadline_failure is not None:
            target=deadline_failure['supporter_goal']
            if (attempt.get('cut_found') or target['source']['run_session_id']!=ctx.run_id
                    or target['source']['turn_index']!=getattr(ctx,'progress',{}).get('next_turn')
                    or provider.get('state_context',{}).get('remaining_turns')!=1):
                self.supporter_deadline_failure=None
                deadline_failure=None
        failure=attempt.get('training_failure_event')
        workflow_training_failure=False
        if (not full_career and self.session.config.restart_after_training_failure and isinstance(failure,dict)
                and failure.get('run_id')==ctx.run_id and not attempt.get('cut_found')):
            # The event choice has completed and the owner has returned to a
            # normal home/race home. Use the existing abandon/restart owner;
            # no extra cut inspection and no next training action are needed.
            if workflow_bound:
                workflow_training_failure=True
                self.pending=True;self.pending_reasons.add('continuation_restart')
            else:
                attempt['status']='training_failed'
                self.session.finish_attempt('cut_training_failed')
                self.session.save()
                return {'stop_reason':'cut_training_failed','training_failure_event':failure}
        cutoff = at_cutoff(period) and not (continuation and continuation.can_continue_junior())
        character_event=(self.session.data['attempts'][-1].get('character_cut_event_pending')
                         if not full_career and self.session.config.restart_after_character_cut_miss else None)
        if character_event:
            self.pending=True;self.pending_reasons.add('character_cut_event')
        from core.auto_trainer.larc_mei_unlock_policy import failed_mei_gamble,observe_mei_gamble_menu
        menu_check=None if full_career else observe_mei_gamble_menu(ctx,source,provider)
        if menu_check is not None:
            self.session.data['attempts'][-1]['mei_unlock_result_check']=menu_check
            self.session.save()
        mei_failure=None if full_career else failed_mei_gamble(ctx)
        if mei_failure is not None and not self.session.data['attempts'][-1].get('cut_found'):
            self.pending=True;self.pending_reasons.add('mei_unlock_failure')
        turn=getattr(ctx,'progress',{}).get('next_turn',provider.get('state_context',{}).get('turn'))
        verified_period=provider.get('larc_period',{}).get('status')=='verified'
        date_key=tuple(period.get(key) for key in ('year_index','month','half','pre_debut')) if verified_period else None
        probe_key=(turn,date_key)
        new_turn=(turn is not None or verified_period) and probe_key!=self.last_seen_key
        if new_turn:self.last_seen_key=probe_key;self.turns_since_probe+=1
        interval=self.session.config.fallback_interval_turns
        due=(self.session.config.check_mode=='every_turn' and probe_key!=self.last_probe_key
            or self.session.config.check_mode=='event_triggered' and interval>0 and self.turns_since_probe>=interval)
        if full_career:
            # No inherited initial/interval/deadline/failed-training screening.
            # A newly observed cut or an explicit bounded gate restart still
            # uses the original probe, so confirmed hits cannot be discarded.
            self.pending_reasons.intersection_update({'event', 'continuation_restart'})
            self.pending=bool(self.pending_reasons)
            cutoff=False;due=False
        if not self.pending and not cutoff and not due:
            return {'status': 'not_due'}
        check_reason=('cutoff' if cutoff else 'initial' if 'initial' in self.pending_reasons
            else 'supporter_deadline' if 'supporter_deadline' in self.pending_reasons
            else 'character_cut_event' if 'character_cut_event' in self.pending_reasons
            else 'mei_unlock_failure' if 'mei_unlock_failure' in self.pending_reasons
            else 'capture_gap' if 'capture_gap' in self.pending_reasons else 'event' if 'event' in self.pending_reasons
            else 'every_turn' if self.session.config.check_mode=='every_turn' else 'interval')
        self.probe_sequence += 1
        ctx.cut_expected_character_name=expected_character_name(self.session.config)
        result = {**self.probe(ctx, Path(output) / f'probe_{self.probe_sequence:03d}'),'check_reason':check_reason}
        self.pending = False
        self.pending_reasons.clear();self.last_event_key=None;self.turns_since_probe=0
        self.last_probe_turn = turn
        self.last_probe_key = probe_key
        attempt = self.session.data['attempts'][-1]
        attempt['last_probe'] = result
        state = result.get('state', 'unknown')
        expected=expected_character_name(self.session.config)
        identity=character_identity_evidence(self.session.config,result.get('trainee_name'))
        result['character_identity']=identity
        if identity['status']=='mismatched':
            diagnostic={'expected':expected,'observed':result.get('trainee_name'),
                'configured_character_id':self.session.config.character_id,
                'expected_character_id':identity['expected_character_id'],
                'observed_character_id':identity['observed_character_id']}
            result['character_mismatch']=diagnostic
            self.session.stop('unknown',reason='cut_character_mismatch',run_id=ctx.run_id,**diagnostic,
                message=f"游戏识别角色为{result.get('trainee_name')}，刷切配置角色为{expected}；请核对角色与运行模式。")
            return {'stop_reason':'cut_character_mismatch',**result}
        if workflow_bound:
            from scripts.career_workflow_runtime import cut_observed
            screening_owned = cut_observed(ctx,result,provider,cutoff=cutoff)
            if screening_owned == 'restart':
                return {'stop_reason': 'continuation_gate_failed', **result}
            if screening_owned and state != 'present' and result.get('returned') is True:
                self.session.save()
                return {'status': 'probed', 'source': result['source'], **result}
        if workflow_training_failure and state=='absent' and result.get('returned'):
            attempt['status']='training_failed'
            self.session.finish_attempt('cut_training_failed')
            self.session.save()
            return {'stop_reason':'cut_training_failed','training_failure_event':failure,**result}
        if mei_failure is not None and state=='absent' and result.get('returned') and not attempt.get('cut_found'):
            attempt['status']='mei_unlock_failed'
            attempt['mei_unlock_failure']=mei_failure
            self.session.finish_attempt('cut_mei_unlock_failed')
            self.session.save()
            return {'stop_reason':'cut_mei_unlock_failed','mei_unlock_failure':mei_failure,**result}
        if deadline_failure is not None and state=='absent' and result.get('returned'):
            attempt['status']='supporter_deadline_missed'
            self.session.finish_attempt('cut_supporter_deadline_missed')
            self.session.save()
            return {'stop_reason':'cut_supporter_deadline_missed',
                'supporter_deadline_failure':deadline_failure,**result}
        if self.session.config.benchmark_until_cutoff and state in {'present','absent'} and result.get('returned'):
            if state=='present':
                attempt['cut_found']=True
                attempt.setdefault('found_at_turn',turn)
                attempt.setdefault('found_probe',result)
            if cutoff:
                from core.auto_trainer.cut_benchmark import save_endpoint
                attempt['benchmark_endpoint']=save_endpoint(ctx,source,provider,result)
                attempt['status']='benchmark_cutoff'
                attempt['cutoff_at_unix_ms']=int(time.time()*1000)
                self.session.finish_attempt('cut_benchmark_cutoff')
                return {'stop_reason':'cut_benchmark_cutoff',**result}
            self.session.save()
            return {'status':'probed','source':result['source'],**result}
        if state == 'present':
            if self.session.config.discard_found_for_validation:
                attempt['status']='found_for_validation'
                attempt['cut_found']=True
                attempt['found_probe']=result
                self.session.save()
                if not attempt.get('notification_attempted'):
                    attempt['notification_attempted']=True
                    self.session.data['notification_attempted']=True
                    self.session.save()
                    message=f"{character_label(self.session.config)} 已确认才能出众，证据已保存。按耐久测试设置，通知后将弃养并继续测试。局号：{ctx.run_id}"
                    try:attempt['notification']=self.notify(self.session.config,message)
                    except Exception:attempt['notification']={'status':'failed'}
                    self.session.data['notification']=attempt['notification']
                    self.session.save()
                if attempt.get('notification',{}).get('status')=='accepted':
                    if not attempt.get('validation_hit_recorded'):
                        self.session.data.setdefault('validation_hits',[]).append({'run_id':ctx.run_id,
                            'source':result.get('source'),'notification':attempt['notification']})
                        attempt['validation_hit_recorded']=True
                        self.session.save()
                    return {'stop_reason':'cut_found_for_validation',**result}
                # A failed notification never silently discards a confirmed hit.
                attempt['validation_discard_blocked']='notification_not_accepted'
            attempt['status'] = 'found'
            self.session.stop('found', run_id=ctx.run_id, probe=result)
            from core.auto_trainer.runtime_control import current
            if current() is not None and not getattr(ctx,'workflow_continue_found',False):
                current().request('stop')
            # Persist success and a one-shot notification marker before any I/O.
            if not self.session.data['notification_attempted']:
                self.session.data['notification_attempted'] = True
                self.session.save()
                disposition=('将沿用当前局自动育成' if getattr(ctx,'workflow_continue_found',False)
                    else '已停止输入并保留当前育成')
                message = f"{character_label(self.session.config)} 已确认才能出众，{disposition}。局号：{ctx.run_id}"
                try:
                    self.session.data['notification'] = self.notify(self.session.config, message)
                except Exception:
                    self.session.data['notification'] = {'status': 'failed'}
                self.session.save()
                try:
                    self.local_alert(message)
                except Exception:
                    self.session.data['local_notification']={'status':'failed'}
                    self.session.save()
            return {'stop_reason': 'cut_found', **result}
        if state != 'absent' or not result.get('returned'):
            attempt['status'] = 'unknown'
            self.session.stop('unknown', run_id=ctx.run_id, probe=result)
            return {'stop_reason': 'cut_status_unknown', **result}
        if character_event:
            attempt['status']='no_cut_after_character_event'
            attempt['character_cut_event_result']={'event':character_event,'probe':result}
            self.session.finish_attempt('cut_character_event_absent')
            self.session.save()
            return {'stop_reason':'cut_character_event_absent',**result}
        self.session.save()
        if cutoff:
            if continuation is not None:
                return {'status': 'continuation_cutoff_ready', **result}
            attempt['status'] = 'no_cut_at_cutoff'
            attempt['cutoff_at_unix_ms']=int(time.time()*1000)
            self.session.finish_attempt('cut_cutoff_absent')
            self.session.save()
            return {'stop_reason': 'cut_cutoff_absent', **result}
        return {'status': 'probed', 'source': result['source'], **result}

    def before_race(self,ctx,source,period,output):
        if self.session.data['status'] in TERMINAL_STATES:
            return {'stop_reason':'cut_farming_stopped','status':self.session.data['status']}
        if self.continuation_flow and not self.pending_reasons.intersection({'event', 'continuation_restart'}):
            return {'status': 'not_due'}
        if period.get('status')!='verified':
            self.session.stop('unknown',reason='cut_race_date_unknown',run_id=ctx.run_id,period=period)
            return {'stop_reason':'cut_race_date_unknown','state':'unknown'}
        return self.before_training(ctx,source,{'larc_period':period},output)
