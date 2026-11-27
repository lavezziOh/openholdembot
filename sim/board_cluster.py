"""
Board clustering via equity analysis.
For each spot (range pair) x board, compute equity distribution of IP vs OOP.
Then cluster boards using k-means with Earth Mover's Distance.

Usage: python board_cluster.py [--spots TYPE] [--clusters N] [--boards N]
  --spots: srp, 3bp, 4bp, all (default: all)
  --clusters: number of clusters (default: 12)
  --boards: number of boards to analyze (default: 1755)
"""
import itertools, json, sys, time, argparse
from collections import defaultdict
import numpy as np

RANKS = "23456789TJQKA"
SUITS = "cdhs"
RANK_VAL = {r: i for i, r in enumerate(RANKS)}
ALL_CARDS = [r + s for r in RANKS for s in SUITS]
CARD_IDX = {c: i for i, c in enumerate(ALL_CARDS)}

try:
    from phevaluator import evaluate_cards
    USE_PHEVAL = True
except ImportError:
    USE_PHEVAL = False

def parse_range(range_str):
    """Parse 'AKs:0.5,QQ:1.0,...' into list of (hand_type, weight)."""
    hands = []
    for part in range_str.split(","):
        if ":" in part:
            h, w = part.split(":")
            hands.append((h.strip(), float(w)))
    return hands

def expand_hand(hand_type):
    """Expand hand type like 'AKs','AKo','AK','AA' to specific card combos."""
    combos = []
    if len(hand_type) == 2 and hand_type[0] == hand_type[1]:
        # Pocket pair: AA, KK, etc.
        r = hand_type[0]
        for s1, s2 in itertools.combinations(SUITS, 2):
            combos.append((r + s1, r + s2))
    elif len(hand_type) == 3 and hand_type[2] == 's':
        r1, r2 = hand_type[0], hand_type[1]
        for s in SUITS:
            combos.append((r1 + s, r2 + s))
    elif len(hand_type) == 3 and hand_type[2] == 'o':
        r1, r2 = hand_type[0], hand_type[1]
        for s1, s2 in itertools.product(SUITS, repeat=2):
            if s1 != s2:
                combos.append((r1 + s1, r2 + s2))
    elif len(hand_type) == 2:
        r1, r2 = hand_type[0], hand_type[1]
        if r1 == r2:
            for s1, s2 in itertools.combinations(SUITS, 2):
                combos.append((r1 + s1, r2 + s2))
        else:
            for s1, s2 in itertools.product(SUITS, repeat=2):
                combos.append((r1 + s1, r2 + s2))
    return combos

def expand_range(parsed_range):
    """Return list of (card1, card2, weight) for all combos."""
    result = []
    for hand_type, weight in parsed_range:
        for c1, c2 in expand_hand(hand_type):
            result.append((c1, c2, weight))
    return result

def generate_all_boards():
    """Generate all 1755 strategically unique flops."""
    boards = set()
    for c1, c2, c3 in itertools.combinations(range(52), 3):
        cards = [ALL_CARDS[c1], ALL_CARDS[c2], ALL_CARDS[c3]]
        ranks = sorted([RANK_VAL[c[0]] for c in cards], reverse=True)
        suits_pattern = []
        suit_map = {}
        for c in cards:
            s = c[1]
            if s not in suit_map:
                suit_map[s] = len(suit_map)
            suits_pattern.append(suit_map[s])
        key = (tuple(ranks), tuple(suits_pattern))
        if key not in boards:
            boards.add(key)
    # Convert back to card strings - pick canonical representative
    result = []
    seen = set()
    canonical_suits = ['h', 'd', 'c', 's']
    for c1, c2, c3 in itertools.combinations(range(52), 3):
        cards = [ALL_CARDS[c1], ALL_CARDS[c2], ALL_CARDS[c3]]
        ranks = sorted([RANK_VAL[c[0]] for c in cards], reverse=True)
        suits_pattern = []
        suit_map = {}
        for c in cards:
            s = c[1]
            if s not in suit_map:
                suit_map[s] = len(suit_map)
            suits_pattern.append(suit_map[s])
        key = (tuple(ranks), tuple(suits_pattern))
        if key not in seen:
            seen.add(key)
            result.append(cards)
    return result

def hand_strength_distribution(board, ip_combos, oop_combos, bins=10):
    """
    For each IP combo, compute hand strength vs OOP range on this board.
    Return histogram (bins) of IP hand strengths weighted by combo weight.
    Hand strength = fraction of OOP combos beaten (flop only, no runout).
    """
    board_set = set(board)
    board_cards = board

    # Filter combos that don't conflict with board
    ip_valid = [(c1, c2, w) for c1, c2, w in ip_combos
                if c1 not in board_set and c2 not in board_set and c1 != c2]
    oop_valid = [(c1, c2, w) for c1, c2, w in oop_combos
                 if c1 not in board_set and c2 not in board_set and c1 != c2]

    if not ip_valid or not oop_valid:
        return np.zeros(bins)

    # Evaluate all OOP hands
    oop_evals = []
    for c1, c2, w in oop_valid:
        if USE_PHEVAL:
            score = evaluate_cards(c1, c2, *board_cards)
        else:
            score = _simple_eval(c1, c2, board_cards)
        oop_evals.append((score, w, c1, c2))

    total_oop_weight = sum(w for _, w, _, _ in oop_evals)
    if total_oop_weight == 0:
        return np.zeros(bins)

    histogram = np.zeros(bins)
    total_ip_weight = 0

    for ip_c1, ip_c2, ip_w in ip_valid:
        ip_set = {ip_c1, ip_c2}
        if USE_PHEVAL:
            ip_score = evaluate_cards(ip_c1, ip_c2, *board_cards)
        else:
            ip_score = _simple_eval(ip_c1, ip_c2, board_cards)

        # Hand strength: fraction of OOP hands beaten (lower score = better in phevaluator)
        wins = 0
        ties = 0
        valid_w = 0
        for oop_score, oop_w, oop_c1, oop_c2 in oop_evals:
            if oop_c1 in ip_set or oop_c2 in ip_set:
                continue
            valid_w += oop_w
            if ip_score < oop_score:
                wins += oop_w
            elif ip_score == oop_score:
                ties += oop_w * 0.5

        if valid_w > 0:
            hs = (wins + ties) / valid_w
            bin_idx = min(int(hs * bins), bins - 1)
            histogram[bin_idx] += ip_w
            total_ip_weight += ip_w

    if total_ip_weight > 0:
        histogram /= total_ip_weight

    return histogram

def _simple_eval(c1, c2, board):
    """Fallback evaluator if phevaluator not available. Very basic."""
    all5 = [c1, c2] + list(board)
    ranks = sorted([RANK_VAL[c[0]] for c in all5], reverse=True)
    return -sum(r * (14 ** i) for i, r in enumerate(ranks))

def earth_movers_distance(h1, h2):
    """1D EMD between two histograms."""
    cdf1 = np.cumsum(h1)
    cdf2 = np.cumsum(h2)
    return np.sum(np.abs(cdf1 - cdf2))

def load_spots(filename="spot_ranges.txt", spot_filter=None):
    """Load spot ranges from file."""
    spots = []
    with open(filename) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            name, pot, stack, ip_str, oop_str = parts[0], parts[1], parts[2], parts[3], parts[4]
            if spot_filter and not name.startswith(spot_filter):
                continue
            ip_range = expand_range(parse_range(ip_str))
            oop_range = expand_range(parse_range(oop_str))
            spots.append({
                "name": name,
                "pot": float(pot),
                "stack": float(stack),
                "ip": ip_range,
                "oop": oop_range,
            })
    return spots

def board_to_str(board):
    return "".join(board)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spots", default="all", help="srp, 3bp, 4bp, all")
    parser.add_argument("--clusters", type=int, default=12)
    parser.add_argument("--boards", type=int, default=1755)
    parser.add_argument("--output", default="board_clusters.json")
    args = parser.parse_args()

    spot_filter = None if args.spots == "all" else args.spots + "_"
    spots = load_spots(spot_filter=spot_filter)
    print(f"Loaded {len(spots)} spots")

    all_boards = generate_all_boards()[:args.boards]
    print(f"Generated {len(all_boards)} unique boards")
    print(f"Using phevaluator: {USE_PHEVAL}")

    # Compute equity distributions for each board (averaged across spots)
    print(f"\nComputing equity distributions for {len(all_boards)} boards x {len(spots)} spots...")
    t0 = time.time()

    board_histograms = {}
    for bi, board in enumerate(all_boards):
        if bi % 100 == 0:
            elapsed = time.time() - t0
            rate = bi / elapsed if elapsed > 0 else 0
            eta = (len(all_boards) - bi) / rate if rate > 0 else 0
            print(f"  Board {bi}/{len(all_boards)} ({rate:.1f}/s, ETA {eta:.0f}s)")

        avg_hist = np.zeros(10)
        for spot in spots:
            h = hand_strength_distribution(board, spot["ip"], spot["oop"], bins=10)
            avg_hist += h
        avg_hist /= len(spots)
        board_histograms[board_to_str(board)] = avg_hist

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s ({len(all_boards)/elapsed:.1f} boards/s)")

    # k-means clustering with EMD
    print(f"\nClustering into {args.clusters} groups...")
    board_keys = list(board_histograms.keys())
    hists = np.array([board_histograms[k] for k in board_keys])

    # Initialize centroids with k-means++
    n = len(hists)
    k = args.clusters
    centroids = [hists[np.random.randint(n)]]
    for _ in range(1, k):
        dists = np.array([min(earth_movers_distance(h, c) for c in centroids) for h in hists])
        probs = dists / dists.sum()
        idx = np.random.choice(n, p=probs)
        centroids.append(hists[idx])
    centroids = np.array(centroids)

    # Iterate
    for iteration in range(50):
        # Assign
        labels = np.zeros(n, dtype=int)
        for i in range(n):
            dists = [earth_movers_distance(hists[i], centroids[j]) for j in range(k)]
            labels[i] = np.argmin(dists)

        # Update centroids
        new_centroids = np.zeros_like(centroids)
        for j in range(k):
            members = hists[labels == j]
            if len(members) > 0:
                new_centroids[j] = members.mean(axis=0)
            else:
                new_centroids[j] = hists[np.random.randint(n)]

        if np.allclose(centroids, new_centroids, atol=1e-6):
            print(f"  Converged at iteration {iteration}")
            break
        centroids = new_centroids

    # Build results
    clusters = defaultdict(list)
    for i, label in enumerate(labels):
        clusters[int(label)].append(board_keys[i])

    # Describe clusters
    print(f"\n{'='*60}")
    print(f"CLUSTER RESULTS ({k} clusters, {len(board_keys)} boards)")
    print(f"{'='*60}")
    for cid in sorted(clusters.keys()):
        members = clusters[cid]
        centroid = centroids[cid]
        # Avg equity of IP
        avg_eq = sum(centroid[i] * (i + 0.5) / 10 for i in range(10))
        # Sample boards
        samples = members[:5]
        print(f"\nCluster {cid}: {len(members)} boards, IP avg equity={avg_eq:.3f}")
        print(f"  Centroid: [{', '.join(f'{v:.3f}' for v in centroid)}]")
        print(f"  Samples: {', '.join(samples)}")

        # Characterize by board properties
        high_counts = {"A": 0, "K": 0, "Q": 0, "J": 0, "T": 0, "low": 0}
        paired_count = 0
        mono_count = 0
        conn_count = 0
        for b in members:
            ranks = [RANK_VAL[b[i]] for i in range(0, len(b), 2)]
            suits = [b[i] for i in range(1, len(b), 2)]
            high = max(ranks)
            if high >= 12: high_counts["A"] += 1
            elif high >= 11: high_counts["K"] += 1
            elif high >= 10: high_counts["Q"] += 1
            elif high >= 9: high_counts["J"] += 1
            elif high >= 8: high_counts["T"] += 1
            else: high_counts["low"] += 1
            if len(set(ranks)) < 3: paired_count += 1
            if len(set(suits)) == 1: mono_count += 1
            ranks_s = sorted(ranks)
            if ranks_s[-1] - ranks_s[0] <= 4: conn_count += 1
        print(f"  High: {dict(high_counts)}, paired={paired_count}, mono={mono_count}, conn={conn_count}")

    # Save
    output = {
        "params": {"spots": args.spots, "clusters": k, "boards": len(board_keys)},
        "clusters": {str(cid): members for cid, members in clusters.items()},
        "centroids": {str(cid): centroid.tolist() for cid, centroid in enumerate(centroids)},
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {args.output}")

if __name__ == "__main__":
    main()
