"""Spin & Go tournament engine + batch runner.

Wraps the existing cash-game Table with blind-level progression,
player elimination, and placement tracking.

Usage (TOML config):
    python -m ohsim.spin spin.toml [games]

Usage (command line):
    python -m ohsim.spin --players bot1.ohf bot2.ohf bot3.ohf --games 1000

From Python:
    from ohsim.spin import run_spins, BETCLIC_STRUCTURE
    results = run_spins(cfg, n_games=1000)
"""
import os
import random
import sys
import time
from collections import Counter

from .engine import Table
from .bot import OpenPPLBot
from .run import DEFAULT_LIB, DEFAULT_BOT

# ---------------------------------------------------------------------------
# Betclic Spin & Rush blind structure (SB, BB) — 500 starting chips, no ante
# ---------------------------------------------------------------------------
BETCLIC_STRUCTURE = [
    (10, 20),     # Level 1 — 25 BB
    (15, 30),     # Level 2 — 16.6 BB
    (20, 40),     # Level 3 — 12.5 BB
    (30, 60),     # Level 4 — 8.3 BB
    (40, 80),     # Level 5 — 6.25 BB
    (50, 100),    # Level 6 — 5 BB
    (75, 150),    # Level 7 — 3.3 BB
    (100, 200),   # Level 8 — 2.5 BB
    (150, 300),   # Level 9 — 1.6 BB
]

STRUCTURES = {
    "betclic": BETCLIC_STRUCTURE,
}

DEFAULT_STARTING_STACK = 500
DEFAULT_HANDS_PER_LEVEL = 16  # ~2 min/level × ~8 hands/min at 3-player table


# ---------------------------------------------------------------------------
# Single tournament
# ---------------------------------------------------------------------------

def play_tournament(bots, names, structure, hands_per_level, starting_stack, seed,
                    hh_file=None, tournament_id=None, buyin=0.0):
    """Play one Spin & Go tournament.

    Returns (placements, hands_played) where placements is {name: place}
    with 1 = winner.

    If *hh_file* is an open text file, every hand is written in PokerStars
    tournament format (importable in PokerTracker 4).
    """
    table = Table(
        bots, names=names,
        sb=structure[0][0], bb=structure[0][1],
        ante=0.0, buyin=starting_stack,
        rebuy_below=0,          # no rebuys — tournament mode
        seed=seed,
        max_stack=None,         # no stack cap
        rake=0.0,
    )

    if hh_file:
        from .hhexport import export_tournament_hand

    level = 0
    hands_in_level = 0
    elim_order = []             # names, first eliminated first
    n_players = len(names)
    hand_base = (tournament_id or 1) * 10000

    while True:
        # --- detect newly busted players -----------------------------------
        newly_busted = []
        for s in table.seats:
            if s.stack <= 0 and not s.sitting_out:
                newly_busted.append(s)
                s.sitting_out = True
        # tie-break: player who started the hand with fewer chips = worse place
        newly_busted.sort(key=lambda s: s.start_stack)
        for s in newly_busted:
            elim_order.append(s.name)

        # --- check if tournament is over -----------------------------------
        alive = [s for s in table.seats if not s.sitting_out]
        if len(alive) < 2:
            for s in alive:
                elim_order.append(s.name)
            break

        # --- set blinds for current level ----------------------------------
        table.sb, table.bb = structure[level]

        # --- play one hand -------------------------------------------------
        res = table.play_hand()
        hands_in_level += 1

        if hh_file:
            hh_file.write(export_tournament_hand(
                table, res,
                hand_id=hand_base + table.hand_no,
                tournament_id=tournament_id or 1,
                buyin=buyin,
                level_no=level + 1,
            ))

        # --- advance blind level ------------------------------------------
        if hands_in_level >= hands_per_level and level < len(structure) - 1:
            level += 1
            hands_in_level = 0

    # assign placements: last eliminated = 1st place
    placements = {}
    for i, name in enumerate(elim_order):
        placements[name] = n_players - i
    return placements, table.hand_no


# ---------------------------------------------------------------------------
# Bot creation helper
# ---------------------------------------------------------------------------

def make_bots(player_cfgs, paths_cfg, seed=1):
    """Create OpenPPL bot objects from config dicts.  Returns (bots, names)."""
    lib = paths_cfg.get("openppl_library", DEFAULT_LIB)
    dbot = paths_cfg.get("default_bot", DEFAULT_BOT) or None
    custom = paths_cfg.get("custom_library") or None
    prwin = paths_cfg.get("prwin_iterations", 300)

    bots, names = [], []
    for i, p in enumerate(player_cfgs):
        name = p.get("name", f"Player{i + 1}")
        profile = p.get("profile") or None
        bot = OpenPPLBot(name, profile, lib, dbot, custom,
                         prwin_iterations=prwin, seed=seed * 100 + i)
        bots.append(bot)
        names.append(name)
    return bots, names


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def _ordinal(n):
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    s = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{s}"


def run_spins(cfg, n_games=None, out=print, hh_dir=None):
    """Run *n_games* Spin & Go tournaments and print + return results.

    *cfg* is a dict with keys ``tournament`` (or ``table``), ``player``,
    ``paths`` — the same layout as the TOML file.

    If *hh_dir* is a directory path, hand histories are saved there in
    PokerStars tournament format (one file per tournament), importable
    in PokerTracker 4.

    Returns a dict keyed by player name:
        {name: {"wins": int, "win_pct": float, "places": {1: n, 2: n, ...},
                "avg_place": float, "games": int}}
    plus a "_meta" key with run-level info.
    """
    tcfg = cfg.get("tournament", cfg.get("table", {}))
    paths = cfg.get("paths", {})

    n_games = n_games or tcfg.get("games", 100)
    structure_name = tcfg.get("structure", "betclic")
    structure = STRUCTURES.get(structure_name, BETCLIC_STRUCTURE)
    hands_per_level = tcfg.get("hands_per_level", DEFAULT_HANDS_PER_LEVEL)
    starting_stack = tcfg.get("starting_stack", DEFAULT_STARTING_STACK)
    base_seed = tcfg.get("seed", 1)
    buyin = tcfg.get("buyin", 5.0)

    if hh_dir:
        os.makedirs(hh_dir, exist_ok=True)

    bots, names = make_bots(cfg["player"], paths, seed=base_seed)
    n_players = len(names)

    # accumulators
    places = {name: Counter() for name in names}
    total_hands = 0

    t0 = time.time()
    rng = random.Random(base_seed)

    for g in range(n_games):
        seed = rng.randrange(2**31)
        tournament_id = base_seed * 100000 + g + 1

        hh_file = None
        if hh_dir:
            hh_path = os.path.join(hh_dir, f"T{tournament_id}.txt")
            hh_file = open(hh_path, "w", encoding="utf-8")

        placements, n_hands = play_tournament(bots, names, structure,
                                              hands_per_level, starting_stack, seed,
                                              hh_file=hh_file,
                                              tournament_id=tournament_id,
                                              buyin=buyin)
        if hh_file:
            hh_file.close()
        total_hands += n_hands
        for name, place in placements.items():
            places[name][place] += 1

        done = g + 1
        if done % 100 == 0 and out:
            elapsed = time.time() - t0
            out(f"  {done}/{n_games} tournaments, {elapsed:.0f}s "
                f"({done / elapsed:.1f} tourn/min, "
                f"{total_hands / elapsed:.0f} hands/min)")

    elapsed = max(time.time() - t0, 0.001)

    # --- report ------------------------------------------------------------
    if out:
        out(f"\n{n_games} Spin & Go tournaments  "
            f"({structure_name} structure, {hands_per_level} hands/level, "
            f"{starting_stack} chips)")
        out(f"Completed in {elapsed:.1f}s = "
            f"{n_games / elapsed:.1f} tournaments/min, "
            f"{total_hands / elapsed:.0f} hands/min, "
            f"avg {total_hands / n_games:.1f} hands/tournament\n")

        hdr = f"{'player':<16}"
        for p in range(1, n_players + 1):
            hdr += f"{_ordinal(p):>8}"
        hdr += f"{'avg':>8}{'win%':>8}"
        out(hdr)

        for name in names:
            row = f"{name:<16}"
            for p in range(1, n_players + 1):
                row += f"{places[name][p]:>8}"
            avg = sum(p * c for p, c in places[name].items()) / max(n_games, 1)
            win_pct = 100 * places[name][1] / max(n_games, 1)
            row += f"{avg:>8.2f}{win_pct:>7.1f}%"
            out(row)

    # --- return value ------------------------------------------------------
    results = {}
    for name in names:
        results[name] = {
            "places": {p: places[name][p] for p in range(1, n_players + 1)},
            "wins": places[name][1],
            "win_pct": 100 * places[name][1] / max(n_games, 1),
            "avg_place": sum(p * c for p, c in places[name].items()) / max(n_games, 1),
            "games": n_games,
        }
    results["_meta"] = {
        "games": n_games,
        "total_hands": total_hands,
        "avg_hands": total_hands / max(n_games, 1),
        "elapsed": elapsed,
        "structure": structure_name,
        "hands_per_level": hands_per_level,
        "starting_stack": starting_stack,
    }
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Spin & Go tournament simulator (OHSim)")
    parser.add_argument("config", nargs="?",
                        help="TOML config file (alternative to --players)")
    parser.add_argument("games_pos", nargs="?", type=int, metavar="games",
                        help="number of tournaments (positional override)")
    parser.add_argument("--players", nargs="+", metavar="FILE",
                        help="OpenPPL profile paths (.ohf)")
    parser.add_argument("--names", nargs="+",
                        help="player names (default: derived from filename)")
    parser.add_argument("--games", type=int, default=None,
                        help="number of tournaments (default 100)")
    parser.add_argument("--structure", default="betclic",
                        choices=list(STRUCTURES), help="blind structure")
    parser.add_argument("--hands-per-level", type=int,
                        default=DEFAULT_HANDS_PER_LEVEL)
    parser.add_argument("--starting-stack", type=int,
                        default=DEFAULT_STARTING_STACK)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    n_games = args.games_pos or args.games

    if args.config:
        import tomllib
        with open(args.config, "rb") as f:
            cfg = tomllib.load(f)
        if n_games is not None:
            cfg.setdefault("tournament", {})["games"] = n_games
    elif args.players:
        names = args.names or [
            os.path.splitext(os.path.basename(p))[0] for p in args.players
        ]
        cfg = {
            "tournament": {
                "games": n_games or 100,
                "structure": args.structure,
                "hands_per_level": args.hands_per_level,
                "starting_stack": args.starting_stack,
                "seed": args.seed,
            },
            "player": [{"name": n, "profile": p}
                       for n, p in zip(names, args.players)],
            "paths": {},
        }
    else:
        parser.error("provide a TOML config file or --players")
        return

    run_spins(cfg, n_games=n_games)


if __name__ == "__main__":
    main()
