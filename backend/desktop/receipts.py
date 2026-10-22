"""Compact user-facing receipts; detailed evidence remains in the run logs."""
from datetime import datetime
TASK_NAMES = {'cleanup': '账号收尾：限时特卖', 'batch': '多账号日活', 'harvest': '收菜', 'jjc': 'JJC', 'jjc_batch': '批量 JJC', 'daily_legend': '日常传奇赛事', 'limited_legend': '限时传奇赛', 'loh': 'LOH英雄联赛', 'champions_registration': '大赛报名', 'champions_race': '大赛', 'story_event': '活动领奖／转盘', 'daily': '每日组合', 'daily_activities': '当前账号日活', 'activities': '日常赛事', 'daily_rewards': '每日任务领奖／礼物箱', 'plant': '开种', 'seed_cycle': '连续刷种马', 'skills': '学习技能', 'account_login': '账号登录'}
ACTION_NAMES = {'dismiss_rank': '继续名次展示，进入比赛结算', 'choose_opponent': '选择指定传奇对手', 'scroll_opponents': '按编号定位传奇对手', 'back_to_opponents': '返回传奇对手名单', 'open_details': '核对赛事与对手', 'open_selection': '选择参赛马娘', 'confirm_selected': '确认参赛马娘', 'continue_lineup': '确认出场名单', 'enter_race': '使用免费次数参赛（不使用道具）', 'view_result': '查看比赛结果', 'continue_victory': '确认比赛成绩', 'continue_rewards': '领取比赛奖励', 'open_training': '打开育成', 'forward_harvest': '进入收菜结算', 'history_next': '查看育成战绩', 'history_scroll': '查看战绩奖励', 'confirm_history': '确认战绩奖励', 'open_skills': '打开技能学习', 'continue_skill_tutorial': '关闭技能教程', 'complete_training': '确认结束育成', 'confirm_training_rating': '确认育成评价', 'confirm_factors': '查看因子', 'confirm_factor_choice': '保留原因子', 'close_trained_detail': '关闭育成详情', 'next_training_rewards': '领取育成奖励', 'next_training_summary': '确认育成总结', 'next_affinity_result': '确认亲密度', 'continue_score_record': '确认评分记录', 'continue_scenario_record': '确认剧本记录', 'confirm_title_notice': '确认称号', 'cancel_follow_trainer': '关闭关注提示', 'open_completed_formation': '打开 JJC 编成', 'auto_team_formation': '自动编成', 'confirm_auto_team_formation': '确认自动编成', 'save_team_formation': '保存编成', 'confirm_save_team_formation': '确认保存编成', 'formation_home': '返回主页', 'close_completed_training': '结算完成，返回主页', 'close_character_story_unlock': '关闭剧情解锁提示', 'claim_missions': '领取每日任务奖励', 'claim_gifts': '领取礼物', 'close_claim': '关闭奖励详情', 'open_missions': '打开任务奖励', 'open_gifts': '打开礼物箱', 'enter_multi': '日常赛事多次参赛', 'close_multi_rewards': '确认多次参赛奖励', 'exchange_shop': '金币商店兑换'}

def receipt_text(event, account=''):
    kind = event.get('type')
    message = event.get('message')
    if not message:
        action = event.get('action') or event.get('action_id')
        login_actions = {'switch_other': '切换登录账号', 'agree': '确认登录协议', 'open_password': '打开账密登录', 'fill_username': '填写登录账号', 'fill_password': '填写密码', 'login_password': '提交账密登录', 'open_records': '打开历史登录记录', 'select_record': '选择登录记录', 'login_saved': '使用登录记录', 'scroll_records': '查找登录记录', 'scroll_records_up': '向前查找登录记录'}
        if not action:
            return ''
        message = login_actions.get(action, ACTION_NAMES.get(action, '执行步骤：' + str(action)))
    if kind == 'started':
        text = '开始：' + TASK_NAMES.get(str(message), str(message))
    elif kind == 'finished' and isinstance(message, dict):
        if message.get('dry_run'):
            text = '预演完成，未操作游戏'
        elif message.get('reason') in {'task_options_invalid', 'task_assets_invalid'}:
            text = message.get('configuration_message') or '本项任务配置无效，请检查设置'
        elif message.get('reason') == 'harvest_empty_home_verified':
            text = '没有可收取的育成，已回主页并跳过收菜'
        elif message.get('kind') == 'batch':
            text = f"多账号日活：完成 {message.get('completed', 0)}/{message.get('total', 0)}，" + ('全部完成' if message.get('ok') else '未完成项已保留，可继续批次')
        elif message.get('reason') == 'single_plant_complete':
            text = '开种完成／已有育成已保留；未等待收菜，后续项目可继续'
        elif message.get('kind') == 'seed_cycle':
            text = f"连续种马：已完成 {message.get('completed_cycles', 0)} 轮；" + ('目标因子全部达标，已停止' if message.get('ok') and message.get('target_met') else '达到最大轮数，已停止（未达标）' if message.get('ok') else '已停止并保留进度：' + str(message.get('reason_text') or message.get('reason', '结果未确认')))
        elif message.get('reason') == 'harvest_home_requested':
            text = '收菜后已回主页；其余日活未执行'
        elif message.get('needs_configuration'):
            text = 'LOH未编队，待配置；继续其他日活'
        elif message.get('ok') is False:
            if message.get('retry_allowed'):
                text = '本次未完成，尚未提交参赛；可以直接点击开始重试'
            elif message.get('reason') == 'mfa_active_attempt_requires_review':
                text = '另一个任务尚未结束或未留下结束记录；已结束的失败任务不会占用入口'
            else:
                text = '未完成：' + str(message.get('reason', '结果未确认'))
        else:
            text = '本次任务完成'
    elif kind == 'error':
        text = '受阻：' + str(message)
    elif kind == 'stopped':
        text = '已停止'
    elif isinstance(message, str):
        action = event.get('action_id') or message.removeprefix('收菜: ')
        text = ACTION_NAMES.get(action, message)
        if event.get('receipt_path'):
            text = '已执行：' + text
        elif action in ACTION_NAMES:
            text = '正在执行：' + text
    else:
        text = str(message.get('reason', '已收到执行结果')) if isinstance(message, dict) else str(message)
    account = str(event.get('account_id') or account or '')
    prefix = '账号 ' + account + ' · ' if account and account != '-' else ''
    if kind in {'finished', 'stopped', 'error'} and event.get('elapsed_seconds') is not None:
        text += f"（用时 {event['elapsed_seconds']:.1f} 秒）"
    return datetime.now().strftime('%H:%M:%S') + '  ' + prefix + text
