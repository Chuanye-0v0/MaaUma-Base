"""User-configured acupuncture choices using the current event's date and energy."""
from copy import deepcopy
import math
import re

DEFAULT={'mode':'decline','fixed_choice':5,'rules':[]}
DATE_VALUES={-1,*range(12,72)}  # Pre-debut, then junior July through senior December.


def normalize_policy(value=None):
    value={} if value is None else value
    if not isinstance(value,dict) or set(value)-set(DEFAULT):raise ValueError('针灸设置格式不正确。')
    result={**deepcopy(DEFAULT),**deepcopy(value)}
    if result['mode'] not in ('decline','fixed','rules'):raise ValueError('针灸请选择默认拒绝、固定选项或按条件选择。')
    if type(result['fixed_choice']) is not int or not 1<=result['fixed_choice']<=5:raise ValueError('针灸选项应为1到5。')
    if not isinstance(result['rules'],list) or len(result['rules'])>20:raise ValueError('针灸条件最多20条。')
    for index,row in enumerate(result['rules'],1):
        if not isinstance(row,dict) or set(row)!={'start','end','vitality_min','vitality_max','choice'}:
            raise ValueError(f'针灸第{index}条条件格式不正确。')
        if any(type(row[key]) is not int or row[key] not in DATE_VALUES for key in ('start','end')) or row['start']>row['end']:
            raise ValueError(f'针灸第{index}条：起始时间不能晚于结束时间。')
        if any(type(row[key]) not in (int,float) or not math.isfinite(row[key]) or not 0<=row[key]<=100 for key in ('vitality_min','vitality_max')) or row['vitality_min']>row['vitality_max']:
            raise ValueError(f'针灸第{index}条：体力范围应为0～100%，下限不能高于上限。')
        if type(row['choice']) is not int or not 1<=row['choice']<=5:raise ValueError(f'针灸第{index}条选项应为1到5。')
    if result['mode']=='rules' and not result['rules']:raise ValueError('请至少添加一条针灸条件，或改用默认拒绝。')
    return result


def configured_policy(preferences):
    if 'acupuncture_policy' in preferences:return normalize_policy(preferences['acupuncture_policy'])
    old=preferences.get('acupuncture_choice',5)
    return normalize_policy({'mode':'fixed' if type(old) is int and 1<=old<=4 else 'decline',
        'fixed_choice':old if type(old) is int and 1<=old<=5 else 5})


def date_label(day):
    if day==-1:return '出道前'
    if day not in DATE_VALUES:return '未读清'
    return f"{('初级','经典级','高级')[day//24]} {day%24//2+1}月{'下' if day%2 else '上'}半月"


def parse_date(text):
    text=re.sub(r'\s+','',str(text))
    if text=='初级出道前':return -1
    match=re.fullmatch(r'(初级|经典级?|高级)([1-9]|1[0-2])月(上|下)(?:半月)?',text)
    if not match:return None
    year={'初级':0,'经典':1,'经典级':1,'高级':2}[match[1]]
    value=year*24+(int(match[2])-1)*2+(match[3]=='下')
    return value if value in DATE_VALUES else None


def observe_date(image,text):
    # Only the current screen header is used. A last-menu date can be one
    # half-month behind an after-training event and must not satisfy a boundary.
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    boundary=next((i for i,line in enumerate(lines) if '针灸师' in line and '登' in line),len(lines))
    header=lines[:boundary]
    candidates=[*header,*[a+b for a,b in zip(header,header[1:]) if a in {'初级','经典','经典级','高级'}]]
    values={day for line in candidates if (day:=parse_date(line)) is not None}
    if len(values)==1:return {'day':values.pop(),'basis':'current_event_header_text'}
    if len(values)>1:return {'day':None,'basis':'conflicting_current_header_dates'}
    if image is not None:
        import cv2
        from core.auto_trainer.career_goal_perception import _GOAL_REGION_READER
        canonical=cv2.resize(image,(720,1280))
        try:
            line,confidence=_GOAL_REGION_READER.read_line(canonical[36:72,155:345])
            day=parse_date(line)
            if type(confidence) in (int,float) and confidence>=.95 and day is not None:
                return {'day':day,'basis':'current_event_header_region'}
        except Exception:
            pass
    return {'day':None,'basis':'current_event_date_unreadable'}


def decide(policy,*,day=None,vitality=None,date_basis=None,vitality_basis=None):
    policy=normalize_policy(policy)
    result={'mode':policy['mode'],'matched_rule':None,'choice_index':5,'reason':'acupuncture_default_decline',
        'day':day,'date_label':date_label(day),'date_basis':date_basis,'vitality':vitality,
        'vitality_basis':vitality_basis,'bounds':'inclusive; first matching rule wins'}
    if policy['mode']=='fixed':
        return {**result,'choice_index':policy['fixed_choice'],'reason':'acupuncture_fixed_choice'}
    if policy['mode']=='decline':return result
    if day not in DATE_VALUES or type(vitality) not in (int,float) or not math.isfinite(vitality) or not 0<=vitality<=100:
        return {**result,'reason':'acupuncture_conditions_unreadable_decline'}
    for index,row in enumerate(policy['rules'],1):
        if row['start']<=day<=row['end'] and row['vitality_min']<=vitality<=row['vitality_max']:
            return {**result,'matched_rule':index,'choice_index':row['choice'],'reason':'acupuncture_condition_matched','rule':deepcopy(row)}
    return {**result,'reason':'acupuncture_no_condition_matched_decline'}


def help_text():
    return '''庸医扎针设置（测试版）

入口：事件设置 → 庸医扎针设置…
当前默认：第5项“不太放心，还是算了吧”，拒绝针灸。

三种模式
1. 默认拒绝：每次都选第5项。
2. 固定选项：每次选择指定项，不检查日期和体力。
3. 按条件选择：逐条比较，执行第一条同时符合日期与体力范围的规则。未命中或条件未读清时拒绝。

选项对应
1：成为强大的优俊少女
2：赢下比赛
3：健康有活力
4：提升魅力
5：拒绝
具体成功或失败由游戏决定；这里没有把随机效果当作保证收益。

日期与体力
日期细到上、下半月。出道前作为一个独立阶段，可设“出道前至初级12月下半月”。
时间和体力使用当前事件画面，不用上一回合状态代替。体力用0～100%的体力条比例，不是体力点数。
日期起止、体力上下限都包含边界。

例子
第一条：出道前至初级12月下半月，体力0～50%，选第3项。
第二条：出道前至初级12月下半月，体力0～100%，选第4项。
初级体力40% → 第一条先命中，选3。
初级体力50% → 包含上限，仍选3。
初级体力70% → 第一条不命中，第二条命中，选4。
经典级体力40% → 两条时间都不符，拒绝。
若把第二条上移，则初级体力40%也会先命中第二条，选4。

保存与后续确认
窗口里的“保存针灸设置”会直接保存针灸规则，对后续针灸事件生效，不操作当前游戏。
此窗口只保存针灸；事件页里的目标技能等未保存修改，仍需点“保存事件设置”。
取消不会写入修改。
初次五项文字未读齐仍拒绝；后续确认沿用之前选项，不重新按规则选针。
拒绝确认页点“我拒绝”；针灸确认页只有之前确实选过针法时才继续。
确认页身份读不清时保留现场停止。针灸规则不另行增加失败后自动重开。
复盘记录会保留命中的规则、当时日期与体力，方便核对。
'''
