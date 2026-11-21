"""Readable local HTML for the advisory assessment; no scripts or remote assets."""
from html import escape
from core.auto_trainer.larc_career_assessment import STATS,NAMES


def _h(value):
    return escape(str(value),quote=True)


def _n(value):
    return '—' if value is None else f'{value:g}' if isinstance(value,(int,float)) else _h(value)


def _list(values):
    return '<ul>'+''.join('<li>'+_h(v)+'</li>' for v in values)+'</ul>'


def _point(point):
    label=_h(point['label'])
    if point.get('status')!='assessed':
        return f'<article class="point pending"><h3>{label}</h3><p>{_h(point["verdict_label"])}</p></article>'
    status=point['verdict'];forecast=point['forecast'];resources=point['resources'];startup=point['startup']
    stats=point['current_stats'];rows=[]
    for key in STATS:
        values=forecast['attributes'].get(key) or {}
        rows.append('<tr><th>'+NAMES[key]+'</th>'+''.join('<td>'+_n(v)+'</td>' for v in
            (stats[key],point['reference_targets'][key],values.get('low'),values.get('estimate'),values.get('high')))+'</tr>')
    mei=resources['mei_outing'];mei_text=('未记录可用段数' if mei['status']=='unknown' else '已用完' if mei['status']=='used_all'
        else f"已观察到第 {mei['stage']} 段可用")
    bond_text=('卡组资料不足' if not startup['profile_complete'] else
        f"已见 {startup['recently_observed_cards']}/{startup['training_cards']} 张，{startup['ready_cards']} 张已到橙条或满羁绊")
    source_note='训练页记录' if point['snapshot_source']=='training_observation' else '主页记录，缺少当时训练页推演'
    return f'''<article class="point {status}">
<div class="point-top"><div><h3>{label}</h3><p class="muted">{_h(point['as_of_label'])} · 行动序号 {_n(point['as_of_turn'])} · {_h(source_note)}</p></div>
<span class="badge">{_h(point['verdict_label'])}</span></div>
{_list(point['reasons'])}
<p class="next">保留后的下一观察点：<strong>{_h(point['next_checkpoint'])}</strong></p>
<div class="metrics"><div><small>持有技能 Pt</small><b>{_n(stats['skill_pt'])}</b></div>
<div><small>当前体力</small><b>{_n(point['vitality'])}%</b></div>
<div><small>已执行 SS / SSS</small><b>{resources['completed_ss_actions']} 次</b></div>
<div><small>当前适应性 Pt</small><b>{_n(resources['growth']['adaptation_points'])}</b></div></div>
<div class="table-scroll"><table><thead><tr><th>属性</th><th>当前</th><th>配置目标</th><th>保守推演</th><th>常规推演</th><th>理想推演</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">推演来自该时点的原策略，并非新的整局模拟或成品保证。技能 Pt 是当前余额，SS 次数不代表获胜人数。</p>
<div class="facts"><p>主力羁绊：{_h(bond_text)}</p><p>黄帽外出：{_h(mei_text)}</p>
<p>才能出众：{'此时已有确认记录' if resources['cut_confirmed'] else '此时未见确认记录，不据此弃养'}</p></div>
<details><summary>资料缺口与需要人工核对的部分</summary>{_list(point['unknowns'])}</details>
</article>'''


def render_assessments(items, *, export_trace_id=None):
    """items: (human-readable label, assessment) pairs. All content is escaped."""
    navigation=''.join(f'<a href="#run-{i}">{_h(label)}</a>' for i,(label,_) in enumerate(items,1))
    sections=[]
    for i,(label,value) in enumerate(items,1):
        points=''.join(_point(p) for p in value['checkpoints'])
        thresholds=value['thresholds']
        later=value.get('later_observed_for_comparison');comparison=''
        if later:
            numbers='　'.join(NAMES[k]+' '+_n(later['stats'].get(k)) for k in STATS)
            comparison=f'<div class="notice"><strong>复核用：记录末尾实际观察到的数值</strong><p>{_h(later["date"])} · {numbers}</p><p class="muted">这不是最终成品结算，只供比较早期推演是否偏差；没有用于回填前面的建议。</p></div>'
        sections.append(f'''<section id="run-{i}"><h2>{_h(label)}</h2>
<p class="muted">局号：{_h(value.get('run_id') or '未记录')}<br>{_h(value['target_basis'])}</p>{points}{comparison}
<details class="rules"><summary>初版判断规则与限制</summary>
<p>常规推演的平均目标完成比例达到 {_n(thresholds['continue_mean_target_ratio']*100)}%，且各项不低于 {_n(thresholds['continue_min_target_ratio']*100)}%，倾向续养。
前期羁绊资料齐全、至少 {_n(thresholds['startup_ready_share']*100)}% 主力已启动且常规推演达到 {_n(thresholds['startup_mean_target_ratio']*100)}%，也可建议续养。</p>
<p>止损建议只会在后两个节点出现：卡组资料完整，连续两个已记录节点有至少两项相同属性，理想推演仍不足当前参考目标的 {_n(thresholds['late_shortfall_high_ratio']*100)}%。已确认出切时优先保留。</p>
<p><strong>这些是可校准的初版参考门槛，尚未用足够成品验证。</strong>缺失信息不扣分，早期边缘局倾向继续观察。</p>
{_list(value['limitations'])}</details></section>''')
    attribution=(f'导出来源编号：{_h(export_trace_id)}。仅标记本次导出程序来源，不代表历史育成的实际运行者。<br>'
                 if export_trace_id else '')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>好局续养 · 评估预览</title><style>
:root{color-scheme:light;--ink:#213349;--muted:#66768a;--line:#dfe7ed}*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:#f3f6f9;color:var(--ink);font:16px/1.7 "Microsoft YaHei",system-ui,sans-serif}
main{max-width:1080px;margin:auto;padding:36px 24px 70px}header{padding:12px 0 20px}h1{font-size:30px;margin:0}h2{font-size:23px;margin:30px 0 8px}h3{font-size:19px;margin:0}p{margin:8px 0}.muted{color:var(--muted);font-size:13px}
.notice{background:#e7f0fb;border-left:4px solid #5287c1;border-radius:7px;padding:13px 18px;margin:16px 0}
nav{display:flex;gap:10px;flex-wrap:wrap}nav a{border:1px solid #cddde9;border-radius:20px;background:white;padding:6px 16px;color:#315b82;text-decoration:none}
.point{background:white;border:1px solid var(--line);border-left:5px solid #d5ac45;border-radius:12px;padding:23px;margin:16px 0;box-shadow:0 2px 8px #21334908}.point.continue{border-left-color:#2e9a73}.point.consider_stop{border-left-color:#c77756}.point.pending{border-left-color:#b9c5d1;background:#f9fbfc;color:var(--muted);padding:16px 23px}
.point-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.badge{white-space:nowrap;font-weight:600;background:#fff3d3;color:#86621b;padding:5px 12px;border-radius:18px;font-size:14px}.continue .badge{background:#ddf5e9;color:#206c4e}.consider_stop .badge{background:#fff0e6;color:#945331}
ul{padding-left:22px;margin:12px 0}.next{padding-top:10px;border-top:1px solid var(--line)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:20px 0}.metrics div{background:#f5f8fb;padding:10px 14px;border-radius:8px}.metrics small{display:block;color:var(--muted);font-size:12px}.metrics b{font-size:19px;font-weight:600}
.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px;text-align:right;white-space:nowrap}th,td{padding:9px 13px;border-bottom:1px solid var(--line)}th:first-child{text-align:left}thead{background:#f6f8fb;color:#576b80}.facts{font-size:14px;margin:15px 0}details{font-size:14px}summary{cursor:pointer;color:#426181}.rules{background:#eaf0f5;border-radius:10px;padding:15px 20px;margin:20px 0}
footer{color:var(--muted);font-size:13px;margin-top:35px}@media(max-width:640px){main{padding:22px 12px}.point{padding:16px}.point-top{display:block}.badge{display:inline-block;margin:8px 0}.metrics{grid-template-columns:repeat(2,1fr)}}
</style><main><header><h1>好局续养 · 评估预览</h1><p>先看每个阶段为什么值得留，再决定是否继续养。</p>
<div class="notice"><strong>第一版，仅供复盘与校准。</strong>这份报告不会操作游戏，也不会改变当前刷切的弃养规则。后两年策略与判断门槛仍需打磨。</div>
<nav>'''+navigation+'</nav></header>'+''.join(sections)+'<footer>'+attribution+'''全部内容来自本地文字复盘。没有重新截图、连接设备或调用在线决策。</footer></main></html>'''
