"""OpenHoldem built-in symbols computed from the simulator state.

One SymbolProvider per bot. `bind(view)` before each evaluation; values are
cached per decision. Unknown / not yet implemented symbols raise KeyError and
are counted in `missing` (to drive further implementation).

Definitions follow OpenHoldem's symbol engines; amounts are in chips
("dollars" in OpenHoldem terms), counts of bets are in big blinds (NL: bet = bblind).
"""
import json
import os
import random
from collections import Counter

from . import handeval as he
from .catalog import is_oh_symbol
from .engine import PREFLOP, FLOP, TURN, RIVER

with open(os.path.join(os.path.dirname(__file__), "handrank.json")) as _f:
    HANDRANK = json.load(_f)       # OpenHoldem handrank tables, per number of opponents

RANKS = {"ace": 14, "king": 13, "queen": 12, "jack": 11, "ten": 10, "nine": 9, "eight": 8,
         "seven": 7, "six": 6, "five": 5, "four": 4, "three": 3, "two": 2, "deuce": 2}
SUITS = {"clubs": 0, "diamonds": 1, "hearts": 2, "spades": 3}
PREVACTION = {"fold": -1, "check": 0, "call": 1, "raise": 2, "betsize": 3, "betpot": 3, "allin": 4}
POKERVAL_CONST = {"hicard": 0x00000001, "onepair": 0x01000000, "twopair": 0x02000000,
                  "threeofakind": 0x04000000, "straight": 0x08000000, "flush": 0x10000000,
                  "fullhouse": 0x20000000, "fourofakind": 0x40000000,
                  "straightflush": 0x80000000, "royalflush": 0x800EDCBA}


def _bits(chairs):
    b = 0
    for c in chairs:
        b |= 1 << c
    return b


def _rankbits(cards):
    b = 0
    for r, _ in cards:
        b |= 1 << r
        if r == 14:
            b |= 1 << 1
    return b


def _std_card(card):
    r, s = card
    return s * 13 + (r - 2)


class SymbolProvider:
    def __init__(self, prwin_iterations=400, seed=None, stats=None):
        self.view = None
        self.cache = {}
        self.missing = Counter()
        self.prwin_iterations = prwin_iterations
        self.rng = random.Random(seed)
        self.prevaction = -2
        self.own = {}
        self.hands_played = 0
        self.stats = stats              # shared PlayerStats (for pt_ symbols), optional
        self.prwin_opponents_fn = None  # callable returning f$prwin_number_of_opponents
        self.range_model = None         # ohsim.ranges.PlayerModel shared by the table (dll$prwin_range)
        self._prwin_cache = {}
        self.random_hand = self.rng.random()
        self.random_round = self.rng.random()

    # -- runtime interface ----------------------------------------------------
    def bind(self, view):
        self.view = view
        self.cache.clear()
        self.random_heartbeat = self.rng.random()     # OpenHoldem: new value every heartbeat

    def known(self, name):
        return is_oh_symbol(name)

    def hole_cards(self):
        return list(self.view.me.hole) if self.view else []

    def board_cards(self):
        return list(self.view.t.board) if self.view else []

    def symbol(self, name):
        if name == "Random":
            # OpenHoldem: built-in, NOT cached, integer 0..99 (CSymbolEngineRandom, Random(100))
            return float(int(self.rng.random() * 100))
        if name == "random":
            return self.rng.random()                 # not cached either
        v = self.cache.get(name)
        if v is not None:
            return v
        v = self._compute(name)
        self.cache[name] = v
        return v

    # -- events -----------------------------------------------------------
    def on_hand_start(self):
        self.prevaction = -2
        self.own = {}                   # street -> Counter(autoplayer action code)
        self.hands_played += 1
        self.random_hand = self.rng.random()
        self._prwin_cache.clear()

    def on_new_round(self):
        self.random_round = self.rng.random()

    def on_own_action(self, code, street):
        """code = autoplayer function used: fold check call raise betsize betpot allin.
        As CSymbolEngineHistory: every action is counted once, under its own code."""
        self.prevaction = PREVACTION.get(code, -2)
        self.own.setdefault(street, Counter())[code] += 1

    # -- helpers ------------------------------------------------------------
    @property
    def t(self):
        return self.view.t

    def _seats_dealt(self):
        return [s for s in self.t.seats if s.in_hand]

    def _positions(self):
        """chair numbers for button, cutoff, mp3, mp2, mp1, ep3, ep2, ep1, utg (OpenHoldem style)."""
        key = ("_pos", self.t.hand_no)
        p = self.cache.get(key)
        if p is not None:
            return p
        t = self.t
        n = t.n
        order = []                    # chairs from SB clockwise
        c = t.sb_chair
        for _ in range(n):
            if t.seats[c].in_hand:
                order.append(c)
            c = (c + 1) % n
        p = {"smallblindchair": t.sb_chair, "bigblindchair": t.bb_chair,
             "dealerchair": t.dealer, "buttonchair": t.dealer}
        if n == 2:
            p["buttonchair"] = t.dealer
        rest = [c for c in order if c not in (t.sb_chair, t.bb_chair)]
        p["utgchair"] = rest[0] if rest else -1
        names = ["buttonchair", "cutoffchair", "mp3chair", "mp2chair", "mp1chair",
                 "ep3chair", "ep2chair", "ep1chair"]
        for name, chair in zip(names, reversed(rest)):
            p[name] = chair
        for name in names:
            p.setdefault(name, -1)
        self.cache[key] = p
        return p

    def _street_records(self, street=None):
        st = self.t.street if street is None else street
        return [h for h in self.t.history if h.street == st and h.kind not in ("ante",)]

    def _raisers(self, street=None):
        return [h.chair for h in self._street_records(street) if h.kind in ("raise", "bet")]

    def _prwin(self):
        me = self.view.me
        board = self.t.board
        try:
            nopp = int(self.prwin_opponents_fn()) if self.prwin_opponents_fn else 0
        except Exception:
            nopp = 0
        if nopp <= 0:
            nopp = max(1, len([s for s in self.t.active() if s.chair != me.chair]))
        nopp = min(nopp, 9)
        key = (tuple(me.hole), tuple(board), nopp)
        r = self._prwin_cache.get(key)
        if r:
            return r
        known = set(me.hole) | set(board)
        deck = [(rk, su) for rk in range(2, 15) for su in range(4) if (rk, su) not in known]
        win = tie = los = 0
        rng = self.rng
        need_board = 5 - len(board)
        for _ in range(self.prwin_iterations):
            sample = rng.sample(deck, need_board + 2 * nopp)
            full = board + sample[:need_board]
            mine = he.score(me.hole + full)
            best_opp = 0
            for i in range(nopp):
                o = sample[need_board + 2 * i: need_board + 2 * i + 2]
                sc = he.score(o + full)
                if sc > best_opp:
                    best_opp = sc
            if mine > best_opp:
                win += 1
            elif mine == best_opp:
                tie += 1
            else:
                los += 1
        n = float(self.prwin_iterations)
        r = (win / n, tie / n, los / n)
        self._prwin_cache[key] = r
        return r

    # -- the big switch -----------------------------------------------------
    def _compute(self, name):
        t = self.t
        me = self.view.me
        bb = t.bb
        if name in RANKS:
            return RANKS[name]
        if name in SUITS:
            return SUITS[name]
        if name in POKERVAL_CONST:
            return POKERVAL_CONST[name]
        if name in ("preflop", "flop", "turn", "river"):
            return {"preflop": 1, "flop": 2, "turn": 3, "river": 4}[name]
        if name[:1] == "_":
            # e.g. "_dealerchair" from me_st_X__dealerchair (library typo): 0 as in OpenHoldem
            return 0
        if name[:1] == "f" and name[1:].isdigit():
            return 0                       # flag buttons f0..f19: never pressed
        if name == "Random":
            return self.rng.random() * 100     # OpenPPL Random: 0..100
        if name in ("true",):
            return 1
        if name in ("false",):
            return 0
        # ---- limits / game type
        if name in ("bblind", "bet", "bet1", "bet2", "bet3", "bet4"):
            return bb
        if name == "sblind":
            return t.sb
        if name == "ante":
            return t.ante
        if name == "isnl":
            return 0 if t.game == "plo" else 1
        if name in ("ispl", "isomaha"):
            return 1 if t.game == "plo" else 0
        if name in ("isfl", "istournament", "ismtt", "issng", "isdon", "isfinaltable",
                    "israke", "isrush", "isfinalanswer_noop"):
            return 0
        if name in ("ismyturn", "isfinalanswer", "isautopost", "issittingin"):
            return 1
        if name in ("issittingout", "islobby", "ispopup", "isppro", "isbring", "ismanual"):
            return 0
        # ---- round
        if name in ("betround", "currentround"):
            return t.street
        if name == "previousround":
            return max(1, t.street - 1)
        # ---- chairs
        if name == "userchair":
            return me.chair
        if name == "nchairs":
            return t.n
        pos = self._positions()
        if name in pos:
            return pos[name]
        if name in ("dealposition", "betposition") or name.startswith(("ac_dealpos", "ac_betpos")):
            if name.startswith("ac_"):
                # "ac_dealposX" (10 characters + chair) or "ac_betposX" (9 + chair)
                kind = "dealpos" if name.startswith("ac_dealpos") else "betpos"
                rest = name[3 + len(kind):]
                chair = int(rest) if rest.isdigit() else -1
            else:
                chair, kind = me.chair, name[:7]
            order = self._order(kind)
            return order.index(chair) + 1 if chair in order else 0
        if name == "headsupchair":
            opp = [s.chair for s in t.active() if s.chair != me.chair]
            return opp[0] if len(opp) == 1 else -1
        if name in ("raischair", "lastraiserchair"):
            r = self._raisers()
            if not r and t.street == PREFLOP:
                return t.bb_chair
            return r[-1] if r else -1
        if name == "firstraiserchair":
            r = self._raisers()
            return r[0] if r else -1
        if name == "dealpositionlastraiser":
            r = self._raisers()
            if not r and t.street == PREFLOP:
                chair = t.bb_chair
            elif r:
                chair = r[-1]
            else:
                return 0
            order = self._order("dealpos")
            return order.index(chair) + 1 if chair in order else 0
        if name in ("firstcallerchair", "lastcallerchair"):
            calls = self._callers()
            if not calls:
                return -1
            return calls[0] if name == "firstcallerchair" else calls[-1]
        if name in ("bigstackchair", "smallstackchair"):
            opp = [s for s in t.active() if s.chair != me.chair]
            if not opp:
                return -1
            f = max if name == "bigstackchair" else min
            return f(opp, key=lambda s: s.stack + s.bet).chair
        # ---- chip amounts
        if name == "balance":
            return me.stack
        if name.startswith("balance") and name[7:].isdigit():
            return t.seats[int(name[7:])].stack if int(name[7:]) < t.n else 0
        if name.startswith("balance_rank") and name[12:].isdigit():
            vals = sorted((s.stack + s.bet for s in t.seats if s.in_hand and not s.folded), reverse=True)
            i = int(name[12:])
            return vals[i] if i < len(vals) else 0
        if name.startswith("stack") and name[5:].isdigit():
            vals = sorted((s.stack for s in t.seats if s.in_hand and not s.folded), reverse=True)
            i = int(name[5:])
            return vals[i] if i < len(vals) else 0
        if name == "currentbet":
            return me.bet
        if name.startswith("currentbet") and name[10:].isdigit():
            c = int(name[10:])
            return t.seats[c].bet if c < t.n else 0
        cb = t.current_bet()
        if name == "call":
            return min(cb - me.bet, me.stack)
        if name == "pot":
            return t.pot
        if name == "potplayer":
            return sum(s.bet for s in t.seats)
        if name == "potcommon":
            return t.pot - sum(s.bet for s in t.seats)
        if name == "nbetstocall":
            return min(cb - me.bet, me.stack) / bb
        if name == "ncallbets":
            return cb / bb
        if name == "ncurrentbets":
            return me.bet / bb
        if name == "nbetstorais":
            return (cb + t.last_raise - me.bet) / bb
        if name in ("totalinvested",):
            return me.invested
        if name in ("maxbalance",):
            return max(s.stack for s in t.seats)
        # ---- OpenPPL aliases (amounts in big blinds, position booleans)
        if name == "stacksize":
            return me.stack / bb
        if name == "startingstacksize":
            return me.start_stack / bb
        if name == "stacksizeatbeginningofcurrentround":
            return (me.stack + me.bet) / bb
        if name == "amounttocall":
            return min(cb - me.bet, me.stack) / bb
        if name == "opponents":
            return len([s for s in t.active() if s.chair != me.chair])
        if name == "opponentsstacksizeheadsup":
            opp = [s for s in t.active() if s.chair != me.chair]
            return opp[0].stack / bb if len(opp) == 1 else 0
        if name in ("insmallblind",):
            return 1.0 if me.chair == t.sb_chair else 0.0
        if name in ("inbigblind",):
            return 1.0 if me.chair == t.bb_chair else 0.0
        if name in ("inbutton",):
            return 1.0 if me.chair == t.dealer else 0.0
        if name in ("incutoff",):
            p = self._positions()
            return 1.0 if me.chair == p.get("cutoffchair", -1) else 0.0
        if name in ("inlateposition",):
            p = self._positions()
            return 1.0 if me.chair in (t.dealer, p.get("cutoffchair", -1)) else 0.0
        if name in ("inmiddleposition",):
            p = self._positions()
            return 1.0 if me.chair in (p.get("mp1chair",-1), p.get("mp2chair",-1), p.get("mp3chair",-1)) else 0.0
        if name in ("inearlyposition",):
            p = self._positions()
            return 1.0 if me.chair in (p.get("ep1chair",-1), p.get("ep2chair",-1), p.get("ep3chair",-1), p.get("utgchair",-1)) else 0.0
        if name == "raises":
            return len([h for h in self._street_records() if h.kind in ("raise", "bet")])
        if name == "calls":
            return len([h for h in self._street_records() if h.kind == "call"])
        if name == "checks":
            return len([h for h in self._street_records() if h.kind == "check"])
        if name == "bets":
            return len([h for h in self._street_records() if h.kind == "bet"])
        if name == "folds":
            return len([h for h in self._street_records() if h.kind == "fold"])
        if name == "botslastaction":
            pa = self.prevaction
            return pa if pa != -2 else 0
        if name == "botslastpreflopaction":
            _PREFLOP = 1  # engine.PREFLOP = 1
            for code, count in self.own.get(_PREFLOP, {}).items():
                if count > 0:
                    return {"fold": -1, "check": 0, "call": 1, "raise": 2, "betsize": 3, "betpot": 3, "allin": 4}.get(code, 0)
            return 0
        if name == "botsactionsonthistround" or name == "botsactionsonthistroundincludingchecks":
            st = t.street
            return sum(self.own.get(st, {}).values())
        if name == "numberofopponentsallin":
            return len([s for s in t.active() if s.allin and s.chair != me.chair])
        if name == "stilltoactnumber" or name == "stilltoact":
            recs = self._street_records()
            acted = {h.chair for h in recs}
            return len([s for s in t.active() if s.chair not in acted and s.chair != me.chair])
        if name == "raisessincelastplay":
            recs = self._street_records()
            last = max((i for i, h in enumerate(recs) if h.chair == me.chair), default=-1)
            return len([h for h in recs[last+1:] if h.kind in ("raise", "bet")])
        if name in ("others",):
            return 1.0
        if name in ("none",):
            return 0.0
        if name in ("call_openppl",):
            return 1.0
        if name in ("raise_openppl", "raiseto"):
            return 2.0
        if name in ("raisemax",):
            return 3.0
        # ---- players
        dealt = self._seats_dealt()
        active = t.active()
        opp_active = [s for s in active if s.chair != me.chair]
        if name in ("nplayersseated", "nplayersdealt", "maxnplayersdealt"):
            return len(dealt)
        if name == "nplayersplaying":
            return len(active)
        if name == "nplayersactive":
            return len([s for s in active if not s.allin])
        if name in ("nopponentsseated", "nopponentsdealt"):
            return len(dealt) - 1
        if name == "nopponentsplaying":
            return len(opp_active)
        if name == "nopponentsactive":
            return len([s for s in opp_active if not s.allin])
        if name == "nopponentsallin":
            return len([s for s in opp_active if s.allin])
        if name == "nopponentsfolded":
            return len([s for s in dealt if s.folded and s.chair != me.chair])
        recs = self._street_records()
        last_mine = max((i for i, h in enumerate(recs) if h.chair == me.chair
                         and h.kind not in ("sb", "bb")), default=-1)
        since = recs[last_mine + 1:]
        if name == "nopponentschecking":
            return len({h.chair for h in recs if h.kind == "check" and h.chair != me.chair})
        if name == "nopponentscalling":
            return len(self._callers())
        if name in ("nopponentstruelyraising", "nopponentsraising", "nopponentsbetting"):
            return len({h.chair for h in since if h.kind in ("raise", "bet") and h.chair != me.chair})
        if name == "nplayersround":
            return len(active)
        if name.startswith("nplayersround") and name[13:].isdigit():
            st = int(name[13:])
            if st > t.street:
                return 0
            folded_before = {h.chair for h in t.history if h.kind == "fold" and h.street < st}
            return len([s for s in dealt if s.chair not in folded_before])
        if name == "nplayersround_currentround":
            return len(active)
        # ---- bits
        if name == "playersseatedbits" or name == "playersdealtbits":
            return _bits(s.chair for s in dealt)
        if name in ("playersplayingbits",):
            return _bits(s.chair for s in active)
        if name in ("playersactivebits",):
            return _bits(s.chair for s in active if not s.allin)
        if name in ("opponentsseatedbits", "opponentsdealtbits"):
            return _bits(s.chair for s in dealt if s.chair != me.chair)
        if name in ("opponentsplayingbits",):
            return _bits(s.chair for s in opp_active)
        if name in ("opponentsactivebits",):
            return _bits(s.chair for s in opp_active if not s.allin)
        for prefix, kinds in (("raisbits", ("raise", "bet")), ("callbits", ("call",)),
                              ("foldbits", ("fold",))):
            if name.startswith(prefix):
                rest = name[len(prefix):]
                st = int(rest) if rest.isdigit() else t.street
                return _bits(h.chair for h in t.history if h.street == st and h.kind in kinds)
        # ---- own actions (counts in the current round, or per round)
        for prefix, code in (("didchec", "check"), ("didcall", "call"), ("didrais", "raise"),
                             ("didswag", "betsize"), ("didbetsize", "betsize"), ("didalli", "allin"),
                             ("didfold", "fold")):
            if name.startswith(prefix):
                rest = name[len(prefix):]
                if rest == "":
                    st = t.street
                elif rest.startswith("round") and rest[5:].isdigit():
                    st = int(rest[5:])
                else:
                    continue
                n = self.own.get(st, {}).get(code, 0)
                if code == "betsize":
                    # pot-fraction bets count as sized bets (fix in our OpenHoldem, CSymbolEngineHistory::didswag)
                    n += self.own.get(st, {}).get("betpot", 0)
                return min(n, 1) if code == "check" else n      # didchec is 0/1 in OpenHoldem
        if name.startswith("nbetsround"):
            rest = name[10:]
            st = int(rest) if rest.isdigit() else t.street
            if st > t.street:
                return 0
            return max((h.to for h in t.history if h.street == st), default=0) / bb
        if name == "prevaction":
            return self.prevaction
        if name in ("handsplayed", "handnumber"):
            return self.hands_played
        if name == "handsplayed_headsup":
            return 0
        # ---- cards
        hole = me.hole
        board = t.board
        if name.startswith("$$"):
            kind, which, idx = name[2], name[3], name[4:]
            if idx.isdigit():
                cards = hole if kind == "p" else board
                i = int(idx)
                if i >= len(cards):
                    return 0
                r, s = cards[i]
                return r if which == "r" else s if which == "s" else _std_card(cards[i])
        if name == "ispair":
            return 1 if hole[0][0] == hole[1][0] else 0
        if name == "issuited":
            return 1 if hole[0][1] == hole[1][1] else 0
        if name == "isconnector":
            return 1 if abs(hole[0][0] - hole[1][0]) == 1 else 0
        if name == "rankhiplayer":
            return max(r for r, _ in hole)
        if name == "rankloplayer":
            return min(r for r, _ in hole)
        if name == "rankhicommon":
            return max((r for r, _ in board), default=0)
        if name == "ranklocommon":
            return min((r for r, _ in board), default=0)
        allc = hole + board
        if name == "rankbits":
            return _rankbits(allc)
        if name == "rankbitsplayer":
            return _rankbits(hole)
        if name == "rankbitscommon":
            return _rankbits(board)
        if name in ("pokerval", "pcbits", "npcbits", "rankbitspoker", "rankhipoker", "ranklopoker"):
            pv, pcb = he.pokerval_cached(allc, hole)
            if name == "pokerval":
                return pv
            if name == "pcbits":
                return pcb
            if name == "npcbits":
                return bin(pcb).count("1")
            cat, ranks = he.evaluate_cached(allc)
            slots = he.five_ranks(cat, ranks)
            if name == "rankbitspoker":
                return _rankbits([(r, 0) for r in slots])
            return max(slots) if name == "rankhipoker" else min(slots)
        if name == "pokervalplayer":
            return he.pokerval_cached(hole)[0]
        if name == "pokervalcommon":
            return he.pokerval_cached(board)[0] if board else 0
        if name in ("ishicard", "isonepair", "istwopair", "isthreeofakind", "isstraight",
                    "isflush", "isfullhouse", "isfourofakind", "isstraightflush", "isroyalflush"):
            cat, ranks = he.evaluate_cached(allc)
            want = {"ishicard": he.HIGHCARD, "isonepair": he.PAIR, "istwopair": he.TWOPAIR,
                    "isthreeofakind": he.TRIPS, "isstraight": he.STRAIGHT, "isflush": he.FLUSH,
                    "isfullhouse": he.FULLHOUSE, "isfourofakind": he.QUADS,
                    "isstraightflush": he.STFLUSH, "isroyalflush": he.STFLUSH}[name]
            if name == "isroyalflush":
                return 1 if cat == he.STFLUSH and ranks[0] == 14 else 0
            return 1 if cat == want else 0
        if name in ("ishipair", "islopair", "ismidpair"):
            cat, ranks = he.evaluate_cached(allc)
            if cat != he.PAIR or not board:
                return 0
            p = ranks[0]
            br = sorted({r for r, _ in board}, reverse=True)
            if name == "ishipair":
                return 1 if p >= br[0] else 0
            if name == "islopair":
                return 1 if p <= br[-1] else 0
            return 1 if br[-1] < p < br[0] else 0
        if name in ("ishistraight",):
            cat, ranks = he.evaluate_cached(allc)
            return 1 if cat == he.STRAIGHT and ranks[0] == 14 else 0
        if name in ("nsuited", "nsuitedcommon", "tsuit", "tsuitcommon"):
            cards = allc if name in ("nsuited", "tsuit") else board
            cnt = Counter(s for _, s in cards)
            if not cnt:
                return 0
            suit, k = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))
            return k if name.startswith("n") else suit
        if name in ("nranked", "nrankedcommon", "trank", "trankcommon"):
            cards = allc if name in ("nranked", "trank") else board
            cnt = Counter(r for r, _ in cards)
            if not cnt:
                return 0
            rank, k = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))
            return k if name.startswith("n") else rank
        if name in ("nstraight", "nstraightcommon", "nstraightfill", "nstraightfillcommon"):
            cards = allc if name in ("nstraight", "nstraightfill") else board
            rb = _rankbits(cards)
            best = 0
            for lo in range(1, 11):
                k = sum(1 for r in range(lo, lo + 5) if rb & (1 << r))
                best = max(best, k)
            return best if not name.startswith("nstraightfill") else 5 - best
        if name.startswith("suitbitsplayer") or name.startswith("suitbitscommon"):
            idx = name[14:]
            if idx.isdigit():
                cards = hole if name.startswith("suitbitsplayer") else board
                return _rankbits([c for c in cards if c[1] == int(idx)])
        if name.startswith("$") and len(name) >= 3:
            return self._hand_symbol(name[1:])
        if name.startswith("handrank"):
            return self._handrank(name)
        if name in ("nchairsdealtleft", "nchairsdealtright"):
            n = t.n
            left = right = 0
            found = False
            for i in range(t.dealer + 1, t.dealer + n + 1):
                c = i % n
                if c == me.chair:
                    found = True
                elif t.seats[c].in_hand:
                    if found:
                        left += 1
                    else:
                        right += 1
            return left if name == "nchairsdealtleft" else right
        if name == "missingsmallblind":
            return 0
        if name == "ac_agchair_after":
            r = self._raisers() or (self._raisers(t.street - 1) if t.street > PREFLOP else [])
            if not r:
                return 0
            order = self._order("betpos")
            ag = r[-1]
            if ag not in order or me.chair not in order:
                return 0
            return 1 if order.index(ag) > order.index(me.chair) else 0
        if name in ("nhands", "nhandshi", "nhandslo", "nhandsti"):
            hi, lo, ti = self._nhands()
            return {"nhands": hi + lo + ti, "nhandshi": hi, "nhandslo": lo, "nhandsti": ti}[name]
        if name in ("prwinnow", "prlosnow"):
            hi, lo, ti = self._nhands()
            n = hi + lo + ti
            nopp = max(1, len(opp_active))
            if name == "prwinnow":
                return ((lo + ti) / n) ** nopp if n else 0
            return 1 - (1 - hi / n) ** nopp if n else 0
        # ---- probabilities and randomness
        if name in ("prwin", "prtie", "prlos"):
            if t.game == "plo":
                # OpenHoldem: "PrWin-simulation, HoldEm only, not Omaha" (no computation -> 0)
                return 0
            w, ti, lo = self._prwin()
            return {"prwin": w, "prtie": ti, "prlos": lo}[name]
        if name == "random":
            return self.rng.random()
        if name == "randomhand":
            return self.random_hand
        if name == "randomround":
            return self.random_round
        if name == "randomheartbeat":
            return self.random_heartbeat
        # ---- misc
        if name.startswith("sitename$") or name.startswith("network$"):
            return 1 if "ohsim" in name[9:].lower() else 0
        if name in ("elapsed", "elapsedhand", "elapsedauto", "elapsedtoday", "elapsed1970",
                    "nattached", "session", "version"):
            return 0
        if name.startswith("dll$"):
            return self._dll(name)
        if name.startswith("pt_") and self.stats is not None:
            try:
                return self.stats.symbol(name, self.view)
            except KeyError:
                # stat not defined in OpenHoldem's PokerTracker queries (e.g. pt_flopaf)
                self.missing[name] += 1
                return -1.0
        self.missing[name] += 1
        raise KeyError(name)

    def _dll(self, name):
        """Symbols that OpenHoldem would get from a user DLL (here computed by the simulator).
        dll$prwin_range: win probability against the ranges of the live opponents (ohsim.ranges);
        dll$range_opponents: number of live opponents used."""
        from . import ranges
        me = self.view.me
        t = self.t
        if name == "dll$range_opponents":
            return float(len([s for s in t.seats if s.in_hand and not s.folded and s is not me]))
        if name == "dll$prwin_range":
            model = self.range_model or ranges.PlayerModel()
            W = ranges.opponent_ranges(t, me.chair, model, me.hole)
            if not W:
                return 1.0
            return ranges.equity(me.hole, list(t.board), list(W.values()), iters=400, rng=self.rng)
        if name == "dll$range_eq_raise":
            key = (t.hand_no, len(t.history), me.chair)
            if getattr(self, "_raise_key", None) != key:
                model = self.range_model or ranges.PlayerModel()
                self._raise_eq = ranges.raise_equity(t, me.chair, model, me.hole, rng=self.rng)
                self._raise_key = key
            return self._raise_eq
        if name == "dll$prwin_oh":
            # OpenHoldem's own weighted prwin, at its best: willplay = mean VPIP of the live opponents (HUD),
            # wontplay = VPIP + 10 points, the same range for every opponent, no change after preflop
            model = self.range_model or ranges.PlayerModel()
            live = [s for s in t.seats if s.in_hand and not s.folded and s is not me]
            if not live:
                return 1.0
            v = sum(model.vpip(s.name) for s in live) / len(live)
            w = ranges.oh_weights(ranges.oh_classes(v), ranges.oh_classes(min(1.0, v + 0.10)), len(live),
                                  dead=list(me.hole) + list(t.board))
            return ranges.equity(me.hole, list(t.board), [w] * len(live), iters=400, rng=self.rng)
        if name in ("dll$range_fold", "dll$range_eq_call", "dll$range_eq_now", "dll$range_bet_gain"):
            # bet outlook, cached per decision (same hand, same history length)
            key = (t.hand_no, len(t.history), me.chair)
            if getattr(self, "_outlook_key", None) != key:
                model = self.range_model or ranges.PlayerModel()
                self._outlook = ranges.bet_outlook(t, me.chair, model, me.hole, rng=self.rng)
                self._outlook_key = key
            fold, eq_call, eq_now, gain = self._outlook
            return {"dll$range_fold": fold, "dll$range_eq_call": eq_call, "dll$range_eq_now": eq_now,
                    "dll$range_bet_gain": gain}[name]
        self.missing[name] += 1
        return 0.0

    def _handrank(self, name):
        hole = self.view.me.hole
        (r1, s1), (r2, s2) = sorted(hole[:2], reverse=True)    # OpenHoldem: first two cards
        names = "23456789TJQKA"
        cardstr = names[r1 - 2] + names[r2 - 2] + ("s" if s1 == s2 and r1 != r2 else "o")
        try:
            nopp = int(self.prwin_opponents_fn()) if self.prwin_opponents_fn else 0
        except Exception:
            nopp = 0
        if nopp <= 0:
            nopp = max(1, len([s for s in self.t.active() if s.chair != self.view.me.chair]))
        nopp = max(1, min(nopp, len(HANDRANK["t169"])))
        i = HANDRANK["t169"][nopp - 1].index(cardstr)
        hr2652 = HANDRANK["t2652"][nopp - 1][i]
        return {"handrank169": i + 1, "handrank2652": hr2652, "handrank": hr2652,
                "handrank1326": hr2652 // 2, "handrank1000": 1000 * hr2652 // 2652,
                "handrankp": 2652.0 / (1.0 + nopp)}.get(name, 0)

    def _callers(self):
        """Opponents calling the current highest bet, in the order they called (CSymbolEngineCallers):
        their bet equals the highest bet (or they are all-in for less in this orbit), they are not the
        player who made that bet, not the user; limpers before a raise are no callers of the raise."""
        t = self.t
        me = self.view.me
        hb = t.current_bet()
        if hb <= 0:
            return []
        recs = self._street_records()
        aggr = [h for h in recs if h.kind in ("raise", "bet")]
        raiser = aggr[-1].chair if aggr else (t.bb_chair if t.street == PREFLOP else -1)
        start = recs.index(aggr[-1]) + 1 if aggr else 0
        out = []
        for h in recs[start:]:
            if h.kind != "call" or h.chair in (me.chair, raiser) or h.chair in out:
                continue
            s = t.seats[h.chair]
            if s.folded or not s.in_hand:
                continue
            if abs(s.bet - hb) < 1e-9 or s.allin:
                out.append(h.chair)
        return out

    def _order(self, kind):
        """Chairs in dealing order (from SB) or betting order of the current round."""
        t = self.t
        n = t.n
        dealt = [s.chair for s in t.seats if s.in_hand]
        if kind == "dealpos":
            start = t.sb_chair
            return [c for c in ((start + i) % n for i in range(n)) if c in dealt]
        if t.street == PREFLOP:
            start = (t.bb_chair + 1) % n
        else:
            start = (t.dealer + 1) % n
        playing = [s.chair for s in t.active()]
        return [c for c in ((start + i) % n for i in range(n)) if c in playing]

    def _nhands(self):
        me = self.view.me
        board = self.t.board
        key = ("_nhands", tuple(me.hole), tuple(board))
        r = self._prwin_cache.get(key)      # cleared at hand start
        if r is not None:
            return r
        known = set(me.hole) | set(board)
        deck = [(rk, su) for rk in range(2, 15) for su in range(4) if (rk, su) not in known]
        mine = he.score(me.hole + board)
        hi = lo = ti = 0
        for i in range(len(deck)):
            a = deck[i]
            for j in range(i + 1, len(deck)):
                sc = he.score([a, deck[j]] + board)
                if sc > mine:
                    hi += 1
                elif sc < mine:
                    lo += 1
                else:
                    ti += 1
        r = (hi, lo, ti)
        self._prwin_cache[key] = r
        return r

    def _hand_symbol(self, spec):
        """$AA, $AKs, $AKo, $AK, $Ax, $AXs (x = any card)."""
        hole = self.view.me.hole[:2]                    # HoldEm symbols: first two cards
        r = sorted(hole, reverse=True)
        names = "23456789TJQKA"
        s = spec.upper()
        suited = s.endswith("S") and len(s) == 3
        offsuit = s.endswith("O") and len(s) == 3
        core = s[:2] if (suited or offsuit) else s
        hand = [names[r[0][0] - 2], names[r[1][0] - 2]]
        ok = False
        if len(core) == 2:
            a, b = core
            if b == "X":
                ok = a in hand
            else:
                ok = sorted([a, b]) == sorted(hand)
        elif len(core) == 1:
            ok = core in hand
        if not ok:
            return 0
        is_suited = hole[0][1] == hole[1][1]
        if suited and not is_suited:
            return 0
        if offsuit and is_suited:
            return 0
        return 1
