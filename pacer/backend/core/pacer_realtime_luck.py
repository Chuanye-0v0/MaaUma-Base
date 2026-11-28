"""Pure probability-expectation objective luck relative to run deck RNG (centered at 0.0)."""

from __future__ import annotations

from collections import defaultdict
from math import comb, exp, floor, isfinite, log, sqrt, tanh
from typing import Any

from .career_training_luck import summarize_training_luck

STAT_TABS = ("speed", "stamina", "power", "guts", "wisdom")
ALL_CARD_TYPES = (*STAT_TABS, "friend", "group")

# Standard Uma Musume SingleModeSupportCardPlacement weights:
#   Stat card (modern L'Arc SSR avg specialty S=78): 178 home, 100 each of 4 off-tabs, 50 absent -> total 628.
#   Friend / Group card:                             100 each of 5 tabs, 100 absent             -> total 600.
DEFAULT_SPECIALTY_PRIORITY = 78.0
DEFAULT_INITIAL_BOND = 25.0
DEFAULT_HINT_FREQUENCY = 30.0
STAT_BASE_TAB_WEIGHT = 100.0
STAT_ABSENT_WEIGHT = 50.0
FRIEND_TAB_PROB = 100.0 / 600.0
FRIEND_ABSENT_PROB = 100.0 / 600.0

# Empirical L'Arc CN skill-hint rate per present stat support & domestic charge dispersion
HINT_PROB_PER_PRESENT_STAT = 0.0773
BASE_HINT_PROB_UNBOOSTED = 0.0595
DOMESTIC_CHARGE_SD_PER_MEMBER = 0.38
DOMESTIC_TURN_CHARGE_SE_FLOOR = 0.18

# L'Arc SSS trigger probability among random non-pity full SS matches (3rd consecutive non-SSS is guaranteed pity)
SSS_TRIGGER_PROB = 0.20
SSS_Z_WEIGHT = 0.40
SS_EXECUTED_BOARD_WEIGHT = 0.50

# Smooth mapping Z -> [-30, +30] with 0.0 strictly at mathematical expectation
Z_TANH_DIVISOR = 2.75
MAX_LUCK_SCORE = 30.0
LUCK_BASIS = "deck_rng_expectation_v3_3"


def _calendar_ordinal(period: Any, fallback_turn: int) -> int:
    if type(period) is int and 0 <= period <= 72:
        return period
    if isinstance(period, dict):
        if period.get("pre_debut") is True:
            return max(0, min(11, int(fallback_turn) - 1))
        y = period.get("year_index")
        m = period.get("month")
        half = period.get("half")
        if type(y) is int and type(m) is int and 1 <= y <= 3 and 1 <= m <= 12:
            return (y - 1) * 24 + (m - 1) * 2 + (1 if half in ("second", "last", 2, "2") else 0)
    return max(0, int(fallback_turn) - 1)


def _is_pre_debut(period: Any, ordinal: int) -> bool:
    if isinstance(period, dict):
        if period.get("pre_debut") is True:
            return True
        if type(period.get("month")) is int:
            return False
    return ordinal < 11


def _is_overseas(period: Any) -> bool:
    return (
        isinstance(period, dict)
        and period.get("year_index") in (2, 3)
        and period.get("month") in (7, 8, 9)
    )


def _stage_bond80_prob(ordinal: int) -> float:
    """Unconditional expected probability a stat support card has >=80 bond at L'Arc calendar ordinal."""
    if ordinal <= 8:
        return 0.0
    if ordinal <= 11:
        return 0.02 * (ordinal - 8)
    if ordinal <= 17:
        return 0.10 + 0.42 * (ordinal - 12) / 5.0
    if ordinal <= 23:
        return 0.57 + 0.25 * (ordinal - 18) / 5.0
    if ordinal <= 35:
        return 0.85 + 0.13 * (ordinal - 24) / 11.0
    return 0.99


def _card_bond80_prob(ordinal: int, initial_bond: float | None = None) -> float:
    """Shift expected >=80 bond curve when initial_bond is known; fall back to stage prior when None."""
    if initial_bond is None:
        return _stage_bond80_prob(ordinal)
    if ordinal <= 6:
        return 0.0
    if ordinal >= 42:
        return 0.99
    clamped_bond = max(0.0, min(50.0, float(initial_bond)))
    eff_ord = ordinal - (DEFAULT_INITIAL_BOND - clamped_bond) * 0.14
    eff_ord = max(0.0, min(48.0, eff_ord))
    lo = int(floor(eff_ord))
    hi = min(48, lo + 1)
    frac = eff_ord - lo
    return (1.0 - frac) * _stage_bond80_prob(lo) + frac * _stage_bond80_prob(hi)


def _expected_domestic_charge_per_member(ordinal: int) -> float:
    """Stage-calibrated expected preview charge cells per non-full L'Arc member."""
    if ordinal < 12:
        return 1.00
    if ordinal <= 17:
        return 1.045
    if ordinal <= 23:
        return 1.085
    if ordinal <= 35:
        return 1.195
    if ordinal <= 47:
        return 1.225
    return 1.235


def _ss_renewal_lambda(n_dom: int) -> float:
    """Integrated SS arrival intensity across elapsed domestic turns (L'Arc gauges unlock on Turn 3)."""
    if n_dom <= 2:
        return 0.0
    d1 = min(n_dom, 15) - 2
    d2 = max(0, min(n_dom, 23) - 15)
    d3 = max(0, n_dom - 23)
    return 0.122 * d1 + 0.215 * d2 + 0.240 * d3


def _erlang3_renewal_moments(lam: float) -> tuple[float, float]:
    """Exact (mean, var) of Erlang-3 renewal count N(lam) = floor(Poisson(3 * lam) / 3)."""
    if lam <= 0.0:
        return 0.0, 0.0
    rate_y = 3.0 * lam
    max_y = max(48, int(rate_y + 7.0 * sqrt(rate_y) + 18))
    pmf_n: dict[int, float] = defaultdict(float)
    log_rate = log(rate_y)
    log_fact = 0.0
    for y in range(max_y + 1):
        if y > 0:
            log_fact += log(y)
        pmf_n[y // 3] += exp(-rate_y + y * log_rate - log_fact)
    m1 = sum(k * pk for k, pk in pmf_n.items())
    var = sum(((k - m1) ** 2) * pk for k, pk in pmf_n.items())
    return m1, max(0.0, var)


def _card_tab_probs(card_type: str, specialty: float = DEFAULT_SPECIALTY_PRIORITY) -> tuple[dict[str, float], float]:
    """Return ({tab: P(card lands on tab)}, P(absent)) summing to 1.0."""
    if card_type in STAT_TABS:
        s = max(0.0, min(150.0, float(specialty)))
        total = 5.0 * STAT_BASE_TAB_WEIGHT + s + STAT_ABSENT_WEIGHT
        probs = {
            tab: ((STAT_BASE_TAB_WEIGHT + s) if tab == card_type else STAT_BASE_TAB_WEIGHT) / total
            for tab in STAT_TABS
        }
        return probs, STAT_ABSENT_WEIGHT / total
    probs = {tab: FRIEND_TAB_PROB for tab in STAT_TABS}
    return probs, FRIEND_ABSENT_PROB


def _extract_deck_cards(payload: dict[str, Any], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve up to 6 deck cards ({type, specialty, initial_bond, hint_frequency}) without stat bias."""
    supports = payload.get("supports")
    if not isinstance(supports, list):
        prof = payload.get("support_card_profile")
        supports = prof.get("cards") if isinstance(prof, dict) else None

    cards: list[dict[str, Any]] = []
    if isinstance(supports, list):
        for item in supports[:6]:
            if not isinstance(item, dict):
                continue
            ctype = item.get("type") or item.get("card_type")
            if ctype in ALL_CARD_TYPES:
                spec = item.get("specialty_priority")
                bond = item.get("initial_bond")
                hint = item.get("hint_frequency")
                spec_known = type(spec) in (int, float) and isfinite(spec)
                cards.append({
                    "type": ctype,
                    "specialty": float(spec) if spec_known else DEFAULT_SPECIALTY_PRIORITY,
                    "initial_bond": float(bond) if type(bond) in (int, float) and isfinite(bond) else None,
                    "hint_frequency": float(hint) if type(hint) in (int, float) and isfinite(hint) else None,
                    "params_resolved": spec_known,
                })
    if len(cards) >= 5:
        while len(cards) < 6:
            cards.append({
                "type": "friend",
                "specialty": 0.0,
                "initial_bond": None,
                "hint_frequency": None,
                "params_resolved": False,
            })
        return cards[:6]

    # Fallback when support_card_profile is absent: infer deck counts from max simultaneous
    # support badges observed per icon_type across turns (pure type counts, zero stat bias).
    max_simultaneous: dict[str, int] = defaultdict(int)
    saw_friend = False
    for obs in observations:
        if not isinstance(obs, dict) or not isinstance(obs.get("trainings"), list):
            continue
        per_turn: dict[str, int] = defaultdict(int)
        for tr in obs["trainings"]:
            if not isinstance(tr, dict):
                continue
            for p in tr.get("participants") or ():
                if not isinstance(p, dict):
                    continue
                role = p.get("role") or {}
                rk, it = role.get("kind"), role.get("icon_type")
                if rk == "friend" or it in ("friend", "group"):
                    saw_friend = True
                elif rk == "support" and it in STAT_TABS:
                    per_turn[it] += 1
        for k, v in per_turn.items():
            if v > max_simultaneous[k]:
                max_simultaneous[k] = min(4, v)

    inferred = list(cards)
    have_counts: dict[str, int] = defaultdict(int)
    for c in inferred:
        have_counts[c["type"]] += 1
    if saw_friend and have_counts["friend"] + have_counts["group"] == 0 and len(inferred) < 6:
        inferred.append({"type": "friend", "specialty": 0.0, "initial_bond": None, "hint_frequency": None, "params_resolved": False})
        have_counts["friend"] += 1
    for tab in ("speed", "wisdom", "stamina", "power", "guts"):
        need = max(0, max_simultaneous.get(tab, 0) - have_counts[tab])
        for _ in range(need):
            if len(inferred) < 6:
                inferred.append({"type": tab, "specialty": DEFAULT_SPECIALTY_PRIORITY, "initial_bond": None, "hint_frequency": None, "params_resolved": False})
                have_counts[tab] += 1

    # Fill any remaining unknown slots with standard L'Arc deck prior
    default_fill = ("speed", "speed", "wisdom", "stamina", "speed", "friend")
    for fallback_type in default_fill:
        if len(inferred) >= 6:
            break
        if fallback_type == "friend" and (have_counts["friend"] + have_counts["group"]) >= 1:
            continue
        inferred.append({
            "type": fallback_type,
            "specialty": DEFAULT_SPECIALTY_PRIORITY if fallback_type in STAT_TABS else 0.0,
            "initial_bond": None,
            "hint_frequency": None,
            "params_resolved": False,
        })
        have_counts[fallback_type] += 1
    while len(inferred) < 6:
        inferred.append({"type": "wisdom", "specialty": DEFAULT_SPECIALTY_PRIORITY, "initial_bond": None, "hint_frequency": None, "params_resolved": False})
    return inferred[:6]


def _bond_rainbow_ready(participant: dict[str, Any]) -> bool:
    bond = participant.get("bond")
    if not isinstance(bond, dict) or bond.get("status") != "accepted":
        return False
    if bond.get("is_max") is True or bond.get("color") in ("orange", "yellow"):
        return True
    fill = bond.get("fill_ratio_estimate")
    return type(fill) in (int, float) and fill >= 0.79


def _poisson_binomial_dist(probs: list[float]) -> list[float]:
    """Exact PMF of sum of independent Bernoullis (len <= 6)."""
    dp = [1.0]
    for p in probs:
        q = 1.0 - p
        nxt = [0.0] * (len(dp) + 1)
        for k, val in enumerate(dp):
            nxt[k] += val * q
            nxt[k + 1] += val * p
        dp = nxt
    return dp


def _clustering_moments(
    card_distributions: list[tuple[str, dict[str, float], float]],
) -> tuple[float, float, float, float]:
    expected_present = sum(1.0 - abs_p for _, _, abs_p in card_distributions)
    var_present = sum(abs_p * (1.0 - abs_p) for _, _, abs_p in card_distributions)
    expected_tab_overload = 0.0
    var_tab_overload = 0.0
    for tab in STAT_TABS:
        pmf = _poisson_binomial_dist([tp[tab] for _, tp, _ in card_distributions])
        m1 = sum(max(0, k - 1) * pk for k, pk in enumerate(pmf))
        m2 = sum((max(0, k - 1) ** 2) * pk for k, pk in enumerate(pmf))
        expected_tab_overload += m1
        var_tab_overload += max(0.0, m2 - m1 * m1)
    return expected_present, var_present, expected_tab_overload, var_tab_overload


_BOARD_TERM_CORR: dict[tuple[str, str], float] = {
    ("a1", "a2"): 0.55,
    ("a1", "a2b"): 0.46,
    ("a2", "a2b"): 0.05,
    ("a2b", "a3"): 0.25,
    ("a1", "a3"): 0.15,
}


def _board_term_corr(k1: str, k2: str) -> float:
    if k1 == k2:
        return 1.0
    if (k1, k2) in _BOARD_TERM_CORR:
        return _BOARD_TERM_CORR[(k1, k2)]
    return _BOARD_TERM_CORR.get((k2, k1), 0.0)


def _excess_stack_moments_poisson_binomial(probs: list[float]) -> tuple[float, float]:
    """Exact (mean, var) of max(0, K - 1) for K ~ PoissonBinomial(probs)."""
    if len(probs) <= 1 or max(probs, default=0.0) <= 0.0:
        return 0.0, 0.0
    pmf = _poisson_binomial_dist([min(1.0, max(0.0, p)) for p in probs])
    m1 = sum(max(0, k - 1) * pk for k, pk in enumerate(pmf))
    m2 = sum((max(0, k - 1) ** 2) * pk for k, pk in enumerate(pmf))
    return m1, max(0.0, m2 - m1 * m1)


def _excess_stack_moments_binomial(n_cards: int, p_eff: float) -> tuple[float, float]:
    """Exact (mean, var) of max(0, K - 1) for K ~ Binomial(n_cards, p_eff)."""
    if n_cards <= 1 or p_eff <= 0.0:
        return 0.0, 0.0
    p = min(1.0, max(0.0, p_eff))
    q = 1.0 - p
    m1 = 0.0
    m2 = 0.0
    for k in range(2, n_cards + 1):
        pk = comb(n_cards, k) * (p ** k) * (q ** (n_cards - k))
        ex = float(k - 1)
        m1 += ex * pk
        m2 += (ex * ex) * pk
    return m1, max(0.0, m2 - m1 * m1)


def _rainbow_sidekick_moments(
    card_distributions: list[tuple[str, dict[str, float], float]],
    stage_q: float,
    card_qs: list[float] | None = None,
) -> tuple[float, float]:
    """Exact (mean, var) of non-rainbow support cards co-clustering onto active rainbow tabs."""
    n = len(card_distributions)
    qs = [min(1.0, max(0.0, float(q))) for q in (card_qs if card_qs is not None else [stage_q] * n)]
    if n <= 1 or not qs or max(qs) <= 0.0:
        return 0.0, 0.0
    non_rb_prob: list[dict[str, float]] = []
    rb_prob: list[dict[str, float]] = []
    for (ctype, tp, _), q in zip(card_distributions, qs):
        non_rb_prob.append({
            t: tp[t] * (1.0 - q) if ctype == t else tp[t]
            for t in STAT_TABS
        })
        rb_prob.append({
            t: tp[t] * q if ctype == t else 0.0
            for t in STAT_TABS
        })

    def prob_any_rb_excluding(tab: str, excl_a: int, excl_b: int = -1) -> float:
        no_rb = 1.0
        for idx in range(n):
            if idx == excl_a or idx == excl_b:
                continue
            pr = rb_prob[idx][tab]
            if pr > 0.0:
                no_rb *= 1.0 - pr
        return 1.0 - no_rb

    p_ind = [0.0] * n
    for j in range(n):
        p_ind[j] = sum(
            non_rb_prob[j][t] * prob_any_rb_excluding(t, j)
            for t in STAT_TABS
        )
    mu = sum(p_ind)
    if mu <= 0.0:
        return 0.0, 0.0

    pair_sum = 0.0
    for j in range(n):
        for k in range(j + 1, n):
            rb_excl = {t: prob_any_rb_excluding(t, j, k) for t in STAT_TABS}
            p_jk = 0.0
            for t1 in STAT_TABS:
                pj = non_rb_prob[j][t1]
                if pj <= 0.0 or rb_excl[t1] <= 0.0:
                    continue
                for t2 in STAT_TABS:
                    pk = non_rb_prob[k][t2]
                    if pk <= 0.0:
                        continue
                    if t1 == t2:
                        p_jk += pj * pk * rb_excl[t1]
                    elif rb_excl[t2] > 0.0:
                        p_jk += pj * pk * rb_excl[t1] * rb_excl[t2]
            pair_sum += 2.0 * p_jk

    var = max(0.0, mu + pair_sum - mu * mu)
    return mu, var


def _ss_preview_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    ss = row.get("ss_preview")
    if isinstance(ss, dict) and ss.get("status") == "accepted":
        return ss
    comp = row.get("ss_comparison")
    prev = comp.get("preview") if isinstance(comp, dict) else None
    if not isinstance(prev, dict) or prev.get("status") != "accepted":
        return None
    kind = prev.get("kind")
    if kind not in ("SS", "SSS"):
        return None
    parts = prev.get("participants")
    parts = [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []
    rc = prev.get("ready_count")
    if type(rc) is not int or not 1 <= rc <= 5:
        rc = len(parts) if 1 <= len(parts) <= 5 else None
    if rc is None:
        return None
    wp = prev.get("win_policy") if isinstance(prev.get("win_policy"), dict) else {}
    gc = sum(1 for p in parts if p.get("win_guaranteed") is True)
    if type(wp.get("guaranteed_participants")) is int:
        gc = max(gc, wp["guaranteed_participants"])
    sig = "|".join(
        str((p.get("member") or {}).get("member_id") or "")
        for p in parts
        if (p.get("member") or {}).get("member_id")
    )
    return {
        "status": "accepted",
        "kind": kind,
        "ready_count": rc,
        "guaranteed": bool(wp.get("guaranteed") is True or (len(parts) == rc and gc == rc)),
        "guaranteed_count": min(rc, gc),
        "member_signature": sig,
    }


def _mood_from_score(scored_dates: int, score: float) -> str:
    if scored_dates < 3:
        return "warming_up"
    if score >= 5.0:
        return "lucky"
    if score <= -5.0:
        return "rough"
    return "steady"


def _map_z_to_score(z: float) -> float:
    return round(max(-MAX_LUCK_SCORE, min(MAX_LUCK_SCORE, MAX_LUCK_SCORE * tanh(z / Z_TANH_DIVISOR))), 1)


def build_pacer_realtime_luck(
    payload: dict[str, Any],
    *,
    judgment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("observations must be a list")

    # Group observations by turn_index; prefer consumed action turn, else final valid 5-tab board
    by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for seq_idx, row in enumerate(observations, start=1):
        if not isinstance(row, dict):
            continue
        trainings = row.get("trainings")
        if not isinstance(trainings, list) or len(trainings) != 5:
            continue
        turn_idx = row.get("turn_index")
        turn = int(turn_idx) if type(turn_idx) in (int, float) else seq_idx
        by_turn[turn].append(row)

    chosen_turns: list[tuple[int, dict[str, Any]]] = []
    for turn in sorted(by_turn):
        entries = by_turn[turn]
        consumed = [r for r in entries if r.get("consumed") is True]
        chosen_turns.append((turn, (consumed or entries)[-1]))

    if not chosen_turns:
        return {
            "available": False,
            "score": None,
            "cumulative_score": None,
            "recent_delta": None,
            "raw_mean": None,
            "scored_dates": 0,
            "mood": "warming_up",
            "basis": LUCK_BASIS,
        }

    deck_cards = _extract_deck_cards(payload, [r for _, r in chosen_turns])
    deck_counts: dict[str, int] = defaultdict(int)
    card_distributions: list[tuple[str, dict[str, float], float]] = []
    stat_home_probs_by_tab: dict[str, list[float]] = defaultdict(list)
    for card in deck_cards:
        ctype = card["type"]
        deck_counts[ctype] += 1
        t_probs, abs_prob = _card_tab_probs(ctype, card["specialty"])
        card_distributions.append((ctype, t_probs, abs_prob))
        if ctype in STAT_TABS:
            stat_home_probs_by_tab[ctype].append(t_probs[ctype])

    # Precompute exact stationary clustering moments across the 5 tabs (full deck + stat-only for Turns 1-2)
    full_moments = _clustering_moments(card_distributions)
    stat_only_indices = [i for i, d in enumerate(card_distributions) if d[0] in STAT_TABS]
    stat_only_dists = [card_distributions[i] for i in stat_only_indices]
    stat_moments = _clustering_moments(stat_only_dists) if stat_only_dists else full_moments
    mu_hints_exact = 0.0
    var_hints_raw = 0.0
    for idx, (ct, _, abs_p) in enumerate(card_distributions):
        if ct not in STAT_TABS:
            continue
        pres_p = 1.0 - abs_p
        hf = deck_cards[idx].get("hint_frequency")
        hp = (
            min(0.35, max(0.01, BASE_HINT_PROB_UNBOOSTED * (1.0 + float(hf) / 100.0)))
            if hf is not None
            else HINT_PROB_PER_PRESENT_STAT
        )
        p_h = pres_p * hp
        mu_hints_exact += p_h
        var_hints_raw += p_h * (1.0 - p_h)
    var_hints_exact = max(0.08, var_hints_raw)

    # Index confirmed risky training judgment rolls by turn_index (exact martingale increments)
    if judgment is None:
        try:
            judgment = summarize_training_luck(payload, "left")
        except Exception:
            judgment = {"status": "insufficient_evidence", "events": []}
    judgment_by_turn: dict[int, float] = {}
    for ev in judgment.get("events") or ():
        if not isinstance(ev, dict):
            continue
        t_ev = ev.get("turn_index")
        risk = ev.get("failure_rate_percent")
        outcome = ev.get("outcome")
        if type(t_ev) is int and type(risk) in (int, float) and risk > 0 and outcome in ("success", "failure"):
            p_fail = min(0.99, max(0.005, float(risk) / 100.0))
            delta_roll = p_fail - (1.0 if outcome == "failure" else 0.0)
            judgment_by_turn[t_ev] = delta_roll / sqrt(max(p_fail * (1.0 - p_fail), 0.035))

    turn_z_list: list[float] = []
    turn_w_list: list[float] = []
    board_z_list: list[float] = []

    n_dom = 0
    k_ss = 0
    k_sss = 0
    pending_held_ss = False
    streak_non_sss = 0
    n_sss_trials = 0
    k_sss_random = 0

    for turn, row in chosen_turns:
        sb = row.get("state_before") if isinstance(row.get("state_before"), dict) else {}
        period = sb.get("period") or row.get("date")
        ordinal = _calendar_ordinal(period, turn)
        pre_debut = _is_pre_debut(period, ordinal)
        abroad = _is_overseas(period)
        if abroad:
            pending_held_ss = False
        else:
            n_dom += 1

        stage_q = _stage_bond80_prob(ordinal)
        card_qs = [
            _card_bond80_prob(ordinal, c.get("initial_bond")) if c["type"] in STAT_TABS else 0.0
            for c in deck_cards
        ]
        stat_qs = [card_qs[i] for i in stat_only_indices]
        mean_stat_q = (sum(stat_qs) / len(stat_qs)) if stat_qs else stage_q

        # Parse the 5 training facilities (strictly ignoring vitality, failure_rate, stats_gain & SS opponent circles)
        tab_rainbows: dict[str, int] = defaultdict(int)
        type_seen_total: dict[str, int] = defaultdict(int)
        raw_tab_supports: dict[str, list[str]] = defaultdict(list)
        total_hints = 0

        for tr in row["trainings"]:
            tab = tr.get("training_type")
            if tab not in STAT_TABS:
                continue
            parts = tr.get("participants") if isinstance(tr.get("participants"), list) else []
            hint_cnt = tr.get("hint_count")
            if type(hint_cnt) is int and hint_cnt > 0:
                total_hints += hint_cnt
            else:
                total_hints += sum(
                    1 for p in parts
                    if isinstance(p, dict) and isinstance(p.get("hint"), dict) and p["hint"].get("present") is True
                )

            rb_here = 0
            for p in parts:
                if not isinstance(p, dict):
                    continue
                role = p.get("role") if isinstance(p.get("role"), dict) else {}
                rk, it = role.get("kind"), role.get("icon_type")
                if rk == "friend" or it in ("friend", "group"):
                    ctype = "friend" if deck_counts["friend"] >= deck_counts["group"] else "group"
                    raw_tab_supports[tab].append(ctype)
                    type_seen_total[ctype] += 1
                elif rk == "support" and it in STAT_TABS:
                    raw_tab_supports[tab].append(it)
                    type_seen_total[it] += 1
                    if _bond_rainbow_ready(p) and it == tab:
                        rb_here += 1

            if rb_here == 0 and tr.get("friendship") is True and deck_counts[tab] > 0:
                rb_here = 1
            tab_rainbows[tab] = min(deck_counts[tab], rb_here)

        # Cap observed portraits per card type to physical deck counts to prevent OCR badge echo inflation
        remaining_type_cap = {ct: deck_counts[ct] for ct in ALL_CARD_TYPES}
        tab_support_counts: dict[str, int] = defaultdict(int)
        for tab in STAT_TABS:
            for ct in raw_tab_supports[tab]:
                if remaining_type_cap.get(ct, 0) > 0:
                    remaining_type_cap[ct] -= 1
                    tab_support_counts[tab] += 1

        # --- Sub-component (a): Board Placement, Rainbow & Stacking Z-score ---
        weighted_terms: list[tuple[str, float, float]] = []

        # (a1) Rainbow count vs exact Poisson-binomial expectation across individual cards
        tab_rb_probs: dict[str, list[float]] = defaultdict(list)
        tab_card_qs: dict[str, list[float]] = defaultdict(list)
        all_rb_probs: list[float] = []
        for idx, (ct, tp, _) in enumerate(card_distributions):
            if ct in STAT_TABS:
                q_i = card_qs[idx]
                p_rb = q_i * tp[ct]
                all_rb_probs.append(p_rb)
                tab_rb_probs[ct].append(p_rb)
                tab_card_qs[ct].append(q_i)
        mu_rb = sum(all_rb_probs)
        obs_rb = float(sum(tab_rainbows.values()))
        if mu_rb >= 0.04:
            var_rb = sum(p * (1.0 - p) for p in all_rb_probs)
            z_rb = (obs_rb - mu_rb) / sqrt(max(var_rb, 0.04))
            weighted_terms.append(("a1", 0.62, max(-8.0, min(8.0, z_rb))))

        # (a2) Multi-rainbow single-facility stacking (double/triple rainbow)
        mu_stack = 0.0
        var_stack = 0.0
        obs_stack = 0.0
        for tab in STAT_TABS:
            probs_t = tab_rb_probs.get(tab, [])
            if len(probs_t) >= 2 and max(tab_card_qs.get(tab, [0.0])) >= 0.15:
                m1, v1 = _excess_stack_moments_poisson_binomial(probs_t)
                mu_stack += m1
                var_stack += v1
                obs_stack += float(max(0, tab_rainbows[tab] - 1))
        if var_stack >= 0.005:
            z_stack = (obs_stack - mu_stack) / sqrt(var_stack)
            weighted_terms.append(("a2", 0.40, max(-8.0, min(8.0, z_stack))))

        # (a2b) Rainbow co-clustering: non-rainbow sidekick cards landing on active rainbow tabs
        use_stat_only = pre_debut and ordinal <= 1 and (type_seen_total["friend"] + type_seen_total["group"] == 0)
        active_dists = stat_only_dists if use_stat_only else card_distributions
        active_qs = [card_qs[idx] for idx in stat_only_indices] if use_stat_only else card_qs
        if max(active_qs, default=0.0) >= 0.10:
            mu_sk, var_sk = _rainbow_sidekick_moments(active_dists, stage_q, card_qs=active_qs)
            if var_sk >= 0.015:
                obs_sk = float(
                    sum(
                        max(0, tab_support_counts[t] - tab_rainbows[t])
                        for t in STAT_TABS
                        if tab_rainbows[t] >= 1
                    )
                )
                z_sk = (obs_sk - mu_sk) / sqrt(var_sk)
                weighted_terms.append(("a2b", 0.36, max(-8.0, min(8.0, z_sk))))

        # (a3) Support presence + multi-card facility clustering
        # Taper generic blank-tab overload weight as rainbow stage_q matures so rainbow co-clustering dominates
        exp_pres, var_pres, exp_over, var_over = stat_moments if use_stat_only else full_moments
        obs_present = float(sum(tab_support_counts.values()))
        obs_overload = float(sum(max(0, tab_support_counts[t] - 1) for t in STAT_TABS))
        z_pres = (obs_present - exp_pres) / sqrt(max(var_pres, 0.15))
        z_over = (obs_overload - exp_over) / sqrt(max(var_over, 0.15))
        z_cluster = 0.35 * z_pres + 0.65 * z_over
        w_cluster = 0.34 * (1.0 - 0.55 * mean_stat_q)
        weighted_terms.append(("a3", w_cluster, max(-8.0, min(8.0, z_cluster))))

        # (a4) Skill hints surprise (using per-card hint_frequency when known)
        z_hints = (float(total_hints) - mu_hints_exact) / sqrt(var_hints_exact)
        weighted_terms.append(("a4", 0.14, max(-8.0, min(8.0, z_hints))))

        board_var = sum(
            wi * wj * _board_term_corr(ki, kj)
            for ki, wi, _ in weighted_terms
            for kj, wj, _ in weighted_terms
        )
        board_norm = sqrt(max(board_var, 1e-12))
        z_board = sum(w * z for _, w, z in weighted_terms) / max(board_norm, 1e-6)
        board_z_list.append(z_board)

        # --- Sub-component (b): Deduplicated SS/SSS metadata + Pity-3 SSS RNG trial tracking ---
        ss_prev = _ss_preview_from_row(row)
        executed_ss = row.get("kind") == "ss"
        full_ss_ready = ss_prev is not None and ss_prev.get("ready_count") == 5
        if not abroad and (executed_ss or full_ss_ready):
            ss_kind = (ss_prev or {}).get("kind")
            if not pending_held_ss:
                k_ss += 1
                is_sss = ss_kind == "SSS"
                if is_sss:
                    k_sss += 1
                if streak_non_sss < 2:
                    n_sss_trials += 1
                    if is_sss:
                        k_sss_random += 1
                        streak_non_sss = 0
                    else:
                        streak_non_sss += 1
                else:
                    # 3rd consecutive full SS without SSS is guaranteed pity SSS (0 RNG variance)
                    streak_non_sss = 0
            pending_held_ss = not executed_ss

        # Attenuate unclicked training-board Z on SS-executed turns to reduce selection-bias drag
        w_board = SS_EXECUTED_BOARD_WEIGHT if executed_ss else 1.0
        z_turn = w_board * z_board

        # --- Sub-component (c): Training Judgment Martingale Shock ---
        if turn in judgment_by_turn:
            z_turn += 0.55 * max(-6.5, min(6.5, judgment_by_turn[turn]))

        turn_z_list.append(z_turn)
        turn_w_list.append(w_board)

    n = len(turn_z_list)
    w_sum_sq = sum(w * w for w in turn_w_list)
    cum_board_z = sum(turn_z_list) / sqrt(max(w_sum_sq, 4.0))
    m_rec = min(n, 5)
    rec_w = turn_w_list[-m_rec:]
    rec_w_sq = sum(w * w for w in rec_w)
    rec_z = sum(turn_z_list[-m_rec:]) / sqrt(max(rec_w_sq, 2.5)) if rec_w_sq > 0.0 else 0.0

    if n_sss_trials >= 1:
        sss_z_raw = (k_sss_random - n_sss_trials * SSS_TRIGGER_PROB) / sqrt(
            n_sss_trials * SSS_TRIGGER_PROB * (1.0 - SSS_TRIGGER_PROB)
        )
        sss_z = max(-6.0, min(6.0, sss_z_raw))
        w_sss = SSS_Z_WEIGHT * min(1.0, sqrt(float(n_sss_trials) / 4.0))
    else:
        sss_z = 0.0
        w_sss = 0.0

    cum_norm = sqrt(1.0 + w_sss * w_sss)
    cum_total_z = (cum_board_z + w_sss * sss_z) / cum_norm

    ss_lam = _ss_renewal_lambda(n_dom)
    mu_ss, _ = _erlang3_renewal_moments(ss_lam)

    # Exact dynamic overlap denominator between cumulative and recent window means
    rho_cr = (
        (rec_w_sq / sqrt(max(w_sum_sq, 4.0) * max(rec_w_sq, 2.5))) / cum_norm
        if rec_w_sq > 0.0 else 0.0
    )
    comp_norm = sqrt(0.78 ** 2 + 0.22 ** 2 + 2.0 * 0.78 * 0.22 * rho_cr)
    composite_z = (0.78 * cum_total_z + 0.22 * rec_z) / comp_norm
    clamped_score = _map_z_to_score(composite_z)
    cumulative_score = _map_z_to_score(cum_total_z)
    recent_rounded = _map_z_to_score(rec_z)
    mood = _mood_from_score(n, clamped_score)

    return {
        "available": True,
        "score": clamped_score,
        "cumulative_score": cumulative_score,
        "recent_delta": recent_rounded,
        "raw_mean": None,
        "scored_dates": n,
        "mood": mood,
        "basis": LUCK_BASIS,
        "coverage_label": f"较理论期望 · {n} 回合",
        "deck_counts": {k: int(deck_counts[k]) for k in ALL_CARD_TYPES if deck_counts[k] > 0},
        "components": {
            "board_z": round(sum(w * zb for w, zb in zip(turn_w_list, board_z_list)) / sqrt(max(w_sum_sq, 1.0)), 3),
            "charge_z": 0.0,
            "sss_z": round(sss_z, 3),
            "sss_random_trials": n_sss_trials,
            "sss_random_hits": k_sss_random,
            "ss_charge_z": 0.0,
            "ss_pace_z": 0.0,
            "ss_count": k_ss,
            "sss_count": k_sss,
            "ss_expected": round(mu_ss, 2),
            "card_params_resolved": sum(1 for c in deck_cards if c.get("params_resolved")),
            "judgment_z": round(sum(judgment_by_turn.values()) / sqrt(max(float(len(judgment_by_turn)), 1.0)), 3)
            if judgment_by_turn else 0.0,
        },
    }


def summarize_realtime_luck(current_review: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "waiting_for_current"}
    if current_review is None:
        return result

    try:
        judgment = summarize_training_luck(current_review, "left")
    except Exception:
        judgment = {
            "status": "insufficient_evidence",
            "score": None,
            "recorded_trainings": 0,
            "scored_trainings": 0,
            "successes": 0,
            "failures": 0,
            "unscored": 0,
            "events": [],
        }
    computed = build_pacer_realtime_luck(current_review, judgment=judgment)
    result["status"] = "insufficient_observations"
    if computed["available"]:
        result.update(
            status="recorded",
            score=computed["score"],
            cumulative_score=computed["cumulative_score"],
            recent_delta=computed["recent_delta"],
            raw_mean=computed["raw_mean"],
            scored_dates=computed["scored_dates"],
            mood=computed["mood"],
            basis=computed["basis"],
            coverage_label=computed["coverage_label"],
            deck_counts=computed["deck_counts"],
            components=computed["components"],
            career_complete=bool((current_review.get("run") or {}).get("career_complete")),
        )

    result["training_luck"] = {
        key: judgment[key]
        for key in (
            "status",
            "score",
            "recorded_trainings",
            "scored_trainings",
            "successes",
            "failures",
            "unscored",
        )
    }
    return result
