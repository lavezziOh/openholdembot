"""OpenPPL runtime: loads library + profile, compiles parse trees to closures
and evaluates functions with OpenHoldem semantics.

The runtime does not know anything about poker by itself: game symbols
(balance, betround, pokerval, ...) come from a *provider* object with
    provider.symbol(name) -> float      (raise KeyError if unknown)
    provider.known(name)  -> bool       (load-time validation)
    provider.hole_cards() / provider.board_cards() -> [(rank 2..14, suit 0..3)]
"""
import math
import os
from collections import Counter

from .parser import (split_file, FunctionParser, parse_list, walk_identifiers,
                     MAIN_FUNCTIONS)

EPS = 1e-6
K_UNDEFINED = -1.0
MAX_RECURSION = 100

# Order as in OpenHoldem (DLLs/Files_DLL/Files.h: kOpenPPLLibraries)
OPENPPL_LIBRARIES = [
    "OpenPPL_Library_Ante_Others_IsFinalTable.ohf",
    "OpenPPL_Library_Bets_Pots_And_Stacks.ohf",
    "OpenPPL_Library_Betting_Action_Symbols.ohf",
    "OpenPPL_Library_Board_Symbols.ohf",
    "OpenPPL_Library_ChipAmounts.ohf",
    "OpenPPL_Library_Constants.ohf",
    "OpenPPL_Library_Deprecated.ohf",
    "OpenPPL_Library_HandStrength_Symbols.ohf",
    "OpenPPL_Library_History_Symbols.ohf",
    "OpenPPL_Library_HoldEm_HandStrength_Symbols.ohf",
    "OpenPPL_Library_HoldEm_NutFullHouseOrFourOfAKind.ohf",
    "OpenPPL_Library_Omaha_Board_Symbols.ohf",
    "OpenPPL_Library_Omaha_HandStrength_Symbols.ohf",
    "OpenPPL_Library_Omaha_NutFullHouseOrFourOfAKind.ohf",
    "OpenPPL_Library_OutsCounter.ohf",
    "OpenPPL_Library_Players_and_Opponents.ohf",
    "OpenPPL_Library_Position_Symbols.ohf",
    "OpenPPL_Library_Technical_Betting_Action_Symbols.ohf",
    "OpenPPL_Library_Technical_Card_Symbols.ohf",
    "OpenPPL_Library_Technical_HandStrength_Symbols.ohf",
    "OpenPPL_Library_Technical_Position_Symbols.ohf",
    "OpenPPL_Library_Technical_Stacksizes.ohf",
    "OpenPPL_Library_Tournament_Symbols.ohf",
    "OpenPPL_Library_Update_History.ohf",
    "OpenPPL_Library_Verbosity.ohf",
]
DEFAULT_BOT_FILES = ["DefaultBot.ohf", "Gecko_NL_6Max_FR_BSS.ohf",
                     "Termita_SNG.ohf", "Winngy_PT_Limit.ohf"]

MULTIPLEX_POSTFIXES = {
    "userchair", "headsupchair", "smallblindchair", "bigblindchair", "dealerchair",
    "buttonchair", "cutoffchair", "mp3chair", "mp2chair", "mp1chair", "ep3chair",
    "ep2chair", "ep1chair", "utgchair", "firstcallerchair", "lastcallerchair",
    "firstraiserchair", "lastraiserchair", "raischair", "bigstackchair",
    "smallstackchair", "preflop", "flop", "turn", "river", "previousround",
    "currentround", "clubs", "diamonds", "hearts", "spades",
}
RANK_OF = {c: i for i, c in enumerate("23456789TJQKA", 2)}
SUIT_OF = {"c": 0, "d": 1, "h": 2, "s": 3}   # OpenHoldem/StdDeck: clubs, diamonds, hearts, spades
PRIMES = {2: 2, 3: 3, 4: 5, 5: 7, 6: 11, 7: 13, 8: 17, 9: 19, 10: 23, 11: 29, 12: 31, 13: 37, 14: 41}


class OpenPPLRuntimeError(Exception):
    pass


def _u32(x):
    try:
        return int(x) & 0xFFFFFFFF
    except (ValueError, OverflowError):
        return 0


def _round(d):
    return int(d + 0.5)


def _bitcount(x):
    return bin(_u32(x)).count("1")


class Runtime:
    def __init__(self, provider=None, log=None):
        self.provider = provider
        self.objects = {}            # name -> ScriptObject
        self.compiled = {}           # name -> closure
        self.cache = {}              # name -> value (per decision)
        self.memory = {}             # memory symbols (me_st_ ...), lower-case keys
        self.user_vars = set()       # user variables (user...)
        self.errors = []             # (file, line, message) load/parse errors
        self.runtime_errors = Counter()
        self.log = log
        self.depth = 0
        self.trace = None            # list to record (function, value) if set
        self._hk = None              # hand key (e.g. "AKs") cached per decision
        self.codegen = True          # compile functions to Python source (fast path)
        self.last_rule_idx = -1      # index of last WHEN clause that matched (-1 = none)

    # ------------------------------------------------------------------ load
    def load_file(self, path, library=False, optional=False):
        if not os.path.exists(path):
            if not optional:
                self.errors.append((path, 0, "file not found"))
            return []
        with open(path, encoding="latin-1") as f:
            text = f.read()
        objs = split_file(text, path, self.errors, library=library)
        for o in objs:
            self.objects[o.name] = o      # later definitions replace earlier ones (as in OH)
        return objs

    def load_standard(self, openppl_dir, default_bot_dir=None, custom_library=None):
        for name in OPENPPL_LIBRARIES:
            self.load_file(os.path.join(openppl_dir, name), library=True, optional=True)
        if default_bot_dir:
            for name in DEFAULT_BOT_FILES:
                self.load_file(os.path.join(default_bot_dir, name), library=True, optional=True)
        if custom_library:
            self.load_file(custom_library, library=True, optional=True)

    def finish_loading(self, validate=True):
        """Parse everything, build lists, compile, validate identifiers."""
        for name, o in self.objects.items():
            if o.is_list:
                o.hands = parse_list(o, self.errors)
            else:
                o.tree = FunctionParser(o, self.errors).parse_function()
        self.compiled = {}
        if validate:
            self.validate()

    def validate(self):
        seen = set()
        for name, o in self.objects.items():
            if o.is_list or o.tree is None:
                continue
            for ident in walk_identifiers(o.tree):
                if ident in seen:
                    continue
                seen.add(ident)
                if not self.is_known(ident):
                    self.errors.append((o.file, o.line, f"{name}: unknown identifier {ident}"))

    def is_known(self, name):
        if name in self.objects:
            return True
        if name.startswith(("me_st_", "me_re_", "me_inc_", "me_add_", "me_sub_")):
            return True
        if name[:4].lower() == "user" and name[4:9] != "chair":
            return True
        if name.startswith(("hand$", "board$", "msgbox$", "log$", "sitename$", "network$")):
            return True
        pure = self._multiplex_pure(name)
        if pure is not None:
            return self.is_known(pure) or any(self.is_known(f"{pure}{i}") for i in range(10))
        if self.provider is not None and self.provider.known(name):
            return True
        return False

    @staticmethod
    def _multiplex_pure(name):
        i = name.rfind("_")
        if i <= 0:
            return None
        if name[i + 1:] in MULTIPLEX_POSTFIXES:
            return name[:i]
        return None

    # ------------------------------------------------------------- evaluate
    def new_decision(self):
        """Clear the per-heartbeat function cache (OpenHoldem: ClearCache)."""
        self.cache.clear()
        self._hk = None
        self.last_rule_idx = -1

    def hand_reset(self):
        self.user_vars.clear()
        self.cache.clear()
        self._hk = None

    def evaluate(self, name):
        v = self.cache.get(name)
        if v is not None:
            return v
        fn = self.compiled.get(name)
        if fn is None:
            o = self.objects.get(name)
            if o is None:
                raise OpenPPLRuntimeError(f"function {name} does not exist")
            if o.is_list:
                return 1.0 if self._hand_key() in o.hands else 0.0
            fn = self._compile_function(o)
            self.compiled[name] = fn
        self.depth += 1
        if self.depth > MAX_RECURSION:
            self.depth -= 1
            self.runtime_errors[f"Recursion too deep in {name}"] += 1
            return 0.0
        try:
            v = float(fn())
        finally:
            self.depth -= 1
        self.cache[name] = v
        if self.trace is not None:
            self.trace.append((name, v))
        return v

    def exists(self, name):
        return name in self.objects

    def identifier(self, name):
        """Evaluate a single identifier (function, list, memory, symbol...)."""
        o = self.objects.get(name)
        if o is not None:
            if o.is_list:
                return 1.0 if self._hand_key() in o.hands else 0.0
            return self.evaluate(name)
        c = name[:3]
        if c == "me_":
            return self._memory(name)
        if name[:4].lower() == "user" and name[4:9] != "chair":
            return 1.0 if name in self.user_vars else 0.0
        if name.startswith("hand$"):
            return self._hand_board(name[5:], True)
        if name.startswith("board$"):
            return self._hand_board(name[6:], False)
        if name.startswith(("msgbox$", "log$")):
            return 1.0 if name.startswith("log$") else 0.0
        pure = self._multiplex_pure(name)
        if pure is not None:
            postfix = name[len(pure) + 1:]
            v = self.identifier(postfix)
            if v < 0:
                return 0.0
            return self.identifier(f"{pure}{int(v)}")
        try:
            return float(self.provider.symbol(name))
        except KeyError:
            self.runtime_errors[f"unknown symbol {name}"] += 1
            return 0.0

    # memory symbols ------------------------------------------------------
    def _memory(self, cmd):
        parts = cmd.split("_", 3)            # me, st, Name, value
        if len(parts) < 3 or not parts[2]:
            self.runtime_errors[f"invalid memory symbol {cmd}"] += 1
            return 0.0
        op, key = parts[1], parts[2].lower()
        if op == "re":
            return self.memory.get(key, 0.0)
        if op == "inc":
            self.memory[key] = self.memory.get(key, 0.0) + 1
            return 0.0
        if len(parts) < 4 or not parts[3]:
            self.runtime_errors[f"invalid memory symbol {cmd}"] += 1
            return 0.0
        rhs = parts[3]
        if rhs[0].isdigit():
            val = float(rhs.replace("_", ".")) if rhs.replace("_", "").isdigit() else 0.0
        else:
            val = self.identifier(rhs)
        if op == "st":
            self.memory[key] = val
        elif op == "add":
            self.memory[key] = self.memory.get(key, 0.0) + val
        elif op == "sub":
            self.memory[key] = self.memory.get(key, 0.0) - val
        return 0.0

    def _set(self, name):
        if name.startswith("me_"):
            self._memory(name)
        else:
            self.user_vars.add(name)

    # hand$ / board$ expressions (CSymbolEngineOpenPPLHandAndBoardExpression)
    def _hand_key(self):
        cards = self.provider.hole_cards()
        if len(cards) < 2:
            return ""
        (r1, s1), (r2, s2) = sorted(cards[:2], reverse=True)
        a, b = "23456789TJQKA"[r1 - 2], "23456789TJQKA"[r2 - 2]
        if r1 == r2:
            return a + b
        return a + b + ("s" if s1 == s2 else "o")

    def _hand_board(self, expr, is_hand):
        cards = self.provider.hole_cards() if is_hand else self.provider.board_cards()
        e = expr
        suited = False
        if e.lower().endswith("suited"):
            suited, e = True, e[:-6]
        # ranks (multiset) must be available
        avail = 1
        for r, _ in cards:
            avail *= PRIMES[r]
        search = 1
        ranks = []
        for ch in e.upper():
            if ch in RANK_OF:
                search *= PRIMES[RANK_OF[ch]]
                ranks.append(RANK_OF[ch])
        if avail % search != 0:
            return 0.0
        if suited:
            wanted = set(ranks)
            ok = False
            for _, suit in cards:
                have = {r for r, s in cards if s == suit}
                if wanted <= have:
                    ok = True
                    break
            if not ok:
                return 0.0
        # cards with specific suits, e.g. AsKs
        for i in range(len(e) - 1):
            if e[i].upper() in RANK_OF and e[i + 1] in SUIT_OF:
                card = (RANK_OF[e[i].upper()], SUIT_OF[e[i + 1]])
                if card not in cards:
                    return 0.0
        return 1.0

    # ------------------------------------------------------------- compile
    def _compile_function(self, o):
        if self.codegen:
            try:
                return _CodeGen(self).function(o.tree, o.name)
            except (SyntaxError, RecursionError, MemoryError):
                pass                       # very deep expressions: closure fallback
        return self._c(o.tree)

    def _c(self, n):
        k = n[0]
        if k == "num":
            v = n[1]
            return lambda: v
        if k == "id":
            return self._c_ident(n[1])
        if k == "un":
            return self._c_unary(n[1], self._c(n[2]))
        if k == "bin":
            return self._c_binary(n[1], self._c(n[2]), self._c(n[3]))
        if k == "tern":
            c, t, e = self._c(n[1]), self._c(n[2]), self._c(n[3])
            return lambda: t() if c() else e()
        if k == "when":
            return self._c_when(n[1], n[2])
        if k == "end":
            return lambda: 0.0
        raise OpenPPLRuntimeError(f"cannot compile node {k}")

    def _c_ident(self, name):
        """Resolve the kind of identifier once, at compile time (same order as identifier())."""
        ident = self.identifier
        o = self.objects.get(name)
        if o is not None:
            if o.is_list:
                hands = o.hands
                hand_key = self._hand_key_cached
                return lambda: 1.0 if hand_key() in hands else 0.0
            cache = self.cache
            evaluate = self.evaluate

            def fn():
                v = cache.get(name)
                return v if v is not None else evaluate(name)
            return fn
        if name[:3] == "me_":
            parts = name.split("_", 3)
            if len(parts) >= 3 and parts[1] == "re" and parts[2]:
                memory, key = self.memory, parts[2].lower()
                return lambda: memory.get(key, 0.0)
            return lambda: ident(name)
        if name[:4].lower() == "user" and name[4:9] != "chair":
            user_vars = self.user_vars
            return lambda: 1.0 if name in user_vars else 0.0
        if name.startswith(("hand$", "board$", "msgbox$", "log$")) \
                or self._multiplex_pure(name) is not None or self.provider is None:
            return lambda: ident(name)
        symbol = self.provider.symbol
        errors = self.runtime_errors

        def sym():
            try:
                return float(symbol(name))
            except KeyError:
                errors[f"unknown symbol {name}"] += 1
                return 0.0
        return sym

    def _hand_key_cached(self):
        k = self._hk
        if k is None:
            k = self._hk = self._hand_key()
        return k

    def _c_unary(self, op, a):
        if op == "neg":
            return lambda: -a()
        if op == "!":
            return lambda: 0.0 if a() else 1.0
        if op == "~":
            return lambda: float((~_u32(a())) & 0xFFFFFFFF)
        if op == "`":
            return lambda: float(_bitcount(a()))
        if op == "ln":
            def ln():
                x = a()
                return math.log(x) if x > 0 else float("nan")
            return ln
        raise OpenPPLRuntimeError(op)

    def _c_binary(self, op, a, b):
        if op == "&&":
            return lambda: (1.0 if b() else 0.0) if a() else 0.0
        if op == "||":
            return lambda: 1.0 if a() else (1.0 if b() else 0.0)
        if op == "+":
            return lambda: a() + b()
        if op == "-":
            return lambda: a() - b()
        if op == "*":
            return lambda: a() * b()
        if op == "/":
            def div():
                x, y = a(), b()
                if y == 0:
                    self.runtime_errors["Division by zero"] += 1
                    return K_UNDEFINED
                return x / y
            return div
        if op == "mod":
            def mod():
                x, y = _u32(a()), _u32(b())
                if y == 0:
                    self.runtime_errors["Division by zero (mod)"] += 1
                    return K_UNDEFINED
                return float(x % y)
            return mod
        if op == "pct":
            return lambda: a() * b() * 0.01
        if op == "**":
            def pw():
                try:
                    return float(a() ** b())
                except (OverflowError, ZeroDivisionError, TypeError):
                    return float("nan")
            return pw
        if op == "==":
            return lambda: 1.0 if abs(a() - b()) < EPS else 0.0
        if op == "!=":
            return lambda: 0.0 if abs(a() - b()) < EPS else 1.0
        if op == "~~":
            return lambda: 1.0 if _round(a()) == _round(b()) else 0.0
        if op == "<":
            return lambda: 1.0 if a() <= b() - EPS else 0.0
        if op == "<=":
            return lambda: 1.0 if a() < b() + EPS else 0.0
        if op == ">":
            return lambda: 1.0 if a() >= b() + EPS else 0.0
        if op == ">=":
            return lambda: 1.0 if a() > b() - EPS else 0.0
        if op == "^^":
            return lambda: 1.0 if bool(a()) != bool(b()) else 0.0
        if op == "&":
            return lambda: float(_u32(a()) & _u32(b()))
        if op == "|":
            return lambda: float(_u32(a()) | _u32(b()))
        if op == "^":
            return lambda: float(_u32(a()) ^ _u32(b()))
        if op == "<<":
            return lambda: float((_u32(a()) << (_u32(b()) & 31)) & 0xFFFFFFFF)
        if op == ">>":
            return lambda: float(_u32(a()) >> (_u32(b()) & 31))
        raise OpenPPLRuntimeError(op)

    def _c_action(self, a):
        k = a[0]
        if k == "ret":
            return self._c(a[1]), False
        if k == "fixed":
            name = a[1]
            ident = self.identifier
            return (lambda: ident(name)), False
        if k == "raiseto":
            return self._c(a[1]), False
        if k == "raiseby":
            e = self._c(a[1])
            ident = self.identifier
            return (lambda: ident("ncallbets") + e()), False
        if k == "raisepct":
            e = self._c(a[1])
            ident = self.identifier

            def pct():
                bet = ident("bet")
                pot_after_call = (ident("pot") / bet if bet > 0 else 0.0) + ident("nbetstocall")
                return ident("ncallbets") + 0.01 * e() * pot_after_call
            return pct, False
        if k == "set":
            name = a[1]
            setter = self._set
            return (lambda: setter(name)), True
        if k == "nop":
            return (lambda: 0.0), True
        raise OpenPPLRuntimeError(k)

    def _c_when(self, groups, default_name):
        compiled = []
        idx = 0
        for guard, clauses in groups:
            g = self._c(guard) if guard is not None else None
            cl = []
            for cond, action in clauses:
                fn, is_set = self._c_action(action)
                cl.append((self._c(cond), fn, is_set, idx))
                idx += 1
            compiled.append((g, cl))
        if default_name and default_name in self.objects:
            evaluate = self.evaluate
            end = lambda: evaluate(default_name)
        else:
            end = lambda: 0.0
        rt = self

        def run():
            for g, cl in compiled:
                if g is not None and not g():
                    continue
                for cond, fn, is_set, ridx in cl:
                    if cond():
                        if is_set:
                            fn()
                            continue
                        rt.last_rule_idx = ridx
                        return fn()
            rt.last_rule_idx = -1
            return end()
        return run


# ---------------------------------------------------------------------------
# Code generation: an OpenPPL function becomes one Python function, so that
# AND/OR chains, comparisons and arithmetic run as plain Python expressions
# instead of a tree of closures. Semantics are identical to Runtime._c
# (same evaluation order, short-circuit, epsilon comparisons, rounding).

def _div_err(rt):
    def div(x, y):
        if y == 0:
            rt.runtime_errors["Division by zero"] += 1
            return K_UNDEFINED
        return x / y
    return div


def _mod_err(rt):
    def mod(x, y):
        x, y = _u32(x), _u32(y)
        if y == 0:
            rt.runtime_errors["Division by zero (mod)"] += 1
            return K_UNDEFINED
        return float(x % y)
    return mod


def _pow(x, y):
    try:
        return float(x ** y)
    except (OverflowError, ZeroDivisionError, TypeError):
        return float("nan")


def _ln(x):
    return math.log(x) if x > 0 else float("nan")


_CMP = {
    "==": "(1.0 if abs({a} - {b}) < EPS else 0.0)",
    "!=": "(0.0 if abs({a} - {b}) < EPS else 1.0)",
    "~~": "(1.0 if int({a} + 0.5) == int({b} + 0.5) else 0.0)",
    "<": "(1.0 if {a} <= {b} - EPS else 0.0)",
    "<=": "(1.0 if {a} < {b} + EPS else 0.0)",
    ">": "(1.0 if {a} >= {b} + EPS else 0.0)",
    ">=": "(1.0 if {a} > {b} - EPS else 0.0)",
    "+": "({a} + {b})",
    "-": "({a} - {b})",
    "*": "({a} * {b})",
    "/": "_div({a}, {b})",
    "mod": "_mod({a}, {b})",
    "pct": "({a} * {b} * 0.01)",
    "**": "_pow({a}, {b})",
    "^^": "(1.0 if bool({a}) != bool({b}) else 0.0)",
    "&": "float(_u32({a}) & _u32({b}))",
    "|": "float(_u32({a}) | _u32({b}))",
    "^": "float(_u32({a}) ^ _u32({b}))",
    "<<": "float((_u32({a}) << (_u32({b}) & 31)) & 0xFFFFFFFF)",
    ">>": "float(_u32({a}) >> (_u32({b}) & 31))",
}
_UN = {
    "neg": "(-{a})",
    "!": "(0.0 if {a} else 1.0)",
    "~": "float((~_u32({a})) & 0xFFFFFFFF)",
    "`": "float(_bitcount({a}))",
    "ln": "_ln({a})",
}


class _CodeGen:
    def __init__(self, rt):
        self.rt = rt
        self.ns = {"EPS": EPS, "_u32": _u32, "_bitcount": _bitcount, "_pow": _pow,
                   "_ln": _ln, "_div": _div_err(rt), "_mod": _mod_err(rt),
                   "abs": abs, "int": int, "float": float, "bool": bool}
        self.names = {}

    def bind(self, obj, prefix="o"):
        n = f"{prefix}{len(self.ns)}"
        self.ns[n] = obj
        return n

    def ident(self, name):
        v = self.names.get(name)
        if v is None:
            v = self.names[name] = self.bind(self.rt._c_ident(name), "i")
        return v + "()"

    def expr(self, n):
        k = n[0]
        if k == "num":
            v = float(n[1])
            return repr(v) if math.isfinite(v) else self.bind(v, "c")
        if k == "id":
            return self.ident(n[1])
        if k == "un":
            return _UN[n[1]].format(a=self.expr(n[2]))
        if k == "bin":
            op = n[1]
            if op in ("&&", "||"):
                parts = []
                self._flatten(n, op, parts)
                joined = (" and " if op == "&&" else " or ").join(f"({p})" for p in parts)
                return f"(1.0 if {joined} else 0.0)"
            return _CMP[op].format(a=self.expr(n[2]), b=self.expr(n[3]))
        if k == "tern":
            return f"({self.expr(n[2])} if {self.expr(n[1])} else {self.expr(n[3])})"
        if k == "end":
            return "0.0"
        return self.bind(self.rt._c(n), "x") + "()"   # nested when etc.

    def _flatten(self, n, op, out):
        if n[0] == "bin" and n[1] == op:
            self._flatten(n[2], op, out)
            self._flatten(n[3], op, out)
        else:
            out.append(self.expr(n))

    def function(self, tree, name):
        rt_name = self.bind(self.rt, "rt")
        lines = ["def _f():"]
        if tree[0] == "when":
            groups, default_name = tree[1], tree[2]
            idx = 0
            for guard, clauses in groups:
                ind = "    "
                if guard is not None:
                    lines.append(f"    if {self.expr(guard)}:")
                    ind = "        "
                if not clauses:
                    lines.append(ind + "pass")
                for cond, action in clauses:
                    fn, is_set = self.rt._c_action(action)
                    a = self.bind(fn, "a")
                    lines.append(f"{ind}if {self.expr(cond)}:")
                    if is_set:
                        lines.append(f"{ind}    {a}()")
                    else:
                        lines.append(f"{ind}    {rt_name}.last_rule_idx={idx}")
                        lines.append(f"{ind}    return {a}()")
                    idx += 1
            if default_name and default_name in self.rt.objects:
                ev = self.bind(self.rt.evaluate, "e")
                lines.append(f"    {rt_name}.last_rule_idx=-1")
                lines.append(f"    return {ev}({default_name!r})")
            else:
                lines.append(f"    {rt_name}.last_rule_idx=-1")
                lines.append("    return 0.0")
        else:
            lines.append(f"    return {self.expr(tree)}")
        code = compile("\n".join(lines), f"<openppl {name}>", "exec")
        exec(code, self.ns)
        return self.ns["_f"]
