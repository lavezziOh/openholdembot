"""Read-only access to the local PokerTracker 4 PostgreSQL database, and the
OpenHoldem PokerTracker stat definitions (ohsim/pt_queries.json, extracted from
OpenHoldem's PokerTracker_Queries_Version_4.h).

The connection data (user / password) are read from PokerTracker's own config
at run time and never printed or stored.
"""
import csv
import io
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PT_CFG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PokerTracker 4", "Config", "PokerTracker.cfg")
PSQL = r"C:\Program Files\PostgreSQL\12\bin\psql.exe"
QUERIES = os.path.join(HERE, "pt_queries.json")


def _cfg(db_index=1):
    vals = {}
    for line in open(PT_CFG, encoding="latin-1"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            vals[k] = v
    p = f"DB{db_index}.Postgres."
    return {"host": vals.get(p + "Server", "localhost"), "port": vals.get(p + "Port", "5432"),
            "db": vals.get(p + "Database", "PT4 DB"), "user": vals.get(p + "User", "postgres"),
            "password": vals.get(p + "Password", "")}


def query(sql, db=None):
    """Run a read-only SQL query; returns a list of dicts."""
    c = _cfg()
    env = dict(os.environ, PGPASSWORD=c["password"], PGOPTIONS="-c default_transaction_read_only=on")
    r = subprocess.run([PSQL, "-h", c["host"], "-p", c["port"], "-U", c["user"], "-d", db or c["db"],
                        "--csv", "-c", sql], capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:2000])
    return list(csv.DictReader(io.StringIO(r.stdout)))


# ------------------------------------------------------------ OH stat definitions
def definitions():
    return {q["name"]: q for q in json.load(open(QUERIES))}


_OUTER = re.compile(r"^SELECT \(case when (\w+) = 0 then -1 else cast\((\w+) as real\) / (\w+) end\) as result FROM \(", re.I)


def split_ratio(sql):
    """(numerator alias, denominator alias) of an OH 'count / opportunities' query, or None."""
    m = _OUTER.match(sql)
    if not m or m.group(1) != m.group(3):
        return None
    return m.group(2), m.group(1)


def per_player_sql(sql, where_extra, type_="cash"):
    """Turn an OH stat query (one player) into a query grouped by player that returns
    pid, num, den (numerator and denominator), or pid, result for other forms."""
    s = sql.replace("%TYPE%", type_)
    s = re.sub(r"S\.id_gametype = %GAMETYPE%", "TRUE", s)
    s = re.sub(r"P\.id_site = %SITEID%", "TRUE", s)
    s = s.replace("P.player_name LIKE '%SCREENNAME%'", f"({where_extra})")
    s = re.sub(r"\bSELECT\(", "SELECT (", s)
    s = re.sub(r"\bFROM\(", "FROM (", s)
    s = s.replace("then - 1", "then -1")
    if "FROM (" not in s:
        # plain form: SELECT <expr> as result FROM player as P, ... WHERE ...
        m = re.match(r"SELECT (.+?) as result FROM (.+)$", s, re.I | re.S)
        if not m:
            raise ValueError("unexpected query form")
        return f"SELECT P.id_player as pid, {m.group(1)} as result FROM {m.group(2)} GROUP BY P.id_player"
    inner_start = s.index("FROM (") + len("FROM (")
    if not s[inner_start:].upper().startswith("SELECT "):
        raise ValueError("unexpected query form")
    s = s[:inner_start] + "SELECT P.id_player as pid, " + s[inner_start + 7:]
    assert s.endswith(") foo")
    s = s[:-len(") foo")] + " GROUP BY P.id_player) foo"
    ratio = split_ratio(s)
    if ratio:
        num, den = ratio
        return re.sub(r"^SELECT \(case.*?end\) as result FROM \(",
                      f"SELECT pid, {num} as num, {den} as den FROM (", s, count=1, flags=re.I)
    return s.replace("SELECT (case", "SELECT pid, (case", 1)
