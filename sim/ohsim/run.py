"""Run a cash-game match between OpenPPL profiles.

    python -m ohsim.run match.toml [hands] [-j workers] [--hh folder]

--hh writes the hand histories in PokerStars format (one file per table),
importable in PokerTracker 4 (hero = first player, or [report] hh_hero).

Hands are split in blocks played in parallel, one per CPU core (-j 1 = single
process). Every block is an independent table with its own seed: results are
reproducible for a given (seed, hands, workers).

Example match.toml:

    [table]
    hands = 2000
    sb = 0.5
    bb = 1.0
    ante = 0.0
    buyin = 100        # in chips (= 100 bb)
    seed = 1
    prwin_iterations = 300
    workers = 0        # 0 = all CPU cores

    [paths]
    openppl_library = 'C:/.../OpenPPL/OpenPPL_Library'
    default_bot = 'C:/.../bot_logic/DefaultBot'     # optional

    [[player]]
    name = "MyBot"
    profile = 'C:/bots/MyBot.ohf'

    [[player]]
    name = "Default1"
    profile = ""            # empty = OpenHoldem default bot only
"""
import datetime
import multiprocessing as mp
import os
import random
import sys
import time
import tomllib
from collections import Counter

from .engine import Table, PREFLOP
from .bot import OpenPPLBot
from .ptstats import PTStats
from .oppl.parser import walk_identifiers

REPORT_STATS = [("vpip", "VPIP"), ("pfr", "PFR"), ("rfi", "RFI"), ("3bet", "3Bet"), ("fold_to_3bet", "F3Bet"),
                ("steal_attempt", "Steal"), ("bb_fold_to_steal", "BBfSteal"), ("flop_cbet", "FCbet"),
                ("flop_fold_to_cbet", "FfCbet"), ("turn_fold_to_cbet", "TfCbet"), ("total_af", "AF"),
                ("wtsd", "WTSD"), ("wssd", "W$SD")]


def _pt_totals(stats):
    stats.flush()
    out = {}
    for (name, stat), a in stats.acc.items():
        if stats.defs[stat][1]:
            out[(name, stat)] = (a[0], a[1])
        else:
            out[(name, "#" + stat)] = (a[0], 0.0)          # plain counts (e.g. hands, *_opp): '#' prefix
    return out


def write_pt_file(path, ps, groups):
    """Every PokerTracker stat of every player, as OpenHoldem defines them: value and times/opportunities."""
    from . import ptdb
    defs = ptdb.definitions()
    order = {"basic": 0, "positional": 1, "advanced": 2}
    stats = sorted(defs, key=lambda s: (order.get(defs[s].get("group"), 3), s))
    w = max(14, max(len(g) for g in groups) + 2)
    lines = ["PokerTracker stats computed by OHSim with OpenHoldem's own queries",
             "value = times / opportunities; '-' = no opportunity in this run; 'n/a' = not computable in the simulator",
             "",
             f"{'stat':<40}" + "".join(f"{g:>{w + 14}}" for g in groups) + "   description"]
    for s in stats:
        cells = []
        for g in groups:
            if (g, s) in ps:
                num, den = ps[(g, s)]
                if not den:
                    cells.append("-")
                elif s.endswith("_af"):
                    cells.append(f"{num / den:.2f} ({num:.0f}/{den:.0f})")
                else:
                    cells.append(f"{100 * num / den:.1f}% ({num:.0f}/{den:.0f})")
            elif (g, "#" + s) in ps:
                cells.append(f"{ps[(g, '#' + s)][0]:.0f}")
            else:
                cells.append("n/a" if s.endswith("_afq") else "-")
        lines.append(f"{s:<40}" + "".join(f"{c:>{w + 14}}" for c in cells) + "   " + defs[s].get("description", ""))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    write_pt_csv(os.path.splitext(path)[0] + ".csv", ps, groups, stats, defs)


def write_pt_csv(path, ps, groups, stats, defs):
    """CSV for Excel: one value per player (% with symbol, AF as factor). Delimiter ';', UTF-8 BOM."""
    import csv
    num_ = lambda x, d=1: f"{x:.{d}f}"
    header = ["stat", "gruppo"] + [g for g in groups] + ["descrizione"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(header)
        for s in stats:
            row = [s, defs[s].get("group", "")]
            for g in groups:
                if (g, s) in ps and ps[(g, s)][1]:
                    num, den = ps[(g, s)]
                    is_af = s.endswith("_af")
                    val = num / den if is_af else 100 * num / den
                    row.append(num_(val, 2 if is_af else 1) + ("" if is_af else "%"))
                elif (g, "#" + s) in ps:
                    row.append(f"{ps[(g, '#' + s)][0]:.0f}")
                else:
                    row.append("")
            row.append(defs[s].get("description", ""))
            w.writerow(row)
from .hhexport import export_hand
from .report import preflop_stats, format_report, line_stats, format_lines

HERE = os.path.dirname(os.path.abspath(__file__))
OH_REPO = os.path.normpath(os.path.join(HERE, "..", "..", "openholdembot"))
DEFAULT_LIB = os.path.join(OH_REPO, "OpenPPL", "OpenPPL_Library")
DEFAULT_BOT = os.path.join(OH_REPO, "##_OpenHoldem_Release_Directory_##", "bot_logic", "DefaultBot")
# release layout: OpenPPL_Library and DefaultBot shipped next to the program (source zip or .exe folder)
_APP_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
             else os.path.normpath(os.path.join(HERE, "..")))
if os.path.isdir(os.path.join(_APP_ROOT, "OpenPPL_Library")):
    DEFAULT_LIB = os.path.join(_APP_ROOT, "OpenPPL_Library")
    DEFAULT_BOT = os.path.join(_APP_ROOT, "DefaultBot")

PROGRESS_STEP = 50
_progress = None          # shared counter of played hands (set in workers)


def _init_worker(counter):
    global _progress
    _progress = counter


def _block_seed(seed, k):
    # block 0 keeps the plain seed: -j 1 gives the same hands as before
    return seed if k == 0 else seed * 1000003 + k


def play_block(cfg, k, hands):
    """Play `hands` hands on an independent table; return summable counters."""
    tcfg = cfg.get("table", {})
    paths = cfg.get("paths", {})
    lib = paths.get("openppl_library", DEFAULT_LIB)
    dbot = paths.get("default_bot", DEFAULT_BOT) or None
    custom = paths.get("custom_library") or None
    seed = _block_seed(tcfg.get("seed", 1), k)

    n_players = len(cfg["player"])
    stats = PTStats(limit_name=f"${tcfg.get('sb', 0.5):.2f}/${tcfg.get('bb', 1.0):.2f} " + ("PL" if tcfg.get('game') == 'plo' else "NL")
                    + (" (6 max)" if n_players <= 6 else ""), bb=tcfg.get("bb", 1.0))
    bots = [OpenPPLBot(p["name"], p.get("profile") or None, lib, dbot, custom,
                       prwin_iterations=tcfg.get("prwin_iterations", 300),
                       seed=seed * 100 + i, stats=stats)
            for i, p in enumerate(cfg["player"])]
    # compute only the PokerTracker stats that the bots (and the report) use
    used = {s for s, _ in REPORT_STATS}
    for b in bots:
        for o in b.rt.objects.values():
            if not o.is_list and o.tree is not None:
                used.update(i for i in walk_identifiers(o.tree) if i.startswith("pt_"))
    stats.want(used)
    if cfg.get("report", {}).get("pt_all"):
        stats.wanted = None                 # every stat (slower): for the full PokerTracker stats file
    table = Table(bots, names=[b.name for b in bots], sb=tcfg.get("sb", 0.5),
                  bb=tcfg.get("bb", 1.0), ante=tcfg.get("ante", 0.0),
                  buyin=tcfg.get("buyin", 100.0), seed=seed,
                  max_stack=tcfg.get("max_stack", 3 * tcfg.get("buyin", 100.0)) or None,
                  rake=tcfg.get("rake", 0.0), rake_cap=tcfg.get("rake_cap", 0.0),
                  game=tcfg.get("game", "nlhe"))
    # opponent ranges for dll$prwin_range: statistics learnt from the public actions of this table
    range_model = None
    uses_ranges = any(i in ("dll$prwin_range", "dll$prwin_oh") or i.startswith("dll$range_") for b in bots for o in b.rt.objects.values()
                      if not o.is_list and o.tree is not None for i in walk_identifiers(o.tree))
    if uses_ranges or tcfg.get("range_model"):
        from .ranges import PlayerModel
        range_model = PlayerModel()
        for b in bots:
            b.provider.range_model = range_model
    vpip, pfr, dealt = Counter(), Counter(), Counter()
    pos_stats = Counter()
    lines = {}
    pairwise = Counter()
    pairwise_n = Counter()
    rep = cfg.get("report", {})
    hh_file = None
    if rep.get("hh"):
        os.makedirs(rep["hh"], exist_ok=True)
        # every run gets its own hand numbers, dates, table names and files: PokerTracker skips
        # hands whose number it has already imported, so reused numbers would be silently ignored
        run_id = int(rep.get("hh_run", 0))
        tag = time.strftime("%Y%m%d_%H%M%S", time.localtime(run_id)) if run_id else "run"
        hh_file = open(os.path.join(rep["hh"], f"ohsim_{tag}_table{k + 1:02d}.txt"), "w", encoding="utf-8")
        hero = rep.get("hh_hero", bots[0].name)
        hh_base = 300000000000 + (k + 1) * 1000000000 if not run_id else \
            (run_id % 10_000_000) * 1_000_000_000 + (k + 1) * 10_000_000
        hh_epoch = datetime.datetime.fromtimestamp(run_id) if run_id else None
    pending = 0
    # Seats are shuffled at the end of every orbit (default on, "shuffle_seats = false" to keep them fixed).
    # Separate generator: the cards dealt are the same with or without shuffling.
    seat_rng = random.Random(seed * 7919 + 13) if tcfg.get("shuffle_seats", True) else None
    # Short-handed: X% of hands are played with fewer players (uniform across 2..N)
    shorthanded_pct = tcfg.get("shorthanded_pct", 0.0) / 100.0
    sh_rng = random.Random(seed * 3571 + 7) if shorthanded_pct > 0 else None
    for _ in range(hands):
        # decide how many players sit this hand
        if sh_rng is not None and table.n > 2 and sh_rng.random() < shorthanded_pct:
            n_sit = sh_rng.randint(2, table.n - 1)
            sitting = sh_rng.sample(range(table.n), table.n - n_sit)
            for s in table.seats:
                s.sitting_out = s.chair in sitting
        else:
            for s in table.seats:
                s.sitting_out = False
        res = table.play_hand()
        if hh_file:
            when = hh_epoch + datetime.timedelta(seconds=40 * table.hand_no) if hh_epoch else None
            hh_file.write(export_hand(table, res, hh_base + table.hand_no,
                                      f"ohsim {tag[-6:]} {k + 1}" if run_id else f"ohsim {k + 1}", hero, when))
        stats.update(table, res)
        if range_model is not None:
            range_model.add_hand(table)
        pos_stats.update(preflop_stats(table))
        contestants = [s for s in table.seats if not s.sitting_out and not s.folded]
        if len(contestants) == 2 and table.board_seen_flop:
            a, b_ = contestants
            da = res["payouts"][a.chair] - a.invested
            for pair in ((a.name, b_.name), (b_.name, a.name)):
                pairwise[pair] += da if pair[0] == a.name else -da
                pairwise_n[pair] += 1
        for key_, (n_, v_) in line_stats(table, res).items():
            a_ = lines.setdefault(key_, [0, 0.0]); a_[0] += n_; a_[1] += v_
        pre = [r for r in table.history if r.street == PREFLOP]
        for s in table.seats:
            if s.sitting_out:
                continue
            dealt[s.name] += 1
            mine = [r.kind for r in pre if r.chair == s.chair]
            if "call" in mine or "raise" in mine or "bet" in mine:
                vpip[s.name] += 1
            if "raise" in mine or "bet" in mine:
                pfr[s.name] += 1
        if seat_rng is not None and table.hand_no % table.n == 0 and table.n_active_seats > 2:
            table.shuffle_seats(seat_rng)           # end of an orbit: new seating (skip when HU)
        pending += 1
        if _progress is not None and pending == PROGRESS_STEP:
            with _progress.get_lock():
                _progress.value += pending
            pending = 0
    if _progress is not None and pending:
        with _progress.get_lock():
            _progress.value += pending
    if hh_file:
        hh_file.close()
    missing, errors = Counter(), Counter()
    for b in bots:
        missing.update(b.provider.missing)
        errors.update(b.rt.runtime_errors)
    return {
        "hands": hands,
        "results": Counter({n: v for n, v in table.results.items()}),
        "vpip": vpip, "pfr": pfr, "dealt": dealt,
        "decisions": {b.name: b.decisions for b in bots},
        "missing": missing, "errors": errors, "positions": pos_stats, "lines": lines,
        "pt": _pt_totals(stats),
        "load_errors": {b.name: b.load_errors for b in bots} if k == 0 else {},
        "bb": table.bb, "rake": table.rake_total,
        "pairwise": pairwise, "pairwise_n": pairwise_n,
    }


def _play_block_args(args):
    return play_block(*args)


def run(cfg, out=print, workers=None):
    tcfg = cfg.get("table", {})
    hands = tcfg.get("hands", 1000)
    if workers is None:
        workers = tcfg.get("workers", 0)
    if workers <= 0:
        workers = os.cpu_count() or 1
    workers = max(1, min(workers, hands // 50 or 1))

    t0 = time.time()
    rep = cfg.setdefault("report", {})
    if rep.get("hh") and not rep.get("hh_run"):
        rep["hh_run"] = int(t0)                 # unique hand numbers / file names for this run
    sizes = [hands // workers + (1 if k < hands % workers else 0) for k in range(workers)]
    if workers == 1:
        global _progress
        _progress = None
        blocks = [play_block(cfg, 0, hands)]
    else:
        counter = mp.Value("i", 0)
        with mp.Pool(workers, initializer=_init_worker, initargs=(counter,)) as pool:
            res = pool.map_async(_play_block_args, [(cfg, k, n) for k, n in enumerate(sizes)])
            last = -1
            while not res.ready():
                res.wait(2)
                done = counter.value
                if done != last and done:
                    el = time.time() - t0
                    out(f"  {done}/{hands} hands, {el:.0f}s ({done / el * 60:.0f} hands/min)")
                    last = done
            blocks = res.get()
    el = time.time() - t0

    total = {"results": Counter(), "vpip": Counter(), "pfr": Counter(), "dealt": Counter(),
             "missing": Counter(), "errors": Counter(), "positions": Counter(),
             "pairwise": Counter(), "pairwise_n": Counter()}
    decisions = {}
    for b in blocks:
        for key in total:
            total[key].update(b[key])
        for name, d in b["decisions"].items():
            decisions.setdefault(name, Counter()).update(d)
    for name, errs in blocks[0]["load_errors"].items():
        if errs:
            out(f"[{name}] {len(errs)} load/parse errors, first ones:")
            for f, line, msg in errs[:10]:
                out(f"    {os.path.basename(f)}:{line}: {msg}")

    bb = blocks[0]["bb"]
    names = [p["name"] for p in cfg["player"]]
    out("")
    out(f"{hands} hands in {el:.1f}s = {hands / el * 60:.0f} hands/min "
        f"({workers} process{'es' if workers > 1 else ''}, load time included)")
    rake = sum(b.get("rake", 0.0) for b in blocks)
    if rake:
        out(f"rake {rake / bb:.0f}bb = {rake / bb / hands * 100:.1f} bb/100 hands (table total)")
    out(f"{'player':<16}{'net bb':>10}{'bb/100':>10}{'VPIP':>8}{'PFR':>8}  decisions")
    for n in names:
        net = total["results"][n] / bb
        dealt = total["dealt"][n] or 1
        top = ", ".join(f"{k}:{v}" for k, v in decisions[n].most_common(6))
        out(f"{n:<16}{net:>10.1f}{net / hands * 100:>10.1f}"
            f"{100 * total['vpip'][n] / dealt:>7.1f}%{100 * total['pfr'][n] / dealt:>7.1f}%  {top}")
    if total["pairwise_n"]:
        out(f"\n{'':>16}  head-to-head postflop (HU pots only)")
        out(f"{'player':<16}{'vs':<16}{'pots':>6}{'net bb':>10}{'bb/pot':>10}")
        for n in names:
            rows = [(opp, total["pairwise"][(n, opp)], total["pairwise_n"][(n, opp)])
                    for opp in names if opp != n and total["pairwise_n"][(n, opp)] > 0]
            rows.sort(key=lambda r: r[1] / r[2] if r[2] else 0)
            for opp, net_chips, npots in rows:
                net_bb = net_chips / bb
                out(f"{n:<16}{opp:<16}{npots:>6}{net_bb:>10.1f}{net_bb / npots:>10.2f}")
    if cfg.get("report", {}).get("positions", True):
        import re
        grp = lambda n: re.sub(r"_\d+$", "", n)        # Rev025_1, Rev025_2 -> Rev025
        regrouped = Counter()
        for (n, p, st), v in total["positions"].items():
            regrouped[(grp(n), p, st)] += v
        format_report(regrouped, list(dict.fromkeys(grp(n) for n in names)), out)
        lines = {}
        for b in blocks:
            for (n, p, k), (cnt, v) in b["lines"].items():
                a = lines.setdefault((grp(n), p, k), [0, 0.0])
                a[0] += cnt
                a[1] += v
        per_group = Counter()
        for n in names:
            per_group[grp(n)] += hands
        format_lines(lines, list(dict.fromkeys(grp(n) for n in names)), bb, per_group, out)
        ps = {}
        for b in blocks:
            for (n, stat), (num, den) in b["pt"].items():
                a = ps.setdefault((grp(n), stat), [0.0, 0.0])
                a[0] += num
                a[1] += den
        if rep.get("pt_file"):
            write_pt_file(rep["pt_file"], ps, list(dict.fromkeys(grp(n) for n in names)))
            out(f"\nAll PokerTracker stats written to {rep['pt_file']}")
        out("\nPokerTracker stats (OpenHoldem definitions)")
        out(f"{'player':<12}" + "".join(f"{lab:>9}" for _, lab in REPORT_STATS))
        for g in dict.fromkeys(grp(n) for n in names):
            cells = []
            for stat, lab in REPORT_STATS:
                num, den = ps.get((g, stat), (0.0, 0.0))
                if not den:
                    cells.append("        -")
                elif stat.endswith("_af"):
                    cells.append(f"{num / den:9.2f}")
                else:
                    cells.append(f"{100 * num / den:9.1f}")
            out(f"{g:<12}" + "".join(cells))
    if total["missing"]:
        out("\nOpenHoldem symbols not available (OpenHoldem would not have them either; "
            "PokerTracker ones evaluate to -1, the others to 0):")
        for k, v in total["missing"].most_common(40):
            out(f"  {v:8}  {k}")
    other = Counter({k: v for k, v in total["errors"].items() if not k.startswith("unknown symbol")})
    if other:
        out("\nRuntime errors:")
        for k, v in other.most_common(20):
            out(f"  {v:8}  {k}")
    # PokerTracker numerators / denominators per player (for programmatic use)
    total["pt"] = {}
    for b in blocks:
        for key, (num, den) in b["pt"].items():
            a = total["pt"].setdefault(key, [0.0, 0.0])
            a[0] += num
            a[1] += den
    total["hands"] = hands
    total["bb"] = bb
    return total, decisions


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    workers = None
    hh = None
    if "--hh" in argv:
        i = argv.index("--hh")
        hh = argv[i + 1]
        del argv[i:i + 2]
    if "-j" in argv:
        i = argv.index("-j")
        workers = int(argv[i + 1])
        del argv[i:i + 2]
    with open(argv[0], "rb") as f:
        cfg = tomllib.load(f)
    if len(argv) > 1:
        cfg.setdefault("table", {})["hands"] = int(argv[1])
    if hh:
        cfg.setdefault("report", {})["hh"] = os.path.abspath(hh)
    run(cfg, workers=workers)


if __name__ == "__main__":
    main()
