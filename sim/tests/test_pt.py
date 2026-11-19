import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim.engine import Table, Action, FOLD, CHECK, CALL, RAISE
from ohsim.handlog import from_table, parse_pokerstars
from ohsim.hhexport import export_hand
from ohsim.ptrows import rows
from ohsim.ptstats import PTStats


class RandomPlayer:
    """Plays random legal-ish actions (the engine normalizes them)."""
    def __init__(self, seed):
        self.rng = random.Random(seed)

    def start_hand(self, view): pass
    def new_round(self, view): pass
    def after_action(self, view, action): pass
    def end_hand(self, view, result): pass

    def decide(self, view):
        r = self.rng.random()
        t = view.t
        if r < 0.3:
            return Action(FOLD)
        if r < 0.65:
            return Action(CALL)
        if r < 0.75:
            return Action(CHECK)
        return Action(RAISE, t.current_bet() * self.rng.choice((2, 3, 5)) + t.bb * self.rng.random() * 50)


class PokerTrackerTest(unittest.TestCase):
    def test_export_parse_roundtrip(self):
        """rows(simulator hand) == rows(PokerStars export of the same hand, parsed back)."""
        t = Table([RandomPlayer(i) for i in range(6)], names=[f"P{i}" for i in range(6)], seed=11)
        for _ in range(400):
            res = t.play_hand()
            direct = rows(from_table(t, res))
            parsed = rows(parse_pokerstars(export_hand(t, res, 1000 + t.hand_no)))
            for name, r in direct.items():
                for col, v in r.items():
                    w = parsed[name][col]
                    if isinstance(v, float):
                        self.assertAlmostEqual(v, w, places=2, msg=f"hand {t.hand_no} {name} {col}")
                    else:
                        self.assertEqual(v, w, f"hand {t.hand_no} {name} {col}")

    def test_stats_engine(self):
        st = PTStats(refresh_every=7)
        st.want(["pt_vpip3", "pt_pfr_raischair", "hands"])
        t = Table([RandomPlayer(i) for i in range(6)], names=[f"P{i}" for i in range(6)], seed=3)
        vpip = 0
        for _ in range(200):
            res = t.play_hand()
            st.update(t, res)
            vpip += rows(from_table(t, res))["P0"]["flg_vpip"]
        st.flush()
        self.assertEqual(st.value("P0", "hands"), 200)
        self.assertAlmostEqual(st.value("P0", "vpip_L"), -1.0)         # not wanted -> no data
        self.assertGreater(st.value("P0", "vpip"), 0)
        self.assertEqual(st.value("nobody", "vpip"), -1.0)             # no data -> -1 as OpenHoldem


if __name__ == "__main__":
    unittest.main()
