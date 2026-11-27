"""Opponents with known leaks, generated from the base (GTO preflop + a postflop profile).

    python -m ohsim.leaks C:/Users/USER/Desktop/Rev024.ohf  out_folder

Every archetype = the base with
  * a deformed preflop solution (tighter / looser / more aggressive ranges),
    regenerated with gto.to_oppl, so the size of the leak is known exactly;
  * a postflop "leak layer": a few rules at the top of the flop/turn/river
    sections that override the base decision with a given probability.

Archetypes (ARCHETYPES below, all parameters editable):
  nit       tight preflop, folds too much postflop without a strong hand
  station   limps and calls a lot preflop, almost never folds a pair or a draw, passive
  maniac    opens/3bets/4bets too much, bets and raises too much postflop
  folder    GTO preflop, folds too much to bets postflop (fit or fold)
  nobluff   GTO preflop, never bluffs on turn and river
"""
import copy
import json
import os
import sys

import numpy as np

from .gto import equity, hands
from .gto.preflop6 import OUT as SOLUTION
from .gto.to_oppl import build_profile

# ---------------------------------------------------------------- hand strength in OPPL
STRONG = ("(HaveTopPair OR HaveOverPair OR HaveTwoPair OR HaveSet OR HaveTrips OR HaveStraight"
          " OR HaveFlush OR HaveFullHouse OR HaveQuads OR HaveStraightFlush)")
VERY_STRONG = ("(HaveTwoPair OR HaveSet OR HaveTrips OR HaveStraight OR HaveFlush OR HaveFullHouse"
               " OR HaveQuads OR HaveStraightFlush)")
DRAW = "(HaveFlushDraw OR HaveOpenEndedStraightDraw OR HaveDoubleGutshotDraw)"
PAIR_OR_DRAW = f"(HavePair OR {DRAW})"


def _r(p):
    return f"randomheartbeat < {p:.2f}"


def postflop_layer(kind, p):
    """Rules (Shanky syntax) for each postflop street."""
    facing, free = "AmountToCall > 0", "AmountToCall = 0"
    rules = {"flop": [], "turn": [], "river": []}
    for st in rules:
        R = rules[st]
        if kind in ("nit", "folder"):
            R.append(f"WHEN {facing} AND NOT {STRONG} AND {_r(p)} Fold FORCE")
        elif kind == "station":
            R.append(f"WHEN {facing} AND NOT {VERY_STRONG} AND ({PAIR_OR_DRAW} OR {_r(p * 0.4)}) Call FORCE")
            R.append(f"WHEN {free} AND NOT {VERY_STRONG} Check FORCE")
        elif kind == "maniac":
            R.append(f"WHEN {free} AND {_r(p)} RaiseBy 75% FORCE")
            R.append(f"WHEN {facing} AND {_r(p * 0.35)} RaiseBy 100% FORCE")
            R.append(f"WHEN {facing} AND {PAIR_OR_DRAW} Call FORCE")
        elif kind == "nobluff" and st in ("turn", "river"):
            R.append(f"WHEN {free} AND NOT HavePair Check FORCE")
    return {k: v for k, v in rules.items() if v}


# ---------------------------------------------------------------- preflop deformations
def _kind(path):
    acts = [s.split("_")[1] for s in path.strip("/").split("/") if s]
    if "open" not in acts:
        return "rfi"
    if "3bet" not in acts:
        return "vs_open"
    if "4bet" not in acts:
        return "vs_3bet"
    return "vs_4bet" if "allin" not in acts else "vs_allin"


class Deformer:
    def __init__(self):
        E, W = equity.load()
        self.strength = (E * W).sum(1) / W.sum(1)        # equity vs a random hand
        self.order = np.argsort(-self.strength)          # strongest first
        self.count = hands.COUNT / hands.COUNT.sum()

    def tighten(self, acts, keep):
        """Keep only the strongest `keep` share of the non-fold range; the rest folds."""
        nonfold = sum(np.array(v) for a, v in acts.items() if a != "fold")
        total = (nonfold * self.count).sum()
        cum = 0.0
        for i in self.order:
            cum += nonfold[i] * self.count[i]
            if cum > keep * total + 1e-12:
                for a in acts:
                    if a != "fold":
                        acts["fold"][i] += acts[a][i]
                        acts[a][i] = 0.0

    def widen(self, acts, label, add):
        """Move `add` (share of all combos) from fold to `label`, strongest folding hands first."""
        acts.setdefault(label, [0.0] * hands.N)
        left = add
        for i in self.order:
            if left <= 0:
                break
            f = acts["fold"][i]
            if f <= 0:
                continue
            take = min(f, left / self.count[i])
            acts["fold"][i] -= take
            acts[label][i] += take
            left -= take * self.count[i]

    def shift(self, acts, src, dst, frac):
        acts.setdefault(dst, [0.0] * hands.N)
        for i in range(hands.N):
            x = acts[src][i] * frac
            acts[src][i] -= x
            acts[dst][i] += x


def deform(data, spec):
    """spec: {node kind: [(operation, args...), ...]}"""
    d = copy.deepcopy(data)
    df = Deformer()
    for path, node in d["nodes"].items():
        kind = _kind(path)
        ops = list(spec.get(kind, []))
        if kind == "vs_open" and node["player"] == "BB":
            ops += spec.get("vs_open_bb", [])          # extra deformation of the big blind defence
        acts = node["actions"]
        for op, *args in ops:
            if op == "tighten":
                df.tighten(acts, *args)
            elif op == "widen":
                label = args[0]
                if label == "aggr":                      # the node's raise action
                    label = next((a for a in acts if a in ("open", "3bet", "4bet", "allin")), None)
                    if label is None:
                        continue
                df.widen(acts, label, *args[1:])
            elif op == "shift":
                src, dst, frac = args
                if src == "aggr":
                    src = next((a for a in acts if a in ("open", "3bet", "4bet", "allin")), None)
                    if src is None:
                        continue
                if src in acts:
                    df.shift(acts, src, dst, frac)
    return d


# Calibrated from PokerTracker NL50 data (168 players, 200+ hands each):
#   reg:     vpip 24.6, 3bet 10.6, fcbet 61, fold_fcbet 41, steal 36, f_raise 15.5
#   nit:     vpip 17.7, 3bet  6.7, fcbet 59, fold_fcbet 44, steal 27, f_raise  4.3
#   passive: vpip 24.9, 3bet  4.7, fcbet 53, fold_fcbet 49, steal 30, f_raise 13.3
#   fish:    vpip 47.7, 3bet  4.6, fcbet 55, fold_fcbet 60, steal 22, f_raise 12.1
#   lag:     vpip 35.1, 3bet 12.1, fcbet 58, fold_fcbet 37, steal 47, f_raise  2.0
ARCHETYPES = {
    "nit": {"preflop": {"rfi": [("tighten", 0.72)], "vs_open": [("tighten", 0.55)],
                        "vs_3bet": [("tighten", 0.55)], "vs_4bet": [("tighten", 0.65)]},
            "postflop": ("nit", 0.44)},
    "station": {"preflop": {"rfi": [("tighten", 0.6), ("widen", "call", 0.12)],
                            "vs_open": [("shift", "aggr", "call", 0.55), ("widen", "call", 0.15)],
                            "vs_3bet": [("widen", "call", 0.08)]},
                "postflop": ("station", 0.49)},
    "maniac": {"preflop": {"rfi": [("widen", "aggr", 0.12)], "vs_open": [("widen", "aggr", 0.08)],
                           "vs_3bet": [("widen", "aggr", 0.04)]},
               "postflop": ("maniac", 0.37)},
    "folder": {"preflop": {}, "postflop": ("folder", 0.60)},
    "nobluff": {"preflop": {}, "postflop": ("nobluff", 1.0)},
    "fish": {"preflop": {"rfi": [("widen", "call", 0.25)],
                         "vs_open": [("shift", "aggr", "call", 0.4), ("widen", "call", 0.25)],
                         "vs_3bet": [("widen", "call", 0.12)]},
             "postflop": ("station", 0.60)},
    "reg": {"preflop": {}, "postflop": ("nit", 0.41)},
}


def build_all(base_profile, out_dir, names=None, solution=SOLUTION):
    data = json.load(open(solution))
    os.makedirs(out_dir, exist_ok=True)
    made = {}
    for name, a in ARCHETYPES.items():
        if names and name not in names:
            continue
        dst = os.path.join(out_dir, f"Leak_{name}.ohf")
        kind, p = a["postflop"]
        build_profile(base_profile, dst, data=deform(data, a["preflop"]),
                      postflop=postflop_layer(kind, p),
                      title=f"LEAK archetype '{name}' (ohsim.leaks): {a}")
        made[name] = dst
    return made


if __name__ == "__main__":
    for name, path in build_all(sys.argv[1], sys.argv[2], sys.argv[3:] or None).items():
        print(f"{name:8} -> {path}")
