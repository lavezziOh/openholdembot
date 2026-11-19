"""Neutral hand representation shared by the PokerStars hand-history parser and the
simulator, used to compute PokerTracker-style per-hand columns (ohsim.ptrows).

HandLog
    players   list of PlayerLog in seat order (only players dealt in)
    button    seat number of the button
    sb, bb    blind sizes
    actions   list of Act in order (blinds/antes included)
    board     list of card strings ("Ah")
    collected {name: amount won from the pot(s)}
    uncalled  {name: amount returned}
"""
import re
from dataclasses import dataclass, field

PREFLOP, FLOP, TURN, RIVER = 1, 2, 3, 4


@dataclass
class PlayerLog:
    name: str
    seat: int
    stack: float
    hole: list = field(default_factory=list)


@dataclass
class Act:
    street: int
    name: str
    kind: str            # ante sb bb post fold check call bet raise
    amount: float = 0.0  # chips put in by this action
    to: float = 0.0      # for bet/raise: street total after the action
    allin: bool = False


@dataclass
class HandLog:
    hand_id: str
    players: list
    button: int
    sb: float
    bb: float
    actions: list
    board: list = field(default_factory=list)
    collected: dict = field(default_factory=dict)
    uncalled: dict = field(default_factory=dict)
    max_seats: int = 6

    def player(self, name):
        return next(p for p in self.players if p.name == name)


# ---------------------------------------------------------------- PokerStars parser
_NUM = r"[$€£]?([0-9][0-9,]*(?:\.[0-9]+)?)"
_RE_HEAD = re.compile(r"PokerStars (?:Zoom |Home Game )?Hand #(\d+):.*?\(" + _NUM + r"/" + _NUM)
_RE_TABLE = re.compile(r"Table '.*?' (\d+)-max (?:\(Play Money\) )?Seat #(\d+) is the button")
_RE_SEAT = re.compile(r"Seat (\d+): (.+?) \(" + _NUM + r" in chips")


def _n(s):
    return float(s.replace(",", ""))


def parse_pokerstars(text):
    """Parse one PokerStars cash hand. Returns HandLog or None (unsupported hand)."""
    lines = [l.rstrip("\r") for l in text.strip().splitlines()]
    m = _RE_HEAD.search(lines[0]) if lines else None
    if not m:
        return None
    hand_id, sb, bb = m.group(1), _n(m.group(2)), _n(m.group(3))
    m = _RE_TABLE.search(lines[1])
    if not m:
        return None
    max_seats, button = int(m.group(1)), int(m.group(2))
    seats = []
    i = 2
    while i < len(lines) and lines[i].startswith("Seat "):
        m = _RE_SEAT.match(lines[i])
        if m and "sitting out" not in lines[i] and "out of hand" not in lines[i]:
            seats.append(PlayerLog(m.group(2), int(m.group(1)), _n(m.group(3))))
        i += 1
    names = sorted((p.name for p in seats), key=len, reverse=True)
    street = PREFLOP
    acts, board = [], []
    collected, uncalled, dealt_in = {}, {}, set()
    street_bet = {}
    summary = False
    for line in lines[i:]:
        if line.startswith("*** SUMMARY"):
            summary = True
        if summary:
            continue
        if line.startswith("*** "):
            if line.startswith("*** SECOND "):        # "run it twice": keep the first board
                continue
            head = line.replace("*** FIRST ", "*** ")
            if head.startswith("*** FLOP"):
                street = FLOP
            elif head.startswith("*** TURN"):
                street = TURN
            elif head.startswith("*** RIVER"):
                street = RIVER
            if head.startswith(("*** FLOP", "*** TURN", "*** RIVER")):
                board = re.findall(r"\b([2-9TJQKA][cdhs])\b", line)
                street_bet = {}                     # new betting round (not at HOLE CARDS!)
            continue
        m = re.match(r"Uncalled bet \(" + _NUM + r"\) returned to (.+)$", line)
        if m:
            uncalled[m.group(2)] = uncalled.get(m.group(2), 0) + _n(m.group(1))
            continue
        name = next((n for n in names if line.startswith(n + ": ") or line.startswith(n + " collected ")
                     or line.startswith(n + " cashed out")), None)
        if name is None:
            continue
        rest = line[len(name):]
        m = re.match(r" (?:collected|cashed out the hand for) " + _NUM, rest)
        if m:
            collected[name] = collected.get(name, 0) + _n(m.group(1))
            continue
        rest = rest[2:]
        allin = rest.endswith("and is all-in")
        m = re.match(r"posts (small blind|big blind|small & big blinds|the ante) " + _NUM, rest)
        if m:
            kind = {"small blind": "sb", "big blind": "bb", "small & big blinds": "post",
                    "the ante": "ante"}[m.group(1)]
            if kind == "bb" and any(a.kind == "bb" for a in acts):
                kind = "post"                 # extra big blind posted by a new player
            x = _n(m.group(2))
            if kind != "ante":
                street_bet[name] = street_bet.get(name, 0) + (bb if kind == "post" else x)
            acts.append(Act(PREFLOP, name, kind, x, street_bet.get(name, 0), allin))
            dealt_in.add(name)
            continue
        if rest.startswith("folds"):
            acts.append(Act(street, name, "fold"))
        elif rest.startswith("checks"):
            acts.append(Act(street, name, "check"))
        elif rest.startswith("calls"):
            x = _n(re.match(r"calls " + _NUM, rest).group(1))
            street_bet[name] = street_bet.get(name, 0) + x
            acts.append(Act(street, name, "call", x, street_bet[name], allin))
        elif rest.startswith("bets"):
            x = _n(re.match(r"bets " + _NUM, rest).group(1))
            street_bet[name] = street_bet.get(name, 0) + x
            acts.append(Act(street, name, "bet", x, street_bet[name], allin))
        elif rest.startswith("raises"):
            m = re.match(r"raises " + _NUM + " to " + _NUM, rest)
            to = _n(m.group(2))
            x = to - street_bet.get(name, 0)
            street_bet[name] = to
            acts.append(Act(street, name, "raise", x, to, allin))
        elif rest.startswith("shows ["):
            p = next(p for p in seats if p.name == name)
            p.hole = re.findall(r"([2-9TJQKA][cdhs])", rest.split("]")[0])
        elif rest.startswith("is sitting out") or rest.startswith("sits out"):
            continue
    for line in lines:
        m = re.match(r"Dealt to (.+?) \[([^\]]+)\]", line)
        if m:
            for p in seats:
                if p.name == m.group(1):
                    p.hole = m.group(2).split()
    players = [p for p in seats]
    return HandLog(hand_id, players, button, sb, bb, acts, board, collected, uncalled, max_seats)


# ---------------------------------------------------------------- simulator adapter
_KIND = {"sb": "sb", "bb": "bb", "ante": "ante", "fold": "fold", "check": "check",
         "call": "call", "bet": "bet", "raise": "raise"}
_RANK = "23456789TJQKA"
_SUIT = "cdhs"


def from_table(t, result, hand_id=None):
    """HandLog of the hand just played on an engine.Table."""
    seats = t.seats
    players = [PlayerLog(s.name, s.chair + 1, s.start_stack,
                         [_RANK[c[0] - 2] + _SUIT[c[1]] for c in s.hole]) for s in seats]
    acts = []
    for h in t.history:
        acts.append(Act(h.street, seats[h.chair].name, _KIND[h.kind], h.added,
                        h.to, h.allin))
    inv = sorted((s.invested for s in seats), reverse=True)
    uncalled = {}
    top = max(seats, key=lambda s: s.invested)
    if len(inv) > 1 and inv[0] - inv[1] > 1e-9:
        uncalled[top.name] = round(inv[0] - inv[1], 2)
    collected = {}
    for s in seats:
        x = result["payouts"][s.chair] - uncalled.get(s.name, 0.0)
        if x > 1e-9:
            collected[s.name] = round(x, 2)
    board = [_RANK[c[0] - 2] + _SUIT[c[1]] for c in t.board]
    return HandLog(str(hand_id or t.hand_no), players, t.dealer + 1, t.sb, t.bb, acts, board,
                   collected, uncalled, t.n)
