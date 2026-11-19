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
VERSION = "1.01"
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
of service of most poker sites."""
SETTINGS = os.path.join(os.path.expanduser("~"), ".ohsim_gui.json")
POP_DIR = os.path.join(HERE, "profiles", "population")
RESULT_RE = re.compile(r"^(\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%")
PROGRESS_RE = re.compile(r"^\s*(\d+)/(\d+) hands, (\d+)s \((\d+) hands/min\)")


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
    root.title(f"{APP} - No-Limit Hold'em and Pot-Limit Omaha simulator")
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

    # ---------- players
    top = ttk.Frame(root, padding=10)
    top.pack(fill="both", expand=False)
    pl_box = ttk.LabelFrame(top, text="Players at the table (2-10)", padding=8)
    pl_box.pack(side="left", fill="both", expand=True)

    cols = ("name", "profile")
    players = ttk.Treeview(pl_box, columns=cols, show="headings", height=9, selectmode="extended")
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
        for i in players.selection():
            players.delete(i)

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
    ttk.Button(btns, text="Add profile…", command=add_files).pack(fill="x", pady=2)
    pop_mb = ttk.Menubutton(btns, text="Add population ▾")
    pop_menu = tk.Menu(pop_mb, tearoff=False)
    for k, lab in (("reg", "Regular"), ("lag", "LAG (aggressive)"), ("nit", "Nit (tight)"),
                   ("fish", "Fish (passive)"), ("maniac", "Maniac")):
        pop_menu.add_command(label=lab, command=lambda k=k: add_pop(k))
    pop_mb["menu"] = pop_menu
    if os.path.isdir(POP_DIR):                    # ready-made opponents are optional
        pop_mb.pack(fill="x", pady=2)
    ttk.Button(btns, text="Duplicate", command=duplicate).pack(fill="x", pady=2)
    ttk.Button(btns, text="Remove", command=remove).pack(fill="x", pady=2)
    ttk.Button(btns, text="Up ▲", command=lambda: move(-1)).pack(fill="x", pady=(10, 2))
    ttk.Button(btns, text="Down ▼", command=lambda: move(1)).pack(fill="x", pady=2)
    ttk.Label(btns, text="Double-click a name\nto rename it.", foreground="#666").pack(pady=(10, 0))

    # ---------- settings
    st_box = ttk.LabelFrame(top, text="Settings", padding=8)
    st_box.pack(side="right", fill="y", padx=(10, 0), before=pl_box)   # keeps its width when the window shrinks
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
    ttk.Checkbutton(st_box, text="Shuffle seats every orbit", variable=shuffle_var).grid(
        row=len(fields) + 5, column=0, columnspan=2, sticky="w")
    pt_all_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(st_box, text="All PokerTracker stats (file with every stat)", variable=pt_all_var).grid(
        row=len(fields) + 4, column=0, columnspan=2, sticky="w")
    hh_save = tk.BooleanVar(value=False)
    hh_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Documents", "OHSim hand histories"))
    lib_var = tk.StringVar(value=DEFAULT_LIB)

    hh_box = ttk.LabelFrame(root, text="Hand histories", padding=8)
    hh_box.pack(fill="x", padx=10)
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

    adv = ttk.LabelFrame(root, text="OpenPPL library", padding=8)
    adv.pack(fill="x", padx=10, pady=(6, 0))
    ttk.Label(adv, text="Library folder").grid(row=0, column=0, sticky="w")
    ttk.Entry(adv, textvariable=lib_var).grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(adv, text="Browse…", command=lambda: lib_var.set(
        filedialog.askdirectory(title="OpenPPL_Library folder") or lib_var.get())).grid(row=0, column=2)
    adv.columnconfigure(1, weight=1)

    # ---------- run bar
    bar = ttk.Frame(root, padding=10)
    bar.pack(fill="x")
    start_b = ttk.Button(bar, text="▶  Start", style="Big.TButton")
    stop_b = ttk.Button(bar, text="■  Stop", state="disabled")
    start_b.pack(side="left")
    stop_b.pack(side="left", padx=6)
    pt_b = ttk.Button(bar, text="PokerTracker stats…", state="disabled", command=lambda: open_pt_file())
    pt_b.pack(side="left", padx=(18, 0))
    ttk.Button(bar, text="Save setup…", command=lambda: save_cfg()).pack(side="right")
    ttk.Button(bar, text="Load setup…", command=lambda: load_cfg()).pack(side="right", padx=6)
    prog = ttk.Progressbar(root, mode="determinate")
    prog.pack(fill="x", padx=10)
    status = tk.StringVar(value="Add at least two profiles and press Start.")
    ttk.Label(root, textvariable=status, padding=(10, 4)).pack(fill="x")

    # ---------- results
    nb = ttk.Notebook(root)
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
        if f:
            write_toml(f, cfg)

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
        rows = []
        for line in state["report"]:
            m = RESULT_RE.match(line)
            if m:
                rows.append(m.groups())
        names = {p["name"] for p in [dict(zip(cols, players.item(i, "values"))) for i in players.get_children()]}
        rows = [r for r in rows if r[0] in names]
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
    root.mainloop()


if __name__ == "__main__":
    mp.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2:])
    else:
        gui()
