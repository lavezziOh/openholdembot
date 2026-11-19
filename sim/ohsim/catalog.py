"""Catalogue of OpenHoldem built-in symbol names (for load-time validation)."""
import os
import re

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "oh_symbols.txt")) as _f:
    OH_SYMBOLS = frozenset(l.strip() for l in _f if l.strip())

# PokerTracker symbols: pt_<stat>_<postfix> or pt_<stat><chair>
_PT = re.compile(r"pt_[a-z0-9_]+?(_(raischair|headsupchair|smallblindchair|bigblindchair|"
                 r"dealerchair|cutoffchair|userchair|firstraiserchair|firstcallerchair|"
                 r"lastcallerchair)|[0-9])$")
# Constants and misc symbols always available in OpenHoldem
_EXTRA = {"true", "false", "random", "randomheartbeat", "randomhand", "utgchair",
          "empty_expression__false__zero__when_others_fold_force"}
# $$cr0 (common card rank), $$cs0 (suit), $$pr0 / $$ps0 (player cards),
# $AA / $AKs (hand symbols), balance_rank0..9
_SPECIAL = re.compile(r"\$\$[cp][rsc]\d|\$[2-9TJQKAXx]{1,2}[so]?|balance_rank\d|didalliround\d|dll\$\w+")


def is_oh_symbol(name: str) -> bool:
    return (name in OH_SYMBOLS or name in _EXTRA or bool(_PT.fullmatch(name))
            or bool(_SPECIAL.fullmatch(name)))


class CatalogProvider:
    """Provider that only knows names (used to validate profiles without a game)."""
    def known(self, name):
        return is_oh_symbol(name)

    def symbol(self, name):
        raise KeyError(name)

    def hole_cards(self):
        return []

    def board_cards(self):
        return []
