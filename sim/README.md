# ohsim - simulatore cash NLHE per profili OpenPPL

Fa giocare tra loro profili OpenHoldem/OpenPPL (`.ohf`, anche in modalita' Shanky)
su un tavolo No-Limit Hold'em cash da 2 a 9 posti.

## Uso

    cd C:\Python313\AAA-BOTS\oh\sim
    python -m ohsim.run examples\rev024_vs_default_6max.toml          # mani dal file
    python -m ohsim.run examples\rev024_vs_default_6max.toml 5000     # 5000 mani
    python -m ohsim.run examples\rev024_x6.toml 100000 -j 8           # 8 processi (default: tutti i core)

Le mani vengono divise in blocchi giocati in parallelo, ognuno su un tavolo
indipendente con il proprio seed. `-j 1` = un solo processo.
Velocita' indicative (16 core): 6x Rev024 ~110.000 mani/min, 1 core ~13.000.
I profili sono compilati in funzioni Python (`_CodeGen` in `runtime.py`).

Il file `.toml` definisce tavolo (blind, ante, buy-in, mani, seed) e giocatori.
`profile = ""` = solo bot predefinito di OpenHoldem (Gecko NL 6-max).

## Struttura

| File | Ruolo |
|---|---|
| `ohsim/oppl/tokenizer.py`, `parser.py` | lettura dei profili, stesse regole di OpenHoldem (precedenze, WHEN, SET, Shanky) |
| `ohsim/oppl/runtime.py` | valutazione: cache, memoria `me_st_`, variabili `user`, `hand$`/`board$`, liste, suffissi `_chair` |
| `ohsim/engine.py` | motore di gioco: blind, puntate, raise minimo, side pot, showdown, ricarica stack |
| `ohsim/symbols.py` | simboli interni di OpenHoldem (chip, posizioni, carte, pokerval, prwin, handrank, storico) |
| `ohsim/stats.py` | statistiche tipo PokerTracker dalle mani simulate (`pt_vpip3`, `pt_pfr_raischair`, ...) |
| `ohsim/bot.py` | ciclo di vita del profilo e traduzione della decisione (come `CAutoplayerFunctions`) |
| `ohsim/handrank.json`, `oh_symbols.txt` | tabelle estratte dai sorgenti di OpenHoldem (`tools/`) |

## Stato

- Parser verificato sulla libreria OpenPPL (25 file), sui bot predefiniti e su Rev023/Rev024:
  trova gli stessi errori di OpenHoldem.
- Simboli implementati: quelli usati da libreria, bot predefiniti e Rev024
  (i mancanti vengono elencati a fine partita e valgono 0).
- NON ancora validato contro OpenHoldem decisione per decisione (prossima fase).

## Test

    python -m unittest tests.test_oppl tests.test_engine

## GTO (preflop)

    python -m ohsim.gto.equity 100000     # tabella equity 169x169 (una volta, ~6 min su 16 core)
    python -m ohsim.gto.pushfold 10       # push/fold heads-up, Nash esatto (verifica del solver)
    python -m ohsim.gto.preflop6 3000     # preflop 6-max 100bb, ~50 s -> gto/data/preflop6_100bb.json

| File | Ruolo |
|---|---|
| `gto/hands.py` | 169 classi di mano, combo, pesi per le carte bloccate |
| `gto/npeval.py` | valutatore 7 carte vettoriale (numpy), identico a `handeval.score` |
| `gto/equity.py` | tabella equity Monte Carlo |
| `gto/cfr.py`, `gto/pushfold.py` | CFR+ a 2 giocatori, push/fold |
| `gto/preflop6.py` | CFR+ a 6 giocatori; modello e parametri descritti in testa al file |

Il modello preflop approssima il postflop con l'equity "realizzata"
(`REALIZATION`, `PLAY`, `STRENGTH_*`): sono i parametri da tarare.

## Storici delle mani, statistiche, avversari con leak

    python -m ohsim.run examples\rev025_vs_rev024.toml 20000 --hh hh\prova   # storici formato PokerStars (PokerTracker 4)
    python tools\check_hh.py hh\prova                                         # controllo coerenza piatti
    python -m ohsim.leaks C:\Users\USER\Desktop\Rev024.ohf profiles\leaks      # genera Leak_nit/station/maniac/folder/nobluff
    python -m ohsim.run examples\leak_station_vs_base.toml 100000

Ogni partita stampa: risultati, report preflop per posizione, risultato per linea preflop,
statistiche postflop (c-bet, fold alla c-bet, WTSD, W$SD, AF).
I leak (ampiezza e probabilita') si regolano in `ARCHETYPES` / `postflop_layer` di `ohsim/leaks.py`.

## PokerTracker: statistiche con le definizioni di OpenHoldem

Il simulatore calcola i simboli `pt_...` esattamente come OpenHoldem: per ogni mano produce
le colonne di `cash_hand_player_statistics` di PokerTracker 4 (`ohsim/ptrows.py`) e applica
le query SQL di OpenHoldem (`PokerTracker_Queries_Version_4.h`, estratte in
`ohsim/pt_queries.json`) su una copia SQLite in memoria (`ohsim/ptstats.py`).
Senza dati una statistica vale -1, una statistica non definita in OpenHoldem vale -1 ed e' segnalata.

Verifica sulle mani reali del database PokerTracker locale (sola lettura, `ohsim/ptdb.py`):

    python tools\validate_ptrows.py 13086 3,6,13     # colonne per mano: ohsim vs PokerTracker
    python tools\validate_ptstats.py 13086 3,6,13    # statistiche: SQLite ohsim vs PostgreSQL PokerTracker

Popolazione reale e tipi di giocatore:

    python tools\pt_population.py 150        # distribuzione delle statistiche per limite
    python tools\pt_clusters.py 100          # tipi reg / nit / lag / fish / maniac
    python -m ohsim.population calibrate 6 50000   # bot tarati sui tipi reali -> profiles\population

| File | Ruolo |
|---|---|
| `ohsim/handlog.py` | lettura storici PokerStars, rappresentazione comune mani reali / simulate |
| `ohsim/ptrows.py` | colonne per mano di PokerTracker |
| `ohsim/ptstats.py` | statistiche `pt_` nel simulatore (query OpenHoldem su SQLite) |
| `ohsim/ptdb.py` | accesso in sola lettura al database PokerTracker |
| `ohsim/population.py` | bot dei tipi di giocatore reali, taratura automatica |

Nota: `[table] max_stack` (default 3 x buyin): chi supera questo stack "si rialza" e si risiede
con il buyin (l'eccedenza e' contabilizzata), come ai tavoli veri; evita mani da migliaia di bb.
Confronto con la popolazione reale: `examples\rev024_vs_population.toml`, `examples\rev025_vs_population.toml`.

## GTO flop: BTN vs BB single-raised pot (`ohsim.gto.flop`)

Solver: TexasSolver (open source) in `oh/solver/TexasSolver-v0.2.0-Windows`
(or set `TEXASSOLVER_DIR`). Ranges = our preflop solution (BTN open, BB call),
pot 5.5bb, stacks 97.5bb, bets flop 33/75%, turn 66%, river 75%, raises 60%.

    python -m ohsim.gto.flop pick 6        # 6 flops per texture (48), stratified
    python -m ohsim.gto.flop solve 16      # TexasSolver, 16 threads; skips flops already solved
    python -m ohsim.gto.flop table         # frequencies per decision x texture x hand class
    python -m ohsim.gto.flop build C:/Users/USER/Desktop/Rev024.ohf C:/Users/USER/Desktop/Rev026.ohf

Hand classes and textures are evaluated by the OpenPPL library itself (same expressions
as the generated rules). Situations: BB first to act, BTN after check, BB vs small/big
c-bet, BTN vs check-raise, BTN vs lead. Detected with `potcommon < 10 * bblind` (single
raised pot), `Position`, `BotsLastPreflopAction`; deeper nodes, turn and river stay
with the base profile.
