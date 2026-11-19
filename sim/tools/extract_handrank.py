"""Extract OpenHoldem's handrank tables (CSymbolEngineHandrank.cpp) into ohsim/handrank.json."""
import json, re, sys

src, out = sys.argv[1], sys.argv[2]
text = open(src, encoding="latin-1").read()

def block(name):
    i = text.index(name)
    start = text.index("{", i)
    depth, j = 0, start
    while True:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1

t2652 = [[int(x) for x in re.findall(r"-?\d+", row)]
         for row in re.findall(r"\{([^{}]*)\}", block("handrank_table_2652"))]
t169 = [re.findall(r'"([^"]+)"', row)
        for row in re.findall(r"\{([^{}]*)\}", block("handrank_table_169"))]
assert all(len(r) == 169 for r in t169), [len(r) for r in t169]
assert all(len(r) == 169 for r in t2652), [len(r) for r in t2652]
json.dump({"t169": t169, "t2652": t2652}, open(out, "w"))
print(len(t169), "tables of 169 hands")
