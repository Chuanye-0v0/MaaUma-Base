"""Completion contract shared by the runner's adapter and receipt recovery."""

def completion_verified(result, endpoint):
    home = result.get('last_observation') or {}
    if result.get('ok') is not True or result.get('pending') is not False or type(result.get('tickets_after')) is not int or (result['tickets_after'] != 0) or (result.get('missions_checked') != ['daily', 'period']) or (home.get('page') != 'home') or (home.get('endpoint') != endpoint):
        return False
    if result.get('reason') == 'limited_legend_home_verified':
        return True
    if result.get('reason') != 'limited_legend_manual_completion_verified':
        return False
    proof = result.get('manual_reconciliation') or {}
    observed = proof.get('observation') or {}
    return proof.get('account_verified') is True and proof.get('tickets_after') == 0 and (proof.get('missions_checked') == ['daily', 'period']) and (type(proof.get('externally_completed_attempts')) is int) and (1 <= proof['externally_completed_attempts'] <= 3) and (proof.get('tickets_before') == proof['externally_completed_attempts']) and (observed.get('page') == 'legend') and (observed.get('tickets') == 0) and (observed.get('endpoint') == endpoint)
