"""Player types of the real population (local PokerTracker 4 database).

    python tools/pt_clusters.py [min_hands]

Players with >= min_hands real-money 6-max hands (hero excluded) are grouped by
VPIP / PFR into nit, reg, lag, fish (loose-passive) and maniac; for every group:
share of players and of hands, and the median of the main stats
(OpenHoldem definitions). Output: ohsim/data/population_types.json
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim import ptdb

LIMITS = "3,6,10,11,13"          # real-money 6-max (see cash_limit)
STATS = ["vpip", "pfr", "3bet", "fold_to_3bet", "steal_attempt", "bb_fold_to_steal", "sb_fold_to_steal",
         "flop_cbet", "flop_fold_to_cbet", "turn_fold_to_cbet", "total_af", "wtsd", "wssd", "rfi"]
OUT = os.path.join(os.path.dirname(__file__), "..", "ohsim", "data", "population_types.json")


def classify(v, p):
    if v >= 0.40 and p < 0.5 * v:
        return "fish"
    if v >= 0.35 and p >= 0.5 * v:
        return "maniac"
    if v < 0.18:
        return "nit"
    if v >= 0.27 and p >= 0.20:
        return "lag"
    if p < 0.5 * v:
        return "fish"
    return "reg"


def main(min_hands=100):
    defs = ptdb.definitions()
    where = f"S.id_limit IN ({LIMITS}) AND NOT S.flg_hero"
    hands = {r["pid"]: int(r["result"]) for r in ptdb.query(
        f"SELECT S.id_player as pid, count(*) as result FROM cash_hand_player_statistics S "
        f"WHERE {where} GROUP BY S.id_player")}
    players = {p for p, n in hands.items() if n >= min_hands}
    vals = {s: {} for s in STATS}
    raw = {s: {} for s in STATS}
    for s in STATS:
        for r in ptdb.query(ptdb.per_player_sql(defs[s]["sql"], where)):
            if r["pid"] in players and r.get("den") and float(r["den"]) > 0:
                vals[s][r["pid"]] = float(r["num"]) / float(r["den"])
                raw[s][r["pid"]] = (float(r["num"]), float(r["den"]))
    groups = {}
    for p in players:
        if p in vals["vpip"] and p in vals["pfr"]:
            groups.setdefault(classify(vals["vpip"][p], vals["pfr"][p]), []).append(p)
    total_hands = sum(hands[p] for p in players)
    out = {"min_hands": min_hands, "players": len(players), "hands": total_hands, "types": {}}
    for g, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        t = {"players": len(ps), "share_players": len(ps) / len(players),
             "share_hands": sum(hands[p] for p in ps) / total_hands, "median": {}, "pooled": {}}
        for s in STATS:
            v = [vals[s][p] for p in ps if p in vals[s]]
            if v:
                t["median"][s] = float(np.median(v))
            num = sum(raw[s][p][0] for p in ps if p in raw[s])
            den = sum(raw[s][p][1] for p in ps if p in raw[s])
            if den:
                t["pooled"][s] = num / den
        out["types"][g] = t
    json.dump(out, open(OUT, "w"), indent=1)
    return out


def show(out):
    print(f"{out['players']} players with >= {out['min_hands']} hands ({out['hands']} player-hands)")
    print(f"{'type':<8}{'players':>8}{'%hands':>8} " + "".join(f"{s[:9]:>10}" for s in STATS))
    for g, t in out["types"].items():
        cells = "".join((f"{t['pooled'][s]:10.2f}" if s.endswith("_af") else f"{100 * t['pooled'][s]:10.1f}")
                        if s in t["pooled"] else "         -" for s in STATS)
        print(f"{g:<8}{t['players']:>8}{100 * t['share_hands']:>7.1f}% {cells}")


if __name__ == "__main__":
    show(main(int(sys.argv[1]) if len(sys.argv) > 1 else 100))
