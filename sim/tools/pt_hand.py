"""Print a stored hand with PokerTracker's values of the given columns (debug aid).
    python tools/pt_hand.py id_hand col1 col2 ..."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim import ptdb
from ohsim.handlog import parse_pokerstars
from ohsim.ptrows import rows
hid, cols = sys.argv[1], sys.argv[2:]
text = ptdb.query(f"select history from cash_hand_histories where id_hand={hid}")[0]["history"]
body = text.split("*** SUMMARY")[0]
print("\n".join(l for l in body.splitlines()[2:] if not l.startswith("Seat ")))
mine = rows(parse_pokerstars(text))
r = ptdb.query(f"select p.player_name, s.position, {', '.join('s.' + c for c in cols)} from cash_hand_player_statistics s "
               f"join player p on p.id_player=s.id_player where s.id_hand={hid} order by s.seat")
for x in r:
    n = x["player_name"]
    print(f"  {n:<18} pos {x['position']:>2}  " + "  ".join(f"{c}: PT={x[c]} ours={mine[n].get(c)}" for c in cols))
