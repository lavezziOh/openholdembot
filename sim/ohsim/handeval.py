"""Hand evaluation for 1..7 cards. Cards are (rank 2..14, suit 0..3).

evaluate(cards) -> (category, ranks)  category 0..8, ranks = significant ranks
                                       of the best 5-card hand (best first)
score(cards)    -> int, larger is better (comparable across hands)
pokerval(...)   -> OpenHoldem 'pokerval' encoding (MagicNumbers.h)
"""
from itertools import combinations

HIGHCARD, PAIR, TWOPAIR, TRIPS, STRAIGHT, FLUSH, FULLHOUSE, QUADS, STFLUSH = range(9)

PV_FLAGS = {HIGHCARD: 0x00000001, PAIR: 0x01000000, TWOPAIR: 0x02000000,
            TRIPS: 0x04000000, STRAIGHT: 0x08000000, FLUSH: 0x10000000,
            FULLHOUSE: 0x20000000, QUADS: 0x40000000, STFLUSH: 0x80000000}

_STRAIGHT_MASKS = [(0b11111 << (hi - 4), hi) for hi in range(14, 5, -1)]
_WHEEL = (1 << 14) | (1 << 5) | (1 << 4) | (1 << 3) | (1 << 2)


def _straight_high(mask):
    for m, hi in _STRAIGHT_MASKS:
        if mask & m == m:
            return hi
    if mask & _WHEEL == _WHEEL:
        return 5
    return 0


def evaluate(cards):
    n = len(cards)
    counts = {}
    suits = [[], [], [], []]
    mask = 0
    for r, s in cards:
        counts[r] = counts.get(r, 0) + 1
        suits[s].append(r)
        mask |= 1 << r
    # straight flush / flush
    if n >= 5:
        for sr in suits:
            if len(sr) >= 5:
                smask = 0
                for r in sr:
                    smask |= 1 << r
                hi = _straight_high(smask)
                if hi:
                    return STFLUSH, (hi,)
                return FLUSH, tuple(sorted(sr, reverse=True)[:5])
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    if groups[0][1] == 4:
        q = groups[0][0]
        kick = max((r for r in counts if r != q), default=0)
        return QUADS, (q, kick) if kick else (q,)
    if groups[0][1] == 3 and len(groups) > 1 and groups[1][1] >= 2:
        t = groups[0][0]
        p = max(r for r, c in counts.items() if c >= 2 and r != t)
        return FULLHOUSE, (t, p)
    if n >= 5:
        hi = _straight_high(mask)
        if hi:
            return STRAIGHT, (hi,)
    if groups[0][1] == 3:
        t = groups[0][0]
        kick = sorted((r for r in counts if r != t), reverse=True)[:2]
        return TRIPS, (t, *kick)
    pairs = sorted((r for r, c in counts.items() if c == 2), reverse=True)
    if len(pairs) >= 2:
        hp, lp = pairs[0], pairs[1]
        kick = sorted((r for r in counts if r not in (hp, lp)), reverse=True)[:1]
        return TWOPAIR, (hp, lp, *kick)
    if len(pairs) == 1:
        p = pairs[0]
        kick = sorted((r for r in counts if r != p), reverse=True)[:3]
        return PAIR, (p, *kick)
    return HIGHCARD, tuple(sorted(counts, reverse=True)[:5])


def score(cards):
    cat, ranks = evaluate(cards)
    v = cat
    for i in range(5):
        v = (v << 4) | (ranks[i] if i < len(ranks) else 0)
    return v


def five_ranks(cat, ranks):
    """The 5 rank slots of the best hand, in OpenHoldem pokerval order."""
    if cat in (STRAIGHT, STFLUSH):
        hi = ranks[0]
        return [hi - i if hi - i >= 2 else 14 for i in range(5)] if hi > 5 else [5, 4, 3, 2, 1]
    if cat == QUADS:
        return [ranks[0]] * 4 + list(ranks[1:2])
    if cat == FULLHOUSE:
        return [ranks[0]] * 3 + [ranks[1]] * 2
    if cat == TRIPS:
        return [ranks[0]] * 3 + list(ranks[1:])
    if cat == TWOPAIR:
        return [ranks[0]] * 2 + [ranks[1]] * 2 + list(ranks[2:])
    if cat == PAIR:
        return [ranks[0]] * 2 + list(ranks[1:])
    return list(ranks)


def pokerval(cards, hole=()):
    """Returns (pokerval, pcbits). pcbits: bit (4-i) set if slot i uses a hole card rank."""
    if not cards:
        return 0, 0
    cat, ranks = evaluate(cards)
    slots = five_ranks(cat, ranks)
    pv = PV_FLAGS[cat] if cat != HIGHCARD else 0
    if cat == HIGHCARD:
        pv = PV_FLAGS[HIGHCARD]
    for i, r in enumerate(slots[:5]):
        pv += (r if r != 1 else 1) << (16 - 4 * i)
    hole_ranks = [r for r, _ in hole]
    pcbits = 0
    used = list(hole_ranks)
    for i, r in enumerate(slots[:5]):
        rr = 14 if r == 1 else r
        if rr in used:
            pcbits |= 1 << (4 - i)
            used.remove(rr)
    return pv, pcbits


def omaha_best(hole, board):
    """Best 5-card hand in Omaha: exactly 2 hole cards + exactly 3 board cards.
    Returns the 5 cards (with fewer than 3 board cards: the best 2 hole cards + the board)."""
    if len(board) < 3:
        best, best_sc = None, -1
        for h in combinations(hole, 2):
            sc = score(list(h) + list(board))
            if sc > best_sc:
                best, best_sc = list(h) + list(board), sc
        return best
    best, best_sc = None, -1
    for h in combinations(hole, 2):
        for b in combinations(board, 3):
            cs = list(h) + list(b)
            sc = score(cs)
            if sc > best_sc:
                best, best_sc = cs, sc
    return best


def omaha_score(hole, board):
    return score(omaha_best(hole, board))


def best_hand_category(cards):
    return evaluate(cards)[0] if cards else HIGHCARD


def compare_7(a, b):
    sa, sb = score(a), score(b)
    return (sa > sb) - (sa < sb)


# Memoized versions for symbols (same cards are evaluated many times per street).
_EVAL_CACHE = {}
_PV_CACHE = {}


def evaluate_cached(cards):
    key = tuple(cards)
    v = _EVAL_CACHE.get(key)
    if v is None:
        if len(_EVAL_CACHE) > 200000:
            _EVAL_CACHE.clear()
        v = _EVAL_CACHE[key] = evaluate(cards)
    return v


def pokerval_cached(cards, hole=()):
    key = (tuple(cards), tuple(hole))
    v = _PV_CACHE.get(key)
    if v is None:
        if len(_PV_CACHE) > 200000:
            _PV_CACHE.clear()
        v = _PV_CACHE[key] = pokerval(cards, hole)
    return v
