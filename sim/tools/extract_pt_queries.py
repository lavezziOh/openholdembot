"""Extract OpenHoldem's PokerTracker 4 stat definitions (name, description, SQL)
from PokerTracker_Queries_Version_4.h into ohsim/pt_queries.json.

    python tools/extract_pt_queries.py <PokerTracker_Queries_Version_4.h> ohsim/pt_queries.json
"""
import json
import re
import sys

STR = r'"(?:[^"\\]|\\.)*"'

src, out = sys.argv[1], sys.argv[2]
text = open(src, encoding="latin-1").read()
body = text[text.index("t_QueryDefinition query_definitions"):]
enabled = {m.group(1): m.group(2) == "TRUE"
           for m in re.finditer(r"#define\s+(PT4_QUERY_SUPPORT__\w+)\s+\((TRUE|FALSE)\)", text)}
# Comments are optional and free text: some entries have no "// stat_group" line, others "// query: ..." with text
COMMENT = r"(?:\s*//[^\n]*)*"
entry = re.compile(r"\{" + COMMENT + r"\s*(" + STR + r")\s*," + COMMENT + r"\s*(" + STR + r")\s*,"
                   + COMMENT + r"\s*((?:" + STR + r"\s*)+)," + COMMENT + r"\s*(pt_group_\w+)", re.S)
defs = []
for m in re.finditer(r"#if\s+(PT4_QUERY_SUPPORT__\w+)(.*?)#endif", body, re.S):
    if not enabled.get(m.group(1), False):
        continue
    for e in entry.finditer(m.group(2)):
        parts = re.findall(STR, e.group(3), re.S)
        sql = "".join(p[1:-1] for p in parts)
        sql = re.sub(r"\\\s*", " ", sql)            # C line continuations
        sql = re.sub(r"\s+", " ", sql).strip()
        defs.append({"name": e.group(1)[1:-1], "description": e.group(2)[1:-1],
                     "group": e.group(4).replace("pt_group_", ""), "sql": sql})
json.dump(defs, open(out, "w"), indent=1)
print(len(defs), "queries")
