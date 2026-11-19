"""Hand histories in PokerStars format (cash NLHE), importable in PokerTracker 4.

    text = export_hand(table, result, hand_id, table_name, hero=None, when=None)

Amounts are written in $ with the table's chip values (bb = 1.0 -> $0.50/$1.00).
`hero` gets the "Dealt to" line (PokerStars writes only the hero's cards).
"""
import datetime

from . import handeval as he
from .engine import PREFLOP, FLOP, TURN, RIVER

RANK_CH = {r: c for r, c in zip(range(2, 15), "23456789TJQKA")}
SUIT_CH = "cdhs"
NAME = {2: "Deuce", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight",
        9: "Nine", 10: "Ten", 11: "Jack", 12: "Queen", 13: "King", 14: "Ace"}
PLURAL = {2: "Deuces", 3: "Threes", 4: "Fours", 5: "Fives", 6: "Sixes", 7: "Sevens", 8: "Eights",
          9: "Nines", 10: "Tens", 11: "Jacks", 12: "Queens", 13: "Kings", 14: "Aces"}
STREET_NAME = {PREFLOP: "Flop", FLOP: "Flop", TURN: "Turn", RIVER: "River"}
EPOCH = datetime.datetime(2026, 1, 1, 12, 0, 0)


def card(c):
    return RANK_CH[c[0]] + SUIT_CH[c[1]]


def cards(cs):
    return " ".join(card(c) for c in cs)


def money(x):
    x = round(x + 1e-9, 2)
    return f"${x:.0f}" if abs(x - round(x)) < 1e-9 else f"${x:.2f}"


def best5(t, hole):
    """Cards that make the hand: all of them in Hold'em, exactly 2 + 3 in Omaha."""
    if getattr(t, "game", "nlhe") == "plo":
        return he.omaha_best(hole, t.board)
    return list(hole) + list(t.board)


def describe(cs):
    """Hand description as written by PokerStars at showdown."""
    cat, r = he.evaluate(cs)
    if cat == he.HIGHCARD:
        return f"high card {NAME[r[0]]}"
    if cat == he.PAIR:
        return f"a pair of {PLURAL[r[0]]}"
    if cat == he.TWOPAIR:
        return f"two pair, {PLURAL[r[0]]} and {PLURAL[r[1]]}"
    if cat == he.TRIPS:
        return f"three of a kind, {PLURAL[r[0]]}"
    if cat in (he.STRAIGHT, he.STFLUSH):
        hi = r[0]
        lo = "Ace" if hi == 5 else NAME[hi - 4]
        if cat == he.STFLUSH:
            return "a Royal Flush" if hi == 14 else f"a straight flush, {lo} to {NAME[hi]}"
        return f"a straight, {lo} to {NAME[hi]}"
    if cat == he.FLUSH:
        return f"a flush, {NAME[r[0]]} high"
    if cat == he.FULLHOUSE:
        return f"a full house, {PLURAL[r[0]]} full of {PLURAL[r[1]]}"
    return f"four of a kind, {PLURAL[r[0]]}"


def export_hand(t, result, hand_id, table_name="ohsim", hero=None, when=None):
    seats = t.seats
    seatno = {s.chair: s.chair + 1 for s in seats}
    when = when or EPOCH + datetime.timedelta(seconds=40 * t.hand_no)
    game = "Omaha Pot Limit" if getattr(t, "game", "nlhe") == "plo" else "Hold'em No Limit"
    L = [f"PokerStars Hand #{hand_id}:  {game} (${t.sb:.2f}/${t.bb:.2f} USD)"
         f" - {when:%Y/%m/%d %H:%M:%S} ET",
         f"Table '{table_name}' {t.n}-max Seat #{seatno[t.dealer]} is the button"]
    for s in seats:
        L.append(f"Seat {seatno[s.chair]}: {s.name} ({money(s.start_stack)} in chips)")

    # uncalled bet: the part of the largest investment nobody matched
    inv = sorted((s.invested for s in seats), reverse=True)
    top = max(seats, key=lambda s: s.invested)
    uncalled = round(inv[0] - inv[1], 2) if len(inv) > 1 else 0.0
    unc_street = None
    if uncalled > 0:
        aggr = [h for h in t.history if h.chair == top.chair and h.kind in ("raise", "bet", "sb", "bb")]
        unc_street = aggr[-1].street if aggr else PREFLOP

    folded_on = {}
    hist = t.history
    for street in (PREFLOP, FLOP, TURN, RIVER):
        if street == FLOP and len(t.board) >= 3:
            L.append(f"*** FLOP *** [{cards(t.board[:3])}]")
        elif street == TURN and len(t.board) >= 4:
            L.append(f"*** TURN *** [{cards(t.board[:3])}] [{card(t.board[3])}]")
        elif street == RIVER and len(t.board) >= 5:
            L.append(f"*** RIVER *** [{cards(t.board[:4])}] [{card(t.board[4])}]")
        elif street != PREFLOP:
            break
        street_max = 0.0
        for h in (h for h in hist if h.street == street):
            nm = seats[h.chair].name
            allin = " and is all-in" if h.allin else ""
            if h.kind == "ante":
                L.append(f"{nm}: posts the ante {money(h.added)}{allin}")
                continue
            if h.kind == "sb":
                L.append(f"{nm}: posts small blind {money(h.added)}{allin}")
            elif h.kind == "bb":
                L.append(f"{nm}: posts big blind {money(h.added)}{allin}")
            elif h.kind == "fold":
                L.append(f"{nm}: folds")
                folded_on[h.chair] = street
            elif h.kind == "check":
                L.append(f"{nm}: checks")
            elif h.kind == "call":
                L.append(f"{nm}: calls {money(h.added)}{allin}")
            elif h.kind == "bet":
                L.append(f"{nm}: bets {money(h.to)}{allin}")
            elif h.kind == "raise":
                L.append(f"{nm}: raises {money(h.to - street_max)} to {money(h.to)}{allin}")
            street_max = max(street_max, h.to)
            if street == PREFLOP and h.kind == "bb":
                L.append("*** HOLE CARDS ***")
                if hero:
                    for s in seats:
                        if s.name == hero:
                            L.append(f"Dealt to {s.name} [{cards(s.hole)}]")
        if street == PREFLOP and not any(h.kind == "bb" for h in hist):
            L.append("*** HOLE CARDS ***")
        if unc_street == street:
            L.append(f"Uncalled bet ({money(uncalled)}) returned to {top.name}")

    payouts = dict(result["payouts"])
    if uncalled > 0:
        payouts[top.chair] = round(payouts[top.chair] - uncalled, 2)
    active = [s for s in seats if s.in_hand and not s.folded]
    showdown = result.get("showdown") and len(active) > 1
    if showdown:
        L.append("*** SHOW DOWN ***")
        for s in active:
            L.append(f"{s.name}: shows [{cards(s.hole)}] ({describe(best5(t, s.hole))})")
    for s in seats:
        if payouts[s.chair] > 0:
            L.append(f"{s.name} collected {money(payouts[s.chair])} from pot")
    if not showdown and active:
        L.append(f"{active[0].name}: doesn't show hand")

    pot = round(sum(s.invested for s in seats) - uncalled, 2)
    L.append("*** SUMMARY ***")
    L.append(f"Total pot {money(pot)} | Rake {money(getattr(t, 'last_rake', 0.0))}")
    if t.board:
        L.append(f"Board [{cards(t.board)}]")
    for s in seats:
        tag = " (button)" if s.chair == t.dealer else (
            " (small blind)" if s.chair == t.sb_chair else (" (big blind)" if s.chair == t.bb_chair else ""))
        head = f"Seat {seatno[s.chair]}: {s.name}{tag}"
        if s.folded:
            st = folded_on.get(s.chair, PREFLOP)
            where = "before Flop" if st == PREFLOP else f"on the {STREET_NAME[st]}"
            didnt = " (didn't bet)" if s.invested == 0 else ""
            L.append(f"{head} folded {where}{didnt}")
        elif showdown:
            d = describe(best5(t, s.hole))
            if payouts[s.chair] > 0:
                L.append(f"{head} showed [{cards(s.hole)}] and won ({money(payouts[s.chair])}) with {d}")
            else:
                L.append(f"{head} showed [{cards(s.hole)}] and lost with {d}")
        else:
            L.append(f"{head} collected ({money(payouts[s.chair])})")
    return "\n".join(L) + "\n\n\n"
