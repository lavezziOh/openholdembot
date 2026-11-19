"""PokerTracker 4 per-hand player columns (cash_hand_player_statistics) from a HandLog.

Only the columns used by OpenHoldem's PokerTracker stat queries are computed.
Definitions are validated against the values stored by PokerTracker itself on
real hands (tools/validate_ptrows.py).
"""
from .handlog import PREFLOP, FLOP, TURN, RIVER

STREET_PREFIX = {FLOP: "f", TURN: "t", RIVER: "r"}
LETTER = {"fold": "F", "check": "X", "call": "C", "bet": "B", "raise": "R"}
VOLUNTARY = ("fold", "check", "call", "bet", "raise")


def positions(h):
    """PokerTracker positions: BTN=0, CO=1, MP=2, EP=3..., BB=8, SB=9."""
    names = [p.name for p in h.players]
    sb = next((a.name for a in h.actions if a.kind == "sb"), None)
    bb = next((a.name for a in h.actions if a.kind == "bb"), None)
    seats = sorted(h.players, key=lambda p: p.seat)
    # clockwise order starting after the button
    after_btn = sorted(seats, key=lambda p: (p.seat - h.button - 1) % 100)
    pos = {}
    others = [p.name for p in after_btn if p.name not in (sb, bb)]
    for k, n in enumerate(reversed(others)):     # button is the last one clockwise
        pos[n] = k
    if sb:
        pos[sb] = 9
    if bb:
        pos[bb] = 8
    for n in names:
        pos.setdefault(n, 0)
    return pos


def postflop_order(pos):
    """Acting order postflop: SB, BB, then from the highest position to the button."""
    return sorted(pos, key=lambda n: (0 if pos[n] == 9 else 1 if pos[n] == 8 else 2, -pos[n]))


def rows(h):
    pos = positions(h)
    names = [p.name for p in h.players]
    n_players = len(names)
    R = {n: _empty() for n in names}
    for p in h.players:
        r = R[p.name]
        r["position"] = pos[p.name]
        r["cnt_players"] = n_players
        r["cnt_players_lookup_position"] = n_players
        r["amt_before"] = p.stack
    for a in h.actions:
        r = R[a.name]
        if a.kind == "ante":
            r["amt_ante"] += a.amount
        elif a.kind in ("sb", "bb", "post"):
            r["amt_blind"] += a.amount
            if a.kind == "sb":
                r["flg_blind_s"] = True
            elif a.kind == "bb":
                r["flg_blind_b"] = True
    folded = set()
    pfr = _preflop(h, R, pos, folded)
    prev_cbettor = pfr
    for street in (FLOP, TURN, RIVER):
        need = {FLOP: 3, TURN: 4, RIVER: 5}[street]
        if len(h.board) < need:
            break
        prev_cbettor = _postflop(h, R, pos, folded, street, prev_cbettor)
    alive = [n for n in names if n not in folded]
    showdown = len(alive) > 1
    for n in names:
        r = R[n]
        r["flg_showdown"] = showdown and n in alive
        r["flg_won_hand"] = h.collected.get(n, 0) > 0
    return R


def _empty():
    r = {"amt_ante": 0.0, "amt_blind": 0.0, "flg_blind_s": False, "flg_blind_b": False,
         "flg_vpip": False, "cnt_p_raise": 0, "cnt_p_call": 0, "flg_p_first_raise": False,
         "flg_p_limp": False, "flg_p_fold": False, "flg_p_ccall": False, "cnt_p_face_limpers": 0,
         "flg_p_open_opp": False, "flg_p_face_raise": False,
         "flg_p_3bet": False, "flg_p_3bet_opp": False, "flg_p_3bet_def_opp": False, "enum_p_3bet_action": "N",
         "flg_p_4bet": False, "flg_p_4bet_opp": False, "flg_p_4bet_def_opp": False, "enum_p_4bet_action": "N",
         "flg_p_squeeze": False, "flg_p_squeeze_opp": False, "flg_p_squeeze_def_opp": False,
         "enum_p_squeeze_action": "N",
         "flg_steal_opp": False, "flg_steal_att": False, "flg_blind_def_opp": False,
         "flg_sb_steal_fold": False, "flg_bb_steal_fold": False,
         "val_p_raise_aggressor_pos": -1, "amt_p_raise_facing": 0.0, "amt_p_2bet_facing": 0.0,
         "action_p": "", "flg_showdown": False, "flg_won_hand": False}
    for s in "ftr":
        r.update({f"flg_{s}_saw": False, f"flg_{s}_bet": False, f"cnt_{s}_raise": 0, f"cnt_{s}_call": 0,
                  f"flg_{s}_check": False, f"flg_{s}_check_raise": False, f"flg_{s}_fold": False,
                  f"flg_{s}_face_raise": False, f"flg_{s}_open_opp": False, f"flg_{s}_first": False,
                  f"flg_{s}_has_position": False, f"flg_{s}_cbet": False, f"flg_{s}_cbet_opp": False,
                  f"flg_{s}_cbet_def_opp": False, f"enum_{s}_cbet_action": "N",
                  f"flg_{s}_3bet_def_opp": False, f"enum_{s}_3bet_action": "N",
                  f"amt_{s}_bet_facing": 0.0, f"action_{s}": ""})
    return r


def _preflop(h, R, pos, folded):
    bets = {}                        # street investment (blinds included)
    for a in h.actions:
        if a.kind in ("sb", "bb", "post"):
            bets[a.name] = a.to
    level = max(bets.values(), default=0.0)
    stack = {pl.name: pl.stack - r_ante(h, pl.name) for pl in h.players}
    allin = set()
    raises = 0
    raisers = []                     # names in raise order
    callers_since = 0                # callers since the last raise
    limpers = 0
    first_raiser = None
    steal = False                    # first raise was a steal attempt
    acted = set()
    voluntary = set()
    for a in h.actions:
        if a.street != PREFLOP or a.kind not in VOLUNTARY:
            continue
        r = R[a.name]
        p = a.name
        first = p not in acted
        to_call = max(level - bets.get(p, 0.0), 0.0)
        left = stack[p] - bets.get(p, 0.0)            # chips behind
        # a raise is possible only with chips behind and somebody left who could answer it
        others_live = any(q != p and q not in folded and q not in allin for q in stack)
        can_raise = left > to_call + 1e-9 and others_live
        # ---- context of this decision
        if first:
            if raises == 0 and limpers == 0:
                r["flg_p_open_opp"] = True
                if pos[p] in (0, 1, 9):
                    r["flg_steal_opp"] = True
            r["cnt_p_face_limpers"] = limpers if raises == 0 else 0
        if raises >= 1:
            # PokerTracker keeps the raise faced at the player's LAST decision
            r["flg_p_face_raise"] = True
            r["val_p_raise_aggressor_pos"] = pos[raisers[-1]]
            r["amt_p_raise_facing"] = round(min(to_call, left), 2)
        # "Nbet opportunity / defence": anybody facing the (N-1)th raise who did not make it
        if raises == 1 and p != raisers[0] and r["amt_p_2bet_facing"] == 0.0:
            r["amt_p_2bet_facing"] = round(min(to_call, left), 2)
        if raises == 1 and p != raisers[0] and can_raise:
            r["flg_p_3bet_opp"] = True
            if callers_since >= 1:
                r["flg_p_squeeze_opp"] = True
        if raises == 1 and p != raisers[0]:
            if steal and (r["flg_blind_s"] or r["flg_blind_b"]) and first and callers_since == 0:
                r["flg_blind_def_opp"] = True
        if raises >= 2 and p != raisers[-1] and can_raise:
            r["flg_p_4bet_opp"] = True                # PokerTracker: "4bet" = 4bet or more
        if raises == 2 and p != raisers[1]:
            if not r["flg_p_3bet_def_opp"]:
                r["enum_p_3bet_action"] = LETTER[a.kind]
            r["flg_p_3bet_def_opp"] = True
            if R[raisers[1]]["flg_p_squeeze"] and (p == raisers[0] or p in voluntary):
                if not r["flg_p_squeeze_def_opp"]:
                    r["enum_p_squeeze_action"] = LETTER[a.kind]
                r["flg_p_squeeze_def_opp"] = True
        if raises >= 3 and p != raisers[-1]:
            if not r["flg_p_4bet_def_opp"]:
                r["enum_p_4bet_action"] = LETTER[a.kind]
            r["flg_p_4bet_def_opp"] = True
        # ---- the action
        r["action_p"] += LETTER[a.kind]
        if a.kind == "fold":
            r["flg_p_fold"] = True
            folded.add(p)
            if r["flg_blind_def_opp"] and first:
                if r["flg_blind_s"]:
                    r["flg_sb_steal_fold"] = True
                if r["flg_blind_b"]:
                    r["flg_bb_steal_fold"] = True
        elif a.kind == "call":
            r["cnt_p_call"] += 1
            r["flg_vpip"] = True
            if raises == 0:
                if first:
                    r["flg_p_limp"] = True
                limpers += 1
            else:
                if first and r["amt_blind"] == 0:      # blinds never cold-call
                    r["flg_p_ccall"] = True
                callers_since += 1
            voluntary.add(p)
        elif a.kind in ("raise", "bet"):
            r["cnt_p_raise"] += 1
            r["flg_vpip"] = True
            if raises == 0:
                r["flg_p_first_raise"] = True
                first_raiser = p
                if r["flg_steal_opp"]:
                    r["flg_steal_att"] = True
                    steal = True
            if raises == 1 and r["flg_p_3bet_opp"]:
                r["flg_p_3bet"] = True
                if r["flg_p_squeeze_opp"]:
                    r["flg_p_squeeze"] = True
            if raises >= 2 and r["flg_p_4bet_opp"]:
                r["flg_p_4bet"] = True
            raises += 1
            raisers.append(p)
            callers_since = 0
            voluntary.add(p)
        if a.kind in ("call", "raise", "bet"):
            bets[p] = a.to if a.kind != "call" else bets.get(p, 0.0) + a.amount
            level = max(level, bets[p])
            if a.allin or stack[p] - bets[p] <= 1e-9:
                allin.add(p)
        acted.add(p)
    return raisers[-1] if raisers else None


def _postflop(h, R, pos, folded, street, prev_aggr):
    s = STREET_PREFIX[street]
    alive = [n for n in postflop_order(pos) if n not in folded]
    for n in alive:
        R[n][f"flg_{s}_saw"] = True
    if alive:
        R[alive[-1]][f"flg_{s}_has_position"] = True
    acts = [a for a in h.actions if a.street == street and a.kind in VOLUNTARY]
    if alive:
        R[alive[0]][f"flg_{s}_first"] = True         # first by position, even if all-in...
    if acts:
        R[acts[0].name][f"flg_{s}_first"] = True     # ...and the first one who really acts
    before = {}
    for a in h.actions:
        if a.street < street:
            before[a.name] = before.get(a.name, 0.0) + a.amount
    bets = 0
    level = 0.0
    inv = {}
    bettors = []
    acted = set()
    cbettor = None
    cbet_faced = set()
    for a in acts:
        p = a.name
        r = R[p]
        first = p not in acted
        to_call = max(level - inv.get(p, 0.0), 0.0)
        left = h.player(p).stack - before.get(p, 0.0) - inv.get(p, 0.0)
        to_call = min(to_call, max(left, 0.0))
        if first and bets == 0:
            r[f"flg_{s}_open_opp"] = True
            if p == prev_aggr:
                r[f"flg_{s}_cbet_opp"] = True
        if bets == 1 and r[f"amt_{s}_bet_facing"] == 0.0 and to_call > 0:
            r[f"amt_{s}_bet_facing"] = round(to_call, 2)
        if bets >= 2:
            r[f"flg_{s}_face_raise"] = True
        if cbettor and p != cbettor and bets == 1 and p not in cbet_faced:
            cbet_faced.add(p)
            r[f"flg_{s}_cbet_def_opp"] = True
            r[f"enum_{s}_cbet_action"] = LETTER[a.kind]
        if bets == 3 and p != bettors[2]:
            if not r[f"flg_{s}_3bet_def_opp"]:
                r[f"enum_{s}_3bet_action"] = LETTER[a.kind]
            r[f"flg_{s}_3bet_def_opp"] = True
        r[f"action_{s}"] += LETTER[a.kind]
        if a.kind == "fold":
            r[f"flg_{s}_fold"] = True
            folded.add(p)
        elif a.kind == "check":
            r[f"flg_{s}_check"] = True
        elif a.kind == "call":
            r[f"cnt_{s}_call"] += 1
            inv[p] = inv.get(p, 0.0) + a.amount
        elif a.kind in ("bet", "raise"):
            if bets == 0:
                r[f"flg_{s}_bet"] = True
                if r[f"flg_{s}_cbet_opp"]:
                    r[f"flg_{s}_cbet"] = True
                    cbettor = p
            else:
                r[f"cnt_{s}_raise"] += 1
                if r[f"flg_{s}_check"]:
                    r[f"flg_{s}_check_raise"] = True
            bets += 1
            bettors.append(p)
            inv[p] = a.to
            level = max(level, a.to)
        acted.add(p)
    # the next street's c-bet needs a c-bet that was not raised
    return cbettor if cbettor and bettors and bettors[-1] == cbettor else None


def r_ante(h, name):
    return sum(a.amount for a in h.actions if a.name == name and a.kind == "ante")
