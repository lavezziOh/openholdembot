"""Bots that reproduce the player types of the real population.

    python -m ohsim.population calibrate [iterations] [hands]   # tune the knobs, build the profiles
    python -m ohsim.population build                             # build the profiles from saved knobs

Targets: ohsim/data/population_types.json (tools/pt_clusters.py, from the local
PokerTracker database). Each type is the base (GTO preflop + Rev024 postflop)
with a deformed preflop solution and a parametric postflop layer; the knobs are
tuned by simulation until the PokerTracker stats (OpenHoldem definitions) match.
Output: profiles/population/Pop_<type>.ohf, ohsim/data/population_knobs.json
"""
import json
import os
import sys

from .gto.preflop6 import OUT as SOLUTION
from .gto.to_oppl import build_profile
from .leaks import deform, STRONG, VERY_STRONG, DRAW, PAIR_OR_DRAW
from .run import run

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
TYPES_FILE = os.path.join(HERE, "data", "population_types.json")
KNOBS_FILE = os.path.join(HERE, "data", "population_knobs.json")
OUT_DIR = os.path.join(ROOT, "profiles", "population")
BASE_PROFILE = os.path.join(ROOT, "profiles", "Rev024.ohf")
TABLE = ["reg", "reg", "lag", "nit", "fish", "maniac"]     # calibration table (population mix)
TUNED = ["rfi", "vpip", "3bet", "fold_to_3bet", "bb_fold_to_steal", "flop_cbet", "flop_fold_to_cbet",
         "turn_fold_to_cbet", "total_af", "wtsd"]

DEFAULT_KNOBS = {   # starting point, refined by calibrate()
    "open": 1.0,     # RFI multiplier (<1 tighter, >1 looser)
    "limp": 0.0,     # share of combos limped first in
    "vo_call": 0.0,  # share of combos added as calls versus an open
    "vo_3bet": 1.0,  # 3bet multiplier versus an open
    "v3_fold": 0.0,  # >0 move calls to folds versus a 3bet, <0 call more
    "cbet_q": 0.0,   # flop as preflop raiser without a strong hand: check with this probability
    "cbet_b": 0.0,   # flop as preflop raiser: bet with this probability
    "fold_w": 0.0,   # facing a bet with a weak hand (no pair, no draw): fold probability
    "call_pd": 0.0,  # facing a bet with a pair or a draw: call probability
    "float_w": 0.0,  # facing a bet on the flop with nothing: call ("float") probability
    "late_call": 0.0,  # turn/river: facing a bet with a pair or a draw (no strong hand): call probability
    "late_fold": 0.0,  # turn/river: facing a bet without top pair or better: fold probability
    "bb_fold": 0.0,  # big blind versus an open: >0 move calls to folds, <0 defend more
    "aggr_b": 0.0,   # no bet to us: bet probability
    "aggr_r": 0.0,   # facing a bet: raise probability
}


def _r(p, upper=False):
    return f"randomheartbeat > {1 - p:.3f}" if upper else f"randomheartbeat < {p:.3f}"


def preflop_spec(k):
    rfi = []
    if k["open"] < 1:
        rfi.append(("tighten", k["open"]))
    elif k["open"] > 1:
        rfi.append(("widen", "aggr", (k["open"] - 1) * 0.30))
    if k["limp"] > 0:
        rfi.append(("widen", "call", k["limp"]))
    vo = []
    if k["vo_3bet"] < 1:
        vo.append(("shift", "aggr", "fold", 1 - k["vo_3bet"]))
    elif k["vo_3bet"] > 1:
        vo.append(("widen", "aggr", (k["vo_3bet"] - 1) * 0.08))
    if k["vo_call"] > 0:
        vo.append(("widen", "call", k["vo_call"]))
    v3 = []
    if k["v3_fold"] > 0:
        v3.append(("shift", "call", "fold", min(k["v3_fold"], 1.0)))
    elif k["v3_fold"] < 0:
        v3.append(("widen", "call", -k["v3_fold"]))
    bb = []
    if k["bb_fold"] > 0:
        bb.append(("shift", "call", "fold", min(k["bb_fold"], 1.0)))
    elif k["bb_fold"] < 0:
        bb.append(("widen", "call", -k["bb_fold"]))
    return {"rfi": rfi, "vs_open": vo, "vs_3bet": v3, "vs_open_bb": bb}


def postflop_layer(k):
    pfr_free = ("AmountToCall = 0 AND BotsLastPreflopAction = Raise "
                "AND BotsActionsOnThisRoundIncludingChecks = 0")
    rules = {}
    for st in ("flop", "turn", "river"):
        R = []
        if st == "flop" and k["cbet_q"] > 0:
            R.append(f"WHEN {pfr_free} AND NOT {STRONG} AND {_r(k['cbet_q'])} Check FORCE")
        if st == "flop" and k["cbet_b"] > 0:
            R.append(f"WHEN {pfr_free} AND {_r(k['cbet_b'])} RaiseBy 60% FORCE")
        if st == "flop" and k["fold_w"] > 0:
            R.append(f"WHEN AmountToCall > 0 AND NOT {STRONG} AND NOT {PAIR_OR_DRAW} AND {_r(k['fold_w'])} Fold FORCE")
        if st == "flop" and k["call_pd"] > 0:
            R.append(f"WHEN AmountToCall > 0 AND NOT {VERY_STRONG} AND {PAIR_OR_DRAW} AND {_r(k['call_pd'])} Call FORCE")
        if st != "flop" and k["late_fold"] > 0:
            R.append(f"WHEN AmountToCall > 0 AND NOT {STRONG} AND {_r(k['late_fold'])} Fold FORCE")
        if st != "flop" and k["late_call"] > 0:
            R.append(f"WHEN AmountToCall > 0 AND NOT {VERY_STRONG} AND {PAIR_OR_DRAW} AND {_r(k['late_call'])} Call FORCE")
        if st == "flop" and k["float_w"] > 0:
            R.append(f"WHEN AmountToCall > 0 AND AmountToCall <= 75% PotSize AND NOT {STRONG} AND NOT {PAIR_OR_DRAW} "
                     f"AND {_r(k['float_w'])} Call FORCE")
        if k["aggr_r"] > 0:
            R.append(f"WHEN AmountToCall > 0 AND {_r(k['aggr_r'], upper=True)} RaiseBy 100% FORCE")
        if k["aggr_b"] > 0:
            R.append(f"WHEN AmountToCall = 0 AND {_r(k['aggr_b'], upper=True)} RaiseBy 66% FORCE")
        if R:
            rules[st] = R
    return rules


def build(knobs, base=BASE_PROFILE, solution=SOLUTION):
    data = json.load(open(solution))
    os.makedirs(OUT_DIR, exist_ok=True)
    paths = {}
    for t, k in knobs.items():
        dst = os.path.join(OUT_DIR, f"Pop_{t}.ohf")
        build_profile(base, dst, data=deform(data, preflop_spec(k)), postflop=postflop_layer(k),
                      title=f"POPULATION type '{t}' (ohsim.population), knobs {json.dumps(k)}")
        paths[t] = dst
    return paths


def measure(paths, hands, seed=5):
    cfg = {"table": {"hands": hands, "sb": 0.5, "bb": 1.0, "buyin": 100, "seed": seed},
           "report": {"positions": False},
           "player": [{"name": f"Pop_{t}_{i}", "profile": paths[t]} for i, t in enumerate(TABLE)]}
    total, _ = run(cfg, out=lambda *a: None)
    got = {}
    for (name, stat), (num, den) in total["pt"].items():
        t = name.split("_")[1]
        a = got.setdefault(t, {}).setdefault(stat, [0.0, 0.0])
        a[0] += num
        a[1] += den
    return {t: {s: (n / d if d else None) for s, (n, d) in st.items()} for t, st in got.items()}


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def adjust(k, got, tgt):
    """One proportional step of every knob towards its target stat."""
    g = lambda s: got.get(s)
    if g("rfi") and tgt.get("rfi"):
        k["open"] = _clip(k["open"] * (tgt["rfi"] / g("rfi")) ** 0.8, 0.2, 3.0)
    if g("vpip") is not None and tgt.get("vpip"):
        d = tgt["vpip"] - g("vpip")
        if tgt["vpip"] - tgt.get("pfr", 0) > 0.15:          # limpers / callers
            k["limp"] = _clip(k["limp"] + 0.5 * d, 0.0, 0.5)
        k["vo_call"] = _clip(k["vo_call"] + 0.6 * d, 0.0, 0.6)
    if g("3bet") and tgt.get("3bet"):
        k["vo_3bet"] = _clip(k["vo_3bet"] * (tgt["3bet"] / g("3bet")) ** 0.8, 0.05, 4.0)
    if g("fold_to_3bet") is not None and tgt.get("fold_to_3bet"):
        k["v3_fold"] = _clip(k["v3_fold"] + 0.8 * (tgt["fold_to_3bet"] - g("fold_to_3bet")), -0.4, 1.0)
    if g("flop_cbet") is not None and tgt.get("flop_cbet"):
        d = g("flop_cbet") - tgt["flop_cbet"]
        if d > 0:
            k["cbet_q"] = _clip(k["cbet_q"] + d / max(g("flop_cbet"), 0.05) * 1.2, 0.0, 1.0)
            k["cbet_b"] = _clip(k["cbet_b"] - d, 0.0, 1.0)
        else:
            if k["cbet_q"] > 0:
                k["cbet_q"] = _clip(k["cbet_q"] + d * 1.2, 0.0, 1.0)
            else:
                k["cbet_b"] = _clip(k["cbet_b"] - d / max(1 - g("flop_cbet"), 0.05), 0.0, 1.0)
    if g("flop_fold_to_cbet") is not None and tgt.get("flop_fold_to_cbet"):
        d = tgt["flop_fold_to_cbet"] - g("flop_fold_to_cbet")
        if d > 0:
            if k["call_pd"] > 0:
                k["call_pd"] = _clip(k["call_pd"] - 1.5 * d, 0.0, 1.0)
            else:
                k["fold_w"] = _clip(k["fold_w"] + 1.5 * d, 0.0, 1.0)
        else:
            if k["fold_w"] > 0:
                k["fold_w"] = _clip(k["fold_w"] + 1.5 * d, 0.0, 1.0)
            elif k["call_pd"] < 1.0:
                k["call_pd"] = _clip(k["call_pd"] - 1.5 * d, 0.0, 1.0)
            else:                                              # pairs/draws already called: float
                k["float_w"] = _clip(k["float_w"] - 1.5 * d, 0.0, 1.0)
    if g("bb_fold_to_steal") is not None and tgt.get("bb_fold_to_steal"):
        k["bb_fold"] = _clip(k["bb_fold"] + 0.9 * (tgt["bb_fold_to_steal"] - g("bb_fold_to_steal")), -0.4, 1.0)
    if g("turn_fold_to_cbet") is not None and tgt.get("turn_fold_to_cbet"):
        d = tgt["turn_fold_to_cbet"] - g("turn_fold_to_cbet")   # >0: should fold more on later streets
        if d > 0:
            if k["late_call"] > 0:
                k["late_call"] = _clip(k["late_call"] - 1.5 * d, 0.0, 1.0)
            else:
                k["late_fold"] = _clip(k["late_fold"] + 1.5 * d, 0.0, 1.0)
        else:
            if k["late_fold"] > 0:
                k["late_fold"] = _clip(k["late_fold"] + 1.5 * d, 0.0, 1.0)
            else:
                k["late_call"] = _clip(k["late_call"] - 1.5 * d, 0.0, 1.0)
    if g("total_af") and tgt.get("total_af"):
        ratio = tgt["total_af"] / g("total_af")
        if ratio > 1:
            k["aggr_b"] = _clip(k["aggr_b"] + 0.35 * (ratio - 1), 0.0, 0.7)
            k["aggr_r"] = _clip(k["aggr_r"] + 0.12 * (ratio - 1), 0.0, 0.35)
        else:
            k["aggr_b"] = _clip(k["aggr_b"] - 0.35 * (1 - ratio), 0.0, 0.7)
            k["aggr_r"] = _clip(k["aggr_r"] - 0.12 * (1 - ratio), 0.0, 0.35)
            if k["aggr_b"] == 0 and k["aggr_r"] == 0:       # less aggression: call more instead
                k["call_pd"] = _clip(k["call_pd"] + 0.1 * (1 - ratio), 0.0, 1.0)
    return k


def show(got, tgt, out=print):
    out(f"{'type':<8}" + "".join(f"{s[:12]:>14}" for s in TUNED))
    for t in tgt:
        cells = []
        for s in TUNED:
            a, b = got.get(t, {}).get(s), tgt[t].get(s)
            if a is None or b is None:
                cells.append(f"{'-':>14}")
            elif s.endswith("_af"):
                cells.append(f"{a:6.2f}/{b:<6.2f} ")
            else:
                cells.append(f"{100 * a:5.1f}/{100 * b:<5.1f}   ")
        out(f"{t:<8}" + "".join(cells))


def calibrate(iterations=5, hands=60000, out=print):
    types = json.load(open(TYPES_FILE))["types"]
    tgt = {t: types[t]["pooled"] for t in dict.fromkeys(TABLE)}
    knobs = json.load(open(KNOBS_FILE)) if os.path.exists(KNOBS_FILE) else {}
    knobs = {t: {**DEFAULT_KNOBS, **knobs.get(t, {})} for t in tgt}
    for it in range(1, iterations + 1):
        got = measure(build(knobs), hands, seed=100 + it)
        out(f"\n--- iteration {it}: measured / target")
        show(got, tgt, out)
        for t in tgt:
            adjust(knobs[t], got.get(t, {}), tgt[t])
        json.dump(knobs, open(KNOBS_FILE, "w"), indent=1)
    got = measure(build(knobs), hands, seed=999)
    out("\n--- final: measured / target")
    show(got, tgt, out)
    return knobs, got


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "calibrate"
    if cmd == "build":
        print(build(json.load(open(KNOBS_FILE))))
    else:
        calibrate(int(sys.argv[2]) if len(sys.argv) > 2 else 5, int(sys.argv[3]) if len(sys.argv) > 3 else 60000)
