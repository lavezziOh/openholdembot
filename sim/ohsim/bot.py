"""An OpenPPL profile playing at the simulated table, OpenHoldem-style.

Life-cycle (OpenHoldem CSymbolEngineOpenPPL / CAutoplayerFunctions):
  hand reset   -> user variables cleared, UpdateMemorySymbolsOnHandReset
  new round    -> UpdateMemorySymbolsOnNewRound
  my turn      -> f$preflop / f$flop / f$turn / f$river, decision translated
  after action -> UpdateMemorySymbolsAfterAutoplayerAction
"""
import hashlib
import os
from collections import Counter

from .engine import Action, FOLD, CHECK, CALL, RAISE
from .oppl.runtime import Runtime
from .symbols import SymbolProvider

MAIN = {1: "f$preflop", 2: "f$flop", 3: "f$turn", 4: "f$river"}
FALLBACK = {1: "f$preflop", 2: "f$FHandStrenght", 3: "f$THandStrenght", 4: "f$RHandStrenght"}
DEFAULT = {1: "DefaultPreflop", 2: "DefaultFlop", 3: "DefaultTurn", 4: "DefaultRiver"}
POT_ACTIONS = {"RaiseTwoPot": 2.0, "RaisePot": 1.0, "RaiseThreeFourthPot": 0.75,
               "RaiseTwoThirdPot": 2 / 3, "RaiseHalfPot": 0.5, "RaiseThirdPot": 1 / 3,
               "RaiseFourthPot": 0.25}
_BASE_CACHE = {}   # base_hash -> (objects, errors)
_FULL_CACHE = {}   # (base_hash, profile_hash) -> (objects, errors, compiled)


def _path_hash(*paths):
    """Fast hash based on mtime+size, not file content."""
    h = hashlib.md5()
    for path in paths:
        if not path:
            continue
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    h.update(fp.encode())
                    h.update(str(os.path.getmtime(fp)).encode())
                    h.update(str(os.path.getsize(fp)).encode())
        elif os.path.isfile(path):
            h.update(path.encode())
            h.update(str(os.path.getmtime(path)).encode())
            h.update(str(os.path.getsize(path)).encode())
    return h.hexdigest()


class OpenPPLBot:
    def __init__(self, name, profile, openppl_dir, default_bot_dir=None,
                 custom_library=None, prwin_iterations=400, seed=None, stats=None):
        self.name = name
        self.provider = SymbolProvider(prwin_iterations=prwin_iterations, seed=seed, stats=stats)
        self.rt = Runtime(self.provider)

        base_hash = _path_hash(openppl_dir, default_bot_dir, custom_library)
        prof_hash = _path_hash(profile) if profile else "none"
        full_key = (base_hash, prof_hash)

        cached = _FULL_CACHE.get(full_key)
        if cached is not None:
            self.rt.objects = dict(cached[0])
            self.rt.errors = list(cached[1])
            self.rt.compiled = dict(cached[2])
        else:
            base = _BASE_CACHE.get(base_hash)
            if base is not None:
                self.rt.objects = dict(base[0])
                self.rt.errors = list(base[1])
            else:
                self.rt.load_standard(openppl_dir, default_bot_dir, custom_library)
                _BASE_CACHE.clear()
                _BASE_CACHE[base_hash] = (dict(self.rt.objects), list(self.rt.errors))
            if profile:
                self.rt.load_file(profile)
            self.rt.finish_loading(validate=True)
            for fn_name in list(MAIN.values()) + list(FALLBACK.values()) + list(DEFAULT.values()):
                o = self.rt.objects.get(fn_name)
                if o is not None and not o.is_list and fn_name not in self.rt.compiled:
                    self.rt.compiled[fn_name] = self.rt._compile_function(o)
            _FULL_CACHE.clear()
            _FULL_CACHE[full_key] = (dict(self.rt.objects), list(self.rt.errors), dict(self.rt.compiled))
        self.load_errors = list(self.rt.errors)
        self.provider.prwin_opponents_fn = (
            (lambda: self.rt.evaluate("f$prwin_number_of_opponents"))
            if self.rt.exists("f$prwin_number_of_opponents") else None)
        # action constants of the library (Fold = -1000001, ...)
        self.constants = {}
        for nm in ["Beep", "Fold", "Call", "Check", "Raise", "RaiseMax", "None"] + list(POT_ACTIONS):
            if self.rt.exists(nm):
                self.constants[nm] = self.rt.evaluate(nm)
        self.decisions = Counter()
        self.timings = []

    # -- helpers ---------------------------------------------------------------
    def _run(self, view, function):
        self.provider.bind(view)
        self.rt.new_decision()
        if self.rt.exists(function):
            return self.rt.evaluate(function)
        return 0.0

    # -- engine callbacks ------------------------------------------------------
    def start_hand(self, view):
        self.rt.hand_reset()
        self.provider.on_hand_start()
        self._run(view, "UpdateMemorySymbolsOnHandReset")

    def new_round(self, view):
        self.provider.on_new_round()
        self._run(view, "UpdateMemorySymbolsOnNewRound")

    def after_action(self, view, action):
        self.provider.on_own_action(self._action_code(action), view.t.street)
        self._run(view, "UpdateMemorySymbolsAfterAutoplayerAction")

    @staticmethod
    def _action_code(action):
        """Autoplayer function that OpenHoldem would have used for the executed action."""
        if action.kind != RAISE:
            return action.kind                       # fold / check / call (engine-normalized)
        note = action.note.split(" ")[0]
        if note == "allin":
            return "allin"
        if note == "minraise":
            return "raise"
        if note in POT_ACTIONS:
            return "betpot"
        return "betsize"                             # betsize / betsize_pot

    def end_hand(self, view, result):
        pass

    def decide(self, view):
        street = view.t.street
        if self.rt.exists(MAIN[street]):
            fn = MAIN[street]
        elif self.rt.exists(FALLBACK[street]):
            fn = FALLBACK[street]
        else:
            fn = DEFAULT[street]
        d = self._run(view, fn)
        action = self.translate(view, d)
        self.decisions[action.note.split(" ")[0]] += 1
        return action

    # -- OpenHoldem CAutoplayerFunctions::TranslateOpenPPLDecisionToAutoplayerFunctions
    def translate(self, view, d):
        t, me = view.t, view.me
        bb = t.bb
        cb = t.current_bet()
        call = min(cb - me.bet, me.stack)
        if d > 0:
            return Action(RAISE, d * bb, f"betsize {d:g}bb")
        if -1000 <= d < 0:
            pot = t.pot
            size = me.bet + call + (-d) * (pot + call)
            return Action(RAISE, size, f"betsize_pot {-100 * d:g}%")
        if d < -1000:
            for nm, val in self.constants.items():
                if val == d:
                    return self._fixed(view, nm, call)
            return Action(CHECK, 0, f"check/fold(unknown {d:g})")
        return Action(CHECK, 0, "check/fold")

    def _fixed(self, view, nm, call):
        t, me = view.t, view.me
        if nm == "RaiseMax":
            return Action(RAISE, me.bet + me.stack, "allin")
        if nm in POT_ACTIONS:
            size = me.bet + call + POT_ACTIONS[nm] * (t.pot + call)
            return Action(RAISE, size, nm)
        if nm == "Raise":
            return Action(RAISE, 0, "minraise")      # engine raises to the minimum
        if nm == "Call":
            return Action(CALL, 0, "call")
        if nm == "Fold":
            return Action(FOLD, 0, "fold")
        if nm == "Check":
            return Action(CHECK, 0, "check")
        return Action(CHECK, 0, f"check/fold({nm})")
