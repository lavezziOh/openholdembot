"""Analyze action frequency leaks per line for each player.

Runs a simulation and collects detailed stats:
  - Fold-to-cbet per street
  - Probe-bet frequency (bet when checked to after prev street bet-call)
  - Check-raise frequency per street
  - Bet frequency when checked to (ip_vs_check)
  - Continuation patterns: cbet flop -> barrel turn -> barrel river

Usage: python analyze_leaks.py examples/rev038_vs_book_nl50rake.toml [hands]
"""
import sys, os, tomllib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import Counter, defaultdict
from ohsim.engine import Table, PREFLOP, FLOP, TURN, RIVER
from ohsim.bot import OpenPPLBot
from ohsim.ptstats import PTStats
from ohsim.oppl.parser import walk_identifiers

STREET_NAME = {PREFLOP: "preflop", FLOP: "flop", TURN: "turn", RIVER: "river"}
from ohsim.run import DEFAULT_LIB, DEFAULT_BOT


def analyze(cfg, hands=10000):
    tcfg = cfg.get("table", {})
    paths = cfg.get("paths", {})
    lib = paths.get("openppl_library", DEFAULT_LIB)
    dbot = paths.get("default_bot", DEFAULT_BOT) or None
    custom = paths.get("custom_library") or None
    seed = tcfg.get("seed", 1)
    bb = tcfg.get("bb", 1.0)

    stats = PTStats(limit_name=f"${tcfg.get('sb', 0.5):.2f}/${bb:.2f} NL (6 max)", bb=bb)
    bots = [OpenPPLBot(p["name"], p.get("profile") or None, lib, dbot, custom,
                       prwin_iterations=tcfg.get("prwin_iterations", 300),
                       seed=seed * 100 + i, stats=stats)
            for i, p in enumerate(cfg["player"])]
    stats.wanted = None  # all stats
    table = Table(bots, names=[b.name for b in bots], sb=tcfg.get("sb", 0.5),
                  bb=bb, ante=tcfg.get("ante", 0.0),
                  buyin=tcfg.get("buyin", 100.0), seed=seed,
                  rake=tcfg.get("rake", 0.0), rake_cap=tcfg.get("rake_cap", 0.0))

    names = [b.name for b in bots]
    # per-player, per-street, per-situation counters
    # situation keys: "fold_to_cbet", "cbet", "probe_bet", "check_raise",
    #                 "bet_vs_check", "barrel_after_cbet", "fold_to_probe"
    sit = {n: defaultdict(lambda: [0, 0]) for n in names}  # [opportunities, did_it]
    # line tracking: for each hand, who bet/raised each street
    cbet_bb = {n: [0, 0.0] for n in names}  # [n_cbet_hands, total_bb_won]
    probe_bb = {n: [0, 0.0] for n in names}

    import random
    sh_rng = random.Random(seed * 3571 + 7)
    shorthanded_pct = tcfg.get("shorthanded_pct", 0.0) / 100.0

    for h in range(hands):
        if shorthanded_pct > 0 and table.n > 2 and sh_rng.random() < shorthanded_pct:
            n_sit = sh_rng.randint(2, table.n - 1)
            sitting = sh_rng.sample(range(table.n), table.n - n_sit)
            for s in table.seats:
                s.sitting_out = s.chair in sitting
        else:
            for s in table.seats:
                s.sitting_out = False

        res = table.play_hand()
        stats.update(table, res)
        if h % 2000 == 0 and h > 0:
            print(f"  {h}/{hands} hands...", flush=True)

        history = table.history
        payouts = res["payouts"]
        chair_name = {s.chair: s.name for s in table.seats if not s.sitting_out}

        # group actions by street
        by_street = defaultdict(list)
        for rec in history:
            by_street[rec.street].append(rec)

        # who was the preflop aggressor?
        pf_actions = by_street[PREFLOP]
        pf_aggressor = None
        pf_first_raiser = None
        pf_raises = []
        for rec in pf_actions:
            if rec.kind == "raise" and rec.street == PREFLOP:
                pf_raises.append(rec.chair)
                if pf_first_raiser is None:
                    pf_first_raiser = rec.chair
        if pf_raises:
            pf_aggressor = pf_raises[-1]

        # preflop analysis: steal, BB defense, 3bet by position
        active_seats = [s for s in table.seats if not s.sitting_out]
        pos_map = {}
        n_active = len(active_seats)
        # position names based on distance from button (simplified)
        POS_NAMES = {0: "BTN", 1: "CO", 2: "HJ", 3: "UTG+1", 4: "UTG", 5: "UTG-1"}
        for s in active_seats:
            dist = (table.dealer - s.chair) % n_active
            pos_map[s.chair] = POS_NAMES.get(dist, f"P{dist}")

        bb_chair = table.bb_chair
        sb_chair = table.sb_chair

        if pf_first_raiser is not None and n_active >= 3:
            raiser_pos = pos_map.get(pf_first_raiser, "?")
            raiser_name = chair_name.get(pf_first_raiser, "?")
            # is it a steal? (open from CO/BTN/SB)
            is_steal = raiser_pos in ("BTN", "CO", "SB")

            if is_steal:
                # BB defense vs steal
                for rec in pf_actions:
                    nm = chair_name.get(rec.chair)
                    if nm and rec.chair == bb_chair:
                        if rec.kind == "fold":
                            sit[nm][f"bb_fold_vs_steal_{raiser_pos}"][0] += 1
                            sit[nm][f"bb_fold_vs_steal_{raiser_pos}"][1] += 1
                            sit[nm]["bb_fold_vs_steal"][0] += 1
                            sit[nm]["bb_fold_vs_steal"][1] += 1
                            sit[nm][f"bb_3bet_vs_steal_{raiser_pos}"][0] += 1
                            sit[nm]["bb_3bet_vs_steal"][0] += 1
                        elif rec.kind in ("call", "raise"):
                            sit[nm][f"bb_fold_vs_steal_{raiser_pos}"][0] += 1
                            sit[nm]["bb_fold_vs_steal"][0] += 1
                            sit[nm][f"bb_3bet_vs_steal_{raiser_pos}"][0] += 1
                            sit[nm]["bb_3bet_vs_steal"][0] += 1
                            if rec.kind == "raise":
                                sit[nm][f"bb_3bet_vs_steal_{raiser_pos}"][1] += 1
                                sit[nm]["bb_3bet_vs_steal"][1] += 1

                # SB defense vs steal (from BTN/CO)
                if raiser_pos in ("BTN", "CO"):
                    for rec in pf_actions:
                        nm = chair_name.get(rec.chair)
                        if nm and rec.chair == sb_chair:
                            if rec.kind == "fold":
                                sit[nm][f"sb_fold_vs_steal_{raiser_pos}"][0] += 1
                                sit[nm][f"sb_fold_vs_steal_{raiser_pos}"][1] += 1
                                sit[nm]["sb_fold_vs_steal"][0] += 1
                                sit[nm]["sb_fold_vs_steal"][1] += 1
                            elif rec.kind in ("call", "raise"):
                                sit[nm][f"sb_fold_vs_steal_{raiser_pos}"][0] += 1
                                sit[nm]["sb_fold_vs_steal"][0] += 1

            # 3bet frequency by position (any open)
            if len(pf_raises) >= 2:
                threebetter = pf_raises[1]
                nm3 = chair_name.get(threebetter)
                pos3 = pos_map.get(threebetter, "?")
                if nm3:
                    sit[nm3][f"3bet_from_{pos3}"][0] += 1
                    sit[nm3][f"3bet_from_{pos3}"][1] += 1

            # 3bet opportunity: everyone who faced the open and didn't 3bet
            for rec in pf_actions:
                nm = chair_name.get(rec.chair)
                if nm and rec.chair != pf_first_raiser:
                    pos = pos_map.get(rec.chair, "?")
                    if rec.kind in ("call", "fold"):
                        sit[nm][f"3bet_from_{pos}"][0] += 1

        # analyze each postflop street
        prev_street_bettor = pf_aggressor  # who bet/raised last on previous street
        prev_street_had_bet = pf_aggressor is not None

        for street in (FLOP, TURN, RIVER):
            acts = by_street[street]
            if not acts:
                break

            sname = STREET_NAME[street]
            street_bettor = None
            street_had_bet = False
            first_bet_chair = None
            first_check_then_raise = {}

            # find who acts in this street
            checked_chairs = set()
            for rec in acts:
                ch = rec.chair
                nm = chair_name.get(ch)
                if not nm:
                    continue

                if rec.kind == "check":
                    checked_chairs.add(ch)

                # C-bet: previous street aggressor bets first on this street
                if rec.kind in ("bet", "raise") and not street_had_bet:
                    first_bet_chair = ch
                    street_bettor = ch
                    street_had_bet = True

                    if ch == prev_street_bettor and prev_street_had_bet:
                        # this is a continuation bet (or barrel)
                        sit[nm][f"cbet_{sname}"][0] += 1
                        sit[nm][f"cbet_{sname}"][1] += 1
                    elif prev_street_had_bet and ch != prev_street_bettor:
                        if prev_street_bettor in checked_chairs:
                            # probe: aggressor already checked, opponent bets
                            sit[nm][f"probe_{sname}"][0] += 1
                            sit[nm][f"probe_{sname}"][1] += 1
                        else:
                            # donk: opponent bets before aggressor acts
                            sit[nm][f"donk_{sname}"][0] += 1
                            sit[nm][f"donk_{sname}"][1] += 1

                elif rec.kind in ("bet", "raise") and street_had_bet and ch in checked_chairs:
                    # check-raise
                    sit[nm][f"checkraise_{sname}"][0] += 1
                    sit[nm][f"checkraise_{sname}"][1] += 1

                elif rec.kind == "fold" and street_had_bet:
                    if first_bet_chair == prev_street_bettor and prev_street_had_bet:
                        sit[nm][f"fold_to_cbet_{sname}"][0] += 1
                        sit[nm][f"fold_to_cbet_{sname}"][1] += 1
                    elif prev_street_had_bet and first_bet_chair != prev_street_bettor:
                        if prev_street_bettor in checked_chairs:
                            sit[nm][f"fold_to_probe_{sname}"][0] += 1
                            sit[nm][f"fold_to_probe_{sname}"][1] += 1
                        else:
                            sit[nm][f"fold_to_donk_{sname}"][0] += 1
                            sit[nm][f"fold_to_donk_{sname}"][1] += 1

                elif rec.kind == "call" and street_had_bet:
                    if first_bet_chair == prev_street_bettor and prev_street_had_bet:
                        sit[nm][f"fold_to_cbet_{sname}"][0] += 1
                    elif prev_street_had_bet and first_bet_chair != prev_street_bettor:
                        if prev_street_bettor in checked_chairs:
                            sit[nm][f"fold_to_probe_{sname}"][0] += 1
                        else:
                            sit[nm][f"fold_to_donk_{sname}"][0] += 1

                elif rec.kind == "check" and prev_street_had_bet and ch == prev_street_bettor:
                    # prev aggressor checks (gave up continuation)
                    sit[nm][f"cbet_{sname}"][0] += 1  # opportunity but didn't cbet

            # donk opportunity: non-aggressor who checks BEFORE aggressor acts
            if prev_street_had_bet:
                for rec in acts:
                    if rec.chair == prev_street_bettor:
                        break
                    nm = chair_name.get(rec.chair)
                    if nm and rec.kind == "check":
                        sit[nm][f"donk_{sname}"][0] += 1

            # probe opportunity: prev aggressor checked, opponent can bet
            if prev_street_had_bet and prev_street_bettor in checked_chairs:
                for rec in acts:
                    nm = chair_name.get(rec.chair)
                    if nm and rec.chair != prev_street_bettor:
                        if rec.kind in ("bet", "raise"):
                            pass  # already counted above
                        elif rec.kind == "check":
                            sit[nm][f"probe_{sname}"][0] += 1

            # check-raise opportunities: anyone who checked then faced a bet
            for rec in acts:
                nm = chair_name.get(rec.chair)
                if nm and rec.chair in checked_chairs and street_had_bet and first_bet_chair != rec.chair:
                    if rec.kind not in ("bet", "raise"):
                        sit[nm][f"checkraise_{sname}"][0] += 1  # opportunity but didn't x/r

            # update for next street
            prev_street_bettor = street_bettor if street_had_bet else prev_street_bettor
            prev_street_had_bet = street_had_bet

    # Report
    print(f"\n{'='*80}")
    print(f"LEAK ANALYSIS - {hands} hands")
    print(f"{'='*80}\n")

    # ---- PREFLOP SITUATIONS ----
    pf_situations = [
        ("bb_fold_vs_steal", "BB fold vs steal (all)", "GTO ~45-55%"),
        ("bb_fold_vs_steal_BTN", "BB fold vs BTN steal", "GTO ~50-58%"),
        ("bb_fold_vs_steal_CO", "BB fold vs CO steal", "GTO ~52-60%"),
        ("bb_fold_vs_steal_SB", "BB fold vs SB steal", "GTO ~40-50%"),
        ("bb_3bet_vs_steal", "BB 3bet vs steal (all)", "GTO ~10-16%"),
        ("bb_3bet_vs_steal_BTN", "BB 3bet vs BTN steal", "GTO ~12-18%"),
        ("bb_3bet_vs_steal_CO", "BB 3bet vs CO steal", "GTO ~10-14%"),
        ("sb_fold_vs_steal", "SB fold vs steal", "GTO ~55-65%"),
        ("sb_fold_vs_steal_BTN", "SB fold vs BTN steal", "GTO ~58-68%"),
        ("3bet_from_BTN", "3bet from BTN", "GTO ~8-12%"),
        ("3bet_from_CO", "3bet from CO", "GTO ~7-10%"),
        ("3bet_from_HJ", "3bet from HJ", "GTO ~5-8%"),
        ("3bet_from_BB", "3bet from BB (all opens)", "GTO ~10-16%"),
        ("3bet_from_SB", "3bet from SB", "GTO ~8-12%"),
    ]

    # ---- POSTFLOP SITUATIONS ----
    situations = [
        ("cbet_flop", "C-bet flop", "GTO ~55-65%"),
        ("cbet_turn", "Barrel turn (after flop cbet)", "GTO ~45-55%"),
        ("cbet_river", "Barrel river (after turn barrel)", "GTO ~35-45%"),
        ("fold_to_cbet_flop", "Fold to c-bet flop", "GTO ~35-45%"),
        ("fold_to_cbet_turn", "Fold to c-bet turn", "GTO ~40-50%"),
        ("fold_to_cbet_river", "Fold to c-bet river", "GTO ~40-50%"),
        ("donk_flop", "Donk bet flop (OOP bets into aggressor)", "GTO ~5-15%"),
        ("donk_turn", "Donk bet turn", "GTO ~5-15%"),
        ("probe_flop", "Probe bet flop (after aggressor checks)", "GTO ~25-35%"),
        ("probe_turn", "Probe bet turn (after IP checks back)", "GTO ~25-35%"),
        ("probe_river", "Probe bet river", "GTO ~25-35%"),
        ("fold_to_donk_flop", "Fold to donk bet flop", "GTO ~20-30%"),
        ("fold_to_donk_turn", "Fold to donk bet turn", "GTO ~20-30%"),
        ("fold_to_probe_flop", "Fold to probe flop", "GTO ~30-40%"),
        ("fold_to_probe_turn", "Fold to probe turn", "GTO ~30-40%"),
        ("fold_to_probe_river", "Fold to probe river", "GTO ~35-45%"),
        ("checkraise_flop", "Check-raise flop", "GTO ~8-12%"),
        ("checkraise_turn", "Check-raise turn", "GTO ~6-10%"),
        ("checkraise_river", "Check-raise river", "GTO ~6-10%"),
    ]

    # ---- Preflop report ----
    print("  PREFLOP")
    print("  " + "-" * 60)
    for skey, label, gto_ref in pf_situations:
        has_data = any(sit[nm][skey][0] > 0 for nm in names)
        if not has_data:
            continue
        print(f"\n  {label} ({gto_ref})")
        print(f"  {'player':<16} {'freq':>8} {'n':>6}  {'anomaly'}")
        for nm in names:
            opp, did = sit[nm][skey]
            if opp > 0:
                freq = did / opp
                flag = ""
                if "fold" in skey and freq > 0.65:
                    flag = "!! FOLDS TOO MUCH"
                elif "fold" in skey and freq < 0.35:
                    flag = "!! DEFENDS TOO MUCH"
                elif "3bet" in skey and freq > 0.20:
                    flag = "!! 3BETS TOO MUCH"
                elif "3bet" in skey and freq < 0.04:
                    flag = "!! 3BETS TOO LITTLE"
                print(f"  {nm:<16} {freq:>7.1%} {opp:>6}  {flag}")

    # ---- Postflop report ----
    print(f"\n  POSTFLOP")
    print("  " + "-" * 60)
    for skey, label, gto_ref in situations:
        print(f"\n  {label} ({gto_ref})")
        print(f"  {'player':<16} {'freq':>8} {'n':>6}  {'anomaly'}")
        for nm in names:
            opp, did = sit[nm][skey]
            if opp > 0:
                freq = did / opp
                flag = ""
                # flag anomalies
                if "fold" in skey and freq > 0.55:
                    flag = "!! FOLDS TOO MUCH"
                elif "fold" in skey and freq < 0.25:
                    flag = "!! CALLS TOO MUCH"
                elif "cbet" in skey and "fold" not in skey and freq > 0.75:
                    flag = "!! BETS TOO MUCH"
                elif "cbet" in skey and "fold" not in skey and freq < 0.30:
                    flag = "!! BETS TOO LITTLE"
                elif "donk" in skey and "fold" not in skey and freq > 0.25:
                    flag = "!! DONKS TOO MUCH"
                elif "probe" in skey and "fold" not in skey and freq > 0.50:
                    flag = "!! PROBES TOO MUCH"
                elif "checkraise" in skey and freq > 0.15:
                    flag = "!! X/R TOO MUCH"
                elif "checkraise" in skey and freq < 0.03:
                    flag = "!! X/R TOO LITTLE"
                print(f"  {nm:<16} {freq:>7.1%} {opp:>6}  {flag}")
            else:
                print(f"  {nm:<16}     n/a      0")

    print(f"\n{'='*80}")
    print("EXPLOIT OPPORTUNITIES (sorted by impact)")
    print(f"{'='*80}\n")

    # find biggest deviations (preflop + postflop)
    exploits = []
    for skey, label, gto_ref in pf_situations:
        for nm in names:
            opp, did = sit[nm][skey]
            if opp >= 15:
                freq = did / opp
                if "fold" in skey and freq > 0.65:
                    exploits.append((freq - 0.55, nm, label, f"{freq:.1%}", "FOLDS TOO MUCH -> steal/open wider"))
                elif "fold" in skey and freq < 0.35:
                    exploits.append((0.45 - freq, nm, label, f"{freq:.1%}", "DEFENDS TOO MUCH -> tighten up, value raise"))
                elif "3bet" in skey and freq > 0.20:
                    exploits.append((freq - 0.14, nm, label, f"{freq:.1%}", "3BETS TOO MUCH -> 4bet light or trap"))
                elif "3bet" in skey and freq < 0.04:
                    exploits.append((0.08 - freq, nm, label, f"{freq:.1%}", "3BETS TOO LITTLE -> steal freely"))
    for skey, label, gto_ref in situations:
        for nm in names:
            opp, did = sit[nm][skey]
            if opp >= 20:
                freq = did / opp
                if "fold" in skey:
                    if freq > 0.55:
                        exploits.append((freq - 0.45, nm, label, f"{freq:.1%}", "FOLDS TOO MUCH -> bet wider"))
                    elif freq < 0.25:
                        exploits.append((0.35 - freq, nm, label, f"{freq:.1%}", "CALLS TOO MUCH -> value bet thinner, bluff less"))
                elif "cbet" in skey:
                    if freq > 0.75:
                        exploits.append((freq - 0.65, nm, label, f"{freq:.1%}", "BETS TOO MUCH -> x/r or float more"))
                    elif freq < 0.30:
                        exploits.append((0.45 - freq, nm, label, f"{freq:.1%}", "GIVES UP TOO MUCH -> probe/steal"))
                elif "donk" in skey and "fold" not in skey and freq > 0.25:
                    exploits.append((freq - 0.15, nm, label, f"{freq:.1%}", "DONKS TOO MUCH -> raise or fold more vs donk"))
                elif "probe" in skey and freq > 0.50:
                    exploits.append((freq - 0.35, nm, label, f"{freq:.1%}", "PROBES TOO MUCH -> trap or x/r"))
                elif "checkraise" in skey:
                    if freq > 0.15:
                        exploits.append((freq - 0.10, nm, label, f"{freq:.1%}", "X/R TOO MUCH -> bet smaller or check back more"))
                    elif freq < 0.03:
                        exploits.append((0.08 - freq, nm, label, f"{freq:.1%}", "NEVER X/R -> bet freely"))

    exploits.sort(reverse=True)
    for impact, nm, label, freq, action in exploits[:20]:
        print(f"  [{impact:.0%}] {nm:<16} {label}: {freq} -> {action}")

    if not exploits:
        print("  No significant anomalies found.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "examples/rev038_vs_book_nl50rake.toml"
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["table"]["hands"] = h
    analyze(cfg, h)
