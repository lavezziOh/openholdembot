"""Population profile of the real opponents in the local PokerTracker 4 database,
with OpenHoldem's PokerTracker stat definitions.

    python tools/pt_population.py [min_hands]

For every limit group and stat: pooled value (all hands of all players) and the
distribution over players with at least `min_hands` hands (and >= 15 opportunities
for that stat). Hero accounts (flg_hero) are excluded. Output: ohsim/data/population.json
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim import ptdb

GROUPS = {                      # id_limit lists (see cash_limit)
    "iPoker 0.02/0.04 6max": [10],
    "iPoker 0.05/0.10 6max": [11],
    "Stars Zoom 0.02/0.05 6max": [6],
    "Stars Zoom 0.05/0.10 6max": [3],
}
SHOW = ["vpip", "pfr", "rfi", "3bet", "fold_to_3bet", "4bet", "steal_attempt", "bb_fold_to_steal",
        "sb_fold_to_steal", "flop_seen", "flop_cbet", "flop_fold_to_cbet", "turn_fold_to_cbet",
        "river_fold_to_cbet", "flop_af", "turn_af", "river_af", "total_af", "wtsd", "wssd"]
OUT = os.path.join(os.path.dirname(__file__), "..", "ohsim", "data", "population.json")


def main(min_hands=150):
    defs = ptdb.definitions()
    heroes = {r["id_player"] for r in ptdb.query(
        "select distinct id_player from cash_hand_player_statistics where flg_hero")}
    result = {}
    for gname, limits in GROUPS.items():
        where = f"S.id_limit IN ({','.join(map(str, limits))}) AND NOT S.flg_hero"
        hands = {r["pid"]: int(r["result"]) for r in ptdb.query(
            f"SELECT S.id_player as pid, count(*) as result FROM cash_hand_player_statistics S "
            f"WHERE {where} GROUP BY S.id_player")}
        regulars = {p for p, n in hands.items() if n >= min_hands and p not in heroes}
        g = {"hands": sum(hands.values()) // 6, "players": len(hands), "players_min_hands": len(regulars),
             "stats": {}}
        for name, q in defs.items():
            if name == "hands":
                continue
            try:
                rows = ptdb.query(ptdb.per_player_sql(q["sql"], where))
            except Exception as e:                # noqa: BLE001 - report and go on
                g["stats"][name] = {"error": str(e)[:200]}
                continue
            if rows and "num" in rows[0]:
                num = sum(float(r["num"] or 0) for r in rows)
                den = sum(float(r["den"] or 0) for r in rows)
                vals = [float(r["num"]) / float(r["den"]) for r in rows
                        if r["pid"] in regulars and r["den"] and float(r["den"]) >= 15]
                st = {"pooled": num / den if den else None, "opportunities": den}
            else:
                vals = [float(r["result"]) for r in rows if r["pid"] in regulars and float(r["result"]) >= 0]
                st = {"pooled": None}
            if vals:
                st.update({f"p{p}": float(np.percentile(vals, p)) for p in (10, 25, 50, 75, 90)})
                st["n_players"] = len(vals)
            g["stats"][name] = st
        result[gname] = g
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(result, open(OUT, "w"), indent=1)
    return result


def show(result):
    for gname, g in result.items():
        print(f"\n=== {gname}: ~{g['hands']} hands, {g['players']} players, "
              f"{g['players_min_hands']} with enough hands")
        print(f"{'stat':<20}{'pooled':>8}   {'p10':>6}{'p25':>6}{'p50':>6}{'p75':>6}{'p90':>6}  players")
        for name in SHOW:
            st = g["stats"].get(name, {})
            if "error" in st or not st:
                print(f"{name:<20}  n/a {st.get('error', '')[:60]}")
                continue
            ratio = name.endswith("_af")
            f = (lambda x: f"{x:6.2f}") if ratio else (lambda x: f"{100 * x:6.1f}")
            pooled = f(st["pooled"]) if st.get("pooled") is not None else "     -"
            dist = "".join(f(st[k]) if k in st else "     -" for k in ("p10", "p25", "p50", "p75", "p90"))
            print(f"{name:<20}{pooled:>8}   {dist}  {st.get('n_players', 0):>5}")


if __name__ == "__main__":
    show(main(int(sys.argv[1]) if len(sys.argv) > 1 else 150))
