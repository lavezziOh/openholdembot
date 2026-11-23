"""Opponent range engine: a weight for each of the 1326 hole-card combinations of every opponent,
narrowed by each public action, and the win probability against those ranges ("prwin vs ranges").

Only public information is used (the actions seen at the table and statistics built from the actions of
past hands, like a PokerTracker HUD): never the opponents' hole cards.

Preflop: per player, frequencies of raise / call / fold in four spots (unopened, vs limpers, vs one raise,
vs 3bet+), blended with population priors. A raise keeps the top of the current range in the "raise"
order (OpenHoldem handrank vs 1 opponent); a call keeps the next part in the "call" order (handrank vs 4
opponents: pairs and suited connectors rank higher), and the raise part only with a small "trap" weight:
this is the cap (a limper rarely has AA, a passive cold-caller rarely has QQ+).
Postflop: each combination gets a strength on the board (made-hand percentile + draw bonus). A bet keeps the
strongest part plus some bluffs (the weakest part), a call the middle part (the top only with the
trap weight, small for players that raise a lot), a check removes most of the strong part.

The symbol `dll$prwin_range` (OpenHoldem would get it from a user DLL) is the win probability against
the ranges of all live opponents (ties count half).
"""
import bisect
import random
from collections import Counter, defaultdict

from . import handeval as he

PREFLOP, FLOP, TURN, RIVER = 1, 2, 3, 4
RCH = "23456789TJQKA"

COMBOS = [((r1, s1), (r2, s2)) for r1 in range(2, 15) for s1 in range(4)
          for r2 in range(2, 15) for s2 in range(4) if (r1, s1) < (r2, s2)]
assert len(COMBOS) == 1326


def _class(c):
    (r1, s1), (r2, s2) = sorted(c, reverse=True)
    if r1 == r2:
        return RCH[r1 - 2] * 2 + "o"
    return RCH[r1 - 2] + RCH[r2 - 2] + ("s" if s1 == s2 else "o")


def _orders():
    from .symbols import HANDRANK
    raise_rank = {h: i for i, h in enumerate(HANDRANK["t169"][0])}     # vs 1 opponent: high cards
    call_rank = {h: i for i, h in enumerate(HANDRANK["t169"][3])}      # vs 4: pairs, suited, connectors
    rk = [-raise_rank[_class(c)] for c in COMBOS]                       # larger = stronger
    ck = [-call_rank[_class(c)] for c in COMBOS]
    return rk, ck


RAISE_KEY, CALL_KEY = _orders()

# population priors (frequencies) and their weight in hands
PRIOR_N = 15
PRIORS = {
    "pf_unopened": {"raise": .17, "call": .05, "fold": .78},
    "pf_vs_limp": {"raise": .10, "call": .25, "fold": .65},
    "pf_vs_raise": {"raise": .07, "call": .15, "fold": .78},
    "pf_vs_3bet": {"raise": .20, "call": .35, "fold": .45},
    "flop_first_pfa": {"bet": .65, "check": .35},
    "flop_first": {"bet": .20, "check": .80},
    "turn_first": {"bet": .35, "check": .65},
    "river_first": {"bet": .30, "check": .70},
    "flop_vs_bet": {"raise": .10, "call": .45, "fold": .45},
    "turn_vs_bet": {"raise": .08, "call": .45, "fold": .47},
    "river_vs_bet": {"raise": .06, "call": .42, "fold": .52},
    # overcall: somebody already called the bet on this street (a stronger, tighter range)
    "flop_vs_bet_oc": {"raise": .04, "call": .30, "fold": .66},
    "turn_vs_bet_oc": {"raise": .03, "call": .30, "fold": .67},
    "river_vs_bet_oc": {"raise": .03, "call": .28, "fold": .69},
}
STREET = {FLOP: "flop", TURN: "turn", RIVER: "river"}
OUT = 0.04          # weight left to combinations outside the part an action keeps (models mistakes / noise)
# tunable shape of the postflop narrowing (calibrated with D:/ohsim_solver/ranges/accuracy.py)
P = {
    "VAL": 0.75,         # share of a bet/raise frequency taken by value hands (the rest are bluffs)
    "CHECK_TRAP": 0.3,   # weight kept by the strong part after a check
    "CALL_TRAP": 0.5,    # weight kept by the raising part after a call (more for passive players)
    "OUT": 0.04,
    "PF_CALL_TRAP": 0.3,
    "OVERCALL": False,   # separate statistics (and tighter priors) for calls after another caller
    "AIR_F": 1.0,        # extra factor for "air" (no pair with the hole cards, no draw) in a calling range
    "AIR_MAX_CONT": 0.6, # ... not for players who continue (call+raise) more often than this
}


def _norm(kind):
    if kind in ("raise", "bet", "allin-raise"):
        return "raise"
    if kind in ("call", "allin-call"):
        return "call"
    return kind                     # check / fold


def walk(history, n_players, button_chair=None):
    """Yields (record, node, last_preflop_raiser) for every voluntary action, with the spot before it."""
    pf_raises = 0
    pf_limps = 0
    last_pf_raiser = None
    street = None
    street_aggr = 0
    street_calls = 0
    for h in history:
        if h.kind in ("sb", "bb", "ante"):
            continue
        if h.street != street:
            street, street_aggr, street_calls = h.street, 0, 0
        k = _norm(h.kind)
        if street == PREFLOP:
            if pf_raises == 0:
                node = "pf_vs_limp" if pf_limps else "pf_unopened"
            elif pf_raises == 1:
                node = "pf_vs_raise"
            else:
                node = "pf_vs_3bet"
            yield h, node, last_pf_raiser
            if k == "raise":
                pf_raises += 1
                last_pf_raiser = h.chair
            elif k == "call" and pf_raises == 0:
                pf_limps += 1
        else:
            name = STREET[street]
            if street_aggr == 0:
                node = f"{name}_first_pfa" if (street == FLOP and h.chair == last_pf_raiser) else f"{name}_first"
            else:
                node = f"{name}_vs_bet_oc" if street_calls else f"{name}_vs_bet"
            yield h, node, last_pf_raiser
            if k == "raise":
                street_aggr += 1
                street_calls = 0
            elif k == "call" and street_aggr:
                street_calls += 1


class PlayerModel:
    """Action frequencies per player and spot, from the public actions of finished hands."""

    def __init__(self):
        self.counts = defaultdict(Counter)          # (player, node) -> Counter(action)
        self.dealt = Counter()                      # VPIP (for OpenHoldem's willplay weighting)
        self.vpip_n = Counter()

    def vpip(self, player, prior=0.25, n=15):
        return (self.vpip_n[player] + prior * n) / (self.dealt[player] + n)

    def add_hand(self, table):
        names = {s.chair: s.name for s in table.seats}
        vol = {h.chair for h in table.history if h.street == PREFLOP and _norm(h.kind) in ("call", "raise")}
        for s in table.seats:
            self.dealt[s.name] += 1
            if s.chair in vol:
                self.vpip_n[s.name] += 1
        for h, node, _ in walk(table.history, table.n):
            k = _norm(h.kind)
            if node.startswith("pf_") and k == "check":
                k = "call"                           # BB checking behind limpers = passive entry
            if "first" in node:
                k = "bet" if k == "raise" else "check"
            self.counts[(names[h.chair], node)][k] += 1
            if node.endswith("_oc"):                 # overcalls also count as plain "vs bet" actions
                self.counts[(names[h.chair], node[:-3])][k] += 1

    def freq(self, player, node):
        pr = PRIORS[node]
        c = self.counts.get((player, node), Counter())
        n = sum(c.values())
        return {a: (c[a] + PRIOR_N * p) / (n + PRIOR_N) for a, p in pr.items()}


# ---------------------------------------------------------------- strength on a board
_STRENGTH = {}


def _draw_bonus(hole, board, street):
    if street == RIVER:
        return 0.0
    cards = list(hole) + list(board)
    b = 0.0
    # flush draw: 4 of a suit with at least one hole card
    for s in range(4):
        n = sum(1 for c in cards if c[1] == s)
        if n == 4 and any(c[1] == s for c in hole):
            b += 0.18 if street == FLOP else 0.09
            break
    # straight draw: ranks that would complete a straight (not already one, not on the board alone)
    mask = 0
    bmask = 0
    for r, _ in cards:
        mask |= 1 << r
    for r, _ in board:
        bmask |= 1 << r
    if he._straight_high(mask):
        return b
    outs = 0
    for x in range(2, 15):
        if he._straight_high(mask | 1 << x) and not he._straight_high(bmask | 1 << x):
            outs += 1
    if outs >= 2:
        b += 0.14 if street == FLOP else 0.07
    elif outs == 1:
        b += 0.05 if street == FLOP else 0.025
    return b


_AIR = {}


def air(board):
    """True for the combinations with nothing: no pocket pair, no hole card paired with the board,
    no straight or flush made with the hole cards, no draw (on the river: no draw by definition)."""
    key = tuple(sorted(board))
    a = _AIR.get(key)
    if a is not None:
        return a
    street = {3: FLOP, 4: TURN, 5: RIVER}[len(board)]
    branks = {r for r, _ in board}
    bmask = 0
    for r, _ in board:
        bmask |= 1 << r
    bset = set(board)
    a = []
    for c in COMBOS:
        (r1, s1), (r2, s2) = c
        if c[0] in bset or c[1] in bset or r1 == r2 or r1 in branks or r2 in branks:
            a.append(False)
            continue
        cards = list(c) + list(board)
        mask = bmask | 1 << r1 | 1 << r2
        straight = he._straight_high(mask) and not he._straight_high(bmask)
        flush = any(sum(1 for x in cards if x[1] == s) >= 5 for s in (s1, s2))
        a.append(not straight and not flush and _draw_bonus(c, board, street) == 0)
    if len(_AIR) > 2000:
        _AIR.clear()
    _AIR[key] = a
    return a


def strengths(board):
    """Strength in [0, ~1.2] of every combination on this board (None if it uses a board card)."""
    key = tuple(sorted(board))
    s = _STRENGTH.get(key)
    if s is not None:
        return s
    bset = set(board)
    street = {3: FLOP, 4: TURN, 5: RIVER}[len(board)]
    sc = [None if (c[0] in bset or c[1] in bset) else he.score(list(c) + list(board)) for c in COMBOS]
    live = sorted(v for v in sc if v is not None)
    n = len(live)
    s = []
    for c, v in zip(COMBOS, sc):
        if v is None:
            s.append(None)
            continue
        pct = (bisect.bisect_left(live, v) + bisect.bisect_right(live, v)) / (2 * n)
        s.append(pct + _draw_bonus(c, board, street))
    if len(_STRENGTH) > 2000:
        _STRENGTH.clear()
    _STRENGTH[key] = s
    return s


# ---------------------------------------------------------------- range narrowing
def _keep(w, key, lo, hi, inside=1.0, outside=None, part_factor=None):
    """Multiply the weights: combinations whose cumulative weight (strongest first by `key`) falls in
    [lo, hi] of the current total keep `inside`, the others `outside`."""
    if outside is None:
        outside = P["OUT"]
    idx = [i for i in range(len(w)) if w[i] > 0 and key[i] is not None]
    idx.sort(key=lambda i: key[i], reverse=True)
    tot = sum(w[i] for i in idx)
    if tot <= 0:
        return
    acc = 0.0
    for i in idx:
        mid = (acc + w[i] / 2) / tot
        acc += w[i]
        w[i] *= inside if lo <= mid <= hi else outside


def _scale_part(w, key, lo, hi, factor):
    """Multiply by `factor` only the combinations in [lo, hi] of the cumulative weight."""
    _keep(w, key, lo, hi, inside=factor, outside=1.0)


def narrow(w, node, kind, f, board):
    k = _norm(kind)
    if node.startswith("pf_"):
        r = max(f.get("raise", 0), 0.01)
        c = max(f.get("call", 0), 0.01)
        if k == "raise":
            _keep(w, RAISE_KEY, 0, r)
        elif k in ("call", "check"):
            # the part this player would have raised: kept only with a small "trap" weight (the cap)
            rp = [0.0] * len(w)
            _mark(w, RAISE_KEY, 0, r, rp)
            trap = P["PF_CALL_TRAP"] if k == "call" else 0.35          # BB check: no choice, the cap is weaker
            if k == "call":
                # calling part: the next c of the range (c/(1-r) of what is left), in the "call" order
                rest = [0.0 if rp[i] else w[i] for i in range(len(w))]
                cp = [0.0] * len(w)
                _mark(rest, CALL_KEY, 0, min(1.0, c / max(1e-6, 1 - r)), cp)
                for i in range(len(w)):
                    w[i] *= trap if rp[i] else (1.0 if cp[i] else P["OUT"])
            else:
                for i in range(len(w)):
                    if rp[i]:
                        w[i] *= trap
        return
    st = strengths(board)
    if "first" in node:
        b = max(f.get("bet", 0), 0.02)
        if k == "raise":
            val, bl = P["VAL"] * b, (1 - P["VAL"]) * b
            # value part from the top, bluffs from the bottom
            keep = [0.0] * len(w)
            _mark(w, st, 0, val, keep)
            _mark(w, st, 1 - bl, 1, keep)
            for i in range(len(w)):
                w[i] *= 1.0 if keep[i] else P["OUT"]
        else:                                           # check: the strong part mostly bets
            _scale_part(w, st, 0, P["VAL"] * b, P["CHECK_TRAP"])
    else:
        r = max(f.get("raise", 0), 0.01)
        c = max(f.get("call", 0), 0.02)
        if k == "raise":
            keep = [0.0] * len(w)
            _mark(w, st, 0, P["VAL"] * r, keep)
            _mark(w, st, 1 - (1 - P["VAL"]) * r, 1, keep)
            for i in range(len(w)):
                w[i] *= 1.0 if keep[i] else P["OUT"]
        elif k == "call":
            # the top is raised (only partly by passive players), then the calling part
            trap = P["CALL_TRAP"] + 0.5 * max(0.0, 0.15 - r) / 0.15
            _scale_part(w, st, 0, P["VAL"] * r, trap)
            _keep(w, st, 0, min(1.0, P["VAL"] * r + c), inside=1.0, outside=P["OUT"])
            if P["AIR_F"] != 1.0 and r + c < P["AIR_MAX_CONT"]:
                # calling a bet with nothing (no pair, no draw) is rare unless the player calls everything
                for i, x in enumerate(air(board)):
                    if x:
                        w[i] *= P["AIR_F"]


def _mark(w, key, lo, hi, out):
    idx = [i for i in range(len(w)) if w[i] > 0 and key[i] is not None]
    idx.sort(key=lambda i: key[i], reverse=True)
    tot = sum(w[i] for i in idx)
    if tot <= 0:
        return
    acc = 0.0
    for i in idx:
        mid = (acc + w[i] / 2) / tot
        acc += w[i]
        if lo <= mid <= hi:
            out[i] = 1.0


def opponent_ranges(table, me_chair, model, my_hole, street_start=None):
    """{chair: weights} for every opponent still in the hand, from the actions of this hand.
    If `street_start` is a dict, it receives a copy of the weights before the actions of the current street."""
    t = table
    dead = set(my_hole)
    live = [s for s in t.seats if s.in_hand and not s.folded and s.chair != me_chair]
    names = {s.chair: s.name for s in t.seats}
    W = {}
    for s in live:
        W[s.chair] = [0.0 if (c[0] in dead or c[1] in dead) else 1.0 for c in COMBOS]
    board_at = {PREFLOP: [], FLOP: list(t.board[:3]), TURN: list(t.board[:4]), RIVER: list(t.board[:5])}
    cur = {0: PREFLOP, 3: FLOP, 4: TURN, 5: RIVER}[len(t.board)]
    snap = False
    for h, node, _ in walk(t.history, t.n):
        if node.endswith("_oc") and not P["OVERCALL"]:
            node = node[:-3]
        if street_start is not None and not snap and h.street >= cur:
            street_start.update({c: list(w) for c, w in W.items()})
            snap = True
        if h.chair not in W:
            continue
        f = model.freq(names[h.chair], node)
        narrow(W[h.chair], node, h.kind, f, board_at[h.street])
    if street_start is not None and not snap:
        street_start.update({c: list(w) for c, w in W.items()})
    bset = set(t.board)
    for w in list(W.values()) + (list(street_start.values()) if street_start is not None else []):
        for i, c in enumerate(COMBOS):                    # board cards can't be in a hand
            if c[0] in bset or c[1] in bset:
                w[i] = 0.0
    return W


def bet_outlook(table, me_chair, model, my_hole, bet=0.66, iters=400, rng=None):
    """What a bet of `bet` x pot would do now (postflop, nothing to call), against the ranges:
    returns (fold, eq_call, eq_now, gain).
      fold    = probability that every opponent folds. Each opponent continues (calls or raises) with the
                share raise+call of his "vs bet" frequencies; the strength cut is taken on his range at the
                start of the street, so a range capped by a check on this street folds more often.
      eq_call = win probability against the ranges that continue;
      eq_now  = win probability against the current ranges (the value of checking, roughly);
      gain    = EV(bet) - EV(check) in pots: fold + (1-fold)*(eq_call*(1+2*bet) - bet) - eq_now."""
    t = table
    start = {}
    W = opponent_ranges(t, me_chair, model, my_hole, street_start=start)
    if not W or len(t.board) < 3:
        return 1.0, 1.0, 1.0, 0.0
    names = {s.chair: s.name for s in t.seats}
    st = strengths(list(t.board))
    node = STREET[{3: FLOP, 4: TURN, 5: RIVER}[len(t.board)]] + "_vs_bet"
    fold_all = 1.0
    calling = []
    for chair, w in W.items():
        f = model.freq(names[chair], node)
        cont = min(1.0, f.get("raise", 0) + f.get("call", 0))
        ws = start.get(chair, w)
        idx = sorted((i for i in range(len(ws)) if ws[i] > 0 and st[i] is not None),
                     key=lambda i: st[i], reverse=True)
        tot = sum(ws[i] for i in idx)
        cut = -1.0
        acc = 0.0
        for i in idx:
            acc += ws[i]
            if acc >= cont * tot:
                cut = st[i]
                break
        now = sum(x for x in w)
        weak = sum(w[i] for i in range(len(w)) if w[i] > 0 and st[i] is not None and st[i] < cut)
        fold_all *= (weak / now) if now > 0 else 0.0
        calling.append([w[i] * (P["OUT"] if (st[i] is None or st[i] < cut) else 1.0) for i in range(len(w))])
    eq_now = equity(my_hole, t.board, list(W.values()), iters=iters, rng=rng)
    eq_call = equity(my_hole, t.board, calling, iters=iters, rng=rng)
    gain = fold_all + (1 - fold_all) * (eq_call * (1 + 2 * bet) - bet) - eq_now
    return fold_all, eq_call, eq_now, gain


def equity(my_hole, board, ranges, iters=400, rng=None):
    """Win probability (ties count half) against one hand drawn from each weighted range."""
    rng = rng or random
    cum = []
    for w in ranges:
        acc, cw = 0.0, []
        for x in w:
            acc += x
            cw.append(acc)
        if acc <= 0:
            return 0.5
        cum.append(cw)
    known = set(my_hole) | set(board)
    deck = [(r, s) for r in range(2, 15) for s in range(4) if (r, s) not in known]
    need = 5 - len(board)
    win = 0.0
    done = 0
    for _ in range(iters):
        used = set(known)
        hands = []
        ok = True
        for cw in cum:
            for _try in range(20):
                c = COMBOS[bisect.bisect_left(cw, rng.random() * cw[-1])]
                if c[0] not in used and c[1] not in used:
                    break
            else:
                ok = False
                break
            used.update(c)
            hands.append(c)
        if not ok:
            continue
        rest = [c for c in deck if c not in used]
        full = list(board) + rng.sample(rest, need)
        mine = he.score(list(my_hole) + full)
        best = max(he.score(list(h) + full) for h in hands)
        win += 1.0 if mine > best else 0.5 if mine == best else 0.0
        done += 1
    return win / done if done else 0.5


def raise_equity(table, me_chair, model, my_hole, iters=400, rng=None):
    """Win probability against the hands that would continue (call or re-raise) if we raised now.
    Each live opponent continues with the share raise+call of his "vs bet" frequencies, taken from the top
    of his CURRENT range (a bettor's range is already narrowed by his bet)."""
    t = table
    W = opponent_ranges(t, me_chair, model, my_hole)
    if not W or len(t.board) < 3:
        return 1.0
    names = {s.chair: s.name for s in t.seats}
    st = strengths(list(t.board))
    node = STREET[{3: FLOP, 4: TURN, 5: RIVER}[len(t.board)]] + "_vs_bet"
    cont_ranges = []
    for chair, w in W.items():
        f = model.freq(names[chair], node)
        cont = min(1.0, f.get("raise", 0) + f.get("call", 0))
        keep = [0.0] * len(w)
        _mark(w, st, 0, cont, keep)
        cont_ranges.append([w[i] * (1.0 if keep[i] else P["OUT"]) for i in range(len(w))])
    return equity(my_hole, t.board, cont_ranges, iters=iters, rng=rng)

# ---------------------------------------------------------------- OpenHoldem's own weighted prwin
# CIteratorThread.cpp: Sklansky-Chubukov order, one range (willplay / wontplay) for every opponent.
OH_TABLE = """AA KK AKs QQ AK JJ AQs TT AQ 99 AJs 88 ATs AJ 77 66 AT A9s 55 A8s KQs 44 A9 A7s KJs A5s A8 A6s A4s 33
KTs A7 A3s KQ A2s A5 A6 A4 KJ QJs A3 22 K9s A2 KT QTs K8s K7s JTs K9 K6s QJ Q9s K5s K8 K4s QT K7 K3s K2s Q8s K6 J9s
K5 Q9 JT K4 Q7s T9s Q6s K3 J8s Q5s K2 Q8 Q4s J9 Q3s T8s J7s Q7 Q2s Q6 98s Q5 J8 T9 J6s T7s J5s Q4 J4s J7 Q3 97s T8
J3s T6s Q2 J2s 87s J6 98 T7 96s J5 T5s T4s 86s J4 T6 97 T3s 76s 95s J3 T2s 87 85s 96 T5 J2 75s 94s T4 65s 86 93s
84s 95 T3 76 92s 74s 54s T2 85 64s 83s 94 75 82s 73s 93 65 53s 63s 84 92 43s 74 72s 54 64 52s 62s 83 42s 82 73 53
63 32s 43 72 52 62 42 32""".split()
assert len(OH_TABLE) == 169


def _oh_rank():
    pos = {h: i + 1 for i, h in enumerate(OH_TABLE)}
    out = []
    for c in COMBOS:
        k = _class(c)
        k = k[:2] if (k[0] == k[1] or k.endswith("o")) else k
        out.append(pos[k])
    return out


OH_RANK = _oh_rank()                    # 1..169 per combination
_OH_CUM = []
for _i in range(169):
    _OH_CUM.append(sum(1 for r in OH_RANK if r <= _i + 1))


def oh_classes(share):
    """Number of hand classes (OpenHoldem willplay/wontplay units) holding `share` of the 1326 combinations."""
    target = share * 1326
    for i, c in enumerate(_OH_CUM):
        if c >= target:
            return i + 1
    return 169


def oh_weights(willplay, wontplay, nopp, dead=()):
    """Weight of every combination in OpenHoldem's weighted prwin (IsHandInWeightedRange, topclip/mustplay unused):
    1 up to willplay, then falling linearly to 0 at wontplay (the random acceptance, as an expected weight)."""
    if willplay and willplay < 2 * nopp + 1:          # OH protection against low willplay
        willplay = 2 * nopp + 1
    if wontplay < willplay:
        wontplay = willplay
    d = wontplay - willplay
    dead = set(dead)
    w = []
    for c, hr in zip(COMBOS, OH_RANK):
        if c[0] in dead or c[1] in dead:
            w.append(0.0)
        elif not willplay:
            w.append(1.0)
        elif hr >= wontplay:
            w.append(0.0)
        elif hr <= willplay or d < 3:
            w.append(1.0)
        else:
            w.append((wontplay - hr) / d)
    return w
