"""OpenPPL / OH-script tokenizer, modelled on OpenHoldem's CTokenizer.

Differences to OpenHoldem are bug-fixes only (see the fix-branch):
comments may contain any character and never run past the end of the text.
"""
from dataclasses import dataclass

# Case-insensitive keywords (OpenHoldem: CTokenizer::IsTokenOpenPPLKeyword)
KEYWORDS = {
    "or": "or", "in": "in", "ln": "ln",
    "not": "not", "mod": "mod", "and": "and", "xor": "xor", "set": "set",
    "when": "when", "hand": "hand",
    "bitor": "bitor", "delay": "delay", "force": "force", "board": "board",
    "return": "return", "bitand": "bitand", "bitxor": "bitxor", "bitnot": "bitnot",
    "bitcount": "bitcount",
}

# Action words, only recognised where an action is expected
# (OpenHoldem: kOpenPPLActionStrings -> TokenString of the action constant)
ACTIONS = {
    "bet": "Raise", "call": "Call", "fold": "Fold", "play": "Call", "beep": "Beep",
    "raise": "Raise", "raiseto": "RaiseTo", "raiseby": "RaiseBy", "check": "Check",
    "allin": "RaiseMax", "betmin": "Raise", "betfourthpot": "RaiseFourthPot",
    "betthirdpot": "RaiseThirdPot", "bethalfpot": "RaiseHalfPot",
    "bettwothirdpot": "RaiseTwoThirdPot", "betthreefourthpot": "RaiseThreeFourthPot",
    "betpot": "RaisePot", "betmax": "RaiseMax", "raisemin": "Raise",
    "raisefourthpot": "RaiseFourthPot", "raisethirdpot": "RaiseThirdPot",
    "raisehalfpot": "RaiseHalfPot", "raisetwothirdpot": "RaiseTwoThirdPot",
    "raisethreefourthpot": "RaiseThreeFourthPot", "raisepot": "RaisePot",
    "raisemax": "RaiseMax", "sitout": "SitOut",
}

RANK_CHARS = set("23456789TtJjQqKkAa")


@dataclass
class Token:
    type: str        # 'num', 'id', 'cards', 'eof', keyword names, operator symbols
    text: str
    line: int        # relative line inside the function (1-based)


class TokenizeError(Exception):
    pass


class Tokenizer:
    def __init__(self, text: str, shanky: bool = False, errors=None):
        self.s = text
        self.n = len(text)
        self.pos = 0
        self.line = 1
        self.shanky = shanky
        self.inside_openppl = False   # becomes True after the first WHEN
        self.last_type = None
        self.errors = errors if errors is not None else []
        self._peeked = []

    # -- public API -------------------------------------------------------
    def peek(self, k: int = 0) -> Token:
        while len(self._peeked) <= k:
            self._peeked.append(self._scan())
        return self._peeked[k]

    def next(self) -> Token:
        tok = self.peek()
        self._peeked.pop(0)
        return tok

    def push_front(self, tok: Token):
        self._peeked.insert(0, tok)

    # -- scanner ------------------------------------------------------------
    def _err(self, msg):
        self.errors.append((self.line, msg))

    def _skip_space_and_comments(self):
        s, n = self.s, self.n
        while self.pos < n:
            c = s[self.pos]
            if c == "\n":
                self.line += 1
                self.pos += 1
            elif c.isspace():
                self.pos += 1
            elif c == "/" and self.pos + 1 < n and s[self.pos + 1] == "/":
                end = s.find("\n", self.pos)
                self.pos = n if end < 0 else end
            elif c == "/" and self.pos + 1 < n and s[self.pos + 1] == "*":
                end = s.find("*/", self.pos + 2)
                if end < 0:
                    self._err("End of function reached while looking for end of comment")
                    self.line += s.count("\n", self.pos)
                    self.pos = n
                else:
                    self.line += s.count("\n", self.pos, end)
                    self.pos = end + 2
            else:
                return

    def _tok(self, typ, text):
        self.last_type = typ
        return Token(typ, text, self.line)

    def _scan(self) -> Token:
        self._skip_space_and_comments()
        s, n = self.s, self.n
        if self.pos >= n:
            return Token("eof", "", self.line)
        start = self.pos
        c = s[start]
        if not c.isascii():
            self._err(f"Illegal character: {c!r}")
            self.pos += 1
            return self._scan()
        # identifiers / keywords
        if c.isalpha() or c == "$":
            p = start + 1
            while p < n and (s[p].isalnum() or s[p] in "$_") and s[p].isascii():
                p += 1
            self.pos = p
            word = s[start:p]
            kw = KEYWORDS.get(word.lower())
            if kw:
                if kw == "when":
                    self.inside_openppl = True
                return self._tok(kw, word)
            return self._tok("id", word)
        # numbers and card tokens like 72o / 98s / 98suited
        if c.isdigit() or c == ".":
            if (start + 2 < n and s[start + 1] in RANK_CHARS
                    and s[start + 2].lower() in "so" and c.isdigit()):
                p = start
                while p < n and s[p].isalnum():
                    p += 1
                self.pos = p
                return self._tok("cards", s[start:p])
            return self._number(start)
        # operators
        two = s[start:start + 2]
        if c == "-":
            if self.last_type in ("id", "num", ")", "]", "}"):
                self.pos += 1
                return self._tok("-", "-")
            self.pos += 1
            if self.pos < n and (s[self.pos].isdigit() or s[self.pos] == "."):
                tok = self._number(self.pos)
                tok.text = "-" + tok.text
                return tok
            return self._tok("neg", "-")
        if two in ("**", "<=", "<<", ">=", ">>", "==", "!=", "&&", "||", "~~", "^^"):
            self.pos += 2
            if two == "==":
                return self._tok("~~" if self.shanky else "==", two)
            return self._tok(two, two)
        if c in "+*/<>`=!?:&|~^":
            self.pos += 1
            if c == "=":
                return self._tok("~~" if self.shanky else "==", c)
            if c == "!":
                return self._tok("!", c)
            return self._tok(c, c)
        if c == "%":
            self.pos += 1
            return self._tok("pct" if self.inside_openppl else "mod", c)
        if c in "([{)]}":
            self.pos += 1
            return self._tok(c, c)
        if two == "##":
            self.pos += 2
            return self._tok("##", two)
        self._err(f"Unexpected character {c!r}")
        self.pos = n
        return Token("eof", "", self.line)

    def _number(self, start) -> Token:
        s, n = self.s, self.n
        p = start
        while p < n and (s[p] in "0123456789abcdefABCDEF.x"):
            p += 1
        self.pos = p
        return self._tok("num", s[start:p])


def string_to_number(text: str) -> float:
    neg = text.startswith("-")
    t = text[1:] if neg else text
    if t[:2].lower() == "0x":
        v = float(int(t[2:] or "0", 16))
    elif t[:2].lower() == "0b":
        v = float(int(t[2:] or "0", 2))
    else:
        # atof semantics: parse the longest valid prefix
        import re
        m = re.match(r"\d*\.?\d*(?:[eE][+-]?\d+)?", t)
        num = m.group(0) if m else ""
        try:
            v = float(num) if num not in ("", ".") else 0.0
        except ValueError:
            v = 0.0
    return -v if neg else v
