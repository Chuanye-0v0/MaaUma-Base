"""Current SS rows are independent of their historical roster associations."""


def visible_ss_participants(preview):
    observation = preview.get("participants_observation") or {}
    rows = observation.get("participants") or []
    count = preview.get("ready_count")
    if (preview.get("status") != "accepted" or preview.get("stage") != "preview"
            or preview.get("kind") not in {"SS", "SSS"}
            or type(count) is not int or not 1 <= count <= 5):
        return []
    current = observation.get("current_frame")
    if current is not None:
        if (len(rows) != count or current.get("status") != "accepted" or current.get("expected_count") != count
                or current.get("visible_portrait_count") != count):
            return []
    elif observation.get("status") != "accepted":
        # Old abstained records lack the independent visual verdict. Do not
        # retrospectively promote them by guessing why association failed.
        return []
    # Older accepted packets can expose only a subset of optional row fields.
    # Retain their known bond/reward facts; consumers needing a complete group
    # (recovery totals, phase ordering) still require len(rows) == ready_count.
    return rows


def ss_victory_evidence(preview):
    count=preview.get('ready_count')
    valid=(preview.get('status')=='accepted' and preview.get('stage')=='preview'
           and type(count) is int and 1<=count<=5)
    rows=visible_ss_participants(preview)
    known=sum(row.get('win_guaranteed') is True for row in rows)
    observation=preview.get('participants_observation') or {}
    if 'win_rates' in observation:
        rates=observation['win_rates'] or []
        complete=(observation.get('win_rates_status')=='accepted' and len(rates)==count
                  and [r.get('slot') for r in rates]==list(range(count or 0)))
        known=sum(r.get('band')=='blue' and r.get('guaranteed') is True for r in rates)
    else:
        complete=len(rows)==count
    # User-confirmed scenario rule: SSS wins independently of current energy.
    # Ordinary SS's "all wins" numeric panel is only a conditional reward.
    sss=valid and preview.get('kind')=='SSS'
    certain=bool(sss or valid and preview.get('kind')=='SS' and complete and known==count)
    return {'guaranteed':certain,'guaranteed_participants':count if sss else known,
        'expected_count':count,'basis':'sss_scenario_rule' if sss else
        'all_visible_win_badges_guaranteed' if certain else 'ordinary_ss_win_not_guaranteed'}
