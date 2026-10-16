"""Follow an enabled server admission mode without reconciling debit numbers."""

def admission_resource(observed, book, *, max_rounds=4, allow_final_jewels=False, allow_jewels=False):
    quote = observed.get('entry_resource') or {}
    kind, cost = (quote.get('kind'), quote.get('cost'))
    used = book.get('resources_used', {})
    from core.champions_race.task_restart import carried_rounds
    number = carried_rounds(book, observed.get('endpoint', book.get('endpoint'))) + len(book.get('rounds', [])) + 1
    if observed.get('page') != 'champions_entry_confirm' or number > max_rounds:
        raise RuntimeError('champions_entry_not_bound')
    if kind == 'free' and cost == 0 and (quote.get('free_verified') is True):
        return dict(kind='free', cost=0)
    if quote.get('today_remaining') == 0:
        raise RuntimeError('champions_daily_entries_exhausted')
    if kind == 'ticket' and cost == 1 and (quote.get('ticket_verified') is True):
        return dict(kind='ticket', cost=1)
    historical_jewels = sum((record.get('resources_used', {}).get('jewels', 0) for record in book.get('task_history', [])))
    allowed = allow_jewels is True or (allow_final_jewels is True and max_rounds == number == 4 and (not used.get('jewels', 0)) and (not historical_jewels))
    if kind == 'jewels' and allowed and (cost == 30) and (quote.get('jewels_verified') is True):
        return dict(kind='jewels', cost=30)
    raise RuntimeError('champions_paid_entry_forbidden' if kind == 'jewels' else 'champions_entry_cost_unverified')
