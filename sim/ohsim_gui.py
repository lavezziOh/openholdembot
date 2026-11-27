"""OHSim - desktop window to play OpenPPL / OpenHoldem profiles against each other.

Copyright (C) 2026 Lavezzi. Licensed under the GNU GPL v3 or later (see LICENSE).

    python ohsim_gui.py

Each match runs in a separate process (this same program with --worker),
so it can be stopped at any time and the window stays responsive.
"""
import os
import re
import sys
import json
import queue
import tempfile
import time
import threading
import subprocess
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, HERE)

APP = "OHSim"
VERSION = "1.04"
COPYRIGHT = "Copyright (C) 2026 Lavezzi"
LICENSE_FILE = os.path.join(HERE, "LICENSE")
ABOUT = f"""{APP} {VERSION}
No-Limit Hold'em and Pot-Limit Omaha cash-game simulator for OpenHoldem / OpenPPL profiles.

{COPYRIGHT}

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version.
It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See Help > License for the full text.

Built with
  - OpenHoldem and the OpenPPL library (GPL v3), by the OpenHoldem team:
    the profile language, its symbols and the library functions the profiles call.
    https://github.com/OpenHoldem/openholdembot
  - ohsim: a Python re-implementation of the OpenHoldem/OpenPPL formula evaluation,
    a poker table engine, hand evaluator and PokerTracker-style statistics.
  - Python and Tkinter (Python Software Foundation License; Tcl/Tk license).

Hand histories are written in the PokerStars text format so that PokerTracker can import
them. PokerStars, PokerTracker and OpenHoldem are names of their respective owners;
this program is not affiliated with or endorsed by them.

For simulation, study and testing. Using bots at real-money tables is against the terms
of service of most poker sites.

--- Changelog ---

v1.04 (2026-11-27)
  New features:
  - Spin & Go tab: 4th tab for single-table tournament simulation
  - Customizable blind structure editor (add/remove levels, double-click to edit)
  - Customizable payout table with multiplier, frequency and split (WTA or 75/15/10)
  - Betclic Spin & Rush presets (real blind levels and payout multipliers)
  - Tournament results: 1st/2nd/3rd %, avg place, win%, ROI calculation
  - ROI accounts for split payouts (WTA for low multipliers, shared for jackpots)
  - Save/Load includes spin settings (blind structure + payouts in TOML)
  - RAM monitor: checks system memory every 5s during simulations
  - RAM warning at 85% usage shown in status bar
  - RAM critical popup at 92% with option to stop simulation (prevents crash)
  - Supports 2-10 players (Spin & Go 3-max, STT 6-max/9-max)

v1.03 (2026-11-26)
  - Two-level profile cache: unchanged base library skips reload (~3-5s vs ~8-10s)
  - Profile hash based on mtime+size for instant cache lookup
  - Fix: spots.py table() filters .json (was hitting subdirectories)
  - Fix: to_oppl.py section-keyword function was overridden by explicit definition
  - Fix: to_oppl.py repeated builds caused list-number collisions
  - Profile builder cleans previously generated blocks before rebuilding
  - Version number and changelog in About window

v1.02 (2026-11-24)
  New features:
  - Hole finder tab: simulates N hands, reports profile gaps by street/texture/hand class
  - Step debugger: mixed-action filter ("Only show actions with multiple choices")
  - Short-handed support: "Short-handed % (0=off)" field, random sitting out
  - Population model: configurable opponent archetypes (reg, lag, nit, fish)
  - Leak archetypes (leaks.py): deformed opponents (nit, station, maniac, folder, nobluff)
  - Ranges module (ranges.py): hand range utilities for preflop analysis
  Engine:
  - Sitting out / shorthanded logic in engine and batch runner
  - Improved hand evaluation (handeval.py)
  - OPPL parser, runtime, and tokenizer updates

v1.01 (2026-11-10)
  New features:
  - All PokerTracker stats: "PokerTracker stats..." button with full pt_ stat table
  - Seat shuffle: "Shuffle seats every orbit" (on by default)
  - Hand replay: play_hand(deck=...) to compare profiles on the same cards
  - New example: Pop_book.ohf (TAG from strategy book)
  Fixes:
  - History symbols (Raises, Calls, BotsLastAction) were one action ahead
  - ac_betposX / ac_dealposX: chair index parsed wrong
  - nopponentscalling, firstcallerchair, lastcallerchair fixed
  - didbetsize counts pot-fraction bets
  - PT ratio stats return -1 without data (not 0)
  - User-DLL symbols return 0 (not implemented)
  OpenPPL library fixes:
  - Positions: FirstCallerPosition, LastCallerPosition, FirstRaiserPosition fixed
  - Blind raises: SmallBlindRaising / BigBlindRaising fixed postflop
  - Calls: Calls / CallsSinceLastPlay lost limps and calls before a raise
  - Kicker: HaveBestKicker now uses hole card, not board card
  - StraightFlushPossible missed A-5 straight flush
  - New squeeze symbols (SqueezeOpportunity, SqueezeSize, etc.)

v1.00 (2026-11-01)
  - Initial release
  - 6-max NL Hold'em and PLO cash-game simulation
  - OpenPPL/OpenHoldem profile evaluation engine
  - PokerTracker-compatible hand history export
  - Population profiles (Book bots) included
  - Leak analysis and per-street statistics
  - Multi-process simulation with live progress
  - GUI with profile selection, settings, and results table"""
SETTINGS = os.path.join(os.path.expanduser("~"), ".ohsim_gui.json")
POP_DIR = os.path.join(HERE, "profiles", "population")
RESULT_RE = re.compile(r"^(\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%")
PROGRESS_RE = re.compile(r"^\s*(\d+)/(\d+) hands, (\d+)s \((\d+) hands/min\)")

# ---------------------------------------------------------------- debugger helpers
_RANK_CH = "??23456789TJQKA"
_SUIT_SYM = ["c", "d", "h", "s"]
_STREET_NM = {1: "Preflop", 2: "Flop", 3: "Turn", 4: "River"}
_POS = {
    2: ["BTN", "BB"], 3: ["BTN", "SB", "BB"], 4: ["BTN", "SB", "BB", "CO"],
    5: ["BTN", "SB", "BB", "HJ", "CO"], 6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
    7: ["BTN", "SB", "BB", "UTG", "UTG1", "HJ", "CO"],
    8: ["BTN", "SB", "BB", "UTG", "UTG1", "UTG2", "HJ", "CO"],
    9: ["BTN", "SB", "BB", "UTG", "UTG1", "UTG2", "HJ", "LJ", "CO"],
    10: ["BTN", "SB", "BB", "UTG", "UTG1", "UTG2", "UTG3", "HJ", "LJ", "CO"],
}


def _card(c):
    return _RANK_CH[c[0]] + _SUIT_SYM[c[1]]


def _cards(cs):
    return " ".join(_card(c) for c in cs)


def _pos_map(t):
    names = _POS.get(t.n, [f"S{i}" for i in range(t.n)])
    return {(t.dealer + i) % t.n: names[i] for i in range(t.n)}


_POT_LABEL = {"RaiseHalfPot": "50%", "RaiseThirdPot": "33%", "RaiseFourthPot": "25%",
              "RaiseTwoThirdPot": "67%", "RaiseThreeFourthPot": "75%",
              "RaisePot": "100%", "RaiseTwoPot": "200%"}


def _fmt_act(action, view):
    """Format an action for clean display: bet/raise Xbb [pot%]."""
    note = action.note
    bb = view.t.bb or 1
    if note in ("fold", "check", "call", "check/fold"):
        return note
    verb = "raise" if view.t.current_bet() > 0 else "bet"
    amt = f"{action.amount / bb:.1f}bb"
    if note == "allin":
        return f"all-in {amt}"
    if note == "minraise":
        return f"{verb} {amt} min"
    if note in _POT_LABEL:
        return f"{verb} {amt} {_POT_LABEL[note]}"
    if note.startswith("betsize_pot"):
        pct = note.split()[-1]
        return f"{verb} {amt} {pct}"
    if note.startswith("betsize"):
        return f"{verb} {amt}"
    return note


def _classify_hand(hole, board):
    """Classify hole+board into (made_hand, draw_or_None)."""
    from ohsim.handeval import evaluate, HIGHCARD, PAIR, TWOPAIR, TRIPS, STRAIGHT, FLUSH, FULLHOUSE, QUADS, STFLUSH
    all_cards = list(hole) + list(board)
    cat, ranks = evaluate(all_cards)
    board_ranks = sorted([c[0] for c in board], reverse=True)
    hole_ranks = [c[0] for c in hole]
    _CAT_NAMES = {STFLUSH: "straight flush", QUADS: "quads", FULLHOUSE: "full house",
                  FLUSH: "flush", STRAIGHT: "straight"}
    if cat in _CAT_NAMES:
        made = _CAT_NAMES[cat]
    elif cat == TRIPS:
        if hole_ranks[0] == hole_ranks[1] and hole_ranks[0] in board_ranks:
            made = "set"
        else:
            made = "trips"
    elif cat == TWOPAIR:
        made = "two pair"
    elif cat == PAIR:
        pr = ranks[0]
        if hole_ranks[0] == hole_ranks[1]:
            made = "overpair" if pr > board_ranks[0] else "pocket pair"
        elif pr == board_ranks[0]:
            made = "top pair"
        elif len(board_ranks) > 1 and pr == board_ranks[1]:
            made = "mid pair"
        else:
            made = "pair"
    else:
        made = "high card"
    draw = None
    if len(board) <= 4:
        for suit in range(4):
            cnt = sum(1 for r, s in all_cards if s == suit)
            if cnt == 4 and sum(1 for r, s in hole if s == suit) >= 1:
                draw = "flush draw"
                break
        rank_set = set(c[0] for c in all_cards)
        best_run = 0
        for lo in range(2, 12):
            run = sum(1 for r in range(lo, lo + 5) if r in rank_set)
            if run > best_run:
                best_run = run
        if best_run == 4 and cat < STRAIGHT:
            draw = ("OESD" if draw is None else draw + " + OESD") if _is_oesd(rank_set) else (
                "gutshot" if draw is None else draw + " + gutshot")
    return (made, draw)


def _is_oesd(rank_set):
    for lo in range(2, 11):
        window = [lo, lo + 1, lo + 2, lo + 3, lo + 4]
        present = [r in rank_set for r in window]
        if sum(present) == 4:
            if not present[0] or not present[4]:
                return True
    return False


def _classify_situation(view):
    """Describe the game situation from the acting player's perspective."""
    t = view.t
    me = view.me
    cb = t.current_bet()
    to_call = cb - me.bet
    street = t.street
    my_chair = me.chair
    pfr_chair = -1
    for rec in t.history:
        if rec.street == 1 and rec.kind in ("raise", "allin-raise"):
            pfr_chair = rec.chair
    is_pfr = (my_chair == pfr_chair)
    if street == 1:
        return "preflop"
    if to_call < 0.01:
        if is_pfr:
            return "c-bet spot"
        return "first to act" if cb < 0.01 else "checked to us"
    raised = False
    for rec in t.history:
        if rec.street == street and rec.kind in ("bet", "raise", "allin-raise"):
            if rec.chair == my_chair:
                raised = True
            elif raised:
                return "facing raise"
    if is_pfr:
        return "facing donk"
    return "facing bet"


def _nuts_on_board(board):
    """True if no possible 2-card combo can beat the board's own 5-card hand."""
    if len(board) != 5:
        return False
    from ohsim.handeval import score
    bs = score(board)
    board_set = set((r, s) for r, s in board)
    remaining = [(r, s) for r in range(2, 15) for s in range(4) if (r, s) not in board_set]
    for i in range(len(remaining)):
        for j in range(i + 1, len(remaining)):
            if score([remaining[i], remaining[j]] + list(board)) > bs:
                return False
    return True


def _is_pure_air(hole, board):
    """True if hole cards are both below the board with no pair and no draw."""
    board_ranks = [c[0] for c in board]
    top = max(board_ranks) if board_ranks else 0
    if hole[0][0] >= top or hole[1][0] >= top:
        return False
    from ohsim.handeval import evaluate, HIGHCARD
    cat, _ = evaluate(list(hole) + list(board))
    if cat > HIGHCARD:
        return False
    all_cards = list(hole) + list(board)
    for suit in range(4):
        if sum(1 for r, s in all_cards if s == suit) >= 4:
            return False
    rank_set = set(c[0] for c in all_cards)
    for lo in range(2, 12):
        if sum(1 for r in range(lo, lo + 5) if r in rank_set) >= 4:
            return False
    return True


def _classify_board(board):
    """Classify board texture. Returns (board_type, straight_possible)."""
    if not board:
        return ("", "")
    ranks = [c[0] for c in board]
    suits = [c[1] for c in board]
    from collections import Counter
    rc = Counter(ranks)
    sc = Counter(suits)
    n = len(board)
    max_rank = max(rc.values())
    max_suit = max(sc.values())
    # board type
    if max_rank == 4:
        btype = "quads"
    elif max_rank == 3 and any(v == 2 for v in rc.values()):
        btype = "full house"
    elif max_rank == 3:
        btype = "trips"
    elif sum(1 for v in rc.values() if v == 2) >= 2:
        btype = "two pair"
    elif max_rank == 2:
        btype = "paired"
    else:
        btype = ""
    # check flush on board (5 cards same suit)
    if n == 5 and max_suit == 5:
        btype = "flush"
    elif n == 5 and max_suit >= 5:
        btype = "flush"
    # check straight on board (5 consecutive)
    if n == 5:
        sorted_r = sorted(set(ranks))
        if len(sorted_r) == 5 and sorted_r[-1] - sorted_r[0] == 4:
            btype = "str. flush" if max_suit == 5 else "straight"
        # wheel
        if set(sorted_r) == {2, 3, 4, 5, 14}:
            btype = "str. flush" if max_suit == 5 else "straight"
    # straight possible (empty at river since board is complete)
    str_poss = ""
    if n < 5:
        rank_set = set(ranks)
        if 14 in rank_set:
            rank_set.add(1)
        for lo in range(1, 12):
            window = set(range(lo, lo + 5))
            on_board = len(window & rank_set)
            if on_board >= 3:
                str_poss = "yes"
                break
    # flush draw possible
    if n < 5 and max_suit >= 2:
        pass  # could add later
    return (btype, str_poss)


class _CoveragePlayer:
    """Wraps a bot to silently record NO-RULE gaps during play."""
    def __init__(self, bot, gaps):
        self.real = bot
        self.name = bot.name
        self._gaps = gaps

    def start_hand(self, view):
        self.real.start_hand(view)

    def new_round(self, view):
        self.real.new_round(view)

    def decide(self, view):
        action = self.real.decide(view)
        ridx = self.real.rt.last_rule_idx
        board = view.t.board
        if board and ridx < 0:
            hole = view.me.hole
            made, draw = _classify_hand(hole, board)
            sit = _classify_situation(view)
            btype, str_poss = _classify_board(board)
            to_call = view.t.current_bet() - view.me.bet
            is_fold = action.kind in ("check", "fold") and to_call > 0.01
            pot = view.t.pot
            odds_pct = (to_call / (pot + to_call) * 100) if (pot + to_call) > 0 else 100
            if is_fold and len(board) == 5 and _nuts_on_board(board):
                self._gaps.append((
                    _STREET_NM[view.t.street],
                    _cards(board), _cards(hole),
                    "NUTS ON BOARD", "FOLD=BUG", sit,
                    btype, str_poss, self.name))
            elif is_fold and to_call > 0.01 and odds_pct < 10:
                self._gaps.append((
                    _STREET_NM[view.t.street],
                    _cards(board), _cards(hole),
                    made, f"FOLD @ {odds_pct:.0f}% odds", sit,
                    btype, str_poss, self.name))
            elif not _is_pure_air(hole, board):
                self._gaps.append((
                    _STREET_NM[view.t.street],
                    _cards(board), _cards(hole),
                    made, draw or "", sit,
                    btype, str_poss, self.name))
        return action

    def after_action(self, view, action):
        self.real.after_action(view, action)

    def end_hand(self, view, result):
        self.real.end_hand(view, result)


def _probe_actions(bot, view, n=20):
    """Probe the bot at different randomheartbeat values to find all mixed actions.
    Returns [(action, freq, rule_idx), ...] where rule_idx is the WHEN clause index (-1 = no match).
    """
    MAIN = {1: "f$preflop", 2: "f$flop", 3: "f$turn", 4: "f$river"}
    DEFAULT = {1: "DefaultPreflop", 2: "DefaultFlop", 3: "DefaultTurn", 4: "DefaultRiver"}
    street = view.t.street
    fn = MAIN[street] if bot.rt.exists(MAIN[street]) else DEFAULT[street]
    rng_state = bot.provider.rng.getstate()
    actions = {}
    for i in range(n):
        rh = (i + 0.5) / n
        bot.provider.bind(view)
        bot.provider.random_heartbeat = rh
        bot.rt.new_decision()
        d = bot.rt.evaluate(fn) if bot.rt.exists(fn) else 0.0
        ridx = bot.rt.last_rule_idx
        a = bot.translate(view, d)
        key = a.note
        if key not in actions:
            actions[key] = [a, 0, ridx]
        actions[key][1] += 1
    bot.provider.rng.setstate(rng_state)
    return [(a, count / n, ridx) for a, count, ridx in actions.values()]


class _StepPlayer:
    """Wraps a bot so decide() pauses for the step-by-step debugger."""
    def __init__(self, real, event, q):
        self.real = real
        self.name = real.name
        self._ev = event
        self._q = q
        self.paused = True
        self.mixed_only = False

    def start_hand(self, view):
        self._had_mixed = False
        self.real.start_hand(view)

    def new_round(self, view):
        self.real.new_round(view)

    def decide(self, view):
        action = self.real.decide(view)
        rh = self.real.provider.random_heartbeat
        if self.paused:
            alts = _probe_actions(self.real, view)
            is_mixed = len(alts) > 1
            if self.mixed_only and not is_mixed:
                return action
            self._had_mixed = True
            self._q.put(("action", view, self.name, action, alts, rh))
            self._ev.wait()
            self._ev.clear()
        return action

    def after_action(self, view, action):
        self.real.after_action(view, action)

    def end_hand(self, view, result):
        self.real.end_hand(view, result)


# ---------------------------------------------------------------- worker mode
def worker(argv):
    """Runs one match: argv = [config.toml, -j, N, (--hh, dir)]."""
    from ohsim.run import main as run_main
    run_main(argv)


# ---------------------------------------------------------------- toml writer
def q(s):
    return "'" + str(s).replace("'", "") + "'"


def write_toml(path, cfg):
    t = cfg["table"]
    lines = ["[table]"]
    lines.append(f"game = \"{t.get('game', 'nlhe')}\"   # nlhe = No-Limit Hold'em, plo = Pot-Limit Omaha")
    for k in ("hands", "sb", "bb", "buyin", "rake", "rake_cap", "seed", "max_stack"):
        lines.append(f"{k} = {t[k]}")
    lines.append(f"shuffle_seats = {'true' if t.get('shuffle_seats', True) else 'false'}"
                 "   # new random seating at the end of every orbit")
    if t.get("shorthanded_pct", 0) > 0:
        lines.append(f"shorthanded_pct = {t['shorthanded_pct']}"
                     "   # % of hands played with fewer players (uniform 2..N)")
    lines.append("# max_stack = buyin: every stack is reset to buyin before each hand;"
                 " larger: top-up below buyin, re-seat above max_stack")
    lines += ["", "[paths]", f"openppl_library = {q(cfg['library'])}", "",
              "[report]", f"positions = {'true' if cfg['positions'] else 'false'}"]
    if cfg.get("pt_all"):
        lines.append("pt_all = true   # compute every PokerTracker stat")
    if cfg.get("pt_file"):
        lines.append(f"pt_file = {q(cfg['pt_file'])}   # text file with every PokerTracker stat of every player")
    lines.append("")
    for p in cfg["players"]:
        lines += ["[[player]]", f"name = {q(p['name'])}", f"profile = {q(p['profile'])}", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- GUI
def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    try:
        from ohsim.run import DEFAULT_LIB
    except Exception:
        DEFAULT_LIB = ""

    root = tk.Tk()
    root.title(f"{APP} v{VERSION} - No-Limit Hold'em and Pot-Limit Omaha simulator")
    root.geometry("1000x720")
    root.minsize(700, 560)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    style.configure("Big.TButton", font=("Segoe UI", 10, "bold"), padding=6)

    # ---------- menu bar
    def text_window(title, text, width=92, height=30):
        w = tk.Toplevel(root)
        w.title(title)
        w.transient(root)
        frame = ttk.Frame(w, padding=10)
        frame.pack(fill="both", expand=True)
        txt = tk.Text(frame, wrap="word", font=("Consolas", 9), width=width, height=height)
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", text)
        txt.configure(state="disabled")
        bar_ = ttk.Frame(w, padding=(10, 0, 10, 10))
        bar_.pack(fill="x")
        ttk.Button(bar_, text="Close", command=w.destroy).pack(side="right")
        return w, bar_

    def show_license():
        try:
            with open(LICENSE_FILE, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = "GNU General Public License v3: https://www.gnu.org/licenses/gpl-3.0.html"
        text_window(f"{APP} - License", text)

    def show_about():
        w, bar_ = text_window(f"About {APP}", ABOUT, width=92, height=30)
        ttk.Button(bar_, text="License…", command=show_license).pack(side="right", padx=6)

    menubar = tk.Menu(root)
    m_file = tk.Menu(menubar, tearoff=False)
    m_file.add_command(label="Load setup…", command=lambda: load_cfg())
    m_file.add_command(label="Save setup…", command=lambda: save_cfg())
    m_file.add_separator()
    m_file.add_command(label="Exit", command=lambda: on_close())
    menubar.add_cascade(label="File", menu=m_file)
    m_help = tk.Menu(menubar, tearoff=False)
    m_help.add_command(label="About OHSim…", command=show_about)
    m_help.add_command(label="License…", command=show_license)
    menubar.add_cascade(label="Help", menu=m_help)
    root.config(menu=menubar)

    state = {"proc": None, "q": queue.Queue(), "report": [], "running": False}

    # ---------- main notebook (Simulation / Hand debugger)
    main_nb = ttk.Notebook(root)
    main_nb.pack(fill="both", expand=True)
    sim_page = ttk.Frame(main_nb)
    main_nb.add(sim_page, text="  Simulation  ")

    # ---------- players
    top = ttk.Frame(sim_page, padding=10)
    top.pack(fill="both", expand=False)
    pl_box = ttk.LabelFrame(top, text="Players at the table (2-10)", padding=8)
    pl_box.pack(side="left", fill="both", expand=False)

    cols = ("name", "profile")
    players = ttk.Treeview(pl_box, columns=cols, show="headings", height=10, selectmode="extended")
    players.heading("name", text="Name")
    players.heading("profile", text="Profile")
    players.column("name", width=130, minwidth=80, stretch=False)
    players.column("profile", width=220, minwidth=120, stretch=True)
    players.pack(side="left", fill="both", expand=True)

    btns = ttk.Frame(pl_box, padding=(8, 0))
    btns.pack(side="left", fill="y")

    def unique_name(base):
        base = re.sub(r"\s+", "_", base) or "Bot"
        names = {players.set(i, "name") for i in players.get_children()}
        if base not in names:
            return base
        k = 2
        while f"{base}_{k}" in names:
            k += 1
        return f"{base}_{k}"

    def add_player(path, name=None):
        if len(players.get_children()) >= 10:
            messagebox.showwarning(APP, "At most 10 players.")
            return
        nm = unique_name(name or os.path.splitext(os.path.basename(path))[0] or "Default")
        players.insert("", "end", values=(nm, path))

    def add_files():
        fs = filedialog.askopenfilenames(title="Choose profiles",
                                         filetypes=[("OpenHoldem/OpenPPL profiles", "*.ohf *.oppl *.txt"),
                                                    ("All files", "*.*")])
        for f in fs:
            add_player(f)

    def add_pop(kind):
        add_player(os.path.join(POP_DIR, f"Pop_{kind}.ohf"), f"Pop_{kind}")

    def remove():
        sel = players.selection()
        if not sel:
            return
        idx = players.index(sel[0])
        for i in sel:
            players.delete(i)
        remaining = players.get_children()
        if remaining:
            new_idx = min(idx, len(remaining) - 1)
            players.selection_set(remaining[new_idx])

    def duplicate():
        for i in players.selection():
            nm, path = players.item(i, "values")
            add_player(path, re.sub(r"_\d+$", "", nm))

    def move(d):
        for i in (players.selection() if d < 0 else reversed(players.selection())):
            players.move(i, "", players.index(i) + d)

    def rename(event):
        i = players.identify_row(event.y)
        if not i or players.identify_column(event.x) != "#1":
            return
        x, y, w, h = players.bbox(i, "#1")
        e = ttk.Entry(players)
        e.insert(0, players.set(i, "name"))
        e.place(x=x, y=y, width=w, height=h)
        e.focus_set()

        def done(_=None):
            v = re.sub(r"\s+", "_", e.get().strip())
            if v:
                players.set(i, "name", v)
            e.destroy()
        e.bind("<Return>", done)
        e.bind("<FocusOut>", done)

    players.bind("<Double-1>", rename)
    btn_add = ttk.Button(btns, text="Add profile…", command=add_files)
    btn_add.pack(fill="x", pady=2)
    pop_mb = ttk.Menubutton(btns, text="Add population ▾")
    pop_menu = tk.Menu(pop_mb, tearoff=False)
    for k, lab in (("reg", "Regular"), ("lag", "LAG (aggressive)"), ("nit", "Nit (tight)"),
                   ("fish", "Fish (passive)"), ("maniac", "Maniac")):
        pop_menu.add_command(label=lab, command=lambda k=k: add_pop(k))
    pop_mb["menu"] = pop_menu
    if os.path.isdir(POP_DIR):                    # ready-made opponents are optional
        pop_mb.pack(fill="x", pady=2)
    btn_dup = ttk.Button(btns, text="Duplicate", command=duplicate)
    btn_dup.pack(fill="x", pady=2)
    btn_rem = ttk.Button(btns, text="Remove", command=remove)
    btn_rem.pack(fill="x", pady=2)
    btn_up = ttk.Button(btns, text="Up ▲", command=lambda: move(-1))
    btn_up.pack(fill="x", pady=(10, 2))
    btn_dn = ttk.Button(btns, text="Down ▼", command=lambda: move(1))
    btn_dn.pack(fill="x", pady=2)
    player_btns = [btn_add, pop_mb, btn_dup, btn_rem, btn_up, btn_dn]
    ttk.Label(btns, text="Double-click a name\nto rename it.", foreground="#666").pack(pady=(10, 0))

    # ---------- middle column (HH + library)
    mid_box = ttk.Frame(top, padding=(10, 0))
    mid_box.pack(side="left", fill="both", expand=True)

    # ---------- settings
    st_box = ttk.LabelFrame(top, text="Settings", padding=8)
    st_box.pack(side="right", fill="y", padx=(10, 0), before=mid_box)   # keeps its width when the window shrinks
    vars_ = {
        "hands": tk.StringVar(value="100000"), "sb": tk.StringVar(value="0.5"), "bb": tk.StringVar(value="1"),
        "buyin": tk.StringVar(value="100"), "rake": tk.StringVar(value="5"), "rake_cap": tk.StringVar(value="5"),
        "workers": tk.StringVar(value=str(max(1, (os.cpu_count() or 2) // 2))), "seed": tk.StringVar(value="1"),
    }
    fields = [("Hands", "hands"), ("Small blind", "sb"), ("Big blind", "bb"), ("Starting stack (big blinds)", "buyin"),
              ("Rake %", "rake"), ("Rake cap (big blinds)", "rake_cap"), ("CPU cores to use", "workers"),
              ("Seed (reproducibility)", "seed")]
    ttk.Label(st_box, text="Game").grid(row=0, column=0, sticky="w", pady=2)
    GAMES = {"No-Limit Hold'em": "nlhe", "Pot-Limit Omaha": "plo"}
    game_var = tk.StringVar(value="No-Limit Hold'em")
    ttk.Combobox(st_box, textvariable=game_var, values=list(GAMES), state="readonly", width=17).grid(
        row=0, column=1, sticky="e", pady=2, padx=(8, 0))
    for r, (lab, k) in enumerate(fields, start=1):
        ttk.Label(st_box, text=lab).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(st_box, textvariable=vars_[k], width=10).grid(row=r, column=1, sticky="e", pady=2, padx=(8, 0))
    STACK_MODES = ["Reset to starting stack every hand", "Cash game: top up, deep up to 3x"]
    stack_mode = tk.StringVar(value=STACK_MODES[0])
    ttk.Label(st_box, text="Stacks between hands").grid(row=len(fields) + 1, column=0, columnspan=2,
                                                        sticky="w", pady=(6, 0))
    ttk.Combobox(st_box, textvariable=stack_mode, values=STACK_MODES, state="readonly", width=32).grid(
        row=len(fields) + 2, column=0, columnspan=2, sticky="ew")
    pos_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(st_box, text="Report by position", variable=pos_var).grid(
        row=len(fields) + 3, column=0, columnspan=2, sticky="w", pady=(6, 0))
    shuffle_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(st_box, text="Shuffle seats every orbit (HU excl.)", variable=shuffle_var).grid(
        row=len(fields) + 5, column=0, columnspan=2, sticky="w")
    pt_all_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(st_box, text="All PokerTracker stats (file with every stat)", variable=pt_all_var).grid(
        row=len(fields) + 4, column=0, columnspan=2, sticky="w")
    sh_var = tk.StringVar(value="0")
    ttk.Label(st_box, text="Short-handed % (0=off)").grid(row=len(fields) + 6, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(st_box, textvariable=sh_var, width=10).grid(row=len(fields) + 6, column=1, sticky="e", pady=(6, 0), padx=(8, 0))
    ttk.Label(st_box, text="% of hands played with fewer opponents\n(uniform between 1 and max opponents)",
              foreground="#666", font=("", 8)).grid(row=len(fields) + 7, column=0, columnspan=2, sticky="w")
    hh_save = tk.BooleanVar(value=False)
    hh_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Documents", "OHSim hand histories"))
    lib_var = tk.StringVar(value=DEFAULT_LIB)

    hh_box = ttk.LabelFrame(mid_box, text="Hand histories", padding=8)
    hh_box.pack(fill="x")
    hh_entry = ttk.Entry(hh_box, textvariable=hh_var)
    hh_browse = ttk.Button(hh_box, text="Browse…", command=lambda: hh_var.set(
        filedialog.askdirectory(title="Folder for the saved hands") or hh_var.get()))

    def hh_toggle():
        st = "normal" if hh_save.get() else "disabled"
        hh_entry.configure(state=st)
        hh_browse.configure(state=st)
    ttk.Checkbutton(hh_box, text="Save the hands played, to import them into PokerTracker",
                    variable=hh_save, command=hh_toggle).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(hh_box, text="Save to folder").grid(row=1, column=0, sticky="w", pady=(4, 0))
    hh_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
    hh_browse.grid(row=1, column=2, pady=(4, 0))
    ttk.Label(hh_box, text="PokerStars text format, one file per table. The hero (whose cards are shown) is the "
                           "first player in the list. About 1 MB every 1,000 hands.",
              foreground="#666").grid(row=2, column=1, columnspan=2, sticky="w", padx=6)
    hh_box.columnconfigure(1, weight=1)
    hh_toggle()

    adv = ttk.LabelFrame(mid_box, text="OpenPPL library", padding=8)
    adv.pack(fill="x", pady=(6, 0))
    ttk.Label(adv, text="Library folder").grid(row=0, column=0, sticky="w")
    ttk.Entry(adv, textvariable=lib_var).grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(adv, text="Browse…", command=lambda: lib_var.set(
        filedialog.askdirectory(title="OpenPPL_Library folder") or lib_var.get())).grid(row=0, column=2)
    adv.columnconfigure(1, weight=1)

    # ---------- run bar
    bar = ttk.Frame(sim_page, padding=10)
    bar.pack(fill="x")
    start_b = ttk.Button(bar, text="▶  Start", style="Big.TButton")
    stop_b = ttk.Button(bar, text="■  Stop", state="disabled")
    start_b.pack(side="left")
    stop_b.pack(side="left", padx=6)
    pt_b = ttk.Button(bar, text="PokerTracker stats…", state="disabled", command=lambda: open_pt_file())
    pt_b.pack(side="left", padx=(18, 0))
    ttk.Button(bar, text="Save setup…", command=lambda: save_cfg()).pack(side="right")
    ttk.Button(bar, text="Load setup…", command=lambda: load_cfg()).pack(side="right", padx=6)
    prog = ttk.Progressbar(sim_page, mode="determinate")
    prog.pack(fill="x", padx=10)
    status = tk.StringVar(value="Add at least two profiles and press Start.")
    ttk.Label(sim_page, textvariable=status, padding=(10, 4)).pack(fill="x")

    # ---------- results
    nb = ttk.Notebook(sim_page)
    nb.pack(fill="both", expand=True, padx=10, pady=(0, 10))
    res_frame = ttk.Frame(nb, padding=6)
    rep_frame = ttk.Frame(nb)
    nb.add(res_frame, text="Results")
    nb.add(rep_frame, text="Full report")
    rcols = ("player", "net", "bb100", "vpip", "pfr")
    results = ttk.Treeview(res_frame, columns=rcols, show="headings", height=8)
    for c, lab, w in (("player", "Player", 140), ("net", "Net (bb)", 90), ("bb100", "bb/100", 80),
                      ("vpip", "VPIP %", 70), ("pfr", "PFR %", 70)):
        results.heading(c, text=lab)
        results.column(c, width=w, minwidth=50, stretch=True, anchor="w" if c == "player" else "e")
    results.tag_configure("win", foreground="#11733a")
    results.tag_configure("loss", foreground="#b42318")
    results.pack(fill="both", expand=True)
    report = tk.Text(rep_frame, wrap="none", font=("Consolas", 9), width=40, height=8)   # small request: follows the window
    ysb = ttk.Scrollbar(rep_frame, orient="vertical", command=report.yview)
    xsb = ttk.Scrollbar(rep_frame, orient="horizontal", command=report.xview)
    report.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
    ysb.pack(side="right", fill="y")
    xsb.pack(side="bottom", fill="x")
    report.pack(fill="both", expand=True)

    # ---------- config helpers
    def collect():
        ps = [dict(zip(cols, players.item(i, "values"))) for i in players.get_children()]
        try:
            t = {"hands": int(vars_["hands"].get()), "sb": float(vars_["sb"].get()), "bb": float(vars_["bb"].get()),
                 "rake": float(vars_["rake"].get()) / 100.0, "seed": int(vars_["seed"].get())}
            # stack and rake cap are entered in big blinds; the engine works in chips
            t["buyin"] = float(vars_["buyin"].get()) * t["bb"]
            t["rake_cap"] = float(vars_["rake_cap"].get()) * t["bb"]
            reset = stack_mode.get() == STACK_MODES[0]
            t["max_stack"] = t["buyin"] if reset else 3 * t["buyin"]
            t["game"] = GAMES[game_var.get()]
            t["shuffle_seats"] = shuffle_var.get()
            t["shorthanded_pct"] = float(sh_var.get() or 0)
            workers = int(vars_["workers"].get())
        except ValueError:
            raise ValueError("Check the settings: they must be numbers.")
        return {"players": ps, "table": t, "workers": workers, "library": lib_var.get(),
                "positions": pos_var.get(), "hh": hh_var.get() if hh_save.get() else "",
                "hh_save": hh_save.get(), "hh_dir": hh_var.get(), "pt_all": pt_all_var.get()}

    def apply(cfg):
        players.delete(*players.get_children())
        for p in cfg.get("players", []):
            players.insert("", "end", values=(p["name"], p["profile"]))
        t = cfg.get("table", {})
        for k in ("hands", "sb", "bb", "seed"):
            if k in t:
                vars_[k].set(str(t[k]))
        bb_ = float(t.get("bb", 1.0)) or 1.0
        for k in ("buyin", "rake_cap"):                   # chips -> big blinds
            if k in t:
                vars_[k].set(f"{float(t[k]) / bb_:g}")
        if "rake" in t:
            vars_["rake"].set(f"{100 * float(t['rake']):g}")
        reset = "max_stack" not in t or float(t["max_stack"]) <= float(t.get("buyin", 100)) + 1e-9
        stack_mode.set(STACK_MODES[0] if reset else STACK_MODES[1])
        game_var.set(next((k for k, v in GAMES.items() if v == t.get("game", "nlhe")), "No-Limit Hold'em"))
        if "workers" in cfg:
            vars_["workers"].set(str(cfg["workers"]))
        lib_var.set(cfg.get("library") or lib_var.get())
        hh_var.set(cfg.get("hh_dir") or cfg.get("hh") or hh_var.get())
        hh_save.set(bool(cfg.get("hh_save", bool(cfg.get("hh")))))
        hh_toggle()
        pos_var.set(cfg.get("positions", True))
        pt_all_var.set(cfg.get("pt_all", True))
        shuffle_var.set(bool(t.get("shuffle_seats", True)))
        sh_var.set(str(t.get("shorthanded_pct", 0)))

    def open_pt_file():
        f = state.get("pt_file")
        csv_f = os.path.splitext(f)[0] + ".csv" if f else None
        if csv_f and os.path.exists(csv_f):
            os.startfile(csv_f)                 # Excel (or the program associated with .csv)
        elif f and os.path.exists(f):
            os.startfile(f)                     # fallback: the text version
        else:
            messagebox.showinfo(APP, "No PokerTracker stats file yet: tick 'All PokerTracker stats' and run.")

    def save_cfg():
        try:
            cfg = collect()
        except ValueError as e:
            messagebox.showerror(APP, str(e))
            return
        f = filedialog.asksaveasfilename(defaultextension=".toml", filetypes=[("Setup", "*.toml")])
        if not f:
            return
        write_toml(f, cfg)
        lines = []
        blinds = spin_collect_structure()
        pays = []
        for iid in pay_tree.get_children():
            v = pay_tree.item(iid, "values")
            pays.append((str(v[0]), str(v[1]), str(v[2])))
        if blinds or pays:
            lines.append("\n[spin]")
            lines.append(f"games = {spin_vars['games'].get()}")
            lines.append(f"starting_stack = {spin_vars['starting_stack'].get()}")
            lines.append(f"hands_per_level = {spin_vars['hands_per_level'].get()}")
            lines.append(f"seed = {spin_vars['seed'].get()}")
            bl = ", ".join(f"[{s}, {b}]" for s, b in blinds)
            lines.append(f"blinds = [{bl}]")
            for multi, freq, split in pays:
                lines.append(f"[[spin.payout]]")
                lines.append(f"multi = \"{multi}\"")
                lines.append(f"freq = {freq}")
                lines.append(f"split = \"{split}\"")
            with open(f, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    def load_cfg():
        import tomllib
        f = filedialog.askopenfilename(filetypes=[("Setup", "*.toml")])
        if not f:
            return
        with open(f, "rb") as fh:
            c = tomllib.load(fh)
        apply({"players": [{"name": p["name"], "profile": p.get("profile", "")} for p in c.get("player", [])],
               "table": c.get("table", {}), "library": c.get("paths", {}).get("openppl_library", ""),
               "positions": c.get("report", {}).get("positions", True)})
        sp = c.get("spin")
        if sp:
            for k in ("games", "starting_stack", "hands_per_level", "seed"):
                if k in sp:
                    spin_vars[k].set(str(sp[k]))
            if "blinds" in sp:
                blind_load([(int(lv[0]), int(lv[1])) for lv in sp["blinds"]])
            if "payout" in sp:
                pay_tree.delete(*pay_tree.get_children())
                for p in sp["payout"]:
                    pay_tree.insert("", "end", values=(p["multi"], p["freq"], p["split"]))

    # ---------- hand debugger (second tab in main notebook)
    dbg_frame = ttk.Frame(main_nb, padding=10)
    main_nb.add(dbg_frame, text="  Hand debugger  ")

    dbg_bar = ttk.Frame(dbg_frame)
    dbg_bar.pack(fill="x", pady=(0, 6))

    dbg_text = tk.Text(dbg_frame, wrap="word", font=("Consolas", 10), state="disabled")
    dbg_ysb = ttk.Scrollbar(dbg_frame, orient="vertical", command=dbg_text.yview)
    dbg_text.configure(yscrollcommand=dbg_ysb.set)
    dbg_ysb.pack(side="right", fill="y")
    dbg_text.pack(fill="both", expand=True)
    dbg_text.tag_configure("hdr", foreground="#1e40af", font=("Consolas", 11, "bold"))
    dbg_text.tag_configure("pos", foreground="#6b7280")
    dbg_text.tag_configure("act", foreground="#047857", font=("Consolas", 10, "bold"))
    dbg_text.tag_configure("alt", foreground="#9ca3af")
    dbg_text.tag_configure("dim", foreground="#d1d5db")
    dbg_text.tag_configure("res", foreground="#7c3aed", font=("Consolas", 10, "bold"))
    dbg_new_b = ttk.Button(dbg_bar, text="New hand", style="Big.TButton")
    dbg_new_b.pack(side="left")
    dbg_step_b = ttk.Button(dbg_bar, text="Step ▶", state="disabled")
    dbg_step_b.pack(side="left", padx=6)
    dbg_auto_b = ttk.Button(dbg_bar, text="Auto ▶▶", state="disabled")
    dbg_auto_b.pack(side="left")
    dbg_mixed_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(dbg_bar, text="Only show actions with multiple choices", variable=dbg_mixed_var).pack(side="left", padx=(12, 0))
    ttk.Label(dbg_bar, text="(each hand is probed to detect randomized decisions)", foreground="gray").pack(side="left", padx=(4, 0))
    dbg_status = tk.StringVar(value="Configure players above, then press New hand.")
    ttk.Label(dbg_bar, textvariable=dbg_status).pack(side="right")

    dbg_st = {"phase": "idle", "event": threading.Event(), "q": queue.Queue(),
              "table": None, "hand_count": 0}

    def dbg_w(text, tag=None):
        dbg_text.configure(state="normal")
        dbg_text.insert("end", text, (tag,) if tag else ())
        dbg_text.configure(state="disabled")
        dbg_text.see("end")

    def dbg_render(view, turn_name=None, action=None, alts=None, rh=None, result=None):
        t = view.t
        pos = _pos_map(t)
        dbg_text.configure(state="normal")
        dbg_text.delete("1.0", "end")
        dbg_text.configure(state="disabled")
        dbg_w(f"Hand #{t.hand_no}    {_STREET_NM[t.street]}    Pot: {t.pot:.1f}\n\n", "hdr")
        if t.board:
            dbg_w(f"Board:  {_cards(t.board)}\n\n")
        for s in t.seats:
            arrow = "►" if turn_name and s.name == turn_name else " "
            p = pos.get(s.chair, "?")
            hole = _cards(s.hole) if s.hole else "??"
            extra = ""
            if s.bet > 0.01:
                extra += f"  bet {s.bet:.1f}"
            if s.allin:
                extra += "  ALL-IN"
            if s.folded:
                extra += "  folded"
            out = s.folded or (not s.hole and t.street > 1)
            if out and arrow != "►":
                dbg_w(f" {arrow} {p:5} {s.name:12} [{hole}]  stack {s.stack:.1f}{extra}\n", "dim")
            else:
                dbg_w(f" {arrow} ", "act" if arrow == "►" else None)
                dbg_w(f"{p:5} ", "pos")
                dbg_w(f"{s.name:12} [{hole}]  stack {s.stack:.1f}{extra}\n")
        dbg_w("\n")
        board_at = {1: [], 2: t.board[:3], 3: t.board[:4], 4: t.board[:5]}
        cur_st = 0
        for rec in t.history:
            if rec.street != cur_st:
                cur_st = rec.street
                bc = _cards(board_at.get(cur_st, [])) if cur_st > 1 else ""
                lbl = f"--- {_STREET_NM[cur_st]} ---" + (f"  [{bc}]" if bc else "")
                dbg_w(f"{lbl}\n", "hdr")
            nm = t.seats[rec.chair].name
            if rec.kind in ("sb", "bb", "ante"):
                dbg_w(f"  {nm}: {rec.kind} {rec.added:.1f}\n", "pos")
            elif rec.kind in ("fold", "check"):
                dbg_w(f"  {nm}: {rec.kind}\n")
            elif rec.kind == "call":
                dbg_w(f"  {nm}: call {rec.added:.1f}\n")
            else:
                dbg_w(f"  {nm}: {rec.kind} to {rec.to:.1f}\n")
        if t.street > cur_st:
            bc = _cards(board_at.get(t.street, [])) if t.street > 1 else ""
            lbl = f"--- {_STREET_NM[t.street]} ---" + (f"  [{bc}]" if bc else "")
            dbg_w(f"{lbl}\n", "hdr")
        if action and alts:
            dbg_w(f"\n  ► {turn_name}:\n", "hdr")
            chosen = action.note
            for alt_act, freq, ridx in sorted(alts, key=lambda x: -x[1]):
                pct = f"{freq * 100:.0f}%"
                label = _fmt_act(alt_act, view)
                rule = "NO RULE" if ridx < 0 else f"Line #{ridx}"
                if alt_act.note == chosen:
                    dbg_w(f"      ● {label} ({pct})  {rule}", "act")
                    if rh is not None:
                        dbg_w(f"  rnd={rh:.2f}", "pos")
                    dbg_w("\n")
                else:
                    dbg_w(f"      - {label} ({pct})  {rule}\n", "alt")
        elif action:
            dbg_w(f"\n  ► {turn_name}: {action.note}", "act")
            if action.kind == "raise":
                dbg_w(f"  (to {action.amount:.1f})", "act")
            dbg_w("\n", "act")
        if result:
            dbg_w("\n--- Result ---\n", "hdr")
            for s in t.seats:
                net = result["payouts"].get(s.chair, 0.0) - s.invested
                if abs(net) > 0.01:
                    sign = "+" if net > 0 else ""
                    dbg_w(f"  {s.name}: {sign}{net:.1f}\n", "res")

    def dbg_new_hand():
        if dbg_st["phase"] not in ("idle", "done"):
            return
        if state["running"]:
            messagebox.showwarning(APP, "Stop the running simulation first.")
            return
        try:
            cfg = collect()
        except ValueError as e:
            messagebox.showerror(APP, str(e))
            return
        if len(cfg["players"]) < 2:
            messagebox.showwarning(APP, "At least two players.")
            return
        missing = [p["profile"] for p in cfg["players"]
                   if p["profile"] and not os.path.exists(p["profile"])]
        if missing:
            messagebox.showerror(APP, "Profile not found:\n" + "\n".join(missing))
            return
        if not os.path.isdir(cfg["library"]):
            messagebox.showerror(APP, "OpenPPL library folder not found:\n" + cfg["library"])
            return
        ev = dbg_st["event"]
        q = dbg_st["q"]
        ev.clear()
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break
        dbg_st["hand_count"] += 1
        dbg_st["phase"] = "stepping"
        dbg_new_b.configure(state="disabled")
        dbg_step_b.configure(state="disabled")
        dbg_auto_b.configure(state="disabled")
        dbg_text.configure(state="normal")
        dbg_text.delete("1.0", "end")
        dbg_text.configure(state="disabled")
        dbg_status.set("Loading profiles…")
        seed_base = int(time.time() * 1000) & 0x7FFFFFFF

        def run():
            from ohsim.engine import Table
            from ohsim.bot import OpenPPLBot, _PARSED
            _PARSED.clear()
            try:
                lib = cfg["library"]
                t_cfg = cfg["table"]
                sps = []
                mixed = dbg_mixed_var.get()
                for p in cfg["players"]:
                    bot = OpenPPLBot(p["name"], p["profile"], lib)
                    sp = _StepPlayer(bot, ev, q)
                    sp.mixed_only = mixed
                    sps.append(sp)
                tbl = Table(sps, [p["name"] for p in cfg["players"]],
                            sb=t_cfg["sb"], bb=t_cfg["bb"], buyin=t_cfg["buyin"],
                            rake=t_cfg["rake"], rake_cap=t_cfg["rake_cap"],
                            seed=seed_base + dbg_st["hand_count"],
                            game=t_cfg.get("game", "nlhe"),
                            max_stack=t_cfg.get("max_stack"))
                dbg_st["table"] = tbl
                for attempt in range(200):
                    result = tbl.play_hand()
                    if not mixed or any(sp._had_mixed for sp in sps):
                        break
                    q.put(("skip", tbl.hand_no))
                q.put(("done", result))
            except Exception as exc:
                q.put(("error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def dbg_do_step():
        if dbg_st["phase"] == "stepping":
            dbg_st["event"].set()

    def dbg_do_auto():
        tbl = dbg_st.get("table")
        if tbl:
            for s in tbl.seats:
                if isinstance(s.player, _StepPlayer):
                    s.player.paused = False
        dbg_st["event"].set()

    def dbg_poll():
        q = dbg_st["q"]
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "skip":
                    dbg_status.set(f"Skipping hand #{msg[1]} (no mixed spots)…")
                elif kind == "turn":
                    _, view, name = msg
                    dbg_render(view, turn_name=name)
                    dbg_step_b.configure(state="normal")
                    dbg_auto_b.configure(state="normal")
                    dbg_status.set(f"Waiting: {name}")
                elif kind == "action":
                    _, view, name, action, alts, rh = msg
                    dbg_render(view, turn_name=name, action=action, alts=alts, rh=rh)
                    dbg_step_b.configure(state="normal")
                    dbg_auto_b.configure(state="normal")
                    dbg_status.set(f"{name}: {action.note}")
                elif kind == "done":
                    tbl = dbg_st["table"]
                    view = tbl.view(0)
                    dbg_render(view, result=msg[1])
                    dbg_st["phase"] = "done"
                    dbg_new_b.configure(state="normal")
                    dbg_step_b.configure(state="disabled")
                    dbg_auto_b.configure(state="disabled")
                    dbg_status.set("Hand complete.")
                elif kind == "error":
                    dbg_status.set(f"Error: {msg[1]}")
                    dbg_st["phase"] = "idle"
                    dbg_new_b.configure(state="normal")
                    dbg_step_b.configure(state="disabled")
                    dbg_auto_b.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, dbg_poll)

    dbg_new_b.configure(command=dbg_new_hand)
    dbg_step_b.configure(command=dbg_do_step)
    dbg_auto_b.configure(command=dbg_do_auto)

    # ---------- coverage analysis (third tab)
    cov_frame = ttk.Frame(main_nb, padding=10)
    main_nb.add(cov_frame, text="  Hole finder  ")

    cov_status = tk.StringVar(value="Configure players, then press Analyze.")
    ttk.Label(cov_frame, textvariable=cov_status).pack(anchor="w", pady=(0, 4))

    cov_bar = ttk.Frame(cov_frame)
    cov_bar.pack(fill="x", pady=(0, 6))
    cov_analyze_b = ttk.Button(cov_bar, text="Analyze", style="Big.TButton")
    cov_analyze_b.pack(side="left")
    ttk.Label(cov_bar, text="Profile:").pack(side="left", padx=(12, 2))
    cov_prof_var = tk.StringVar()
    cov_prof_cb = ttk.Combobox(cov_bar, textvariable=cov_prof_var, state="readonly", width=18)
    cov_prof_cb.pack(side="left")
    ttk.Label(cov_bar, text="Hands:").pack(side="left", padx=(12, 2))
    cov_hands_var = tk.StringVar(value="50000")
    ttk.Spinbox(cov_bar, from_=50000, to=500000, increment=10000,
                textvariable=cov_hands_var, width=8).pack(side="left")
    cov_ignore_board = tk.BooleanVar(value=False)
    ttk.Checkbutton(cov_bar, text="Ignore board for errors", variable=cov_ignore_board).pack(side="left", padx=(12, 0))

    cov_cols = ("street", "board", "hole", "hand", "draw", "situation", "btype", "strposs", "profile")
    cov_tree = ttk.Treeview(cov_frame, columns=cov_cols, show="headings", height=25)
    for col, hdr, w in [("street", "Street", 50), ("board", "Board", 110),
                         ("hole", "Hole", 50), ("hand", "Hand", 80),
                         ("draw", "Draw", 80), ("situation", "Situation", 90),
                         ("btype", "Board type", 75), ("strposs", "Str. poss.", 60),
                         ("profile", "Profile", 75)]:
        cov_tree.heading(col, text=hdr)
        cov_tree.column(col, width=w, minwidth=40)
    cov_tree.tag_configure("gap", foreground="#b42318")
    cov_sb = ttk.Scrollbar(cov_frame, orient="vertical", command=cov_tree.yview)
    cov_tree.configure(yscrollcommand=cov_sb.set)
    cov_sb.pack(side="right", fill="y")
    cov_tree.pack(side="left", fill="both", expand=True)

    cov_state = {"running": False, "raw_gaps": [], "n_hands": 0}

    def cov_refresh_tree():
        cov_tree.delete(*cov_tree.get_children())
        seen = set()
        ign_board = cov_ignore_board.get()
        for row in cov_state["raw_gaps"]:
            if ign_board:
                key = (row[0], row[2], row[3], row[4], row[5], row[6], row[7], row[8])
            else:
                key = tuple(row)
            if key in seen:
                continue
            seen.add(key)
            cov_tree.insert("", "end", values=row, tags=("gap",))
        if cov_state["raw_gaps"]:
            cov_status.set(f"Done — {len(seen)} unique gaps from {cov_state['n_hands']} hands")

    cov_ignore_board.trace_add("write", lambda *_: cov_refresh_tree())

    def cov_run():
        if cov_state["running"]:
            return
        target = cov_prof_var.get()
        if not target:
            messagebox.showwarning(APP, "Select a profile to analyze.")
            return
        try:
            cfg = collect()
        except ValueError as e:
            messagebox.showerror(APP, str(e))
            return
        if len(cfg["players"]) < 2:
            messagebox.showwarning(APP, "At least two players.")
            return
        missing = [p["profile"] for p in cfg["players"]
                   if p["profile"] and not os.path.exists(p["profile"])]
        if missing:
            messagebox.showerror(APP, "Profile not found:\n" + "\n".join(missing))
            return
        if not os.path.isdir(cfg["library"]):
            messagebox.showerror(APP, "OpenPPL library folder not found:\n" + cfg["library"])
            return
        n_hands = int(cov_hands_var.get())
        cov_tree.delete(*cov_tree.get_children())
        cov_state["running"] = True
        cov_analyze_b.configure(state="disabled")
        cov_status.set(f"Analyzing {target}…")
        cov_q = queue.Queue()

        def cov_thread():
            from ohsim.engine import Table
            from ohsim.bot import OpenPPLBot, _PARSED
            _PARSED.clear()
            try:
                lib = cfg["library"]
                t_cfg = cfg["table"]
                gaps = []
                players_list = []
                for p in cfg["players"]:
                    bot = OpenPPLBot(p["name"], p["profile"], lib)
                    if p["name"] == target:
                        players_list.append(_CoveragePlayer(bot, gaps))
                    else:
                        players_list.append(bot)
                seed = int(time.time() * 1000) & 0x7FFFFFFF
                tbl = Table(players_list, [p["name"] for p in cfg["players"]],
                            sb=t_cfg["sb"], bb=t_cfg["bb"], buyin=t_cfg["buyin"],
                            rake=t_cfg["rake"], rake_cap=t_cfg["rake_cap"],
                            seed=seed, game=t_cfg.get("game", "nlhe"),
                            max_stack=t_cfg.get("max_stack"))
                for i in range(n_hands):
                    tbl.play_hand()
                    if (i + 1) % 50 == 0:
                        cov_q.put(("progress", i + 1, n_hands, len(gaps)))
                cov_q.put(("done", gaps))
            except Exception as exc:
                cov_q.put(("error", str(exc)))

        threading.Thread(target=cov_thread, daemon=True).start()

        def cov_poll():
            try:
                while True:
                    msg = cov_q.get_nowait()
                    if msg[0] == "progress":
                        _, done, total, found = msg
                        cov_status.set(f"Hand {done}/{total}… ({found} gaps)")
                    elif msg[0] == "done":
                        cov_state["raw_gaps"] = msg[1]
                        cov_state["n_hands"] = n_hands
                        cov_refresh_tree()
                        cov_state["running"] = False
                        cov_analyze_b.configure(state="normal")
                        return
                    elif msg[0] == "error":
                        cov_status.set(f"Error: {msg[1]}")
                        cov_state["running"] = False
                        cov_analyze_b.configure(state="normal")
                        return
            except queue.Empty:
                pass
            root.after(200, cov_poll)

        cov_poll()

    cov_analyze_b.configure(command=cov_run)

    def cov_copy_all():
        rows = []
        header = "Street\tBoard\tHole\tHand\tDraw\tSituation\tBoard type\tStr. poss.\tProfile"
        rows.append(header)
        for iid in cov_tree.get_children():
            vals = cov_tree.item(iid, "values")
            rows.append("\t".join(str(v) for v in vals))
        txt = "\n".join(rows)
        root.clipboard_clear()
        root.clipboard_append(txt)
        cov_status.set(f"Copied {len(rows)-1} rows to clipboard")

    def cov_copy_sel(event=None):
        sel = cov_tree.selection()
        if not sel:
            return
        rows = []
        for iid in sel:
            vals = cov_tree.item(iid, "values")
            rows.append("\t".join(str(v) for v in vals))
        root.clipboard_clear()
        root.clipboard_append("\n".join(rows))

    cov_copy_b = ttk.Button(cov_bar, text="Copy all", command=cov_copy_all)
    cov_copy_b.pack(side="left", padx=(10, 0))
    cov_tree.bind("<Control-c>", cov_copy_sel)

    def _cov_refresh_profiles(*_a):
        try:
            cfg = collect()
        except Exception:
            return
        names = [p["name"] for p in cfg["players"] if p["profile"]]
        cov_prof_cb["values"] = names
        if names and not cov_prof_var.get():
            cov_prof_var.set(names[0])

    main_nb.bind("<<NotebookTabChanged>>", _cov_refresh_profiles)

    # ---------- Spin & Go tab (fourth tab)
    spin_frame = ttk.Frame(main_nb, padding=10)
    main_nb.add(spin_frame, text="  Spin & Go  ")

    spin_top = ttk.Frame(spin_frame)
    spin_top.pack(fill="x")

    # -- settings
    spin_st = ttk.LabelFrame(spin_top, text="Tournament settings", padding=8)
    spin_st.pack(side="left", fill="y", padx=(0, 10))
    spin_vars = {
        "games": tk.StringVar(value="1000"),
        "starting_stack": tk.StringVar(value="500"),
        "hands_per_level": tk.StringVar(value="16"),
        "seed": tk.StringVar(value="1"),
    }
    for r, (lab, k) in enumerate([("Tournaments", "games"), ("Starting stack (chips)", "starting_stack"),
                                   ("Hands per blind level", "hands_per_level"), ("Seed", "seed")]):
        ttk.Label(spin_st, text=lab).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(spin_st, textvariable=spin_vars[k], width=10).grid(row=r, column=1, sticky="e", pady=2, padx=(8, 0))

    # -- blind structure editor
    blind_box = ttk.LabelFrame(spin_top, text="Blind structure (SB / BB per level)", padding=8)
    blind_box.pack(side="left", fill="both", expand=True, padx=(0, 10))
    blind_cols = ("level", "sb", "bb")
    blind_tree = ttk.Treeview(blind_box, columns=blind_cols, show="headings", height=9, selectmode="browse")
    for c, lab, w in [("level", "Lvl", 40), ("sb", "SB", 60), ("bb", "BB", 60)]:
        blind_tree.heading(c, text=lab)
        blind_tree.column(c, width=w, minwidth=30, stretch=True, anchor="center")
    blind_tree.pack(side="left", fill="both", expand=True)
    blind_sb = ttk.Scrollbar(blind_box, orient="vertical", command=blind_tree.yview)
    blind_tree.configure(yscrollcommand=blind_sb.set)
    blind_sb.pack(side="left", fill="y")

    blind_btns = ttk.Frame(blind_box, padding=(6, 0))
    blind_btns.pack(side="left", fill="y")

    BETCLIC_BLINDS = [(10, 20), (15, 30), (20, 40), (30, 60), (40, 80),
                      (50, 100), (75, 150), (100, 200), (150, 300)]

    def blind_load(levels):
        blind_tree.delete(*blind_tree.get_children())
        for i, (sb, bb) in enumerate(levels, 1):
            blind_tree.insert("", "end", values=(i, sb, bb))

    def blind_add():
        items = blind_tree.get_children()
        if items:
            last = blind_tree.item(items[-1], "values")
            new_sb, new_bb = int(last[1]) * 2, int(last[2]) * 2
        else:
            new_sb, new_bb = 10, 20
        n = len(items) + 1
        blind_tree.insert("", "end", values=(n, new_sb, new_bb))

    def blind_remove():
        sel = blind_tree.selection()
        if sel:
            blind_tree.delete(sel[0])
            for i, iid in enumerate(blind_tree.get_children(), 1):
                vals = blind_tree.item(iid, "values")
                blind_tree.item(iid, values=(i, vals[1], vals[2]))

    def blind_edit(event):
        item = blind_tree.identify_row(event.y)
        col = blind_tree.identify_column(event.x)
        if not item or col == "#1":
            return
        col_idx = int(col.replace("#", "")) - 1
        vals = list(blind_tree.item(item, "values"))
        x, y, w, h = blind_tree.bbox(item, col)
        entry = tk.Entry(blind_tree, width=6, justify="center")
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, vals[col_idx])
        entry.select_range(0, "end")
        entry.focus()

        def confirm(e=None):
            try:
                vals[col_idx] = int(entry.get())
            except ValueError:
                pass
            blind_tree.item(item, values=vals)
            entry.destroy()
        entry.bind("<Return>", confirm)
        entry.bind("<FocusOut>", confirm)

    blind_tree.bind("<Double-1>", blind_edit)
    ttk.Button(blind_btns, text="Add level", command=blind_add, width=12).pack(pady=2)
    ttk.Button(blind_btns, text="Remove", command=blind_remove, width=12).pack(pady=2)
    ttk.Button(blind_btns, text="Betclic preset", command=lambda: blind_load(BETCLIC_BLINDS), width=12).pack(pady=(10, 2))

    blind_load(BETCLIC_BLINDS)

    # -- payout structure
    pay_box = ttk.LabelFrame(spin_top, text="Payouts (multiplier × buyin)", padding=8)
    pay_box.pack(side="left", fill="y")
    pay_cols = ("multi", "freq", "split")
    pay_tree = ttk.Treeview(pay_box, columns=pay_cols, show="headings", height=9, selectmode="browse")
    for c, lab, w in [("multi", "Multi", 55), ("freq", "Freq %", 60), ("split", "Payout", 80)]:
        pay_tree.heading(c, text=lab)
        pay_tree.column(c, width=w, minwidth=40, stretch=True, anchor="center")
    pay_tree.pack(side="left", fill="both", expand=True)

    BETCLIC_PAYOUTS = [
        (2, 47.80, "WTA"), (3, 35.09, "WTA"), (4, 12.00, "WTA"),
        (5, 4.50, "WTA"), (10, 0.50, "WTA"),
        (20, 0.10, "75/15/10"), (100, 0.01, "75/15/10"),
        (1000, 0.001, "75/15/10"),
    ]

    def pay_load(payouts):
        pay_tree.delete(*pay_tree.get_children())
        for entry in payouts:
            multi, freq, split = entry
            pay_tree.insert("", "end", values=(f"{multi}x", freq, split))

    def pay_add():
        pay_tree.insert("", "end", values=("2x", 10, "WTA"))

    def pay_remove():
        sel = pay_tree.selection()
        if sel:
            pay_tree.delete(sel[0])

    def pay_edit(event):
        item = pay_tree.identify_row(event.y)
        col = pay_tree.identify_column(event.x)
        if not item:
            return
        col_idx = int(col.replace("#", "")) - 1
        vals = list(pay_tree.item(item, "values"))
        x, y, w, h = pay_tree.bbox(item, col)
        entry = tk.Entry(pay_tree, width=6, justify="center")
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, vals[col_idx])
        entry.select_range(0, "end")
        entry.focus()

        def confirm(e=None):
            vals[col_idx] = entry.get()
            pay_tree.item(item, values=vals)
            entry.destroy()
        entry.bind("<Return>", confirm)
        entry.bind("<FocusOut>", confirm)

    pay_tree.bind("<Double-1>", pay_edit)
    pay_btns = ttk.Frame(pay_box, padding=(6, 0))
    pay_btns.pack(side="left", fill="y")
    ttk.Button(pay_btns, text="Add", command=pay_add, width=8).pack(pady=2)
    ttk.Button(pay_btns, text="Remove", command=pay_remove, width=8).pack(pady=2)
    ttk.Button(pay_btns, text="Betclic", command=lambda: pay_load(BETCLIC_PAYOUTS), width=8).pack(pady=(10, 2))

    pay_load(BETCLIC_PAYOUTS)

    # -- spin run bar
    spin_bar = ttk.Frame(spin_frame, padding=(0, 6))
    spin_bar.pack(fill="x")
    spin_start_b = ttk.Button(spin_bar, text="▶  Run tournaments", style="Big.TButton")
    spin_stop_b = ttk.Button(spin_bar, text="■  Stop", state="disabled")
    spin_start_b.pack(side="left")
    spin_stop_b.pack(side="left", padx=6)
    spin_prog = ttk.Progressbar(spin_frame, mode="determinate")
    spin_prog.pack(fill="x")
    spin_status = tk.StringVar(value="Set up players in the Simulation tab, then press Run.")
    ttk.Label(spin_frame, textvariable=spin_status, padding=(0, 4)).pack(fill="x")

    # -- spin results
    spin_nb = ttk.Notebook(spin_frame)
    spin_nb.pack(fill="both", expand=True, pady=(0, 6))
    spin_res_frame = ttk.Frame(spin_nb, padding=6)
    spin_rep_frame = ttk.Frame(spin_nb)
    spin_nb.add(spin_res_frame, text="Results")
    spin_nb.add(spin_rep_frame, text="Full report")

    spin_rcols = ("player", "1st", "2nd", "3rd", "avg", "winpct", "roi")
    spin_results = ttk.Treeview(spin_res_frame, columns=spin_rcols, show="headings", height=8)
    for c, lab, w in [("player", "Player", 140), ("1st", "1st %", 65), ("2nd", "2nd %", 65),
                      ("3rd", "3rd+ %", 65), ("avg", "Avg place", 80), ("winpct", "Win %", 65),
                      ("roi", "ROI %", 65)]:
        spin_results.heading(c, text=lab)
        spin_results.column(c, width=w, minwidth=50, stretch=True, anchor="w" if c == "player" else "e")
    spin_results.tag_configure("win", foreground="#11733a")
    spin_results.tag_configure("loss", foreground="#b42318")
    spin_results.pack(fill="both", expand=True)

    spin_report = tk.Text(spin_rep_frame, wrap="none", font=("Consolas", 9), width=40, height=8)
    spin_rep_ysb = ttk.Scrollbar(spin_rep_frame, orient="vertical", command=spin_report.yview)
    spin_report.configure(yscrollcommand=spin_rep_ysb.set)
    spin_rep_ysb.pack(side="right", fill="y")
    spin_report.pack(fill="both", expand=True)

    spin_state = {"thread": None, "running": False, "q": queue.Queue()}

    def spin_collect_structure():
        levels = []
        for iid in blind_tree.get_children():
            v = blind_tree.item(iid, "values")
            levels.append((int(v[1]), int(v[2])))
        return levels

    def spin_collect_payouts():
        payouts = []
        for iid in pay_tree.get_children():
            v = pay_tree.item(iid, "values")
            multi = float(str(v[0]).replace("x", ""))
            freq = float(v[1]) / 100.0
            split_str = str(v[2]).strip()
            if split_str.upper() == "WTA":
                split = (100, 0, 0)
            else:
                parts = [float(x) for x in split_str.split("/")]
                while len(parts) < 3:
                    parts.append(0)
                split = tuple(parts[:3])
            payouts.append((multi, freq, split))
        return payouts

    def spin_start():
        try:
            cfg = collect()
        except ValueError as e:
            messagebox.showerror(APP, str(e))
            return
        if len(cfg["players"]) < 2:
            messagebox.showwarning(APP, "At least two players are needed.")
            return
        missing = [p["profile"] for p in cfg["players"] if p["profile"] and not os.path.exists(p["profile"])]
        if missing:
            messagebox.showerror(APP, "Profile not found:\n" + "\n".join(missing))
            return
        structure = spin_collect_structure()
        if not structure:
            messagebox.showwarning(APP, "Add at least one blind level.")
            return
        n_games = int(spin_vars["games"].get())
        starting_stack = int(spin_vars["starting_stack"].get())
        hands_per_level = int(spin_vars["hands_per_level"].get())
        seed = int(spin_vars["seed"].get())

        spin_results.delete(*spin_results.get_children())
        spin_report.delete("1.0", "end")
        spin_prog.configure(maximum=n_games, value=0)
        spin_status.set("Loading profiles…")
        spin_start_b.configure(state="disabled")
        spin_stop_b.configure(state="normal")

        from ohsim.spin import STRUCTURES
        STRUCTURES["_gui"] = structure

        spin_cfg = {
            "tournament": {
                "games": n_games,
                "structure": "_gui",
                "hands_per_level": hands_per_level,
                "starting_stack": starting_stack,
                "seed": seed,
            },
            "player": [{"name": p["name"], "profile": p["profile"]} for p in cfg["players"]],
            "paths": {"openppl_library": cfg["library"]},
        }
        spin_state["running"] = True

        def run_thread():
            q = spin_state["q"]
            try:
                from ohsim.spin import run_spins
                def out(msg):
                    q.put(("log", msg))
                    import re as _re
                    m = _re.match(r'\s*(\d+)/(\d+) tournaments', msg)
                    if m:
                        q.put(("progress", int(m.group(1))))
                results = run_spins(spin_cfg, n_games=n_games, out=out)
                q.put(("done", results))
            except Exception as e:
                q.put(("error", str(e)))

        t = threading.Thread(target=run_thread, daemon=True)
        spin_state["thread"] = t
        t.start()

    def spin_stop():
        spin_state["running"] = False
        spin_status.set("Stopped.")

    def spin_finish(results):
        spin_state["running"] = False
        spin_start_b.configure(state="normal")
        spin_stop_b.configure(state="disabled")
        if not results or "_meta" not in results:
            return
        meta = results["_meta"]
        n_games = meta["games"]
        n_players = len([k for k in results if k != "_meta"])
        payouts = spin_collect_payouts()

        for name in [k for k in results if k != "_meta"]:
            r = results[name]
            pcts = []
            for p in range(1, n_players + 1):
                pcts.append(f"{100 * r['places'].get(p, 0) / max(n_games, 1):.1f}")
            while len(pcts) < 3:
                pcts.append("-")
            roi_str = "-"
            if payouts:
                total_freq = sum(f for _, f, _ in payouts)
                if total_freq > 0:
                    ev = 0
                    for multi, f, split in payouts:
                        for place_idx, pct in enumerate(split):
                            place_num = place_idx + 1
                            place_rate = r['places'].get(place_num, 0) / max(n_games, 1)
                            ev += f * multi * (pct / 100) * place_rate
                    ev_per_tourn = ev / total_freq
                    roi_pct = (ev_per_tourn - 1) * 100
                    roi_str = f"{roi_pct:+.1f}"
            tag = "win" if r["win_pct"] > 100 / n_players else "loss"
            spin_results.insert("", "end", values=(
                name, pcts[0], pcts[1], pcts[2],
                f"{r['avg_place']:.2f}", f"{r['win_pct']:.1f}", roi_str
            ), tags=(tag,))

        spin_prog.configure(value=n_games)
        spin_status.set(f"{n_games:,} tournaments  ·  "
                        f"{meta['total_hands']:,} hands  ·  "
                        f"avg {meta['avg_hands']:.1f} hands/tourn  ·  "
                        f"{meta['elapsed']:.1f}s")

    def spin_poll():
        try:
            while True:
                msg = spin_state["q"].get_nowait()
                kind, data = msg
                if kind == "progress":
                    spin_prog.configure(value=data)
                    spin_status.set(f"{data:,} / {int(spin_vars['games'].get()):,} tournaments…")
                elif kind == "log":
                    spin_report.insert("end", data + "\n")
                elif kind == "done":
                    spin_finish(data)
                elif kind == "error":
                    spin_state["running"] = False
                    spin_start_b.configure(state="normal")
                    spin_stop_b.configure(state="disabled")
                    spin_status.set(f"Error: {data}")
                    spin_report.insert("end", f"ERROR: {data}\n")
        except queue.Empty:
            pass
        root.after(200, spin_poll)

    spin_start_b.configure(command=spin_start)
    spin_stop_b.configure(command=spin_stop)
    spin_poll()

    # ---------- run
    def start():
        try:
            cfg = collect()
        except ValueError as e:
            messagebox.showerror(APP, str(e))
            return
        if len(cfg["players"]) < 2:
            messagebox.showwarning(APP, "At least two players are needed.")
            return
        missing = [p["profile"] for p in cfg["players"] if p["profile"] and not os.path.exists(p["profile"])]
        if missing:
            messagebox.showerror(APP, "Profile not found:\n" + "\n".join(missing))
            return
        if not os.path.isdir(cfg["library"]):
            messagebox.showerror(APP, "The OpenPPL library folder does not exist:\n" + cfg["library"])
            return
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except OSError:
            pass
        state["pt_file"] = None
        pt_b.configure(state="disabled")
        if cfg.get("pt_all"):
            rep_dir = os.path.join(os.path.expanduser("~"), "Documents", "OHSim reports")
            os.makedirs(rep_dir, exist_ok=True)
            cfg["pt_file"] = os.path.join(rep_dir, time.strftime("pt_stats_%Y%m%d_%H%M%S.txt"))
            state["pt_file"] = cfg["pt_file"]
        tmp = os.path.join(tempfile.gettempdir(), "ohsim_gui_match.toml")
        write_toml(tmp, cfg)
        args = [tmp, "-j", str(max(1, cfg["workers"]))]
        if cfg["hh"]:
            try:
                os.makedirs(cfg["hh"], exist_ok=True)
            except OSError as e:
                messagebox.showerror(APP, f"Cannot create the hand history folder:\n{e}")
                return
            args += ["--hh", cfg["hh"]]
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--worker"] + args
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--worker"] + args
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=HERE, env=env,
                             creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1)
        state.update(proc=p, report=[], running=True, hands=cfg["table"]["hands"])
        results.delete(*results.get_children())
        report.delete("1.0", "end")
        prog.configure(maximum=cfg["table"]["hands"], value=0)
        status.set("Loading profiles…")
        start_b.configure(state="disabled")
        stop_b.configure(state="normal")
        for b in player_btns:
            b.configure(state="disabled")

        def reader():
            for line in p.stdout:
                state["q"].put(line.rstrip("\n"))
            state["q"].put(None)
        threading.Thread(target=reader, daemon=True).start()

    def stop():
        p = state["proc"]
        if p and p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                p.kill()
        status.set("Stopped.")

    def finish():
        state["running"] = False
        start_b.configure(state="normal")
        stop_b.configure(state="disabled")
        for b in player_btns:
            b.configure(state="normal")
        rows = []
        for line in state["report"]:
            m = RESULT_RE.match(line)
            if m:
                rows.append(m.groups())
        names = {p["name"] for p in [dict(zip(cols, players.item(i, "values"))) for i in players.get_children()]}
        rows = [r for r in rows if r[0] in names]
        rows.sort(key=lambda r: float(r[2]), reverse=True)
        for r in rows:
            tag = "win" if float(r[2]) > 0 else "loss"
            results.insert("", "end", values=(r[0], f"{float(r[1]):+.1f}", f"{float(r[2]):+.1f}",
                                                f"{r[3]}", f"{r[4]}"), tags=(tag,))
        if state.get("pt_file") and os.path.exists(state["pt_file"]):
            pt_b.configure(state="normal")
        if rows:
            prog.configure(value=prog["maximum"])
            msg = next((l.strip() for l in state["report"] if "hands in" in l), "Done.")
            if hh_save.get():
                msg += f"   ·   Hands saved in: {hh_var.get()}"
            status.set(msg)
        elif state["proc"] and state["proc"].returncode not in (0, None):
            if not status.get().startswith("Stopped"):
                status.set("Error: see the Full report tab.")
                nb.select(1)

    def poll():
        try:
            while True:
                line = state["q"].get_nowait()
                if line is None:
                    state["proc"].wait()
                    finish()
                    break
                m = PROGRESS_RE.match(line)
                if m:
                    done, tot, el, hpm = map(int, m.groups())
                    prog.configure(value=done)
                    eta = (tot - done) / max(hpm, 1)
                    status.set(f"{done:,} / {tot:,} hands  ·  {hpm:,} hands/min  ·  "
                               f"about {eta:.0f} min left")
                    continue
                state["report"].append(line)
                report.insert("end", line + "\n")
        except queue.Empty:
            pass
        root.after(200, poll)

    start_b.configure(command=start)
    stop_b.configure(command=stop)

    # ---------- RAM monitor (runs every 5s, warns at 85% and offers stop at 92%)
    ram_warned = {"level": 0}

    def get_ram_pct():
        if os.name == "nt":
            import ctypes
            class MEMSTAT(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MEMSTAT(dwLength=ctypes.sizeof(MEMSTAT))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.dwMemoryLoad, ms.ullAvailPhys // (1024 * 1024)
        return 0, 0

    def ram_monitor():
        is_busy = state["running"] or spin_state["running"]
        if is_busy:
            pct, avail_mb = get_ram_pct()
            if pct >= 92 and ram_warned["level"] < 2:
                ram_warned["level"] = 2
                if messagebox.askyesno(APP, f"RAM at {pct}% ({avail_mb} MB free).\n\n"
                                       "Risk of crash! Stop the simulation?"):
                    stop()
                    spin_stop()
            elif pct >= 85 and ram_warned["level"] < 1:
                ram_warned["level"] = 1
                status.set(f"⚠ RAM {pct}% ({avail_mb} MB free) — "
                           + status.get().split(" — ")[-1] if " — " in status.get() else status.get())
            elif pct < 80:
                ram_warned["level"] = 0
        else:
            ram_warned["level"] = 0
        root.after(5000, ram_monitor)

    ram_monitor()

    def on_close():
        if state["running"] and not messagebox.askyesno(APP, "A match is running. Stop it and quit?"):
            return
        stop()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    try:
        with open(SETTINGS, encoding="utf-8") as f:
            apply(json.load(f))
    except (OSError, ValueError, KeyError):
        pass
    poll()
    dbg_poll()
    root.mainloop()


if __name__ == "__main__":
    mp.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2:])
    else:
        gui()
