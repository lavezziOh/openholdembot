"""Compare ohsim.ptrows with the columns PokerTracker 4 stored for the same real hands.

    python tools/validate_ptrows.py [n_hands] [limit_ids]
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim import ptdb
from ohsim.handlog import parse_pokerstars
from ohsim.ptrows import rows

SKIP = {"action_p", "action_f", "action_t", "action_r"}


def main(n=3000, limits="3,6,9,13"):
    hands = ptdb.query(f"""select h.id_hand, hh.history from cash_hand_summary h
        join cash_hand_histories hh on hh.id_hand=h.id_hand
        where h.id_limit in ({limits}) order by h.id_hand limit {n}""")
    ids = (f"select id_hand from cash_hand_summary where id_limit in ({limits}) "
           f"order by id_hand limit {n}")
    sample = rows_for_columns()
    cols = [c for c in sample if c not in SKIP]
    pt = ptdb.query(f"""select s.id_hand, p.player_name, {', '.join('s.' + c for c in cols)},
        lp.action as action_p, lf.action as action_f, lt.action as action_t, lr.action as action_r
        from cash_hand_player_statistics s join player p on p.id_player=s.id_player
        join lookup_actions lp on lp.id_action=s.id_action_p join lookup_actions lf on lf.id_action=s.id_action_f
        join lookup_actions lt on lt.id_action=s.id_action_t join lookup_actions lr on lr.id_action=s.id_action_r
        where s.id_hand in ({ids})""")
    by_hand = defaultdict(dict)
    for r in pt:
        by_hand[r["id_hand"]][r["player_name"]] = r
    bad, total, examples = Counter(), Counter(), defaultdict(list)
    parsed = 0
    for x in hands:
        h = parse_pokerstars(x["history"])
        if h is None:
            continue
        parsed += 1
        ours = rows(h)
        for name, theirs in by_hand[x["id_hand"]].items():
            if name not in ours:
                bad["<missing player>"] += 1
                continue
            mine = ours[name]
            for c in cols + sorted(SKIP):
                a, b = mine.get(c), theirs[c]
                total[c] += 1
                if not same(a, b):
                    bad[c] += 1
                    if len(examples[c]) < 3:
                        examples[c].append((x["id_hand"], name, a, b, mine["action_p"], mine["action_f"]))
    print(f"{parsed}/{len(hands)} hands parsed, {len(pt)} player rows")
    for c in sorted(total, key=lambda c: -bad[c]):
        if bad[c]:
            print(f"  {c:<28} {bad[c]:6} / {total[c]}   e.g. {examples[c][:2]}")
    print("columns matching 100%:", sum(1 for c in total if not bad[c]), "of", len(total))


def same(a, b):
    if isinstance(a, bool):
        return (b == "t") == a
    if isinstance(a, (int, float)):
        try:
            return abs(float(a) - float(b)) < 0.006
        except ValueError:
            return False
    return (a or "") == (b or "")


def rows_for_columns():
    from ohsim.ptrows import _empty
    return [c for c in _empty() if not c.startswith("action_")] + ["position", "cnt_players",
            "cnt_players_lookup_position", "amt_before"]


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3000, *(sys.argv[2:3] or []))
