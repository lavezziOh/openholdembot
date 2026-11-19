"""Consistency check of exported PokerStars hand histories.

For every hand: chips put in (blinds, antes, calls, bets, raises) - uncalled
== Total pot == sum collected, and every summary line matches the actions.
    python tools/check_hh.py folder
"""
import glob
import re
import sys

MONEY = r"\$([0-9]+(?:\.[0-9]+)?)"


def check(text):
    errors = 0
    hands = [h for h in text.split("\n\n\n") if h.strip()]
    for h in hands:
        put = {}
        street_bet = {}
        uncalled = collected = 0.0
        total = None
        for line in h.splitlines():
            if line.startswith("*** ") and any(s in line for s in ("FLOP", "TURN", "RIVER")):
                street_bet = {}
            m = re.match(r"(.+?): (posts small blind|posts big blind|posts the ante|calls|bets) " + MONEY, line)
            if m:
                name, kind, x = m.group(1), m.group(2), float(m.group(3))
                put[name] = put.get(name, 0) + x
                if kind != "posts the ante":
                    street_bet[name] = street_bet.get(name, 0) + x
                continue
            m = re.match(r"(.+?): raises " + MONEY + " to " + MONEY, line)
            if m:
                name, to = m.group(1), float(m.group(3))
                add = to - street_bet.get(name, 0)
                put[name] = put.get(name, 0) + add
                street_bet[name] = to
                continue
            m = re.match(r"Uncalled bet \(" + MONEY + r"\) returned to (.+)", line)
            if m:
                uncalled += float(m.group(1))
                continue
            m = re.match(r"(.+?) collected " + MONEY + " from pot", line)
            if m:
                collected += float(m.group(2))
                continue
            m = re.match(r"Total pot " + MONEY, line)
            if m:
                total = float(m.group(1))
        pot = sum(put.values()) - uncalled
        if total is None or abs(pot - total) > 0.011 or abs(collected - total) > 0.011:
            errors += 1
            if errors <= 3:
                print(f"MISMATCH put-uncalled={pot:.2f} total={total} collected={collected:.2f}\n{h}\n")
    return len(hands), errors


if __name__ == "__main__":
    n = e = 0
    for f in glob.glob(sys.argv[1] + "/*.txt"):
        a, b = check(open(f, encoding="utf-8").read())
        n += a
        e += b
    print(f"{n} hands checked, {e} inconsistent")
