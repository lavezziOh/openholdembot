import random
import sys, os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim.engine import Table, Action, FOLD, CHECK, CALL, RAISE
from ohsim import handeval as he


class RandomPlayer:
    def __init__(self, seed):
        self.r = random.Random(seed)

    def start_hand(self, v): pass
    def new_round(self, v): pass
    def after_action(self, v, a): pass
    def end_hand(self, v, r): pass

    def decide(self, v):
        x = self.r.random()
        if x < 0.15:
            return Action(FOLD)
        if x < 0.55:
            return Action(CALL)
        if x < 0.70:
            return Action(CHECK)
        return Action(RAISE, v.t.current_bet() * self.r.choice([2, 3, 10]) + 1)


class EngineTest(unittest.TestCase):
    def test_chip_conservation(self):
        for n in (2, 3, 6, 9):
            players = [RandomPlayer(i) for i in range(n)]
            t = Table(players, seed=42 + n, buyin=100)
            for _ in range(1500):
                t.play_hand()
                for s in t.seats:
                    self.assertGreaterEqual(s.stack, -1e-9)
            # zero-sum game, and every stack = buy-in + net result + top-ups
            self.assertAlmostEqual(sum(t.results.values()), 0.0, places=6)
            for s in t.seats:
                self.assertAlmostEqual(s.stack, 100 + t.results[s.name] + t.rebuys[s.name], places=6)

    def test_hand_ranking(self):
        def c(s):
            return [("23456789TJQKA".index(x[0]) + 2, "cdhs".index(x[1])) for x in s.split()]
        order = ["2c 7d 9h Jc Ks 3d 4h",        # high card
                 "2c 2d 9h Jc Ks 3d 4h",        # pair
                 "2c 2d 9h 9c Ks 3d 4h",        # two pair
                 "2c 2d 2h 9c Ks 3d 4h",        # trips
                 "Ac 2d 3h 4c 5s Jd Kh",        # wheel
                 "2c 7c 9c Jc Kc 3d 4h",        # flush
                 "2c 2d 2h 9c 9s 3d 4h",        # full house
                 "2c 2d 2h 2s 9s 3d 4h",        # quads
                 "Ac 2c 3c 4c 5c Jd Kh"]        # straight flush
        scores = [he.score(c(h)) for h in order]
        self.assertEqual(scores, sorted(scores))
        pv, _ = he.pokerval(c("Tc Jc Qc Kc Ac 2d 3h"))
        self.assertEqual(pv, 0x800EDCBA)          # royal flush as in OpenHoldem


if __name__ == "__main__":
    unittest.main()
