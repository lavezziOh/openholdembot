"""PokerTracker statistics inside the simulator, with OpenHoldem's own definitions.

Every played hand is turned into PokerTracker per-hand rows (ohsim.ptrows) and
stored in an in-memory SQLite copy of the relevant PokerTracker tables. The
OpenHoldem queries (ohsim/pt_queries.json, from PokerTracker_Queries_Version_4.h)
are run on it, translated from PostgreSQL to SQLite, and summed per player
incrementally (every query is additive: count / opportunities or a plain sum).

Symbols: pt_<stat><chair>, e.g. pt_vpip3, pt_flop_cbet0; the runtime multiplexes
pt_3bet_raischair -> pt_3bet<chair>. No data -> -1 (as OpenHoldem's queries).
"""
import json
import os
import re
import sqlite3

from . import ptdb
from .handlog import from_table
from .ptrows import rows as pt_rows, _empty, positions as pt_positions

HERE = os.path.dirname(os.path.abspath(__file__))
LOOKUP_POSITIONS = os.path.join(HERE, "data", "pt_lookup_positions.json")
ROW_COLS = [c for c in _empty() if not c.startswith("action_")] + [
    "position", "cnt_players", "cnt_players_lookup_position", "amt_before"]


def to_sqlite(sql):
    s = sql.replace("%TYPE%", "cash").replace("%GAMETYPE%", "1").replace("%SITEID%", "1")
    # placeholders of the fixed OpenHoldem queries (limit table differs between cash and tournaments)
    s = s.replace("%LIMIT_TABLE%", "cash_limit").replace("%LIMIT_ID%", "id_limit").replace("%LIMIT_NAME%", "limit_name")
    s = re.sub(r"substring\((.+?) from (\d+) for (\d+)\)", r"substr(\1, \2, \3)", s, flags=re.I)
    # NULLIF(substr(...), '')::int (empty-string-safe casts of the fixed queries)
    s = re.sub(r"NULLIF\((substr\([^()]*\)),\s*''\)::int", r"CAST(NULLIF(\1, '') AS INTEGER)", s)
    s = re.sub(r"\bSELECT\(", "SELECT (", s)
    s = re.sub(r"\bFROM\(", "FROM (", s)
    s = re.sub(r"then - 1", "then -1", s)
    s = re.sub(r"\bchar_length\(", "length(", s, flags=re.I)
    s = re.sub(r"(substr\([^()]*\))::int", r"CAST(\1 AS INTEGER)", s)
    s = re.sub(r"=\s*'t'", "= 1", s)           # PostgreSQL boolean literals
    s = re.sub(r"=\s*'f'", "= 0", s)
    return s


_NULLIF_RATIO = re.compile(
    r"as pid, \(\s*cast\((sum\((?:(?!cast\().)*?\)) as decimal\)\s*/\s*"
    r"NULLIF\(\s*cast\((sum\((?:(?!cast\().)*?\)) as decimal\)\s*,\s*0\s*\)\s*\)\s*as result",
    re.I | re.S)


class PTStats:
    def __init__(self, refresh_every=25, limit_name="$0.50/$1 NL (6 max)", bb=1.0):
        self.db = sqlite3.connect(":memory:")
        self.refresh_every = refresh_every
        self.defs = {}
        for name, q in ptdb.definitions().items():
            norm = re.sub(r"\bFROM\(", "FROM (", re.sub(r"\bSELECT\(", "SELECT (", q["sql"]))
            ratio = ptdb.split_ratio(norm.replace("then - 1", "then -1"))
            try:
                per = to_sqlite(ptdb.per_player_sql(q["sql"], "S.id_hand > ?"))
            except (ValueError, AssertionError):
                continue
            if ratio is None:
                # "cast(sum(A) as decimal) / NULLIF(cast(sum(B) as decimal), 0)" (queries with a zero guard)
                # is a ratio too: summing the per-batch ratios was wrong, and no data gave 0 instead of -1
                m = _NULLIF_RATIO.search(per)
                if m:
                    per = per[:m.start()] + f"as pid, {m.group(1)} as num, {m.group(2)} as den" + per[m.end():]
                    ratio = True
            self.defs[name] = (per, ratio is not None)
        self.names = sorted(self.defs, key=len, reverse=True)
        self.wanted = set()             # stats actually used by the bots (None = all)
        self.broken = {}                # stat -> SQLite error: not computable in the simulator
        self.acc = {}                   # (player, stat) -> [num, den] or [sum]
        self.players = {}
        self.actions = {}
        self.hand_id = 0
        self.flushed_id = 0
        self.pending = 0
        self._schema(limit_name, bb)

    # -------------------------------------------------------------- schema
    def _schema(self, limit_name, bb):
        c = self.db.cursor()
        bools = [k for k, v in _empty().items() if isinstance(v, bool)]
        coldefs = ", ".join(f"{k} {'INTEGER' if k in bools or k.startswith('cnt') or k in ('position', 'cnt_players', 'cnt_players_lookup_position', 'val_p_raise_aggressor_pos') else 'REAL' if k.startswith('amt') else 'TEXT'}"
                            for k in ROW_COLS)
        c.execute(f"CREATE TABLE cash_hand_player_statistics (id_hand INTEGER, id_player INTEGER, "
                  f"id_gametype INTEGER, id_limit INTEGER, id_action_p INTEGER, id_action_f INTEGER, "
                  f"id_action_t INTEGER, id_action_r INTEGER, flg_hero INTEGER DEFAULT 0, {coldefs})")
        c.execute("CREATE INDEX s_hand ON cash_hand_player_statistics(id_hand)")
        c.execute("CREATE TABLE player (id_player INTEGER PRIMARY KEY, player_name TEXT, id_site INTEGER)")
        c.execute("CREATE TABLE lookup_actions (id_action INTEGER PRIMARY KEY, action TEXT)")
        c.execute("CREATE TABLE cash_limit (id_limit INTEGER PRIMARY KEY, limit_name TEXT, amt_bb REAL)")
        c.execute("CREATE TABLE cash_hand_summary (id_hand INTEGER PRIMARY KEY, id_limit INTEGER, "
                  "id_gametype INTEGER, cnt_players INTEGER, str_aggressors_p TEXT, str_aggressors_f TEXT)")
        lp = json.load(open(LOOKUP_POSITIONS))
        cols = list(lp[0])
        c.execute(f"CREATE TABLE lookup_positions ({', '.join(cols)})")
        conv = lambda v: 1 if v == "t" else 0 if v == "f" else v
        c.executemany(f"INSERT INTO lookup_positions VALUES ({','.join('?' * len(cols))})",
                      [[conv(r[k]) for k in cols] for r in lp])
        c.execute("INSERT INTO cash_limit VALUES (1, ?, ?)", (limit_name, bb))
        self._action_id("")

    def _action_id(self, s):
        i = self.actions.get(s)
        if i is None:
            i = self.actions[s] = len(self.actions)
            self.db.execute("INSERT INTO lookup_actions VALUES (?, ?)", (i, s))
        return i

    def _player_id(self, name):
        i = self.players.get(name)
        if i is None:
            i = self.players[name] = len(self.players) + 1
            self.db.execute("INSERT INTO player VALUES (?, ?, 1)", (i, name))
        return i

    # -------------------------------------------------------------- feeding
    def add_hand(self, handlog):
        self.hand_id += 1
        R = pt_rows(handlog)
        recs = []
        for name, r in R.items():
            recs.append([self.hand_id, self._player_id(name), 1, 1,
                         self._action_id(r["action_p"]), self._action_id(r["action_f"]),
                         self._action_id(r["action_t"]), self._action_id(r["action_r"]), 0]
                        + [int(r[k]) if isinstance(r[k], bool) else r[k] for k in ROW_COLS])
        self.db.executemany(f"INSERT INTO cash_hand_player_statistics VALUES ({','.join('?' * len(recs[0]))})", recs)
        pos = pt_positions(handlog)
        aggr = "8" + "".join(str(pos[a.name]) for a in handlog.actions
                             if a.street == 1 and a.kind in ("raise", "bet"))
        aggr_f = "".join(str(pos[a.name]) for a in handlog.actions if a.street == 2 and a.kind in ("raise", "bet"))
        self.db.execute("INSERT INTO cash_hand_summary VALUES (?, 1, 1, ?, ?, ?)",
                        (self.hand_id, len(R), aggr, aggr_f))
        self.pending += 1
        if self.pending >= self.refresh_every:
            self.flush()

    def update(self, table, result=None):
        """Compatible with stats.PlayerStats.update (called after every hand)."""
        self.add_hand(from_table(table, result or {"payouts": {s.chair: 0 for s in table.seats}}))

    def flush(self):
        if not self.pending:
            return
        stats = self.defs if self.wanted is None else {k: v for k, v in self.defs.items() if k in self.wanted}
        names = {v: k for k, v in self.players.items()}
        for stat, (sql, ratio) in stats.items():
            if stat in self.broken:
                continue
            try:
                rows = list(self.db.execute(sql, (self.flushed_id,)))
            except sqlite3.Error as e:
                # a query needing data the simulator does not record: skipped (the bots get -1, like no data)
                self.broken[stat] = str(e)
                continue
            for row in rows:
                key = (names[row[0]], stat)
                a = self.acc.setdefault(key, [0.0, 0.0])
                if ratio:
                    a[0] += row[1] or 0
                    a[1] += row[2] or 0
                else:
                    a[0] += row[1] if row[1] is not None and row[1] >= 0 else 0
        self.flushed_id = self.hand_id
        self.pending = 0
        # keep the table small: old rows are already summed
        self.db.execute("DELETE FROM cash_hand_player_statistics WHERE id_hand <= ?", (self.flushed_id,))
        self.db.execute("DELETE FROM cash_hand_summary WHERE id_hand <= ?", (self.flushed_id,))

    # -------------------------------------------------------------- reading
    def value(self, player, stat):
        a = self.acc.get((player, stat))
        if stat not in self.defs:
            raise KeyError(stat)
        if a is None:
            return -1.0 if self.defs[stat][1] else 0.0
        if self.defs[stat][1]:
            return a[0] / a[1] if a[1] else -1.0
        return a[0]

    def parse(self, name):
        """pt_<stat><chair> -> (stat, chair)."""
        body = name[3:]
        if not body or not body[-1].isdigit():
            raise KeyError(name)
        stat, chair = body[:-1], int(body[-1])
        if stat not in self.defs:
            raise KeyError(name)
        return stat, chair

    def symbol(self, name, view):
        stat, chair = self.parse(name)
        if chair >= view.t.n:
            return -1.0
        return self.value(view.t.seats[chair].name, stat)

    def want(self, identifiers):
        """Restrict the computation to the stats used by the bots (pt_ identifiers of the
        profiles, also multiplexed ones such as pt_vpip_raischair, or plain stat names).
        Returns the pt_ identifiers whose stat OpenHoldem does not define."""
        from .oppl.runtime import Runtime
        unknown = set()
        for n in identifiers:
            if n in self.defs:
                self.wanted.add(n)
                continue
            if not n.startswith("pt_"):
                continue
            pure = Runtime._multiplex_pure(n)
            stat = (pure or n)[3:]
            if pure is None and stat[-1:].isdigit():
                stat = stat[:-1]
            if stat in self.defs:
                self.wanted.add(stat)
            else:
                unknown.add(n)
        return unknown
