"""PokerTracker-like statistics collected from the simulated hands.

Symbols: pt_<stat><chair>, e.g. pt_vpip3, pt_pfr_raischair (multiplexed to a chair).
Values are fractions 0..1 (hands: number of hands). Without data: 0.
"""
from collections import defaultdict

from .engine import PREFLOP, FLOP


class PlayerStats:
    def __init__(self):
        self.c = defaultdict(lambda: defaultdict(float))   # player name -> counter -> value

    def update(self, table, result=None):
        hist = table.history
        pre = [r for r in hist if r.street == PREFLOP and r.kind not in ("ante", "sb", "bb")]
        raises_before = 0
        first_raiser = None
        pfr_chair = None
        for r in pre:
            if r.kind in ("raise", "bet"):
                raises_before += 1
                if first_raiser is None:
                    first_raiser = r.chair
                pfr_chair = r.chair
        active = [s for s in table.seats if s.in_hand and not s.folded]
        showdown = len(active) > 1
        winners = {k for k, v in (result or {}).get("payouts", {}).items() if v > 0}
        for s in table.seats:
            c = self.c[s.name]
            c["hands"] += 1
            mine = [r for r in pre if r.chair == s.chair]
            if any(r.kind in ("call", "raise", "bet") for r in mine):
                c["vpip"] += 1
            if any(r.kind in ("raise", "bet") for r in mine):
                c["pfr"] += 1
            # 3bet: raise when exactly one raise happened before
            seen = 0
            for r in pre:
                if r.chair == s.chair and r.kind == "raise" and seen == 1:
                    c["3bet"] += 1
                    break
                if r.kind in ("raise", "bet"):
                    seen += 1
            for r in pre:
                if r.chair == s.chair:
                    if seen_raise_before(pre, r) == 1:
                        c["3bet_opp"] += 1
                    break
            # steal attempt: first raise from CO/BTN/SB when folded to
            if s.chair in (table.dealer, table.sb_chair) and first_raiser == s.chair:
                c["steal_attempt"] += 1
            # fold to flop c-bet
            flop = [r for r in hist if r.street == FLOP]
            if pfr_chair is not None and pfr_chair != s.chair:
                cbet = next((r for r in flop if r.kind == "bet"), None)
                if cbet is not None and cbet.chair == pfr_chair:
                    after = [r for r in flop[flop.index(cbet) + 1:] if r.chair == s.chair]
                    if after:
                        c["flop_fold_to_cbet_opp"] += 1
                        if after[0].kind == "fold":
                            c["flop_fold_to_cbet"] += 1
            # flop c-bet: preflop raiser, no donk bet before his first flop action
            if pfr_chair == s.chair:
                mine = [r for r in flop if r.chair == s.chair]
                if mine:
                    before = flop[:flop.index(mine[0])]
                    if not any(r.kind in ("bet", "raise") for r in before):
                        c["flop_cbet_opp"] += 1
                        if mine[0].kind == "bet":
                            c["flop_cbet"] += 1
            # postflop aggression: (bets + raises) / calls
            for r in hist:
                if r.street >= FLOP and r.chair == s.chair:
                    if r.kind in ("bet", "raise"):
                        c["post_aggr"] += 1
                    elif r.kind == "call":
                        c["post_call"] += 1
            # went to showdown (PokerTracker: saw the flop and reached a real showdown)
            saw_flop = any(r.street >= FLOP and r.chair == s.chair for r in hist) or (
                s.in_hand and not s.folded and table.board)
            if saw_flop:
                c["saw_flop"] += 1
                if not s.folded and showdown:
                    c["wtsd"] += 1
                    if s.chair in winners:
                        c["wsd"] += 1

    def value(self, player, stat):
        c = self.c.get(player)
        if not c or c["hands"] == 0:
            return 0.0
        if stat == "hands":
            return c["hands"]
        if stat == "3bet":
            return c["3bet"] / c["3bet_opp"] if c["3bet_opp"] else 0.0
        if stat == "flop_fold_to_cbet":
            return c["flop_fold_to_cbet"] / c["flop_fold_to_cbet_opp"] if c["flop_fold_to_cbet_opp"] else 0.0
        if stat == "wtsd":
            return c["wtsd"] / c["saw_flop"] if c["saw_flop"] else 0.0
        if stat == "flop_cbet":
            return c["flop_cbet"] / c["flop_cbet_opp"] if c["flop_cbet_opp"] else 0.0
        if stat == "af":
            return c["post_aggr"] / c["post_call"] if c["post_call"] else 0.0
        if stat in c:
            return c[stat] / c["hands"]
        return 0.0

    def symbol(self, name, view):
        body = name[3:]
        i = len(body)
        while i > 0 and body[i - 1].isdigit():
            i -= 1
        stat, chair = body[:i], body[i:]
        if not chair:
            raise KeyError(name)
        chair = int(chair)
        if chair >= view.t.n:
            return 0.0
        return self.value(view.t.seats[chair].name, stat)


def seen_raise_before(pre, rec):
    n = 0
    for r in pre:
        if r is rec:
            return n
        if r.kind in ("raise", "bet"):
            n += 1
    return n
