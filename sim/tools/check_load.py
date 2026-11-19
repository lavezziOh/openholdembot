"""Load library + default bot + profiles with the Python parser and summarise errors."""
import sys, os, time, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ohsim.oppl.runtime import Runtime
from ohsim.catalog import CatalogProvider

LIB = r"C:\Python313\AAA-BOTS\oh\openholdembot\OpenPPL\OpenPPL_Library"
BOT = r"C:\Python313\AAA-BOTS\oh\openholdembot\##_OpenHoldem_Release_Directory_##\bot_logic\DefaultBot"

profiles = sys.argv[1:] or [None]
for prof in profiles:
    t = time.time()
    rt = Runtime(CatalogProvider())
    rt.load_standard(LIB, BOT)
    if prof and prof != "-":
        rt.load_file(prof)
    rt.finish_loading()
    dt = time.time() - t
    print(f"=== {prof or 'library + DefaultBot'}: {len(rt.objects)} objects, {dt:.2f}s, errors {len(rt.errors)}")
    kinds = collections.Counter()
    for f, l, m in rt.errors:
        k = m.split(": ", 1)[1] if ": " in m else m
        if "old style" in k:
            k = "old style list format"
        kinds[(os.path.basename(f), k[:100])] += 1
    for (f, k), c in kinds.most_common(40):
        print(f"  {c:4} {f}: {k}")
