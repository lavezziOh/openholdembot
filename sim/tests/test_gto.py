import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim import handeval
from ohsim.gto import npeval, hands, equity, pushfold
from ohsim.gto.cfr import range_pct


class GTOTest(unittest.TestCase):
    def test_numpy_evaluator_matches_handeval(self):
        rng = random.Random(3)
        rows = [rng.sample(range(52), 7) for _ in range(20000)]
        s = npeval.score(np.array(rows))
        for row, v in zip(rows, s):
            self.assertEqual(v, handeval.score([((c >> 2) + 2, c & 3) for c in row]))

    def test_hand_classes(self):
        self.assertEqual(hands.N, 169)
        self.assertEqual(hands.COUNT.sum(), 1326)

    @unittest.skipUnless(os.path.exists(equity.OUT), "equity table not computed")
    def test_equity_vs_random(self):
        E, W = equity.load()
        vr = (E * W).sum(1) / W.sum(1)
        for h, ref in {"AA": 0.852, "KK": 0.824, "72o": 0.346, "32o": 0.323}.items():
            self.assertAlmostEqual(vr[hands.INDEX[h]], ref, delta=0.003)

    @unittest.skipUnless(os.path.exists(equity.OUT), "equity table not computed")
    def test_pushfold_nash(self):
        s, push, call = pushfold.solve(10, iterations=500)
        self.assertLess(s.exploitability(), 0.001)
        self.assertAlmostEqual(range_pct(push), 0.583, delta=0.01)
        self.assertAlmostEqual(range_pct(call), 0.374, delta=0.01)


if __name__ == "__main__":
    unittest.main()
