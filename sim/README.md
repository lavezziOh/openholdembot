# OHSim - No-Limit Hold'em cash simulator for OpenHoldem / OpenPPL profiles

OHSim plays OpenHoldem profiles (`.ohf`, OpenPPL and Shanky-import mode) against each other
on a No-Limit Hold'em cash table with 2 to 10 seats (Pot-Limit Omaha is supported too).
It evaluates the profiles with the same rules as OpenHoldem (operator precedence, `WHEN`,
memory symbols, `hand$`/`board$`, lists) and the OpenPPL library shipped in this repository,
so you can test a bot for hundreds of thousands of hands in minutes instead of playing it.

## Download

Windows program (no Python needed): see the
[latest release](https://github.com/lavezziOh/openholdembot/releases/latest),
file `OHSim-windows.zip`. Unzip it and start `OHSim.exe`.
The zip contains the fixed OpenPPL library and OpenHoldem's default bot.

## Run from source

Python 3.11+ with `numpy` (`pip install numpy`).

    cd sim
    python ohsim_gui.py                                              # window
    python -m ohsim.run examples/example_nlhe_6max.toml              # hands from the file
    python -m ohsim.run examples/example_nlhe_6max.toml 50000 -j 8   # 50000 hands, 8 processes

The hands are split into blocks played in parallel, each on its own table with its own seed.
Speed: about 100,000 hands per minute with 6 OpenPPL bots on 16 cores.

The `.toml` file defines the table (blinds, ante, buy-in, rake, hands, seed) and the players.
`profile = ""` = OpenHoldem's default bot. At the end OHSim prints the results (bb/100),
a preflop report per position, the result per preflop line, postflop statistics
(c-bet, fold to c-bet, WTSD, W$SD, AF) and the symbols a profile used that are not
implemented (they count as 0).

Hand histories in PokerStars format (importable in PokerTracker 4):

    python -m ohsim.run examples/example_nlhe_6max.toml 20000 --hh hh/test
    python tools/check_hh.py hh/test

## What is simulated

| File | Role |
|---|---|
| `ohsim/oppl/tokenizer.py`, `parser.py`, `shanky.py` | reading profiles with OpenHoldem's rules |
| `ohsim/oppl/runtime.py` | evaluation: cache, `me_st_` memory, `user` variables, `hand$`/`board$`, lists |
| `ohsim/engine.py` | game engine: blinds, minimum raise, side pots, showdown, rebuys |
| `ohsim/symbols.py` | OpenHoldem's own symbols (chips, positions, cards, pokerval, prwin, handrank, history) |
| `ohsim/stats.py`, `ptrows.py`, `ptstats.py` | PokerTracker `pt_` symbols, computed with OpenHoldem's own SQL queries on an in-memory SQLite copy |
| `ohsim/bot.py` | profile life cycle and translation of the decision (as OpenHoldem's autoplayer) |

`ohsim/ptdb.py` and `tools/pt_*.py` read a local PokerTracker 4 database (read-only, using
PokerTracker's own connection settings) to validate the `pt_` statistics and to measure a real population.

`[table] max_stack` (default 3 x buy-in): a player above this stack leaves and sits down again
with a buy-in (the difference is accounted), as at real tables.

## Tests

    python -m unittest tests.test_oppl tests.test_engine

## License

GPL v3, like OpenHoldem.
