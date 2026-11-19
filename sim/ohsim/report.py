"""Preflop statistics by position, computed from the table history of each hand.

Keys: (player, position, stat) -> count. Stats:
  rfi_opp / rfi            first in (nobody raised or called before): opened
  vo_opp / vo_fold / vo_call / vo_3bet   facing a single open, no callers, first action
  v3_opp / v3_fold / v3_call / v3_4bet   opener facing a 3bet (no callers)
"""
from collections import Counter

from .engine import PREFLOP

POS6 = ["SB", "BB", "UTG", "HJ", "CO", "BTN"]


def positions(table):
    n = table.n
    if n != 6:
        return {}
    return {(table.sb_chair + k) % n: POS6[k] for k in range(6)}


def preflop_stats(table):
    pos = positions(table)
    c = Counter()
    if not pos:
        return c
    acts = [r for r in table.history if r.street == PREFLOP and r.kind not in ("ante", "sb", "bb")]
    raises = calls = 0
    first_action = {}
    opener = None
    for r in acts:
        name = table.seats[r.chair].name
        p = pos[r.chair]
        aggressive = r.kind in ("raise", "bet")
        if r.chair not in first_action:
            first_action[r.chair] = True
            if raises == 0 and calls == 0:
                c[(name, p, "rfi_opp")] += 1
                if aggressive:
                    c[(name, p, "rfi")] += 1
                    opener = r.chair
            elif raises == 1 and calls == 0:
                c[(name, p, "vo_opp")] += 1
                c[(name, p, "vo_" + ("3bet" if aggressive else r.kind))] += 1
        elif r.chair == opener and raises == 2 and calls == 0:
            c[(name, p, "v3_opp")] += 1
            c[(name, p, "v3_" + ("4bet" if aggressive else r.kind))] += 1
            opener = None
        if aggressive:
            raises += 1
        elif r.kind == "call":
            calls += 1
    return c


def format_report(c, names, out=print):
    def pct(a, b):
        return f"{100 * a / b:5.1f}" if b else "    -"
    out("\nPreflop by position (6-handed hands only)")
    out(f"{'player':<12}{'pos':<5}{'RFI%':>7}{'n':>8} | {'vs open: fold':>14}{'call':>7}{'3bet':>7}{'n':>8}"
        f" | {'vs 3bet: fold':>14}{'call':>7}{'4bet':>7}{'n':>7}")
    for name in names:
        for p in ["UTG", "HJ", "CO", "BTN", "SB", "BB"]:
            g = lambda s: c.get((name, p, s), 0)
            out(f"{name:<12}{p:<5}{pct(g('rfi'), g('rfi_opp')):>7}{g('rfi_opp'):>8} | "
                f"{pct(g('vo_fold'), g('vo_opp')):>14}{pct(g('vo_call'), g('vo_opp')):>7}"
                f"{pct(g('vo_3bet'), g('vo_opp')):>7}{g('vo_opp'):>8} | "
                f"{pct(g('v3_fold'), g('v3_opp')):>14}{pct(g('v3_call'), g('v3_opp')):>7}"
                f"{pct(g('v3_4bet'), g('v3_opp')):>7}{g('v3_opp'):>7}")


def line_stats(table, result):
    """(player, position, preflop line) -> [hands, net chips]. Line = own preflop actions
    (open / 3bet / 4bet / call ...) + whether the hand went to the flop."""
    pos = positions(table)
    out = {}
    if not pos:
        return out
    acts = [r for r in table.history if r.street == PREFLOP and r.kind not in ("ante", "sb", "bb")]
    saw_flop = any(r.street != PREFLOP for r in table.history) or result.get("showdown")
    raises = 0
    line = {s.chair: [] for s in table.seats}
    for r in acts:
        if r.kind in ("raise", "bet"):
            raises += 1
            line[r.chair].append(["open", "3bet", "4bet", "5bet+"][min(raises, 4) - 1])
        elif r.kind == "call":
            line[r.chair].append(["limp", "call", "call3b", "call4b", "call5b+"][min(raises, 4)])
        elif r.kind == "fold":
            line[r.chair].append("fold")
    for s in table.seats:
        acts_s = line[s.chair]
        if not acts_s:
            key = "walk/uncontested"
        else:
            key = "-".join(acts_s)
            if acts_s[-1] != "fold":
                key += " +flop" if saw_flop else ""
        net = result["payouts"][s.chair] - s.invested
        out[(s.name, pos[s.chair], key)] = (1, net)
    return out


def format_lines(c, names, bb, hands_by_name, out=print, top=40):
    out("\nResult by preflop line (bb/100 = contribution to the player's total win rate)")
    keys = sorted({k for (n, p, k) in c})
    for name in names:
        rows = {}
        for (n, p, k), (cnt, net) in c.items():
            if n == name:
                r = rows.setdefault(k, [0, 0.0])
                r[0] += cnt
                r[1] += net
        out(f"  {name}")
        for k, (cnt, net) in sorted(rows.items(), key=lambda kv: kv[1][1])[:top]:
            out(f"    {k:<28}{cnt:>9} hands {net / bb / cnt:>+8.2f} bb/hand {100 * net / bb / hands_by_name[name]:>+8.2f} bb/100")
