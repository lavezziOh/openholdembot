"""Formula-file splitter and recursive-descent parser for OpenPPL / OH-script.

Semantics follow OpenHoldem (CFormulaFileSplitter, CFormulaParser, CParseTreeRotator):
the tree OpenHoldem builds naively and then rotates is equivalent to the
precedence climbing done here with the same priority table.
"""
import re
from dataclasses import dataclass, field

from .tokenizer import Tokenizer, Token, ACTIONS, string_to_number
from .shanky import translate as shanky_translate

# Binary operators: token type -> (priority, right_associative)
BINARY = {
    "+": (11, False), "-": (11, False),
    "*": (12, False), "/": (12, False), "mod": (12, False), "pct": (12, False),
    "**": (14, True),
    "==": (8, False), "~~": (8, False), "!=": (8, False),
    "<": (9, False), "<=": (9, False), ">": (9, False), ">=": (9, False),
    "&&": (4, False), "and": (4, False),
    "||": (2, False), "or": (2, False),
    "^^": (3, False), "xor": (3, False),
    "&": (7, False), "bitand": (7, False),
    "|": (5, False), "bitor": (5, False),
    "^": (6, False), "bitxor": (6, False),
    "<<": (10, False), ">>": (10, False),
}
# normalise synonyms
CANON = {"and": "&&", "or": "||", "xor": "^^", "bitand": "&", "bitor": "|", "bitxor": "^"}
UNARY = {"neg": "neg", "!": "!", "not": "!", "~": "~", "bitnot": "~", "`": "`",
         "bitcount": "`", "ln": "ln"}
OPEN = {"(": ")", "[": "]", "{": "}"}

SHANKY_BETROUNDS = ["preflop", "flop", "turn", "river"]
MAIN_FUNCTIONS = {"f$preflop": "DefaultPreflop", "f$flop": "DefaultFlop",
                  "f$turn": "DefaultTurn", "f$river": "DefaultRiver"}

HAND_EXPR_CHARS = set("23456789TJQKASUIEDCH")


class ParseError(Exception):
    pass


@dataclass
class ScriptObject:
    name: str
    text: str
    file: str
    line: int                 # absolute line of the header
    shanky: bool = False
    is_list: bool = False
    library: bool = False
    tree: object = None       # parsed tree (functions)
    hands: set = field(default_factory=set)   # lists


# --------------------------------------------------------------------------
# File splitter
# --------------------------------------------------------------------------
def _is_shanky_betround(line: str) -> int:
    for i, br in enumerate(SHANKY_BETROUNDS, 1):
        if line.startswith(br) and not line[len(br):len(br) + 1].isalnum():
            return i
    return 0


def split_file(text: str, path: str, errors: list, library=False):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    shanky = False
    i = 0
    # Skip Shanky option settings / everything before the first header
    while i < len(lines):
        ln = lines[i]
        if ln[:2] == "##" or (shanky and _is_shanky_betround(ln)):
            break
        if "=" in ln[1:]:
            shanky = True
        elif ln.strip().lower() == "custom":
            shanky = True
        i += 1
    objects = []
    while i < len(lines):
        header, header_line = lines[i], i + 1
        i += 1
        body = []
        while i < len(lines):
            ln = lines[i].rstrip()
            if ln[:2] == "##" or (shanky and _is_shanky_betround(ln)):
                break
            body.append(ln)
            i += 1
        name = _extract_name(header, shanky, path, header_line, errors)
        if name is None:
            continue
        text_ = "\n".join(body).rstrip() + "\n"
        obj = ScriptObject(name, text_, path, header_line, shanky=shanky and not name.startswith("list"),
                           is_list=name.startswith("list"), library=library)
        objects.append(obj)
    return objects


def _extract_name(header: str, shanky: bool, path, line, errors):
    br = _is_shanky_betround(header.lower()) if shanky else 0
    if br:
        return ["", "f$preflop", "f$flop", "f$turn", "f$river"][br]
    h = header.strip()
    if not (h.startswith("##") and h.endswith("##") and len(h) > 4):
        errors.append((path, line, f"Malformed function header: {header!r}"))
        return None
    name = h[2:-2]
    if name[:1].isdigit():          # ##2014-02-09 23:16:55##
        return None
    if name.lower() in ("dll", "notes"):     # OpenHoldem ignores these sections
        return None
    return name


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
class FunctionParser:
    def __init__(self, obj: ScriptObject, errors: list):
        self.obj = obj
        self.errors = errors
        self.tok_errors = []
        self.t = Tokenizer(obj.text, shanky=obj.shanky, errors=self.tok_errors)

    def error(self, msg, tok=None):
        line = tok.line if tok else self.t.peek().line
        self.errors.append((self.obj.file, self.obj.line + line, f"{self.obj.name}: {msg}"))

    def ident(self, name):
        if self.obj.shanky:
            name = shanky_translate(name)
        return ("id", name)

    # -- expressions ------------------------------------------------------
    def parse_expression(self, min_prec=0):
        left = self.parse_unary()
        while True:
            tok = self.t.peek()
            typ = tok.type
            if typ in BINARY:
                prec, right = BINARY[typ]
                if prec < min_prec:
                    break
                if typ == "pct":
                    nxt = self.t.peek(1).type
                    if nxt in ("force", "when", "eof"):
                        break          # "RaiseBy 60% Force": percentage suffix
                self.t.next()
                rhs = self.parse_expression(prec if right else prec + 1)
                left = ("bin", CANON.get(typ, typ), left, rhs)
                continue
            if typ == "?" and min_prec <= 1:
                self.t.next()
                then = self.parse_expression(0)
                if self.t.peek().type != ":":
                    self.error("Malformed conditional expression, expecting ':'")
                    return left
                self.t.next()
                other = self.parse_expression(1)
                left = ("tern", left, then, other)
                continue
            break
        return left

    def parse_unary(self):
        tok = self.t.peek()
        typ = tok.type
        if typ in OPEN:
            self.t.next()
            e = self.parse_expression(0)
            close = self.t.next()
            if close.type != OPEN[typ]:
                self.error(f"Expecting closing bracket {OPEN[typ]!r}, found {close.text!r}", close)
            return e
        if typ in UNARY:
            self.t.next()
            return ("un", UNARY[typ], self.parse_expression(14))
        if typ == "num":
            self.t.next()
            return ("num", string_to_number(tok.text))
        if typ == "id":
            self.t.next()
            return self.ident(tok.text)
        if typ in ("hand", "board"):
            return self.parse_shanky_hand_board()
        if typ == "in":
            self.t.next()
            nxt = self.t.next()
            if nxt.type != "id":
                self.error("Expecting position after 'In'", nxt)
                return ("num", 0.0)
            return ("id", shanky_translate("In" + nxt.text))
        self.error(f"Unexpected token {tok.text or tok.type!r} inside expression", tok)
        self.t.next()
        return ("num", 0.0)

    def parse_shanky_hand_board(self):
        kw = self.t.next()
        prefix = "hand$" if kw.type == "hand" else "board$"
        eq = self.t.next()
        if eq.type not in ("==", "~~"):
            self.error("Expecting '=' in hand/board expression", eq)
            return ("num", 0.0)
        ident = prefix
        while True:
            tok = self.t.peek()
            if tok.type not in ("id", "num", "cards"):
                break
            if not tok.text or not set(tok.text.upper()) <= HAND_EXPR_CHARS:
                break
            ident += tok.text
            self.t.next()
        return ("id", ident)

    # -- function bodies -------------------------------------------------
    def parse_function(self):
        tok = self.t.peek()
        if tok.type == "eof":
            tree = ("end",)
        elif tok.type == "when":
            tree = self.parse_when_sequence()
        else:
            tree = self.parse_expression(0)
            nxt = self.t.peek()
            if nxt.type != "eof":
                self.error(f"Unexpected token(s) after end of function: {nxt.text!r}", nxt)
        for line, msg in self.tok_errors:
            self.errors.append((self.obj.file, self.obj.line + line, f"{self.obj.name}: {msg}"))
        return tree

    def parse_when_sequence(self):
        # groups: list of (guard or None, [(cond, action), ...])
        groups = [(None, [])]
        while self.t.peek().type == "when":
            self.t.next()
            nxt = self.t.peek()
            if nxt.type in ("eof", "when"):
                self.error("Empty when-condition", nxt)
                if nxt.type == "eof":
                    break
                continue
            cond = self.parse_expression(0)
            tok = self.t.peek()
            action = self.try_parse_action()
            if action is not None:
                groups[-1][1].append((cond, action))
            elif tok.type == "when" or tok.type == "eof":
                groups.append((cond, []))          # open-ended when-condition
            else:
                self.error(f"Missing action after when-condition, found {tok.text!r}", tok)
                # skip to the next WHEN to continue gracefully
                while self.t.peek().type not in ("when", "eof"):
                    self.t.next()
        if self.t.peek().type != "eof":
            tok = self.t.peek()
            self.error(f"Unexpected token(s) after end of function: {tok.text!r}", tok)
        return ("when", groups, MAIN_FUNCTIONS.get(self.obj.name))

    def try_parse_action(self):
        tok = self.t.peek()
        if tok.type == "return":
            self.t.next()
            e = self.parse_expression(0)
            self.expect_force()
            return ("ret", e)
        if tok.type == "set":
            self.t.next()
            v = self.t.next()
            if v.type != "id" or not (v.text.lower().startswith("user") or v.text.startswith("me_")):
                self.error(f"User-variable or memory-command expected after SET, found {v.text!r}", v)
                return ("nop",)
            return ("set", v.text)
        if tok.type != "id":
            return None
        low = tok.text.lower()
        if self.obj.shanky and low.startswith("user"):
            self.t.next()
            return ("set", tok.text)
        if low not in ACTIONS:
            return None
        self.t.next()
        name = ACTIONS[low]
        if name == "RaiseTo":
            e = self.parse_expression(0)
            self.expect_force()
            return ("raiseto", e)
        if name == "RaiseBy" or (name == "Raise" and self.t.peek().type != "force"):
            if name == "Raise" and not self.obj.shanky:
                self.error("Missing keyword FORCE after Raise (use RaiseTo or RaiseBy)")
            e = self.parse_expression(0)
            if self.t.peek().type == "pct":
                self.t.next()
                self.expect_force()
                return ("raisepct", e)
            self.expect_force()
            return ("raiseby", e)
        self.expect_force()
        return ("fixed", name)

    def expect_force(self):
        tok = self.t.peek()
        if tok.type != "force":
            self.error(f"Missing keyword FORCE after action, found {tok.text!r}", tok)
            return
        self.t.next()
        if self.t.peek().type == "force":
            self.error("Duplicate keyword force")
            self.t.next()
        if self.t.peek().type == "delay":
            self.t.next()
            if not self.obj.shanky:
                self.error("Unsupported Shanky-style delay")
            if self.t.peek().type == "num":
                self.t.next()


# --------------------------------------------------------------------------
# Hand lists
# --------------------------------------------------------------------------
RANKS = "23456789TJQKA"


def parse_list(obj: ScriptObject, errors: list):
    hands = set()
    text = re.sub(r"/\*.*?\*/", " ", obj.text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    for tok in text.split():
        t = tok.upper()
        m = re.fullmatch(r"([2-9TJQKA])([2-9TJQKA])([SO]?)", t)
        if not m:
            errors.append((obj.file, obj.line, f"{obj.name}: invalid list member {tok!r}"))
            continue
        a, b, suf = m.groups()
        if RANKS.index(a) < RANKS.index(b):
            errors.append((obj.file, obj.line,
                           f"{obj.name}: old style list format {tok!r} (use {b}{a}s / {b}{a}o)"))
            a, b = b, a
        if a == b:
            hands.add(a + b)
        elif suf == "S":
            hands.add(a + b + "s")
        elif suf == "O":
            hands.add(a + b + "o")
        else:
            hands.update((a + b + "s", a + b + "o"))
    return hands


def walk_identifiers(tree):
    """Yield all identifier names in a parsed tree."""
    stack = [tree]
    while stack:
        n = stack.pop()
        if not isinstance(n, tuple) or not n:
            continue
        kind = n[0]
        if kind == "id":
            yield n[1]
        elif kind == "when":
            for guard, clauses in n[1]:
                if guard is not None:
                    stack.append(guard)
                for c, a in clauses:
                    stack.append(c)
                    stack.append(a)
        elif kind == "fixed":
            yield n[1]
        else:
            for x in n[1:]:
                if isinstance(x, tuple):
                    stack.append(x)
