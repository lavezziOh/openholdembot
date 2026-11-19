"""Extract the catalogue of OpenHoldem built-in symbols from the C++ sources
(CSymbolEngine*::SymbolsProvided) into ohsim/oh_symbols.txt."""
import re, sys, glob, os

src = sys.argv[1]          # .../openholdembot/OpenHoldem
out = sys.argv[2]
names = set()
for f in glob.glob(os.path.join(src, "CSymbolEngine*.cpp")):
    text = open(f, encoding="latin-1").read()
    m = re.search(r"::SymbolsProvided\(\)\s*\{(.*?)\n\}", text, re.S)
    if not m:
        continue
    body = m.group(1)
    for lit in re.findall(r'"([^"]*)"', body):
        if "%" in lit:
            continue
        names.update(lit.split())
    for fmt, lo, hi in re.findall(r'RangeOfSymbols\("([^"]+)",\s*(\w+),\s*(\w+)\)', body):
        consts = {"kFirstChair": 0, "kLastChair": 9, "kLastChairOmaha": 9,
                  "kBetroundPreflop": 1, "kBetroundFlop": 2, "kBetroundTurn": 3,
                  "kBetroundRiver": 4, "kMaxNumberOfPlayers": 10,
                  "kMaxChairNumber": 9, "kNumberOfBetrounds": 4, "StdDeck_Suit_FIRST": 0, "StdDeck_Suit_LAST": 3, "kLastDebugIndex": 9}
        def val(x):
            if x.isdigit():
                return int(x)
            if x in consts:
                return consts[x]
            print("unknown constant", x, "in", os.path.basename(f))
            return 0
        lo_v, hi_v = val(lo), val(hi)
        for i in range(lo_v, hi_v + 1):
            names.add(fmt.replace("%i", str(i)).replace("%d", str(i)))
names = sorted(n for n in names if re.fullmatch(r"[A-Za-z_$][\w$]*", n))
open(out, "w").write("\n".join(names) + "\n")
print(len(names), "symbols")
