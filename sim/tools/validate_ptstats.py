"""Same hands, same OpenHoldem queries: PokerTracker (PostgreSQL) vs ohsim.ptstats (SQLite).

    python tools/validate_ptstats.py [n_hands] [limit_ids]
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim import ptdb
from ohsim.handlog import parse_pokerstars
from ohsim.ptstats import PTStats


def main(n=14000, limits="3,6,9,13"):
    sub = f"select id_hand from cash_hand_summary where id_limit in ({limits}) order by id_hand limit {n}"
    hands = ptdb.query(f"select hh.history from cash_hand_histories hh where hh.id_hand in ({sub}) order by hh.id_hand")
    st = PTStats(refresh_every=200, limit_name="$0.05 NL FAST (6 max)", bb=0.10)
    st.wanted = set(st.defs)
    for x in hands:
        h = parse_pokerstars(x["history"])
        if h:
            st.add_hand(h)
    st.flush()
    worst = []
    for stat, q in ptdb.definitions().items():
        if stat not in st.defs:
            continue
        try:
            rows = ptdb.query(ptdb.per_player_sql(q["sql"], f"S.id_hand in ({sub})"))
        except RuntimeError as e:
            print(f"  {stat}: PokerTracker itself fails on this query ({str(e)[:50]})")
            continue
        names = {r["id_player"]: r["player_name"] for r in ptdb.query(
            f"select distinct p.id_player, p.player_name from player p join cash_hand_player_statistics s on s.id_player=p.id_player where s.id_hand in ({sub})")}
        diffs, total = 0, 0
        ratio = st.defs[stat][1]
        maxd = 0.0
        for r in rows:
            name = names.get(r["pid"])
            if name is None:
                continue
            if ratio:
                den = float(r["den"] or 0)
                if den == 0:
                    continue
                theirs = float(r["num"] or 0) / den
            else:
                theirs = float(r["result"] or 0)
            ours = st.value(name, stat)
            total += 1
            d = abs(ours - theirs)
            if d > 1e-6:
                diffs += 1
                maxd = max(maxd, d)
        worst.append((diffs, stat, total, maxd))
    worst.sort(reverse=True)
    ok = sum(1 for d, *_ in worst if d == 0)
    print(f"{len(worst)} stats compared on {len(hands)} hands: {ok} identical for every player")
    for d, stat, total, maxd in worst:
        if d:
            print(f"  {stat:<40} {d:5} / {total} players differ, max diff {maxd:.4f}")


if __name__ == "__main__":
    main(*(int(a) if i == 0 else a for i, a in enumerate(sys.argv[1:])))
