import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim.oppl.runtime import Runtime


class FakeProvider:
    def __init__(self, **values):
        self.values = values
        self.hole = [(14, 3), (13, 3)]          # AsKs
        self.board = [(13, 0), (7, 1), (2, 2)]  # Kc 7d 2h

    def known(self, name):
        return name in self.values

    def symbol(self, name):
        return self.values[name]

    def hole_cards(self):
        return self.hole

    def board_cards(self):
        return self.board


def runtime(text, **values):
    prov = FakeProvider(**values)
    rt = Runtime(prov)
    with tempfile.NamedTemporaryFile("w", suffix=".ohf", delete=False) as f:
        f.write(text + "\n##Others##\n1\n")
    rt.load_file(f.name)
    rt.finish_loading()
    os.unlink(f.name)
    return rt


class OpenPPLTest(unittest.TestCase):
    def ev(self, expr, **v):
        rt = runtime(f"##f$x##\n{expr}\n", **v)
        self.assertEqual(rt.errors, [])
        return rt.evaluate("f$x")

    def test_precedence(self):
        self.assertEqual(self.ev("2*3 + 4*5"), 26)
        self.assertEqual(self.ev("2 - 3 - 4"), -5)
        self.assertEqual(self.ev("2 ** 3 ** 2"), 512)          # right associative
        self.assertEqual(self.ev("-2 ** 2"), 4)                # -2 is a number literal
        self.assertEqual(self.ev("1 + 2 > 2 ? 10 : 20"), 10)
        self.assertEqual(self.ev("NOT 0 AND 0"), 0)            # (NOT 0) AND 0
        self.assertEqual(self.ev("NOT 5 > 3"), 0)              # (NOT 5) > 3, as OpenHoldem
        self.assertEqual(self.ev("7 % 3"), 1)                  # modulo outside OpenPPL functions

    def test_comparisons(self):
        self.assertEqual(self.ev("0.1 + 0.2 = 0.3"), 1)       # epsilon
        self.assertEqual(self.ev("0.1 + 0.2 != 0.3"), 0)      # fixed: negation of =

    def test_when_sequences(self):
        rt = runtime("""##f$x##
WHEN a = 1
  WHEN b = 1 RETURN 10 FORCE
  WHEN b = 2 RETURN 20 FORCE
WHEN Others
  WHEN b = 2 SET user_seen
  WHEN user_seen RETURN 30 FORCE
  WHEN Others RETURN 40 FORCE
""", a=1, b=2)
        self.assertEqual(rt.errors, [])
        self.assertEqual(rt.evaluate("f$x"), 20)
        rt.provider.values["a"] = 0
        rt.new_decision()
        self.assertEqual(rt.evaluate("f$x"), 30)                 # SET falls through

    def test_default_fallback_and_memory(self):
        rt = runtime("""##f$preflop##
WHEN Others SET me_st_Counter_5
WHEN x = 1 RETURN 1 FORCE

##DefaultPreflop##
me_re_Counter + 2
""", x=0)
        self.assertEqual(rt.evaluate("f$preflop"), 7)

    def test_raise_actions(self):
        rt = runtime("""##f$x##
WHEN Others RaiseBy 50% FORCE
""", ncallbets=2, bet=1, pot=10, nbetstocall=2)
        # ncallbets + 0.5 * (pot/bet + nbetstocall) = 2 + 0.5*12
        self.assertEqual(rt.evaluate("f$x"), 8)

    def test_hand_board_expressions(self):
        rt = runtime("##f$x##\nhand$AKsuited AND board$K AND NOT hand$AA AND hand$AsKs\n")
        self.assertEqual(rt.evaluate("f$x"), 1)

    def test_shanky_mode(self):
        rt = runtime("""custom
preflop
When raises = 0 And hand = AK RaiseMax Force
When Others Fold Force

##RaiseMax##
-1000009
##Fold##
-1000001
##Raises##
raises_oh
""", raises_oh=0)
        self.assertEqual(rt.errors, [])
        self.assertEqual(rt.evaluate("f$preflop"), -1000009)


if __name__ == "__main__":
    unittest.main()
