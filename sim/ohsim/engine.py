"""No-Limit Hold'em cash-game engine (2..9 seats).

Chairs are numbered 0..n-1 clockwise. Amounts are floats in chips.
Players implement:
    start_hand(view), new_round(view), decide(view) -> Action,
    after_action(view, action), end_hand(view, result)
"""
import math
import random
from dataclasses import dataclass, field

from .handeval import score, omaha_score

PREFLOP, FLOP, TURN, RIVER = 1, 2, 3, 4
FOLD, CHECK, CALL, RAISE = "fold", "check", "call", "raise"   # RAISE = bet or raise TO amount


@dataclass
class Action:
    kind: str
    amount: float = 0.0          # for RAISE: total bet of this street ("raise to")
    note: str = ""               # free text, e.g. the OpenPPL decision


@dataclass
class ActionRecord:
    street: int
    chair: int
    kind: str                    # fold/check/call/bet/raise/allin-call/allin-raise, blinds: sb/bb/ante
    to: float                    # street bet after the action
    added: float                 # chips put in by this action
    allin: bool = False


@dataclass
class Seat:
    chair: int
    name: str
    player: object
    stack: float
    hole: list = field(default_factory=list)
    in_hand: bool = False
    folded: bool = False
    allin: bool = False
    bet: float = 0.0             # current street
    invested: float = 0.0        # whole hand
    acted: bool = False          # acted since the last full raise
    start_stack: float = 0.0     # stack at hand start
    sitting_out: bool = False    # temporarily away from the table


class Table:
    def __init__(self, players, names=None, sb=0.5, bb=1.0, ante=0.0,
                 buyin=100.0, rebuy_below=None, seed=None, unit=0.01, max_stack=None,
                 rake=0.0, rake_cap=0.0, game="nlhe"):
        self.n = len(players)
        # "nlhe" = No-Limit Hold'em (2 hole cards); "plo" = Pot-Limit Omaha (4 hole cards,
        # best hand = exactly 2 hole cards + 3 board cards, maximum bet = the pot)
        assert game in ("nlhe", "plo"), game
        self.game = game
        self.n_hole = 4 if game == "plo" else 2
        self.max_stack = max_stack   # stack above which a player re-sits with `buyin` (None = no cap)
        self.unit = unit          # smallest chip (cent): bets and pot splits are rounded to it
        assert 2 <= self.n <= 10
        self.sb, self.bb, self.ante = sb, bb, ante
        self.buyin = buyin
        self.rebuy_below = buyin if rebuy_below is None else rebuy_below
        self.rng = random.Random(seed)
        names = names or [f"P{i}" for i in range(self.n)]
        self.seats = [Seat(i, names[i], p, buyin) for i, p in enumerate(players)]
        self.dealer = self.rng.randrange(self.n)
        self.hand_no = 0
        self.history = []         # list of ActionRecord for the current hand
        self.board = []
        self.street = PREFLOP
        self.results = {s.name: 0.0 for s in self.seats}   # net won per player (chips)
        self.rebuys = {s.name: 0.0 for s in self.seats}    # chips added by top-ups
        self.hands_played = 0
        # rake: share of the called pot, capped, only when a flop is dealt (no flop no drop)
        self.rake, self.rake_cap = rake, rake_cap
        self.rake_total = 0.0

    def shuffle_seats(self, rng):
        """New random seating (the button stays on its chair, the players move).
        Called at the end of every orbit: no player keeps the same neighbours for the whole run,
        so a result does not depend on sitting right after a weak (or strong) player.
        Results, stats and hand histories are keyed by name, not by chair."""
        order = list(range(self.n))
        rng.shuffle(order)
        self.seats = [self.seats[i] for i in order]
        for c, s in enumerate(self.seats):
            s.chair = c

    # -- helpers ------------------------------------------------------------
    def next_chair(self, c, cond=lambda s: True):
        for i in range(1, self.n + 1):
            j = (c + i) % self.n
            if not self.seats[j].sitting_out and cond(self.seats[j]):
                return j
        return None

    @property
    def n_active_seats(self):
        return sum(1 for s in self.seats if not s.sitting_out)

    def active(self):
        return [s for s in self.seats if s.in_hand and not s.folded]

    def can_act(self):
        return [s for s in self.active() if not s.allin]

    @property
    def pot(self):
        return sum(s.invested for s in self.seats)

    def current_bet(self):
        return max((s.bet for s in self.seats if s.in_hand), default=0.0)

    # -- one hand -----------------------------------------------------------
    def play_hand(self, deck=None):
        """`deck` (optional): the cards in dealing order (hole cards seat by seat, then burn/flop/burn/turn/
        burn/river), e.g. to replay a hand with another board; by default a shuffled deck."""
        self.hand_no += 1
        for s in self.seats:                       # cash game: top up
            if s.stack < self.rebuy_below - 1e-9:
                self.rebuys[s.name] += self.buyin - s.stack
                s.stack = self.buyin
            elif self.max_stack and s.stack > self.max_stack + 1e-9:
                # a big winner leaves and sits down again with a normal buy-in
                self.rebuys[s.name] -= s.stack - self.buyin
                s.stack = self.buyin
        self.dealer = self.next_chair(self.dealer)
        if deck is None:
            deck = [(r, su) for r in range(2, 15) for su in range(4)]
            self.rng.shuffle(deck)
        else:
            deck = list(reversed(deck))              # cards are taken with pop()
        self.board_deck = deck
        self.history = []
        self.board = []
        self.street = PREFLOP
        for s in self.seats:
            if s.sitting_out:
                s.hole = []
                s.in_hand, s.folded, s.allin = False, True, False
                s.bet = s.invested = 0.0
                s.acted = False
                s.start_stack = s.stack
                continue
            s.hole = [deck.pop() for _ in range(self.n_hole)]
            s.in_hand, s.folded, s.allin = True, False, False
            s.bet = s.invested = 0.0
            s.acted = False
            s.start_stack = s.stack
        self.deck = deck
        # blinds (heads-up: dealer posts the small blind)
        if self.n_active_seats == 2:
            self.sb_chair = self.dealer
        else:
            self.sb_chair = self.next_chair(self.dealer)
        self.bb_chair = self.next_chair(self.sb_chair)
        if self.ante > 0:
            for s in self.seats:
                if not s.sitting_out:
                    self._put(s, min(self.ante, s.stack), "ante", ante=True)
        self._put(self.seats[self.sb_chair], min(self.sb, self.seats[self.sb_chair].stack), "sb")
        self._put(self.seats[self.bb_chair], min(self.bb, self.seats[self.bb_chair].stack), "bb")
        self.last_raise = self.bb
        for s in self.seats:
            if not s.sitting_out:
                s.player.start_hand(self.view(s.chair))
        first = self.next_chair(self.bb_chair)
        if self._betting_round(first):
            for street, ncards in ((FLOP, 3), (TURN, 1), (RIVER, 1)):
                self.street = street
                self.deck.pop()                       # burn
                self.board += [self.deck.pop() for _ in range(ncards)]
                for s in self.seats:
                    s.bet = 0.0
                    s.acted = False
                self.last_raise = self.bb
                for s in self.active():
                    s.player.new_round(self.view(s.chair))
                first = self.next_chair(self.dealer, lambda x: x.in_hand and not x.folded)
                if len(self.can_act()) >= 2:
                    if not self._betting_round(first):
                        break
        self.board_seen_flop = self.street != PREFLOP
        result = self._showdown()
        for s in self.seats:
            s.player.end_hand(self.view(s.chair), result)
        self.hands_played += 1
        return result

    def hand_score(self, hole):
        return omaha_score(hole, self.board) if self.game == "plo" else score(hole + self.board)

    def _put(self, s, amount, kind, ante=False):
        amount = min(amount, s.stack)
        s.stack -= amount
        s.invested += amount
        if not ante:
            s.bet += amount
        if s.stack <= 1e-9:
            s.stack = 0.0
            s.allin = True
        self.history.append(ActionRecord(self.street, s.chair, kind, s.bet, amount, s.allin))

    def _betting_round(self, first):
        """Returns False if the hand is over (only one player left)."""
        c = first
        while True:
            if len(self.active()) == 1:
                return False
            to_act = [s for s in self.can_act()
                      if not s.acted or s.bet < self.current_bet() - 1e-9]
            if not to_act:
                return True
            s = self.seats[c]
            if s.in_hand and not s.folded and not s.allin and (
                    not s.acted or s.bet < self.current_bet() - 1e-9):
                self._ask(s)
            c = (c + 1) % self.n

    def legal(self, s):
        cb = self.current_bet()
        to_call = min(cb - s.bet, s.stack)
        # A full raise resets 'acted' of the others; a short all-in does not,
        # so players who already acted may only call or fold (no re-opening).
        can_raise = s.stack > to_call + 1e-9 and not s.acted
        min_to = cb + self.last_raise
        max_to = s.bet + s.stack
        if self.game == "plo":
            # pot limit: raise to at most current bet + (pot after calling)
            max_to = min(max_to, cb + self.pot + to_call)
        return to_call, can_raise, min(min_to, max_to), max_to

    def _ask(self, s):
        view = self.view(s.chair)
        a = s.player.decide(view)
        to_call, can_raise, min_to, max_to = self.legal(s)
        cb = self.current_bet()
        kind = a.kind
        if kind == RAISE and not can_raise:
            kind = CALL if to_call > 0 else CHECK
        if kind == CALL and to_call <= 1e-9:
            kind = CHECK
        if kind == CHECK and to_call > 1e-9:
            kind = FOLD
        if kind == FOLD and to_call <= 1e-9:
            kind = CHECK                       # never fold when checking is free
        # OpenHoldem runs UpdateMemorySymbolsAfterAutoplayerAction right after clicking, in the same
        # heartbeat: its own action counters (didrais, ...) are already updated, but the table is the
        # scraped frame BEFORE the action (our chips are not yet in front of us). The OpenPPL library
        # relies on that (e.g. "SET me_st_MemRaises_Raises"), so the bot is told before the table changes.
        s.player.after_action(self.view(s.chair), Action(kind, a.amount, a.note))
        if kind == FOLD:
            s.folded = True
            self.history.append(ActionRecord(self.street, s.chair, "fold", s.bet, 0.0))
        elif kind == CHECK:
            self.history.append(ActionRecord(self.street, s.chair, "check", s.bet, 0.0))
        elif kind == CALL:
            self._put(s, to_call, "call")
        else:
            target = round(round(a.amount / self.unit) * self.unit, 6) if self.unit else a.amount
            target = max(min_to, min(target, max_to))
            raise_size = target - cb
            full = raise_size >= self.last_raise - 1e-9
            self._put(s, target - s.bet, "raise" if cb > 0 else "bet")
            if full:
                self.last_raise = raise_size
                for o in self.seats:
                    if o is not s:
                        o.acted = False
        s.acted = True

    def _showdown(self):
        active = self.active()
        payouts = {s.chair: 0.0 for s in self.seats}
        if len(active) == 1:
            payouts[active[0].chair] = self.pot
            shown = False
        else:
            while len(self.board) < 5:
                self.board.append(self.deck.pop())
            shown = True
            scores = {s.chair: self.hand_score(s.hole) for s in active}
            levels = sorted({s.invested for s in self.seats if s.invested > 0})
            prev = 0.0
            for lvl in levels:
                contrib = sum(min(s.invested, lvl) - min(s.invested, prev) for s in self.seats)
                eligible = [s for s in active if s.invested >= lvl - 1e-9]
                if contrib <= 0:
                    prev = lvl
                    continue
                if not eligible:            # everybody eligible folded: give to best remaining
                    eligible = active
                best = max(scores[s.chair] for s in eligible)
                winners = [s for s in eligible if scores[s.chair] == best]
                # odd chip(s) to the first winner left of the button, as in real card rooms
                winners.sort(key=lambda s: (s.chair - self.dealer - 1) % self.n)
                if self.unit and len(winners) > 1:
                    share = math.floor(contrib / len(winners) / self.unit + 1e-9) * self.unit
                    rest = contrib - share * len(winners)
                else:
                    share, rest = contrib / len(winners), 0.0
                for k, w in enumerate(winners):
                    payouts[w.chair] += share + (rest if k == 0 else 0.0)
                prev = lvl
        self._take_rake(payouts)
        for s in self.seats:
            payouts[s.chair] = round(payouts[s.chair], 6)
            s.stack = round(s.stack + payouts[s.chair], 6)
            self.results[s.name] += payouts[s.chair] - s.invested
        return {"payouts": payouts, "showdown": shown, "board": list(self.board)}

    # -- view for players -------------------------------------------------
    def _take_rake(self, payouts):
        self.last_rake = 0.0                          # rake of this hand (for the hand history)
        if self.rake <= 0 or not self.board_seen_flop:
            return
        inv = sorted((s.invested for s in self.seats), reverse=True)
        uncalled = inv[0] - inv[1]                  # returned to the last bettor, not raked
        base = self.pot - uncalled
        rk = self.rake * base
        if self.rake_cap > 0:
            rk = min(rk, self.rake_cap)
        if self.unit:
            rk = math.floor(rk / self.unit + 1e-9) * self.unit
        if rk <= 0:
            return
        top = max(self.seats, key=lambda s: s.invested)
        won = {c: v - (uncalled if c == top.chair else 0.0) for c, v in payouts.items()}
        tot = sum(v for v in won.values() if v > 0)
        if tot <= 0:
            return
        for c, v in won.items():
            if v > 0:
                payouts[c] -= rk * v / tot
        self.rake_total += rk
        self.last_rake = rk

    def view(self, chair):
        return TableView(self, chair)


class TableView:
    """Read-only view of the table from one chair (what a screen-scraper would see)."""
    __slots__ = ("t", "chair")

    def __init__(self, table, chair):
        self.t = table
        self.chair = chair

    @property
    def me(self):
        return self.t.seats[self.chair]
